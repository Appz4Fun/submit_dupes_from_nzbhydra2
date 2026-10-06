"""The nzbget extension (nzbget_extension/): a QUEUE extension on NZB_ADDED that runs discovery in a worker."""
import filecmp
import importlib.util
import os
import time

from tests.fakes import FakeHydra, FakeNntp, article_ids, release

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXT = os.path.join(ROOT, "nzbget_extension")
TITLE = "Show.S01E01.1080p.WEB.H264-GRP"
KEY = "tvdbid=1-S01-E01|show-s01e01"


def _main():
    spec = importlib.util.spec_from_file_location("dupe_donors_main", os.path.join(EXT, "main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _env(nzbget, hydra, tmp_path, **extra):
    env = {"NZBOP_CONTROLIP": "0.0.0.0", "NZBOP_CONTROLPORT": nzbget.url.rsplit(":", 1)[1],
           "NZBOP_CONTROLUSERNAME": "admin", "NZBOP_CONTROLPASSWORD": "pw", "NZBOP_MAINDIR": str(tmp_path),
           "NZBOP_VERSION": "27.0", "NZBPO_HydraUrl": hydra.url, "NZBPO_HydraApiKey": "KEY",
           "NZBPO_SettleSeconds": "0", "NZBPO_PrimaryScore": "100"}
    env.update(extra)
    return env


def _pick(nzbget, tmp_path, nzb, nzbid=500, score=23859118):
    nzbdir = tmp_path / "nzbs"
    nzbdir.mkdir(exist_ok=True)
    (nzbdir / (TITLE + ".nzb.queued")).write_bytes(nzb)
    nzbget.config_entries = [{"Name": "NzbDir", "Value": str(nzbdir)}]
    nzbget.queue_items.append({"NZBID": nzbid, "Status": "QUEUED", "DupeKey": KEY, "DupeScore": score,
                               "NZBName": TITLE, "NZBFilename": TITLE + ".nzb", "Category": "tv",
                               "FileSizeLo": 0, "FileSizeHi": 0})


def test_nzb_added_starts_a_detached_worker_and_returns_at_once(monkeypatch, nzbget, hydra, tmp_path, capsys):
    m, started = _main(), []
    monkeypatch.setattr(m.subprocess, "Popen", lambda args, **kw: started.append((args, kw)))
    t0 = time.time()
    rc = m.main(_env(nzbget, hydra, tmp_path, NZBNA_EVENT="NZB_ADDED", NZBNA_NZBID="500", NZBNA_NZBNAME=TITLE), [])
    assert rc == 0 and time.time() - t0 < 1
    (args, kw), = started
    assert args[-2:] == ["--worker", "500"] and kw["start_new_session"] is True
    assert "searching for other postings" in capsys.readouterr().out


def test_other_events_and_missing_options_start_nothing(monkeypatch, nzbget, hydra, tmp_path, capsys):
    m, started = _main(), []
    monkeypatch.setattr(m.subprocess, "Popen", lambda args, **kw: started.append(args))
    assert m.main(_env(nzbget, hydra, tmp_path, NZBNA_EVENT="NZB_DELETED", NZBNA_NZBID="5"), []) == 0
    env = _env(nzbget, hydra, tmp_path, NZBNA_EVENT="NZB_ADDED", NZBNA_NZBID="5")
    del env["NZBPO_HydraUrl"]
    assert m.main(env, []) == 0
    assert started == [] and "[ERROR]" in capsys.readouterr().out


def test_worker_adds_donors_under_the_picks_key(nzbget, hydra, tmp_path):
    donor = release(TITLE, prefix="r")
    hydra.add(TITLE, donor)
    _pick(nzbget, tmp_path, release(TITLE, prefix="p"))
    assert _main().main(_env(nzbget, hydra, tmp_path), ["--worker", "500"]) == 0
    (d,) = nzbget.appends
    assert d["params"][6] == KEY and d["params"][7] == 23859118 - 1000 + 90 and d["params"][2] == "tv"
    logged = [r.body for r in nzbget.requests if b'"writelog"' in r.body]
    assert logged and all(b"KEY" not in b or b"apikey=***" in b for b in logged)   # its log goes to nzbget's


def test_worker_leaves_a_backup_alone(nzbget, hydra, tmp_path):
    hydra.add(TITLE, release(TITLE, prefix="r"))
    _pick(nzbget, tmp_path, release(TITLE, prefix="p"), nzbid=500)
    nzbget.history_items.append({"NZBID": 400, "DupeKey": KEY, "DupeScore": 23859118 + 5})   # a higher pick
    assert _main().main(_env(nzbget, hydra, tmp_path), ["--worker", "500"]) == 0
    assert nzbget.appends == [] and hydra.queries == []


def test_connection_test_command(nzbget, hydra, tmp_path, capsys):
    rc = _main().main(_env(nzbget, hydra, tmp_path, NZBCP_COMMAND="ConnectionTest"), [])
    assert rc == 93 and "Hydra" in capsys.readouterr().out
    bad = FakeHydra()
    bad.server.shutdown()
    bad.server.server_close()
    assert _main().main(_env(nzbget, bad, tmp_path, NZBCP_COMMAND="ConnectionTest"), []) == 94


def test_extension_carries_the_service_code_unchanged():
    for f in ("nzbget_dupe_proxy.py", "donor_health.py"):
        assert filecmp.cmp(os.path.join(ROOT, f), os.path.join(EXT, f), shallow=False), f
    cmp = filecmp.dircmp(os.path.join(ROOT, "vendor"), os.path.join(EXT, "vendor"), ignore=["__pycache__"])
    assert not (cmp.left_only or cmp.right_only or cmp.diff_files)


def test_connection_test_without_saved_options_says_so(nzbget, hydra, tmp_path, capsys):
    env = _env(nzbget, hydra, tmp_path, NZBCP_COMMAND="ConnectionTest")
    del env["NZBPO_HydraUrl"]
    assert _main().main(env, []) == 94
    out = capsys.readouterr().out
    assert "HydraUrl" in out and "Save" in out and "unknown url type" not in out


def _failed_pick(nzbget, tmp_path, nntp, backups, nzbid=500, score=23859118):
    """A pick that already failed (HEALTH) with DupeMode FORCE, and its backups in history: (prefix, nzbid)."""
    nzbdir = tmp_path / "nzbs"
    nzbdir.mkdir(exist_ok=True)
    nzbget.config_entries = nntp.config(1) + [{"Name": "NzbDir", "Value": str(nzbdir)}]
    nzbget.history_items.append({"NZBID": nzbid, "Status": "FAILURE/HEALTH", "DupeKey": KEY, "DupeScore": score,
                                 "DupeMode": "FORCE", "NZBName": TITLE, "Name": TITLE})
    for prefix, bid in backups:
        nzb, name = release(TITLE, prefix=prefix), "%s.%s" % (TITLE, prefix)
        (nzbdir / (name + ".nzb.queued")).write_bytes(nzb)
        nzbget.history_items.append({"NZBID": bid, "Status": "DELETED/DUPE", "DupeKey": KEY, "DupeScore": score - bid,
                                     "NZBName": name, "Name": name, "NZBFilename": name + ".nzb",
                                     "FileSizeLo": len(nzb), "FileSizeHi": 0})


def test_health_failure_starts_a_worker(monkeypatch, nzbget, hydra, tmp_path):
    m, started = _main(), []
    monkeypatch.setattr(m.subprocess, "Popen", lambda args, **kw: started.append(args))
    env = _env(nzbget, hydra, tmp_path, NZBNA_EVENT="NZB_DELETED", NZBNA_NZBID="500", NZBNA_DELETESTATUS="HEALTH")
    assert m.main(env, []) == 0
    assert started and started[0][-2:] == ["--worker", "500"]


def test_failed_pick_returns_its_wholest_backup(nzbget, hydra, tmp_path):
    # live (Shrinking S02E07 4144): a FORCE pick died (0 of 1,034 articles) and nzbget parked it, "no better
    # duplicate", with whole backups of the same key in history
    nntp = FakeNntp(article_ids(release(TITLE, prefix="w")))
    _failed_pick(nzbget, tmp_path, nntp, [("d", 501), ("w", 502)])
    assert _main().main(_env(nzbget, hydra, tmp_path), ["--worker", "500"]) == 0
    assert ("HistoryRedownload", "", [502]) in nzbget.edits
    assert not [e for e in nzbget.edits if e[0] == "HistoryRedownload" and e[2] != [502]]


def test_failed_pick_with_only_dead_backups_returns_nothing(nzbget, hydra, tmp_path):
    _failed_pick(nzbget, tmp_path, FakeNntp([]), [("d", 501), ("e", 502)])
    assert _main().main(_env(nzbget, hydra, tmp_path), ["--worker", "500"]) == 0
    assert not [e for e in nzbget.edits if e[0] == "HistoryRedownload"]


def test_failed_pick_whose_failover_already_happened_is_left_alone(nzbget, hydra, tmp_path):
    nntp = FakeNntp(article_ids(release(TITLE, prefix="w")))
    _failed_pick(nzbget, tmp_path, nntp, [("w", 502)])
    nzbget.queue_items.append({"NZBID": 503, "Status": "DOWNLOADING", "DupeKey": KEY, "DupeScore": 1})
    assert _main().main(_env(nzbget, hydra, tmp_path), ["--worker", "500"]) == 0
    assert not [e for e in nzbget.edits if e[0] == "HistoryRedownload"]


def test_a_pick_that_failed_over_before_its_worker_ran_still_gets_its_backups_ranked(nzbget, hydra, tmp_path):
    # live (Industry S04E07 4162): the pick failed over within the settle time, and nzbget's failover had
    # already queued a backup, so the worker found it gone and exited without ranking the remaining backups
    nntp = FakeNntp(article_ids(release(TITLE, prefix="w")))
    _failed_pick(nzbget, tmp_path, nntp, [("d", 501), ("w", 502)])
    nzbget.queue_items.append({"NZBID": 503, "Status": "DOWNLOADING", "DupeKey": KEY, "DupeScore": 1})
    assert _main().main(_env(nzbget, hydra, tmp_path), ["--worker", "500"]) == 0
    assert ("HistorySetParameter", "DupeAlive=100", [502]) in nzbget.edits
    assert not [e for e in nzbget.edits if e[0] == "HistoryRedownload"]


def test_a_pick_that_failed_before_its_worker_ran_still_gets_donors(nzbget, hydra, tmp_path):
    # live (Industry S04E07 4162): the pick failed over within the settle time, so the indexer was never
    # searched; ~50 postings were listed but only the client's 8 backups were in nzbget
    donor = release(TITLE, prefix="r")
    hydra.add(TITLE, donor)
    _failed_pick(nzbget, tmp_path, FakeNntp(article_ids(donor)), [])
    nzbdir = tmp_path / "nzbs"
    pick = release(TITLE, prefix="p")
    (nzbdir / (TITLE + ".nzb.queued")).write_bytes(pick)
    nzbget.history_items[0].update({"NZBFilename": TITLE + ".nzb", "FileSizeLo": len(pick), "FileSizeHi": 0,
                                    "Category": "tv"})
    assert _main().main(_env(nzbget, hydra, tmp_path), ["--worker", "500"]) == 0
    (d,) = nzbget.appends
    assert d["params"][6] == KEY


def test_a_downloaded_file_starts_a_sweep_at_most_every_15_seconds(monkeypatch, nzbget, hydra, tmp_path):
    # live (Foundation S02E01 4224): nzbget restarted 15 s after the NZB was added; systemd killed the detached
    # worker, and nothing ever searched the pick. nzbget runs the extension on every downloaded file, so use that
    m, started = _main(), []
    monkeypatch.setattr(m.subprocess, "Popen", lambda args, **kw: started.append(args))
    env = _env(nzbget, hydra, tmp_path, NZBNA_EVENT="FILE_DOWNLOADED", NZBNA_NZBID="500")
    assert m.main(env, []) == 0 and m.main(env, []) == 0
    assert len(started) == 1 and started[0][-1] == "--sweep"
    stamp = tmp_path / "dupe-donors" / "sweep.stamp"
    old = time.time() - 20
    os.utime(stamp, (old, old))
    assert m.main(env, []) == 0
    assert len(started) == 2


def test_sweep_searches_a_pick_whose_worker_died(nzbget, hydra, tmp_path):
    donor = release(TITLE, prefix="r")
    hydra.add(TITLE, donor)
    _pick(nzbget, tmp_path, release(TITLE, prefix="p"))
    assert _main().main(_env(nzbget, hydra, tmp_path), ["--sweep"]) == 0
    (d,) = nzbget.appends
    assert d["params"][6] == KEY


def test_sweep_leaves_a_pick_that_was_already_searched(nzbget, hydra, tmp_path):
    hydra.add(TITLE, release(TITLE, prefix="r"))
    _pick(nzbget, tmp_path, release(TITLE, prefix="p"))
    m, env = _main(), _env(nzbget, hydra, tmp_path)
    assert m.main(env, ["--worker", "500"]) == 0 and len(nzbget.appends) == 1
    queries = len(hydra.queries)
    assert m.main(env, ["--sweep"]) == 0
    assert len(nzbget.appends) == 1 and len(hydra.queries) == queries


def test_a_sweep_is_not_started_while_a_worker_holds_the_lock(monkeypatch, nzbget, hydra, tmp_path):
    # live (Sisters Grimm S02E01 4230): a sweep killed by a restart left its 90 s throttle stamp behind, so no
    # sweep could run before the download finished; only a live worker (the lock) should hold the next one off
    import fcntl
    m, started = _main(), []
    monkeypatch.setattr(m.subprocess, "Popen", lambda args, **kw: started.append(args))
    env = _env(nzbget, hydra, tmp_path, NZBNA_EVENT="FILE_DOWNLOADED", NZBNA_NZBID="500")
    state = tmp_path / "dupe-donors"
    state.mkdir()
    with open(state / "worker.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert m.main(env, []) == 0
        assert started == []                    # a worker is running: leave it alone
    assert m.main(env, []) == 0 and len(started) == 1


def test_a_failed_pick_is_not_rescued_when_the_key_already_has_a_success(nzbget, hydra, tmp_path):
    # live (Slow Horses S06E01 4320): the pick was dead, nzbget failed over and backup 4321 succeeded; a later
    # rescue for the failed pick saw an empty queue and returned another backup: the episode downloaded twice
    nntp = FakeNntp(article_ids(release(TITLE, prefix="w")))
    _failed_pick(nzbget, tmp_path, nntp, [("w", 502)])
    nzbget.history_items.append({"NZBID": 600, "Status": "SUCCESS/ALL", "DupeKey": KEY, "DupeScore": 1,
                                 "NZBName": TITLE + ".ok", "Name": TITLE + ".ok"})
    assert _main().main(_env(nzbget, hydra, tmp_path), ["--worker", "500"]) == 0
    assert not [e for e in nzbget.edits if e[0] == "HistoryRedownload"]


def test_a_rescue_rechecks_for_a_success_after_the_slow_ranking(monkeypatch, nzbget, hydra, tmp_path):
    # live (Raymond and Ray 4340): the backup nzbget returned was still downloading when the rescue started
    # (no success yet), the ranking took 4 minutes, the backup finished meanwhile, and the rescue then saw an
    # empty queue and returned another backup: a 20 GB episode downloaded twice
    import nzbget_dupe_proxy as ndp
    nntp = FakeNntp(article_ids(release(TITLE, prefix="w")))
    _failed_pick(nzbget, tmp_path, nntp, [("w", 502)])
    real = ndp.Proxy.rank_backups

    def slow_rank(self, *a, **kw):
        out = real(self, *a, **kw)
        nzbget.history_items.append({"NZBID": 600, "Status": "SUCCESS/ALL", "DupeKey": KEY, "DupeScore": 1,
                                     "NZBName": TITLE + ".ok", "Name": TITLE + ".ok"})  # finished while ranking
        return out
    monkeypatch.setattr(ndp.Proxy, "rank_backups", slow_rank)
    assert _main().main(_env(nzbget, hydra, tmp_path), ["--worker", "500"]) == 0
    assert not [e for e in nzbget.edits if e[0] == "HistoryRedownload"]
