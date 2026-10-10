#!/usr/bin/env python3
"""nzbget-dupe-proxy: impersonates nzbget toward NZBHydra2 and adds duplicate postings as donors.

Requests are forwarded verbatim to nzbget; JSON-RPC `append` gets a DupeKey, then a background worker finds other
postings of the same release via Hydra and appends them under that DupeKey with lower DupeScores, as donors for
nzbget's DupeArticleFallback. "Same release" is decided by PTT-parsed release names (title, episode, group,
resolution, source, codec, audio, HDR, ...), not by size. Python 3 stdlib + vendored PTT (pure Python)."""
import base64
import functools
import glob
import hashlib
import heapq
import itertools
import http.client
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import Counter, namedtuple
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, fields, replace
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
DEAD_TTL = 3 * 86400     # s: remember postings found dead (taken-down articles do not come back)
FETCH_RETRY_DELAY = 2.0  # s before re-fetching an NZB after an indexer error / non-NZB reply
MAX_NZB_BYTES = 64 * 1024 * 1024  # cap a fetched NZB: one posting's NZB is a few MB, so a huge reply is junk/hostile
SEARCH_PAGE, SEARCH_PAGES = 100, 5  # Hydra results per request, and pages read when a query fills them
WATCH_STATUSES = ("QUEUED", "PAUSED", "DOWNLOADING", "FETCHING")  # not yet past download
RELIST_WINDOW = 120.0     # s: listings of one size posted this close together are one posting on several indexers
INDEXER_COOLDOWN = 1800.0  # s an indexer is not asked for NZBs after it refused one (403/429: its grab limit)
Result = namedtuple("Result", "title link size grabs date indexer")  # one Hydra search hit
EXT_RE = re.compile(r"(\.(part\d+\.rar|vol\d+[+-]\d+\.par2|7z\.\d{3}|r\d{2}|z\d{2}|nzb|mkv|mp4|m4v|avi|ts|"
                    r"rar|par2|7z|zip|nfo|sfv|srr|srt|sub|idx|jpg|png|txt)|(?<![hx])\.\d{3})$", re.I)  # not H.264
JUNK_RE = re.compile(r"([.\-_ ](xpost|postbot|obfuscated|scrambled|asrequested|rp|rakuv[a-z0-9]*|buymore|"
                     r"chamele0n|sample|repost))+$", re.I)  # rakuv[a-z0-9]* not rakuv\w*: \w absorbs the "_"

MARKER_RE = re.compile(r"^(s\d{1,3}e\d{1,4}|(19|20)\d\d)$")      # episode / year: must match
RES_RE = re.compile(r"^(\d{3,4}p|4k|uhd)$")


@dataclass
class Config:
    listen_port: int = 6790
    listen_host: str = "0.0.0.0"  # set LISTEN_HOST=127.0.0.1 to keep the proxy off the network
    nzbget_url: str = "http://127.0.0.1:6789"
    hydra_url: str = ""
    hydra_apikey: str = ""
    max_donors: int = 0  # <= 0 (0 or -1): unlimited
    size_tolerance: float = 0.0  # > 0: skip Hydra results whose size differs more than this fraction
    state_dir: str = "/var/lib/nzbget-dupe-proxy"
    enabled: bool = True
    dry_run: bool = False
    health_percent: float = 5.0   # STAT this % of each NZB's articles on all news servers (0 = off) ...
    health_min_articles: int = 50   # ... but at least this many articles
    health_max_articles: int = 1000  # ... and at most this many
    donor_min_alive: float = 0.5  # drop donors whose sampled articles are alive on no server below this share
    swap_below: float = 0.9       # a pick sampled less alive than this is sure to fail: swap it ...
    swap_backup_alive: float = 0.95  # ... for a backup sampled at least this alive
    nzbs_to_check_concurrently: int = 10      # NZBs whose articles are checked at the same time
    nntp_server_connection_per_nzb: int = 1   # connections per news server for each NZB being checked
    max_conns_per_nntp_server: int = 20       # hard cap per server (also <= the server's nzbget Connections)
    body_percent: float = 20.0                # share of sampled articles that also get BODY + yEnc check ...
    body_max_per_nzb: int = 20                # ... but at most this many per NZB (one server downloads each)
    health_budget: float = 120.0  # s per health pass (probe of all donors / full samples of the rest)
    fast_donors: int = 5          # donors appended right after the quick probe; the rest after a full sample
    primary_score: int = 1000000  # DupeScore of a primary the proxy manages; donors sit just under it (nzbget's
    #   failover takes a backup only if backup score >= primary score * health / 1000)
    watch_nzbget: bool = False    # also watch nzbget's queue for picks added without the proxy (nzbdavkodi, uploads)
    watch_interval: float = 15.0  # s between queue polls
    watch_settle: float = 20.0    # s a pick waits in the queue first (its submitter's own backups arrive)
    nzbget_username: str = ""     # nzbget login for the watcher (the Hydra path uses Hydra's own)
    nzbget_password: str = ""
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
    listed: float = 1.0  # share of the files' parts the NZB lists (subjects declare "yEnc (1/N)"); 1.0 = all
    par_index: str = ""      # first article of the smallest par2 file (the index: holds the block size)
    vol_bytes: tuple = ()     # bytes of every other par2 file (the recovery volumes)
    data_bytes: int = 0       # bytes of the files par2 protects (all but the par2 files)
    article_bytes: int = 0    # median article size of those files

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
    n_listed = n_declared = 0  # parts the NZB lists / parts the subjects say each file has
    par2, articles = [], []    # (bytes, first message-id) of each par2 file; article sizes of the other files
    for el in safe_xml(data).iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "meta" and el.get("type"):
            meta[el.get("type").lower()] = (el.text or "").strip()
        elif tag == "file":
            files += 1
            poster = poster or el.get("poster", "")
            m = re.search(r'"([^"]+)"', el.get("subject", ""))
            name = (m.group(1) if m else el.get("subject", "")).strip()
            segs, first = [], None  # (number, bytes); the lowest-numbered article's message-id
            for seg in el.iter():
                if seg.tag.rsplit("}", 1)[-1] == "segment":
                    segs.append((_int(seg.get("number")), int(seg.get("bytes") or 0)))
                    sizes[name] = sizes.get(name, 0) + int(seg.get("bytes") or 0)
                    sid = (seg.text or "").strip()
                    if sid:  # a blank message-id is unaddressable; keeping "" would falsely match postings
                        ids.add(sid)
                        if first is None or segs[-1][0] < first[0]:
                            first = (segs[-1][0], sid)
            if re.search(r"\.par2$", name, re.I):
                par2.append((sum(b for _, b in segs), first[1] if first else ""))
            else:
                articles += [b for _, b in segs]
            n_listed += len(segs)
            n_declared += truncated_parts(segs, el.get("subject", "")) or numbered_parts(segs)
    if not files or not ids:
        raise ValueError("NZB has no files/segments")
    data_files = {n: b for n, b in sizes.items() if not re.search(r"\.par2$|\.vol\d+[+-]\d+", n, re.I)} or sizes
    par2.sort()
    return NzbInfo(files, sum(sizes.values()), frozenset(n.lower() for n in sizes), poster, frozenset(ids), meta,
                   max(data_files, key=data_files.get), n_listed / n_declared if n_declared else 1.0,
                   par_index=par2[0][1] if par2 else "", vol_bytes=tuple(b for b, _ in par2[1:]),
                   data_bytes=sum(articles), article_bytes=sorted(articles)[len(articles) // 2] if articles else 0)


def par2_block_size(data):
    """The slice (block) size in a par2 file's Main packet, or None."""
    i = data.find(b"PAR2\x00PKT")
    while 0 <= i and i + 72 <= len(data):
        length = int.from_bytes(data[i + 8:i + 16], "little")
        if data[i + 48:i + 64] == b"PAR 2.0\x00Main\x00\x00\x00\x00":
            return int.from_bytes(data[i + 64:i + 72], "little") or None
        i = data.find(b"PAR2\x00PKT", i + max(length, 8))
    return None


PAR_SWAP_BACKUP_ALIVE = 0.999  # a pick swapped on par2 grounds goes only to a backup sampled all there
PAR_MARGIN = 1.25  # the predicted damage must exceed the recovery blocks by this much (plus 2) to call a pick doomed


def par_doomed(info, alive, block):
    """Will par2 fail to repair a posting sampled `alive` whole? Each missing article spoils its whole block, so
    with independent losses a block of k articles survives with chance alive**k: damaged = data blocks *
    (1 - alive**k), against the recovery blocks its volumes hold (bytes / (block + 68), the packet overhead).
    (doomed, damaged, recovery); never doomed without known recovery volumes, block or article sizes."""
    if not (block and info.vol_bytes and info.article_bytes and info.data_bytes) or alive is None:
        return False, 0.0, 0
    blocks = -(-info.data_bytes // block)
    damaged = blocks * (1 - alive ** (block / info.article_bytes))
    recovery = sum(round(b / (block * 1.02 + 68)) for b in info.vol_bytes)  # NZB bytes are yEnc (~2% larger)
    return damaged > PAR_MARGIN * recovery + 2, damaged, recovery


MAX_PART_NUMBER = 200000  # beyond this a segment number is not a part number (no posting has that many)


def numbered_parts(segs):
    """Parts a file has up to its last listed one: segment numbers count from 1, so a number with no segment
    below the highest is a part the NZB leaves out (an NZBIndex fragment lists one article numbered 9374).
    Unusable numbering (0, negative, absurd) falls back to the listed count."""
    nums = [n for n, _ in segs]
    if not nums or min(nums) < 1 or max(nums) > MAX_PART_NUMBER:
        return len(segs)
    return max(max(nums), len(segs))


def truncated_parts(segs, subject):
    """The part count a file's subject declares ("yEnc (1/3709)") when its NZB plainly stops short of it, else
    None. Obfuscated postings pad the count by tens of parts (rars of 137 declaring 139..165, 20 full parts
    declaring 34..48, a one-part par2 declaring 26); a cut-short listing misses thousands. So it counts only
    when under 2/3 is listed, at least 100 parts lie past the last listed one, the file is no par2 (their short
    last parts run 92..99.8% of full), and the last listed part is full-size: the common size when most parts
    share one, else 99% of the median. A file that really ends there has a short last part."""
    m = re.search(r"\(\d+/(\d+)\)\s*$", subject)
    if not m or len(segs) < 2 or re.search(r"\.par2\b", subject, re.I):
        return None
    declared, (last_no, last_bytes) = int(m.group(1)), max(segs)
    if len(segs) * 3 >= declared * 2 or declared - last_no < 100:
        return None
    sizes = [b for _, b in segs]
    common, count = Counter(sizes).most_common(1)[0]
    full = last_bytes == common if count * 2 > len(sizes) else last_bytes >= 0.99 * sorted(sizes)[len(sizes) // 2]
    return declared if full else None


def with_unlisted(h, listed):
    """Health of a whole posting from a sample of the share `listed` of its parts the NZB lists: the parts it
    never lists are as missing as a 430, so a fully present 40%-listed NZB reads 40% alive."""
    if h is None or listed >= 1.0 or listed <= 0 or h.answered < donor_health.MIN_KNOWN:
        return h
    extra = round(h.answered * (1 / listed - 1))
    return replace(h, checked=h.checked + extra, missing=h.missing + extra)


def same_posting(a, b):
    """Any real article-ID overlap (> 1%) -> same posting: donors must point at different articles.
    (Indexers re-list one posting with a re-uploaded segment; distinct postings share 0%.)"""
    return len(a & b) > 0.01 * min(len(a), len(b))


SKETCH_K = 64  # message-ID hashes kept per posting sketch


def sketch(ids, k=SKETCH_K):
    """Bottom-k sketch of a posting: the k smallest CRC32s of its message-IDs. Postings that share most of
    their articles (one posting re-listed with a re-uploaded segment) share most of their sketch; distinct
    postings share about none. Compact enough to keep for every kept or dead NZB."""
    return tuple(heapq.nsmallest(k, {zlib.crc32(i.encode()) for i in ids}))


def same_sketch(a, b):
    k = min(len(a), len(b))
    return bool(k) and len(set(a) & set(b)) >= max(1, k // 8)


_KEPT = {}  # (path, mtime, size) -> sketch of an NZB file nzbget keeps


def kept_sketch(path):
    st = os.stat(path)
    key = (path, st.st_mtime, st.st_size)
    if key not in _KEPT:
        if len(_KEPT) > 4096:
            _KEPT.clear()
        with open(path, "rb") as f:
            _KEPT[key] = sketch(parse_nzb(f.read()).message_ids)
    return _KEPT[key]


def candidate_ok(primary_title, primary_bytes, r, tol):
    """Hydra result worth fetching: same release by name; size only matters if a tolerance is set."""
    if tol and primary_bytes and abs(r.size - primary_bytes) > tol * primary_bytes:
        return False
    return same_release(primary_title, r.title)


def group_listings(results):
    """Hydra results -> lists of listings of one posting: same size, posted within RELIST_WINDOW (indexers list
    a posting with its own usenetdate, a few seconds apart). A repost of the same files has the same size but
    another posting time. Listings without a size or date stay alone."""
    groups, last = [], None
    for r in sorted(results, key=lambda r: (r.size, r.date)):
        if last is not None and r.size and r.date and last[0].date and r.size == last[0].size and \
                r.date - last[0].date <= RELIST_WINDOW:
            last.append(r)
        else:
            last = [r]
            groups.append(last)
    return groups


def listing_mismatch(r, info):
    """The indexer served an NZB more than 2% off the size it lists (often another indexer's NZB)."""
    return bool(r.size) and abs(info.total_bytes - r.size) > 0.02 * r.size


def mask(text):
    """Hide apikeys and user:password pairs in URLs/paths before logging."""
    text = re.sub(r"(?i)(apikey=)[^&\s]+", r"\1***", str(text))
    text = re.sub(r"//[^/@\s]+:[^/@\s]+@", "//***@", text)
    return re.sub(r"/[^/:\s]+:[^/\s]+/(json|xml)rpc", r"/***/\1rpc", text)


class State:
    """JSON file {key: {t, title, fps: {message-id fingerprint: nzbid}},
    "_dead": {t, fps: {fingerprint: {t, s: sketch}}}}."""

    def __init__(self, state_dir):
        self.path = os.path.join(state_dir, "state.json")
        self.lock = threading.RLock()
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        # keep only well-formed entries: valid JSON of the wrong shape (a list, or values
        # without a numeric "t") would otherwise crash save()/group_for() later.
        if not isinstance(data, dict):
            data = {}
        self.data = {k: v for k, v in data.items() if isinstance(v, dict) and isinstance(v.get("t"), (int, float))}

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

    def nzbids_for(self, fp):
        """NZBIDs this posting was recorded under (as a primary or a donor), in any group."""
        return {g["fps"][fp] for k, g in self.data.items()
                if k != "_dead" and isinstance(g, dict) and fp in g.get("fps", {})}

    def mark_dead(self, fp, sk):
        with self.lock:
            dead = self.data.setdefault("_dead", {"t": time.time(), "fps": {}})
            dead["t"] = time.time()
            dead["fps"] = {f: e for f, e in dead["fps"].items()
                           if isinstance(e, dict) and time.time() - e["t"] < DEAD_TTL}
            dead["fps"][fp] = {"t": time.time(), "s": list(sk)}
            self.save()

    def is_dead(self, sk):
        """Was this posting (or a near-identical re-listing of it) found dead recently?"""
        entries = self.data.get("_dead", {}).get("fps", {}).values()
        return any(isinstance(e, dict) and time.time() - e["t"] < DEAD_TTL and same_sketch(sk, e["s"])
                   for e in entries)

    def record(self, key, fp, nzbid, title=None, touch=False):
        g = self.data.setdefault(key, {"t": time.time(), "title": title, "fps": {}})
        if touch:
            g.update(t=time.time(), title=title)
        g["fps"][fp] = nzbid
        self.save()

    def begin_search(self, key, nzbid):
        g = self.data.get(key)
        if g is not None:
            g.setdefault("searching", {})[str(nzbid)] = time.time()
            self.save()

    def end_search(self, key, nzbid):
        with self.lock:
            g = self.data.get(key)
            if g is not None and g.get("searching", {}).pop(str(nzbid), None) is not None:
                self.save()

    def search_unfinished(self, nzbid):
        """True when nzbid's search began over SEARCH_STALE seconds ago and never ended: its worker died."""
        now = time.time()
        return any(now - g.get("searching", {}).get(str(nzbid), now) > SEARCH_STALE
                   for k, g in self.data.items() if k != "_dead" and isinstance(g, dict))


def fleet_ranked(key, items):
    """True when any item of `key` carries the DupeFleet post-processing parameter: nzbget's appendfleet
    measured and ranked the copies itself, so the extension must not run a second discovery/ranking (B87)."""
    k = str(key).lower()
    for x in items:
        if str(x.get("DupeKey", "")).lower() == k and any(
                p.get("Name") == "DupeFleet" for p in (x.get("Parameters") or [])):
            return True
    return False


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


SEARCH_STALE = 600  # seconds after which an unfinished search (its worker was killed) may be started again
RANK_REUSE = 120  # seconds a ranking of a key's backups is reused (every backup's nzbget event starts a worker)


def score_base(primary):
    """Offset of every DupeScore sent under a primary scored `primary`: donors base+2..base+90, dead base+1."""
    return max(0, primary - 1000)


def target_score(alive, twin=False):
    """DupeScore for a donor: the most whole first. nzbget tries dupes by highest DupeScore, so 9 + 80 * alive
    share (unknown = 1.0): 100% alive = 89, 50% = 49; a byte-identical twin of the primary (same files and bytes:
    article borrowing and whole-file recreation work best with it) gets +1, so it wins a tie. Always below the
    primary's 100; 1 is reserved for dead postings."""
    return min(90, 9 + round(80 * (1.0 if alive is None else alive)) + bool(twin))


class Ranks:
    """Unique donor DupeScores (target_score; equal values count down: 90, 89, ...)."""

    def __init__(self):
        self.used, self.lock = set(), threading.Lock()  # shared by donors and the submitter's own backups

    def take(self, alive, twin=False):
        with self.lock:
            score = target_score(alive, twin)
            while score in self.used and score > 2:
                score -= 1
            self.used.add(score)
            return score

    def release(self, score):
        with self.lock:
            self.used.discard(score)


@dataclass
class Placed:
    """A donor appended to nzbget, as last scored."""
    id: int
    score: int
    alive: object  # share, or None if unknown
    twin: bool
    v: tuple       # (Result, nzb bytes, NzbInfo)


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


MAX_BODY_BYTES = 512 * 1024 * 1024  # 512 MiB: far above any real appendfleet (<=50 members), bounds an upload OOM


class Handler(BaseHTTPRequestHandler):
    proxy = None  # set by Proxy.start
    protocol_version = "HTTP/1.1"
    timeout = 120  # bound a slow or short body against a (lying) Content-Length so a read can't hang the worker

    def log_message(self, fmt, *args):
        log.debug(mask(fmt % args))

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0:
                raise ValueError("negative Content-Length")
        except ValueError:  # a malformed Content-Length is a bad request, not a crash
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if length > MAX_BODY_BYTES:  # reject on the header; never buffer a multi-GB upload (OOM)
            self.send_response(413)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            body = self.rfile.read(length)
        except (socket.timeout, OSError):  # incomplete/slow body: free the worker instead of hanging
            return
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
        self.indexer_locks = {}  # indexer -> one fetch at a time
        self.refused = dict(self.state.data.get("_refused", {}).get("until", {}))  # indexer -> refused until
        self.ctx = threading.local()             # per discovery: .base, added to every DupeScore sent
        self.watched, self.first_seen = {}, {}  # watcher: NZBID -> name it was handled under; NZBID -> first seen in the queue
        self.blocks, self.block_lock = {}, threading.Lock()  # par2 index article -> its block size (or None)

    def _indexer_lock(self, indexer):
        with self.workers_lock:
            return self.indexer_locks.setdefault(indexer, threading.Lock())

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
            given = params[6] if isinstance(params[6], str) else ""  # a non-string DupeKey is unusable (unhashable)
            key = given or self.state.group_for(title, t0) or "dupes:" + normalize_title(title)
            g = self.state.data.get(key)
            fresh = bool(g) and t0 - g["t"] < GROUP_WINDOW
            old = info and fresh and self.state.sent(key, info.fingerprint)
            if old:
                log.info("append key=%s nzbid=%s title=%s: posting already sent, not re-adding", key, old, title)
                reply = {"version": "1.1", "id": req.get("id"), "result": old}
                return 200, json.dumps(reply).encode(), "application/json"
            params[6:9] = [key, self.cfg.primary_score, "SCORE"]
            status, rbody, ctype = self.forward(path, json.dumps(req).encode(), headers)
            nzbid = rpc_result(status, rbody)
            if info and nzbid > 0:
                self.state.record(key, info.fingerprint, nzbid, title, touch=not fresh)
        if nzbid <= 0 or not info or fresh or not self.cfg.hydra_url:
            why = "joined existing group" if fresh else "no discovery"
            log.info("append key=%s nzbid=%s title=%s: %s", key, nzbid, title, why)
        else:
            t = threading.Thread(target=self._discover_safe, daemon=True,
                                 args=(key, title, info, params[2], path, headers.get("Authorization"), nzbid, t0,
                                       score_base(self.cfg.primary_score)))
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
        """All results of one newznab query, reading further pages while a page comes back full."""
        out = []
        for page in range(SEARCH_PAGES):
            got = self._hydra_page(dict(params, limit=SEARCH_PAGE, offset=page * SEARCH_PAGE))
            out += got
            if len(got) < SEARCH_PAGE:
                break
        return out

    def _hydra_page(self, params):
        query = urllib.parse.urlencode(dict(params, apikey=self.cfg.hydra_apikey))
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
        norm, short, group = normalize_title(title), short_query(title), release_attrs(title)["group"]
        qs = [{"t": "search", "q": norm.replace(".", " ")}]
        if short and short != qs[0]["q"]:
            qs.append({"t": "search", "q": short})
        if short and group:  # other indexers often rename a release; the group narrows to its postings
            qs.append({"t": "search", "q": "%s %s" % (short, group)})
        imdb, tvdb = (re.sub(r"\D", "", info.meta.get(k, "")) for k in ("imdb", "tvdb"))
        if imdb:
            qs.append({"t": "movie", "imdbid": imdb})
        m = re.search(r"(?:^|\.)s(\d+)e(\d+)(?:\.|$)", norm)
        if tvdb and m:
            qs.append({"t": "tvsearch", "tvdbid": tvdb, "season": int(m.group(1)), "ep": int(m.group(2))})
        return qs

    def fetch(self, r, deadline, retries=1):
        """-> (reason, nzb bytes, NzbInfo); reason 'ok', 'fetch', 'parse' or 'refused'. Indexers answer rate limits
        with 403/429 or an error body, sometimes only for a moment, so a failed fetch is retried once; an indexer
        still refusing (403/429) is not asked again for INDEXER_COOLDOWN. One fetch per indexer at a time, so
        its refusal is known before the next grab."""
        with self._indexer_lock(r.indexer):
            if self.refused.get(r.indexer, 0) > time.time():
                return "refused", None, None
            reason, data, info, status = self._fetch(r, deadline, retries)
            if status in (403, 429) and r.indexer:
                self.refused[r.indexer] = time.time() + INDEXER_COOLDOWN
                with self.state.lock:  # kept on disk: the next process (nzbget extension) honours it too
                    self.state.data["_refused"] = {"t": time.time(), "until": {
                        k: v for k, v in self.refused.items() if v > time.time()}}
                    self.state.save()
                log.info("indexer %s refused NZB downloads (HTTP %d): skipping it for %d min", r.indexer, status,
                         INDEXER_COOLDOWN // 60)
            return reason, data, info

    def _fetch(self, r, deadline, retries):
        status = None
        for attempt in range(retries + 1):
            timeout = max(1.0, min(self.cfg.timeout, deadline - time.time()))
            try:
                with urllib.request.urlopen(r.link, timeout=timeout) as resp:
                    data = resp.read(MAX_NZB_BYTES + 1)  # bounded: never buffer a huge/hostile reply into memory
            except (OSError, http.client.HTTPException, ValueError) as e:  # a cut-short reply or an unusable link too
                reason, why, status = "fetch", mask(e), getattr(e, "code", None)
            else:
                status = None
                if len(data) > MAX_NZB_BYTES:
                    reason, why = "too-big", "reply over %d MiB" % (MAX_NZB_BYTES >> 20)
                else:
                    try:
                        return "ok", data, parse_nzb(data), None
                    except ValueError:
                        reason, why = "parse", "not an NZB: %r" % mask(data[:160].decode("utf-8", "replace"))
            log.info("donor %s failed %s (%s), attempt %d: %s", reason, r.title, r.indexer, attempt + 1, why)
            if attempt < retries and time.time() + FETCH_RETRY_DELAY < deadline:
                time.sleep(FETCH_RETRY_DELAY)
        return reason, None, None, status

    def fetch_posting(self, listings, deadline):
        """One NZB of a posting listed by several indexers: its listings in turn until one serves an NZB of its
        listed size (an indexer may serve another indexer's NZB for a listing; that one is kept only if no
        listing serves the posting itself). -> (Result, reason, data, NzbInfo, Counter of the attempts)."""
        stats, last, other = Counter(), (listings[0], "fetch", None, None), None
        for j, r in enumerate(listings):
            reason, data, ci = self.fetch(r, deadline)
            if reason == "ok" and not listing_mismatch(r, ci):
                if j + 1 < len(listings):
                    stats["relisted"] += len(listings) - j - 1  # never fetched: the same posting
                return r, reason, data, ci, stats
            if reason == "ok":
                other = other or (r, reason, data, ci)
            else:
                stats[reason] += 1
                last = (r, reason, None, None)
        return (other or last) + (stats,)

    def discover(self, key, title, info, category, path, auth, nzbid, t0, base=0):
        try:
            return self._discover(key, title, info, category, path, auth, nzbid, t0, base)
        finally:
            self.state.end_search(key, abs(nzbid))  # a killed worker never gets here: the search stays unfinished

    def _discover(self, key, title, info, category, path, auth, nzbid, t0, base=0):
        """`base` lifts every DupeScore sent (donors base+2..base+90, dead base+1): 0 under the proxy's own
        primary at 100, pick - 1000 under a pick that a submitter scored higher (and its own backups)."""
        self.ctx.base = base
        cfg, stats, deadline, results = self.cfg, Counter(), t0 + self.cfg.deadline, {}
        nzbget_config = self.rpc_call(path, auth, "config", []) or []
        servers = self.news_servers(nzbget_config, title) if cfg.health_percent else []
        # the primary's own probe starts now, beside the searches: a dead primary is demoted within seconds,
        # before nzbget downloads (or parks) it
        primary_check = threading.Thread(target=self.check_primary, daemon=True,
                                         args=(servers, path, auth, nzbid, title, info, base))
        primary_check.start()
        ranks = Ranks()
        # the submitter's own backups (already in nzbget's history) get the same health ranking as donors
        checked_backups = {}  # rank_backups fills in "ranked" and "pick": the swap waits for the donors too
        backups = threading.Thread(target=self.rank_backups, daemon=True,
                                   args=(servers, path, auth, key, title, abs(nzbid), ranks, stats, base,
                                         info.message_ids if nzbid > 0 else None, checked_backups,
                                         info.total_bytes, info.listed))
        backups.start()
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
            rank = {id(r): i for i, r in enumerate(cands)}
            # postings (each listed by one or more indexers; most grabbed listing first), best listing first,
            # then one posting of each distinct size before the rest
            order = sorted((sorted(g, key=lambda r: rank[id(r)]) for g in group_listings(cands)),
                           key=lambda g: rank[id(g[0])])
            first_of_size = {g[0].size: g for g in reversed(order)}
            order = sorted(order, key=lambda g: first_of_size[g[0].size] is not g)
            if cfg.max_donors > 0:  # each fetch costs an indexer grab
                order = order[:3 * cfg.max_donors]
            known_f = pool.submit(self.known_postings, path, auth, nzbget_config, key, title)  # beside the fetches
            verified, postings = [], [info.message_ids]
            for i in range(0, len(order), 4):  # fetch the whole (capped) order: health may drop some later
                if time.time() > deadline:
                    stats["deadline"] += len(order) - i
                    break
                chunk = order[i:i + 4]
                for r, reason, data, ci, tried in pool.map(self.fetch_posting, chunk, [deadline] * len(chunk)):
                    stats.update(tried)
                    if reason != "ok":
                        continue
                    if listing_mismatch(r, ci):
                        stats["listing-mismatch"] += 1  # the indexer served some other NZB than it lists
                        log.info("%s served a different NZB than it lists for %s: %d bytes / %d files, "
                                 "listed %d bytes", r.indexer, r.title, ci.total_bytes, ci.files, r.size)
                    known, sk = known_f.result(), sketch(ci.message_ids)
                    if readable(ci.main_name) and not same_release(title, ci.main_name):
                        stats["other-release"] += 1
                    elif any(same_sketch(sk, k) for k in known):
                        stats["in-nzbget"] += 1
                    elif self.state.is_dead(sk):
                        stats["known-dead"] += 1  # found dead on an earlier grab: no new health check
                    elif any(same_posting(ci.message_ids, ids) for ids in postings):
                        stats["same-posting"] += 1
                    else:
                        postings.append(ci.message_ids)
                        verified.append((r, data, ci))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        verified.sort(key=lambda v: (dist(v[2].total_bytes), -v[0].grabs, -v[0].date))
        all_servers, servers = servers, servers if verified else []  # the swap still needs them for the pick
        probe = self.health(servers, title, {ci.fingerprint: ci.message_ids for _, _, ci in verified}, full=False,
                            listed={ci.fingerprint: ci.listed for _, _, ci in verified})
        twin = lambda ci: ci.files == info.files and ci.total_bytes == info.total_bytes  # noqa: E731
        live = [v for v in verified if not self.dead(v, probe, stats, "probe")]
        n_fast = int(min(cfg.fast_donors, cfg.donor_cap))
        added, placed = 0, {}  # placed: fingerprint -> Placed, donors now in nzbget
        for v in live[:n_fast]:  # quick: probe-alive donors go to nzbget right away
            score = ranks.take(alive_of(probe, v), twin(v[2]))
            donor_id = self.add_donor(key, category, path, auth, v, score, probe, "fast", stats)
            added += donor_id != 0
            if donor_id > 0:
                placed[v[2].fingerprint] = Placed(donor_id, score, alive_of(probe, v), twin(v[2]), v)
        # then full samples of every donor at once, handled as each finishes: refine fast donors, add the rest
        todo = {v[2].fingerprint: (v, True) for v in live[:n_fast] if servers}
        todo.update({v[2].fingerprint: (v, False) for v in live[n_fast:]})
        groups = {fp: v[2].message_ids for fp, (v, _) in todo.items()}
        groups["primary"] = info.message_ids
        listed = {fp: v[2].listed for fp, (v, _) in todo.items()}
        listed["primary"] = info.listed
        checked = self.health_iter(servers, title, groups, full=True, listed=listed) if servers else iter(())
        unchecked = ((fp, None) for fp in list(todo))  # whatever the check did not report (no servers, failure)
        for fp, h in itertools.chain(checked, unchecked):
            if fp == "primary":
                log.info("health %s: primary alive=%s on %d server(s)", title, pct(h.alive), len(servers))
                continue
            entry = todo.pop(fp, None)
            if entry is None:  # already handled from the check's own result
                continue
            v, was_fast = entry
            health = {fp: h} if h else {}
            if was_fast:
                if fp in placed:
                    self.rescore(path, auth, placed[fp], health, ranks, stats)
            elif added >= cfg.donor_cap:
                stats["over-cap"] += 1
            elif not self.dead(v, health, stats, "full sample"):
                score = ranks.take(alive_of(health, v), twin(v[2]))
                donor_id = self.add_donor(key, category, path, auth, v, score, health, "checked", stats)
                added += donor_id != 0
                if donor_id > 0:
                    placed[fp] = Placed(donor_id, score, alive_of(health, v), twin(v[2]), v)
        self.rerank(path, auth, placed.values(), dist, stats)
        primary_check.join()
        backups.join()
        if checked_backups.get("pick") is not None:  # swap candidates: the client's backups and the new donors
            candidates = dict(checked_backups.get("ranked", {}))
            candidates.update({p.id: (p.score, p.alive, False) for p in placed.values()
                               if p.score > 1 and p.alive is not None})
            self.swap_if_failing(path, auth, title, abs(nzbid), checked_backups["pick"], candidates, info, all_servers)
        log.info("append key=%s nzbid=%s title=%s results=%d candidates=%d verified=%d added=%d%s rejected=%s "
                 "time=%.1fs", key, nzbid, title, len(results), len(cands), len(verified),
                 0 if cfg.dry_run else added, " dry_run would_add=%d" % added if cfg.dry_run else "",
                 dict(stats), time.time() - t0)

    def known_postings(self, path, auth, nzbget_config, key, title):
        """Sketches of the NZBs nzbget already holds for this release: queue and history items with this
        DupeKey or the same release name, read (cached) from the copies nzbget keeps in its NzbDir."""
        opts = {e.get("Name"): str(e.get("Value", "")) for e in nzbget_config}
        nzbdir = opts.get("NzbDir", "").replace("${MainDir}", opts.get("MainDir", ""))
        if not nzbdir:
            return []
        items = (self.rpc_call(path, auth, "history", [True]) or []) + (self.rpc_call(path, auth, "listgroups", [0]) or [])
        names = {x["NZBFilename"] for x in items if x.get("NZBFilename") and (
            x.get("DupeKey") == key or same_release(title, x.get("NZBName") or ""))}
        out = []
        for name in names:
            for f in glob.glob(glob.escape(os.path.join(nzbdir, name)) + "*.queued"):
                try:
                    out.append(kept_sketch(f))
                except (OSError, ValueError):
                    continue
        return out

    def news_servers(self, nzbget_config, title):
        try:
            servers = donor_health.servers_from_nzbget_config(nzbget_config, self.cfg.max_conns_per_nntp_server,
                                                              self.cfg.timeout)
        except (ValueError, TypeError, KeyError) as e:
            log.warning("health check skipped for %s: unreadable news server config (%s)", title, type(e).__name__)
            return []
        if not servers:
            log.info("health check skipped for %s: no active news servers in nzbget config", title)
        return servers

    def health_iter(self, servers, title, groups, full, listed=None):
        """Yields (key, Health) per NZB as its parallel check finishes; nothing when unchecked (advisory).
        listed: {key: NzbInfo.listed}; the parts an NZB never lists count as missing, since a sample of the
        listed articles alone reads an NZB that lists 40% of its file as whole."""
        if not servers:
            return
        limits = donor_health.Limits(self.cfg.nzbs_to_check_concurrently, self.cfg.nntp_server_connection_per_nzb)
        try:
            for key, h in donor_health.check_iter(servers, groups, self.cfg.health_percent,
                                                  budget=self.cfg.health_budget, full=full, limits=limits,
                                                  body_percent=self.cfg.body_percent, max_body=self.cfg.body_max_per_nzb,
                                                  minimum=self.cfg.health_min_articles,
                                                  maximum=self.cfg.health_max_articles):
                yield key, with_unlisted(h, (listed or {}).get(key, 1.0))
        except Exception as e:
            log.warning("health check failed for %s: %s", title, type(e).__name__)

    def health(self, servers, title, groups, full, listed=None):
        """{key: Health} for all `groups` (see health_iter)."""
        return dict(self.health_iter(servers, title, groups, full, listed))

    def dead(self, v, health, stats, phase):
        """Probe: dead only if nothing was found (10 articles are too few to judge a share). Full sample:
        dead below DONOR_MIN_ALIVE."""
        h = health.get(v[2].fingerprint)
        if h is None or h.alive is None or h.missing < donor_health.MIN_KNOWN:  # errors alone prove nothing
            return False
        if not donor_health.dead_probe(h) if phase == "probe" else (h.alive >= self.cfg.donor_min_alive):
            return False
        stats["dead"] += 1
        self.state.mark_dead(v[2].fingerprint, sketch(v[2].message_ids))
        log.info("dropping dead donor %s [%s] after %s: alive=%s (%d of %d answered articles on no server, "
                 "%d errors)", v[0].title, v[0].indexer, phase, pct(h.alive), h.answered - h.present, h.answered,
                 h.error)
        return True

    def check_primary(self, servers, path, auth, nzbid, title, info, base=0):
        """Probe the primary; one with nothing on any server is demoted to DupeScore 1, so nzbget swaps in
        the healthiest donor as soon as one arrives. Never a partly alive one: the swap deletes what it
        downloaded, which is exactly what nzbget's repair from duplicates needs."""
        h = self.health(servers, title, {"primary": info.message_ids}, full=False,
                        listed={"primary": info.listed}).get("primary")
        if h is None or not donor_health.dead_probe(h) or nzbid <= 0:
            return
        self.state.mark_dead(info.fingerprint, sketch(info.message_ids))
        what = "%s: primary is dead (%d of %d probe articles on no server)" % (title, h.missing, h.answered)
        if self.cfg.dry_run:
            log.info("%s; DRY-RUN: not demoted", what)
        elif self.rpc_call(path, auth, "editqueue", ["GroupSetDupeScore", str(base + 1), [nzbid]]):
            log.info("%s: DupeScore -> %d, the healthiest donor takes over", what, base + 1)
        else:
            log.info("%s but already left the queue (nzbget parked or finished it); donors take over from history",
                     what)

    def rank_backups(self, servers, path, auth, key, title, pick_id, ranks, stats, base=0, pick_ids=None,
                     out=None, pick_bytes=None, pick_listed=1.0):
        """Health-check the backups nzbget already holds under `key` (the submitter's own, parked in history
        as DUPE or COPY) and set their DupeScore like a donor's: most whole first, dead ones at base+1, so
        nzbget's failover goes straight to the wholest instead of trying them in the submitter's order."""
        self.ctx.base = base
        ranked = {}  # nzbid -> (score, alive) of each backup checked
        if not servers or not key:
            return ranked
        hist_all = self.rpc_call(path, auth, "history", [True]) or []
        if fleet_ranked(key, hist_all):  # nzbget's appendfleet ranked these; a second ranking only races it (B87)
            log.info("rank_backups %s: key=%s was ranked by nzbget (DupeFleet set): leaving the backups alone", title, key)
            return ranked
        items = [x for x in hist_all
                 if str(x.get("DupeKey", "")).lower() == key.lower() and _int(x.get("NZBID")) != pick_id
                 and x.get("Status") in ("DELETED/DUPE", "DELETED/COPY")]
        groups, seen, twins = {}, {}, []  # twins: backups with the same NZB as one already in groups
        unknown = []  # backups whose health stays unknown (NZB unreadable, or no server answered)
        for x in items:
            info, _ = self.queued_nzb(path, auth, x)
            if info is None:
                unknown.append(x)
                continue
            if info.fingerprint in seen:
                twins.append((_int(x["NZBID"]), x, seen[info.fingerprint]))
                continue
            seen[info.fingerprint] = _int(x["NZBID"])
            groups[_int(x["NZBID"])] = (x, info)
        if not groups and not pick_ids and not unknown:
            return ranked
        cached = self.state.data.get(key, {}).get("ranked") or {}
        if (not pick_ids and cached and time.time() - cached.get("t", 0) < RANK_REUSE
                and set(cached.get("items", {})) == {str(i) for i in groups}
                and {_int(x.get("NZBID")) for x in unknown} <= set(cached.get("unknown", []))):
            for bid, (score, alive, dead) in cached["items"].items():  # same backups, ranked a moment ago
                ranked[int(bid)] = (score, alive, dead)
            log.info("health %s: reusing the ranking of %d backup(s) from %d s ago", title, len(groups),
                     time.time() - cached["t"])
            return ranked
        log.info("health %s: checking %d backup(s) already in nzbget", title, len(groups))
        check = {i: info.message_ids for i, (_, info) in groups.items()}
        listed = {i: info.listed for i, (_, info) in groups.items()}
        if pick_ids:  # the pick's own full sample: is it sure to fail?
            check["pick"], listed["pick"] = pick_ids, pick_listed
        pick_h, live = None, []
        for bid, h in self.health_iter(servers, title, check, full=True, listed=listed):
            if bid == "pick":
                pick_h = h
                continue
            x, info = groups[bid]
            if h is None or h.alive is None:
                unknown.append(x)
                continue
            dead = h.alive < self.cfg.donor_min_alive and h.missing >= donor_health.MIN_KNOWN
            if not dead:
                live.append((bid, h))  # scored below, once all are in: closest size first among equals
                continue
            score = 1
            if dead and _int(x.get("DupeScore")) <= score + base:  # already below base+1: never raise a dead one
                self.rpc_call(path, auth, "editqueue", ["HistorySetParameter", "DupeAlive=0", [bid]])
                score = _int(x.get("DupeScore")) - base
            elif not self.set_score(path, auth, bid, score, "DupeAlive=%d" % round(100 * h.alive)):
                stats["rescore"] += 1
                continue
            stats["backup-ranked"] += 1
            ranked[bid] = (score, h.alive, dead)
            if dead:
                self.state.mark_dead(info.fingerprint, sketch(info.message_ids))
            log.info("ranked backup nzbid=%d %s: score %d -> %d, alive=%s%s", bid, x.get("NZBName") or x.get("Name"),
                     _int(x.get("DupeScore")), score + base, pct(h.alive), ", dead" if dead else "")
        # live backups, wholest first; among equals the size closest to the pick's (a different size is a different file)
        live.sort(key=lambda bh: (-round(100 * bh[1].alive),
                                  abs(groups[bh[0]][1].total_bytes - pick_bytes) if pick_bytes else 0))
        for bid, h in live:
            x, info = groups[bid]
            score = ranks.take(h.alive)
            if not self.set_score(path, auth, bid, score, "DupeAlive=%d" % round(100 * h.alive)):
                stats["rescore"] += 1
                continue
            stats["backup-ranked"] += 1
            ranked[bid] = (score, h.alive, False)
            log.info("ranked backup nzbid=%d %s: score %d -> %d, alive=%s", bid, x.get("NZBName") or x.get("Name"),
                     _int(x.get("DupeScore")), score + base, pct(h.alive))
        # unknown health: below the checked live ones, above the dead, in the submitter's order; left at its own
        # score (just under the pick) it would outrank every checked backup and be failed over to first
        for x in sorted(unknown, key=lambda x: -_int(x.get("DupeScore"))):
            score = ranks.take(None)
            if _int(x.get("DupeScore")) <= score + base:  # already in or below the ranked band: never raise
                ranks.release(score)
                continue
            if self.set_score(path, auth, _int(x.get("NZBID")), score):
                stats["backup-ranked"] += 1
                log.info("ranked backup nzbid=%s %s: score %d -> %d, health unknown", x.get("NZBID"),
                         x.get("NZBName") or x.get("Name"), _int(x.get("DupeScore")), score + base)
        for tid, x, sibling in twins:  # the same posting is as healthy as its sibling: same rank, never its stale score
            if sibling not in ranked:
                continue
            score, alive, dead = ranked[sibling]
            if dead and _int(x.get("DupeScore")) <= score + base:
                continue  # already below a dead one's rank: never raise
            if self.set_score(path, auth, tid, score, "DupeAlive=%d" % round(100 * alive)):
                stats["backup-ranked"] += 1
                log.info("ranked backup nzbid=%d %s: same NZB as nzbid=%d, score %d -> %d", tid,
                         x.get("NZBName") or x.get("Name"), sibling, _int(x.get("DupeScore")), score + base)
        if ranked or unknown:
            with self.state.lock:
                g = self.state.data.setdefault(key, {"t": time.time(), "title": title, "fps": {}})
                g["ranked"] = {"t": time.time(), "items": {str(b): list(v) for b, v in ranked.items()},
                               "unknown": [_int(x.get("NZBID")) for x in unknown], "base": base, "pick": pick_id,
                               "seen": [_int(x.get("NZBID")) for x in items]}  # every backup this ranking covered
                self.state.save()
        if out is not None:  # the caller swaps once its own donors are in too
            out.update(ranked=ranked, pick=pick_h)
        elif pick_h is not None:
            self.swap_if_failing(path, auth, title, pick_id, pick_h, ranked)
        return ranked

    def rank_late_backup(self, path, auth, backup):
        """A submitter's backup that landed after its key was ranked (the pick's settle time ran out first) still
        holds its own score, just under the pick and above every ranked backup: rank the key's backups again."""
        key, bid = backup.get("DupeKey") or "", _int(backup.get("NZBID"))
        with self.state.lock:
            g = self.state.data.get(key) or {}
            r, ours = dict(g.get("ranked") or {}), set(g.get("fps", {}).values())
        if not key or "base" not in r or bid in r.get("seen", []) or bid in ours:
            return {}  # not a key ranked here, a backup that ranking covered, or a donor (or pick) of this proxy's
        title = backup.get("NZBName") or backup.get("Name") or ""
        log.info("rank %s: backup nzbid=%d landed after key=%s was ranked: ranking its backups again", title, bid, key)
        servers = self.news_servers(self.rpc_call(path, auth, "config", []) or [], title)
        return self.rank_backups(servers, path, auth, key, title, r.get("pick", 0), Ranks(), Counter(), r["base"])

    def rank_late_backups(self, path, auth):
        """Recovery: late backups whose own workers an nzbget restart killed (nzbget never repeats NZB_ADDED)."""
        for x in self.rpc_call(path, auth, "history", [True]) or []:
            if x.get("Status") in ("DELETED/DUPE", "DELETED/COPY"):
                self.rank_late_backup(path, auth, x)

    def par_block(self, servers, info):
        """The par2 block size of an NZB's index file (one small BODY), cached per index article; None if unknown."""
        if not info.par_index or not servers:
            return None
        with self.block_lock:
            if info.par_index in self.blocks:
                return self.blocks[info.par_index]
        data = donor_health.fetch_body(servers, info.par_index)
        block = par2_block_size(data) if data else None
        with self.block_lock:
            self.blocks[info.par_index] = block
        return block

    def swap_if_failing(self, path, auth, title, pick_id, h, ranked, info=None, servers=None):
        """A pick whose full sample shows it will fail (alive below SWAP_BELOW; nzbget's own health only counts
        failures against the whole download, so it crawls for hours first) is swapped for the wholest backup,
        if that one is at least SWAP_BACKUP_ALIVE: the backup goes back to the queue, then the pick is filed as a
        dupe backup scored by its sample (GroupDelete would file it DELETED/MANUAL, which nzbget never fails over to)."""
        if h.alive is None or h.missing < donor_health.MIN_KNOWN:
            return 0
        need = self.cfg.swap_backup_alive
        if h.alive >= self.cfg.swap_below:  # mostly whole, but big par2 blocks can still make it unrepairable
            need = PAR_SWAP_BACKUP_ALIVE  # ... and then so could a backup a little less whole: only an all-there one
            block = self.par_block(servers, info) if info is not None else None
            doomed, damaged, recovery = par_doomed(info, h.alive, block) if block else (False, 0, 0)
            if not doomed:
                return 0
            log.info("swap %s: pick nzbid=%d sampled alive=%s, but its %d MB par2 blocks put ~%d blocks out of %d "
                     "recovery: par2 cannot repair it", title, pick_id, pct(h.alive), block // 1000000, damaged,
                     recovery)
        queued = self.rpc_call(path, auth, "listgroups", [0]) or []
        item = next((g for g in queued if _int(g.get("NZBID")) == pick_id), None)
        if item and item.get("Status") in WATCH_STATUSES and _int(item.get("FileSizeMB")) > 0 \
                and _int(item.get("DownloadedSizeMB")) >= 0.8 * _int(item.get("FileSizeMB")) \
                and _int(item.get("Health")) >= _int(item.get("CriticalHealth")):
            log.info("swap %s: pick nzbid=%d sampled alive=%s, but it is %d%% downloaded and healthy (%d of critical "
                     "%d): no swap", title, pick_id, pct(h.alive),
                     100 * _int(item.get("DownloadedSizeMB")) // _int(item.get("FileSizeMB")),
                     _int(item.get("Health")), _int(item.get("CriticalHealth")))
            return 0  # nearly done and above critical: par repair or stream repair finishes it, nzbget's failover covers the rest
        if not any(_int(g.get("NZBID")) == pick_id and g.get("Status") in WATCH_STATUSES for g in queued):
            log.info("swap %s: pick nzbid=%d sampled alive=%s, but it already left the queue: no swap", title,
                     pick_id, pct(h.alive))
            return 0  # discovery outlasted the download: the pick is done (or parked), a swap would redownload
        best = max(((alive, bid) for bid, (_, alive, dead) in ranked.items() if not dead), default=None)
        if best is None or best[0] < need:
            log.info("swap %s: pick nzbid=%d sampled alive=%s, will likely fail, but no backup is whole enough",
                     title, pick_id, pct(h.alive))
            return 0
        alive, bid = best
        if self.cfg.dry_run:
            log.info("swap %s: DRY-RUN would swap pick nzbid=%d (alive=%s) for nzbid=%d (alive=%s)", title,
                     pick_id, pct(h.alive), bid, pct(alive))
            return 0
        if not self.rpc_call(path, auth, "editqueue", ["HistoryRedownload", "", [bid]]):
            log.warning("swap %s: could not return backup nzbid=%d", title, bid)
            return 0
        self.set_score(path, auth, pick_id, target_score(h.alive), "DupeAlive=%d" % round(100 * h.alive))
        self.rpc_call(path, auth, "editqueue", ["GroupDupeDelete", "", [pick_id]])
        log.info("swap %s: pick nzbid=%d sampled alive=%s will fail: swapped for backup nzbid=%d (alive=%s)",
                 title, pick_id, pct(h.alive), bid, pct(alive))
        return bid

    def rescue(self, path, auth, failed):
        """A pick that failed its health check (history item `failed`) while nothing of its DupeKey is left
        in the queue (DupeMode FORCE, or backups nzbget filed as copies): rank its backups by health and send
        the wholest alive one back to the queue. Never fail outright while a viable backup remains."""
        key, title = failed.get("DupeKey") or "", failed.get("NZBName") or failed.get("Name") or ""
        if not key:
            return 0
        done = [x for x in self.rpc_call(path, auth, "history", [True]) or []
                if str(x.get("DupeKey", "")).lower() == key.lower() and str(x.get("Status", "")).startswith("SUCCESS")]
        if done:
            log.info("rescue %s: pick nzbid=%s failed, but key=%s already has a successful download (nzbid=%s)", title,
                     failed.get("NZBID"), key, done[0].get("NZBID"))
            return 0  # nzbget's failover already delivered one: another backup would download the episode twice
        servers = self.news_servers(self.rpc_call(path, auth, "config", []) or [], title)
        # rank the remaining backups either way: if nzbget's failover already returned one and it dies too,
        # the next failover must go to the wholest
        ranked = self.rank_backups(servers, path, auth, key, title, _int(failed.get("NZBID")), Ranks(), Counter(),
                                   score_base(_int(failed.get("DupeScore"))))
        queue = self.rpc_call(path, auth, "listgroups", [0]) or []
        if any(str(x.get("DupeKey", "")).lower() == key.lower() for x in queue):
            log.info("rescue %s: pick nzbid=%s failed; nzbget already returned a backup of key=%s", title,
                     failed.get("NZBID"), key)
            return 0
        done = [x for x in self.rpc_call(path, auth, "history", [True]) or []  # the ranking can take minutes
                if str(x.get("DupeKey", "")).lower() == key.lower() and str(x.get("Status", "")).startswith("SUCCESS")]
        if done:  # a backup nzbget returned finished while the backups were ranked
            log.info("rescue %s: pick nzbid=%s failed, but key=%s now has a successful download (nzbid=%s)", title,
                     failed.get("NZBID"), key, done[0].get("NZBID"))
            return 0
        alive = [(alive, score, bid) for bid, (score, alive, dead) in ranked.items() if not dead]
        if not alive:
            log.info("rescue %s: pick failed and no backup of key=%s is alive", title, key)
            return 0
        _, _, best = max(alive)
        if not self.rpc_call(path, auth, "editqueue", ["HistoryRedownload", "", [best]]):
            log.warning("rescue %s: could not return backup nzbid=%d to the queue", title, best)
            return 0
        log.info("rescue %s: pick nzbid=%s failed with nothing queued: returned backup nzbid=%d (alive=%s)",
                 title, failed.get("NZBID"), best, pct(ranked[best][1]))
        return best

    def set_score(self, path, auth, donor_id, score, param=None):
        """DupeScore (and a parameter) of a donor in history or, once nzbget moved it back, in the queue."""
        for kind in ("History", "Group"):  # donors normally sit in history as dupe backups
            cmd = [kind + "SetDupeScore", str(score + self.base()), [donor_id]]
            if self.rpc_call(path, auth, "editqueue", cmd):
                if param:
                    self.rpc_call(path, auth, "editqueue", [kind + "SetParameter", param, [donor_id]])
                return True
        return False

    def rerank(self, path, auth, placed, dist, stats):
        """Once every check is in: DupeScores in wholeness order (whole percent; then twin, closest size, grabs),
        strictly falling, each at most its target_score. Donors were added in the order their checks finished,
        so a wholer one can sit below a worse one until now."""
        live = sorted((p for p in placed if p.score > 1), key=lambda p: (
            -round(100 * (1.0 if p.alive is None else p.alive)), not p.twin, dist(p.v[2].total_bytes),
            -p.v[0].grabs))
        prev = 100
        for p in live:
            want = max(2, min(target_score(p.alive, p.twin), prev - 1))
            prev = want
            if want == p.score:
                continue
            if self.set_score(path, auth, p.id, want):
                log.info("reranked donor nzbid=%d %s [%s]: score %d -> %d, alive=%s", p.id, p.v[0].title,
                         p.v[0].indexer, p.score, want, pct(p.alive))
                p.score = want
            else:
                stats["rescore"] += 1

    def rescore(self, path, auth, p, health, ranks, stats):
        """After its full sample, move a fast donor's DupeScore (and DupeAlive) to its real health."""
        donor_id, old, is_twin, v = p.id, p.score, p.twin, p.v
        h = health.get(v[2].fingerprint)
        if h is None or h.alive is None:
            return
        dead = h.alive < self.cfg.donor_min_alive and h.missing >= donor_health.MIN_KNOWN
        ranks.release(old)
        new = 1 if dead else ranks.take(h.alive, is_twin)
        if not self.set_score(path, auth, donor_id, new, "DupeAlive=%d" % round(100 * h.alive)):
            ranks.release(new)
            ranks.used.add(old)
            stats["rescore"] += 1
            log.warning("could not rescore donor nzbid=%d %s [%s]: keeps score %d, alive=%s", donor_id, v[0].title,
                        v[0].indexer, old, pct(h.alive))
            return
        p.score, p.alive = new, h.alive
        if dead:
            stats["dead"] += 1
            self.state.mark_dead(v[2].fingerprint, sketch(v[2].message_ids))
        log.info("rescored donor nzbid=%d %s [%s]: score %d -> %d, alive=%s (full sample%s)", donor_id, v[0].title,
                 v[0].indexer, old, new, pct(h.alive), ", dead" if dead else "")

    def add_donor(self, key, category, path, auth, v, score, health, how, stats):
        """Append one donor (or log it in dry run); returns its NZBID, -1 in dry run, 0 if not added."""
        r, data, ci = v
        h = health.get(ci.fingerprint)
        desc = "score=%d %s [%s, %d files, %d bytes, grabs=%d, alive=%s] (%s)" % (
            score + self.base(), r.title, r.indexer, ci.files, ci.total_bytes, r.grabs, pct(h.alive if h else None), how)
        if self.cfg.dry_run:
            log.info("DRY-RUN would add donor %s", desc)
            return -1
        with self.state.lock:
            if self.state.sent(key, ci.fingerprint):
                stats["already-sent"] += 1
                return 0
            name = r.title if r.title.lower().endswith(".nzb") else r.title + ".nzb"
            pp = [{"Name": "DupeAlive", "Value": str(round(100 * h.alive))}] if h and h.alive is not None else []
            params = [name, base64.b64encode(data).decode(), category, 0, False, False, key, score + self.base(),
                      "SCORE", pp]
            donor_id = self.rpc(path, auth, "append", params)
            if donor_id <= 0:
                stats["append"] += 1
                return 0
            self.state.record(key, ci.fingerprint, donor_id)
        log.info("added donor nzbid=%d %s", donor_id, desc)
        return donor_id

    def base(self):
        return getattr(self.ctx, "base", 0)

    def watch_auth(self):
        if not self.cfg.nzbget_username:
            return ""
        login = "%s:%s" % (self.cfg.nzbget_username, self.cfg.nzbget_password)
        return "Basic " + base64.b64encode(login.encode()).decode()

    def watch_once(self):
        """One look at nzbget's queue: each new pick (the top DupeScore of its DupeKey, not yet past download,
        not appended by this proxy) gets a donor discovery, as if it had come through the proxy. Backups and
        nzbget's failover promotions rank below their pick and are left alone."""
        path, auth, now = "/jsonrpc", self.watch_auth(), time.time()
        queue = self.rpc_call(path, auth, "listgroups", [0]) or []
        history = None
        for g in queue:
            nzbid = _int(g.get("NZBID"))
            if g.get("Status") not in WATCH_STATUSES or nzbid <= 0 or self.watched.get(nzbid) == g.get("NZBName"):
                continue
            if now - self.first_seen.setdefault(nzbid, now) < self.cfg.watch_settle:
                continue
            self.watched[nzbid] = g.get("NZBName")
            if g.get("DupeKey") and history is None:
                history = self.rpc_call(path, auth, "history", [True]) or []
            job = self.pick_job(path, auth, g, queue, history or [])
            if job is None:
                continue
            t = threading.Thread(target=self._discover_safe, daemon=True, args=job)
            with self.workers_lock:
                self.workers = [w for w in self.workers if w.is_alive()] + [t]
            t.start()

    def pick_job(self, path, auth, g, queue, history):
        """discover() arguments for queue item `g` if it is a new pick (the top DupeScore of its DupeKey, not
        appended by this proxy), after giving it a DupeKey and primary_score when it lacks them; else None.
        Backups and nzbget's failover promotions rank below their pick and are left alone."""
        nzbid = _int(g.get("NZBID"))
        key, score, title = g.get("DupeKey") or "", _int(g.get("DupeScore")), g.get("NZBName") or ""
        if key and fleet_ranked(key, [g] + queue + history):  # nzbget's appendfleet already ranked this key
            log.info("watch: nzbid=%d %s: key=%s was ranked by nzbget (DupeFleet set): leaving it to nzbget", nzbid,
                     title, key)
            return None
        above = [x for x in queue + history if x is not g and _int(x.get("NZBID")) != nzbid
                 and x.get("Status") != "DELETED/COPY"  # a copy nzbget skipped is not a pick
                 and str(x.get("DupeKey", "")).lower() == key.lower() and _int(x.get("DupeScore")) > score]
        if key and above:
            log.info("watch: nzbid=%d %s is a backup (nzbid=%s scores higher under its key): left alone", nzbid,
                     title, above[0].get("NZBID"))
            return None  # a backup or promoted duplicate: its pick was (or is) handled
        info, ambiguous = self.queued_nzb(path, auth, g)
        if info is None:
            return None
        if nzbid in self.state.nzbids_for(info.fingerprint):
            if self.renamed(key, title):
                log.info("watch: nzbid=%d %s was searched as %s: searching again under its new name", nzbid, title, key)
                key = "dupes:" + normalize_title(title)
                self.rpc_call(path, auth, "editqueue", ["GroupSetDupeKey", key, [nzbid]])
            elif not self.state.search_unfinished(nzbid):
                log.info("watch: nzbid=%d %s was handled already (appended or searched by this proxy)", nzbid, title)
                return None  # the same queue item again; a re-submission of the same NZB gets a new NZBID
            else:
                log.info("watch: nzbid=%d %s: its search never finished (the worker was killed): searching again",
                         nzbid, title)
        if not key or score < self.cfg.primary_score:  # managed here from now on: lift it to primary_score
            key = key or "dupes:" + normalize_title(title)
            for cmd, arg in (("GroupSetDupeKey", key), ("GroupSetDupeScore", str(self.cfg.primary_score)), ("GroupSetDupeMode", "SCORE")):
                self.rpc_call(path, auth, "editqueue", [cmd, arg, [nzbid]])
            score = self.cfg.primary_score
        elif str(g.get("DupeMode") or "SCORE").upper() != "SCORE":  # FORCE/ALL turn off nzbget's failover
            self.rpc_call(path, auth, "editqueue", ["GroupSetDupeMode", "SCORE", [nzbid]])
            log.info("watch: nzbid=%d %s had DupeMode %s: set to SCORE, so it can fail over", nzbid, title,
                     g.get("DupeMode"))
        with self.state.lock:
            self.state.record(key, info.fingerprint, nzbid, title, touch=True)
            self.state.begin_search(key, nzbid)
        log.info("watch: new pick nzbid=%d %s (key=%s score=%d): discovering donors%s", nzbid, title, key, score,
                 "; its NZB is one of several postings of the same size, so it is never demoted" if ambiguous
                 else "")
        return (key, title, info, g.get("Category") or "", path, auth, -nzbid if ambiguous else nzbid, time.time(),
                score_base(score))

    def renamed(self, key, title):
        """True when a pick this proxy keyed from its name ("dupes:...") now shows a name its key wasn't made
        from: renamed in nzbget, or a name the old normalization got wrong (a par2 subject's "volNN-NN")."""
        g = self.state.data.get(key)
        if not key.startswith("dupes:") or not isinstance(g, dict) or not g.get("title"):
            return False
        return key != "dupes:" + normalize_title(g["title"]) or not same_release(g["title"], title)

    def queued_nzb(self, path, auth, g):
        """(NzbInfo, ambiguous) of a queue item from the copy nzbget keeps in NzbDir (name[.N].queued: the one
        whose size matches), or (None, False). Ambiguous: another posting of exactly that size shares the name
        (byte-identical reposts), so which one is the item's own can't be told."""
        opts = {e.get("Name"): str(e.get("Value", "")) for e in self.rpc_call(path, auth, "config", []) or []}
        nzbdir = opts.get("NzbDir", "").replace("${MainDir}", opts.get("MainDir", ""))
        size = (_int(g.get("FileSizeHi")) << 32) + _int(g.get("FileSizeLo"))
        infos = []
        names = [n for n in (g.get("NZBFilename"), (g.get("NZBName") or "") + ".nzb") if n and n != ".nzb"]
        # nzbget stores the copy under a sanitized name ('/' and quotes become '_'), which NZBName shows
        found = sorted({f for n in names for f in glob.glob(glob.escape(os.path.join(nzbdir, n)) + "*.queued")})
        for f in found:
            try:
                with open(f, "rb") as fh:
                    infos.append(parse_nzb(fh.read()))
            except (OSError, ValueError):
                continue
        if not infos:
            log.info("watch: no readable NZB for nzbid=%s %s in %s", g.get("NZBID"), g.get("NZBName"), nzbdir)
            return None, False
        best = min(infos, key=lambda i: abs(i.total_bytes - size))
        rivals = {i.fingerprint for i in infos if i.total_bytes == best.total_bytes}
        return best, len(rivals) > 1

    def watch_forever(self):
        while True:
            try:
                self.watch_once()
            except Exception:
                log.exception("watch: queue poll failed")
            time.sleep(self.cfg.watch_interval)

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

    def start(self, host=None):
        host = self.cfg.listen_host if host is None else host
        handler = type("BoundHandler", (Handler,), {"proxy": self})
        self.server = ThreadingHTTPServer((host, self.cfg.listen_port), handler)  # daemon_threads by default
        self.url = "http://%s:%d" % (host, self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, args=(0.1,), daemon=True).start()
        if self.cfg.watch_nzbget and self.cfg.enabled:
            threading.Thread(target=self.watch_forever, daemon=True).start()
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
    log.info("listening on :%d -> %s (enabled=%s dry_run=%s hydra=%s max_donors=%s watch_nzbget=%s)", port,
             mask(cfg.nzbget_url), cfg.enabled, cfg.dry_run, mask(cfg.hydra_url), cfg.max_donors if cfg.max_donors > 0
             else "unlimited", cfg.watch_nzbget)
    threading.Event().wait()


if __name__ == "__main__":
    main()
