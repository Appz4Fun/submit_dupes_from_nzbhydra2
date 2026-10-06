"""Watching nzbget's queue: picks submitted straight to nzbget (nzbdavkodi, manual uploads) get donors too."""
import base64

from tests.fakes import FakeNntp, article_ids, basic, release

TITLE = "Show.S01E01.1080p.WEB.H264-GRP"
KEY = "tvdbid=1-S01-E01|show-s01e01"
PICK = 23859118  # nzbdavkodi: seconds since 2026


def _pick(nzbget, tmp_path, nzb, nzbid=500, key=KEY, score=PICK, name=TITLE, status="DOWNLOADING"):
    nzbdir = tmp_path / "nzbs"
    nzbdir.mkdir(exist_ok=True)
    fname = name + ".nzb"
    (nzbdir / (fname + ".queued")).write_bytes(nzb)
    nzbget.config_entries = [e for e in nzbget.config_entries if e["Name"] != "NzbDir"] + [
        {"Name": "NzbDir", "Value": str(nzbdir)}]
    item = {"NZBID": nzbid, "Status": status, "DupeKey": key, "DupeScore": score, "NZBName": name,
            "NZBFilename": fname, "Category": "tv", "FileSizeLo": len(nzb), "FileSizeHi": 0}
    nzbget.queue_items.append(item)
    return item


def _watcher(make_proxy, **kw):
    return make_proxy(watch_settle=0, nzbget_username="admin", nzbget_password="pw", **kw)


def test_pick_submitted_straight_to_nzbget_gets_donors_under_its_key(make_proxy, nzbget, hydra, tmp_path):
    donor = release(TITLE, prefix="r")
    hydra.add(TITLE, donor)
    _pick(nzbget, tmp_path, release(TITLE, prefix="p"))
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(20)
    (d,) = nzbget.appends
    assert base64.b64decode(d["params"][1]) == donor
    assert d["params"][2] == "tv" and d["params"][6] == KEY
    assert d["params"][7] == PICK - 1000 + 90                       # under the pick and its own backups
    assert d["auth"] == basic("admin", "pw")


def test_each_pick_is_handled_once_and_backups_never(make_proxy, nzbget, hydra, tmp_path):
    hydra.add(TITLE, release(TITLE, prefix="r"))
    _pick(nzbget, tmp_path, release(TITLE, prefix="p"))
    _pick(nzbget, tmp_path, release(TITLE, prefix="b"), nzbid=501, score=PICK - 1, name=TITLE + ".b")  # promoted
    p = _watcher(make_proxy)
    for _ in range(3):
        p.watch_once()
        p.wait_idle(20)
    assert len(nzbget.appends) == 1
    assert sum("t=search" in q for q in hydra.queries) <= 3             # one discovery's queries


def test_items_the_proxy_appended_are_not_rediscovered(make_proxy, nzbget, hydra, tmp_path):
    from tests.fakes import append_body, post
    prim = release(TITLE, prefix="p")
    p = _watcher(make_proxy)
    post(p.url + "/jsonrpc", append_body(prim, title=TITLE), auth=("admin", "pw"))   # the Hydra path
    p.wait_idle(20)
    queries = len(hydra.queries)
    _pick(nzbget, tmp_path, prim, nzbid=nzbget.appends[0]["id"], key=nzbget.appends[0]["params"][6], score=100)
    p.watch_once()
    p.wait_idle(20)
    assert len(hydra.queries) == queries


def test_pick_without_dupekey_gets_one(make_proxy, nzbget, hydra, tmp_path):
    hydra.add(TITLE, release(TITLE, prefix="r"))
    _pick(nzbget, tmp_path, release(TITLE, prefix="p"), key="", score=0)
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(20)
    key = "dupes:show.s01e01.1080p.web.h264.grp"
    assert ("GroupSetDupeKey", key, [500]) in nzbget.edits
    assert nzbget.appends[0]["params"][6:9] == [key, 90, "SCORE"]   # base 0: plain donor scores under 100
    assert ("GroupSetDupeScore", "100", [500]) in nzbget.edits


def test_dead_pick_is_demoted_below_its_donors(make_proxy, nzbget, hydra, tmp_path):
    donor = release(TITLE, prefix="r")
    nzbget.config_entries = FakeNntp(article_ids(donor)).config(1)
    hydra.add(TITLE, donor)
    _pick(nzbget, tmp_path, release(TITLE, prefix="p"))
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(20)
    assert ("GroupSetDupeScore", str(PICK - 1000 + 1), [500]) in nzbget.edits


def test_items_past_download_are_left_alone(make_proxy, nzbget, hydra, tmp_path):
    hydra.add(TITLE, release(TITLE, prefix="r"))
    _pick(nzbget, tmp_path, release(TITLE, prefix="p"), status="PP_QUEUED")
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(20)
    assert nzbget.appends == [] and hydra.queries == []


def test_pick_whose_nzb_file_is_ambiguous_is_never_demoted(make_proxy, nzbget, hydra, tmp_path):
    # live (Industry S04E02): two postings of exactly the same size share one NZBFilename in NzbDir, so the
    # pick's own NZB can't be told apart by size; the dead one's probe must not demote an alive pick
    alive, dead = release(TITLE, prefix="p"), release(TITLE, prefix="q")
    nzbget.config_entries = FakeNntp(article_ids(alive)).config(1)
    _pick(nzbget, tmp_path, alive)
    (tmp_path / "nzbs" / (TITLE + ".nzb.2.queued")).write_bytes(dead)     # the twin, same size
    (tmp_path / "nzbs" / (TITLE + ".nzb.queued")).write_bytes(dead)       # sorted first: the one size picks
    (tmp_path / "nzbs" / (TITLE + ".nzb.3.queued")).write_bytes(alive)
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(20)
    assert not [e for e in nzbget.edits if e[0] == "GroupSetDupeScore" and e[2] == [500]]


def test_a_resubmitted_pick_is_searched_again(make_proxy, nzbget, hydra, tmp_path):
    # live (Lanterns S01E04): the client's pick and backups were removed, then the same NZB came back as a new
    # pick under a new NZBID; recording the old pick's fingerprint must not make the new one look like ours
    nzb = release(TITLE, prefix="p")
    _pick(nzbget, tmp_path, nzb, nzbid=500)
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(20)
    searches = sum("t=search" in q for q in hydra.queries)
    nzbget.queue_items.clear()
    hydra.add(TITLE, release(TITLE, prefix="r"))
    _pick(nzbget, tmp_path, nzb, nzbid=600, score=PICK + 5000)
    p.watch_once()
    p.wait_idle(20)
    assert sum("t=search" in q for q in hydra.queries) > searches
    assert [a["params"][6] for a in nzbget.appends] == [KEY]


def _backup(nzbget, tmp_path, nzb, nzbid, score, name, status="DELETED/DUPE", key=KEY):
    """A backup the submitter sent beside its pick: nzbget keeps it in history, its NZB in NzbDir."""
    fname = name + ".nzb"
    (tmp_path / "nzbs" / (fname + ".queued")).write_bytes(nzb)
    item = {"NZBID": nzbid, "Status": status, "DupeKey": key, "DupeScore": score, "NZBName": name, "Name": name,
            "NZBFilename": fname, "FileSizeLo": len(nzb), "FileSizeHi": 0}
    nzbget.history_items.append(item)
    return item


def test_backups_already_in_nzbget_are_ranked_by_health(make_proxy, nzbget, hydra, tmp_path):
    # live (Shrinking S02E07): nzbdavkodi sent 14 backups; nzbget tried them in its own score order and burned
    # through dead ones (569 failed articles each) while whole ones waited: rank them by health like donors
    pick, dead, whole = release(TITLE, prefix="p"), release(TITLE, prefix="d"), release(TITLE, prefix="w")
    nzbget.config_entries = FakeNntp(article_ids(pick) + article_ids(whole)).config(1)
    _pick(nzbget, tmp_path, pick)
    _backup(nzbget, tmp_path, dead, 501, PICK - 1, TITLE + ".d")
    _backup(nzbget, tmp_path, whole, 502, PICK - 2, TITLE + ".w")
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    scores = nzbget.final_scores()
    base = PICK - 1000
    assert scores[501] == base + 1                                  # dead: last
    assert base + 80 <= scores[502] <= base + 90                    # whole: first among backups
    assert ("HistorySetParameter", "DupeAlive=100", [502]) in nzbget.edits
    assert ("HistorySetParameter", "DupeAlive=0", [501]) in nzbget.edits


def test_backups_of_other_keys_and_the_pick_itself_are_not_ranked(make_proxy, nzbget, hydra, tmp_path):
    pick, other = release(TITLE, prefix="p"), release(TITLE, prefix="o")
    nzbget.config_entries = FakeNntp(article_ids(pick)).config(1)
    _pick(nzbget, tmp_path, pick)
    _backup(nzbget, tmp_path, other, 503, PICK - 1, TITLE + ".o", key="tvdbid=2-S01-E01|other")
    _backup(nzbget, tmp_path, other, 504, PICK - 3, TITLE + ".f", status="FAILURE/HEALTH")
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    assert not [e for e in nzbget.edits if e[2] in ([503], [504], [500]) and e[0].startswith("History")]


def test_a_forced_pick_is_switched_to_score_mode(make_proxy, nzbget, hydra, tmp_path):
    # live (Shrinking S02E07 4144): the pick came in with DupeMode FORCE, which turns off every failover
    item = _pick(nzbget, tmp_path, release(TITLE, prefix="p"))
    item["DupeMode"] = "FORCE"
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(20)
    assert ("GroupSetDupeMode", "SCORE", [500]) in nzbget.edits


def test_a_dead_backup_is_never_raised(make_proxy, nzbget, hydra, tmp_path):
    # live (Shrinking S02E07 4150): nzbdavkodi's backups sat ~2,500 below the pick; "dead = base+1" lifted
    # them by ~1,500, above where they were
    pick, dead = release(TITLE, prefix="p"), release(TITLE, prefix="d")
    nzbget.config_entries = FakeNntp(article_ids(pick)).config(1)
    _pick(nzbget, tmp_path, pick)
    _backup(nzbget, tmp_path, dead, 501, PICK - 2500, TITLE + ".d")
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    assert nzbget.final_scores().get(501, PICK - 2500) == PICK - 2500
    assert ("HistorySetParameter", "DupeAlive=0", [501]) in nzbget.edits


def _part(nzb, share):
    ids = article_ids(nzb)
    return ids[:int(len(ids) * share)]


def test_a_pick_sure_to_fail_is_swapped_for_a_whole_backup(make_proxy, nzbget, hydra, tmp_path):
    # live (Industry S01E06 4151): 31% of its articles arrived but nzbget's health read 97% (failures are a small
    # share of the whole), so it would crawl for hours before failing while a 100% backup waited
    pick, whole = release(TITLE, prefix="p"), release(TITLE, prefix="w")
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + article_ids(whole)).config(1)
    _pick(nzbget, tmp_path, pick)
    _backup(nzbget, tmp_path, whole, 502, PICK - 2, TITLE + ".w")
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    assert ("HistoryRedownload", "", [502]) in nzbget.edits
    assert ("GroupDelete", "", [500]) in nzbget.edits
    assert nzbget.edits.index(("HistoryRedownload", "", [502])) < nzbget.edits.index(("GroupDelete", "", [500]))


def test_a_mostly_whole_pick_is_not_swapped(make_proxy, nzbget, hydra, tmp_path):
    pick, whole = release(TITLE, prefix="p"), release(TITLE, prefix="w")
    nzbget.config_entries = FakeNntp(_part(pick, 0.97) + article_ids(whole)).config(1)
    _pick(nzbget, tmp_path, pick)
    _backup(nzbget, tmp_path, whole, 502, PICK - 2, TITLE + ".w")
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    assert not [e for e in nzbget.edits if e[0] in ("HistoryRedownload", "GroupDelete")]


def test_a_failing_pick_is_not_swapped_for_a_backup_no_better(make_proxy, nzbget, hydra, tmp_path):
    pick, half = release(TITLE, prefix="p"), release(TITLE, prefix="h")
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + _part(half, 0.5)).config(1)
    _pick(nzbget, tmp_path, pick)
    _backup(nzbget, tmp_path, half, 502, PICK - 2, TITLE + ".h")
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    assert not [e for e in nzbget.edits if e[0] in ("HistoryRedownload", "GroupDelete")]


def test_a_skipped_copy_scored_above_the_pick_does_not_hide_it(make_proxy, nzbget, hydra, tmp_path):
    # live (Industry S04E06 4155): nzbdavkodi sent the same NZB twice; nzbget filed the one scored 8 higher as
    # DELETED/COPY and downloads the other, which then looked like a backup and was never searched
    hydra.add(TITLE, release(TITLE, prefix="r"))
    nzb = release(TITLE, prefix="p")
    _pick(nzbget, tmp_path, nzb)
    _backup(nzbget, tmp_path, nzb, 499, PICK + 8, TITLE, status="DELETED/COPY")
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(20)
    assert len(nzbget.appends) == 1
