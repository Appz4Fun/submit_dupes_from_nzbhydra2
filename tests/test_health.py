import base64
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
    s = dh.servers_from_nzbget_config(entries, connections=8, timeout=7)
    assert [(x.host, x.port, x.ssl, x.username, x.password, x.max_connections, x.timeout) for x in s] == [
        ("news.a", 563, True, "u1", "secret", 8, 7), ("news.c", 119, False, None, None, 1, 7)]
    assert dh.servers_from_nzbget_config(entries[:7], connections=50)[0].max_connections == 15  # half of 30
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
    assert (dead.present, dead.missing) == (0, 10) and dead.alive == 0.0     # probe only: dead after 10


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


def test_check_many_probes_dead_nzbs_before_full_sample():
    alive = ["a%d@x" % i for i in range(2000)]
    dead = ["d%d@x" % i for i in range(2000)]
    s1, s2 = FakeNntp(alive), FakeNntp()
    servers = dh.servers_from_nzbget_config(s1.config(1) + s2.config(2))
    res = dh.check_many(servers, {"alive": alive, "dead": dead}, percent=2.0, probe=10)
    assert (res["alive"].checked, res["alive"].present) == (40, 40)              # full 2% sample
    assert (res["dead"].checked, res["dead"].missing) == (10, 10)                # stopped after the probe
    assert sum(m.startswith("d") for m in s1.stats + s2.stats) == 20             # 10 ids x 2 servers


def test_check_many_budget_leaves_unknown():
    import time
    slow = FakeNntp()
    slow.delay = 0.3
    servers = dh.servers_from_nzbget_config(slow.config(1), connections=1)
    t0 = time.time()
    res = dh.check_many(servers, {"x": ["x%d@x" % i for i in range(50)]}, percent=100, probe=50, budget=1.0)
    assert time.time() - t0 < 3
    assert res["x"].alive is None or res["x"].present + res["x"].missing < 50


def test_alive_counts_errors_as_not_present():
    # a server erroring (e.g. transient 451) must not hide dead articles: present share of all answers
    assert dh.Health(300, 0, 1, 299).alive == 0.0
    assert dh.Health(300, 150, 0, 150).alive == 0.5
    assert dh.Health(300, 10, 0, 0).alive == 1.0      # 290 unanswered (budget): judged on the 10 answers
    assert dh.Health(300, 0, 2, 2).alive is None      # too few answers


def test_erroring_server_does_not_make_dead_nzb_look_alive():
    dead = ["d%d@x" % i for i in range(100)]
    ok, broken = FakeNntp(), FakeNntp(password="right")
    entries = [e if e["Name"] != "Server2.Password" else {"Name": e["Name"], "Value": "wrong"}  # auth fails:
               for e in ok.config(1) + broken.config(2)]                                       # every STAT errors
    servers = dh.servers_from_nzbget_config(entries)
    h = dh.check_many(servers, {"d": dead}, percent=100, probe=10)["d"]
    assert h.present == 0 and h.alive == 0.0


def test_check_many_probe_only():
    ids = ["a%d@x" % i for i in range(2000)]
    servers = dh.servers_from_nzbget_config(FakeNntp(ids).config(1))
    assert dh.check_many(servers, {"k": ids}, percent=2.0, probe=10, full=False)["k"].checked == 10


def _donor_appends(nzbget):
    return [a for a in nzbget.appends[1:]]


def test_fast_donors_first_then_checked_one_by_one(make_proxy, nzbget, hydra, caplog):
    caplog.set_level(logging.INFO)
    prim = release(TITLE, prefix="p")
    donors = [release(TITLE, prefix="d%d" % i) for i in range(4)]
    news = FakeNntp([m for d in donors + [prim] for m in article_ids(d)])
    nzbget.config_entries = news.config(1)
    for i, d in enumerate(donors):
        hydra.add(TITLE, d, grabs=10 - i)
    p = make_proxy(fast_donors=2)
    post(p.url + "/jsonrpc", append_body(prim, title=TITLE), auth=("admin", "pw"))
    p.wait_idle(30)
    added = [r.getMessage() for r in caplog.records if r.getMessage().startswith("added donor")]
    assert len(added) == 4
    assert [m.split("(")[-1] for m in added] == ["fast)", "fast)", "checked)", "checked)"]
    assert [a["params"][7] for a in _donor_appends(nzbget)] == [90, 89, 88, 87]


def test_slow_phase_drops_mostly_dead_donor(make_proxy, nzbget, hydra, caplog):
    caplog.set_level(logging.INFO)
    prim, donor = release(TITLE, prefix="p"), release(TITLE, prefix="d")
    ids = article_ids(donor)
    news = FakeNntp(article_ids(prim) + ids[: len(ids) * 3 // 10])         # only 30% of the donor survives
    nzbget.config_entries = news.config(1)
    hydra.add(TITLE, donor)
    p = make_proxy(fast_donors=0)
    post(p.url + "/jsonrpc", append_body(prim, title=TITLE), auth=("admin", "pw"))
    p.wait_idle(30)
    assert len(nzbget.appends) == 1
    assert "'dead': 1" in " ".join(r.getMessage() for r in caplog.records)


def test_dupescore_ranks_donors_by_alive_share(make_proxy, nzbget, hydra):
    prim = release(TITLE, prefix="p")
    partial, full = release(TITLE, prefix="h", n_files=11), release(TITLE, prefix="f", n_files=12)
    pids = article_ids(partial)
    news = FakeNntp(article_ids(prim) + article_ids(full) + pids[: len(pids) * 6 // 10])   # 60% vs 100%
    nzbget.config_entries = news.config(1)
    hydra.add(TITLE, partial, grabs=99)          # closer in size and more grabs: would rank first without health
    hydra.add(TITLE, full, grabs=1)
    p = make_proxy(health_percent=100)
    post(p.url + "/jsonrpc", append_body(prim, title=TITLE), auth=("admin", "pw"))
    p.wait_idle(30)
    by_prefix = {base64.b64decode(a["params"][1]).count(b">f-") > 0: a for a in nzbget.appends[1:]}
    full_d, partial_d = by_prefix[True], by_prefix[False]
    final = nzbget.final_scores()
    assert final[full_d["id"]] == 90
    assert 56 <= final[partial_d["id"]] <= 60                   # 10 + 80 * 0.6 after the full sample
    assert final[full_d["id"]] > final[partial_d["id"]]
    assert any(p.get("Name") == "DupeAlive" for p in full_d["params"][9])
    assert any(c == "HistorySetParameter" and pr == "DupeAlive=60%" for c, pr, _ in nzbget.edits)


def test_fast_donor_found_dead_by_full_sample_is_demoted(make_proxy, nzbget, hydra):
    prim, donor = release(TITLE, prefix="p"), release(TITLE, prefix="d")
    ids = article_ids(donor)
    import donor_health as dh
    probe = set(dh.sample(ids, 100)[:10])                     # exactly the probe articles survive
    news = FakeNntp(article_ids(prim) + list(probe))
    nzbget.config_entries = news.config(1)
    hydra.add(TITLE, donor)
    p = make_proxy(health_percent=100)
    post(p.url + "/jsonrpc", append_body(prim, title=TITLE), auth=("admin", "pw"))
    p.wait_idle(30)
    (d,) = nzbget.appends[1:]
    assert d["params"][7] == 90 and nzbget.final_scores()[d["id"]] == 1


def test_dupescore_unique_and_below_primary(make_proxy, nzbget, hydra):
    for i in range(4):
        hydra.add(TITLE, release(TITLE, prefix="u%d" % i))
    p = make_proxy()
    post(p.url + "/jsonrpc", append_body(release(TITLE, prefix="p"), title=TITLE), auth=("admin", "pw"))
    p.wait_idle(30)
    assert [a["params"][7] for a in nzbget.appends[1:]] == [90, 89, 88, 87]
