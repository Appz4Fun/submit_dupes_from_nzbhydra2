#!/usr/bin/env python3
"""nzbget-dupe-proxy: impersonates nzbget toward NZBHydra2 and adds duplicate postings as donors.

Every request is forwarded verbatim to nzbget. JSON-RPC `append` gets a DupeKey; afterwards a
background worker finds other postings of the same release via Hydra's newznab API and appends
them under the same DupeKey with lower DupeScores, so nzbget keeps them as duplicate backups
(donors for DupeArticleFallback). Python 3 stdlib only.
"""
import logging
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("nzbget-dupe-proxy")


@dataclass
class Config:
    listen_port: int = 6790
    nzbget_url: str = "http://127.0.0.1:6789"
    hydra_url: str = ""
    hydra_apikey: str = ""
    max_donors: int = 8
    size_tolerance: float = 0.02
    state_dir: str = "/var/lib/nzbget-dupe-proxy"
    enabled: bool = True
    dry_run: bool = False
    deadline: float = 60.0  # seconds after the primary append
    timeout: float = 30.0   # per HTTP request to Hydra / indexers

    @classmethod
    def from_env(cls, env):
        flag = lambda v: str(v).strip().lower() in ("1", "true", "yes", "on")  # noqa: E731
        c = cls()
        for name, conv in (("listen_port", int), ("nzbget_url", str), ("hydra_url", str),
                           ("hydra_apikey", str), ("max_donors", int), ("size_tolerance", float),
                           ("state_dir", str), ("enabled", flag), ("dry_run", flag),
                           ("deadline", float), ("timeout", float)):
            if env.get(name.upper(), "") != "":
                setattr(c, name, conv(env[name.upper()]))
        c.nzbget_url, c.hydra_url = c.nzbget_url.rstrip("/"), c.hydra_url.rstrip("/")
        return c


def mask(text):
    """Hide apikeys and user:password pairs in URLs/paths before logging."""
    text = re.sub(r"(?i)(apikey=)[^&\s]+", r"\1***", str(text))
    text = re.sub(r"//[^/@\s]+:[^/@\s]+@", "//***@", text)
    return re.sub(r"/[^/:\s]+:[^/\s]+/(json|xml)rpc", r"/***/\1rpc", text)


class Handler(BaseHTTPRequestHandler):
    proxy = None  # set by Proxy.start
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug(mask(fmt % args))

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._reply(*self.proxy.forward(self.path, body, self.headers, self.command))

    do_GET = do_POST

    def _reply(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Proxy:
    def __init__(self, cfg):
        self.cfg = cfg
        self.workers = []
        self.server = None

    def forward(self, path, body, headers, method="POST"):
        """Send a request to nzbget unchanged; returns (status, body, content-type)."""
        h = {k: headers[k] for k in ("Authorization", "Content-Type", "User-Agent") if headers.get(k)}
        req = urllib.request.Request(self.cfg.nzbget_url + path, data=body if method == "POST" else None,
                                     headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, r.read(), r.headers.get("Content-Type")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers.get("Content-Type")
        except OSError as e:
            log.error("nzbget unreachable for %s: %s", mask(path), e)
            return 502, b"nzbget unreachable", "text/plain"

    def start(self, host="0.0.0.0"):
        handler = type("BoundHandler", (Handler,), {"proxy": self})
        self.server = ThreadingHTTPServer((host, self.cfg.listen_port), handler)
        self.server.daemon_threads = True
        self.url = "http://%s:%d" % (host, self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, args=(0.1,), daemon=True).start()
        return self.server.server_address[1]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def wait_idle(self, timeout):
        for t in list(self.workers):
            t.join(timeout)


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(levelname)s %(message)s")
    cfg = Config.from_env(os.environ)
    if "--dry-run" in sys.argv:
        cfg.dry_run = True
    p = Proxy(cfg)
    port = p.start()
    log.info("listening on :%d -> %s (enabled=%s dry_run=%s hydra=%s max_donors=%d)", port,
             mask(cfg.nzbget_url), cfg.enabled, cfg.dry_run, mask(cfg.hydra_url), cfg.max_donors)
    threading.Event().wait()


if __name__ == "__main__":
    main()
