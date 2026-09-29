"""Tiny localhost web UI: run the agent live against any local git repo."""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

from .backends import available
from .cli import build

PAGE = open(os.path.join(os.path.dirname(__file__), "ui.html")).read()


def generate(d):
    repo = os.path.expanduser(d.get("repo") or ".")
    md, used, n, rev_from, draft = build(
        repo, d.get("from") or None, d.get("to") or "HEAD", d.get("version") or "Unreleased",
        d.get("backend") or "auto", d.get("repo_url") or "",
        d.get("audience") or "end users", d.get("tone") or "clear, confident, friendly",
        d.get("format") or "markdown")
    return {"markdown": md, "draft": draft or md, "backend": used, "commits": n, "from": rev_from}


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body.encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/api/backends":
            return self._send(200, json.dumps(available()))
        self._send(200, PAGE, "text/html; charset=utf-8")

    def do_POST(self):
        try:
            d = json.loads(self.rfile.read(int(self.headers["content-length"])))
            self._send(200, json.dumps(generate(d)))
        except Exception as e:  # noqa: BLE001
            self._send(400, json.dumps({"error": str(e)}))

    def log_message(self, *a):
        pass


def main():
    port = int(os.environ.get("PORT", "8765"))
    print(f"changelog-agent UI on http://127.0.0.1:{port}", flush=True)
    HTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    main()
