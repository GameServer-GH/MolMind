#!/usr/bin/env python3
"""Validate the frozen HepG2-FFA registry and optional SSBD archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.evidence_facade.hepg2_ffa_resources import (  # noqa: E402
    load_hepg2_ffa_resource_registry,
)


SSBD_ACCESSION = "SSBD:dataset-12051"
XML_NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _cell_text(cell: ElementTree.Element, shared: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    value = cell.find("main:v", XML_NS)
    if value is None or value.text is None:
        inline = cell.find("main:is/main:t", XML_NS)
        return "" if inline is None or inline.text is None else inline.text
    if cell_type == "s":
        return shared[int(value.text)]
    return value.text


def _xlsx_summary(raw: bytes) -> dict[str, object]:
    from io import BytesIO

    with zipfile.ZipFile(BytesIO(raw)) as book:
        required = {
            "xl/workbook.xml",
            "xl/worksheets/sheet1.xml",
            "xl/worksheets/sheet2.xml",
        }
        missing = sorted(required - set(book.namelist()))
        if missing:
            raise ValueError(f"SSBD XLSX missing files: {missing}")
        shared: list[str] = []
        if "xl/sharedStrings.xml" in book.namelist():
            root = ElementTree.fromstring(book.read("xl/sharedStrings.xml"))
            for item in root.findall("main:si", XML_NS):
                shared.append("".join(node.text or "" for node in item.findall(".//main:t", XML_NS)))
        sheets: list[dict[str, object]] = []
        for index in (1, 2):
            root = ElementTree.fromstring(book.read(f"xl/worksheets/sheet{index}.xml"))
            dimension = root.find("main:dimension", XML_NS)
            dimension_ref = "" if dimension is None else dimension.attrib.get("ref", "")
            first_row = root.find("main:sheetData/main:row", XML_NS)
            headers = [] if first_row is None else [_cell_text(c, shared) for c in first_row]
            sheets.append(
                {
                    "sheet_index": index,
                    "dimension": dimension_ref,
                    "header_count": len(headers),
                    "headers": headers,
                }
            )
    return {"xlsx_sha256": _sha256(raw), "sheets": sheets}


def _validate_ssbd_archive(path: Path, expected_archive_sha: str, expected_xlsx_sha: str) -> dict:
    archive_raw = path.read_bytes()
    archive_sha = _sha256(archive_raw)
    if archive_sha != expected_archive_sha:
        raise ValueError(f"SSBD archive SHA mismatch: {archive_sha}")
    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".xlsx")]
        if len(members) != 1:
            raise ValueError(f"expected one XLSX in SSBD archive, got {members}")
        xlsx_raw = archive.read(members[0])
    summary = _xlsx_summary(xlsx_raw)
    if summary["xlsx_sha256"] != expected_xlsx_sha:
        raise ValueError(f"SSBD XLSX SHA mismatch: {summary['xlsx_sha256']}")
    raw_headers = summary["sheets"][0]["headers"]
    conditions: dict[str, int] = {}
    for header in raw_headers[1:]:
        condition = re.sub(r"_P\d+$", "", str(header))
        conditions[condition] = conditions.get(condition, 0) + 1
    summary["replicates_by_condition"] = conditions
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--ssbd-archive", type=Path)
    args = parser.parse_args()

    registry = load_hepg2_ffa_resource_registry(args.registry)
    output: dict[str, object] = registry.to_runtime_dict()
    if args.ssbd_archive:
        ssbd = next(
            item for item in registry.resources if item.canonical_accession == SSBD_ACCESSION
        )
        output["ssbd_validation"] = _validate_ssbd_archive(
            args.ssbd_archive,
            str(ssbd.raw["archive_sha256"]),
            str(ssbd.raw["xlsx_sha256"]),
        )
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
