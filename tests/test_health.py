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
    s = dh.servers_from_nzbget_config(entries, max_conns=8, timeout=7)
    assert [(x.host, x.port, x.ssl, x.username, x.password, x.max_connections, x.timeout) for x in s] == [
        ("news.a", 563, True, "u1", "secret", 8, 7), ("news.c", 119, False, None, None, 1, 7)]
    assert dh.servers_from_nzbget_config(entries[:7], max_conns=50)[0].max_connections == 15  # half of nzbget's 30
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
    servers = dh.servers_from_nzbget_config(slow.config(1), max_conns=1)
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
    assert final[full_d["id"]] == 89                            # other packaging: 9 + 80 * alive
    assert 56 <= final[partial_d["id"]] <= 58                   # 9 + 80 * 0.6 after the full sample
    assert final[full_d["id"]] > final[partial_d["id"]]
    assert {"Name": "DupeAlive", "Value": "100"} in full_d["params"][9]
    assert any(c == "HistorySetParameter" and pr == "DupeAlive=60" for c, pr, _ in nzbget.edits)


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


def _setup(nzbget, hydra, alive_ids, donors, prim=None):
    prim = prim or release(TITLE, prefix="p")
    news = FakeNntp(article_ids(prim) + list(alive_ids))
    nzbget.config_entries = news.config(1)
    for d in donors:
        hydra.add(TITLE, d)
    return prim


def test_probe_only_drops_donors_with_nothing_found(make_proxy, nzbget, hydra):
    donor = release(TITLE, prefix="d")
    full = dh.sample(article_ids(donor), 2.0)                      # 20 ids; the probe is full[:10]
    prim = _setup(nzbget, hydra, full[:4] + full[10:], [donor])    # probe 4/10, full sample 14/20 = 70%
    p = make_proxy()
    post(p.url + "/jsonrpc", append_body(prim, title=TITLE), auth=("admin", "pw"))
    p.wait_idle(30)
    assert len(nzbget.appends) == 2


def test_bad_server_config_skips_health_but_keeps_donors(make_proxy, nzbget, hydra, caplog):
    caplog.set_level(logging.INFO)
    prim = _setup(nzbget, hydra, [], [release(TITLE, prefix="d")])
    nzbget.config_entries = [{"Name": "Server1.Host", "Value": "h"}, {"Name": "Server1.Port", "Value": "abc"}]
    p = make_proxy()
    post(p.url + "/jsonrpc", append_body(prim, title=TITLE), auth=("admin", "pw"))
    p.wait_idle(30)
    assert len(nzbget.appends) == 2
    assert "donor discovery crashed" not in " ".join(r.getMessage() for r in caplog.records)


def test_failed_rescore_is_reported_not_claimed(make_proxy, nzbget, hydra, caplog):
    caplog.set_level(logging.INFO)
    donor = release(TITLE, prefix="d")
    probe = set(dh.sample(article_ids(donor), 100)[:10])
    prim = _setup(nzbget, hydra, probe, [donor])
    nzbget.editqueue_result = False
    p = make_proxy(health_percent=100)
    post(p.url + "/jsonrpc", append_body(prim, title=TITLE), auth=("admin", "pw"))
    p.wait_idle(30)
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "rescored donor" not in msgs and "could not rescore donor" in msgs


def test_capped_run_keeps_fetching_past_dead_donors(make_proxy, nzbget, hydra):
    dead = [release(TITLE, prefix="x%d" % i, extra_bytes=i) for i in range(5)]   # closest in size: fetched first
    good = release(TITLE, prefix="g", n_files=11)
    prim = _setup(nzbget, hydra, article_ids(good), dead + [good])
    p = make_proxy(max_donors=2)
    post(p.url + "/jsonrpc", append_body(prim, title=TITLE), auth=("admin", "pw"))
    p.wait_idle(30)
    assert [base64.b64decode(a["params"][1]) for a in nzbget.appends[1:]] == [good]


# ---- parallel fan-out engine -------------------------------------------------------------------


def test_fanout_does_not_wait_for_slow_server():
    import time
    ids = ["f%d@x" % i for i in range(20)]
    fast, slow = FakeNntp(ids), FakeNntp(ids)
    slow.delay = 0.5                                                   # 20 STATs alone would take 10 s
    servers = dh.servers_from_nzbget_config(fast.config(1) + slow.config(2))
    t0 = time.time()
    h = dh.check_many(servers, {"n": ids}, percent=100, probe=20, body_percent=0)["n"]
    assert (h.present, h.answered) == (20, 20)
    assert time.time() - t0 < 3
    assert len(slow.stats) < 20                                        # it skipped articles already found


def test_missing_needs_every_server():
    ids = ["m%d@x" % i for i in range(10)]
    a, b = FakeNntp(), FakeNntp()
    b.delay = 0.05
    servers = dh.servers_from_nzbget_config(a.config(1) + b.config(2))
    h = dh.check_many(servers, {"n": ids}, percent=100, probe=10, body_percent=0)["n"]
    assert h.missing == 10
    assert sorted(a.stats) == sorted(b.stats) == sorted(ids)


def test_body_checks_catch_soft_dead_articles():
    ids = ["s%d@x" % i for i in range(30)]
    srv = FakeNntp(ids, soft_dead=ids)                                  # STAT says yes, BODY is gone
    servers = dh.servers_from_nzbget_config(srv.config(1))
    stat_only = dh.check_many(servers, {"n": ids}, percent=100, probe=30, body_percent=0)["n"]
    assert stat_only.alive == 1.0
    with_body = dh.check_many(servers, {"n": ids}, percent=100, probe=30, body_percent=100, max_body=100)["n"]
    assert with_body.alive == 0.0 and with_body.body_checked == 30 and with_body.body_bad == 30


def test_body_check_runs_on_every_server_and_needs_valid_data():
    ids = ["b%d@x" % i for i in range(20)]
    good, soft = FakeNntp(ids), FakeNntp(ids, soft_dead=ids)
    servers = dh.servers_from_nzbget_config(soft.config(1) + good.config(2))
    h = dh.check_many(servers, {"n": ids}, percent=100, probe=20, body_percent=100, max_body=100)["n"]
    assert h.alive == 1.0                                                # the server with real data counts
    assert set(good.bodies) == set(ids)


def test_plan_marks_about_a_fifth_for_body():
    ids = ["p%d@x" % i for i in range(5000)]
    plan = dh.plan(ids, percent=10, body_percent=20, maximum=500, max_body=10 ** 6)
    assert len(plan) == 500
    assert 70 <= sum(b for _, b in plan) <= 130


def test_connection_caps_hold_with_many_nzbs():
    groups = {k: ["%d-%d@x" % (k, i) for i in range(20)] for k in range(30)}
    srv = FakeNntp([m for ids in groups.values() for m in ids])
    srv.delay = 0.01
    servers = dh.servers_from_nzbget_config(srv.config(1, connections=50), max_conns=4)
    res = dh.check_many(servers, groups, percent=100, probe=20, body_percent=0,
                        limits=dh.Limits(nzbs=10, per_nzb=1))
    assert all(h.present == 20 for h in res.values())
    assert 2 <= srv.max_active <= 4


def test_check_iter_streams_one_result_per_nzb():
    groups = {k: ["%d-%d@x" % (k, i) for i in range(10)] for k in range(5)}
    srv = FakeNntp([m for ids in groups.values() for m in ids])
    servers = dh.servers_from_nzbget_config(srv.config(1))
    seen = [k for k, _ in dh.check_iter(servers, groups, percent=100, probe=10, body_percent=0)]
    assert sorted(seen) == list(range(5))


def test_parallel_check_env_vars():
    import nzbget_dupe_proxy as ndp
    c = ndp.Config.from_env({})
    assert (c.nzbs_to_check_concurrently, c.nntp_server_connection_per_nzb, c.max_conns_per_nntp_server,
            c.body_percent) == (10, 1, 20, 20.0)
    c = ndp.Config.from_env({"NZBS_TO_CHECK_CONCURRENTLY": "4", "NNTP_SERVER_CONNECTION_PER_NZB": "2",
                             "MAX_CONNS_PER_NNTP_SERVER": "6", "BODY_PERCENT": "0"})
    assert (c.nzbs_to_check_concurrently, c.nntp_server_connection_per_nzb, c.max_conns_per_nntp_server,
            c.body_percent) == (4, 2, 6, 0.0)


def test_proxy_health_respects_connection_cap(make_proxy, nzbget, hydra):
    prim = release(TITLE, prefix="p")
    donors = [release(TITLE, prefix="c%d" % i) for i in range(8)]
    news = FakeNntp([m for d in donors + [prim] for m in article_ids(d)])
    news.delay = 0.01
    nzbget.config_entries = news.config(1, connections=50)
    for d in donors:
        hydra.add(TITLE, d)
    p = make_proxy(max_conns_per_nntp_server=3, nzbs_to_check_concurrently=10)
    post(p.url + "/jsonrpc", append_body(prim, title=TITLE), auth=("admin", "pw"))
    p.wait_idle(60)
    assert len(nzbget.appends) == 9
    assert news.max_active <= 3



def test_each_nzb_gets_its_own_budget():
    groups = {k: ["%d-%d@x" % (k, i) for i in range(5)] for k in range(3)}
    srv = FakeNntp([m for ids in groups.values() for m in ids])
    srv.delay = 0.1                                                   # one NZB takes ~0.5 s with 1 connection
    servers = dh.servers_from_nzbget_config(srv.config(1, connections=2), max_conns=1)
    res = dh.check_many(servers, groups, percent=100, probe=5, body_percent=0, budget=1.5,
                        limits=dh.Limits(nzbs=1, per_nzb=1))
    assert all(h.present == 5 for h in res.values())                  # the third NZB is not starved


def test_body_checks_capped_per_nzb():
    plan = dh.plan(["p%d@x" % i for i in range(1000)], percent=30, body_percent=100, maximum=300, max_body=5)
    assert len(plan) == 300 and sum(b for _, b in plan) == 5


def test_server_retried_after_give_up(monkeypatch):
    ids = ["r%d@x" % i for i in range(12)]
    srv = FakeNntp(ids)
    servers = dh.servers_from_nzbget_config(srv.config(1), max_conns=1)
    calls = {"n": 0}
    real = dh._Pool._ask

    async def flaky(conn, mid, body):
        calls["n"] += 1
        if calls["n"] <= dh.SERVER_GIVE_UP:
            raise OSError("451 try later")
        return await real(conn, mid, body)
    monkeypatch.setattr(dh._Pool, "_ask", staticmethod(flaky))
    monkeypatch.setattr(dh, "SERVER_RETRY_AFTER", 0.05)
    h = dh.check_many(servers, {"n": ids}, percent=100, probe=12, body_percent=0)["n"]
    assert h.present >= len(ids) - dh.SERVER_GIVE_UP - 1               # the server came back


def test_health_failure_midway_still_adds_remaining_donors(make_proxy, nzbget, hydra, monkeypatch):
    prim = release(TITLE, prefix="p")
    donors = [release(TITLE, prefix="k%d" % i) for i in range(4)]
    news = FakeNntp([m for d in donors + [prim] for m in article_ids(d)])
    nzbget.config_entries = news.config(1)
    for d in donors:
        hydra.add(TITLE, d)
    real = dh.check_iter

    def breaks(servers, groups, *a, **kw):
        if not kw.get("full", True):
            yield from real(servers, groups, *a, **kw)
            return
        yield next(iter(real(servers, groups, *a, **kw)))
        raise RuntimeError("checker died")
    monkeypatch.setattr(dh, "check_iter", breaks)
    p = make_proxy(fast_donors=1)
    post(p.url + "/jsonrpc", append_body(prim, title=TITLE), auth=("admin", "pw"))
    p.wait_idle(30)
    assert len(nzbget.appends) == 5                                    # primary + all 4 donors


def test_dead_posting_is_remembered_across_grabs(make_proxy, nzbget, hydra, caplog, tmp_path, monkeypatch):
    import nzbget_dupe_proxy as ndp
    monkeypatch.setattr(ndp, "GROUP_WINDOW", 0)                       # the second grab comes much later
    caplog.set_level(logging.INFO)
    prim, dead = release(TITLE, prefix="p"), release(TITLE, prefix="d")
    news = FakeNntp(article_ids(prim))
    nzbget.config_entries = news.config(1)
    hydra.add(TITLE, dead)
    p = make_proxy(state_dir=str(tmp_path / "s"))
    post(p.url + "/jsonrpc", append_body(prim, title=TITLE), auth=("admin", "pw"))
    p.wait_idle(30)
    assert "'dead': 1" in " ".join(r.getMessage() for r in caplog.records)
    news.stats.clear()
    p.stop()
    p2 = make_proxy(state_dir=str(tmp_path / "s"))                   # a later grab (state survives restarts)
    post(p2.url + "/jsonrpc", append_body(release(TITLE, prefix="q"), title=TITLE), auth=("admin", "pw"))
    p2.wait_idle(30)
    assert not [m for m in news.stats if m.startswith("d-")]          # no new STATs for the dead posting
    assert "'known-dead': 1" in [r.getMessage() for r in caplog.records if r.getMessage().startswith("append key=")][-1]


def test_outage_never_marks_donors_dead(make_proxy, nzbget, hydra, caplog, tmp_path):
    caplog.set_level(logging.INFO)
    prim, donor = release(TITLE, prefix="p"), release(TITLE, prefix="d")
    news = FakeNntp(article_ids(prim) + article_ids(donor), password="right")
    nzbget.config_entries = [e if e["Name"] != "Server1.Password" else {"Name": e["Name"], "Value": "wrong"}
                             for e in news.config(1)]                    # every request fails: an outage
    hydra.add(TITLE, donor)
    p = make_proxy(state_dir=str(tmp_path / "s"))
    post(p.url + "/jsonrpc", append_body(prim, title=TITLE), auth=("admin", "pw"))
    p.wait_idle(60)
    assert len(nzbget.appends) == 2                                       # donor kept (health unknown)
    import nzbget_dupe_proxy as ndp
    assert not p.state.is_dead(ndp.sketch(ndp.parse_nzb(donor).message_ids))


def test_one_erroring_server_still_reveals_missing_articles():
    ids = ["e%d@x" % i for i in range(20)]
    ok, broken = FakeNntp(), FakeNntp(password="right")
    entries = [e if e["Name"] != "Server2.Password" else {"Name": e["Name"], "Value": "wrong"}
               for e in ok.config(1) + broken.config(2)]
    h = dh.check_many(dh.servers_from_nzbget_config(entries), {"n": ids}, percent=100, probe=20, body_percent=0)["n"]
    assert h.missing == 20 and h.error == 0


def test_dead_cache_recognizes_relisted_posting_with_reuploaded_segment(make_proxy, nzbget, hydra, caplog,
                                                                         tmp_path, monkeypatch):
    import nzbget_dupe_proxy as ndp
    monkeypatch.setattr(ndp, "GROUP_WINDOW", 0)
    caplog.set_level(logging.INFO)
    prim, dead = release(TITLE, prefix="p"), release(TITLE, prefix="d")
    nzbget.config_entries = FakeNntp(article_ids(prim)).config(1)
    hydra.add(TITLE, dead)
    p = make_proxy(state_dir=str(tmp_path / "s"))
    post(p.url + "/jsonrpc", append_body(prim, title=TITLE), auth=("admin", "pw"))
    p.wait_idle(30)
    hydra.items[0].nzb = dead.replace(b"d-0-0@x", b"refill@x")       # the same dead posting, re-listed
    post(p.url + "/jsonrpc", append_body(release(TITLE, prefix="q"), title=TITLE), auth=("admin", "pw"))
    p.wait_idle(30)
    assert "'known-dead': 1" in [r.getMessage() for r in caplog.records if r.getMessage().startswith("append key=")][-1]


def test_concurrent_grabs_share_the_connection_cap(make_proxy, nzbget, hydra):
    import threading as th
    news = FakeNntp()
    news.delay = 0.02
    groups = []
    for g in range(3):                                                  # three different releases grabbed at once
        t = "Show.S01E0%d.1080p.WEB.H264-GRP" % (g + 1)
        prim = release(t, prefix="p%d" % g)
        donors = [release(t, prefix="g%dd%d" % (g, i)) for i in range(4)]
        news.articles |= {m for d in donors + [prim] for m in article_ids(d)}
        for d in donors:
            hydra.add(t, d)
        groups.append((t, prim))
    nzbget.config_entries = news.config(1, connections=50)
    p = make_proxy(max_conns_per_nntp_server=3)
    threads = [th.Thread(target=post, args=(p.url + "/jsonrpc", append_body(prim, title=t)), kwargs={"auth": ("a", "b")})
               for t, prim in groups]
    [x.start() for x in threads]
    [x.join() for x in threads]
    p.wait_idle(60)
    assert len(nzbget.appends) == 15
    assert news.max_active <= 3



def test_dead_primary_is_demoted_before_the_first_donor_arrives(make_proxy, nzbget, hydra, caplog):
    caplog.set_level(logging.INFO)
    prim, donor = release(TITLE, prefix="p"), release(TITLE, prefix="d")
    nzbget.config_entries = FakeNntp(article_ids(donor)).config(1)       # primary: nothing on any server
    hydra.add(TITLE, donor)
    p = make_proxy()
    post(p.url + "/jsonrpc", append_body(prim, title=TITLE), auth=("admin", "pw"))
    p.wait_idle(30)
    primary_id = nzbget.appends[0]["id"]
    demote = [i for i, e in enumerate(nzbget.edits) if e == ("GroupSetDupeScore", "1", [primary_id])]
    assert demote, nzbget.edits
    assert nzbget.edit_times[demote[0]] < nzbget.appends[1]["time"]       # before the first donor
    assert "primary is dead" in " ".join(r.getMessage() for r in caplog.records)


def test_partly_alive_primary_is_never_demoted(make_proxy, nzbget, hydra):
    prim, donor = release(TITLE, prefix="p"), release(TITLE, prefix="d")
    pids = article_ids(prim)
    nzbget.config_entries = FakeNntp(article_ids(donor) + pids[: len(pids) // 2]).config(1)
    hydra.add(TITLE, donor)
    p = make_proxy()
    post(p.url + "/jsonrpc", append_body(prim, title=TITLE), auth=("admin", "pw"))
    p.wait_idle(30)
    assert not [e for e in nzbget.edits if e[0] == "GroupSetDupeScore"]


def test_byte_identical_twin_outranks_other_packaging(make_proxy, nzbget, hydra):
    prim = release(TITLE, prefix="p")
    twin, other = release(TITLE, prefix="t"), release("x", prefix="o", n_files=20, segs_per_file=10, obfuscate=True)
    nzbget.config_entries = FakeNntp(article_ids(prim) + article_ids(twin) + article_ids(other)).config(1)
    hydra.add(TITLE, other, grabs=1000)
    hydra.add(TITLE, twin, grabs=1)
    p = make_proxy()
    post(p.url + "/jsonrpc", append_body(prim, title=TITLE), auth=("admin", "pw"))
    p.wait_idle(30)
    final = nzbget.final_scores()
    by = {base64.b64decode(a["params"][1]): final[a["id"]] for a in nzbget.appends[1:]}
    assert by[twin] == 90 and by[other] == 89                       # equally whole: the twin first


def test_dead_primary_is_demoted_while_hydra_is_still_searching(make_proxy, nzbget, hydra, caplog):
    caplog.set_level(logging.INFO)
    prim, donor = release(TITLE, prefix="p"), release(TITLE, prefix="d")
    nzbget.config_entries = FakeNntp(article_ids(donor)).config(1)       # primary: nothing on any server
    hydra.add(TITLE, donor)
    hydra.delay = 1.5                                                     # a slow search (many indexers)
    p = make_proxy()
    post(p.url + "/jsonrpc", append_body(prim, title=TITLE), auth=("admin", "pw"))
    p.wait_idle(30)
    primary_id = nzbget.appends[0]["id"]
    i = nzbget.edits.index(("GroupSetDupeScore", "1", [primary_id]))
    assert nzbget.edit_times[i] < hydra.fetch_times[0]                    # before any donor NZB was even fetched
    msg = [r.getMessage() for r in caplog.records if "primary" in r.getMessage() and "dead" in r.getMessage()][0]
    assert TITLE in msg


def test_primary_that_already_left_the_queue_is_reported_plainly(make_proxy, nzbget, hydra, caplog):
    caplog.set_level(logging.INFO)
    prim, donor = release(TITLE, prefix="p"), release(TITLE, prefix="d")
    nzbget.config_entries = FakeNntp(article_ids(donor)).config(1)
    nzbget.editqueue_result = False                                       # nzbget: no such queue item any more
    hydra.add(TITLE, donor)
    p = make_proxy()
    post(p.url + "/jsonrpc", append_body(prim, title=TITLE), auth=("admin", "pw"))
    p.wait_idle(30)
    assert "already left the queue" in " ".join(r.getMessage() for r in caplog.records)


def test_server_answering_451_for_missing_articles_keeps_its_connection(monkeypatch):
    # super.newsgroupdirect.com says 451 (not 430) for a missing article: an answer, not a broken connection
    monkeypatch.setattr(dh, "SERVER_RETRY_AFTER", 30.0)
    import time
    ids = ["q%d@x" % i for i in range(20)]
    says_451, says_430 = FakeNntp(missing_code=451), FakeNntp()
    servers = dh.servers_from_nzbget_config(says_451.config(1) + says_430.config(2), max_conns=1)
    t0 = time.time()
    h = dh.check_many(servers, {"n": ids}, percent=100, probe=20, body_percent=0)["n"]
    assert h.missing == 20 and time.time() - t0 < 3
    assert says_451.sessions == 1 and len(says_451.stats) == 20


def test_paused_server_does_not_hold_up_missing_articles(monkeypatch):
    monkeypatch.setattr(dh, "SERVER_RETRY_AFTER", 30.0)
    import time
    ids = ["h%d@x" % i for i in range(20)]
    ok, broken = FakeNntp(), FakeNntp(password="right")
    entries = [e if e["Name"] != "Server2.Password" else {"Name": e["Name"], "Value": "wrong"}
               for e in ok.config(1) + broken.config(2)]
    t0 = time.time()
    h = dh.check_many(dh.servers_from_nzbget_config(entries), {"n": ids}, percent=100, probe=20, body_percent=0)["n"]
    assert h.missing == 20 and time.time() - t0 < 5


def test_budget_end_counts_articles_most_servers_miss_as_missing():
    # present articles settle at the first hit, missing ones wait for every server: when the budget ends
    # first, leaving the unsettled ones out would make a half-dead NZB look whole
    ids = ["g%d@x" % i for i in range(20)]
    a, b, slow = FakeNntp(ids[:10]), FakeNntp(), FakeNntp()
    slow.delay = 3.0
    servers = dh.servers_from_nzbget_config(a.config(1) + b.config(2) + slow.config(3), max_conns=1)
    h = dh.check_many(servers, {"n": ids}, percent=100, probe=20, body_percent=0, budget=1.0)["n"]
    assert (h.present, h.missing) == (10, 10) and h.alive == 0.5


def test_sample_bounds_are_configurable():
    ids = ["c%d@x" % i for i in range(20000)]
    servers = dh.servers_from_nzbget_config(FakeNntp(ids).config(1, connections=40), max_conns=20)
    h = dh.check_many(servers, {"n": ids}, percent=5, body_percent=0, minimum=50, maximum=1000,
                      limits=dh.Limits(1, 4))["n"]
    assert h.checked == 1000 and h.present == 1000                       # 5% = 1000, not capped at 300
    small = dh.check_many(servers, {"n": ids[:300]}, percent=5, body_percent=0, minimum=50, maximum=1000)["n"]
    assert small.checked == 50


def test_each_body_is_downloaded_by_one_server_only():
    # all servers reach the first articles at once; the body (~750 KB live) needs fetching from one of them
    ids = ["o%d@x" % i for i in range(10)]
    srvs = [FakeNntp(ids) for _ in range(3)]
    for s in srvs:
        s.delay = 0.02
    servers = dh.servers_from_nzbget_config([e for n, s in enumerate(srvs, 1) for e in s.config(n)])
    h = dh.check_many(servers, {"n": ids}, percent=100, probe=10, body_percent=100, max_body=100)["n"]
    assert h.alive == 1.0 and h.body_checked == 10
    assert sum(len(s.bodies) for s in srvs) == 10


def test_bad_body_is_tried_on_the_next_server():
    ids = ["t%d@x" % i for i in range(10)]
    soft, good = FakeNntp(ids, soft_dead=ids), FakeNntp(ids)
    good.delay = 0.05                                                    # the soft-dead server claims first
    servers = dh.servers_from_nzbget_config(soft.config(1) + good.config(2))
    h = dh.check_many(servers, {"n": ids}, percent=100, probe=10, body_percent=100, max_body=100)["n"]
    assert h.alive == 1.0 and set(good.bodies) == set(ids)


def test_sample_env_vars():
    import nzbget_dupe_proxy as ndp
    c = ndp.Config.from_env({})
    assert (c.health_percent, c.health_min_articles, c.health_max_articles, c.body_max_per_nzb) == (5.0, 50, 1000, 20)
    c = ndp.Config.from_env({"HEALTH_MIN_ARTICLES": "10", "HEALTH_MAX_ARTICLES": "3000"})
    assert (c.health_min_articles, c.health_max_articles) == (10, 3000)


def test_final_dupescores_rank_the_most_whole_donor_first(make_proxy, nzbget, hydra):
    # nzbget tries dupes by DupeScore: after every check, the order must be wholeness first (twin breaks a
    # tie), whatever order the donors were added in
    prim = release(TITLE, prefix="p")
    twin90, twin100 = release(TITLE, prefix="t"), release(TITLE, prefix="w")
    other100, other95 = release(TITLE, prefix="o", n_files=11), release(TITLE, prefix="q", n_files=12)
    t_ids, q_ids = article_ids(twin90), article_ids(other95)
    alive = (article_ids(prim) + t_ids[: len(t_ids) * 9 // 10] + article_ids(twin100) + article_ids(other100)
             + q_ids[: len(q_ids) * 95 // 100])
    nzbget.config_entries = FakeNntp(alive).config(1)
    for d, grabs in ((twin90, 50), (other95, 40), (twin100, 1), (other100, 1)):
        hydra.add(TITLE, d, grabs=grabs)
    p = make_proxy(health_percent=100, fast_donors=2)
    post(p.url + "/jsonrpc", append_body(prim, title=TITLE), auth=("admin", "pw"))
    p.wait_idle(30)
    final = nzbget.final_scores()
    by = {base64.b64decode(a["params"][1]): final[a["id"]] for a in nzbget.appends[1:]}
    assert [by[d] for d in (twin100, other100, other95, twin90)] == [90, 89, 85, 82]
