"""Polls the Gateway on a loop, logs to SQLite, and alerts on anomalies."""

import os
import sqlite3
import time

import alerts
import poller
import web

DB_PATH = os.environ.get("PW_DB_PATH", "readings.db")


def load_env(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS readings (
            ts REAL PRIMARY KEY,
            solar_w REAL,
            load_w REAL,
            battery_w REAL,
            grid_w REAL,
            battery_soe REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS anomalies (
            ts REAL PRIMARY KEY,
            message TEXT
        )
        """
    )
    conn.commit()
    return conn


def main() -> None:
    load_env()

    site_id = os.environ["PW_SITE_ID"]
    poll_interval = float(os.environ.get("POLL_INTERVAL", "30"))
    web_port = int(os.environ.get("WEB_PORT", "8080"))

    conn = init_db(DB_PATH)
    history: list[dict] = []

    web.start_server(web_port)
    print(
        f"Reading from Tesla's Owner API. Logging to {DB_PATH}. "
        f"Webpage at http://<pi-ip>:{web_port}/. Ctrl-C to stop."
    )

    try:
        while True:
            reading = poller.read(site_id)
            conn.execute(
                "INSERT OR REPLACE INTO readings VALUES (?, ?, ?, ?, ?, ?)",
                (
                    reading["ts"],
                    reading["solar_w"],
                    reading["load_w"],
                    reading["battery_w"],
                    reading["grid_w"],
                    reading["battery_soe"],
                ),
            )
            conn.commit()

            print(
                f"solar={reading['solar_w']:.0f}W  load={reading['load_w']:.0f}W  "
                f"battery={reading['battery_w']:.0f}W  grid={reading['grid_w']:.0f}W  "
                f"soe={reading['battery_soe']:.1f}%"
            )

            problem = alerts.check_for_anomaly(reading, history)
            if problem:
                print(f"ALERT: {problem}")
                conn.execute(
                    "INSERT OR REPLACE INTO anomalies VALUES (?, ?)",
                    (reading["ts"], problem),
                )
                conn.commit()

            history.append(reading)
            web.set_latest({**reading, "anomaly": problem})
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()


if __name__ == "__main__":
    main()
