import base64
import json
import logging
import time

from tests.fakes import append_body, basic, make_nzb, nzb_total, post, release

TITLE = "Show.S01E01.1080p.WEB.H264-GRP"
KEY = "dupes:show.s01e01.1080p.web.h264.grp"
AUTH = ("admin", "pw")


def primary(prefix="p", **kw):
    return release(TITLE, prefix=prefix, **kw)


def append(proxy, nzb, title=TITLE, wait=True, **kw):
    status, _, resp = post(proxy.url + "/jsonrpc", append_body(nzb, title=title, **kw), auth=AUTH)
    assert status == 200
    if wait:
        proxy.wait_idle(10)
    return json.loads(resp)


def donors(nzbget):
    return [a for a in nzbget.appends[1:]]


def summary(caplog):
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("append ")]
    assert lines, "no summary line logged"
    return lines[-1]


def test_primary_gets_dupekey_and_returns_before_discovery(proxy, nzbget, hydra):
    hydra.delay = 2.0
    hydra.add("Show.S01E01.1080p.WEB.H264-OTHER", release(TITLE, prefix="r"))
    t0 = time.time()
    resp = append(proxy, primary(), wait=False, category="Series")
    assert time.time() - t0 < 1.0
    assert resp["result"] == nzbget.appends[0]["id"]
    p = nzbget.appends[0]["params"]
    assert p[0] == TITLE + ".nzb" and p[2] == "Series"
    assert (p[6], p[7], p[8]) == (KEY, 100, "SCORE")
    assert len(nzbget.appends) == 1
    proxy.wait_idle(10)
    assert len(nzbget.appends) == 2


def test_primary_response_bytes_unchanged(proxy, nzbget):
    status, _, resp = post(proxy.url + "/jsonrpc", append_body(primary(), title=TITLE, rid="406397322"), auth=AUTH)
    proxy.wait_idle(10)
    assert len(nzbget.appends) == 1
    assert resp == nzbget.last_response
    assert json.loads(resp)["id"] == "406397322"


def test_repost_with_other_title_same_size_accepted_with_donor_params(proxy, nzbget, hydra):
    donor = release(TITLE, prefix="r")
    hydra.add("Show S01E01 1080p WEB H264-OTHER", donor, grabs=5)
    append(proxy, primary(), category="Series")
    (d,) = donors(nzbget)
    p = d["params"]
    assert p[0] == "Show S01E01 1080p WEB H264-OTHER.nzb"
    assert base64.b64decode(p[1]) == donor
    assert p[2:] == ["Series", 0, False, False, KEY, 90, "SCORE", []]
    assert d["path"] == "/jsonrpc" and d["auth"] == basic(*AUTH)


def test_same_title_wrong_size_never_fetched(proxy, nzbget, hydra):
    hydra.add(TITLE, release(TITLE, prefix="w", n_files=13))
    append(proxy, primary())
    assert donors(nzbget) == [] and hydra.fetches == []


def test_identical_message_ids_rejected(proxy, nzbget, hydra, caplog):
    caplog.set_level(logging.INFO)
    hydra.add(TITLE, primary())                       # the primary itself, from another indexer
    append(proxy, primary())
    assert donors(nzbget) == []
    assert "same-posting" in summary(caplog)


def test_duplicate_posting_across_indexers_added_once(proxy, nzbget, hydra):
    hydra.add(TITLE, release(TITLE, prefix="r"), grabs=9)
    hydra.add(TITLE, release(TITLE, prefix="r"), grabs=1)
    append(proxy, primary())
    assert len(donors(nzbget)) == 1


def test_obfuscated_filenames_same_size_accepted(proxy, nzbget, hydra):
    hydra.add(TITLE, release(TITLE, prefix="o", obfuscate=True))
    append(proxy, primary())
    assert len(donors(nzbget)) == 1


def test_different_packaging_same_bytes_wrong_count_rejected(proxy, nzbget, hydra, caplog):
    caplog.set_level(logging.INFO)
    hydra.add(TITLE, release("x", prefix="q", n_files=20, segs_per_file=10))  # same bytes, 20 files, no shared names
    append(proxy, primary())
    assert donors(nzbget) == []
    assert "mismatch" in summary(caplog)


def test_max_donors_cap_scores_and_ranking(make_proxy, nzbget, hydra):
    p = make_proxy(max_donors=2)
    for i, grabs in enumerate([1, 50, 10, 5]):
        hydra.add(TITLE, release(TITLE, prefix="d%d" % i), grabs=grabs)
    append(p, primary())
    ds = donors(nzbget)
    assert [d["params"][7] for d in ds] == [90, 89]
    assert [base64.b64decode(d["params"][1]) for d in ds] == [release(TITLE, prefix="d1"), release(TITLE, prefix="d2")]


def test_fetches_capped(make_proxy, nzbget, hydra):
    p = make_proxy(max_donors=1)
    for _ in range(10):
        hydra.add(TITLE, b"garbage", size=nzb_total(primary()))
    append(p, primary())
    assert len(hydra.fetches) == 3                    # 3 * MAX_DONORS


def test_hydra_dupekey_kept(proxy, nzbget, hydra):
    hydra.add(TITLE, release(TITLE, prefix="r"))
    append(proxy, primary(), dupekey="mykey")
    assert [a["params"][6] for a in nzbget.appends] == ["mykey", "mykey"]


def test_queries_title_short_title_and_imdb(proxy, hydra):
    nzb = make_nzb([("a.mkv", [100] * 5)], prefix="p", meta={"imdb": "tt0123456"})
    append(proxy, nzb, title="Some.Movie.2020.Extended.1080p.BluRay.x264-GRP")
    qs = " ".join(hydra.queries)
    assert "q=some+movie+2020+extended+1080p+bluray+x264+grp" in qs
    assert "q=some+movie+2020+1080p" in qs
    assert "t=movie" in qs and "imdbid=0123456" in qs
    assert all("limit=100" in q and "apikey=KEY" in q for q in hydra.queries)


def test_send_selected_collapses_to_one_key_without_resending(proxy, nzbget, hydra):
    donor = release(TITLE, prefix="r")
    hydra.add(TITLE + "-OTHER", donor)
    append(proxy, primary())
    assert len(nzbget.appends) == 2
    n_queries = len(hydra.queries)
    # Hydra "send selected": the donor row arrives as its own append -> already sent, existing NZBID returned
    resp = append(proxy, donor, title="Show.S01E01.1080p.WEB.H264-GRP-OTHER", rid=5)
    assert len(nzbget.appends) == 2
    assert resp == {"version": "1.1", "id": 5, "result": nzbget.appends[1]["id"]}
    # a third matching posting not seen before joins the same key; no second discovery round
    append(proxy, release(TITLE, prefix="z"), title="Show.S01E01.1080p.WEB.H264-ZZZ")
    assert len(nzbget.appends) == 3
    assert nzbget.appends[2]["params"][6] == KEY
    assert len(hydra.queries) == n_queries


def test_state_survives_restart(make_proxy, nzbget, hydra):
    p1 = make_proxy()
    append(p1, primary())
    p1.stop()
    p2 = make_proxy()
    resp = append(p2, primary())
    assert len(nzbget.appends) == 1 and resp["result"] == nzbget.appends[0]["id"]


def test_hydra_down_primary_still_added(make_proxy, nzbget, caplog):
    caplog.set_level(logging.INFO)
    p = make_proxy(hydra_url="http://127.0.0.1:9")
    resp = append(p, primary())
    assert resp["result"] == nzbget.appends[0]["id"] and len(nzbget.appends) == 1
    assert any(r.levelno >= logging.WARNING and "hydra" in r.getMessage().lower() for r in caplog.records)
    assert "added=0" in summary(caplog)


def test_candidate_404_and_malformed_rejected(proxy, nzbget, hydra, caplog):
    caplog.set_level(logging.INFO)
    size = nzb_total(primary())
    hydra.add(TITLE, b"", size=size, status=404)
    hydra.add(TITLE, b"<nzb><broken", size=size)
    resp = append(proxy, primary())
    assert resp["result"] == nzbget.appends[0]["id"] and len(nzbget.appends) == 1
    s = summary(caplog)
    assert "'fetch': 1" in s and "'parse': 1" in s


def test_malformed_primary_still_added_with_key(proxy, nzbget, hydra, caplog):
    caplog.set_level(logging.INFO)
    resp = append(proxy, b"not an nzb")
    assert resp["result"] == nzbget.appends[0]["id"]
    assert nzbget.appends[0]["params"][6] == KEY
    assert hydra.queries == []


def test_link_mode_and_named_params_passthrough(proxy, nzbget, hydra):
    body = json.dumps({"method": "append", "params": [TITLE + ".nzb", "http://h/x.nzb", "", 0, False, False, "", 0, "SCORE", []]}).encode()
    post(proxy.url + "/jsonrpc", body)
    named = json.dumps({"method": "append", "params": {"NZBFilename": "x.nzb"}}).encode()
    post(proxy.url + "/jsonrpc", named)
    proxy.wait_idle(5)
    assert nzbget.appends[0]["params"][6] == KEY
    assert nzbget.requests[-1].body == named
    assert hydra.queries == []


def test_deadline_stops_late_donors(make_proxy, nzbget, hydra, caplog):
    caplog.set_level(logging.INFO)
    p = make_proxy(deadline=0.5)
    hydra.delay = 1.0
    hydra.add(TITLE, release(TITLE, prefix="r"))
    append(p, primary())
    assert donors(nzbget) == []
    assert "deadline" in summary(caplog)


def test_dry_run_logs_donors_without_appending(make_proxy, nzbget, hydra, caplog):
    caplog.set_level(logging.INFO)
    p = make_proxy(dry_run="1")
    hydra.add("Show.S01E01.1080p.WEB.H264-OTHER", release(TITLE, prefix="r"))
    append(p, primary())
    assert len(nzbget.appends) == 1
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "DRY-RUN" in msgs and "Show.S01E01.1080p.WEB.H264-OTHER" in msgs
    assert "dry_run" in summary(caplog)


def test_summary_line(proxy, hydra, caplog):
    caplog.set_level(logging.INFO)
    hydra.add("Show.S01E01.1080p.WEB.H264-OTHER", release(TITLE, prefix="r"))
    append(proxy, primary())
    s = summary(caplog)
    for part in ("key=" + KEY, "nzbid=1001", "candidates=1", "verified=1", "added=1", "rejected="):
        assert part in s
    assert "apikey=KEY" not in " ".join(r.getMessage() for r in caplog.records)


def test_donor_verified_by_deadline_is_still_added(make_proxy, nzbget, hydra):
    p = make_proxy(deadline=0.5)
    hydra.fetch_delay = 0.7                            # fetch started before, finishes after the deadline
    hydra.add(TITLE, release(TITLE, prefix="r"))
    append(p, primary())
    assert len(donors(nzbget)) == 1
