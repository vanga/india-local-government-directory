#!/usr/bin/env python3
"""Fetch and parse official LGD Download Directory reports.

The LGD public site protects report downloads with CAPTCHA. This module keeps
that boundary explicit: it opens a session, saves the CAPTCHA image for a human
operator, accepts the typed answer, and posts the same form the browser uses.
"""

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import getpass
import html
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin
from xml.etree import ElementTree

import requests

BASE_URL = "https://lgdirectory.gov.in/"
DOWNLOAD_URL = urljoin(BASE_URL, "downloadDirectory.do")
CAPTCHA_URL = urljoin(BASE_URL, "captchaImage")

REPORT_BY_ENTITY = "villageofSpecificState@state"
REPORT_ALL_VILLAGES_INDIA = "allVillagesofIndia"
DEFAULT_DOWNLOAD_TYPE = "xls"

STATE_CODE_TO_NAME = {
    "28": "Andhra Pradesh",
    "36": "Telangana",
}


@dataclass(frozen=True)
class LgdVillageRow:
    state_code: str
    state_name: str
    district_code: str
    district_name: str
    subdistrict_code: str
    subdistrict_name: str
    village_code: str
    village_name: str
    village_version: str = ""
    local_language_name: str = ""
    village_category: str = ""
    village_status: str = ""
    hierarchy: str = ""
    census_2001_code: str = ""
    census_2011_code: str = ""
    pesa_status: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "state_code": self.state_code,
            "state_name": self.state_name,
            "district_code": self.district_code,
            "district_name": self.district_name,
            "subdistrict_code": self.subdistrict_code,
            "subdistrict_name": self.subdistrict_name,
            "village_code": self.village_code,
            "village_name": self.village_name,
            "village_version": self.village_version,
            "local_language_name": self.local_language_name,
            "village_category": self.village_category,
            "village_status": self.village_status,
            "hierarchy": self.hierarchy,
            "census_2001_code": self.census_2001_code,
            "census_2011_code": self.census_2011_code,
            "pesa_status": self.pesa_status,
        }


VILLAGE_CSV_FIELDS = list(LgdVillageRow("", "", "", "", "", "", "", "").as_dict().keys())


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._current_table: list[list[str]] | None = None
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._current_table = []
        elif tag == "tr" and self._current_table is not None:
            self._current_row = []
        elif tag in {"th", "td"} and self._current_row is not None:
            self._current_cell = []
        elif self._current_cell is not None and tag in {"br", "p", "div"}:
            self._current_cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"th", "td"} and self._current_cell is not None:
            self._current_row.append(_clean_cell("".join(self._current_cell)))
            self._current_cell = None
        elif tag == "tr" and self._current_table is not None and self._current_row is not None:
            if any(cell for cell in self._current_row):
                self._current_table.append(self._current_row)
            self._current_row = None
        elif tag == "table" and self._current_table is not None:
            if self._current_table:
                self.tables.append(self._current_table)
            self._current_table = None

    def handle_data(self, data: str) -> None:
        if self._current_cell is not None:
            self._current_cell.append(data)


def _clean_cell(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _normalize_header(value: str) -> str:
    normalized = re.sub(r"[()]", " ", value).lower()
    normalized = normalized.replace("-", " ")
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _header_index(headers: list[str], *candidates: str) -> int | None:
    normalized_headers = [_normalize_header(header) for header in headers]
    for candidate in candidates:
        candidate_norm = _normalize_header(candidate)
        for index, header in enumerate(normalized_headers):
            if header == candidate_norm:
                return index
        for index, header in enumerate(normalized_headers):
            if candidate_norm and candidate_norm in header:
                return index
    return None


def _get(row: list[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index].strip()


def _parse_hierarchy(hierarchy: str) -> tuple[str, str, str]:
    parts = [part.strip() for part in hierarchy.split("/") if part.strip()]
    state_name = district_name = subdistrict_name = ""
    for part in parts:
        match = re.match(r"^(.*?)\s*\((.*?)\)\s*$", part)
        if not match:
            continue
        name, kind = match.group(1).strip(), match.group(2).strip().lower()
        if "sub" in kind and "district" in kind:
            subdistrict_name = name
        elif "district" in kind:
            district_name = name
        elif "state" in kind:
            state_name = name
    return state_name, district_name, subdistrict_name


def parse_village_report(
    path: Path,
    *,
    state_code: str = "",
    state_name: str = "",
) -> list[LgdVillageRow]:
    """Parse an LGD village report saved as HTML/XLS/CSV."""
    content = path.read_bytes()
    if content.startswith(b"PK\x03\x04"):
        rows = _read_xlsx_rows(path)
    elif path.suffix.lower() == ".csv":
        text = content.decode("utf-8-sig", errors="ignore")
        rows = list(csv.reader(text.splitlines()))
    else:
        text = content.decode("utf-8-sig", errors="ignore")
        parser = _TableParser()
        parser.feed(text)
        tables = sorted(parser.tables, key=len, reverse=True)
        rows = tables[0] if tables else []

    if not rows:
        return []

    header_row_index = _find_header_row(rows)
    headers = rows[header_row_index]
    data_rows = rows[header_row_index + 1 :]

    state_code_idx = _header_index(headers, "State LGD Code", "State Code")
    state_name_idx = _header_index(headers, "State Name")
    district_code_idx = _header_index(headers, "District LGD Code", "District Code")
    district_name_idx = _header_index(headers, "District Name")
    subdistrict_code_idx = _header_index(headers, "Sub-District LGD Code", "Sub District LGD Code", "Sub-District Code", "Sub District Code", "Subdistrict Code")
    subdistrict_name_idx = _header_index(headers, "Sub-District Name", "Sub District Name", "Subdistrict Name")
    village_code_idx = _header_index(headers, "Village LGD Code", "Village Code")
    village_version_idx = _header_index(headers, "Village Version")
    village_name_idx = _header_index(headers, "Village Name")
    local_name_idx = _header_index(headers, "Village Name In Local Language", "Village Name In Local", "Local Language")
    village_category_idx = _header_index(headers, "Village Category")
    village_status_idx = _header_index(headers, "Village Status")
    hierarchy_idx = _header_index(headers, "Hierarchy")
    census_2001_idx = _header_index(headers, "Census 2001 Code")
    census_2011_idx = _header_index(headers, "Census2011 Code", "Census 2011 Code")
    pesa_idx = _header_index(headers, "Pesa Status")

    parsed: list[LgdVillageRow] = []
    for row in data_rows:
        if len(row) < 2:
            continue
        hierarchy = _get(row, hierarchy_idx)
        hierarchy_state, hierarchy_district, hierarchy_subdistrict = _parse_hierarchy(hierarchy)
        village_code = _clean_numeric_code(_get(row, village_code_idx))
        village_name = _get(row, village_name_idx)
        if not village_code or not village_name or village_code.lower() == "village lgd code":
            continue
        parsed.append(
            LgdVillageRow(
                state_code=_clean_numeric_code(_get(row, state_code_idx)) or state_code,
                state_name=_get(row, state_name_idx) or state_name or hierarchy_state,
                district_code=_clean_numeric_code(_get(row, district_code_idx)),
                district_name=_get(row, district_name_idx) or hierarchy_district,
                subdistrict_code=_clean_numeric_code(_get(row, subdistrict_code_idx)),
                subdistrict_name=_get(row, subdistrict_name_idx) or hierarchy_subdistrict,
                village_code=village_code,
                village_name=village_name,
                village_version=_clean_numeric_code(_get(row, village_version_idx)),
                local_language_name=_get(row, local_name_idx),
                village_category=_get(row, village_category_idx),
                village_status=_get(row, village_status_idx),
                hierarchy=hierarchy,
                census_2001_code=_clean_numeric_code(_get(row, census_2001_idx)),
                census_2011_code=_clean_numeric_code(_get(row, census_2011_idx)),
                pesa_status=_get(row, pesa_idx),
            )
        )
    return parsed


def _clean_numeric_code(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"\d+\.0", value):
        return value[:-2]
    return value


def _read_xlsx_rows(path: Path) -> list[list[str]]:
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(path) as workbook:
        shared_strings = _read_shared_strings(workbook, ns)
        sheet_names = [name for name in workbook.namelist() if name.startswith("xl/worksheets/sheet")]
        if not sheet_names:
            return []

        rows: list[list[str]] = []
        for _event, row_elem in ElementTree.iterparse(workbook.open(sheet_names[0]), events=("end",)):
            if row_elem.tag != f"{ns}row":
                continue
            row: list[str] = []
            for cell in row_elem.findall(f"{ns}c"):
                row.append(_read_xlsx_cell(cell, shared_strings, ns))
            rows.append(row)
            row_elem.clear()
        return rows


def _read_shared_strings(workbook: zipfile.ZipFile, ns: str) -> list[str]:
    if "xl/sharedStrings.xml" not in workbook.namelist():
        return []
    root = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
    strings: list[str] = []
    for item in root.findall(f"{ns}si"):
        strings.append(_clean_cell("".join(text.text or "" for text in item.iter(f"{ns}t"))))
    return strings


def _read_xlsx_cell(cell: ElementTree.Element, shared_strings: list[str], ns: str) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        inline = cell.find(f"{ns}is")
        if inline is None:
            return ""
        return _clean_cell("".join(text.text or "" for text in inline.iter(f"{ns}t")))

    value = cell.find(f"{ns}v")
    if value is None or value.text is None:
        return ""
    if cell_type == "s":
        return shared_strings[int(value.text)] if value.text.isdigit() else ""
    return _clean_cell(value.text)


def _find_header_row(rows: list[list[str]]) -> int:
    best_index = 0
    best_score = -1
    for index, row in enumerate(rows[:20]):
        normalized = " ".join(_normalize_header(cell) for cell in row)
        score = int("village" in normalized) + int("code" in normalized)
        score += int("district" in normalized) + int("hierarchy" in normalized)
        if score > best_score:
            best_score = score
            best_index = index
    return best_index


def write_village_csv(rows: Iterable[LgdVillageRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=VILLAGE_CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.as_dict())


def write_village_parquet(csv_path: Path, parquet_path: Path, metadata_path: Path | None = None) -> dict[str, object]:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise SystemExit("pyarrow is required to export parquet. Install the scraper dependencies first.") from exc

    rows: list[dict[str, str]] = []
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({field: row.get(field, "") for field in VILLAGE_CSV_FIELDS})

    summary = _summarize_rows(rows)
    summary.update(
        {
            "source_csv": str(csv_path),
            "source_csv_sha256": _sha256_file(csv_path),
            "generated_at": datetime.now(UTC).isoformat(),
            "source_url": DOWNLOAD_URL,
            "source_report": REPORT_ALL_VILLAGES_INDIA,
            "license_note": "Official public LGD download directory data. Attribute Local Government Directory, Government of India.",
            "columns": VILLAGE_CSV_FIELDS,
        }
    )

    metadata_bytes = {f"lgd.{key}".encode(): json.dumps(value, ensure_ascii=False).encode() for key, value in summary.items()}
    table = pa.Table.from_pylist(rows, schema=pa.schema([(field, pa.string()) for field in VILLAGE_CSV_FIELDS]))
    table = table.replace_schema_metadata(metadata_bytes)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, parquet_path, compression="zstd")
    summary["parquet"] = str(parquet_path)
    summary["parquet_sha256"] = _sha256_file(parquet_path)

    if metadata_path:
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def _summarize_rows(rows: list[dict[str, str]]) -> dict[str, object]:
    state_counts = collections.Counter(row["state_code"] for row in rows if row.get("state_code"))
    return {
        "row_count": len(rows),
        "state_count": len({row["state_code"] for row in rows if row.get("state_code")}),
        "district_count": len({(row["state_code"], row["district_code"]) for row in rows if row.get("district_code")}),
        "subdistrict_count": len({(row["state_code"], row["district_code"], row["subdistrict_code"]) for row in rows if row.get("subdistrict_code")}),
        "village_count": len({row["village_code"] for row in rows if row.get("village_code")}),
        "state_row_counts": dict(sorted(state_counts.items(), key=lambda item: item[0])),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_state_village_report(
    *,
    state_code: str,
    state_name: str,
    out_path: Path,
    captcha_path: Path,
    captcha_answer: str | None,
    download_type: str = DEFAULT_DOWNLOAD_TYPE,
) -> None:
    fetch_village_report(
        report_name=REPORT_BY_ENTITY,
        entity_code=state_code,
        state_name=state_name,
        out_path=out_path,
        captcha_path=captcha_path,
        captcha_answer=captcha_answer,
        download_type=download_type,
    )


def fetch_village_report(
    *,
    report_name: str,
    out_path: Path,
    captcha_path: Path,
    captcha_answer: str | None,
    entity_code: str = "",
    state_name: str = "",
    download_type: str = DEFAULT_DOWNLOAD_TYPE,
) -> None:
    session = requests.Session()
    session.headers.update({"User-Agent": "Assetly-LGD-Directory-Scraper/0.1 (+https://assetlyhq.com)", "Referer": BASE_URL})

    download_page = session.get(DOWNLOAD_URL, timeout=30)
    download_page.raise_for_status()
    token = _extract_csrf(download_page.text)

    captcha_response = session.get(CAPTCHA_URL, timeout=30)
    captcha_response.raise_for_status()
    captcha_path.parent.mkdir(parents=True, exist_ok=True)
    captcha_path.write_bytes(captcha_response.content)

    answer = captcha_answer
    if not answer:
        print(f"Saved CAPTCHA image to {captcha_path}")
        answer = getpass.getpass("Enter LGD CAPTCHA text: ").strip()
    if not answer:
        raise SystemExit("CAPTCHA answer is required.")

    payload = {
        "OWASP_CSRFTOKEN": token,
        "DDOption": "DFD",
        "downloadOption": "DFD",
        "rptFileName": report_name,
        "entityCodes": entity_code,
        "stateName": state_name,
        "districtName": "",
        "blockName": "",
        "downloadType": download_type,
        "captchaAnswer": answer,
    }
    response = session.post(DOWNLOAD_URL, params={"OWASP_CSRFTOKEN": token}, data=payload, timeout=120)
    response.raise_for_status()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(response.content)

    if _looks_like_captcha_error(response.text):
        raise SystemExit(f"Downloaded response looks like a CAPTCHA/form error. Saved it to {out_path}.")


def _extract_csrf(text: str) -> str:
    match = re.search(r'name="OWASP_CSRFTOKEN"\s+value="([^"]+)"', text)
    if not match:
        match = re.search(r"OWASP_CSRFTOKEN=([A-Z0-9-]+)", text)
    if not match:
        raise RuntimeError("Could not find OWASP_CSRFTOKEN in LGD page.")
    return match.group(1)


def _looks_like_captcha_error(text: str) -> bool:
    lowered = text.lower()
    return "captcha" in lowered and any(phrase in lowered for phrase in ("please enter", "invalid", "not matched", "shown above"))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    download = sub.add_parser("download-state-villages")
    download.add_argument("--state-code", required=True)
    download.add_argument("--state-name", default="")
    download.add_argument("--out", required=True, type=Path)
    download.add_argument("--captcha-path", default=Path("/private/tmp/lgd-captcha.png"), type=Path)
    download.add_argument("--captcha-answer", default=None)
    download.add_argument("--download-type", default=DEFAULT_DOWNLOAD_TYPE, choices=("xls", "htm", "pdf", "odt"))

    download_all = sub.add_parser("download-all-india-villages")
    download_all.add_argument("--out", required=True, type=Path)
    download_all.add_argument("--captcha-path", default=Path("/private/tmp/lgd-captcha.png"), type=Path)
    download_all.add_argument("--captcha-answer", default=None)
    download_all.add_argument("--download-type", default=DEFAULT_DOWNLOAD_TYPE, choices=("xls", "htm", "pdf", "odt"))

    parse = sub.add_parser("parse-villages")
    parse.add_argument("--input", required=True, type=Path)
    parse.add_argument("--out", required=True, type=Path)
    parse.add_argument("--state-code", default="")
    parse.add_argument("--state-name", default="")

    export = sub.add_parser("export-villages")
    export.add_argument("--csv", required=True, type=Path)
    export.add_argument("--parquet", required=True, type=Path)
    export.add_argument("--metadata", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.command == "download-state-villages":
        state_name = args.state_name or STATE_CODE_TO_NAME.get(args.state_code, "")
        if not state_name:
            raise SystemExit("--state-name is required for unknown state codes.")
        fetch_state_village_report(state_code=args.state_code, state_name=state_name, out_path=args.out, captcha_path=args.captcha_path, captcha_answer=args.captcha_answer, download_type=args.download_type)
        print(f"Wrote LGD report to {args.out}")
    elif args.command == "download-all-india-villages":
        fetch_village_report(report_name=REPORT_ALL_VILLAGES_INDIA, out_path=args.out, captcha_path=args.captcha_path, captcha_answer=args.captcha_answer, download_type=args.download_type)
        print(f"Wrote LGD report to {args.out}")
    elif args.command == "parse-villages":
        rows = parse_village_report(args.input, state_code=args.state_code, state_name=args.state_name)
        write_village_csv(rows, args.out)
        print(f"Wrote {len(rows)} village rows to {args.out}")
    elif args.command == "export-villages":
        summary = write_village_parquet(args.csv, args.parquet, args.metadata)
        print(f"Wrote {summary['row_count']} village rows to {args.parquet}")
        if args.metadata:
            print(f"Wrote metadata to {args.metadata}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
