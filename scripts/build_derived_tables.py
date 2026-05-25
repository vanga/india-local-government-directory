#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def _write(rows: list[dict[str, object]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, path, compression="zstd")


def build(villages_path: Path, out_dir: Path) -> None:
    rows = pq.read_table(villages_path).to_pylist()

    states = {}
    districts = {}
    subdistricts = {}
    for row in rows:
        state_key = row["state_code"]
        district_key = (row["state_code"], row["district_code"])
        subdistrict_key = (row["state_code"], row["district_code"], row["subdistrict_code"])

        states.setdefault(state_key, {
            "state_code": row["state_code"],
            "state_name": row["state_name"],
            "district_count": set(),
            "subdistrict_count": set(),
            "village_count": 0,
        })
        states[state_key]["district_count"].add(district_key)
        states[state_key]["subdistrict_count"].add(subdistrict_key)
        states[state_key]["village_count"] += 1

        districts.setdefault(district_key, {
            "state_code": row["state_code"],
            "state_name": row["state_name"],
            "district_code": row["district_code"],
            "district_name": row["district_name"],
            "subdistrict_count": set(),
            "village_count": 0,
        })
        districts[district_key]["subdistrict_count"].add(subdistrict_key)
        districts[district_key]["village_count"] += 1

        subdistricts.setdefault(subdistrict_key, {
            "state_code": row["state_code"],
            "state_name": row["state_name"],
            "district_code": row["district_code"],
            "district_name": row["district_name"],
            "subdistrict_code": row["subdistrict_code"],
            "subdistrict_name": row["subdistrict_name"],
            "village_count": 0,
        })
        subdistricts[subdistrict_key]["village_count"] += 1

    state_rows = []
    for row in states.values():
        state_rows.append({
            "state_code": row["state_code"],
            "state_name": row["state_name"],
            "district_count": len(row["district_count"]),
            "subdistrict_count": len(row["subdistrict_count"]),
            "village_count": row["village_count"],
        })

    district_rows = []
    for row in districts.values():
        district_rows.append({
            "state_code": row["state_code"],
            "state_name": row["state_name"],
            "district_code": row["district_code"],
            "district_name": row["district_name"],
            "subdistrict_count": len(row["subdistrict_count"]),
            "village_count": row["village_count"],
        })

    subdistrict_rows = list(subdistricts.values())
    state_rows.sort(key=lambda r: (str(r["state_code"]), str(r["state_name"])))
    district_rows.sort(key=lambda r: (str(r["state_code"]), str(r["district_code"]), str(r["district_name"])))
    subdistrict_rows.sort(key=lambda r: (str(r["state_code"]), str(r["district_code"]), str(r["subdistrict_code"]), str(r["subdistrict_name"])))

    _write(state_rows, out_dir / "lgd_states.parquet")
    _write(district_rows, out_dir / "lgd_districts.parquet")
    _write(subdistrict_rows, out_dir / "lgd_subdistricts.parquet")
    print(json.dumps({
        "states": len(state_rows),
        "districts": len(district_rows),
        "subdistricts": len(subdistrict_rows),
        "villages": len(rows),
    }, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--villages", type=Path, default=Path("data/lgd_villages.parquet"))
    parser.add_argument("--out-dir", type=Path, default=Path("data"))
    args = parser.parse_args()
    build(args.villages, args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
