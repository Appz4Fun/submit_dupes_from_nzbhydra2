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
import donor_health  # noqa: E402  (sampled STAT on all news servers, vendored cyclops)

log = logging.getLogger("nzbget-dupe-proxy")
GROUP_WINDOW = 600        # s: appends of the same release within this window share one DupeKey
STATE_TTL = 30 * 86400   # s: forget groups after this
Result = namedtuple("Result", "title link size grabs date indexer")  # one Hydra search hit
EXT_RE = re.compile(r"(\.(part\d+\.rar|vol\d+\+\d+\.par2|7z\.\d{3}|r\d{2}|z\d{2}|nzb|mkv|mp4|m4v|avi|ts|"
                    r"rar|par2|7z|zip|nfo|sfv|srr|srt|sub|idx|jpg|png|txt)|(?<![hx])\.\d{3})$", re.I)  # not H.264
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
    max_donors: int = 0  # <= 0 (0 or -1): unlimited
    size_tolerance: float = 0.0  # > 0: skip Hydra results whose size differs more than this fraction
    state_dir: str = "/var/lib/nzbget-dupe-proxy"
    enabled: bool = True
    dry_run: bool = False
    health_percent: float = 2.0   # STAT this % of each NZB's articles on all news servers (0 = off)
    donor_min_alive: float = 0.5  # drop donors whose sampled articles are alive on no server below this share
    health_connections: int = 8   # per news server (and <= half its nzbget Connections)
    health_budget: float = 120.0  # s per health pass (probe of all donors / full sample of up to 4 donors)
    fast_donors: int = 5          # donors appended right after the quick probe; the rest after a full sample
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

    @property
    def donor_cap(self):
        """MAX_DONORS as a number, or infinity when MAX_DONORS <= 0."""
        return self.max_donors if self.max_donors > 0 else float("inf")


def clean_name(name):
    """Release name without extensions / volume suffixes, [tags] and indexer junk ('-xpost')."""
    name, prev = name.strip(), None
    while name != prev:
        prev, name = name, EXT_RE.sub("", name)
    return JUNK_RE.sub("", re.sub(r"\[[^\]]*\]|\{[^}]*\}", " ", name).strip()).strip(" ._-")


def normalize_title(name):
    """Lowercase clean name, separators -> single dots."""
    return re.sub(r"[\s._\-()+,]+", ".", clean_name(name).lower()).strip(".")


COMPAT = ("resolution", "quality", "codec", "bit_depth", "audio", "channels", "network", "edition")
EXACT = ("hdr",)  # a missing HDR tag means SDR, so HDR formats must match exactly


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
    out = {k: _tokens(a.get(k)) for k in COMPAT + EXACT}
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
    strict = ("title", "group", "seasons", "episodes", "repack", "proper") + EXACT
    if any(a[k] != b[k] for k in strict):
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


class Ranks:
    """Unique donor DupeScores from health: 10 + 80 * alive share (unknown = 1.0), always below the primary's
    100; equal health counts down (90, 89, ...). nzbget tries donors and backups by highest DupeScore."""

    def __init__(self):
        self.used = set()

    def take(self, alive):
        score = 10 + round(80 * (1.0 if alive is None else alive))
        while score in self.used:
            score -= 1
        self.used.add(score)
        return score

    def release(self, score):
        self.used.discard(score)


def alive_of(health, v):
    h = health.get(v[2].fingerprint)
    return h.alive if h else None


def pct(share):
    return "?" if share is None else "%d%%" % round(100 * share)


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
        self.workers, self.server, self.workers_lock = [], None, threading.Lock()

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
            with self.workers_lock:
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
            order = sorted(cands, key=lambda r: first_of_size[r.size] is not r)
            if cfg.max_donors > 0:  # each fetch costs an indexer grab
                order = order[:3 * cfg.max_donors]
            verified, postings = [], [info.message_ids]
            for i in range(0, len(order), 4):  # fetch the whole (capped) order: health may drop some later
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
        servers = self.news_servers(path, auth, title) if cfg.health_percent and verified else []
        probe = self.health(servers, title, {ci.fingerprint: ci.message_ids for _, _, ci in verified}, full=False)
        live = [v for v in verified if not self.dead(v, probe, stats, "probe")]
        n_fast = int(min(cfg.fast_donors, cfg.donor_cap))
        ranks, added, fast_ids = Ranks(), 0, {}
        for v in live[:n_fast]:  # quick: probe-alive donors go to nzbget right away
            score = ranks.take(alive_of(probe, v))
            donor_id = self.add_donor(key, category, path, auth, v, score, probe, "fast", stats)
            added += donor_id != 0
            if donor_id > 0:
                fast_ids[v[2].fingerprint] = (donor_id, score)
        # then full samples, 4 donors per connection pool: refine the fast donors' scores, add the rest
        todo = [(v, True) for v in live[:n_fast] if servers] + [(v, False) for v in live[n_fast:]]
        for i in range(0, len(todo), 4):
            chunk = todo[i:i + 4]
            groups = {v[2].fingerprint: v[2].message_ids for v, _ in chunk}
            if i == 0:
                groups["primary"] = info.message_ids
            h = self.health(servers, title, groups, full=True)
            if i == 0 and h:
                log.info("health %s: primary alive=%s on %d server(s)", title, pct(h["primary"].alive), len(servers))
            for v, was_fast in chunk:
                if was_fast:
                    if v[2].fingerprint in fast_ids:
                        self.rescore(path, auth, v, fast_ids[v[2].fingerprint], h, ranks, stats)
                elif added >= cfg.donor_cap:
                    stats["over-cap"] += 1
                elif not self.dead(v, h, stats, "full sample"):
                    score = ranks.take(alive_of(h, v))
                    added += self.add_donor(key, category, path, auth, v, score, h, "checked", stats) != 0
        log.info("append key=%s nzbid=%s title=%s results=%d candidates=%d verified=%d added=%d%s rejected=%s "
                 "time=%.1fs", key, nzbid, title, len(results), len(cands), len(verified),
                 0 if cfg.dry_run else added, " dry_run would_add=%d" % added if cfg.dry_run else "",
                 dict(stats), time.time() - t0)

    def news_servers(self, path, auth, title):
        try:
            servers = donor_health.servers_from_nzbget_config(self.rpc_call(path, auth, "config", []) or [],
                                                              self.cfg.health_connections, self.cfg.timeout)
        except (ValueError, TypeError, KeyError) as e:
            log.warning("health check skipped for %s: unreadable news server config (%s)", title, type(e).__name__)
            return []
        if not servers:
            log.info("health check skipped for %s: no active news servers in nzbget config", title)
        return servers

    def health(self, servers, title, groups, full):
        """{key: Health} from sampled STATs on all news servers; {} when unchecked (health is advisory)."""
        if not servers:
            return {}
        try:
            return donor_health.check_many(servers, groups, self.cfg.health_percent, budget=self.cfg.health_budget,
                                           full=full)
        except Exception as e:
            log.warning("health check failed for %s: %s", title, type(e).__name__)
            return {}

    def dead(self, v, health, stats, phase):
        """Probe: dead only if nothing was found (10 articles are too few to judge a share). Full sample:
        dead below DONOR_MIN_ALIVE."""
        h = health.get(v[2].fingerprint)
        if h is None or h.alive is None:
            return False
        if (h.present > 0) if phase == "probe" else (h.alive >= self.cfg.donor_min_alive):
            return False
        stats["dead"] += 1
        log.info("dropping dead donor %s [%s] after %s: alive=%s (%d of %d answered articles on no server, "
                 "%d errors)", v[0].title, v[0].indexer, phase, pct(h.alive), h.answered - h.present, h.answered,
                 h.error)
        return True

    def rescore(self, path, auth, v, added, health, ranks, stats):
        """After its full sample, move a fast donor's DupeScore (and DupeAlive) to its real health."""
        donor_id, old = added
        h = health.get(v[2].fingerprint)
        if h is None or h.alive is None:
            return
        dead = h.alive < self.cfg.donor_min_alive
        ranks.release(old)
        new = 1 if dead else ranks.take(h.alive)
        alive = "DupeAlive=%s" % pct(h.alive)
        for kind in ("History", "Group"):  # donors normally sit in history as dupe backups
            if self.rpc_call(path, auth, "editqueue", [kind + "SetDupeScore", str(new), [donor_id]]):
                self.rpc_call(path, auth, "editqueue", [kind + "SetParameter", alive, [donor_id]])
                break
        else:
            ranks.release(new)
            ranks.used.add(old)
            stats["rescore"] += 1
            log.warning("could not rescore donor nzbid=%d %s [%s]: keeps score %d, alive=%s", donor_id, v[0].title,
                        v[0].indexer, old, pct(h.alive))
            return
        if dead:
            stats["dead"] += 1
        log.info("rescored donor nzbid=%d %s [%s]: score %d -> %d, alive=%s (full sample%s)", donor_id, v[0].title,
                 v[0].indexer, old, new, pct(h.alive), ", dead" if dead else "")

    def add_donor(self, key, category, path, auth, v, score, health, how, stats):
        """Append one donor (or log it in dry run); returns its NZBID, -1 in dry run, 0 if not added."""
        r, data, ci = v
        h = health.get(ci.fingerprint)
        desc = "score=%d %s [%s, %d files, %d bytes, grabs=%d, alive=%s] (%s)" % (
            score, r.title, r.indexer, ci.files, ci.total_bytes, r.grabs, pct(h.alive if h else None), how)
        if self.cfg.dry_run:
            log.info("DRY-RUN would add donor %s", desc)
            return -1
        with self.state.lock:
            if self.state.sent(key, ci.fingerprint):
                stats["already-sent"] += 1
                return 0
            name = r.title if r.title.lower().endswith(".nzb") else r.title + ".nzb"
            pp = [{"Name": "DupeAlive", "Value": pct(h.alive)}] if h and h.alive is not None else []
            params = [name, base64.b64encode(data).decode(), category, 0, False, False, key, score, "SCORE", pp]
            donor_id = self.rpc(path, auth, "append", params)
            if donor_id <= 0:
                stats["append"] += 1
                return 0
            self.state.record(key, ci.fingerprint, donor_id)
        log.info("added donor nzbid=%d %s", donor_id, desc)
        return donor_id

    def rpc_call(self, path, auth, method, params):
        """JSON-RPC call to nzbget with Hydra's own path + credentials; raw result or None."""
        body = json.dumps({"jsonrpc": "2.0", "id": "dupe-proxy", "method": method, "params": params}).encode()
        status, rbody, _ = self.forward(path, body, {"Authorization": auth, "Content-Type": "application/json"})
        try:
            return json.loads(rbody).get("result") if status == 200 else None
        except (ValueError, AttributeError):
            return None

    def rpc(self, path, auth, method, params):
        """Integer result of an nzbget JSON-RPC call (NZBID for append), 0 on error."""
        return _int(self.rpc_call(path, auth, method, params))

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
    log.info("listening on :%d -> %s (enabled=%s dry_run=%s hydra=%s max_donors=%s)", port,
             mask(cfg.nzbget_url), cfg.enabled, cfg.dry_run, mask(cfg.hydra_url), cfg.max_donors if cfg.max_donors > 0
             else "unlimited")
    threading.Event().wait()


if __name__ == "__main__":
    main()
