#!/usr/bin/env python3
"""nzbget-dupe-proxy: impersonates nzbget toward NZBHydra2 and adds duplicate postings as donors.

Requests are forwarded verbatim to nzbget; JSON-RPC `append` gets a DupeKey, then a background worker finds other
postings of the same release via Hydra and appends them under that DupeKey with lower DupeScores, as donors for
nzbget's DupeArticleFallback. "Same release" is decided by PTT-parsed release names (title, episode, group,
resolution, source, codec, audio, HDR, ...), not by size. Python 3 stdlib + vendored PTT (pure Python)."""
import base64
import functools
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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
from ptt import parse_title  # noqa: E402  (vendored PTT, MIT)

log = logging.getLogger("nzbget-dupe-proxy")
GROUP_WINDOW = 600        # s: appends of the same release within this window share one DupeKey
STATE_TTL = 30 * 86400   # s: forget groups after this
Result = namedtuple("Result", "title link size grabs date indexer")  # one Hydra search hit
EXT_RE = re.compile(r"\.(part\d+\.rar|vol\d+\+\d+\.par2|7z\.\d{3}|r\d{2}|z\d{2}|\d{3}|nzb|mkv|mp4|m4v|avi|ts|"
                    r"rar|par2|7z|zip|nfo|sfv|srr|srt|sub|idx|jpg|png|txt)$", re.I)
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
    size_tolerance: float = 0.0  # > 0: skip Hydra results whose size differs more than this fraction
    state_dir: str = "/var/lib/nzbget-dupe-proxy"
    enabled: bool = True
    dry_run: bool = False
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


def clean_name(name):
    """Release name without extensions / volume suffixes, [tags] and indexer junk ('-xpost')."""
    name, prev = name.strip(), None
    while name != prev:
        prev, name = name, EXT_RE.sub("", name)
    return JUNK_RE.sub("", re.sub(r"\[[^\]]*\]|\{[^}]*\}", " ", name).strip()).strip(" ._-")


def normalize_title(name):
    """Lowercase clean name, separators -> single dots."""
    return re.sub(r"[\s._\-()+,]+", ".", clean_name(name).lower()).strip(".")


COMPAT = ("resolution", "quality", "codec", "bit_depth", "hdr", "audio", "channels", "network", "edition")


def _tokens(v):
    v = " ".join(map(str, v)) if isinstance(v, list) else str(v or "")
    return frozenset(re.findall(r"[a-z0-9]+", v.lower()))


@functools.lru_cache(maxsize=4096)
def release_attrs(name):
    """PTT attributes of a release/file name, normalized for comparison (lowercase token sets)."""
    try:
        a = parse_title(clean_name(name))
    except Exception:  # PTT on garbage input: treat as unreadable
        a = {}
    out = {k: _tokens(a.get(k)) for k in COMPAT}
    out.update(title=re.sub(r"[^a-z0-9]", "", str(a.get("title", "")).lower()), group=str(a.get("group") or "").lower(),
               seasons=tuple(a.get("seasons") or ()), episodes=tuple(a.get("episodes") or ()), year=a.get("year"),
               repack=bool(a.get("repack")), proper=bool(a.get("proper")))
    return out


def readable(name):
    """Does a (file) name carry release info, i.e. is it not obfuscated?"""
    a = release_attrs(name)
    return bool(a["title"] and (a["group"] or a["resolution"]))


def same_release(a_name, b_name):
    """Same title/episode/year, same group, same repack/proper, and no conflicting quality attributes."""
    a, b = release_attrs(a_name), release_attrs(b_name)
    if not a["group"]:  # nothing to anchor on: require the same normalized name
        return normalize_title(a_name) == normalize_title(b_name)
    if (a["title"], a["group"], a["seasons"], a["episodes"], a["repack"], a["proper"]) != \
            (b["title"], b["group"], b["seasons"], b["episodes"], b["repack"], b["proper"]):
        return False
    if a["year"] and b["year"] and a["year"] != b["year"]:
        return False
    return all(not a[k] or not b[k] or a[k] <= b[k] or b[k] <= a[k] for k in COMPAT)


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
    main_name: str = ""  # name of the largest file

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
    files, sizes, ids, poster, meta = 0, {}, set(), "", {}
    for el in safe_xml(data).iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "meta" and el.get("type"):
            meta[el.get("type").lower()] = (el.text or "").strip()
        elif tag == "file":
            files += 1
            poster = poster or el.get("poster", "")
            m = re.search(r'"([^"]+)"', el.get("subject", ""))
            name = (m.group(1) if m else el.get("subject", "")).strip()
            for seg in el.iter():
                if seg.tag.rsplit("}", 1)[-1] == "segment":
                    sizes[name] = sizes.get(name, 0) + int(seg.get("bytes") or 0)
                    ids.add((seg.text or "").strip())
    if not files or not ids:
        raise ValueError("NZB has no files/segments")
    data_files = {n: b for n, b in sizes.items() if not re.search(r"\.par2$|\.vol\d+[+-]\d+", n, re.I)} or sizes
    return NzbInfo(files, sum(sizes.values()), frozenset(n.lower() for n in sizes), poster, frozenset(ids), meta,
                   max(data_files, key=data_files.get))


def same_posting(a, b):
    """Any real article-ID overlap (> 1%) -> same posting: donors must point at different articles.
    (Indexers re-list one posting with a re-uploaded segment; distinct postings share 0%.)"""
    return len(a & b) > 0.01 * min(len(a), len(b))


def candidate_ok(primary_title, primary_bytes, r, tol):
    """Hydra result worth fetching: same release by name; size only matters if a tolerance is set."""
    if tol and primary_bytes and abs(r.size - primary_bytes) > tol * primary_bytes:
        return False
    return same_release(primary_title, r.title)


def mask(text):
    """Hide apikeys and user:password pairs in URLs/paths before logging."""
    text = re.sub(r"(?i)(apikey=)[^&\s]+", r"\1***", str(text))
    text = re.sub(r"//[^/@\s]+:[^/@\s]+@", "//***@", text)
    return re.sub(r"/[^/:\s]+:[^/\s]+/(json|xml)rpc", r"/***/\1rpc", text)


class State:
    """JSON file {key: {t, title, fps: {message-id fingerprint: nzbid}}}."""

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

    def group_for(self, title, now):
        """Key of a recent group whose primary is the same release as `title`."""
        for key, g in sorted(self.data.items(), key=lambda kv: -kv[1]["t"]):
            if now - g["t"] < GROUP_WINDOW and g.get("title") and same_release(g["title"], title):
                return key
        return None

    def sent(self, key, fp):
        return self.data.get(key, {}).get("fps", {}).get(fp)

    def record(self, key, fp, nzbid, title=None, touch=False):
        g = self.data.setdefault(key, {"t": time.time(), "title": title, "fps": {}})
        if touch:
            g.update(t=time.time(), title=title)
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
            key = params[6] or self.state.group_for(title, t0) or "dupes:" + normalize_title(title)
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
                self.state.record(key, info.fingerprint, nzbid, title, touch=not fresh)
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
        dist = lambda size: abs(size - info.total_bytes)  # noqa: E731  closest size = most likely byte-identical
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
                title, info.total_bytes, r, cfg.size_tolerance)), key=lambda r: (dist(r.size), -r.grabs, -r.date))
            # one posting of each distinct size first (same size is often the same posting), then the rest
            first_of_size = {r.size: r for r in reversed(cands)}
            order = sorted(cands, key=lambda r: first_of_size[r.size] is not r)[:3 * cfg.max_donors]
            verified, postings = [], [info.message_ids]
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
                    elif readable(ci.main_name) and not same_release(title, ci.main_name):
                        stats["other-release"] += 1
                    elif any(same_posting(ci.message_ids, ids) for ids in postings):
                        stats["same-posting"] += 1
                    else:
                        postings.append(ci.message_ids)
                        verified.append((r, data, ci))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        verified.sort(key=lambda v: (dist(v[2].total_bytes), -v[0].grabs, -v[0].date))
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
