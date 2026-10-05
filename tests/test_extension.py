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
