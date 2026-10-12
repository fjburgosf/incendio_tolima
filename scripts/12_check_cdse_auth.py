"""Comprueba la autenticación OAuth de CDSE sin registrar credenciales ni tokens."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def main() -> None:
    values = read_env(ENV_FILE)
    client_id = values.get("CDSE_CLIENT_ID")
    client_secret = values.get("CDSE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("Faltan CDSE_CLIENT_ID o CDSE_CLIENT_SECRET en .env")
    request = Request(
        TOKEN_URL,
        data=urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request, timeout=60) as response:
        payload = json.load(response)
    print(
        json.dumps(
            {
                "authentication": "success",
                "token_type": payload.get("token_type"),
                "expires_in_seconds": payload.get("expires_in"),
                "credential_source": ".env not persisted in outputs",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
