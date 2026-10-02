#!/usr/bin/env python3
"""nzbget-dupe-proxy: impersonates nzbget toward NZBHydra2 and adds duplicate postings as donors.

Requests are forwarded verbatim to nzbget; JSON-RPC `append` gets a DupeKey, then a background worker finds other
postings of the same release via Hydra and appends them under that DupeKey with lower DupeScores, as donors for
nzbget's DupeArticleFallback. Python 3 stdlib only."""
import base64
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, namedtuple
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, fields
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.etree import ElementTree as ET
from xml.parsers import expat

log = logging.getLogger("nzbget-dupe-proxy")
GROUP_WINDOW = 600        # s: appends of the same release within this window share one DupeKey
STATE_TTL = 30 * 86400   # s: forget groups after this
Result = namedtuple("Result", "title link size grabs date indexer")  # one Hydra search hit
EXT_RE = re.compile(r"\.(nzb|mkv|mp4|m4v|avi|ts|rar|par2|7z|zip|nfo|sfv)$", re.I)
JUNK_RE = re.compile(r"([.\-_ ](xpost|postbot|obfuscated|scrambled|asrequested|rp|rakuv\w*|buymore|"
                     r"chamele0n|sample|repost))+$", re.I)
MARKER_RE = re.compile(r"^(s\d{1,3}e\d{1,4}|(19|20)\d\d)$")      # episode / year: must match
RES_RE = re.compile(r"^(\d{3,4}p|4k|uhd)$")


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
    verify_count: bool = True  # false: accept repackaged reposts (same bytes, different file count)
    deadline: float = 60.0  # seconds after the primary append
    timeout: float = 30.0   # per HTTP request to Hydra / indexers

    @classmethod
    def from_env(cls, env):
        """Each field from the env var of the same name in upper case (LISTEN_PORT, ...)."""
        c = cls()
        for f in fields(cls):
            v, conv = str(env.get(f.name.upper(), "")).strip(), type(getattr(c, f.name))
            if v:
                setattr(c, f.name, v.lower() in ("1", "true", "yes", "on") if conv is bool else conv(v))
        c.nzbget_url, c.hydra_url = c.nzbget_url.rstrip("/"), c.hydra_url.rstrip("/")
        return c


def normalize_title(name):
    """Lowercase, drop extension / [tags] / indexer junk, separators -> single dots."""
    name = EXT_RE.sub("", EXT_RE.sub("", name.strip()))
    name = JUNK_RE.sub("", re.sub(r"\[[^\]]*\]|\{[^}]*\}", " ", name).strip())
    return re.sub(r"[\s._\-()+,]+", ".", name.lower()).strip(".")


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


def safe_xml(data):
    """ElementTree.fromstring that refuses entity declarations (billion laughs / XXE); ValueError."""
    def no_entities(*_):
        raise ValueError("XML declares entities")
    tb, p = ET.TreeBuilder(), expat.ParserCreate(namespace_separator="}")
    p.StartElementHandler, p.EndElementHandler, p.CharacterDataHandler = tb.start, tb.end, tb.data
    p.EntityDeclHandler = no_entities
    try:
        p.Parse(data, True)
        return tb.close()
    except (expat.ExpatError, AssertionError) as e:
        raise ValueError("malformed XML: %s" % e)


def parse_nzb(data):
    """Parse NZB bytes into an NzbInfo; ValueError if malformed or empty."""
    files, names, ids, total, poster, meta = 0, set(), set(), 0, "", {}
    for el in safe_xml(data).iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "meta" and el.get("type"):
            meta[el.get("type").lower()] = (el.text or "").strip()
        elif tag == "file":
            files += 1
            poster = poster or el.get("poster", "")
            subj = el.get("subject", "")
            m = re.search(r'"([^"]+)"', subj)
            names.add((m.group(1) if m else subj).strip().lower())
        elif tag == "segment":
            total += int(el.get("bytes") or 0)
            ids.add((el.text or "").strip())
    if not files or not ids:
        raise ValueError("NZB has no files/segments")
    return NzbInfo(files, total, frozenset(names), poster, frozenset(ids), meta)


def verify(p, c, check_count=True):
    """Same release? (bytes within 1% and file count within 10%) or >= 50% filenames shared.
    check_count=False drops the file-count clause: reposts are often repackaged (rar <-> 7z, volume size)."""
    size_ok = abs(c.total_bytes - p.total_bytes) <= 0.01 * p.total_bytes
    count_ok = not check_count or abs(c.files - p.files) <= 0.10 * p.files
    shared = len(p.filenames & c.filenames) / max(1, min(len(p.filenames), len(c.filenames)))
    return (size_ok and count_ok) or shared >= 0.5


def candidate_ok(primary_title, primary_bytes, r, tol):
    """Hydra result worth fetching: size close, same episode/year, and similar title."""
    if not primary_bytes or abs(r.size - primary_bytes) > tol * primary_bytes:
        return False
    p, c = (set(normalize_title(t).split(".")) for t in (primary_title, r.title))
    return not {t for t in p if MARKER_RE.match(t)} - c and len(p & c) >= 0.5 * len(p)


def mask(text):
    """Hide apikeys and user:password pairs in URLs/paths before logging."""
    text = re.sub(r"(?i)(apikey=)[^&\s]+", r"\1***", str(text))
    text = re.sub(r"//[^/@\s]+:[^/@\s]+@", "//***@", text)
    return re.sub(r"/[^/:\s]+:[^/\s]+/(json|xml)rpc", r"/***/\1rpc", text)


class State:
    """JSON file {key: {t, files, bytes, names, fps: {message-id fingerprint: nzbid}}}."""

    def __init__(self, state_dir):
        self.path = os.path.join(state_dir, "state.json")
        self.lock = threading.RLock()
        try:
            with open(self.path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def save(self):
        now = time.time()
        self.data = {k: v for k, v in self.data.items() if now - v["t"] < STATE_TTL}
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path + ".tmp", "w") as f:
                json.dump(self.data, f)
            os.replace(self.path + ".tmp", self.path)
        except OSError as e:
            log.error("cannot write state %s: %s", self.path, e)

    def group_for(self, info, now, check_count=True):
        """Key of a recent group whose primary looks like the same release as `info`."""
        for key, g in sorted(self.data.items(), key=lambda kv: -kv[1]["t"]):
            if now - g["t"] < GROUP_WINDOW and g["bytes"] and verify(
                    NzbInfo(g["files"], g["bytes"], frozenset(g["names"]), "", frozenset(), {}), info, check_count):
                return key
        return None

    def sent(self, key, fp):
        return self.data.get(key, {}).get("fps", {}).get(fp)

    def record(self, key, fp, nzbid, info=None, touch=False):
        g = self.data.setdefault(key, {"t": time.time(), "files": 0, "bytes": 0, "names": [], "fps": {}})
        if touch:
            g["t"] = time.time()
        if info and (touch or not g["bytes"]):
            g.update(files=info.files, bytes=info.total_bytes, names=sorted(info.filenames))
        g["fps"][fp] = nzbid
        self.save()


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def rpc_result(status, body):
    """Integer `result` of an nzbget JSON-RPC reply (NZBID for append), 0 on any error."""
    try:
        return _int(json.loads(body).get("result")) if status == 200 else 0
    except (ValueError, AttributeError):
        return 0


class Handler(BaseHTTPRequestHandler):
    proxy = None  # set by Proxy.start
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug(mask(fmt % args))

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        intercept = self.proxy.cfg.enabled and self.command == "POST" and self.path.endswith("/jsonrpc")
        reply = self.proxy.handle_append(self.path, body, self.headers) if intercept else None
        status, body, ctype = reply or self.proxy.forward(self.path, body, self.headers, self.command)
        self.send_response(status)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST


class Proxy:
    def __init__(self, cfg):
        self.cfg, self.state = cfg, State(cfg.state_dir)
        self.workers, self.server = [], None

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

    def handle_append(self, path, body, headers):
        """Rewrite + forward a positional JSON-RPC append; None means 'not ours, pass through'."""
        try:
            req = json.loads(body)
            params = req["params"]
            if req.get("method") != "append" or not isinstance(params, list) or len(params) != 10:
                return None
        except (ValueError, TypeError, KeyError, AttributeError):
            return None
        t0, title, info = time.time(), re.sub(r"(?i)\.nzb$", "", str(params[0])), None
        try:
            info = parse_nzb(base64.b64decode(params[1], validate=True))
        except (ValueError, TypeError) as e:
            log.info("append %s: content is not an NZB (%s); no donor discovery", title, e)
        with self.state.lock:
            key = params[6] or (info and self.state.group_for(info, t0, self.cfg.verify_count)) or "dupes:" + normalize_title(title)
            g = self.state.data.get(key)
            fresh = bool(g) and t0 - g["t"] < GROUP_WINDOW
            old = info and fresh and self.state.sent(key, info.fingerprint)
            if old:
                log.info("append key=%s nzbid=%s title=%s: posting already sent, not re-adding", key, old, title)
                reply = {"version": "1.1", "id": req.get("id"), "result": old}
                return 200, json.dumps(reply).encode(), "application/json"
            params[6:9] = [key, 100, "SCORE"]
            status, rbody, ctype = self.forward(path, json.dumps(req).encode(), headers)
            nzbid = rpc_result(status, rbody)
            if info and nzbid > 0:
                self.state.record(key, info.fingerprint, nzbid, info, touch=not fresh)
        if nzbid <= 0 or not info or fresh or not self.cfg.hydra_url:
            why = "joined existing group" if fresh else "no discovery"
            log.info("append key=%s nzbid=%s title=%s: %s", key, nzbid, title, why)
        else:
            t = threading.Thread(target=self._discover_safe, daemon=True,
                                 args=(key, title, info, params[2], path, headers.get("Authorization"), nzbid, t0))
            self.workers = [w for w in self.workers if w.is_alive()] + [t]
            t.start()
        return status, rbody, ctype

    def _discover_safe(self, *args):
        try:
            self.discover(*args)
        except Exception:  # never let a donor problem escape the worker
            log.exception("donor discovery crashed for key=%s", args[0])

    def hydra_search(self, params):
        query = urllib.parse.urlencode(dict(params, limit=100, apikey=self.cfg.hydra_apikey))
        with urllib.request.urlopen(self.cfg.hydra_url + "/api?" + query, timeout=self.cfg.timeout) as r:
            root = safe_xml(r.read())
        if root.tag == "error":
            raise OSError("hydra error %s: %s" % (root.get("code"), root.get("description")))
        out = []
        for it in root.iter("item"):
            a = {x.get("name"): x.get("value") for x in it if x.tag.endswith("}attr")}
            try:
                date = parsedate_to_datetime(a["usenetdate"]).timestamp()
            except (KeyError, TypeError, ValueError):
                date = 0.0
            out.append(Result((it.findtext("title") or "").strip(), (it.findtext("link") or "").strip(),
                              _int(a.get("size") or it.findtext("size")), _int(a.get("grabs")), date,
                              a.get("hydraIndexerName", "")))
        return out

    def queries(self, title, info):
        norm, short = normalize_title(title), short_query(title)
        qs = [{"t": "search", "q": norm.replace(".", " ")}]
        if short and short != qs[0]["q"]:
            qs.append({"t": "search", "q": short})
        imdb, tvdb = (re.sub(r"\D", "", info.meta.get(k, "")) for k in ("imdb", "tvdb"))
        if imdb:
            qs.append({"t": "movie", "imdbid": imdb})
        m = re.search(r"(?:^|\.)s(\d+)e(\d+)(?:\.|$)", norm)
        if tvdb and m:
            qs.append({"t": "tvsearch", "tvdbid": tvdb, "season": int(m.group(1)), "ep": int(m.group(2))})
        return qs

    def fetch(self, r, deadline):
        """-> (reason, nzb bytes, NzbInfo); reason 'ok', 'fetch' or 'parse'."""
        timeout = max(1.0, min(self.cfg.timeout, deadline - time.time()))
        try:
            with urllib.request.urlopen(r.link, timeout=timeout) as resp:
                data = resp.read()
        except OSError as e:
            log.info("donor fetch failed %s (%s): %s", r.title, r.indexer, mask(e))
            return "fetch", None, None
        try:
            return "ok", data, parse_nzb(data)
        except ValueError:
            return "parse", None, None

    def discover(self, key, title, info, category, path, auth, nzbid, t0):
        cfg, stats, deadline, results = self.cfg, Counter(), t0 + self.cfg.deadline, {}
        pool = ThreadPoolExecutor(4)
        try:
            for f in [pool.submit(self.hydra_search, q) for q in self.queries(title, info)]:
                try:
                    for r in f.result(timeout=max(0.0, deadline - time.time())):
                        results.setdefault(r.link, r)
                except FutureTimeout:
                    stats["deadline"] += 1
                except Exception as e:
                    log.warning("hydra search failed for %s: %s", title, mask(e))
            cands = sorted((r for r in results.values() if r.link and candidate_ok(
                title, info.total_bytes, r, cfg.size_tolerance)), key=lambda r: (-r.grabs, -r.date))
            # one posting of each distinct size first (same size is often the same posting), then the rest
            first_of_size = {r.size: r for r in reversed(cands)}
            order = sorted(cands, key=lambda r: first_of_size[r.size] is not r)[:3 * cfg.max_donors]
            verified, fps = [], {info.fingerprint}
            for i in range(0, len(order), 4):
                if len(verified) >= cfg.max_donors:
                    break
                if time.time() > deadline:
                    stats["deadline"] += len(order) - i
                    break
                chunk = order[i:i + 4]
                for r, (reason, data, ci) in zip(chunk, pool.map(self.fetch, chunk, [deadline] * len(chunk))):
                    if reason != "ok":
                        stats[reason] += 1
                    elif not verify(info, ci, cfg.verify_count):
                        stats["mismatch"] += 1
                    elif ci.fingerprint in fps:
                        stats["same-posting"] += 1
                    else:
                        fps.add(ci.fingerprint)
                        verified.append((r, data, ci))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        verified.sort(key=lambda v: (-v[0].grabs, -v[0].date))
        if len(verified) > cfg.max_donors:
            stats["over-cap"] = len(verified) - cfg.max_donors
        added = 0
        for score, (r, data, ci) in zip(range(90, 0, -1), verified[:cfg.max_donors]):
            name = r.title if r.title.lower().endswith(".nzb") else r.title + ".nzb"
            desc = "score=%d %s [%s, %d files, %d bytes, grabs=%d]" % (
                score, r.title, r.indexer, ci.files, ci.total_bytes, r.grabs)
            if cfg.dry_run:
                log.info("DRY-RUN would add donor %s", desc)
                continue
            with self.state.lock:
                if self.state.sent(key, ci.fingerprint):
                    stats["already-sent"] += 1
                    continue
                params = [name, base64.b64encode(data).decode(), category, 0, False, False, key, score, "SCORE", []]
                donor_id = self.rpc(path, auth, "append", params)
                if donor_id > 0:
                    self.state.record(key, ci.fingerprint, donor_id)
                    added += 1
                    log.info("added donor nzbid=%d %s", donor_id, desc)
                else:
                    stats["append"] += 1
        log.info("append key=%s nzbid=%s title=%s results=%d candidates=%d verified=%d added=%d%s rejected=%s "
                 "time=%.1fs", key, nzbid, title, len(results), len(cands), len(verified), added,
                 " dry_run would_add=%d" % min(len(verified), cfg.max_donors) if cfg.dry_run else "",
                 dict(stats), time.time() - t0)

    def rpc(self, path, auth, method, params):
        """JSON-RPC call to nzbget with Hydra's own path + credentials; result or 0 on error."""
        body = json.dumps({"jsonrpc": "2.0", "id": "dupe-proxy", "method": method, "params": params}).encode()
        return rpc_result(*self.forward(path, body, {"Authorization": auth, "Content-Type": "application/json"})[:2])

    def start(self, host="0.0.0.0"):
        handler = type("BoundHandler", (Handler,), {"proxy": self})
        self.server = ThreadingHTTPServer((host, self.cfg.listen_port), handler)  # daemon_threads by default
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
    cfg.dry_run = cfg.dry_run or "--dry-run" in sys.argv
    port = Proxy(cfg).start()
    log.info("listening on :%d -> %s (enabled=%s dry_run=%s hydra=%s max_donors=%d)", port,
             mask(cfg.nzbget_url), cfg.enabled, cfg.dry_run, mask(cfg.hydra_url), cfg.max_donors)
    threading.Event().wait()


if __name__ == "__main__":
    main()
