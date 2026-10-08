import json

import nzbget_dupe_proxy as ndp
from tests.fakes import append_body, basic, post, release


def test_jsonrpc_forwarded_byte_exact(proxy, nzbget):
    body = b'{"id":7,"jsonrpc":"2.0","method":"status","params":[]}'
    status, _, resp = post(proxy.url + "/jsonrpc", body, auth=("admin", "pw"))
    assert status == 200
    assert resp == nzbget.last_response
    req = nzbget.requests[-1]
    assert req.path == "/jsonrpc"
    assert req.body == body
    assert req.headers["Authorization"] == basic("admin", "pw")


def test_hydra_test_connection_writelog(proxy, nzbget):
    body = b'{"id":1,"jsonrpc":"2.0","method":"writelog","params":["INFO","NZBHydra 2 connected to test connection"]}'
    status, _, resp = post(proxy.url + "/jsonrpc", body, auth=("admin", "pw"))
    assert status == 200 and json.loads(resp)["result"] is True


def test_userpass_path_and_xmlrpc_forwarded(proxy, nzbget):
    post(proxy.url + "/admin:pw/jsonrpc", b'{"method":"version","params":[]}')
    xml = b"<?xml version='1.0'?><methodCall><methodName>version</methodName></methodCall>"
    status, _, resp = post(proxy.url + "/xmlrpc", xml, headers={"Content-Type": "text/xml"})
    assert [r.path for r in nzbget.requests[-2:]] == ["/admin:pw/jsonrpc", "/xmlrpc"]
    assert nzbget.requests[-1].body == xml
    assert status == 200 and resp == nzbget.last_response


def test_upstream_401_passed_through(proxy, nzbget):
    nzbget.require_auth = ("admin", "pw")
    status, _, _ = post(proxy.url + "/jsonrpc", b'{"method":"version","params":[]}', auth=("admin", "bad"))
    assert status == 401


def test_nzbget_down_gives_502(make_proxy):
    p = make_proxy(nzbget_url="http://127.0.0.1:9")
    status, _, _ = post(p.url + "/jsonrpc", b'{"method":"version","params":[]}')
    assert status == 502


def test_disabled_is_pure_passthrough(make_proxy, nzbget, hydra):
    p = make_proxy(enabled="false")
    body = append_body(release("Show.S01E01.1080p.WEB-GRP"), title="Show.S01E01.1080p.WEB-GRP")
    status, _, resp = post(p.url + "/jsonrpc", body)
    p.wait_idle(5)
    assert status == 200 and resp == nzbget.last_response
    assert nzbget.requests[-1].body == body
    assert len(nzbget.appends) == 1
    assert hydra.queries == []


def test_append_with_non_string_dupekey_does_not_crash(proxy, nzbget):
    # a crafted append whose DupeKey param is a structured (non-string) value must not crash
    # the handler — the proxy derives its own key and still forwards the append to nzbget.
    body = append_body(nzb=release("Show.S01E01.2160p.ATVP.WEB-DL-GRP"),
                       title="Show.S01E01.2160p.ATVP.WEB-DL-GRP", dupekey=[1, 2])
    status, _, _ = post(proxy.url + "/jsonrpc", body, auth=("admin", "pw"))
    assert status == 200
    assert any(r.path.endswith("/jsonrpc") and b'"append"' in r.body for r in nzbget.requests)


def test_malformed_content_length_returns_400_not_dropped(proxy):
    # a non-numeric Content-Length must not crash do_POST (int() ValueError) and drop the
    # connection with no response; it should get a graceful 400.
    import socket as _socket
    import urllib.parse as _up
    u = _up.urlparse(proxy.url)
    s = _socket.socket(); s.settimeout(5); s.connect((u.hostname, u.port))
    s.sendall(b"POST /jsonrpc HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\nConnection: close\r\n\r\n{}")
    try:
        resp = s.recv(100)
    finally:
        s.close()
    assert resp.startswith(b"HTTP/1.1 400"), resp[:40]


def test_handler_bounds_body_read_with_a_timeout():
    # a lying Content-Length (huge value, short body) must not hang the worker thread forever:
    # the request socket needs a finite timeout so the read gives up.
    assert isinstance(ndp.Handler.timeout, (int, float)) and 0 < ndp.Handler.timeout <= 300


def test_chunked_request_without_content_length_is_handled(proxy):
    # a client using Transfer-Encoding: chunked (no Content-Length) must get a response, not a hang/crash.
    import socket as _s, urllib.parse as _up
    u = _up.urlparse(proxy.url)
    s = _s.socket(); s.settimeout(8); s.connect((u.hostname, u.port))
    s.sendall(b"POST /jsonrpc HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n2\r\n{}\r\n0\r\n\r\n")
    try:
        resp = s.recv(100)
    except _s.timeout:
        resp = b"HANG"
    finally:
        s.close()
    assert resp and resp != b"HANG" and resp.startswith(b"HTTP/"), resp[:40]


def test_oversized_header_line_is_handled(proxy):
    # a very long header line must not hang/crash the handler (stdlib caps header size).
    import socket as _s, urllib.parse as _up
    u = _up.urlparse(proxy.url)
    s = _s.socket(); s.settimeout(8); s.connect((u.hostname, u.port))
    s.sendall(b"POST /jsonrpc HTTP/1.1\r\nHost: x\r\nX-Big: " + b"A" * 70000 + b"\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
    try:
        resp = s.recv(100)
    except _s.timeout:
        resp = b"HANG"
    finally:
        s.close()
    assert resp != b"HANG", "handler hung on oversized header"


def test_oversized_content_length_rejected_before_read(proxy):
    # a body larger than the cap must be rejected on the Content-Length header (413) rather than
    # read into memory (OOM risk on a network-exposed proxy). Send only headers, no body.
    import socket as _s, urllib.parse as _up, time as _t
    u = _up.urlparse(proxy.url)
    s = _s.socket(); s.settimeout(5); s.connect((u.hostname, u.port))
    s.sendall(b"POST /jsonrpc HTTP/1.1\r\nHost: x\r\nContent-Length: 999999999999\r\nConnection: close\r\n\r\n")
    t0 = _t.time()
    try:
        resp = s.recv(100)
    except _s.timeout:
        resp = b"HANG"
    finally:
        s.close()
    assert resp.startswith(b"HTTP/1.1 413") and (_t.time() - t0) < 3, resp[:40]
