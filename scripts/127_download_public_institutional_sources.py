"""Download public institutional fire sources with manifests and hashes.

This script downloads only public pages and public PDFs. It does not use or
store credentials. The project uses verify=False because the local Windows
certificate stack rejects these official domains, while the same URLs were
read through the official browser and web audit. URLs, status, byte counts and
SHA256 hashes are recorded for reproducibility.
"""

from __future__ import annotations

import hashlib
import json
import re
import warnings
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from urllib3.exceptions import InsecureRequestWarning


ROOT = Path(__file__).resolve().parents[1]
RECORDS = ROOT / "data" / "raw" / "institutional_fire_records_2026"
CORTOLIMA_DIR = RECORDS / "cortolima_news"
GOB_DIR = RECORDS / "gobernacion_tolima"
BAICV_DIR = RECORDS / "ideam_baicv"
IDEAM_HIST_DIR = RECORDS / "ideam_historical"
OUT = ROOT / "results" / "exploratory"
OUT.mkdir(parents=True, exist_ok=True)
for p in (CORTOLIMA_DIR, GOB_DIR, BAICV_DIR, IDEAM_HIST_DIR):
    p.mkdir(parents=True, exist_ok=True)

warnings.filterwarnings("ignore", category=InsecureRequestWarning)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "incendio-tolima-reproducible-audit/1.0"})


PUBLIC_PAGES = [
    (
        "cortolima_news_2026-08-12_affected_area",
        "https://cortolima.gov.co/sala-de-prensa/noticias/4587-incendios-forestales-han-afectado-cerca-de-2-000-hectareas-en-el-tolima",
        CORTOLIMA_DIR,
    ),
    (
        "cortolima_news_2026-08-22_affected_area",
        "https://cortolima.gov.co/sala-de-prensa/noticias/4598-incendios-forestales-han-afectado-mas-de-22-000-hectareas-en-el-tolima",
        CORTOLIMA_DIR,
    ),
    (
        "cortolima_news_2026-08-26_affected_area",
        "https://cortolima.gov.co/sala-de-prensa/noticias/4601-incendios-forestales-dejan-mas-de-31-000-hectareas-afectadas-en-tolima",
        CORTOLIMA_DIR,
    ),
    (
        "gobernacion_tolima_calamidad_2026-08-14",
        "https://tolima.gov.co/noticias/noticias-secretaria-de-ambiente/10190-se-declara-la-calamidad-publica-en-el-tolima-ante-emergencia-por-incendios",
        GOB_DIR,
    ),
    (
        "gobernacion_tolima_operativo_2026-08-12",
        "https://tolima.gov.co/noticias/noticias-secretaria-de-ambiente/10180-tenemos-36-incendios-activos-en-20-municipios-la-gobernadora-del-tolima-a-las-11-00-de-la-noche-del-martes-desde-san-luis",
        GOB_DIR,
    ),
    (
        "gobernacion_tolima_operativo_2026-08-13",
        "https://tolima.gov.co/noticias/10187-desde-el-aire-y-tierra-el-tolima-combate-39-incendios-forestales",
        GOB_DIR,
    ),
]

BAICV_INDEX = (
    "https://www.ideam.gov.co/sala-de-prensa/boletines/"
    "Bolet%C3%ADn-de-Alertas-por-Pron%C3%B3stico-de-la-Amenaza-por-Incendios-"
    "de-la-Cobertura-Vegetal-(BAICV)"
)

IDEAM_HISTORICAL_FILES = [
    (
        "bd_icv_nacional_2021_2023_2024_0.xlsx",
        "https://ideam.gov.co/sites/default/files/prensa/publicaciones/bd_icv_nacional_2021_2023_2024_0.xlsx",
    ),
    (
        "bd_icv_nacional_2010_2020.xlsx",
        "https://ideam.gov.co/sites/default/files/prensa/publicaciones/bd_icv_nacional_2010_2020.xlsx",
    ),
    (
        "bd_icv_nacional_2002_2010.xlsx",
        "https://ideam.gov.co/sites/default/files/prensa/publicaciones/bd_icv_nacional_2002_2010.xlsx",
    ),
]


def get(url: str) -> requests.Response:
    response = SESSION.get(url, verify=False, timeout=90)
    response.raise_for_status()
    return response


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def write_record(stem: str, url: str, directory: Path, data: bytes, suffix: str) -> dict:
    target = directory / f"{stem}{suffix}"
    target.write_bytes(data)
    return {
        "file": str(target.relative_to(ROOT)),
        "url": url,
        "suffix": suffix,
        "bytes": len(data),
        "sha256": sha256_bytes(data),
    }


def download_pages() -> list[dict]:
    records = []
    for stem, url, directory in PUBLIC_PAGES:
        try:
            response = get(url)
            records.append(write_record(stem, url, directory, response.content, ".html"))
            records[-1]["status"] = response.status_code
            records[-1]["retrieved_utc"] = datetime.now(timezone.utc).isoformat()
        except Exception as exc:  # record failures rather than hiding them
            records.append({"url": url, "status": "ERROR", "error": repr(exc)})
    return records


def discover_baicv() -> list[tuple[str, str]]:
    discovered: dict[str, str] = {}
    first = get(BAICV_INDEX)
    page_numbers = {0}
    for number in re.findall(r"[?&]page=(\d+)", first.text):
        if int(number) <= 8:
            page_numbers.add(int(number))
    for page_number in sorted(page_numbers):
        response = first if page_number == 0 else get(f"{BAICV_INDEX}?page={page_number}")
        anchors = re.findall(
            r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([^<]+)</a>',
            response.text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        for href, anchor_text in anchors:
            match = re.search(
                r"(\d+)_baicv_(agosto|septiembre)_(\d{1,2})_2026\.pdf",
                anchor_text.strip(),
                flags=re.IGNORECASE,
            )
            if not match:
                continue
            month = match.group(2).lower()
            day = int(match.group(3))
            if month == "agosto" or (month == "septiembre" and day <= 15):
                date_text = f"2026-{8 if month == 'agosto' else 9:02d}-{day:02d}"
                url = urljoin(BAICV_INDEX, href)
                discovered[date_text] = url
    return sorted(discovered.items())


def download_baicv() -> list[dict]:
    records = []
    try:
        dates_urls = discover_baicv()
    except Exception as exc:
        return [{"source": BAICV_INDEX, "status": "ERROR", "error": repr(exc)}]
    for date_text, url in dates_urls:
        stem = f"ideam_baicv_{date_text}"
        try:
            response = get(url)
            item = write_record(stem, url, BAICV_DIR, response.content, ".pdf")
            item["status"] = response.status_code
            item["retrieved_utc"] = datetime.now(timezone.utc).isoformat()
            item["date"] = date_text
            records.append(item)
        except Exception as exc:
            records.append({"date": date_text, "url": url, "status": "ERROR", "error": repr(exc)})
    return records


def download_ideam_historical() -> list[dict]:
    records = []
    for filename, url in IDEAM_HISTORICAL_FILES:
        try:
            response = get(url)
            target = IDEAM_HIST_DIR / filename
            target.write_bytes(response.content)
            records.append(
                {
                    "file": str(target.relative_to(ROOT)),
                    "url": url,
                    "bytes": len(response.content),
                    "sha256": sha256_bytes(response.content),
                    "status": response.status_code,
                    "retrieved_utc": datetime.now(timezone.utc).isoformat(),
                }
            )
        except Exception as exc:
            records.append({"file": filename, "url": url, "status": "ERROR", "error": repr(exc)})
    return records


def main() -> None:
    manifest = {
        "retrieval_date": "2026-09-08",
        "scope": "public institutional pages and IDEAM BAICV PDFs",
        "inference_class": "descriptive source acquisition",
        "pages": download_pages(),
        "baicv": download_baicv(),
        "ideam_historical": download_ideam_historical(),
        "interpretation_limit": (
            "These files are source documents. They do not by themselves provide "
            "independent event perimeters or make institutional totals equivalent to GWIS."
        ),
    }
    target = OUT / "audit_public_institutional_sources_download_2026-09-08.json"
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"manifest": str(target), "pages": len(manifest["pages"]), "baicv": len(manifest["baicv"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
