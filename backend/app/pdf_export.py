"""Bounded native engraving of saved canonical MusicXML into vector PDF pages.

Verovio supplies music layout, including system and page breaks. svglib and
ReportLab place its vector paths on real A4 PDF pages; no browser screenshot is
involved. Native dependencies run in a disposable subprocess, never API threads.
"""
from __future__ import annotations

import importlib.util
import io
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

logger = logging.getLogger(__name__)
MAX_XML_BYTES = 10 * 1024 * 1024
MAX_PDF_BYTES = 32 * 1024 * 1024
MAX_PAGES = 100
DEPENDENCIES = ("verovio", "svglib", "reportlab", "pypdf")


class PdfExportError(Exception):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


def pdf_capability() -> dict:
    missing = [name for name in DEPENDENCIES if importlib.util.find_spec(name) is None]
    return {"available": not missing, "engine": "Verovio", "format": "vector PDF",
            "missing": missing,
            "detail": "Install backend/requirements-engraving.txt in the API environment." if missing
            else "On-demand engraving of saved MusicXML into paginated A4 scores."}


def validate_pdf(content: bytes) -> int:
    from pypdf import PdfReader

    if not content.startswith(b"%PDF-") or not content.rstrip().endswith(b"%%EOF"):
        raise ValueError("Engraver did not produce a complete PDF.")
    reader = PdfReader(io.BytesIO(content), strict=True)
    if reader.is_encrypted or not 1 <= len(reader.pages) <= MAX_PAGES:
        raise ValueError("Engraver produced an unsupported page count or encrypted document.")
    for page in reader.pages:
        if abs(float(page.mediabox.width) - 595.276) > 1 or abs(float(page.mediabox.height) - 841.89) > 1:
            raise ValueError("Engraver produced a page outside the configured A4 bounds.")
        contents = page.get_contents()
        if contents is None or not contents.get_data():
            raise ValueError("Engraver produced an empty page.")
    return len(reader.pages)


def engrave_pdf(musicxml: str, settings, revision: int) -> bytes:
    capability = pdf_capability()
    if not capability["available"]:
        raise PdfExportError(capability["detail"], 503)
    encoded = musicxml.encode("utf-8")
    if not encoded or len(encoded) > MAX_XML_BYTES:
        raise PdfExportError("This score exceeds the 10 MB PDF engraving input limit. Use a shorter clip.")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="score-pdf-", dir=settings.data_dir) as directory:
        source = Path(directory) / "score.musicxml"
        output = Path(directory) / "score.pdf"
        source.write_bytes(encoded)
        command = [sys.executable, str(Path(__file__).resolve()), "--render", str(source), str(output),
                   str(revision), str(settings.pdf_timeout_seconds), str(settings.pdf_memory_mb)]
        with (Path(directory) / "engraver.log").open("wb") as log:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       start_new_session=os.name == "posix", cwd=directory)
            try:
                process.wait(timeout=settings.pdf_timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait()
                raise PdfExportError("PDF engraving reached its time limit. Try a shorter clip or simpler quantization.", 504) from exc
        if process.returncode != 0 or not output.is_file():
            # Native diagnostics can contain score titles or XML fragments.
            # Keep them only in this private temporary directory, never app logs.
            logger.error("PDF engraving failed (exit %s)", process.returncode)
            raise PdfExportError("The saved score could not be engraved as PDF. See the API log for details; MusicXML is available for notation software.")
        if output.stat().st_size > MAX_PDF_BYTES:
            raise PdfExportError("The engraved PDF exceeds the 32 MB output limit. Use a shorter clip.")
        content = output.read_bytes()
        try:
            validate_pdf(content)
        except Exception as exc:
            logger.exception("PDF validation failed")
            raise PdfExportError("The engraver did not produce a complete, valid PDF.") from exc
        return content


def _printable_svg(svg: str) -> bytes:
    """Preserve Verovio's geometry through svglib's SVG viewport/text support.

    svglib applies CSS's px-to-pt conversion a second time to nested SVG viewports.
    Flattening Verovio's known viewport into an equivalent group avoids that
    shrinking. SMuFL metronome text uses an embedded web font unavailable to the
    PDF converter; render the same quarter-note tempo using ordinary text.
    Music glyphs themselves are already outlined vector paths.
    """
    from lxml import etree

    namespace = "{http://www.w3.org/2000/svg}"
    root = etree.fromstring(svg.encode(), parser=etree.XMLParser(resolve_entities=False, no_network=True))
    width = float(root.get("width").removesuffix("px"))
    height = float(root.get("height").removesuffix("px"))
    for child in root:
        if child.tag == namespace + "svg":
            x, y, nested_width, nested_height = map(float, child.attrib.pop("viewBox").split())
            if x or y:
                raise ValueError("Unexpected engraving viewport origin.")
            child.tag = namespace + "g"
            child.set("transform", f"scale({width / nested_width},{height / nested_height})")
    for group in root.iter(namespace + "g"):
        if group.get("class") == "tempo":
            for text in list(group.findall(namespace + "text")):
                label = "".join(text.itertext()).replace("\ueca5", "Quarter")
                label = " ".join(label.split())
                if any(0xE000 <= ord(c) <= 0xF8FF for c in label):
                    raise ValueError("Unsupported metronome symbol in canonical score.")
                position = {"x": text.get("x", "0"), "y": text.get("y", "0"),
                            "font-size": "405px", "font-family": "Times"}
                replacement = etree.Element(namespace + "text", attrib=position)
                replacement.text = label
                group.replace(text, replacement)
    return etree.tostring(root)


def _render(source: Path, output: Path, revision: int) -> None:
    import verovio
    from lxml import etree
    from reportlab.graphics import renderPDF
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen.canvas import Canvas
    from svglib.svglib import svg2rlg

    xml = source.read_text()
    document = etree.fromstring(xml.encode(), parser=etree.XMLParser(resolve_entities=False, no_network=True))
    title = document.findtext("work/work-title") or document.findtext("movement-title") or "Transcription"
    toolkit = verovio.toolkit()
    creator = document.findtext("identification/creator", "")
    readable = any(label in creator for label in ("Derived piano accompaniment", "Singer's piano accompaniment",
                   "Solo piano arrangement", "Faithful instrumental transcription"))
    toolkit.setOptions({"pageWidth": 2100, "pageHeight": 2970, "pageMarginTop": 220,
                        "pageMarginBottom": 180, "pageMarginLeft": 150, "pageMarginRight": 150,
                        "scale": 100, "scaleToPageSize": True, "breaks": "auto",
                        "adjustPageHeight": False, "header": "none", "footer": "none",
                        "svgViewBox": False,
                        **({"unit": 9.5, "spacingStaff": 16, "spacingBraceGroup": 16,
                            "spacingSystem": 10, "spacingLinear": 0.3, "spacingNonLinear": 0.65,
                            "breaks": "smart", "breaksSmartSb": 0.5, "systemMaxPerPage": 6,
                            "condense": "none"} if readable else {})})
    if not toolkit.loadData(xml):
        raise ValueError("Verovio could not import the saved MusicXML.")
    pages = toolkit.getPageCount()
    if not 1 <= pages <= MAX_PAGES:
        raise ValueError(f"PDF export supports 1–{MAX_PAGES} pages; this score has {pages}.")
    canvas = Canvas(str(output), pagesize=A4, pageCompression=1)
    canvas.setTitle(title)
    canvas.setAuthor("Scorecraft")
    canvas.setCreator(f"Scorecraft / Verovio {toolkit.getVersion()} / saved score revision {revision}")
    header = title.replace("\n", " ")
    while stringWidth(header, "Helvetica", 12) > A4[0] - 86:
        header = header[:-2].rstrip("…") + "…"
    for number in range(1, pages + 1):
        svg = toolkit.renderToSVG(number)
        if len(svg.encode()) > MAX_XML_BYTES:
            raise ValueError("An engraved page exceeded the vector complexity limit.")
        drawing = svg2rlg(io.BytesIO(_printable_svg(svg)))
        if drawing is None or drawing.width <= 0 or drawing.height <= 0:
            raise ValueError("Could not convert engraved page vectors.")
        scale_x, scale_y = A4[0] / drawing.width, A4[1] / drawing.height
        bounds = drawing.getBounds()
        if bounds is None or (bounds[0] * scale_x < 18 or bounds[1] * scale_y < 18
                              or bounds[2] * scale_x > A4[0] - 18 or bounds[3] * scale_y > A4[1] - 18):
            raise ValueError("The engraving exceeds printable page margins. Simplify the score or use MusicXML in notation software.")
        drawing.scale(scale_x, scale_y)
        renderPDF.draw(drawing, canvas, 0, 0)
        canvas.setFont("Helvetica", 12)
        canvas.drawString(43, A4[1] - 30, header)
        canvas.setFont("Helvetica", 8)
        canvas.drawString(43, 25, f"Saved score revision {revision}")
        canvas.drawRightString(A4[0] - 43, 25, f"{number} / {pages}")
        canvas.showPage()
    canvas.save()
    validate_pdf(output.read_bytes())


def _main() -> None:
    # No preexec_fn: limits are set in this disposable child before native imports.
    source, output = Path(sys.argv[2]), Path(sys.argv[3])
    revision, timeout, memory = map(int, sys.argv[4:7])
    if os.name == "posix":
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_PDF_BYTES, MAX_PDF_BYTES))
        _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        descriptors = 64 if hard == resource.RLIM_INFINITY else min(64, hard)
        resource.setrlimit(resource.RLIMIT_NOFILE, (descriptors, descriptors))
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (memory * 1024 ** 2, memory * 1024 ** 2))
    _render(source, output, revision)


if __name__ == "__main__":
    _main()
