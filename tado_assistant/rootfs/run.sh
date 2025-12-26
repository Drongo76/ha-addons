import os
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.getenv("PORT", "8099"))
STATIC_DIR = Path("/static")
DATA_DIR = Path("/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Beispiel: hier kannst du später Tokens speichern/lesen:
AUTH_FILE = DATA_DIR / "tado_auth.json"

def _content_type(path: str) -> str:
    if path.endswith(".js"):
        return "application/javascript; charset=utf-8"
    if path.endswith(".css"):
        return "text/css; charset=utf-8"
    if path.endswith(".png"):
        return "image/png"
    if path.endswith(".svg"):
        return "image/svg+xml"
    return "text/html; charset=utf-8"

class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, obj):
        data = json.dumps(obj).encode("utf-8")
        self._send(code, data, "application/json; charset=utf-8")

    def do_GET(self):
        # UI
        if self.path == "/" or self.path.startswith("/static/"):
            req_path = "index.html" if self.path == "/" else self.path[len("/static/"):]
            fs_path = STATIC_DIR / req_path

            try:
                data = fs_path.read_bytes()
                return self._send(200, data, _content_type(fs_path.name))
            except Exception:
                return self._send(404, b"not found")

        # Health
        if self.path == "/api/health":
            return self._send_json(200, {"ok": True})

        return self._send(404, b"not found")

    def do_POST(self):
        # Auth Start (Device Code Flow Trigger)
        if self.path == "/auth/start" or self.path == "/api/auth/start":
            # Hier rufst du deinen bestehenden Tado Device-Code-Flow Code auf
            # und speicherst Ergebnis nach /data (z.B. AUTH_FILE).
            # Für jetzt: nur Antwort, damit UI nicht crasht.
            return self._send_json(200, {"status": "started"})

        return self._send(404, b"not found")

def main():
    print(f"[server] listening on 0.0.0.0:{PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

if __name__ == "__main__":
    main()
