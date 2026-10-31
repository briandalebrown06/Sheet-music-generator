# Paginated score PDF export

The PDF endpoint engraves the **saved canonical MusicXML**. It does not print the application, rasterize the score panel, or treat MIDI as finished notation. The selected engine is native [Verovio 6.3.0](https://pypi.org/project/verovio/6.3.0/), a headless MusicXML/MEI engraver. Verovio chooses system and page breaks, then [svglib](https://pypi.org/project/svglib/2.3.2/) and [ReportLab](https://docs.reportlab.com/install/open_source_installation/) place each complete engraved page's vector paths onto A4 PDF pages. Notes remain vector graphics. Page numbers and the saved revision appear in the footer.

## Installation and use

Use the same Python 3.11 virtual environment as the API:

```sh
.venv/bin/python -m pip install -r backend/requirements-engraving.txt
.venv/bin/python -m pip check
.venv/bin/python -m pytest backend/tests/test_pdf_export.py -q
```

The installed wheel path was verified on macOS arm64 with Python 3.11.18. The pinned Verovio wheel requires Python 3.10 or later and its macOS arm64 wheel targets macOS 13 or later. Linux and Windows installation/printing have not been exercised here. No system engraving executable, desktop preferences, graphical session or network service is required.

Save the review, then download `GET /api/projects/{id}/score/exports/pdf?revision=N`. The optional revision query rejects a mismatched revision with HTTP 409. All canonical exports include `X-Score-Revision`, `X-Notes-Revision` and `Cache-Control: no-store`. PDF and MIDI also recheck revisions after generation; edits made while an export is running reject that result. Outputs are generated on demand, so there is no stale export cache. Raw-performance MIDI remains a separate artifact.

Missing dependencies return an actionable HTTP 503. The health capability is installation availability, not a guarantee that every complex score can be engraved. Rendering failure returns 422; wall-clock timeout returns 504. MusicXML remains available for further editing in notation software.

## Limits and isolation

Each request creates a private server-named directory below `DATA_DIR`, writes only the saved XML, and starts a separate Python process using an argument array and no shell. The process has closed standard input and bounded logs/files; there is no `preexec_fn` in the threaded API server. Every success, rendering failure and timeout removes temporary files. Timeouts kill the process group on POSIX.

- `PDF_TIMEOUT_SECONDS`: wall-clock and CPU bound, default 120 seconds, allowed 1–600.
- `PDF_MEMORY_MB`: Linux address-space limit, default 1024 MB, allowed 256–4096. macOS does not provide a reliable `RLIMIT_AS`; no hard aggregate RAM limit is claimed there.
- Input XML: 10 MB. One page's SVG: 10 MB. Output PDF/log file: 32 MB each. Maximum 100 pages.
- POSIX also limits file descriptors and disables core dumps. Limits on Windows are input/page/output bounds and the subprocess timeout; POSIX resource limits are not claimed there.
- Engraved vector bounds must remain inside an 18 pt printable margin. Output is also checked for a complete PDF signature/trailer, parsed with pypdf in strict mode, checked for nonempty pages, and restricted to A4 page boxes.

The SVG converter has two explicit compatibility adjustments: an equivalent group transform flattens Verovio's nested SVG viewport to prevent a second px-to-point scaling, and the metronome's web-font glyph becomes the text `Quarter = …`. The score compiler uses quarter-note BPM, so this preserves the tempo meaning. All note, rest, clef, accidental and triplet graphics remain the engraver's outlines. Headers use a standard PDF font; comprehensive international title-glyph support has not been tested.

## Verification on 2026-10-27

Actual production-helper and API tests cover real native engraving, A4 bounds, vector-only pages, footer revision/page numbers, missing dependencies, oversized input, invalid PDF bytes, corrupt XML, a real one-second subprocess timeout and cleanup (a controlled sleeping process makes this deterministic), stale saved notes, and note/score edits during engraving. A requested stale revision is rejected for MusicXML, notation MIDI, canonical JSON and PDF.

The reproducible `pagination_fixture()` in `backend/tests/test_pdf_export.py` generates 48 bars with upper triplets, deliberate rests, overlapping bass voices, ties across barlines and a heuristic piano staff split. These are **development fixtures, not transcription or accuracy results**. The resulting 3-page PDF was rendered with Poppler and all three pages, including the final system, were visually reviewed. Staves and tuplets are readable; systems and page margins are intact. Raster ink bounds on every page leave approximately 43 pt left/right, 21 pt top and 25 pt bottom, including headings and footers. No embedded bitmap images appear in the PDFs. A single-note score was also exported through the actual API.

Local review artifacts (ignored by Git) are `output/pdf/review-piano.pdf`, `output/pdf/review-piano.musicxml` and `output/pdf/review-piano-{1,2,3}.png`. These can be reproduced by passing `score_to_musicxml(pagination_fixture())` to `engrave_pdf` and rasterizing all output pages with `pdftoppm`. Physical paper printing and exhaustive pathological polyphonic scores were not tested. A valid canonical score may still need musical review or a coarser grid to produce useful notation.

## Engine evaluation and licenses

[Current Verovio layout options](https://book.verovio.org/toolkit-reference/toolkit-options.html) and [automatic page layout](https://book.verovio.org/first-steps/layout-options.html) were checked before selecting options and versions. Code and font assets were checked separately:

| Component | Installed version | License / scope |
| --- | --- | --- |
| Verovio native engraver | 6.3.0 | LGPL-3.0-only, installed wheel `COPYING` / `COPYING.LESSER` and [official source](https://github.com/rism-digital/verovio) |
| Leipzig music glyphs | Bundled with Verovio | [SIL Open Font License 1.1](https://github.com/rism-digital/leipzig); these are font assets, not the engraver's code license |
| svglib | 2.3.2 | LGPL-3.0-or-later, installed license and [official source](https://github.com/deeplook/svglib) |
| ReportLab | 5.1.0 | BSD, installed package license |
| pypdf | 6.21.2 | BSD-3-Clause, installed package license |

These are separately installed dependencies, not vendored code or model weights. Retain their license notices when distributing dependencies. The transcription model's code/weights are covered separately in the project's dependency audit.

MuseScore Studio was evaluated as an alternative. The installed `/Applications/MuseScore 4.app/Contents/MacOS/mscore` reports version 4.7.5. Its real `--help`, [official command-line handbook](https://handbook.musescore.org/appendix/command-line-usage) and [4.7.5 argument parser](https://github.com/musescore/MuseScore/blob/v4.7.5/src/app/internal/commandlineparser.cpp) confirm `-o file.pdf`. Its [code license is GPLv3 with the documented font exception](https://github.com/musescore/MuseScore/blob/v4.7.5/LICENSE.txt). Current CLI flags differ from older handbook examples; obsolete `--config-folder` / `--no-synthesizer` flags were not used. The local macOS package exposes only the Cocoa Qt platform, so `offscreen` cannot run. Actual PDF conversion wrote a parseable document but then aborted during shutdown with `std::system_error: mutex lock failed: Invalid argument`, including when explicitly launched as arm64. The underlying system-library cause was not resolved. The API therefore uses the verified Verovio path and does not depend on MuseScore or modify its preferences.

## Accompaniment readability verification

Derived accompaniment uses a fixed 7.6 mm engraved staff, increased staff/system spacing, smart measure-break guides and at most six systems per page. The existing 77-bar regression expands from a crowded four-page single staff to a ten-page piano grand staff. Every final page was rendered and visually inspected on 2026-10-27; the earlier redundant rest stacks and extreme final ledger column were repaired in the canonical model. This does not establish pianist playability. See [PIANO_ACCOMPANIMENT.md](PIANO_ACCOMPANIMENT.md) for remaining passage warnings and reproducible comparisons.
