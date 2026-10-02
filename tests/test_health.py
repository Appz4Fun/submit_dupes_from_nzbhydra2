import logging

import donor_health as dh
from tests.fakes import FakeNntp, append_body, article_ids, post, release

TITLE = "Show.S01E01.1080p.WEB.H264-GRP"


def test_servers_from_nzbget_config():
    entries = [{"Name": n, "Value": v} for n, v in [
        ("Server1.Active", "yes"), ("Server1.Host", "news.a"), ("Server1.Port", "563"), ("Server1.Encryption", "yes"),
        ("Server1.Username", "u1"), ("Server1.Password", "secret"), ("Server1.Connections", "30"),
        ("Server2.Active", "no"), ("Server2.Host", "news.b"), ("Server2.Port", "119"),
        ("Server3.Active", "yes"), ("Server3.Host", "news.c"), ("Server3.Port", "119"), ("Server3.Encryption", "no"),
        ("Server3.Username", ""), ("Server3.Password", ""), ("Server3.Connections", "1"),
        ("Server4.Active", "yes"), ("Server4.Host", ""), ("ControlPassword", "x")]]
    s = dh.servers_from_nzbget_config(entries, connections=2, timeout=7)
    assert [(x.host, x.port, x.ssl, x.username, x.password, x.max_connections, x.timeout) for x in s] == [
        ("news.a", 563, True, "u1", "secret", 2, 7), ("news.c", 119, False, None, None, 1, 7)]
    assert "secret" not in repr(s)


def test_sample_size_bounds():
    assert len(dh.sample([str(i) for i in range(100)], 2.0)) == 20            # minimum
    assert len(dh.sample([str(i) for i in range(5000)], 2.0)) == 100          # 2%
    assert len(dh.sample([str(i) for i in range(100000)], 2.0)) == 300        # maximum


def test_availability_falls_back_across_all_servers():
    ids = ["a%d@x" % i for i in range(40)]
    s1, s2 = FakeNntp(ids[:20]), FakeNntp(ids[20:])
    servers = dh.servers_from_nzbget_config(s1.config(1) + s2.config(2))
    h = dh.availability(servers, ids, percent=100)
    assert (h.checked, h.present, h.missing) == (40, 40, 0) and h.alive == 1.0
    dead = dh.availability(servers, ["zz%d@x" % i for i in range(30)], percent=100)
    assert (dead.present, dead.missing) == (0, 30) and dead.alive == 0.0


def test_dead_donor_dropped_alive_donor_kept(make_proxy, nzbget, hydra, caplog):
    caplog.set_level(logging.INFO)
    alive, dead, prim = release(TITLE, prefix="r"), release(TITLE, prefix="d"), release(TITLE, prefix="p")
    news = FakeNntp(article_ids(alive) + article_ids(prim))
    nzbget.config_entries = news.config(1)
    hydra.add(TITLE, dead, grabs=100)
    hydra.add(TITLE, alive, grabs=1)
    p = make_proxy()
    post(p.url + "/jsonrpc", append_body(prim, title=TITLE), auth=("admin", "pw"))
    p.wait_idle(20)
    added = [a["params"][1] for a in nzbget.appends[1:]]
    assert len(added) == 1
    import base64
    assert base64.b64decode(added[0]) == alive
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "'dead': 1" in msgs and "primary alive=100%" in msgs
    assert "pw" not in msgs.replace("nzbget-dupe-proxy", "")


def test_health_check_skipped_without_servers(make_proxy, nzbget, hydra, caplog):
    caplog.set_level(logging.INFO)
    hydra.add(TITLE, release(TITLE, prefix="r"))
    p = make_proxy()
    post(p.url + "/jsonrpc", append_body(release(TITLE, prefix="p"), title=TITLE), auth=("admin", "pw"))
    p.wait_idle(20)
    assert len(nzbget.appends) == 2
    assert "health check skipped" in " ".join(r.getMessage() for r in caplog.records)
