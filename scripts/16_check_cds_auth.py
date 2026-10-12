"""Comprueba el token personal de CDS sin guardar credenciales ni datos de cuenta."""

from __future__ import annotations

import json
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
PROFILE_URL = "https://cds.climate.copernicus.eu/api/profiles/v1/account/"


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def main() -> None:
    values = read_env(ENV_FILE)
    token = values.get("CDS_API_KEY")
    if not token:
        raise RuntimeError("Falta CDS_API_KEY en .env")
    response = requests.get(
        PROFILE_URL,
        headers={"PRIVATE-TOKEN": token},
        timeout=60,
    )
    if response.status_code != 200:
        raise RuntimeError(f"La comprobación de CDS devolvió estado HTTP {response.status_code}")
    print(
        json.dumps(
            {
                "authentication": "success",
                "service": "Climate Data Store",
                "credential_source": ".env not persisted in outputs",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
