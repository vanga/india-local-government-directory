# India Local Government Directory

Scraper and versioned data exports for India's Local Government Directory (LGD)
village roster.

This project preserves the official LGD Download Directory village export as a
small, analysis-friendly Parquet dataset.

## Data

| File | Description |
| --- | --- |
| `data/lgd_villages.parquet` | Full official all-India village roster from LGD |
| `data/lgd_villages.metadata.json` | Source URL, report name, row counts, checksums |
| `data/lgd_states.parquet` | Derived state/UT table |
| `data/lgd_districts.parquet` | Derived district table |
| `data/lgd_subdistricts.parquet` | Derived subdistrict table |

Current export summary:

- Villages: 676,743
- States/UTs: 35
- Districts: 781
- Subdistricts: 7,073

Village columns:

~~~text
state_code
state_name
district_code
district_name
subdistrict_code
subdistrict_name
village_code
village_name
village_version
local_language_name
village_category
village_status
hierarchy
census_2001_code
census_2011_code
pesa_status
~~~

## Scrape

The LGD website requires CAPTCHA for downloads. The scraper keeps that explicit:
it saves the CAPTCHA image and asks a human to type the answer.

~~~bash
python scripts/lgd_directory.py download-all-india-villages \
  --out /tmp/lgd-india-villages.xls

python scripts/lgd_directory.py parse-villages \
  --input /tmp/lgd-india-villages.xls \
  --out /tmp/lgd-india-villages.csv

python scripts/lgd_directory.py export-villages \
  --csv /tmp/lgd-india-villages.csv \
  --parquet data/lgd_villages.parquet \
  --metadata data/lgd_villages.metadata.json
~~~

State-specific downloads also work:

~~~bash
python scripts/lgd_directory.py download-state-villages \
  --state-code 36 --state-name Telangana \
  --out /tmp/lgd-telangana-villages.xls
~~~

