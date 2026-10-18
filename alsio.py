"""Read and write Ableton Live .als files without reserializing the XML."""

from __future__ import annotations

import gzip
import io
import re
from pathlib import Path

KEYMIDI_RE = re.compile(r"<KeyMidi\b[^>]*>.*?</KeyMidi>", re.DOTALL)
TAG_RE = re.compile(r"<(/?)([A-Za-z0-9_.]+)([^>]*)>")


class AlsError(Exception):
    """The Live set could not be read or its MIDI mappings could not be updated."""


def load_als(path: Path) -> str:
    """Return the Live set XML. Accepts gzip-compressed .als files and raw XML."""
    data = Path(path).read_bytes()
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except OSError as exc:
            raise AlsError(f"Could not decompress {path.name}: {exc}") from exc
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AlsError(f"{path.name} is not UTF-8 Ableton XML.") from exc


def save_als(path: Path, xml_text: str) -> None:
    """Gzip the XML with no original filename stored in the header."""
    destination = Path(path)
    payload = gzip_xml(xml_text)
    destination.write_bytes(payload)


def gzip_xml(xml_text: str) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as archive:
        archive.write(xml_text.encode("utf-8"))
    return buffer.getvalue()


def default_save_path(source: Path) -> Path:
    source = Path(source)
    return source.with_name(f"{source.stem} remapped{source.suffix or '.als'}")


def keymidi_spans(xml_text: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in KEYMIDI_RE.finditer(xml_text)]


def replace_attr(block: str, tag: str, value: str) -> str:
    """Replace the first Value attribute of a direct child tag, keeping surrounding text."""
    pattern = re.compile(rf"(<{tag}\b[^>]*\bValue=\")(.*?)(\")")
    updated, count = pattern.subn(lambda match: match.group(1) + value + match.group(3), block, count=1)
    if count != 1:
        raise AlsError(f"Could not update <{tag}> in this Live set.")
    return updated


def sibling_range_span(xml_text: str, after: int) -> tuple[int, int] | None:
    """Return the MidiControllerRange that is a sibling following a KeyMidi block.

    Scanning stops at the parent close tag, so a later parameter's range is ignored.
    """
    position = after
    depth = 0
    while True:
        match = TAG_RE.search(xml_text, position)
        if match is None:
            return None
        is_close = match.group(1) == "/"
        name = match.group(2)
        rest = match.group(3)
        self_closing = rest.rstrip().endswith("/")
        position = match.end()
        if is_close:
            if depth == 0:
                return None
            depth -= 1
            continue
        if self_closing:
            continue
        if depth == 0 and name == "MidiControllerRange":
            close_at = xml_text.find("</MidiControllerRange>", match.start())
            if close_at < 0:
                return None
            return match.start(), close_at + len("</MidiControllerRange>")
        depth += 1
