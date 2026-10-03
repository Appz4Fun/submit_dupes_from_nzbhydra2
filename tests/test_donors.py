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
    hydra.add("Show S01E01 1080p WEB H264-GRP", release(TITLE, prefix="r"))
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
    assert resp == nzbget.appends[0]["response"]
    assert json.loads(resp)["id"] == "406397322"


def test_repost_other_formatting_and_size_accepted_with_donor_params(proxy, nzbget, hydra):
    donor = release(TITLE, prefix="r", n_files=19)          # +90% bytes: size is not a filter
    hydra.add("Show S01E01 1080p WEB H264-GRP", donor, grabs=5)
    append(proxy, primary(), category="Series")
    (d,) = donors(nzbget)
    p = d["params"]
    assert p[0] == "Show S01E01 1080p WEB H264-GRP.nzb"
    assert base64.b64decode(p[1]) == donor
    assert p[2:] == ["Series", 0, False, False, KEY, 49, "SCORE", []]     # other packaging (19 files): 10-49 band
    assert d["path"] == "/jsonrpc" and d["auth"] == basic(*AUTH)


def test_other_group_resolution_or_episode_never_fetched(proxy, nzbget, hydra):
    hydra.add("Show.S01E01.1080p.WEB.H264-OTHER", release(TITLE, prefix="a"))
    hydra.add("Show.S01E01.720p.WEB.H264-GRP", release(TITLE, prefix="b"))
    hydra.add("Show.S01E02.1080p.WEB.H264-GRP", release(TITLE, prefix="c"))
    append(proxy, primary())
    assert donors(nzbget) == [] and hydra.fetches == []


def test_size_tolerance_cap_when_set(make_proxy, nzbget, hydra):
    p = make_proxy(size_tolerance=0.2)
    hydra.add(TITLE, release(TITLE, prefix="r", n_files=14))   # +40%
    append(p, primary())
    assert hydra.fetches == []


def test_inner_filename_of_other_release_rejected(proxy, nzbget, hydra, caplog):
    caplog.set_level(logging.INFO)
    hydra.add(TITLE, release("Show.S01E01.720p.WEB.H264-GRP", prefix="r"))
    append(proxy, primary())
    assert donors(nzbget) == []
    assert "other-release" in summary(caplog)


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


def test_repackaged_repost_accepted(proxy, nzbget, hydra):
    hydra.add(TITLE, release("x", prefix="q", n_files=20, segs_per_file=10, obfuscate=True))  # 7z-style, 20 files
    append(proxy, primary())
    assert len(donors(nzbget)) == 1


def test_closest_size_ranked_first(make_proxy, nzbget, hydra):
    p = make_proxy(max_donors=1)
    hydra.add(TITLE, release(TITLE, prefix="far", n_files=15), grabs=100)
    hydra.add(TITLE, release(TITLE, prefix="near"), grabs=1)
    append(p, primary())
    assert base64.b64decode(donors(nzbget)[0]["params"][1]) == release(TITLE, prefix="near")


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
    assert len(set(hydra.fetches)) == 3               # 3 * MAX_DONORS candidates (each retried once)


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
    hydra.add("Show S01E01 1080p WEB H264-GRP", donor)
    append(proxy, primary())
    assert len(nzbget.appends) == 2
    n_queries = len(hydra.queries)
    # Hydra "send selected": the donor row arrives as its own append -> already sent, existing NZBID returned
    resp = append(proxy, donor, title="Show S01E01 1080p WEB H264-GRP", rid=5)
    assert len(nzbget.appends) == 2
    assert resp == {"version": "1.1", "id": 5, "result": nzbget.appends[1]["id"]}
    # a third matching posting not seen before joins the same key; no second discovery round
    append(proxy, release(TITLE, prefix="z", n_files=12), title="Show.S01E01.1080p.WEB.H264-GRP-xpost")
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
    hydra.add("Show S01E01 1080p WEB H264-GRP", release(TITLE, prefix="r"))
    append(p, primary())
    assert len(nzbget.appends) == 1
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "DRY-RUN" in msgs and "Show S01E01 1080p WEB H264-GRP" in msgs
    assert "dry_run" in summary(caplog)


def test_summary_line(proxy, hydra, caplog):
    caplog.set_level(logging.INFO)
    hydra.add("Show S01E01 1080p WEB H264-GRP", release(TITLE, prefix="r"))
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


def test_summary_omits_zero_counters(proxy, hydra, caplog):
    caplog.set_level(logging.INFO)
    hydra.add("Show S01E01 1080p WEB H264-GRP", release(TITLE, prefix="r"))
    append(proxy, primary())
    assert "rejected={}" in summary(caplog)


def test_near_identical_message_ids_are_the_same_posting(proxy, nzbget, hydra, caplog):
    caplog.set_level(logging.INFO)
    donor = release(TITLE, prefix="r")
    hydra.add(TITLE, donor)
    hydra.add(TITLE, donor.replace(b"r-0-0@x", b"refilled@x"))   # same posting, one segment re-uploaded
    hydra.add(TITLE, primary().replace(b"p-3-3@x", b"other@x"))  # the primary itself, one segment differs
    append(proxy, primary())
    assert len(donors(nzbget)) == 1
    assert "'same-posting': 2" in summary(caplog)


def test_partial_article_overlap_rejected(proxy, nzbget, hydra, caplog):
    caplog.set_level(logging.INFO)
    nzb = primary()
    for f in range(2, 10):                                  # re-id 8 of 10 files: still shares 20% of article ids
        nzb = nzb.replace(b">p-%d-" % f, b">x-%d-" % f)
    hydra.add(TITLE, nzb)
    append(proxy, primary())
    assert donors(nzbget) == []
    assert "'same-posting': 1" in summary(caplog)


def test_max_donors_zero_or_negative_is_unlimited(make_proxy, nzbget, hydra):
    for i in range(12):
        hydra.add(TITLE, release(TITLE, prefix="u%d" % i))
    for value in (0, -1):
        nzbget.appends.clear()
        p = make_proxy(max_donors=value, state_dir=str(__import__("tempfile").mkdtemp()))
        append(p, primary())
        assert [d["params"][7] for d in donors(nzbget)] == list(range(90, 78, -1))


def test_default_max_donors_is_unlimited():
    import nzbget_dupe_proxy as ndp
    assert ndp.Config.from_env({}).max_donors == 0


def test_transient_indexer_error_is_retried_once(proxy, nzbget, hydra, caplog):
    caplog.set_level(logging.INFO)
    hydra.add(TITLE, release(TITLE, prefix="r"), flaky=1)
    append(proxy, primary())
    assert len(donors(nzbget)) == 1
    assert "Request limit reached" in " ".join(r.getMessage() for r in caplog.records)   # what the indexer said


def test_persistent_indexer_error_still_rejected(proxy, nzbget, hydra, caplog):
    caplog.set_level(logging.INFO)
    hydra.add(TITLE, release(TITLE, prefix="r"), flaky=5)
    append(proxy, primary())
    assert donors(nzbget) == []
    assert "'parse': 1" in summary(caplog)


def test_finds_group_postings_renamed_by_other_indexers_beyond_the_first_page(proxy, nzbget, hydra):
    for i in range(120):                                   # other groups fill the first page of the short query
        hydra.add("Show.S01E01.1080p.WEB.H264-OTHER%d" % i, release(TITLE, prefix="o%d" % i), grabs=500)
    hydra.add("Show.S01E01.The.Pilot.1080p.WEB.H.264-GRP", release(TITLE, prefix="g"))  # other indexer's name
    append(proxy, primary())
    assert len(donors(nzbget)) == 1


def test_hydra_search_reads_all_pages(proxy, hydra):
    for i in range(250):
        hydra.add("Show.S01E01.1080p.WEB-X%d" % i, b"<nzb/>", size=1)
    assert len(proxy.hydra_search({"t": "search", "q": "show s01e01"})) == 250
    assert [q for q in hydra.queries if "offset=200" in q]


def _nzbget_already_has(nzbget, tmp_path, name, nzb, key=KEY, copies=1):
    nzbdir = tmp_path / "nzbs"
    nzbdir.mkdir(exist_ok=True)
    for c in range(copies):
        (nzbdir / ("%s.nzb%s.queued" % (name, "" if c == 0 else ".%d" % (c + 1)))).write_bytes(nzb)
    nzbget.config_entries = [{"Name": "MainDir", "Value": str(tmp_path)}, {"Name": "NzbDir", "Value": "${MainDir}/nzbs"}]
    nzbget.history_items.append({"NZBID": 7, "Kind": "NZB", "NZBName": name, "NZBFilename": name + ".nzb",
                                 "DupeKey": key, "Status": "FAILURE/PAR"})


def test_posting_nzbget_already_has_is_not_sent_again(proxy, nzbget, hydra, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    donor = release(TITLE, prefix="r")
    _nzbget_already_has(nzbget, tmp_path, "Show S01E01 1080p WEB H264-GRP", donor)    # e.g. an earlier grab
    hydra.add(TITLE, donor)
    hydra.add(TITLE, release(TITLE, prefix="n"))
    append(proxy, primary())
    assert [base64.b64decode(d["params"][1]) for d in donors(nzbget)] == [release(TITLE, prefix="n")]
    assert "'in-nzbget': 1" in summary(caplog)


def test_same_release_under_another_key_also_counts(proxy, nzbget, hydra, tmp_path):
    donor = release(TITLE, prefix="r")
    _nzbget_already_has(nzbget, tmp_path, TITLE, donor, key="", copies=2)               # plain NZBGet grab, no key
    hydra.add(TITLE, donor)
    append(proxy, primary())
    assert donors(nzbget) == []


def test_listing_that_serves_a_different_nzb_is_flagged(proxy, nzbget, hydra, caplog):
    caplog.set_level(logging.INFO)
    donor = release(TITLE, prefix="r")
    hydra.add(TITLE, donor, size=nzb_total(donor) * 2)                                  # indexer lists 2x the size
    append(proxy, primary())
    assert len(donors(nzbget)) == 1                                                    # still a real, new posting
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "'listing-mismatch': 1" in summary(caplog) and "served a different NZB" in msgs


def test_mismatched_listing_flagged_even_when_it_is_a_known_posting(proxy, nzbget, hydra, caplog):
    caplog.set_level(logging.INFO)
    hydra.add(TITLE, primary(), size=nzb_total(primary()) * 2)       # serves the primary's NZB for another listing
    append(proxy, primary())
    s = summary(caplog)
    assert "'same-posting': 1" in s and "'listing-mismatch': 1" in s


def test_nzbget_copy_with_reuploaded_segment_still_counts(proxy, nzbget, hydra, tmp_path):
    donor = release(TITLE, prefix="r")
    _nzbget_already_has(nzbget, tmp_path, TITLE, donor.replace(b"r-0-0@x", b"refill@x"))
    hydra.add(TITLE, donor)
    append(proxy, primary())
    assert donors(nzbget) == []


def test_one_posting_listed_by_several_indexers_is_fetched_once(proxy, nzbget, hydra, caplog):
    # same size, same posting time: one posting, re-listed (each fetch costs an indexer grab)
    caplog.set_level(logging.INFO)
    when = time.time() - 86400
    for i, idx in enumerate(["slug", "finder", "ninja"]):
        hydra.add(TITLE, release(TITLE, prefix="r"), grabs=i, posted=when + i, indexer=idx)
    append(proxy, primary())
    assert len(hydra.fetches) == 1 and len(donors(nzbget)) == 1
    assert "'relisted': 2" in summary(caplog)


def test_same_size_reposts_at_other_times_are_all_fetched(proxy, nzbget, hydra):
    # byte-identical reposts have the same size but another posting time: the most useful donors
    for p in ("r1", "r2", "r3"):
        hydra.add(TITLE, release(TITLE, prefix=p))
    append(proxy, primary())
    assert len(hydra.fetches) == 3 and len(donors(nzbget)) == 3


def test_failed_fetch_falls_back_to_another_listing_of_the_same_posting(proxy, nzbget, hydra):
    when = time.time() - 86400
    hydra.add(TITLE, release(TITLE, prefix="r"), grabs=9, posted=when, status=403, indexer="capped")
    hydra.add(TITLE, release(TITLE, prefix="r"), grabs=1, posted=when + 5, indexer="other")
    append(proxy, primary())
    (d,) = donors(nzbget)
    assert base64.b64decode(d["params"][1]) == release(TITLE, prefix="r")


def test_indexer_that_refused_is_not_asked_again(proxy, nzbget, hydra, caplog):
    # a 403 from an indexer is its download limit: further grabs there only fail (and count against it)
    caplog.set_level(logging.INFO)
    hydra.add(TITLE, release(TITLE, prefix="a"), grabs=9, status=403, indexer="capped")
    for p in ("b", "c", "d"):
        hydra.add(TITLE, release(TITLE, prefix=p), status=403, indexer="capped")
    hydra.add(TITLE, release(TITLE, prefix="e"), indexer="open")
    append(proxy, primary())
    capped = [f for f in hydra.fetches if hydra.items[int(f.split("/")[2].split("?")[0])].indexer == "capped"]
    assert len(capped) == 2                                              # the first grab and its one retry
    assert len(donors(nzbget)) == 1 and "'refused': 3" in summary(caplog)
