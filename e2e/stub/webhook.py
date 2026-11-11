"""Test-only webhook receiver: appends every POSTed JSON body to /out/alerts.jsonl."""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


class Hook(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with Path("/out/alerts.jsonl").open("a") as f:
            f.write(json.dumps(json.loads(body or b"{}")) + "\n")
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


HTTPServer(("0.0.0.0", 8099), Hook).serve_forever()
