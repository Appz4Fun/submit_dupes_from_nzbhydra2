"""In-process fakes: a JSON-RPC nzbget and a newznab NZBHydra2, plus request/NZB builders."""
import base64
import json
import socketserver
import threading
import time
import urllib.error
import urllib.request
from email.utils import format_datetime
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from xml.sax.saxutils import escape


def serve(handler_cls, owner):
    """Start handler_cls on 127.0.0.1:<free port> in a daemon thread; handler gets .owner."""
    cls = type(handler_cls.__name__, (handler_cls,), {"owner": owner, "log_message": lambda *a: None})
    srv = type("Server", (ThreadingHTTPServer,), {"request_queue_size": 128})(("127.0.0.1", 0), cls)  # concurrent grabs
    threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True).start()
    owner.server = srv
    owner.url = "http://127.0.0.1:%d" % srv.server_address[1]
    return owner


class _NzbgetHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        o = self.owner
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        with o.lock:
            o.requests.append(SimpleNamespace(path=self.path, headers=dict(self.headers), body=body))
        if o.require_auth and self.headers.get("Authorization") != basic(*o.require_auth):
            return self._send(401, b"Unauthorized", "text/plain")
        if self.path.endswith("/xmlrpc"):
            o.last_response = b"<?xml version='1.0'?><methodResponse><params><param><value><string>27.0</string></value></param></params></methodResponse>"
            return self._send(200, o.last_response, "text/xml")
        req = json.loads(body)
        method, params = req.get("method"), req.get("params")
        if method == "append":
            with o.lock:
                o.next_id += 1
                nzbid = o.next_id
                o.appends.append({"id": nzbid, "params": params, "path": self.path,
                                  "auth": self.headers.get("Authorization"), "time": time.time()})
            result = nzbid
        elif method == "editqueue":
            with o.lock:
                o.edits.append(tuple(params))
                o.edit_times.append(time.time())
            result = o.editqueue_result
        else:
            result = {"version": "27.0", "writelog": True, "config": o.config_entries,
                      "history": o.history_items, "listgroups": o.queue_items}.get(method, [])
        o.last_response = json.dumps({"version": "1.1", "id": req.get("id"), "result": result}, indent=1).encode()
        if method == "append":
            o.appends[-1]["response"] = o.last_response
        self._send(200, o.last_response, "application/json")

    do_GET = do_POST

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeNzbget:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests, self.appends = [], []
        self.next_id = 1000
        self.require_auth = None
        self.last_response = None
        self.config_entries = []
        self.edits = []
        self.editqueue_result = True
        self.edit_times = []
        self.history_items, self.queue_items = [], []
        serve(_NzbgetHandler, self)

    def final_scores(self):
        """{nzbid: DupeScore after any (History|Group)SetDupeScore edits}."""
        scores = {a["id"]: a["params"][7] for a in self.appends}
        for cmd, param, ids in self.edits:
            if cmd.endswith("SetDupeScore"):
                scores.update((i, int(param)) for i in ids)
        return scores


class _HydraHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        o = self.owner
        if self.path.startswith("/api"):
            o.queries.append(self.path)
            time.sleep(o.delay)
            import re
            from urllib.parse import parse_qs, urlparse
            qs = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            words = qs.get("q", "").lower().split()
            norm = lambda t: " ".join(re.split(r"[\s._\-()+,\[\]]+", t.lower()))  # noqa: E731
            hits = [(i, it) for i, it in enumerate(o.items)
                    if all(w in norm(it.title).split() for w in words)]          # like an indexer: every word
            offset, limit = int(qs.get("offset", 0)), int(qs.get("limit", 100))
            items = "".join(o.item_xml(i, it) for i, it in hits[offset:offset + limit])
            body = ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" '
                    'xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"><channel>%s</channel></rss>' % items).encode()
            return self._send(200, body)
        if self.path.startswith("/getnzb/"):
            o.fetches.append(self.path)
            o.fetch_times.append(time.time())
            time.sleep(o.fetch_delay)
            it = o.items[int(self.path.split("/")[2].split("?")[0])]
            if it.flaky > 0:                        # transient indexer hiccup: error body with HTTP 200
                it.flaky -= 1
                return self._send(200, b'<?xml version="1.0"?><error code="429" description="Request limit reached"/>')
            if it.status != 200:
                return self._send(it.status, b"nope")
            return self._send(200, it.nzb)
        self._send(404, b"")

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeHydra:
    def __init__(self):
        self.items, self.queries, self.fetches = [], [], []
        self.delay = self.fetch_delay = 0.0
        self.fetch_times = []
        serve(_HydraHandler, self)

    def add(self, title, nzb, size=None, grabs=0, age_days=1, status=200, flaky=0, posted=None, indexer=None):
        """posted: the listing's usenetdate (epoch); by default 10 min before the previous item's, as distinct
        postings are. Several items with the same size and `posted` are one posting listed by several indexers."""
        info_size = size if size is not None else nzb_total(nzb)
        if posted is None:
            posted = time.time() - age_days * 86400 - 600 * len(self.items)
        self.items.append(SimpleNamespace(title=title, nzb=nzb, size=info_size, grabs=grabs, posted=posted,
                                          status=status, flaky=flaky, indexer=indexer or "idx%d" % len(self.items)))

    def item_xml(self, i, it):
        link = "%s/getnzb/%d?apikey=KEY" % (self.url, i)
        date = format_datetime(datetime.fromtimestamp(it.posted, timezone.utc))
        attrs = {"size": it.size, "grabs": it.grabs, "usenetdate": date, "guid": "g%d" % i, "hydraIndexerName": it.indexer}
        a = "".join('<newznab:attr name="%s" value="%s"/>' % (k, escape(str(v))) for k, v in attrs.items())
        return ("<item><title>%s</title><link>%s</link><guid>g%d</guid><size>%d</size>%s</item>"
                % (escape(it.title), escape(link), i, it.size, a))


def make_nzb(files, prefix="a", poster="poster@example.com", meta=None):
    """files: [(filename, [segment_bytes, ...])]. Message-IDs are <prefix-file-seg@x>."""
    head = "".join('<meta type="%s">%s</meta>' % (k, v) for k, v in (meta or {}).items())
    out = ['<?xml version="1.0" encoding="UTF-8"?>\n<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">',
           "<head>%s</head>" % head if head else ""]
    for fi, (name, segs) in enumerate(files):
        out.append('<file poster="%s" date="1" subject="[%d/%d] - &quot;%s&quot; yEnc (1/%d)"><groups><group>a.b.x</group></groups><segments>'
                   % (poster, fi + 1, len(files), escape(name), len(segs)))
        out += ['<segment bytes="%d" number="%d">%s-%d-%d@x</segment>' % (b, n + 1, prefix, fi, n) for n, b in enumerate(segs)]
        out.append("</segments></file>")
    out.append("</nzb>")
    return "".join(out).encode()


def nzb_total(nzb):
    import re
    return sum(int(b) for b in re.findall(rb'bytes="(\d+)"', nzb))


def release(name, n_files=10, seg=700000, segs_per_file=20, prefix="a", obfuscate=False, extra_bytes=0):
    """A typical posting: n_files rar volumes of equal size (+extra_bytes on the last segment)."""
    files = []
    for i in range(n_files):
        fname = ("%s%02d.bin" % (prefix * 8, i)) if obfuscate else "%s.part%02d.rar" % (name, i + 1)
        files.append((fname, [seg] * segs_per_file))
    files[-1][1][-1] += extra_bytes
    return make_nzb(files, prefix=prefix)


def basic(user, pw):
    return "Basic " + base64.b64encode(("%s:%s" % (user, pw)).encode()).decode()


def post(url, body, auth=None, headers=None):
    h = {"Content-Type": "application/json", "User-Agent": "NZBHydra2"}
    if auth:
        h["Authorization"] = basic(*auth)
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def append_body(nzb=b"<nzb/>", title="Some.Release", category="", dupekey="", score=0, rid=1):
    """Exactly what Hydra 9.0.4's jsonrpc4j client sends for addContent()."""
    params = [title + ".nzb", base64.b64encode(nzb).decode(), category, 0, False, False, dupekey, score, "SCORE", []]
    return json.dumps({"id": rid, "jsonrpc": "2.0", "method": "append", "params": params}).encode()


YENC_DATA = b"a" * 64  # (97 + 42) % 256 needs no yEnc escaping


def _yenc_body():
    import binascii
    enc = bytes((c + 42) % 256 for c in YENC_DATA)
    return (b"=ybegin line=128 size=%d name=x.bin\r\n" % len(YENC_DATA) + enc + b"\r\n" +
            b"=yend size=%d crc32=%08x\r\n.\r\n" % (len(YENC_DATA), binascii.crc32(YENC_DATA) & 0xFFFFFFFF))


class _NntpHandler(socketserver.StreamRequestHandler):
    def handle(self):
        o = self.server.owner
        with o.lock:
            o.active += 1
            o.sessions += 1
            o.max_active = max(o.max_active, o.active)
        try:
            self._session(o)
        finally:
            with o.lock:
                o.active -= 1

    def _lines(self, o):
        """Command lines as they arrive; counts the ones that came with others (sent before any reply)."""
        buf = b""
        while True:
            chunk = self.connection.recv(65536)
            if not chunk:
                return
            buf += chunk
            *lines, buf = buf.split(b"\n")
            with o.lock:
                o.pipelined += max(0, len(lines) - 1)
            yield from lines

    def _session(self, o):
        self.wfile.write(b"200 fake news\r\n")
        for raw in self._lines(o):
            cmd = raw.decode().strip()
            if cmd.startswith("AUTHINFO USER"):
                self.wfile.write(b"381 more\r\n")
            elif cmd.startswith("AUTHINFO PASS"):
                self.wfile.write(b"281 ok\r\n" if cmd == "AUTHINFO PASS " + o.password else b"481 denied\r\n")
            elif cmd.startswith(("STAT ", "BODY ")):
                verb, mid = cmd[:4], cmd[5:].strip("<>")
                time.sleep(o.delay)
                with o.lock:
                    (o.stats if verb == "STAT" else o.bodies).append(mid)
                if verb == "BODY" and mid in o.drop_on_body:                 # connection dies mid-download
                    o.drop_on_body.discard(mid)
                    return
                has = mid in o.articles and not (verb == "BODY" and mid in o.soft_dead)
                if verb == "STAT":
                    self.wfile.write(("223 0 <%s>\r\n" % mid if has else "%d no such article\r\n" % o.missing_code).encode())
                else:
                    self.wfile.write(b"222 0 body\r\n" + _yenc_body() if has else b"%d no such article\r\n" % o.missing_code)
            elif cmd == "QUIT":
                self.wfile.write(b"205 bye\r\n")
                return
            else:
                self.wfile.write(b"500 unknown\r\n")


class FakeNntp:
    """Plain-TCP NNTP server answering STAT/BODY from a set of message-ids (without <>).

    soft_dead: ids whose STAT says 223 but whose BODY is gone (430). missing_code: the answer for a missing
    article (some providers say 451, not 430). Tracks sessions and concurrent connections."""

    def __init__(self, articles=(), password="pw", soft_dead=(), missing_code=430, drop_on_body=()):
        self.articles, self.password, self.soft_dead = set(articles), password, set(soft_dead)
        self.missing_code, self.sessions, self.pipelined = missing_code, 0, 0
        self.drop_on_body = set(drop_on_body)  # the first BODY of these ids closes the connection
        self.stats, self.bodies, self.delay = [], [], 0.0
        self.lock, self.active, self.max_active = threading.Lock(), 0, 0
        srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), type("H", (_NntpHandler,), {}))
        srv.daemon_threads, srv.owner, self.server = True, self, srv
        threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True).start()
        self.port = srv.server_address[1]

    def config(self, n, active="yes", connections=8):
        """nzbget `config` entries describing this server as ServerN."""
        return [{"Name": "Server%d.%s" % (n, k), "Value": v} for k, v in
                (("Active", active), ("Host", "127.0.0.1"), ("Port", str(self.port)), ("Username", "u"),
                 ("Password", self.password), ("Encryption", "no"), ("Connections", str(connections)))]


def article_ids(nzb):
    import re
    return [m.decode() for m in re.findall(rb">([^<>]+@x)</segment>", nzb)]
