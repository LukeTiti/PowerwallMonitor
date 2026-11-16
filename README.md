# Powerwall Monitor

A fully **local** dashboard for a Tesla Powerwall 3 (Gateway 3) system — reads live
energy data straight from the gateway over the LAN, with **no Tesla cloud** required
for monitoring. Built to fix the Tesla app misreporting production vs. consumption, and
to add richer history, stats, and (in progress) local control.

> The headline file is **`pw_local_dashboard.py`** — a single-file, standard-library
> web dashboard that polls the Powerwall via `pypowerwall`'s TEDAPI interface.

## What it does

- **Live power flow** — Tesla-app-style animated diagram (Solar / Grid / Home / Powerwall),
  with each stream drawn as one continuous, source-coloured path and an allocation that
  honours your grid-charging setting (so it never shows grid→battery when that's disabled).
- **Trends** — per-channel power charts, battery SOC, self-powered %, "today vs a typical
  day" comparison, records (peaks with timestamps), and milestones (kWh, CO₂ avoided,
  EV-miles, battery cycles).
- **Wall Connector monitoring** — live vitals from Gen-3 Tesla Wall Connectors on the LAN.
- **Grid-outage safety** — a prominent alarm (plus browser notification) when the grid
  drops, showing time-to-empty and warning if an EV is charging off the backup.
- **Control panel** — UI for backup reserve, operation mode, grid charging, grid export,
  and off-grid/max-backup. Writes require a registered signed key (see "Control" below).
- **iOS-style "Liquid Glass" UI**, served to any device on the home network.

History is logged to a small SQLite DB (~0.2 MB/day) so the charts survive reboots.

## How it works

Tesla's TEDAPI is a signed-protobuf interface on the Powerwall gateway. `pypowerwall`
implements the handshake, so a Raspberry Pi joined to the Powerwall's Wi-Fi AP (gateway
`192.168.91.1`) can read `get_status()`, `get_config()`, per-unit vitals, etc. locally.
The `meterAggregates` give distinct CT-measured channels (SOLAR / LOAD / SITE / BATTERY),
which is what fixes the swapped production/consumption readings.

## Setup

On the host (e.g. a Raspberry Pi dual-homed to your LAN on Ethernet and the Powerwall
Wi-Fi AP):

```bash
python3 -m venv ~/pw-tedapi-test
~/pw-tedapi-test/bin/pip install pypowerwall
# provide the gateway password via env or the dashboard's "Gateway connection" panel
PW_GW_PWD='your-gateway-password' ~/pw-tedapi-test/bin/python pw_local_dashboard.py
```

Then open `http://<host-ip>:8675` from any device on your home network.

Run it as a service with the provided unit:

```bash
sudo cp pw-dashboard.service /etc/systemd/system/
sudo systemctl enable --now pw-dashboard
```

### Configuration (environment variables)

| Var | Default | Meaning |
|-----|---------|---------|
| `PW_GW_PWD` | *(empty)* | Gateway password (or set it via the dashboard, saved to `~/.pw_dashboard_secret`) |
| `PW_PORT` | `8675` | HTTP port |
| `PW_WC` | `192.168.1.74` | Comma-separated Wall Connector IPs |
| `PW_POLL` / `PW_HEAVY` | `10` / `30` | Poll intervals (seconds) for core flows / heavy vitals |
| `PW_LOG` | `20` | Seconds between history samples |
| `PW_HISTORY_DAYS` | `60` | History retention |

The gateway password is **never** stored in this repo; it lives only in `~/.pw_dashboard_secret`
(owner-readable) or the `PW_GW_PWD` env var on the host.

## Control (work in progress)

Reads are fully local. **Writes** (reserve %, mode, grid charging, off-grid vehicle charging)
require a signed "v1r" key registered with the gateway, which requires Tesla **Fleet API**
(a developer app + a public key hosted on your domain). The control UI is built and waits
on that registration.

## Legacy cloud scripts

`main.py`, `poller.py`, `tesla_api.py`, `tesla_auth.py`, `web.py`, `alerts.py`,
`setup_tesla_auth.py` are the earlier **cloud** (Owner API) approach. Tesla retired the
Owner API OAuth, so the local TEDAPI dashboard above is the current path; these are kept
for reference.

## Security notes

- No secrets are committed. See `.gitignore`.
- The dashboard is served **unauthenticated on the home LAN** — anyone on your Wi-Fi can
  reach it. Keep it to a trusted network or add access control if needed.
