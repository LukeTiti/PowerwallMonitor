"""Calls to Tesla's Owner API for energy site data — the same backend the
Tesla app itself reads from, called directly rather than through the app's
(buggy) display layer."""

import httpx

from tesla_auth import HEADERS, OWNER_API_BASE


def get_energy_site_id(access_token: str) -> str:
    """Finds your Powerwall system's site ID from the account's product list."""
    resp = httpx.get(
        f"{OWNER_API_BASE}/api/1/products",
        http2=True,
        headers={**HEADERS, "Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    for product in resp.json()["response"]:
        if "energy_site_id" in product:
            return str(product["energy_site_id"])
    raise RuntimeError("No energy site found on this Tesla account")


def get_live_status(access_token: str, site_id: str) -> dict:
    resp = httpx.get(
        f"{OWNER_API_BASE}/api/1/energy_sites/{site_id}/live_status",
        http2=True,
        headers={**HEADERS, "Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    return resp.json()["response"]
