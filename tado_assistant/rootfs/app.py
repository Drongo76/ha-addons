from http.server import BaseHTTPRequestHandler, HTTPServer
import os

PORT = int(os.getenv("PORT", "8099"))

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"NO-FLASK OK")

print("NO-FLASK APP STARTED")
HTTPServer(("0.0.0.0", PORT), H).serve_forever()
