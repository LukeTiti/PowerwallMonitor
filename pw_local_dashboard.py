#!/usr/bin/env python3
"""
Powerwall LOCAL dashboard — runs on the Pi, reads live data straight from the
Gateway via TEDAPI (pypowerwall), serves a self-updating webpage on the home LAN.
No Tesla cloud involved.
"""
import json, os, sqlite3, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from pypowerwall.tedapi import TEDAPI

GW_PWD = os.environ.get("PW_GW_PWD", "")
PORT = int(os.environ.get("PW_PORT", "8675"))
STATUS_EVERY = float(os.environ.get("PW_POLL", "10"))   # core power numbers
HEAVY_EVERY = float(os.environ.get("PW_HEAVY", "30"))   # bulky vitals/blocks
TIMEOUT = float(os.environ.get("PW_TIMEOUT", "15"))     # per TEDAPI call
DB_PATH = os.environ.get("PW_DB", os.path.expanduser("~/pw_history.db"))
LOG_EVERY = float(os.environ.get("PW_LOG", "20"))       # seconds between history rows
HISTORY_DAYS = int(os.environ.get("PW_HISTORY_DAYS", "60"))
WC_IPS = [x.strip() for x in os.environ.get("PW_WC", "192.168.1.74").split(",") if x.strip()]
GW_HOST = os.environ.get("PW_GW_HOST", "192.168.91.1")
KEY_PATH = os.environ.get("PW_RSA_KEY", os.path.expanduser("~/.pypowerwall/tedapi_rsa_private.pem"))
VALID_MODES = ("self_consumption", "autonomous", "backup")  # Self-Powered / Time-Based / Backup

_data = {"ok": False, "error": "starting up", "ts": time.time()}
_lock = threading.Lock()

# ── gateway password: an optional secret file overrides the env/embedded default,
#    so it can be (re)entered from the dashboard without code changes ──
SECRET_FILE = os.environ.get("PW_SECRET", os.path.expanduser("~/.pw_dashboard_secret"))
_force_reconnect = threading.Event()


def current_pwd():
    try:
        with open(SECRET_FILE) as f:
            p = f.read().strip()
            if p:
                return p
    except Exception:
        pass
    return GW_PWD


def save_pwd(p):
    with open(SECRET_FILE, "w") as f:
        f.write(p)
    try:
        os.chmod(SECRET_FILE, 0o600)
    except Exception:
        pass
    _force_reconnect.set()


# ── history store (SQLite, WAL: one writer + many readers, survives reboots) ──
def db_connect():
    c = sqlite3.connect(DB_PATH, timeout=5)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def db_init():
    c = db_connect()
    c.execute("""CREATE TABLE IF NOT EXISTS samples(
        ts INTEGER PRIMARY KEY, solar INTEGER, home INTEGER,
        grid INTEGER, batt INTEGER, soc REAL)""")
    try:
        c.execute("ALTER TABLE samples ADD COLUMN wc INTEGER")  # Wall Connector watts
    except Exception:
        pass  # column already exists
    c.commit()
    c.close()


def history(range_s):
    """Return downsampled series + integrated kWh totals for the last range_s seconds."""
    now = time.time()
    start = int(now - range_s)
    try:
        c = db_connect()
        rows = c.execute(
            "SELECT ts,solar,home,grid,batt,soc,wc FROM samples WHERE ts>=? ORDER BY ts",
            (start,)).fetchall()
        c.close()
    except Exception as e:
        return {"error": str(e), "t": []}

    # integrate power -> energy from the RAW rows (robust to gaps/restarts)
    kwh = {"solar": 0.0, "home": 0.0, "grid_in": 0.0, "grid_out": 0.0,
           "batt_charge": 0.0, "batt_discharge": 0.0,
           "home_solar": 0.0, "home_batt": 0.0, "home_grid": 0.0,
           "solar_batt": 0.0, "solar_grid": 0.0, "grid_batt": 0.0, "car": 0.0}
    maxgap = LOG_EVERY * 3
    for a, b in zip(rows, rows[1:]):
        dt = b[0] - a[0]
        if dt <= 0 or dt > maxgap:
            continue
        h = dt / 3600000.0  # Wh -> kWh (seconds/3600 /1000)
        s, hm, g, bt = a[1] or 0, a[2] or 0, a[3] or 0, a[4] or 0
        kwh["solar"] += max(s, 0) * h
        kwh["home"] += hm * h
        kwh["grid_in"] += max(g, 0) * h
        kwh["grid_out"] += max(-g, 0) * h
        kwh["batt_charge"] += max(-bt, 0) * h
        kwh["batt_discharge"] += max(bt, 0) * h
        # allocate home's supply: solar first, then battery, then grid
        S, Hd, bD, bC = max(s, 0), max(hm, 0), max(bt, 0), max(-bt, 0)
        sh = min(S, Hd); r = Hd - sh
        bh = min(bD, r); r -= bh
        kwh["home_solar"] += sh * h
        kwh["home_batt"] += bh * h
        kwh["home_grid"] += r * h
        # allocate solar's output: home first, then charging, then export
        sr = S - sh
        sb = min(bC, sr); sr -= sb
        kwh["solar_batt"] += sb * h
        kwh["solar_grid"] += max(sr, 0) * h
        kwh["grid_batt"] += max(bC - sb, 0) * h  # battery charging not covered by solar = from grid
        kwh["car"] += (a[6] or 0) * h  # Wall Connector energy (subset of home load)
    kwh = {k: round(v, 2) for k, v in kwh.items()}

    # downsample to <= NPTS buckets by time-averaging
    NPTS = 320
    if len(rows) > NPTS:
        bucket = max(1.0, range_s / NPTS)
        agg = {}
        for r in rows:
            ts = r[0]
            k = int((ts - start) / bucket)
            d = agg.setdefault(k, [0, 0, 0, 0, 0, 0.0, 0, 0, 0])  # sums+count+socN+wc
            d[0] += ts; d[1] += r[1] or 0; d[2] += r[2] or 0; d[3] += r[3] or 0
            d[4] += r[4] or 0
            if r[5] is not None:
                d[5] += r[5]; d[7] += 1
            d[6] += 1
            d[8] += (r[6] or 0) if len(r) > 6 else 0
        out = {"t": [], "solar": [], "home": [], "grid": [], "batt": [], "soc": [], "wc": []}
        for k in sorted(agg):
            d = agg[k]
            n = d[6]
            out["t"].append(int(d[0] / n))
            out["solar"].append(round(d[1] / n))
            out["home"].append(round(d[2] / n))
            out["grid"].append(round(d[3] / n))
            out["batt"].append(round(d[4] / n))
            out["soc"].append(round(d[5] / d[7], 1) if d[7] else None)
            out["wc"].append(round(d[8] / n))
    else:
        out = {"t": [r[0] for r in rows], "solar": [r[1] for r in rows],
               "home": [r[2] for r in rows], "grid": [r[3] for r in rows],
               "batt": [r[4] for r in rows], "soc": [r[5] for r in rows],
               "wc": [(r[6] if len(r) > 6 and r[6] is not None else 0) for r in rows]}
    out["kwh"] = kwh
    out["range"] = range_s
    out["n"] = len(rows)
    return out


def stats(range_s):
    """Records, milestones, and a typical-day hour profile over the last range_s."""
    now = time.time()
    start = int(now - range_s)
    try:
        c = db_connect()
        rows = c.execute(
            "SELECT ts,solar,home,grid,batt,soc,wc FROM samples WHERE ts>=? ORDER BY ts",
            (start,)).fetchall()
        c.close()
    except Exception as e:
        return {"error": str(e), "n": 0}
    out = {"range": range_s, "n": len(rows)}
    if not rows:
        return out

    # peak records (value + when)
    def peak(idx, sign=1):
        best = None
        for r in rows:
            v = r[idx]
            if v is None:
                continue
            sv = v * sign
            if best is None or sv > best[0]:
                best = (sv, r[0])
        return {"w": round(best[0]), "ts": best[1]} if best else None
    rec = {
        "peak_solar": peak(1),
        "peak_home": peak(2),
        "peak_grid_imp": peak(3, 1),
        "peak_grid_exp": peak(3, -1),
        "peak_batt_dis": peak(4, 1),
        "peak_batt_chg": peak(4, -1),
    }
    socs = [(r[5], r[0]) for r in rows if r[5] is not None]
    if socs:
        hi = max(socs); lo = min(socs)
        rec["max_soc"] = {"v": hi[0], "ts": hi[1]}
        rec["min_soc"] = {"v": lo[0], "ts": lo[1]}
    out["records"] = rec

    # The typical-day profile is built from PRIOR days only (ts before today's
    # local midnight), so "today vs typical" compares today against OTHER days,
    # never itself. Totals/milestones still integrate over everything.
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    prof = {k: [[0.0, 0] for _ in range(24)] for k in ("solar", "home", "grid", "batt", "wc")}
    kwh = {"solar": 0.0, "home": 0.0, "batt_dis": 0.0, "batt_chg": 0.0,
           "car": 0.0, "grid_in": 0.0, "grid_out": 0.0}
    prior_dates = set()
    maxgap = LOG_EVERY * 3
    for i, r in enumerate(rows):
        if r[0] < midnight:  # prior days only feed the "typical" baseline
            prior_dates.add(time.localtime(r[0])[0:3])
            hr = time.localtime(r[0]).tm_hour
            for j, k in ((1, "solar"), (2, "home"), (3, "grid"), (4, "batt"), (6, "wc")):
                v = r[j]
                if v is not None:
                    prof[k][hr][0] += v; prof[k][hr][1] += 1
        if i > 0:
            dt = r[0] - rows[i - 1][0]
            if 0 < dt <= maxgap:
                h = dt / 3600000.0
                p = rows[i - 1]
                s, hm, g, bt, wc = p[1] or 0, p[2] or 0, p[3] or 0, p[4] or 0, p[6] or 0
                kwh["solar"] += max(s, 0) * h; kwh["home"] += hm * h
                kwh["batt_dis"] += max(bt, 0) * h; kwh["batt_chg"] += max(-bt, 0) * h
                kwh["grid_in"] += max(g, 0) * h; kwh["grid_out"] += max(-g, 0) * h
                kwh["car"] += wc * h
    out["profile"] = {k: [round(a / n) if n else None for a, n in prof[k]] for k in prof}
    out["baseline_days"] = len(prior_dates)

    # fun milestones
    pack = 43.2
    try:
        with _lock:
            fw = (_data.get("energy") or {}).get("fullWh")
        if fw:
            pack = fw / 1000.0
    except Exception:
        pass
    days = max((rows[-1][0] - rows[0][0]) / 86400.0, 1e-6)
    out["totals"] = {
        "solar_kwh": round(kwh["solar"], 1),
        "home_kwh": round(kwh["home"], 1),
        "batt_dis_kwh": round(kwh["batt_dis"], 1),
        "batt_chg_kwh": round(kwh["batt_chg"], 1),
        "car_kwh": round(kwh["car"], 1),
        "grid_in_kwh": round(kwh["grid_in"], 1),
        "grid_out_kwh": round(kwh["grid_out"], 1),
        "equiv_cycles": round(kwh["batt_dis"] / pack, 1) if pack else None,
        "co2_kg": round(kwh["solar"] * 0.39),            # ~0.39 kg CO2 / kWh (US grid avg)
        "trees": round(kwh["solar"] * 0.39 / 21.77, 1),  # ~21.77 kg CO2 / tree / yr
        "ev_miles": round(kwh["solar"] / 0.25),          # ~0.25 kWh / mile
        "avg_solar_day": round(kwh["solar"] / days, 1) if days >= 1 else None,
        "avg_home_day": round(kwh["home"] / days, 1) if days >= 1 else None,
        "self_powered_pct": round(kwh["solar"] / (kwh["solar"] + kwh["grid_in"]) * 100)
        if (kwh["solar"] + kwh["grid_in"]) > 0 else None,
    }

    # today-so-far vs a typical day (lt/midnight computed above)
    tprof = {k: [[0.0, 0] for _ in range(24)] for k in ("solar", "home")}
    tkwh = {"solar": 0.0, "home": 0.0}
    trows = [r for r in rows if r[0] >= midnight]
    for i, r in enumerate(trows):
        hr = time.localtime(r[0]).tm_hour
        for j, k in ((1, "solar"), (2, "home")):
            v = r[j]
            if v is not None:
                tprof[k][hr][0] += v; tprof[k][hr][1] += 1
        if i > 0:
            dt = r[0] - trows[i - 1][0]
            if 0 < dt <= maxgap:
                h = dt / 3600000.0; p = trows[i - 1]
                tkwh["solar"] += max(p[1] or 0, 0) * h
                tkwh["home"] += (p[2] or 0) * h
    cur_h = lt.tm_hour + lt.tm_min / 60.0

    def typical_so_far(profile_list):
        tot = 0.0
        for hh in range(24):
            v = profile_list[hh]
            if v is None:
                continue
            frac = 1.0 if hh < int(cur_h) else (cur_h - hh if hh == int(cur_h) else 0.0)
            tot += v * max(frac, 0.0)
        return tot / 1000.0
    tsol = typical_so_far(out["profile"]["solar"])
    thom = typical_so_far(out["profile"]["home"])
    out["today"] = {
        "solar_kwh": round(tkwh["solar"], 1),
        "home_kwh": round(tkwh["home"], 1),
        "typical_solar_sofar": round(tsol, 1),
        "typical_home_sofar": round(thom, 1),
        "vs_solar_pct": round((tkwh["solar"] - tsol) / tsol * 100) if tsol > 0.2 else None,
        "vs_home_pct": round((tkwh["home"] - thom) / thom * 100) if thom > 0.2 else None,
        "profile_solar": [round(a / n) if n else None for a, n in tprof["solar"]],
        "profile_home": [round(a / n) if n else None for a, n in tprof["home"]],
    }
    return out


# ── control (signed v1r writes). Requires a registered RSA key at KEY_PATH. ──
_ctl_client = None
_ctl_lock = threading.Lock()
_force_heavy = threading.Event()


def control_available():
    return os.path.exists(KEY_PATH)


def control_client():
    """Lazily build a v1r-capable Powerwall client for writes."""
    global _ctl_client
    with _ctl_lock:
        if _ctl_client is None:
            from pypowerwall import Powerwall
            _ctl_client = Powerwall(host=GW_HOST, gw_pwd=current_pwd(),
                                    rsa_key_path=KEY_PATH, timeout=TIMEOUT)
        return _ctl_client


def reset_control_client():
    global _ctl_client
    with _ctl_lock:
        _ctl_client = None


def do_control(action, value):
    """Dispatch one control action. Never called by the dashboard itself."""
    if not control_available():
        return {"ok": False, "error": "control not enabled — register an RSA key first (see setup)"}
    try:
        pw = control_client()
        if action == "reserve":
            lvl = float(value)
            if not (0 <= lvl <= 100):
                return {"ok": False, "error": "reserve must be 0-100"}
            r = pw.set_reserve(lvl)
        elif action == "mode":
            if value not in VALID_MODES:
                return {"ok": False, "error": "invalid mode"}
            r = pw.set_mode(value)
        elif action == "grid_charging":
            r = pw.set_grid_charging(bool(value))
        elif action == "grid_export":
            if value not in ("battery_ok", "pv_only", "never"):
                return {"ok": False, "error": "invalid export mode"}
            r = pw.set_grid_export(value)
        elif action == "off_grid":
            r = pw.go_off_grid()
        elif action == "reconnect":
            r = pw.reconnect_grid()
        elif action == "max_backup_on":
            r = pw.schedule_max_backup(duration_seconds=int(value or 7200))
        elif action == "max_backup_off":
            r = pw.cancel_max_backup()
        else:
            return {"ok": False, "error": "unknown action"}
    except Exception as e:
        reset_control_client()
        return {"ok": False, "error": str(e)}
    _force_heavy.set()  # re-read settings on the next poll
    ok = r is not None and r is not False
    return {"ok": ok, "result": r if isinstance(r, (dict, list, str, int, float, bool)) else str(r)}


def fetch_wc(ip, timeout=2.0):
    """Read a Gen-3 Tesla Wall Connector's local (read-only) API."""
    import urllib.request
    out = {"ip": ip}
    for name, path in (("vitals", "/api/1/vitals"),
                       ("version", "/api/1/version"),
                       ("lifetime", "/api/1/lifetime")):
        try:
            with urllib.request.urlopen("http://%s%s" % (ip, path), timeout=timeout) as r:
                out[name] = json.loads(r.read().decode())
        except Exception as e:
            out[name + "_err"] = str(e)
    return out


def poller():
    """Slow, staggered polling with last-good caching: one background thread
    that never lets a single timeout erase a good reading."""
    global _data
    t = None
    din = fw = None
    cache = {"ok": False, "error": "starting up", "ts": time.time()}
    last_heavy = 0.0
    last_log = 0.0
    last_prune = 0.0
    try:
        logdb = db_connect()
    except Exception:
        logdb = None

    while True:
        now = time.time()
        if _force_reconnect.is_set():
            _force_reconnect.clear()
            t = None
        try:
            if t is None:
                t = TEDAPI(current_pwd())
                try:
                    t.timeout = TIMEOUT
                except Exception:
                    pass
                din = t.get_din()
                fw = t.get_firmware_version()

            # --- light: the four power flows + battery + grid ---
            st = t.get_status()
            ctrl = st.get("control", {})
            flows = {}
            for m in ctrl.get("meterAggregates", []):
                loc = m.get("location")
                if loc:
                    flows[loc] = round(float(m.get("realPowerW", 0)))
            cache["flows"] = flows
            ss = ctrl.get("systemStatus", {})
            rem = ss.get("nominalEnergyRemainingWh")
            full = ss.get("nominalFullPackEnergyWh")
            cache["energy"] = {
                "remainingWh": rem, "fullWh": full,
                "soc": round(rem / full * 100, 1) if rem and full else None,
            }
            alerts = ctrl.get("alerts", {}).get("active", []) or []
            cache["alerts"] = alerts
            cache["grid_connected"] = "SystemConnectedToGrid" in alerts
            cache["islanded"] = any("Island" in a for a in alerts)
            cache["din"] = din
            cache["firmware"] = fw
            cache["ok"] = True
            cache["error"] = None
            cache["ts"] = time.time()

            # --- heavy: per-unit blocks, pack vitals, CT readings, settings ---
            if now - last_heavy >= HEAVY_EVERY or _force_heavy.is_set():
                for key, fn in (("blocks", t.get_blocks),
                                ("pw3", t.get_pw3_vitals),
                                ("vitals", t.vitals)):
                    try:
                        cache[key] = fn() or {}
                        cache.pop(key + "_err", None)
                    except Exception as e:
                        cache[key + "_err"] = str(e)
                # Wall Connectors (read-only local API)
                cache["wc"] = [fetch_wc(ip) for ip in WC_IPS]
                wcp = 0.0
                for c in cache["wc"]:
                    v = c.get("vitals") or {}
                    if v.get("contactor_closed"):
                        wcp += (v.get("vehicle_current_a") or 0) * (v.get("grid_v") or 0)
                cache["wc_power"] = round(wcp)
                # current Powerwall settings (for the Control panel)
                try:
                    cfg = t.get_config() or {}
                    si = cfg.get("site_info", {})
                    dis = si.get("disallow_charge_from_grid_with_solar_installed")
                    cache["settings"] = {
                        "reserve": si.get("backup_reserve_percent"),
                        "mode": cfg.get("default_real_mode") or si.get("default_real_mode"),
                        "grid_charging": (not dis) if dis is not None else True,
                        "grid_export": si.get("customer_preferred_export_rule") or "battery_ok",
                        "control_available": control_available(),
                    }
                    cache.pop("settings_err", None)
                except Exception as e:
                    cache["settings_err"] = str(e)
                last_heavy = now
                _force_heavy.clear()

            # --- history: log a sample at LOG_EVERY cadence (good data only) ---
            if logdb is not None and flows and now - last_log >= LOG_EVERY:
                try:
                    logdb.execute(
                        "INSERT OR REPLACE INTO samples(ts,solar,home,grid,batt,soc,wc) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (int(now), flows.get("SOLAR"), flows.get("LOAD"),
                         flows.get("SITE"), flows.get("BATTERY"),
                         cache["energy"].get("soc"), cache.get("wc_power", 0)))
                    logdb.commit()
                    last_log = now
                    cache.pop("log_err", None)
                except Exception as e:
                    cache["log_err"] = str(e)
                if now - last_prune >= 3600:
                    try:
                        logdb.execute("DELETE FROM samples WHERE ts < ?",
                                      (int(now - HISTORY_DAYS * 86400),))
                        logdb.commit()
                    except Exception:
                        pass
                    last_prune = now

        except Exception as e:
            # keep serving last-good data; just note the error + staleness
            cache["error"] = str(e)
            cache["ts_error"] = time.time()
            if not cache.get("flows"):
                cache["ok"] = False
            t = None  # force reconnect next cycle

        with _lock:
            _data = dict(cache)
        time.sleep(STATUS_EVERY)


PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Powerwall Live</title>
<style>
:root{--bg:#000000;--card:rgba(28,28,30,0.68);--card2:rgba(118,118,128,0.24);--line:rgba(255,255,255,0.10);--txt:#f5f5f7;--dim:rgba(235,235,245,0.60);
--solar:#ffd60a;--home:#0a84ff;--grid:#ff453a;--grid2:#30d158;--batt:#30d158;--acc:#0a84ff;--car:#bf5af2;}
*{box-sizing:border-box}
body{margin:0;color:var(--txt);-webkit-font-smoothing:antialiased;letter-spacing:-0.01em;
font:16px/1.45 -apple-system,"SF Pro Text",BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
background:radial-gradient(60vw 55vw at 12% -5%,rgba(10,132,255,0.20),transparent 55%),
radial-gradient(55vw 50vw at 96% 8%,rgba(191,90,242,0.16),transparent 55%),
radial-gradient(65vw 60vw at 50% 112%,rgba(48,209,88,0.13),transparent 55%),#000;
background-attachment:fixed}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:6px 2px 18px}
h1{font-size:20px;margin:0;font-weight:700;letter-spacing:.2px}
.badge{font-size:12px;padding:3px 9px;border-radius:999px;border:1px solid var(--line);color:var(--dim)}
.badge.on{color:var(--grid2);border-color:#1f5e3a;background:#0f2619}
.badge.off{color:var(--grid);border-color:#5e1f1f;background:#260f0f}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--grid2);margin-right:5px;vertical-align:middle;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.grid{display:grid;gap:12px}
.flows{grid-template-columns:repeat(4,1fr)}
@media(max-width:720px){.flows{grid-template-columns:repeat(2,1fr)}}
.tile{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;position:relative;overflow:hidden}
.tile .lbl{font-size:12px;text-transform:uppercase;letter-spacing:.8px;color:var(--dim)}
.tile .val{font-size:30px;font-weight:700;margin-top:6px;font-variant-numeric:tabular-nums}
.tile .val span{font-size:15px;color:var(--dim);font-weight:500}
.tile .sub{font-size:12px;color:var(--dim);margin-top:4px;min-height:14px}
.tile .bar{position:absolute;left:0;top:0;height:4px;width:100%}
.solar .bar{background:var(--solar)} .home .bar{background:var(--home)}
.grid-t .bar{background:var(--grid)} .batt .bar{background:var(--batt)}
.section{margin-top:22px}
.section h2{font-size:13px;text-transform:uppercase;letter-spacing:1px;color:var(--dim);margin:0 2px 10px;font-weight:600}
.batrow{display:grid;grid-template-columns:1fr;gap:12px}
.gauge{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}
.gwrap{height:26px;background:#0c121d;border:1px solid var(--line);border-radius:8px;overflow:hidden;position:relative}
.gfill{height:100%;background:linear-gradient(90deg,#2a8f54,#49d17a);width:0;transition:width .6s ease}
.gtxt{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:14px;text-shadow:0 1px 2px #000}
.cards{grid-template-columns:repeat(auto-fill,minmax(230px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.card .t{font-weight:700;font-size:13px;margin-bottom:8px;color:var(--acc);word-break:break-all}
.kv{display:flex;justify-content:space-between;gap:10px;padding:3px 0;font-size:13px;border-bottom:1px dashed #1c2740}
.kv:last-child{border-bottom:0}
.kv .k{color:var(--dim)} .kv .v{font-variant-numeric:tabular-nums}
.alerts{display:flex;flex-wrap:wrap;gap:6px}
.chip{font-size:11px;padding:3px 8px;border-radius:6px;background:var(--card2);border:1px solid var(--line);color:var(--dim)}
details{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 14px;margin-top:12px}
summary{cursor:pointer;color:var(--dim);font-size:13px}
pre{overflow:auto;font-size:11px;color:#b9c6dd;max-height:420px}
.err{background:#260f0f;border:1px solid #5e1f1f;color:#ff9a9a;padding:12px 14px;border-radius:12px;margin-bottom:14px}
footer{margin:26px 2px 10px;color:var(--dim);font-size:12px;display:flex;gap:14px;flex-wrap:wrap}
.toggle{display:inline-flex;background:var(--card);border:1px solid var(--line);border-radius:999px;padding:3px}
.tbtn{background:none;border:0;color:var(--dim);padding:5px 15px;border-radius:999px;font-size:13px;cursor:pointer;font-weight:600;font-family:inherit}
.tbtn.active{background:var(--card2);color:var(--txt)}
[hidden]{display:none!important}
.flowwrap{display:flex;justify-content:center;margin-top:8px}
svg.flow{width:100%;max-width:480px;height:auto;touch-action:none}
.base{stroke:#2a3750;stroke-width:3;fill:none}
.flowline{stroke-width:4;fill:none;stroke-dasharray:4 12;stroke-dashoffset:0;opacity:0;stroke-linecap:round;transition:opacity .45s}
.flowline.active{opacity:1;animation:dash linear infinite}
.flowline.rev{animation-direction:reverse}
@keyframes dash{to{stroke-dashoffset:-32}}
.route{fill:none;stroke-linecap:round;stroke-linejoin:round;stroke-dasharray:0.1 13;animation:dashmove linear infinite;filter:drop-shadow(0 0 2px currentColor)}
@keyframes dashmove{to{stroke-dashoffset:-26}}
.charts2{grid-template-columns:repeat(2,1fr)}
@media(max-width:720px){.charts2{grid-template-columns:1fr}}
.node circle{fill:var(--card);stroke:var(--line);stroke-width:2}
.node.live circle{stroke:currentColor}
.node .ic{stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.hub rect{fill:var(--card2);stroke:var(--line);stroke-width:2}
.hub .ic{stroke:var(--dim);fill:none;stroke-width:1.6;stroke-linecap:round}
text.lbl2{fill:var(--dim);font-size:11px;letter-spacing:.7px;font-weight:600}
text.val2{fill:var(--txt);font-size:18px;font-weight:700}
text.sub2{fill:var(--dim);font-size:10px}
.flowhint{text-align:center;color:var(--dim);font-size:12px;margin-top:14px}
.wcflow{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px;margin-top:18px}
.wccard{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;overflow:hidden}
.wccard.charging{border-color:var(--car)}
.wccard .wct{font-size:12px;color:var(--car);font-weight:700}
.wccard .wcp{font-size:22px;font-weight:700;margin-top:3px;font-variant-numeric:tabular-nums}
.wccard .wcs{font-size:12px;margin-top:3px;color:var(--dim)}
.wcbar{height:4px;border-radius:3px;background:#0c121d;margin-top:9px;overflow:hidden}
.wcbar i{display:block;height:100%;width:30%;background:var(--car);border-radius:3px;animation:wcpulse 1.4s linear infinite}
@keyframes wcpulse{0%{margin-left:-30%}100%{margin-left:100%}}
.pwbar-wrap{max-width:480px;margin:20px auto 0}
.pwbar-top{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;gap:10px}
.pwbar-pct{font-size:20px;font-weight:700}
.pwbar-eta{font-size:13px;color:var(--batt);font-weight:600;text-align:right}
.pwbar{position:relative;height:24px;background:#0c121d;border:1px solid var(--line);border-radius:8px;overflow:visible}
.pwbar-fill{height:100%;background:linear-gradient(90deg,#2a8f54,#49d17a);width:0;border-radius:7px;transition:width .6s ease}
.pwbar-res{position:absolute;top:-4px;bottom:-4px;width:2px;background:var(--grid)}
.pwbar-res::after{content:'reserve';position:absolute;top:-15px;left:50%;transform:translateX(-50%);font-size:9px;color:var(--grid);white-space:nowrap}
.pwbar-sub{font-size:12px;color:var(--dim);margin-top:7px;text-align:center}
.ranges{display:inline-flex;gap:4px;flex-wrap:wrap;margin:2px 0 6px}
.rbtn{background:var(--card);border:1px solid var(--line);color:var(--dim);padding:5px 12px;border-radius:8px;font-size:12px;cursor:pointer;font-weight:600;font-family:inherit}
.rbtn.active{background:var(--card2);color:var(--txt);border-color:var(--acc)}
.chartcard{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 10px 8px;margin-bottom:14px}
.chartcard h3{margin:0 6px 6px;font-size:12px;text-transform:uppercase;letter-spacing:.8px;color:var(--dim);font-weight:600}
svg.chart{width:100%;height:auto;display:block;touch-action:pan-y}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin:4px 8px 2px;font-size:12px;color:var(--dim)}
.legend b{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:middle}
.kwh{grid-template-columns:repeat(3,1fr)}
@media(max-width:560px){.kwh{grid-template-columns:repeat(2,1fr)}}
.kwh .tile .val{font-size:22px}
.chartx{fill:var(--dim);font-size:10px}
.charty{fill:var(--dim);font-size:10px}
.chartgrid{stroke:#1c2740;stroke-width:1}
.chartzero{stroke:#36476a;stroke-width:1;stroke-dasharray:3 3}
.emptymsg{text-align:center;color:var(--dim);font-size:13px;padding:30px 0}
.bigpct{font-size:46px;font-weight:800;color:var(--grid2);font-variant-numeric:tabular-nums;line-height:1}
.statlbl{font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:var(--dim);margin-bottom:6px}
.sbar{display:flex;height:18px;border-radius:7px;overflow:hidden;background:#0c121d;border:1px solid var(--line)}
.sbar span{display:block;height:100%}
.statlegend{display:flex;gap:14px;flex-wrap:wrap;margin-top:7px;font-size:12px;color:var(--dim)}
.statlegend b{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px;vertical-align:middle}
.donuts{grid-template-columns:repeat(3,1fr);gap:10px}
@media(max-width:400px){.donuts{grid-template-columns:1fr}}
.donut{text-align:center}
.donut svg{width:100%;max-width:118px;height:auto;display:block;margin:0 auto}
.donut .dtitle{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.4px;margin-bottom:6px;font-weight:600}
.donut .dleg{margin-top:6px;font-size:11px;color:var(--dim);line-height:1.5}
.donut .dleg span{display:inline-block;margin:0 4px;white-space:nowrap}
.donut .dleg b{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:3px;vertical-align:middle}
.setinput{background:#0c121d;border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:9px 11px;font:inherit;flex:1;min-width:170px}
.setbtn{background:var(--acc);border:0;color:#06122a;border-radius:8px;padding:9px 15px;font-weight:700;cursor:pointer;font-family:inherit}
.setmsg{font-size:12px;margin-top:9px;min-height:15px}
.setmsg.okm{color:var(--grid2)} .setmsg.errm{color:var(--grid)}
.setnote{font-size:11px;color:var(--dim);margin-top:10px;line-height:1.5}
.recs{grid-template-columns:repeat(4,1fr)}
@media(max-width:720px){.recs{grid-template-columns:repeat(2,1fr)}}
.rec{background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.rec .rl{font-size:11px;color:var(--dim);letter-spacing:.3px}
.rec .rv{font-size:19px;font-weight:700;margin-top:3px;font-variant-numeric:tabular-nums}
.rec .rw{font-size:10px;color:var(--dim);margin-top:2px}
.miles{grid-template-columns:repeat(3,1fr)}
@media(max-width:560px){.miles{grid-template-columns:repeat(2,1fr)}}
.mile{background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:13px 10px;text-align:center}
.mile .me{font-size:20px}
.mile .mv{font-size:21px;font-weight:800;margin-top:3px;font-variant-numeric:tabular-nums}
.mile .ml{font-size:11px;color:var(--dim);margin-top:4px;line-height:1.3}
.tvt{background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:13px}
.tvt .tvl{font-size:12px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px}
.tvt .tvv{font-size:26px;font-weight:800;margin-top:4px;font-variant-numeric:tabular-nums}
.tvt .tvd{font-size:13px;margin-top:4px;font-weight:600}
.tvt .up{color:var(--grid2)} .tvt .down{color:var(--grid)} .tvt .flat{color:var(--dim)}
.ctlgrid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}
@media(max-width:600px){.ctlgrid{grid-template-columns:1fr}}
.ctlcard{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.ctlh{font-size:12px;text-transform:uppercase;letter-spacing:.7px;color:var(--dim);font-weight:600}
.ctlnow{font-size:13px;margin:6px 0 11px}
.ctlbtns{display:flex;gap:8px;flex-wrap:wrap}
.mbtn{background:var(--card2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:8px 13px;font-weight:600;cursor:pointer;font-family:inherit;font-size:13px}
.mbtn:hover{border-color:var(--acc)}
.mbtn.on{border-color:var(--grid2);color:var(--grid2);background:#0f2619}
.mbtn.danger{border-color:#5e1f1f;color:#ff9a9a}
/* ===== iOS 27 "Liquid Glass" restyle ===== */
.wrap{max-width:1000px;padding:20px}
header{position:sticky;top:0;z-index:30;margin:-20px -20px 18px;padding:16px 20px;flex-wrap:wrap;
  background:rgba(0,0,0,0.5);backdrop-filter:blur(30px) saturate(180%);-webkit-backdrop-filter:blur(30px) saturate(180%);
  border-bottom:0.5px solid var(--line)}
h1{font-size:22px;font-weight:700;letter-spacing:-0.02em}
.badge{border-radius:980px;border:0.5px solid var(--line);background:rgba(118,118,128,0.18);font-weight:590;padding:4px 11px;
  backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}
.badge.on{background:rgba(48,209,88,0.20);color:#30d158;border-color:transparent}
.badge.off{background:rgba(255,69,58,0.20);color:#ff453a;border-color:transparent}
.toggle{background:rgba(118,118,128,0.24);border:0;border-radius:13px;padding:2px;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}
.tbtn{border-radius:11px;font-weight:590;letter-spacing:-0.01em;padding:7px 15px;color:var(--dim);
  transition:background .25s cubic-bezier(.3,.8,.3,1),color .2s}
.tbtn.active{background:rgba(99,99,102,0.7);color:#fff;box-shadow:0 1px 4px rgba(0,0,0,0.4)}
.tile,.card,.chartcard,.gauge,details,.wccard{border-radius:22px;border:0.5px solid var(--line);background:var(--card);
  backdrop-filter:blur(26px) saturate(165%);-webkit-backdrop-filter:blur(26px) saturate(165%);
  box-shadow:0 1px 1px rgba(0,0,0,0.22),0 10px 30px rgba(0,0,0,0.20)}
.tile{padding:18px}.card{padding:16px}
.tile .val,.card .t{letter-spacing:-0.02em}
.rec,.mile,.tvt,.ctlcard{border-radius:16px;border:0.5px solid var(--line);background:rgba(118,118,128,0.14)}
.chip{border-radius:980px;background:rgba(118,118,128,0.2);border:0.5px solid var(--line);font-weight:590;padding:4px 10px}
.section h2{text-transform:none;letter-spacing:-0.01em;font-size:15px;font-weight:600;color:var(--dim)}
.chartcard h3,.ctlh,.dtitle,.statlbl,.rec .rl,.tvt .tvl,.lbl{text-transform:none;letter-spacing:-0.006em}
.chartcard h3{font-size:14px;font-weight:600}
.kv{border-bottom:0.5px solid var(--line);padding:10px 0;font-size:15px}
.kv .k{color:var(--dim)}
.setbtn,.mbtn{border-radius:12px;font-weight:600;letter-spacing:-0.01em;padding:10px 16px;border:0.5px solid var(--line);
  background:rgba(118,118,128,0.22);color:var(--txt);transition:transform .12s,background .2s}
.setbtn{background:var(--acc);color:#fff;border:0}
.mbtn:active,.setbtn:active,.rbtn:active{transform:scale(0.96)}
.mbtn.on{background:rgba(48,209,88,0.22);color:#30d158;border-color:transparent}
.mbtn.danger{background:rgba(255,69,58,0.18);color:#ff453a;border-color:transparent}
.setinput{border-radius:12px;background:rgba(118,118,128,0.22);border:0.5px solid var(--line);padding:11px 13px}
.rbtn{border-radius:11px;background:rgba(118,118,128,0.2);border:0.5px solid var(--line);font-weight:590;padding:6px 13px}
.rbtn.active{background:var(--acc);color:#fff;border-color:transparent}
.gwrap,.pwbar,.sbar{border-radius:12px;border:0.5px solid var(--line)}
.gfill,.pwbar-fill{border-radius:11px}
.gauge{padding:18px}
svg.flow{filter:drop-shadow(0 10px 30px rgba(0,0,0,0.28))}
.hub rect{fill:rgba(118,118,128,0.3)}
.err{border-radius:16px;background:rgba(255,69,58,0.16);border:0.5px solid rgba(255,69,58,0.4);color:#ff9a90}
details{padding:12px 16px}
summary{font-weight:590}
.dot{box-shadow:0 0 8px var(--grid2)}
.outage{margin:0 0 16px;padding:15px 17px;border-radius:18px;display:flex;gap:13px;align-items:flex-start;
  background:rgba(255,159,10,0.18);border:0.5px solid rgba(255,159,10,0.5);color:#ffd79a;
  backdrop-filter:blur(22px) saturate(160%);-webkit-backdrop-filter:blur(22px) saturate(160%)}
.outage.critical{background:rgba(255,69,58,0.20);border-color:rgba(255,69,58,0.6);color:#ff9f95;animation:opulse 1.7s infinite}
@keyframes opulse{0%,100%{box-shadow:0 0 0 0 rgba(255,69,58,0.45)}50%{box-shadow:0 0 0 7px rgba(255,69,58,0)}}
.outage .oico{font-size:24px;line-height:1}
.outage b{color:#fff;font-weight:700}
.outage .osub{font-size:13px;margin-top:3px;opacity:.92}
.outage .ocrit{font-size:13.5px;margin-top:9px;font-weight:600;line-height:1.4}
svg.chart{cursor:crosshair}
.xhair{stroke:var(--txt);stroke-width:1;opacity:.45;stroke-dasharray:3 3;pointer-events:none}
#ctip{position:fixed;z-index:60;pointer-events:none;display:none;background:rgba(28,28,30,0.94);
  border:0.5px solid var(--line);border-radius:11px;padding:8px 11px;font-size:12.5px;color:var(--txt);
  backdrop-filter:blur(18px) saturate(160%);-webkit-backdrop-filter:blur(18px) saturate(160%);box-shadow:0 10px 30px rgba(0,0,0,0.45)}
#ctip .ctw{color:var(--dim);font-size:11px;margin-bottom:4px}
#ctip b{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px;vertical-align:middle}
#ctip div{line-height:1.5;font-variant-numeric:tabular-nums}
</style></head>
<body><div class="wrap">
<header>
  <h1>⚡ Powerwall Live</h1>
  <span id="gridbadge" class="badge">grid …</span>
  <span id="pwbadge" class="badge">… units</span>
  <span style="flex:1"></span>
  <span class="toggle"><button id="tab-flow" class="tbtn active" onclick="setView('flow')">Flow</button><button id="tab-trends" class="tbtn" onclick="setView('trends')">Trends</button><button id="tab-control" class="tbtn" onclick="setView('control')">Control</button><button id="tab-details" class="tbtn" onclick="setView('details')">Details</button></span>
  <span class="badge"><span class="dot"></span><span id="age">—</span></span>
</header>
<div id="err"></div>
<div id="outage" class="outage" hidden></div>

<div id="view-flow">
<div class="flowwrap">
<svg class="flow" viewBox="0 0 360 412" xmlns="http://www.w3.org/2000/svg">
  <!-- connecting lines (drawn node -> hub) -->
  <path class="base" d="M180,72 L180,196"/>
  <path class="base" d="M300,196 L180,196"/>
  <path class="base" d="M60,196 L180,196"/>
  <path class="base" d="M180,320 L180,196"/>
  <g id="routes"><!-- animated source->hub->destination routes built in JS --></g>

  <!-- central gateway hub -->
  <g class="hub"><rect x="166" y="182" width="28" height="28" rx="6"/>
    <g class="ic" transform="translate(180,196)"><circle r="2.2"/><path d="M-6,-6 L6,6 M6,-6 L-6,6"/></g></g>

  <!-- Solar (top) -->
  <text class="lbl2" x="180" y="16" text-anchor="middle">SOLAR</text>
  <text class="val2" x="180" y="37" text-anchor="middle" id="n_solar_v">—</text>
  <g class="node" id="nd_solar" style="color:var(--solar)" transform="translate(180,72)"><circle r="26"/>
    <g class="ic"><circle r="7"/><path d="M0,-13 L0,-10 M0,10 L0,13 M-13,0 L-10,0 M10,0 L13,0 M-9,-9 L-7,-7 M7,7 L9,9 M9,-9 L7,-7 M-7,7 L-9,9"/></g></g>

  <!-- Grid (bottom, opposite Solar) -->
  <g class="node" id="nd_grid" style="color:var(--grid)" transform="translate(180,320)"><circle r="26"/>
    <g class="ic"><path d="M0,-13 L0,13 M-9,13 L9,13 M-7,-5 L7,-5 M-6,1 L6,1 M-4,-13 L4,-13"/></g></g>
  <text class="lbl2" x="180" y="366" text-anchor="middle">GRID</text>
  <text class="val2" x="180" y="387" text-anchor="middle" id="n_grid_v">—</text>
  <text class="sub2" x="180" y="402" text-anchor="middle" id="n_grid_s"></text>

  <!-- Home (right) -->
  <g class="node" id="nd_home" style="color:var(--home)" transform="translate(300,196)"><circle r="26"/>
    <g class="ic"><path d="M-11,1 L0,-10 L11,1 M-8,-1 L-8,10 L8,10 L8,-1"/></g></g>
  <text class="lbl2" x="300" y="242" text-anchor="middle">HOME</text>
  <text class="val2" x="300" y="263" text-anchor="middle" id="n_home_v">—</text>

  <!-- Powerwall (left) -->
  <g class="node" id="nd_batt" style="color:var(--batt)" transform="translate(60,196)"><circle r="26"/>
    <g class="ic"><rect x="-8" y="-12" width="16" height="24" rx="3"/><path d="M-3,-15 L3,-15"/><path d="M-4,4 L4,4"/></g></g>
  <text class="lbl2" x="60" y="242" text-anchor="middle">POWERWALL·3×</text>
  <text class="val2" x="60" y="263" text-anchor="middle" id="n_batt_v">—</text>
  <text class="sub2" x="60" y="278" text-anchor="middle" id="n_batt_s"></text>
</svg>
</div>
<div class="flowhint">Each moving dot is coloured by its source and travels the whole way to where that power is going — speed tracks the kW. When power comes from two places at once, you'll see two colours converge on the same destination.</div>
<div class="pwbar-wrap">
  <div class="pwbar-top"><span class="pwbar-pct" id="pwbar_pct">—</span><span class="pwbar-eta" id="pwbar_eta"></span></div>
  <div class="pwbar"><div class="pwbar-fill" id="pwbar_fill"></div><div class="pwbar-res" id="pwbar_res" style="display:none"></div></div>
  <div class="pwbar-sub" id="pwbar_sub"></div>
</div>
<div class="wcflow" id="wcflow"></div>
</div>

<div id="view-trends" hidden>
  <div class="ranges" id="ranges">
    <button class="rbtn" data-r="21600">6 h</button>
    <button class="rbtn active" data-r="86400">24 h</button>
    <button class="rbtn" data-r="604800">7 d</button>
    <button class="rbtn" data-r="2592000">30 d</button>
  </div>
  <div id="trends-body">
    <div class="chartcard">
      <h3>Self-powered</h3>
      <div style="display:flex;align-items:center;gap:20px;flex-wrap:wrap">
        <div style="text-align:center">
          <div class="bigpct" id="sp_pct">—</div>
          <div class="statlbl" style="margin-top:6px;text-transform:none;letter-spacing:0">of all energy you drew<br>came from your own solar<br>(grid charging counts against)</div>
        </div>
        <div class="grid donuts" id="donuts" style="flex:1;min-width:270px"></div>
      </div>
    </div>
    <div class="chartcard">
      <h3>Today vs a typical day</h3>
      <div class="grid" style="grid-template-columns:1fr 1fr;gap:12px">
        <div class="tvt" id="tvt_solar"></div>
        <div class="tvt" id="tvt_home"></div>
      </div>
    </div>
    <div class="chartcard"><h3>Records</h3><div class="grid recs" id="recs"></div></div>
    <div class="chartcard"><h3>Milestones</h3><div class="grid miles" id="miles"></div></div>
    <div class="grid charts2">
      <div class="chartcard"><h3 style="color:var(--solar)">☀️ Solar — today vs typical (kW by hour)</h3><svg class="chart" id="chart-prof-solar" viewBox="0 0 640 180" preserveAspectRatio="none"></svg></div>
      <div class="chartcard"><h3 style="color:var(--home)">🏠 Home — today vs typical (kW by hour)</h3><svg class="chart" id="chart-prof-home" viewBox="0 0 640 180" preserveAspectRatio="none"></svg></div>
    </div>
    <div class="grid charts2">
      <div class="chartcard"><h3 style="color:var(--solar)">☀️ Solar — kW</h3><svg class="chart" id="chart-solar" viewBox="0 0 640 170" preserveAspectRatio="none"></svg></div>
      <div class="chartcard"><h3 style="color:var(--home)">🏠 Home — kW</h3><svg class="chart" id="chart-home" viewBox="0 0 640 170" preserveAspectRatio="none"></svg></div>
      <div class="chartcard"><h3 style="color:var(--grid)">⚡ Grid — kW (+import / −export)</h3><svg class="chart" id="chart-grid" viewBox="0 0 640 170" preserveAspectRatio="none"></svg></div>
      <div class="chartcard"><h3 style="color:var(--batt)">🔋 Battery — kW (+discharge / −charge)</h3><svg class="chart" id="chart-batt" viewBox="0 0 640 170" preserveAspectRatio="none"></svg></div>
      <div class="chartcard"><h3 style="color:var(--car)">🔌 Wall Connector — kW</h3><svg class="chart" id="chart-wc" viewBox="0 0 640 170" preserveAspectRatio="none"></svg></div>
    </div>
    <div class="chartcard">
      <h3>Battery charge %</h3>
      <svg class="chart" id="chart-soc" viewBox="0 0 640 180" preserveAspectRatio="none"></svg>
    </div>
    <div class="chartcard">
      <h3>Energy this period</h3>
      <div class="grid kwh" style="margin-top:6px">
        <div class="tile solar"><div class="bar"></div><div class="lbl">Solar produced</div><div class="val" id="k_solar">—</div></div>
        <div class="tile home"><div class="bar"></div><div class="lbl">Home used</div><div class="val" id="k_home">—</div></div>
        <div class="tile batt"><div class="bar"></div><div class="lbl">Battery charged</div><div class="val" id="k_bchg">—</div></div>
        <div class="tile batt"><div class="bar"></div><div class="lbl">Battery discharged</div><div class="val" id="k_bdis">—</div></div>
        <div class="tile grid-t"><div class="bar"></div><div class="lbl">From grid</div><div class="val" id="k_gin">—</div></div>
        <div class="tile grid-t"><div class="bar"></div><div class="lbl">To grid</div><div class="val" id="k_gout">—</div></div>
        <div class="tile"><div class="bar" style="background:var(--car)"></div><div class="lbl">Car charged</div><div class="val" id="k_car">—</div></div>
      </div>
    </div>
  </div>
</div>

<div id="view-control" hidden>
  <div id="ctl_banner" class="err" style="display:none"></div>
  <div class="section"><h2>Powerwall control</h2>
    <div class="ctlgrid">
      <div class="ctlcard">
        <div class="ctlh">Backup reserve</div>
        <div class="ctlnow">Now: <b id="cur_reserve">—</b></div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <input id="reserve_in" class="setinput" type="number" min="0" max="100" step="5" placeholder="0–100 %">
          <button class="setbtn" onclick="setReserve()">Set</button>
        </div>
      </div>
      <div class="ctlcard">
        <div class="ctlh">Operation mode</div>
        <div class="ctlnow">Now: <b id="cur_mode">—</b></div>
        <div class="ctlbtns">
          <button class="mbtn" data-mode="self_consumption" onclick="ctl('mode','self_consumption','Switch to Self-Powered?')">Self-Powered</button>
          <button class="mbtn" data-mode="autonomous" onclick="ctl('mode','autonomous','Switch to Time-Based Control?')">Time-Based</button>
          <button class="mbtn" data-mode="backup" onclick="ctl('mode','backup','Switch to Backup-only?')">Backup</button>
        </div>
      </div>
      <div class="ctlcard">
        <div class="ctlh">Grid charging</div>
        <div class="ctlnow">Now: <b id="cur_gridchg">—</b></div>
        <div class="ctlbtns">
          <button class="mbtn" data-gc="1" onclick="ctl('grid_charging',true,'Allow charging the Powerwalls from the grid?')">Allow</button>
          <button class="mbtn" data-gc="0" onclick="ctl('grid_charging',false,'Stop charging the Powerwalls from the grid?')">Don't allow</button>
        </div>
      </div>
      <div class="ctlcard">
        <div class="ctlh">Grid export</div>
        <div class="ctlnow">Now: <b id="cur_export">—</b></div>
        <div class="ctlbtns">
          <button class="mbtn" data-ex="battery_ok" onclick="ctl('grid_export','battery_ok','Allow export from battery + solar?')">Battery OK</button>
          <button class="mbtn" data-ex="pv_only" onclick="ctl('grid_export','pv_only','Export solar only (not battery)?')">Solar only</button>
          <button class="mbtn" data-ex="never" onclick="ctl('grid_export','never','Never export to grid?')">Never</button>
        </div>
      </div>
    </div>
    <div class="setmsg" id="ctl_msg"></div>
  </div>
  <div class="section"><h2>Advanced</h2>
    <div class="gauge">
      <div class="ctlbtns">
        <button class="mbtn danger" onclick="ctl('off_grid',1,'⚠ Disconnect your home from the grid (island)? Home will run on solar + battery only until you reconnect.')">Go off-grid</button>
        <button class="mbtn" onclick="ctl('reconnect',1,'Reconnect to the grid?')">Reconnect grid</button>
        <button class="mbtn" onclick="ctl('max_backup_on',7200,'Charge Powerwalls toward 100% now (2-hour max-backup)?')">Max backup (2h)</button>
        <button class="mbtn" onclick="ctl('max_backup_off',0,'Cancel the max-backup charge?')">Cancel max backup</button>
      </div>
      <div class="setnote">Off-grid intentionally islands your home. Max-backup charges the Powerwalls to full (useful before a storm). These take effect immediately — confirm carefully.</div>
    </div>
  </div>
</div>

<div id="view-details" hidden>
<div class="grid flows">
  <div class="tile solar"><div class="bar"></div><div class="lbl">Solar</div><div class="val" id="f_solar">—</div><div class="sub" id="s_solar">production</div></div>
  <div class="tile home"><div class="bar"></div><div class="lbl">Home</div><div class="val" id="f_home">—</div><div class="sub" id="s_home">consumption</div></div>
  <div class="tile grid-t"><div class="bar"></div><div class="lbl">Grid</div><div class="val" id="f_grid">—</div><div class="sub" id="s_grid">—</div></div>
  <div class="tile batt"><div class="bar"></div><div class="lbl">Battery</div><div class="val" id="f_batt">—</div><div class="sub" id="s_batt">—</div></div>
</div>

<div class="section"><h2>Battery charge</h2>
  <div class="gauge">
    <div class="gwrap"><div class="gfill" id="gfill"></div><div class="gtxt" id="gtxt">—</div></div>
    <div class="kv" style="margin-top:10px"><span class="k">Energy stored</span><span class="v" id="e_rem">—</span></div>
    <div class="kv"><span class="k">Full pack capacity</span><span class="v" id="e_full">—</span></div>
  </div>
</div>

<div class="section" id="alertsec"><h2>Active alerts</h2><div class="alerts" id="alerts"></div></div>

<div class="section"><h2>Powerwall units</h2><div class="grid cards" id="units"></div></div>

<div class="section"><h2>Wall Connectors</h2><div class="grid cards" id="wcs"></div></div>

<div class="section"><h2>Gateway connection</h2>
  <div class="gauge">
    <div class="kv"><span class="k">Status</span><span class="v" id="conn_status">—</span></div>
    <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
      <input id="pwd_in" class="setinput" type="password" autocomplete="off" placeholder="Gateway (TEG) Wi-Fi password">
      <button class="setbtn" onclick="savePwd()">Save &amp; reconnect</button>
    </div>
    <div class="setmsg" id="pwd_msg"></div>
    <div class="setnote"><b>Which password?</b> The Gateway (TEG) password on the sticker behind your Powerwall's front cover — the same one used for the <b>TeslaPW_…</b> Wi-Fi network. <b>Not</b> your Tesla account password. You only need this if the connection ever drops; it's already saved. It's checked against your gateway, stored locally on the Pi (owner-readable only), and sent only to your Powerwall — never anywhere else.</div>
  </div>
</div>

<div class="section"><h2>Everything else</h2>
  <details><summary>Raw vitals (CT readings, temps, fan speeds)</summary><pre id="raw_vitals"></pre></details>
  <details><summary>Raw blocks &amp; PW3 pack data</summary><pre id="raw_blocks"></pre></details>
  <details><summary>Full JSON payload</summary><pre id="raw_all"></pre></details>
</div>
</div>

<footer>
  <span>DIN: <b id="din">—</b></span>
  <span>Firmware: <b id="fw">—</b></span>
  <span>Source: local TEDAPI · <b id="host"></b></span>
</footer>
</div>
<script>
const $=id=>document.getElementById(id);
const W=w=>{if(w===null||w===undefined)return'—';const a=Math.abs(w);
  if(a>=1000)return (w/1000).toFixed(2)+'<span> kW</span>';return Math.round(w)+'<span> W</span>';};
const Wh=v=>v==null?'—':(v>=1000?(v/1000).toFixed(2)+' kWh':Math.round(v)+' Wh');
const kW=w=>{if(w==null)return'—';const k=Math.abs(w)/1000;return (k<0.05?'0.0':k.toFixed(1))+' kW';};

let _histTimer=null;
function setView(v){
  ['flow','trends','control','details'].forEach(x=>{
    $('view-'+x).hidden=(x!==v);
    $('tab-'+x).classList.toggle('active',x===v);
  });
  try{localStorage.setItem('pwview',v);}catch(e){}
  clearInterval(_histTimer); _histTimer=null;
  if(v==='trends'){ loadHistory(); loadStats(); _histTimer=setInterval(()=>{loadHistory();loadStats();},30000); }
}
const THRESH=50; // W below this = "idle", line off
function liveNode(id,on){$(id).classList.toggle('live',on);}

// node centres; every route is drawn source -> hub -> destination
const POS={solar:[180,72],grid:[180,320],home:[300,196],batt:[60,196],hub:[180,196]};
const PERP={solar:[1,0],grid:[1,0],home:[0,1],batt:[0,1]}; // lane-offset axis per leg
const LANE=8; // px between parallel route lanes
// Priority allocation that matches real power flow (and your settings):
// solar serves home, then charges the battery, then exports; battery and grid
// only fill what's LEFT. So grid reaches the battery only if grid import truly
// exceeds home demand (real grid-charging) — never as a proportional artifact.
function computeFlows(solar,home,grid,batt,gridChg){
  let S=Math.max(solar,0);        // solar production (source)
  let Gi=Math.max(grid,0);        // grid import (source)
  let Bd=Math.max(batt,0);        // battery discharge (source)
  let H=Math.max(home,0);         // home load (sink)
  let Bc=Math.max(-batt,0);       // battery charge (sink)
  let Ge=Math.max(-grid,0);       // grid export (sink)
  const flows=[]; let x;
  const add=(sc,sk,w)=>{ if(w>THRESH) flows.push({sc,sk,w}); };
  if(gridChg===false){
    // grid charging disallowed → the battery can ONLY be charged by solar.
    x=Math.min(S,Bc); add('solar','batt',x); S-=x; Bc-=x; // solar charges battery first
    x=Math.min(S,H);  add('solar','home',x); S-=x; H-=x;  // then serves home
    x=Math.min(S,Ge); add('solar','grid',x); S-=x; Ge-=x; // then exports
    x=Math.min(Bd,H); add('batt','home',x);  Bd-=x; H-=x; // battery discharge to home
    x=Math.min(Gi,H); add('grid','home',x);  Gi-=x; H-=x; // grid covers the rest of home
    x=Math.min(Bd,Ge);add('batt','grid',x);  Bd-=x; Ge-=x;
    // deliberately NO grid->battery: your setting forbids it
  }else{
    // grid charging allowed → conventional: solar serves home first, grid may charge
    x=Math.min(S,H);  add('solar','home',x); S-=x; H-=x;
    x=Math.min(S,Bc); add('solar','batt',x); S-=x; Bc-=x;
    x=Math.min(S,Ge); add('solar','grid',x); S-=x; Ge-=x;
    x=Math.min(Bd,H); add('batt','home',x);  Bd-=x; H-=x;
    x=Math.min(Gi,H); add('grid','home',x);  Gi-=x; H-=x;
    x=Math.min(Gi,Bc);add('grid','batt',x);  Gi-=x; Bc-=x;
    x=Math.min(Bd,Ge);add('batt','grid',x);  Bd-=x; Ge-=x;
  }
  return flows;
}

function renderFlow(solar,home,grid,batt,soc,gridChg){
  $('n_solar_v').textContent=kW(solar);
  $('n_home_v').textContent=kW(home);
  $('n_grid_v').textContent=kW(grid);
  $('n_batt_v').textContent=kW(batt);
  $('n_grid_s').textContent = grid>THRESH?'importing':(grid<-THRESH?'exporting':'idle');
  $('n_batt_s').textContent = (soc!=null?soc+'%':'') + (batt<-THRESH?' · charging':(batt>THRESH?' · discharging':' · idle'));
  const flows=computeFlows(solar,home,grid,batt,gridChg);
  // Each flow is ONE continuous path source -> hub -> destination with a single
  // animation, so its dots line up evenly the whole way (like the Tesla app).
  // When a source feeds several places, or a place is fed by several sources,
  // the paths fan into side-by-side lanes so colours sit next to each other.
  const groups={};
  flows.forEach(fl=>{(groups[fl.sc]=groups[fl.sc]||[]).push(fl);(groups[fl.sk]=groups[fl.sk]||[]).push(fl);});
  for(const node in groups){const arr=groups[node];
    arr.forEach((fl,i)=>{const off=(i-(arr.length-1)/2)*LANE;
      if(fl.sc===node)fl.offS=off; if(fl.sk===node)fl.offK=off;});}
  let g='';
  flows.forEach(fl=>{
    const w=Math.max(2,Math.min(5.5,2+fl.w/1600));
    const dur=Math.max(0.7,Math.min(3.2,1.8/(fl.w/1000+0.2)));
    const ps=PERP[fl.sc],pk=PERP[fl.sk],oS=fl.offS||0,oK=fl.offK||0;
    const S=POS[fl.sc],K=POS[fl.sk],H=POS.hub;
    const x1=S[0]+ps[0]*oS,y1=S[1]+ps[1]*oS;   // source node (its lane)
    const x2=H[0]+ps[0]*oS,y2=H[1]+ps[1]*oS;    // reach hub in the source lane
    const x3=H[0]+pk[0]*oK,y3=H[1]+pk[1]*oK;    // cross to the destination lane
    const x4=K[0]+pk[0]*oK,y4=K[1]+pk[1]*oK;    // destination node (its lane)
    const d='M'+x1+','+y1+' L'+x2+','+y2+' L'+x3+','+y3+' L'+x4+','+y4;
    g+='<path class="route" style="stroke:var(--'+fl.sc+');stroke-width:'+w.toFixed(1)+
       ';animation-duration:'+dur.toFixed(2)+'s" d="'+d+'"/>';
  });
  $('routes').innerHTML=g;
  const act={}; flows.forEach(fl=>{act[fl.sc]=1; act[fl.sk]=1;});
  liveNode('nd_solar',!!act.solar); liveNode('nd_grid',!!act.grid);
  liveNode('nd_home',!!act.home);   liveNode('nd_batt',!!act.batt);
}

// ---------- Trends / history charts (self-contained SVG, no libs) ----------
let _range=86400;
function cssVar(v){return getComputedStyle(document.documentElement).getPropertyValue(v).trim()||'#888';}
const COL={solar:cssVar('--solar'),home:cssVar('--home'),grid:cssVar('--grid'),batt:cssVar('--batt'),car:cssVar('--car')};
function niceMax(v){if(v<=0)return 1;const p=Math.pow(10,Math.floor(Math.log10(v)));const n=v/p;const s=n<=1?1:n<=2?2:n<=5?5:10;return s*p;}
function fmtTime(ts){const d=new Date(ts*1000),p=x=>String(x).padStart(2,'0');
  if(_range>86400)return (d.getMonth()+1)+'/'+d.getDate();
  let h=d.getHours(),ap=h<12?'a':'p';h=h%12||12;const m=d.getMinutes();
  return h+(m?':'+p(m):'')+ap;}
// ---- drag / hover to read a chart's values at a point ----
let _ctip;
function tipEl(){if(!_ctip){_ctip=document.createElement('div');_ctip.id='ctip';document.body.appendChild(_ctip);}return _ctip;}
function hideTip(svg){tipEl().style.display='none';if(svg){const c=svg.querySelector('.xhair');if(c)c.remove();}}
function nearestIdx(a,t){const n=a.length;if(t<=a[0])return 0;if(t>=a[n-1])return n-1;let lo=0,hi=n-1;
  while(hi-lo>1){const m=(lo+hi)>>1;if(a[m]<t)lo=m;else hi=m;}return (t-a[lo]<=a[hi]-t)?lo:hi;}
function hourLabel(h){return (h%12||12)+' '+(h<12?'AM':'PM');}
function wireChart(svg,cx){svg._cx=cx;
  if(svg._wired)return;svg._wired=true;
  const mv=e=>chartScrub(svg,e);
  svg.addEventListener('pointermove',mv);
  svg.addEventListener('pointerdown',mv);
  svg.addEventListener('pointerleave',()=>hideTip(svg));
}
function chartScrub(svg,e){
  const cx=svg._cx;if(!cx||!cx.times||cx.times.length<1)return;
  const vb=svg.viewBox.baseVal,rect=svg.getBoundingClientRect();if(!rect.width)return;
  const px=(e.clientX-rect.left)/rect.width*vb.width;
  let idx,xx;
  if(cx.hour){let f=(px-cx.x0)/(cx.x1-cx.x0);f=Math.max(0,Math.min(1,f));idx=Math.round(f*23);xx=cx.x0+idx/23*(cx.x1-cx.x0);}
  else{const t=cx.tmin+(px-cx.x0)/(cx.x1-cx.x0)*(cx.tmax-cx.tmin);idx=nearestIdx(cx.times,t);xx=cx.x0+(cx.times[idx]-cx.tmin)/((cx.tmax-cx.tmin)||1)*(cx.x1-cx.x0);}
  let ln=svg.querySelector('.xhair');
  if(!ln){ln=document.createElementNS('http://www.w3.org/2000/svg','line');ln.setAttribute('class','xhair');svg.appendChild(ln);}
  ln.setAttribute('x1',xx.toFixed(1));ln.setAttribute('x2',xx.toFixed(1));ln.setAttribute('y1',cx.y1);ln.setAttribute('y2',cx.y0);
  const when=cx.hour?hourLabel(idx):fmtWhen(cx.times[idx]);
  let rows='';
  (cx.series||[]).forEach(s=>{const v=(s.data||[])[idx];if(v==null)return;
    const disp=cx.unit==='%'?Math.round(v)+'%':(v/1000).toFixed(2)+' kW';
    rows+='<div><b style="background:'+s.color+'"></b>'+(s.name?s.name+' ':'')+disp+'</div>';});
  if(!rows)rows='<div style="color:var(--dim)">no reading</div>';
  const tp=tipEl();tp.innerHTML='<div class="ctw">'+when+'</div>'+rows;tp.style.display='block';
  tp.style.left=Math.max(6,Math.min(e.clientX+14,window.innerWidth-160))+'px';
  tp.style.top=(e.clientY+16)+'px';
}

function lineChart(svgId,times,lines,opt){
  const svg=$(svgId),vb=svg.viewBox.baseVal,w=vb.width,h=vb.height;
  const pad={l:40,r:10,t:10,b:22},x0=pad.l,x1=w-pad.r,y0=h-pad.b,y1=pad.t;
  if(!times||times.length<2){
    svg.innerHTML='<text x="'+(w/2)+'" y="'+(h/2)+'" text-anchor="middle" fill="#8ea0bd" font-size="12">collecting data… check back soon</text>';return;}
  let mn=Infinity,mx=-Infinity;
  lines.forEach(L=>L.data.forEach(v=>{if(v==null)return;if(v<mn)mn=v;if(v>mx)mx=v;}));
  if(!isFinite(mn))mn=0;if(!isFinite(mx))mx=1;
  if(opt.symmetric){const a=niceMax(Math.max(Math.abs(mn),Math.abs(mx),1));mn=-a;mx=a;}
  else{mn=opt.ymin!=null?opt.ymin:Math.min(mn,0); mx=opt.ymax!=null?opt.ymax:niceMax(mx);}
  if(mx===mn)mx=mn+1;
  const tmin=times[0],tmax=times[times.length-1],tr=(tmax-tmin)||1;
  const sx=t=>x0+(t-tmin)/tr*(x1-x0),sy=v=>y0-(v-mn)/(mx-mn)*(y0-y1);
  let g='';
  for(let i=0;i<=4;i++){const v=mn+(mx-mn)*i/4,yy=sy(v).toFixed(1);
    g+='<line class="chartgrid" x1="'+x0+'" y1="'+yy+'" x2="'+x1+'" y2="'+yy+'"/>'+
       '<text class="charty" x="'+(x0-4)+'" y="'+(+yy+3)+'" text-anchor="end">'+opt.yfmt(v)+'</text>';}
  if(mn<0&&mx>0){const yz=sy(0).toFixed(1);g+='<line class="chartzero" x1="'+x0+'" y1="'+yz+'" x2="'+x1+'" y2="'+yz+'"/>';}
  for(let i=0;i<=5;i++){const t=tmin+tr*i/5,xx=sx(t).toFixed(1);
    g+='<text class="chartx" x="'+xx+'" y="'+(h-6)+'" text-anchor="middle">'+fmtTime(t)+'</text>';}
  lines.forEach(L=>{
    if(L.area){const pts=[];for(let i=0;i<times.length;i++)if(L.data[i]!=null)pts.push([sx(times[i]),sy(L.data[i])]);
      if(pts.length>1){let a='M'+pts[0][0].toFixed(1)+','+y0+' ';
        pts.forEach(p=>a+='L'+p[0].toFixed(1)+','+p[1].toFixed(1)+' ');
        a+='L'+pts[pts.length-1][0].toFixed(1)+','+y0+' Z';
        g+='<path d="'+a+'" fill="'+L.color+'" opacity="0.13"/>';}}
    let dAttr='',pen=false;
    for(let i=0;i<times.length;i++){const v=L.data[i];
      if(v==null){pen=false;continue;}
      dAttr+=(pen?'L':'M')+sx(times[i]).toFixed(1)+','+sy(v).toFixed(1)+' ';pen=true;}
    g+='<path d="'+dAttr+'" fill="none" stroke="'+L.color+'" stroke-width="'+(L.w||1.6)+
       '" stroke-linejoin="round" stroke-linecap="round" opacity="'+(L.op||1)+'"/>';});
  svg.innerHTML=g;
  wireChart(svg,{times,series:lines,x0,x1,tmin,tmax,y0,y1,hour:false,unit:opt.unit||'kW'});
}

// parts: [centerName, value, color, legendLabel]
function donut(title,parts){
  const tot=parts.reduce((a,p)=>a+(p[1]||0),0)||1;
  const r=42,C=2*Math.PI*r,cx=55,cy=55,sw=14;
  let off=0,segs='';
  parts.forEach(p=>{const f=(p[1]||0)/tot; if(f<=0)return;
    const len=f*C;
    segs+='<circle cx="'+cx+'" cy="'+cy+'" r="'+r+'" fill="none" stroke="'+p[2]+'" stroke-width="'+sw+
      '" stroke-dasharray="'+len.toFixed(2)+' '+(C-len).toFixed(2)+'" stroke-dashoffset="'+(-off).toFixed(2)+
      '" transform="rotate(-90 '+cx+' '+cy+')"/>';
    off+=len;});
  let big=parts[0]; parts.forEach(p=>{if((p[1]||0)>(big[1]||0))big=p;});
  const bigpct=Math.round((big[1]||0)/tot*100);
  const center='<text x="'+cx+'" y="'+(cy-1)+'" text-anchor="middle" font-size="21" font-weight="800" fill="var(--txt)">'+bigpct+'%</text>'+
    '<text x="'+cx+'" y="'+(cy+13)+'" text-anchor="middle" font-size="9" fill="var(--dim)">'+big[0]+'</text>';
  const leg=parts.filter(p=>(p[1]||0)>0).map(p=>'<span><b style="background:'+p[2]+'"></b>'+p[3]+' '+Math.round((p[1]||0)/tot*100)+'%</span>').join('');
  return '<div class="donut"><div class="dtitle">'+title+'</div>'+
    '<svg viewBox="0 0 110 110"><circle cx="'+cx+'" cy="'+cy+'" r="'+r+'" fill="none" stroke="var(--line)" stroke-width="'+sw+'"/>'+segs+center+'</svg>'+
    '<div class="dleg">'+leg+'</div></div>';
}
function drawTrends(d){
  const t=d.t||[];
  const kwf=v=>(v/1000).toFixed(1);
  lineChart('chart-solar',t,[{data:d.solar||[],color:COL.solar,w:1.8,area:true}],{ymin:0,yfmt:kwf});
  lineChart('chart-home', t,[{data:d.home||[], color:COL.home, w:1.8,area:true}],{ymin:0,yfmt:kwf});
  lineChart('chart-grid', t,[{data:d.grid||[], color:COL.grid, w:1.8}],{symmetric:true,yfmt:kwf});
  lineChart('chart-batt', t,[{data:d.batt||[], color:COL.batt, w:1.8}],{symmetric:true,yfmt:kwf});
  lineChart('chart-wc',   t,[{data:d.wc||[],   color:COL.car,  w:1.8,area:true}],{ymin:0,yfmt:kwf});
  lineChart('chart-soc',t,[{data:d.soc||[],color:COL.batt,w:2,area:true,name:'charge'}],
    {ymin:0,ymax:100,unit:'%',yfmt:v=>Math.round(v)+''});
  const k=d.kwh||{},kwh=x=>x==null?'—':x.toFixed(1)+' kWh';
  $('k_solar').textContent=kwh(k.solar); $('k_home').textContent=kwh(k.home);
  $('k_bchg').textContent=kwh(k.batt_charge); $('k_bdis').textContent=kwh(k.batt_discharge);
  $('k_gin').textContent=kwh(k.grid_in); $('k_gout').textContent=kwh(k.grid_out);
  $('k_car').textContent=kwh(k.car);
  // self-powered = own solar / (own solar + all grid imported), so grid used
  // to charge the Powerwalls counts against you too
  const hs=k.home_solar||0,hb=k.home_batt||0,hg=k.home_grid||0;
  const solT=k.solar||0, gi=k.grid_in||0, ext=solT+gi;
  $('sp_pct').textContent=(ext>0?Math.round(solT/ext*100):0)+'%';
  $('donuts').innerHTML=
    donut('Home from',[['solar',hs,COL.solar,'☀'],['battery',hb,COL.batt,'🔋'],['grid',hg,COL.grid,'⚡']])+
    donut('Solar to',[['home',hs,COL.home,'🏠'],['battery',k.solar_batt||0,COL.batt,'🔋'],['export',k.solar_grid||0,COL.grid,'⚡']])+
    donut('Grid to',[['home',hg,COL.home,'🏠'],['battery',k.grid_batt||0,COL.batt,'🔋']]);
}
async function loadHistory(){
  try{const r=await fetch('history.json?range='+_range,{cache:'no-store'});drawTrends(await r.json());}
  catch(e){}
}
function profileChart(svgId,lines){
  const svg=$(svgId),vb=svg.viewBox.baseVal,w=vb.width,h=vb.height;
  const pad={l:40,r:10,t:10,b:22},x0=pad.l,x1=w-pad.r,y0=h-pad.b,y1=pad.t;
  let mx=-Infinity,mn=0;
  lines.forEach(L=>(L.data||[]).forEach(v=>{if(v==null)return;if(v>mx)mx=v;if(v<mn)mn=v;}));
  if(!isFinite(mx)||mx<=0)mx=1; mx=niceMax(mx); if(mn<0)mn=-niceMax(-mn);
  const sx=i=>x0+i/23*(x1-x0), sy=v=>y0-(v-mn)/(mx-mn)*(y0-y1);
  let g='';
  for(let k=0;k<=4;k++){const v=mn+(mx-mn)*k/4,yy=sy(v).toFixed(1);
    g+='<line class="chartgrid" x1="'+x0+'" y1="'+yy+'" x2="'+x1+'" y2="'+yy+'"/><text class="charty" x="'+(x0-4)+'" y="'+(+yy+3)+'" text-anchor="end">'+(v/1000).toFixed(1)+'</text>';}
  if(mn<0){const yz=sy(0).toFixed(1);g+='<line class="chartzero" x1="'+x0+'" y1="'+yz+'" x2="'+x1+'" y2="'+yz+'"/>';}
  [[0,'12a'],[6,'6a'],[12,'12p'],[18,'6p'],[23,'11p']].forEach(p=>{const xx=sx(p[0]).toFixed(1);
    g+='<text class="chartx" x="'+xx+'" y="'+(h-6)+'" text-anchor="middle">'+p[1]+'</text>';});
  lines.forEach(L=>{let d='',pen=false;const data=L.data||[];
    for(let i=0;i<24;i++){const v=data[i];if(v==null){pen=false;continue;}d+=(pen?'L':'M')+sx(i).toFixed(1)+','+sy(v).toFixed(1)+' ';pen=true;}
    g+='<path d="'+d+'" fill="none" stroke="'+L.color+'" stroke-width="'+(L.w||1.8)+'" stroke-linejoin="round" stroke-linecap="round" opacity="'+(L.op||1)+'"'+(L.dash?' stroke-dasharray="4 4"':'')+'/>';});
  svg.innerHTML=g;
  wireChart(svg,{times:Array.from({length:24},(_,i)=>i),series:lines,x0,x1,y0,y1,hour:true,unit:'kW'});
}
function fmtWhen(ts){if(!ts)return'';const d=new Date(ts*1000),p=x=>String(x).padStart(2,'0');
  let hh=d.getHours(),ap=hh<12?'AM':'PM';hh=hh%12||12;return hh+':'+p(d.getMinutes())+' '+ap+' · '+(d.getMonth()+1)+'/'+d.getDate();}
function tvtTile(el,label,todayKwh,typicalKwh,pct,ready){
  let cls='flat',arrow='',txt;
  if(!ready)txt='building baseline — compares once you have a full prior day';
  else if(pct==null)txt='not enough data yet today';
  else if(pct>3){cls='up';arrow='▲ ';txt=pct+'% above a normal day';}
  else if(pct<-3){cls='down';arrow='▼ ';txt=(-pct)+'% below a normal day';}
  else txt='about a normal day';
  el.innerHTML='<div class="tvl">'+label+' today</div>'+
    '<div class="tvv">'+(todayKwh!=null?todayKwh.toFixed(1):'—')+' kWh</div>'+
    '<div class="tvd '+cls+'">'+arrow+txt+'</div>'+
    (ready?'<div class="tvl" style="margin-top:6px;text-transform:none">typical by now: '+(typicalKwh!=null?typicalKwh.toFixed(1):'—')+' kWh</div>':'');
}
function renderStats(d){
  const r=d.records||{}, kwv=o=>o?{disp:kW(o.w),ts:o.ts}:null;
  const recs=[
    ['☀️ Peak solar',kwv(r.peak_solar)],['🏠 Peak home',kwv(r.peak_home)],
    ['⚡ Peak grid draw',kwv(r.peak_grid_imp)],['⚡ Peak export',kwv(r.peak_grid_exp)],
    ['🔋 Peak discharge',kwv(r.peak_batt_dis)],['🔋 Peak charge',kwv(r.peak_batt_chg)],
    ['▲ Highest charge',r.max_soc?{disp:r.max_soc.v+'%',ts:r.max_soc.ts}:null],
    ['▼ Lowest charge',r.min_soc?{disp:r.min_soc.v+'%',ts:r.min_soc.ts}:null]];
  $('recs').innerHTML=recs.map(x=>x[1]?'<div class="rec"><div class="rl">'+x[0]+'</div><div class="rv">'+x[1].disp+'</div><div class="rw">'+fmtWhen(x[1].ts)+'</div></div>':'').join('');
  const t=d.totals||{}, nf=n=>(n==null?'—':Math.round(n).toLocaleString());
  const mile=(e,v,l)=>'<div class="mile"><div class="me">'+e+'</div><div class="mv">'+v+'</div><div class="ml">'+l+'</div></div>';
  $('miles').innerHTML=[
    mile('☀️',nf(t.solar_kwh)+' kWh','solar generated'),
    mile('🌱',nf(t.co2_kg)+' kg','CO₂ emissions avoided'),
    mile('🚗',nf(t.ev_miles),'EV-miles of charge'),
    mile('🔄',(t.equiv_cycles!=null?t.equiv_cycles:'—'),'full battery cycles'),
    mile('🌳',(t.trees!=null?t.trees:'—'),'tree-years of CO₂'),
    mile('📅',(t.avg_solar_day!=null?t.avg_solar_day.toFixed(1):'—')+' kWh','avg solar / day')].join('');
  const ty=d.today||{}, ready=(d.baseline_days||0)>=1;
  tvtTile($('tvt_solar'),'☀️ Solar',ty.solar_kwh,ty.typical_solar_sofar,ty.vs_solar_pct,ready);
  tvtTile($('tvt_home'),'🏠 Home',ty.home_kwh,ty.typical_home_sofar,ty.vs_home_pct,ready);
  const p=d.profile||{};
  profileChart('chart-prof-solar',[{data:p.solar,color:COL.solar,w:1.6,op:.45,dash:true,name:'typical'},{data:ty.profile_solar,color:COL.solar,w:2.4,name:'today'}]);
  profileChart('chart-prof-home', [{data:p.home, color:COL.home, w:1.6,op:.45,dash:true,name:'typical'},{data:ty.profile_home, color:COL.home, w:2.4,name:'today'}]);
}
async function loadStats(){
  try{const r=await fetch('stats.json?range='+_range,{cache:'no-store'});renderStats(await r.json());}catch(e){}
}

async function savePwd(){
  const el=$('pwd_in'),msg=$('pwd_msg'),pwd=el.value.trim();
  if(!pwd){msg.className='setmsg errm';msg.textContent='Enter a password first.';return;}
  msg.className='setmsg';msg.textContent='Checking against the gateway…';
  try{
    const r=await fetch('set_password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pwd})});
    const d=await r.json();
    if(d.ok){msg.className='setmsg okm';msg.textContent='✓ Connected and saved — reconnecting now.';el.value='';}
    else{msg.className='setmsg errm';msg.textContent='✗ '+(d.error||'failed');}
  }catch(e){msg.className='setmsg errm';msg.textContent='✗ '+e;}
}

function setReserve(){const v=$('reserve_in').value; if(v==='')return;
  ctl('reserve',v,'Set backup reserve to '+v+'%?');}
async function ctl(action,value,confirmMsg){
  if(confirmMsg && !confirm(confirmMsg))return;
  const msg=$('ctl_msg'); msg.className='setmsg'; msg.textContent='Sending…';
  try{
    const r=await fetch('control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,value})});
    const d=await r.json();
    if(d.ok){msg.className='setmsg okm';msg.textContent='✓ Done — settings update shortly.';load();}
    else{msg.className='setmsg errm';msg.textContent='✗ '+(d.error||'failed');}
  }catch(e){msg.className='setmsg errm';msg.textContent='✗ '+e;}
}
const MODE_LABEL={self_consumption:'Self-Powered',autonomous:'Time-Based',backup:'Backup'};
function renderControl(st){
  $('cur_reserve').textContent = st.reserve!=null?st.reserve+'%':'—';
  $('cur_mode').textContent = MODE_LABEL[st.mode]||st.mode||'—';
  $('cur_gridchg').textContent = st.grid_charging==null?'—':(st.grid_charging?'allowed':'not allowed');
  $('cur_export').textContent = ({battery_ok:'battery OK',pv_only:'solar only',never:'never'})[st.grid_export]||st.grid_export||'—';
  document.querySelectorAll('#view-control [data-mode]').forEach(b=>b.classList.toggle('on',b.dataset.mode===st.mode));
  document.querySelectorAll('#view-control [data-gc]').forEach(b=>b.classList.toggle('on',(b.dataset.gc==='1')===!!st.grid_charging));
  document.querySelectorAll('#view-control [data-ex]').forEach(b=>b.classList.toggle('on',b.dataset.ex===st.grid_export));
  const banner=$('ctl_banner');
  if(st.control_available===false){banner.style.display='block';
    banner.innerHTML='⚠ Local control isn\\'t enabled yet — register an RSA key on the Pi to unlock these buttons. Settings above are read-only until then.';}
  else{banner.style.display='none';}
}

function fmtDur(h){if(!isFinite(h)||h<=0)return '';const m=Math.round(h*60),H=Math.floor(m/60),M=m%60;return (H?H+'h ':'')+M+'m';}
let _wasOutage=false,_wasCrit=false;
function notify(msg){try{if(!('Notification'in window))return;
  if(Notification.permission==='granted')new Notification('⚡ Powerwall',{body:msg,tag:'pw-outage',renotify:true});
  else if(Notification.permission!=='denied')Notification.requestPermission();}catch(e){}}
function wcChargeW(d){let p=0;(d.wc||[]).forEach(c=>{const v=c.vitals||{};if(v.contactor_closed)p+=(v.vehicle_current_a||0)*(v.grid_v||0);});return Math.round(p);}
function renderOutage(d,batt,e){
  const el=$('outage'); if(!el)return;
  const out=(d.grid_connected===false)||d.islanded===true;
  if(!out){el.hidden=true;_wasOutage=false;_wasCrit=false;return;}
  const soc=e.soc,rem=e.remainingWh;
  const resPct=(d.settings&&d.settings.reserve!=null)?d.settings.reserve:null;
  const resWh=(resPct!=null&&e.fullWh)?resPct/100*e.fullWh:null;
  let eta='';
  if(batt>30&&rem!=null){
    const toRes=(resWh!=null&&rem>resWh)?(rem-resWh)/batt:null;
    eta=' · ~'+fmtDur(rem/batt)+' until empty'+(toRes?' ('+fmtDur(toRes)+' until backup reserve)':'')+' at current load';
  }
  const wcp=wcChargeW(d), crit=wcp>500;
  let html='<div class="oico">'+(crit?'🚗⚡':'⚡')+'</div><div style="flex:1">'+
    '<b>GRID OUTAGE — running on Powerwall backup</b>'+
    '<div class="osub">'+(soc!=null?soc+'% remaining':'')+eta+'</div>';
  if(crit){html+='<div class="ocrit">⚠️ Your Wall Connector is charging at '+kW(wcp)+' during the outage — this is draining your backup fast. Stop the car charging now in the Tesla app'+
    ((d.settings&&d.settings.control_available)?' or from the Control tab':'')+'.</div>';}
  html+='</div>';
  el.innerHTML=html; el.hidden=false; el.classList.toggle('critical',crit);
  if((out&&!_wasOutage)||(crit&&!_wasCrit))
    notify(crit?('Grid is DOWN and your car is charging at '+kW(wcp)+' — draining backup fast. Stop charging!'):'Grid outage — now running on Powerwall backup.');
  _wasOutage=out; _wasCrit=crit;
}
function renderPwBar(batt,e,settings){
  const soc=e.soc,rem=e.remainingWh,full=e.fullWh;
  const resPct=(settings&&settings.reserve!=null)?settings.reserve:null;
  $('pwbar_pct').textContent = soc!=null?soc+'%':'—';
  $('pwbar_fill').style.width=(soc!=null?soc:0)+'%';
  const resEl=$('pwbar_res');
  if(resPct!=null){resEl.style.display='block';resEl.style.left='calc('+resPct+'% - 1px)';}
  else{resEl.style.display='none';}
  const resWh=(resPct!=null&&full)?resPct/100*full:null;
  let eta='',sub='';
  if(batt<-50 && full && rem!=null){
    eta='⚡ Full in '+fmtDur((full-rem)/(-batt));
    sub='charging at '+kW(batt)+(resPct!=null?' · backup reserve '+resPct+'%':'');
  }else if(batt>50 && rem!=null){
    if(resWh!=null && rem>resWh){eta=fmtDur((rem-resWh)/batt)+' until backup reserve';}
    else{eta=fmtDur(rem/batt)+' until empty';}
    sub='discharging at '+kW(batt)+(resPct!=null?' · reserve '+resPct+'%':'');
  }else{
    sub='idle'+(resPct!=null?' · backup reserve '+resPct+'%':'');
  }
  $('pwbar_eta').textContent=eta; $('pwbar_sub').textContent=sub;
}
function renderWCFlow(wc){
  const el=$('wcflow'); if(!el)return;
  if(!wc||!wc.length){el.innerHTML='';return;}
  el.innerHTML=wc.map((c,i)=>{
    const v=c.vitals||{},online=!!c.vitals,charging=!!v.contactor_closed,connected=!!v.vehicle_connected;
    const pw=charging?Math.round((v.vehicle_current_a||0)*(v.grid_v||0)):0;
    const state=!online?'offline':(charging?'Charging':(connected?'Plugged in':'Idle'));
    return '<div class="wccard'+(charging?' charging':'')+'">'+
      '<div class="wct">🔌 Wall Connector'+(wc.length>1?' '+(i+1):'')+'</div>'+
      '<div class="wcp" style="color:'+(charging?'var(--car)':'var(--dim)')+'">'+(charging?kW(pw):(connected?'0 kW':'—'))+'</div>'+
      '<div class="wcs">'+state+'</div>'+
      (charging?'<div class="wcbar"><i></i></div>':'')+'</div>';
  }).join('');
}

function render(d){
  $('host').textContent=location.host;
  renderControl(d.settings||{});
  const cs=$('conn_status');
  if(cs){cs.textContent=d.ok?'connected ✓':('error — '+(d.error||'no data'));
    cs.style.color=d.ok?'var(--grid2)':'var(--grid)';}
  if(!d.ok){$('err').innerHTML='<div class="err">⚠ '+(d.error||'no data')+' — if this looks like a login problem, open <b>Details → Gateway connection</b> to re-enter the password.</div>';return;}
  $('err').innerHTML='';
  const f=d.flows||{};
  const solar=f.SOLAR??0, home=f.LOAD??0, grid=f.SITE??0, batt=f.BATTERY??0;
  $('f_solar').innerHTML=W(solar);
  $('f_home').innerHTML=W(home);
  $('f_grid').innerHTML=W(Math.abs(grid));
  $('s_grid').textContent = grid>5?('importing from grid'):(grid<-5?'exporting to grid':'idle');
  // Battery: negative realPowerW = charging, positive = discharging
  $('f_batt').innerHTML=W(Math.abs(batt));
  $('s_batt').textContent = batt<-5?'charging':(batt>5?'discharging':'idle');

  const e=d.energy||{};
  if(e.soc!=null){$('gfill').style.width=e.soc+'%';$('gtxt').textContent=e.soc+'%';}
  $('e_rem').textContent=Wh(e.remainingWh);
  $('e_full').textContent=Wh(e.fullWh);

  renderFlow(solar,home,grid,batt,e.soc,(d.settings||{}).grid_charging);
  renderPwBar(batt,e,d.settings);
  renderWCFlow(d.wc||[]);
  renderOutage(d,batt,e);

  const gb=$('gridbadge');
  gb.textContent = d.grid_connected?'grid connected':(d.islanded?'off-grid (islanded)':'grid status unknown');
  gb.className='badge '+(d.grid_connected?'on':(d.islanded?'off':''));

  const blocks=d.blocks||{};
  const keys=Object.keys(blocks);
  $('pwbadge').textContent=keys.length?keys.length+' units':'units';

  // alerts
  const al=d.alerts||[];
  $('alerts').innerHTML = al.length? al.map(a=>'<span class="chip">'+a+'</span>').join('') : '<span class="chip">none</span>';

  // unit cards
  const pw3=d.pw3||{};
  const tempFor=serial=>{for(const k in pw3){if(k.includes(serial)){const v=pw3[k];
      if(v.HVP_PackTempMax!=null)return v.HVP_PackTempMin+'–'+v.HVP_PackTempMax+' °C';}}return null;};
  $('units').innerHTML = keys.map(k=>{
    const b=blocks[k]; const soc=(b.nominal_energy_remaining&&b.nominal_full_pack_energy)?
      (b.nominal_energy_remaining/b.nominal_full_pack_energy*100).toFixed(1)+'%':'—';
    const temp=tempFor(b.PackageSerialNumber||k);
    const rows=[
      ['State', b.pinv_state||'—'],
      ['Charge', soc],
      ['Stored', Wh(b.nominal_energy_remaining)],
      ['Capacity', Wh(b.nominal_full_pack_energy)],
      ['Output', b.p_out!=null?W(b.p_out*1000):'—'],
      ['Voltage', b.v_out!=null?b.v_out.toFixed(1)+' V':'—'],
      ['Freq', b.f_out!=null?b.f_out.toFixed(2)+' Hz':'—'],
    ];
    if(temp)rows.push(['Pack temp',temp]);
    return '<div class="card"><div class="t">'+(b.PackageSerialNumber||k)+'</div>'+
      rows.map(r=>'<div class="kv"><span class="k">'+r[0]+'</span><span class="v">'+r[1]+'</span></div>').join('')+'</div>';
  }).join('');

  // wall connectors (read-only local API)
  const wc=d.wc||[];
  $('wcs').innerHTML = wc.length ? wc.map(c=>{
    const v=c.vitals||{}, ver=c.version||{}, lt=c.lifetime||{};
    const online=!!c.vitals, charging=!!v.contactor_closed, connected=!!v.vehicle_connected;
    const amps=v.vehicle_current_a||0, volts=v.grid_v||0;
    const pw=charging?Math.round(amps*volts):0;
    const state = !online?'offline':(charging?'⚡ Charging':(connected?'Connected (idle)':'Idle'));
    const rows=[
      ['Status', state],
      ['Power', charging?W(pw):'0 W'],
      ['Current', online&&v.vehicle_current_a!=null?amps.toFixed(1)+' A':'—'],
      ['Grid', v.grid_v!=null?v.grid_v.toFixed(1)+' V · '+(v.grid_hz||0).toFixed(1)+' Hz':'—'],
      ['This session', v.session_energy_wh!=null?Wh(v.session_energy_wh):'—'],
      ['Lifetime', lt.energy_wh!=null?(lt.energy_wh/1e6).toFixed(2)+' MWh':'—'],
      ['Charges', lt.charge_starts!=null?lt.charge_starts:'—'],
      ['Handle temp', v.handle_temp_c!=null?v.handle_temp_c.toFixed(1)+' °C':'—'],
      ['Firmware', ver.firmware_version?String(ver.firmware_version).split('+')[0]:'—'],
    ];
    const title=(ver.serial_number?('WC '+ver.serial_number):'Wall Connector');
    return '<div class="card"><div class="t">'+title+' · '+(c.ip||'')+'</div>'+
      rows.map(r=>'<div class="kv"><span class="k">'+r[0]+'</span><span class="v">'+r[1]+'</span></div>').join('')+'</div>';
  }).join('') : '<span class="chip">no Wall Connectors found</span>';

  $('din').textContent=d.din||'—';
  $('fw').textContent=d.firmware||'—';
  $('raw_vitals').textContent=JSON.stringify(d.vitals||{},null,2);
  $('raw_blocks').textContent=JSON.stringify({blocks:d.blocks,pw3:d.pw3},null,2);
  $('raw_all').textContent=JSON.stringify(d,null,2);
  window._lastTs=d.ts;
}
function tick(){
  if(window._lastTs){const s=Math.max(0,Math.round(Date.now()/1000-window._lastTs));
    $('age').textContent='updated '+s+'s ago';}
}
async function load(){try{const r=await fetch('data.json',{cache:'no-store'});render(await r.json());}catch(e){}}
document.querySelectorAll('#ranges .rbtn').forEach(b=>b.onclick=()=>{
  _range=+b.dataset.r;
  document.querySelectorAll('#ranges .rbtn').forEach(x=>x.classList.toggle('active',x===b));
  loadHistory(); loadStats();
});
let _v0='flow';try{_v0=localStorage.getItem('pwview')||'flow';}catch(e){}
setView(_v0);
load();setInterval(load,3000);setInterval(tick,1000);
</script>
</body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        if self.path.startswith("/set_password"):
            try:
                ln = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(ln).decode() or "{}") if ln else {}
                pwd = (data.get("pwd") or "").strip()
            except Exception:
                pwd = ""
            resp = {"ok": False}
            if not pwd:
                resp["error"] = "empty password"
            else:
                # verify against the gateway before persisting it
                try:
                    tt = TEDAPI(pwd)
                    try:
                        tt.timeout = TIMEOUT
                    except Exception:
                        pass
                    din = tt.get_din()
                    if din:
                        save_pwd(pwd)
                        resp = {"ok": True, "din": din}
                    else:
                        resp["error"] = "gateway did not respond (wrong password?)"
                except Exception as e:
                    resp["error"] = str(e)
            body = json.dumps(resp).encode()
            self.send_response(200 if resp.get("ok") else 400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/control"):
            try:
                ln = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(ln).decode() or "{}") if ln else {}
                action = data.get("action")
                value = data.get("value")
            except Exception:
                action, value = None, None
            resp = do_control(action, value)
            body = json.dumps(resp).encode()
            self.send_response(200 if resp.get("ok") else 400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path.startswith("/data.json"):
            with _lock:
                body = json.dumps(_data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/history.json"):
            q = parse_qs(urlparse(self.path).query)
            try:
                range_s = max(300, min(int(q.get("range", ["86400"])[0]), HISTORY_DAYS * 86400))
            except Exception:
                range_s = 86400
            body = json.dumps(history(range_s)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/stats.json"):
            q = parse_qs(urlparse(self.path).query)
            try:
                range_s = max(300, min(int(q.get("range", ["604800"])[0]), HISTORY_DAYS * 86400))
            except Exception:
                range_s = 604800
            body = json.dumps(stats(range_s)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/", "/index.html"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    db_init()
    threading.Thread(target=poller, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print(f"Powerwall local dashboard on http://0.0.0.0:{PORT}")
    srv.serve_forever()
