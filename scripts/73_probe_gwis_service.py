"""Inspect the public GWIS viewer bundle for documented service endpoints.

This is a diagnostic probe. It performs read-only HTTP requests and prints
candidate URLs and nearby strings. It does not treat a map layer as an
analysable dataset until a response schema and geographic coverage are
verified.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

import requests


VIEWER_URL = "https://forest-fire.emergency.copernicus.eu/apps/gwis_current_situation/"
BUNDLE_URL = VIEWER_URL + "static/js/app.bundle-2.11.4.js"
VENDOR_URL = VIEWER_URL + "static/js/vendor-2.11.4.js"
ENV_GWIS_URL = "https://maps.effis.emergency.copernicus.eu/gwis"


def main() -> None:
    viewer_response = requests.get(VIEWER_URL, timeout=60)
    viewer_response.raise_for_status()
    scripts = re.findall(
        r"<script[^>]*src=[\"']([^\"']+)", viewer_response.text, flags=re.IGNORECASE
    )
    print("scripts")
    for script in scripts:
        print(urljoin(VIEWER_URL, script))

    response = requests.get(BUNDLE_URL, timeout=60)
    response.raise_for_status()
    text = response.text
    print(f"bundle_bytes={len(response.content)}")

    urls = sorted(set(re.findall(r"https?[^\"'\\ ]{10,300}", text)))
    keywords = ("burn", "wfs", "wms", "geojson", "api", "mapserver")
    for url in urls:
        if any(keyword in url.lower() for keyword in keywords):
            print(url)

    for keyword in ("nrt.ba", "burntarea", "burnedarea", "WFS", "GetFeature"):
        print(f"\n=== {keyword} ===")
        for match in list(re.finditer(keyword, text, flags=re.IGNORECASE))[:25]:
            start = max(0, match.start() - 300)
            end = min(len(text), match.end() + 500)
            print(text[start:end].replace("\n", " "))

    print("\n=== HTML environment references ===")
    for keyword in ("ENV", "gwis-layers-url", "api.effis"):
        for match in list(
            re.finditer(keyword, viewer_response.text, flags=re.IGNORECASE)
        )[:25]:
            start = max(0, match.start() - 300)
            end = min(len(viewer_response.text), match.end() + 500)
            print(viewer_response.text[start:end].replace("\n", " "))

    vendor_response = requests.get(VENDOR_URL, timeout=60)
    vendor_response.raise_for_status()
    print(f"\nvendor_bytes={len(vendor_response.content)}")
    for source_name, source_text in (
        ("html", viewer_response.text),
        ("app", text),
        ("vendor", vendor_response.text),
    ):
        for keyword in ("gwis-layers-url", "proxy-url", "ENV="):
            match = re.search(keyword, source_text, flags=re.IGNORECASE)
            if match:
                start = max(0, match.start() - 500)
                end = min(len(source_text), match.end() + 1000)
                print(f"\n=== {source_name} {keyword} ===")
                print(source_text[start:end].replace("\n", " "))

    capabilities = requests.get(
        ENV_GWIS_URL,
        params={"service": "WMS", "request": "GetCapabilities", "version": "1.1.1"},
        timeout=60,
    )
    capabilities.raise_for_status()
    root = ET.fromstring(capabilities.content)
    print("\n=== WMS GetMap formats ===")
    for fmt in root.findall("./Capability/Request/GetMap/Format"):
        print(fmt.text)
    wanted = {"nrt.ba", "nrt.ba.query", "gwis.globfire.finalperim"}
    print("\n=== WMS burnt-area layers ===")
    for layer in root.findall(".//Layer"):
        name = layer.findtext("Name")
        if name not in wanted:
            continue
        print(ET.tostring(layer, encoding="unicode")[:10000])


if __name__ == "__main__":
    main()
