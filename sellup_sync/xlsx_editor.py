"""Surgical cell editing that leaves the rest of the workbook byte-identical.

An ``.xlsx`` is a zip of XML parts. Loading one with openpyxl and saving it
rewrites every part, and that round-trip is **not** lossless for the SellUp
template: cells holding an empty shared string come back as truly empty, which
changes 28,000 cells in a file where only a few hundred should move.

Functionally ``''`` and empty are the same to SellUp, but "leave everything
else untouched" is a hard requirement here, so this module edits the sheet XML
directly and copies every other zip entry across verbatim.

A cell is rewritten from::

    <c r="G4" s="11" t="s"><v>22</v></c>      (empty shared string)

to::

    <c r="G4" s="11"><v>21</v></c>            (number, same style)

The style index ``s`` is preserved, so formatting is unchanged.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree

_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"

# Matches a whole <c> element, self-closing or not. Cell XML never contains
# newlines in files produced by Excel or by the SellUp portal.
_CELL_RE = re.compile(rb'<c r="([A-Z]+[0-9]+)"([^>]*?)(?:/>|>(.*?)</c>)')
_STYLE_RE = re.compile(rb's="(\d+)"')


class XlsxEditError(Exception):
    """Raised when the workbook cannot be edited safely."""


@dataclass
class EditReport:
    """What the editor changed."""

    cells_written: int = 0
    cells_missing: list[str] = field(default_factory=list)
    parts_rewritten: list[str] = field(default_factory=list)
    parts_copied: int = 0


def _column_letter(index: int) -> str:
    """1-based column index to its spreadsheet letter."""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def cell_ref(row: int, column: int) -> str:
    """``(4, 7)`` becomes ``'G4'``."""
    return f"{_column_letter(column)}{row}"


def _sheet_part_map(archive: zipfile.ZipFile) -> dict[str, str]:
    """Map worksheet display names to their part names inside the zip.

    Excel does not guarantee that ``Smartphones`` lives in ``sheet1.xml``, so
    the workbook relationships have to be followed rather than assumed.
    """
    workbook_xml = archive.read("xl/workbook.xml")
    rels_xml = archive.read("xl/_rels/workbook.xml.rels")

    rel_targets: dict[str, str] = {}
    for relationship in ElementTree.fromstring(rels_xml):
        rel_id = relationship.get("Id")
        target = relationship.get("Target", "")
        if not rel_id or not target:
            continue
        if target.startswith("/"):
            target = target.lstrip("/")
        elif not target.startswith("xl/"):
            target = f"xl/{target}"
        rel_targets[rel_id] = target

    mapping: dict[str, str] = {}
    root = ElementTree.fromstring(workbook_xml)
    sheets = root.find(f"{{{_NS_MAIN}}}sheets")
    if sheets is None:
        raise XlsxEditError("workbook.xml contains no <sheets> element.")

    for sheet in sheets:
        name = sheet.get("name")
        rel_id = sheet.get(f"{{{_NS_REL}}}id")
        if name and rel_id and rel_id in rel_targets:
            mapping[name] = rel_targets[rel_id]

    if not mapping:
        raise XlsxEditError("Could not resolve any worksheet parts.")
    return mapping


def _rewrite_sheet(
    xml: bytes,
    values: dict[str, int],
    report: EditReport,
) -> bytes:
    """Replace the target cells inside one sheet's XML in a single pass."""
    seen: set[str] = set()

    def replace(match: re.Match) -> bytes:
        ref = match.group(1).decode("ascii")
        if ref not in values:
            return match.group(0)

        seen.add(ref)
        attrs = match.group(2) or b""
        style = _STYLE_RE.search(attrs)
        style_attr = b' s="' + style.group(1) + b'"' if style else b""
        number = str(int(values[ref])).encode("ascii")
        report.cells_written += 1
        # No t attribute means "number", which is exactly what a Qty cell is.
        return b'<c r="' + ref.encode("ascii") + b'"' + style_attr + b"><v>" + number + b"</v></c>"

    result = _CELL_RE.sub(replace, xml)

    missing = set(values) - seen
    if missing:
        # Every SellUp Qty cell exists in the template, so a missing reference
        # means the row index was computed wrongly -- worth failing loudly.
        report.cells_missing.extend(sorted(missing))

    return result


def write_cells(
    source: bytes,
    edits: dict[str, dict[str, int]],
) -> tuple[bytes, EditReport]:
    """Apply ``{sheet_name: {cell_ref: value}}`` to a workbook.

    Returns the new file bytes and a report. Every zip entry other than the
    edited sheets is copied across without being re-encoded.
    """
    report = EditReport()
    buffer = io.BytesIO()

    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        part_map = _sheet_part_map(archive)

        unknown = set(edits) - set(part_map)
        if unknown:
            raise XlsxEditError(
                f"Worksheet(s) not found in the workbook: {sorted(unknown)}"
            )

        targets = {
            part_map[sheet]: values for sheet, values in edits.items() if values
        }

        # ZIP_DEFLATED matches what Excel and openpyxl produce.
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as output:
            for item in archive.infolist():
                data = archive.read(item.filename)
                if item.filename in targets:
                    data = _rewrite_sheet(data, targets[item.filename], report)
                    report.parts_rewritten.append(item.filename)
                else:
                    report.parts_copied += 1
                # Preserve the original entry metadata (date, external attrs).
                info = zipfile.ZipInfo(item.filename, date_time=item.date_time)
                info.compress_type = item.compress_type
                info.external_attr = item.external_attr
                info.internal_attr = item.internal_attr
                info.create_system = item.create_system
                output.writestr(info, data)

    if report.cells_missing:
        raise XlsxEditError(
            "These target cells do not exist in the template: "
            + ", ".join(report.cells_missing[:20])
        )

    return buffer.getvalue(), report


def compare_archives(original: bytes, produced: bytes) -> list[str]:
    """Confirm that only the intended sheet parts differ between two files.

    Every other zip entry must be byte-identical. This is a much stronger
    guarantee than comparing cell values.
    """
    problems: list[str] = []
    with zipfile.ZipFile(io.BytesIO(original)) as a, zipfile.ZipFile(io.BytesIO(produced)) as b:
        names_a = [i.filename for i in a.infolist()]
        names_b = [i.filename for i in b.infolist()]
        if names_a != names_b:
            problems.append(
                f"Zip entry list changed.\n  before: {names_a}\n  after:  {names_b}"
            )
            return problems

        for name in names_a:
            if a.read(name) != b.read(name):
                problems.append(name)

    return problems
