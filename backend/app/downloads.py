"""Portable download names from saved metadata, never filesystem paths."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote


# A signature does not establish major/minor mode. Name both alternatives rather
# than claiming that an assumed key signature is a detected musical key.
KEY_SIGNATURE_NAMES = (
    "C-flat-major-A-flat-minor", "G-flat-major-E-flat-minor",
    "D-flat-major-B-flat-minor", "A-flat-major-F-minor",
    "E-flat-major-C-minor", "B-flat-major-G-minor", "F-major-D-minor",
    "C-major-A-minor", "G-major-E-minor", "D-major-B-minor",
    "A-major-F-sharp-minor", "E-major-C-sharp-minor",
    "B-major-G-sharp-minor", "F-sharp-major-D-sharp-minor",
    "C-sharp-major-A-sharp-minor",
)


def safe_title(title: str) -> str:
    """Keep international titles while bounding bytes and removing path syntax."""
    value = unicodedata.normalize("NFKC", title)
    # Preserve explicit register direction in filenames instead of turning
    # both “Melody +1 octave” and “Melody -1 octave” into the same name.
    value = re.sub(r"(?<!\S)([+-])(\d+)(?=\s|$)",
                   lambda match: ("plus-" if match[1] == "+" else "minus-") + match[2], value)
    value = "".join(character if character.isalnum() or character in "-_" else "-"
                    for character in value)
    value = re.sub(r"[-_]+", "-", value).strip("-")
    # Leave room for key/revision/extension under common 255-byte filename caps.
    value = value.encode("utf-8")[:120].decode("utf-8", errors="ignore").rstrip("-")
    return value or "Untitled-recording"


def score_filename(saved: dict, format: str) -> str:
    score = saved["score"]
    signature = score["settings"]["key_signature"]
    key = KEY_SIGNATURE_NAMES[signature + 7]
    suffix = "mid" if format == "midi" else format
    return f'{safe_title(score["title"])}_{key}_score-r{saved["revision"]}.{suffix}'


def performance_filename(title: str, *, raw: bool, revision: int, job_id: str) -> str:
    if raw:
        # Raw events remain in the recording's original key and initial revision,
        # even after the saved score is corrected or transposed.
        return f"{safe_title(title)}_original-key_raw-performance-r1-{job_id[:8]}.mid"
    return f"{safe_title(title)}_original-key_edited-performance-notes-r{revision}.mid"


def attachment_header(filename: str) -> str:
    """RFC 6266 fallback plus RFC 5987 UTF-8 for non-ASCII song titles."""
    fallback = unicodedata.normalize("NFKD", filename).encode("ascii", errors="ignore").decode()
    fallback = re.sub(r'[^a-zA-Z0-9_.-]', "-", fallback)
    if filename.isascii():
        return f'attachment; filename="{fallback}"'
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(filename, safe="")}'
