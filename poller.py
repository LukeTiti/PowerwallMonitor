"""Pulls one raw reading from Tesla's cloud (Owner API), read directly
from Tesla's servers rather than through the phone app's display layer.

Requires a one-time login via setup_tesla_auth.py before this will work.
"""

import time

import tesla_api
import tesla_auth

AUTH_FILE = "tesla_auth.json"


def read(site_id: str) -> dict:
    """Return one normalized reading from Tesla's energy site live_status.

    Sign convention (as reported by Tesla):
      solar_w    >= 0, always production
      load_w     >= 0, always consumption
      battery_w  > 0 discharging, < 0 charging
      grid_w     > 0 importing from grid, < 0 exporting to grid
    """
    access_token = tesla_auth.get_valid_access_token(AUTH_FILE)
    status = tesla_api.get_live_status(access_token, site_id)
    return {
        "ts": time.time(),
        "solar_w": status["solar_power"],
        "load_w": status["load_power"],
        "battery_w": status["battery_power"],
        "grid_w": status["grid_power"],
        "battery_soe": status["percentage_charged"],
    }
