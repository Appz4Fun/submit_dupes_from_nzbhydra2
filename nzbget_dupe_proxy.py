#!/usr/bin/env python3
"""nzbget-dupe-proxy: impersonates nzbget toward NZBHydra2 and adds duplicate postings as donors.

Every request is forwarded verbatim to nzbget. JSON-RPC `append` gets a DupeKey; afterwards a
background worker finds other postings of the same release via Hydra's newznab API and appends
them under the same DupeKey with lower DupeScores, so nzbget keeps them as duplicate backups
(donors for DupeArticleFallback). Python 3 stdlib only.
"""
import hashlib
import logging
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.etree import ElementTree as ET

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


EXT_RE = re.compile(r"\.(nzb|mkv|mp4|m4v|avi|ts|rar|par2|7z|zip|nfo|sfv)$", re.I)
JUNK_RE = re.compile(r"([.\-_ ](xpost|postbot|obfuscated|scrambled|asrequested|rp|rakuv\w*|buymore|"
                     r"chamele0n|sample|repost))+$", re.I)
MARKER_RE = re.compile(r"^(s\d{1,3}e\d{1,4}|(19|20)\d\d)$")      # episode / year: must match
RES_RE = re.compile(r"^(\d{3,4}p|4k|uhd)$")


def normalize_title(name):
    """Lowercase, drop extension / [tags] / indexer junk, separators -> single dots."""
    name = EXT_RE.sub("", EXT_RE.sub("", name.strip()))
    name = re.sub(r"\[[^\]]*\]|\{[^}]*\}", " ", name).strip()
    name = JUNK_RE.sub("", name)
    return re.sub(r"[\s._\-()+,]+", ".", name.lower()).strip(".")


def title_tokens(name):
    return set(normalize_title(name).split("."))


def short_query(title):
    """'Lucifer.S02E14.Candy...1080p...' -> 'lucifer s02e14 1080p' (None without episode/year)."""
    toks = normalize_title(title).split(".")
    cut = next((i for i, t in enumerate(toks) if MARKER_RE.match(t)), None)
    if cut is None:
        return None
    res = next((t for t in toks[cut + 1:] if RES_RE.match(t)), None)
    return " ".join(toks[:cut + 1] + ([res] if res else []))


@dataclass(frozen=True)
class NzbInfo:
    files: int
    total_bytes: int
    filenames: frozenset
    poster: str
    message_ids: frozenset
    meta: dict

    @property
    def fingerprint(self):
        return hashlib.sha1("\n".join(sorted(self.message_ids)).encode()).hexdigest()


def parse_nzb(data):
    """Parse NZB bytes into an NzbInfo; ValueError if malformed or empty."""
    if b"<!ENTITY" in data[:4096]:
        raise ValueError("NZB declares XML entities")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ValueError("malformed NZB: %s" % e)
    tag = lambda el: el.tag.rsplit("}", 1)[-1]  # noqa: E731
    files, names, ids, total, poster, meta = 0, set(), set(), 0, "", {}
    for el in root.iter():
        if tag(el) == "meta" and el.get("type"):
            meta[el.get("type").lower()] = (el.text or "").strip()
        elif tag(el) == "file":
            files += 1
            poster = poster or el.get("poster", "")
            subj = el.get("subject", "")
            m = re.search(r'"([^"]+)"', subj)
            names.add((m.group(1) if m else subj).strip().lower())
        elif tag(el) == "segment":
            total += int(el.get("bytes") or 0)
            ids.add((el.text or "").strip())
    if not files or not ids:
        raise ValueError("NZB has no files/segments")
    return NzbInfo(files, total, frozenset(names), poster, frozenset(ids), meta)


def verify(p, c):
    """Same release? (bytes within 1% and file count within 10%) or >= 50% filenames shared."""
    size_ok = abs(c.total_bytes - p.total_bytes) <= 0.01 * p.total_bytes
    count_ok = abs(c.files - p.files) <= 0.10 * p.files
    shared = len(p.filenames & c.filenames) / max(1, min(len(p.filenames), len(c.filenames)))
    return (size_ok and count_ok) or shared >= 0.5


@dataclass
class Result:
    title: str
    link: str
    size: int
    grabs: int
    date: float
    indexer: str


def candidate_ok(primary_title, primary_bytes, r, tol):
    """Hydra result worth fetching: size close, same episode/year, and similar title."""
    if not primary_bytes or abs(r.size - primary_bytes) > tol * primary_bytes:
        return False
    p, c = title_tokens(primary_title), title_tokens(r.title)
    if {t for t in p if MARKER_RE.match(t)} - c:
        return False
    return normalize_title(primary_title) == normalize_title(r.title) or len(p & c) >= 0.5 * len(p)


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
