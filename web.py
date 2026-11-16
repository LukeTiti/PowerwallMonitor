"""Minimal local webpage showing the latest reading. Stdlib only — no
Flask needed for something this small, and one less thing to install on
the Pi."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_lock = threading.Lock()
_latest: dict = {}

PAGE = """<!doctype html>
<title>Powerwall</title>
<style>
  body { font-family: system-ui, sans-serif; background: #111; color: #eee;
         display: flex; flex-direction: column; align-items: center;
         padding-top: 3rem; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem;
          margin-top: 2rem; }
  .card { background: #1c1c1c; border-radius: 12px; padding: 1.5rem 2rem;
          text-align: center; min-width: 140px; }
  .label { color: #999; font-size: 0.85rem; text-transform: uppercase; }
  .value { font-size: 2rem; font-weight: 600; margin-top: 0.25rem; }
  #updated { color: #666; font-size: 0.8rem; margin-top: 2rem; }
  #anomaly { display: none; background: #4a1c1c; color: #ffb3b3; padding: 0.75rem 1.5rem;
             border-radius: 8px; margin-top: 1.5rem; max-width: 24rem; text-align: center; }
</style>
<h1>Powerwall</h1>
<div id="anomaly"></div>
<div class="grid">
  <div class="card"><div class="label">Solar</div><div class="value" id="solar">--</div></div>
  <div class="card"><div class="label">Home</div><div class="value" id="load">--</div></div>
  <div class="card"><div class="label">Battery</div><div class="value" id="battery">--</div></div>
  <div class="card"><div class="label">Grid</div><div class="value" id="grid">--</div></div>
</div>
<div id="soe"></div>
<div id="updated"></div>
<script>
async function refresh() {
  const r = await fetch('/api/latest');
  if (!r.ok) return;
  const d = await r.json();
  if (!d.ts) return;
  document.getElementById('solar').textContent = Math.round(d.solar_w) + ' W';
  document.getElementById('load').textContent = Math.round(d.load_w) + ' W';
  document.getElementById('battery').textContent = Math.round(d.battery_w) + ' W';
  document.getElementById('grid').textContent = Math.round(d.grid_w) + ' W';
  document.getElementById('soe').textContent = 'Battery: ' + d.battery_soe.toFixed(1) + '%';
  document.getElementById('updated').textContent =
    'Updated ' + new Date(d.ts * 1000).toLocaleTimeString();
  const anomalyEl = document.getElementById('anomaly');
  if (d.anomaly) {
    anomalyEl.textContent = 'Anomaly: ' + d.anomaly;
    anomalyEl.style.display = 'block';
  } else {
    anomalyEl.style.display = 'none';
  }
}
refresh();
setInterval(refresh, 5000);
</script>
"""


def set_latest(reading: dict) -> None:
    with _lock:
        _latest.clear()
        _latest.update(reading)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # keep stdout to the poll loop's own status lines

    def do_GET(self):
        if self.path == "/api/latest":
            with _lock:
                body = json.dumps(_latest).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def start_server(port: int) -> None:
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
