import json

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
