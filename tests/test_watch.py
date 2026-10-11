"""Watching nzbget's queue: picks submitted straight to nzbget (nzbdavkodi, manual uploads) get donors too."""
import base64
import os

import pytest

import nzbget_dupe_proxy as ndp
from tests.fakes import FakeNntp, article_ids, basic, make_nzb, par2_main, release

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


def test_a_twin_backup_gets_its_siblings_rank_not_its_old_score(make_proxy, nzbget, hydra, tmp_path):
    # live (Fury 4878): a backup with the same NZB as another was skipped by the ranking and kept its old score,
    # which sat above every backup that had been checked: it would have been tried first, unchecked
    pick, whole, other = release(TITLE, prefix="p"), release(TITLE, prefix="w"), release(TITLE, prefix="o")
    nzbget.config_entries = FakeNntp(article_ids(pick) + article_ids(whole) + article_ids(other)).config(1)
    _pick(nzbget, tmp_path, pick)
    _backup(nzbget, tmp_path, whole, 502, PICK - 2, TITLE + ".w")
    _backup(nzbget, tmp_path, whole, 503, PICK - 1, TITLE + ".t")   # the twin: same NZB, the highest old score
    _backup(nzbget, tmp_path, other, 504, PICK - 3, TITLE + ".o")
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    scores = nzbget.final_scores()
    base = PICK - 1000
    assert base + 1 < scores[503] < base + 100                      # ranked like a donor, not left at PICK - 1
    assert abs(scores[503] - scores[502]) <= 1                      # the twin shares its sibling's rank
    assert scores[503] < PICK - 500


def test_a_donor_and_a_backup_that_are_equally_whole_never_tie(make_proxy, nzbget, hydra, tmp_path):
    # rank_backups scores the client's backups and discover scores the donors: both draw from one set of
    # scores, so two equally healthy postings must not end on the same DupeScore (a tie has no failover order)
    pick, back, donor = release(TITLE, prefix="p"), release(TITLE, prefix="w"), release(TITLE, prefix="d")
    nzbget.config_entries = FakeNntp(article_ids(pick) + article_ids(back) + article_ids(donor)).config(1)
    hydra.add(TITLE, donor)
    _pick(nzbget, tmp_path, pick)
    _backup(nzbget, tmp_path, back, 502, PICK - 2, TITLE + ".w")
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    scores = nzbget.final_scores()
    ranked = [v for k, v in scores.items() if k != 500]
    assert len(ranked) >= 2 and len(set(ranked)) == len(ranked), scores


def test_a_pick_whose_search_was_killed_midway_is_searched_again_after_a_restart(make_proxy, nzbget, hydra, tmp_path, monkeypatch):
    # pick_job records the pick in the state file before discover runs: a worker killed by an nzbget restart
    # (systemd stops the whole service group) left the pick looking handled, so no sweep ever searched it
    pick = release(TITLE, prefix="p")
    _pick(nzbget, tmp_path, pick)
    p = _watcher(make_proxy)
    path, auth = "/jsonrpc", p.watch_auth()
    queue = p.rpc_call(path, auth, "listgroups", [0])
    assert p.pick_job(path, auth, queue[0], queue, []) is not None   # the worker starts ... and is killed here
    import nzbget_dupe_proxy as ndp
    monkeypatch.setattr(ndp, "SEARCH_STALE", 0)                      # the unfinished search counts as stale
    restarted = type(p)(p.cfg)                                       # a new process reads the state file
    again = restarted.pick_job(path, auth, queue[0], queue, [])
    assert again is not None                                         # the unfinished search must be retried


def test_a_fleet_ranked_pick_is_left_to_nzbget(make_proxy, nzbget, hydra, tmp_path):
    # nzbget's appendfleet already measured and ranked the copies and tags each with DupeFleet=<rank>; the
    # extension must not run a second discovery/ranking for that key (it would race and tie the scores, B87)
    hydra.add(TITLE, release(TITLE, prefix="r"))
    item = _pick(nzbget, tmp_path, release(TITLE, prefix="p"))
    item["Parameters"] = [{"Name": "DupeFleet", "Value": "1"}]
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(10)
    assert nzbget.appends == []                                     # no donors added
    assert not any("t=search" in q for q in hydra.queries)          # no indexer search


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
    assert ("GroupDupeDelete", "", [500]) in nzbget.edits
    assert nzbget.edits.index(("HistoryRedownload", "", [502])) < nzbget.edits.index(("GroupDupeDelete", "", [500]))


def test_a_swapped_out_pick_stays_a_dupe_backup_scored_by_its_health(make_proxy, nzbget, hydra, tmp_path):
    # live (Ted Lasso S04E05 6995): the swap GroupDelete'd the pick; nzbget filed it DELETED/MANUAL, which its
    # failover (ReturnBestDupe takes only dupe-deleted backups) never returns, and history read as a manual delete
    pick, whole = release(TITLE, prefix="p"), release(TITLE, prefix="w")
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + article_ids(whole)).config(1)
    _pick(nzbget, tmp_path, pick)
    _backup(nzbget, tmp_path, whole, 502, PICK - 2, TITLE + ".w")
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    assert ("GroupDupeDelete", "", [500]) in nzbget.edits
    assert not [e for e in nzbget.edits if e[0] == "GroupDelete"]
    rescored = [i for i, e in enumerate(nzbget.edits) if e[0].endswith("SetDupeScore") and e[2] == [500]]
    assert rescored and rescored[0] < nzbget.edits.index(("GroupDupeDelete", "", [500]))
    scores = nzbget.final_scores()
    assert scores[500] < scores[502]


def test_a_backup_of_unknown_health_is_ranked_below_a_whole_one(make_proxy, nzbget, hydra, tmp_path):
    # live (Ted Lasso S04E06 7111): backups the check could not answer (a saturated server reads unknown, B90)
    # kept the submitter's scores, just under the pick and above every ranked one, so nzbget's failover took
    # an unchecked backup over one sampled 100% whole
    pick, whole, unknown = release(TITLE, prefix="p"), release(TITLE, prefix="w"), release(TITLE, prefix="u")
    nzbget.config_entries = FakeNntp(article_ids(pick) + article_ids(whole), missing_code=481).config(1)
    _pick(nzbget, tmp_path, pick)
    _backup(nzbget, tmp_path, unknown, 501, PICK - 1, TITLE + ".u")  # the submitter scored it higher
    _backup(nzbget, tmp_path, whole, 502, PICK - 2, TITLE + ".w")
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    scores = nzbget.final_scores()
    base = ndp.score_base(PICK)
    assert base + 1 < scores.get(501, PICK - 1) < scores[502]


def test_a_backup_whose_nzb_is_unreadable_still_leaves_the_slot_under_the_pick(make_proxy, nzbget, hydra, tmp_path):
    # the only backup's NZB is gone from NzbDir: its health can't be read, so it goes to the ranked band too,
    # also when ranking without the pick's own sample (the rescue path)
    from collections import Counter
    pick, gone = release(TITLE, prefix="p"), release(TITLE, prefix="g")
    nzbget.config_entries = FakeNntp(article_ids(pick)).config(1)
    _pick(nzbget, tmp_path, pick)
    _backup(nzbget, tmp_path, gone, 501, PICK - 1, TITLE + ".g")
    (tmp_path / "nzbs" / (TITLE + ".g.nzb.queued")).unlink()
    p = _watcher(make_proxy)
    base = ndp.score_base(PICK)
    servers = p.news_servers(nzbget.config_entries, TITLE)
    p.rank_backups(servers, "/jsonrpc", p.watch_auth(), KEY, TITLE, 500, ndp.Ranks(), Counter(), base)
    assert base + 1 < nzbget.final_scores().get(501, PICK - 1) <= base + 90


def test_a_backup_whose_nzb_lists_only_part_of_its_files_is_not_ranked_whole(make_proxy, nzbget, hydra, tmp_path):
    # live (Puppy Place S02E08 7223): every listed article was on the servers, so a sample read 100%, but the
    # NZB listed 1,510 of the file's 3,709 parts and the download could only fail
    pick, whole = release(TITLE, prefix="p"), release(TITLE, prefix="w")
    partial = release(TITLE, prefix="q").replace(b"yEnc (1/20)", b"yEnc (1/150)")  # 13% of each file listed
    nzbget.config_entries = FakeNntp(article_ids(pick) + article_ids(whole) + article_ids(partial)).config(1)
    _pick(nzbget, tmp_path, pick)
    _backup(nzbget, tmp_path, partial, 501, PICK - 1, TITLE + ".q")
    _backup(nzbget, tmp_path, whole, 502, PICK - 2, TITLE + ".w")
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    scores, base = nzbget.final_scores(), ndp.score_base(PICK)
    assert scores[501] == base + 1 < scores[502]  # 13% whole is dead, like any posting mostly gone


def test_a_pick_whose_nzb_lists_only_part_of_its_files_is_swapped_for_a_whole_backup(make_proxy, nzbget, hydra,
                                                                                     tmp_path):
    # live (Puppy Place S02E08 7223): the pick's 1,510 listed articles were all there, so it downloaded 1.5 GB
    # and failed on the 2,199 parts its NZB never listed
    pick = release(TITLE, prefix="p").replace(b"yEnc (1/20)", b"yEnc (1/150)")
    whole = release(TITLE, prefix="w")
    nzbget.config_entries = FakeNntp(article_ids(pick) + article_ids(whole)).config(1)
    _pick(nzbget, tmp_path, pick)
    _backup(nzbget, tmp_path, whole, 502, PICK - 2, TITLE + ".w")
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    assert ("HistoryRedownload", "", [502]) in nzbget.edits
    assert ("GroupDupeDelete", "", [500]) in nzbget.edits


def _big_block_pick(block_articles, recovery_blocks=2):
    """400 data articles (every 20th gone), an index par2 and a recovery volume; blocks of `block_articles`."""
    art = 768000
    nzb = make_nzb([(TITLE + ".mkv", [art] * 400), (TITLE + ".vol-06.par2", [10000]),
                    (TITLE + ".vol-01.par2", [art * block_articles] * recovery_blocks)], prefix="p")
    ids = article_ids(nzb)
    data_ids = [i for i in ids if i.startswith("p-0-")]
    present = [i for n, i in enumerate(data_ids) if n % 20] + [i for i in ids if not i.startswith("p-0-")]
    return nzb, present, {"p-1-0@x": par2_main(art * block_articles)}


def test_a_pick_whose_damage_outruns_its_pars_is_swapped_though_mostly_whole(make_proxy, nzbget, hydra, tmp_path):
    # live (Joker Folie a Deux 7826, House of the Dragon 7896): 93..98% of articles there, but 15..25 MB par
    # blocks put the damage at 33..87% of blocks, far past the pars; nzbget downloaded 7..11 GB, then failed par
    pick, present, payloads = _big_block_pick(20)       # 95% alive, 20 articles per block, 2 recovery blocks
    whole = release(TITLE, prefix="w")
    nzbget.config_entries = FakeNntp(present + article_ids(whole), payloads=payloads).config(1)
    _pick(nzbget, tmp_path, pick)
    _backup(nzbget, tmp_path, whole, 502, PICK - 2, TITLE + ".w")
    p = _watcher(make_proxy, health_min_articles=500)
    p.watch_once()
    p.wait_idle(30)
    assert ("HistoryRedownload", "", [502]) in nzbget.edits
    assert ("GroupDupeDelete", "", [500]) in nzbget.edits


def test_a_par_doomed_pick_is_not_swapped_for_a_backup_that_is_not_whole(make_proxy, nzbget, hydra, tmp_path):
    # with big blocks a 97% backup is as doomed as the 95% pick: the par swap only goes to an all-there backup
    pick, present, payloads = _big_block_pick(20)
    most = release(TITLE, prefix="m")
    nzbget.config_entries = FakeNntp(present + _part(most, 0.97), payloads=payloads).config(1)
    _pick(nzbget, tmp_path, pick)
    _backup(nzbget, tmp_path, most, 502, PICK - 2, TITLE + ".m")
    p = _watcher(make_proxy, health_min_articles=500)
    p.watch_once()
    p.wait_idle(30)
    assert not [e for e in nzbget.edits if e[0] in ("HistoryRedownload", "GroupDelete", "GroupDupeDelete")]


def test_a_mostly_whole_pick_with_small_par_blocks_is_not_swapped(make_proxy, nzbget, hydra, tmp_path):
    pick, present, payloads = _big_block_pick(1, 30)    # same 95%, one article per block: 20 bad, 30 to fix them
    whole = release(TITLE, prefix="w")
    nzbget.config_entries = FakeNntp(present + article_ids(whole), payloads=payloads).config(1)
    _pick(nzbget, tmp_path, pick)
    _backup(nzbget, tmp_path, whole, 502, PICK - 2, TITLE + ".w")
    p = _watcher(make_proxy, health_min_articles=500)
    p.watch_once()
    p.wait_idle(30)
    assert not [e for e in nzbget.edits if e[0] in ("HistoryRedownload", "GroupDelete", "GroupDupeDelete")]


def test_a_mostly_whole_pick_is_not_swapped(make_proxy, nzbget, hydra, tmp_path):
    pick, whole = release(TITLE, prefix="p"), release(TITLE, prefix="w")
    nzbget.config_entries = FakeNntp(_part(pick, 0.97) + article_ids(whole)).config(1)
    _pick(nzbget, tmp_path, pick)
    _backup(nzbget, tmp_path, whole, 502, PICK - 2, TITLE + ".w")
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    assert not [e for e in nzbget.edits if e[0] in ("HistoryRedownload", "GroupDelete", "GroupDupeDelete")]


def test_a_failing_pick_is_not_swapped_for_a_backup_no_better(make_proxy, nzbget, hydra, tmp_path):
    pick, half = release(TITLE, prefix="p"), release(TITLE, prefix="h")
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + _part(half, 0.5)).config(1)
    _pick(nzbget, tmp_path, pick)
    _backup(nzbget, tmp_path, half, 502, PICK - 2, TITLE + ".h")
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    assert not [e for e in nzbget.edits if e[0] in ("HistoryRedownload", "GroupDelete", "GroupDupeDelete")]


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


def test_a_failing_pick_is_swapped_for_a_whole_donor_found_by_the_search(make_proxy, nzbget, hydra, tmp_path):
    # live (Foundation S03E08 4185): the pick sampled 39% alive, the client's one backup 38%; the search added
    # three 100% donors, but the swap only looked at the client's backups: "no backup is whole enough"
    pick, donor = release(TITLE, prefix="p"), release(TITLE, prefix="r")
    hydra.add(TITLE, donor)
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + article_ids(donor)).config(1)
    _pick(nzbget, tmp_path, pick)
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    (d,) = nzbget.appends
    assert ("HistoryRedownload", "", [d["id"]]) in nzbget.edits
    assert ("GroupDupeDelete", "", [500]) in nzbget.edits


def test_a_pick_whose_name_has_slashes_and_quotes_is_still_found_in_nzbdir(make_proxy, nzbget, hydra, tmp_path):
    # live (Ted Lasso S03E01 4253): nzbget reports NZBFilename '[1/2] "X.mkv".nzb' but stores the copy as
    # '2] _X.mkv_.nzb.queued' (slash, quotes sanitized), so the lookup by NZBFilename found nothing ("no readable
    # NZB"), the pick was never searched: 8,895 failed articles and no donors
    nzb = release(TITLE, prefix="p")
    nzbdir = tmp_path / "nzbs"
    nzbdir.mkdir()
    (nzbdir / ("2] _" + TITLE + ".mkv_.nzb.queued")).write_bytes(nzb)
    nzbget.config_entries = [{"Name": "NzbDir", "Value": str(nzbdir)}]
    item = {"NZBID": 500, "Status": "DOWNLOADING", "NZBName": "2] _" + TITLE + ".mkv_",
            "NZBFilename": '[1/2] "' + TITLE + '.mkv".nzb', "FileSizeLo": len(nzb), "FileSizeHi": 0}
    p = _watcher(make_proxy)
    info, ambiguous = p.queued_nzb("/jsonrpc", p.watch_auth(), item)
    assert info is not None and not ambiguous


def test_equally_healthy_backups_rank_by_closeness_to_the_picks_size(make_proxy, nzbget, hydra, tmp_path):
    # live (Slow Horses S05E01): a 16 GB backup ranked above two backups the pick's own size, because the
    # client's backups were scored by health alone; a failover would pull a far bigger, different file
    pick = release(TITLE, prefix="p")
    far = release(TITLE, prefix="f", segs_per_file=60)     # about 3x the size
    near = release(TITLE, prefix="n")
    nzbget.config_entries = FakeNntp(article_ids(pick) + article_ids(far) + article_ids(near)).config(1)
    _pick(nzbget, tmp_path, pick)
    _backup(nzbget, tmp_path, far, 501, PICK - 1, TITLE + ".f")      # the client scored the far one higher
    _backup(nzbget, tmp_path, near, 502, PICK - 2, TITLE + ".n")
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    scores = nzbget.final_scores()
    assert scores[502] > scores[501]


def test_a_pick_already_out_of_the_queue_is_never_swapped(make_proxy, nzbget, hydra, tmp_path):
    # live (Slow Horses S05E01 4256): the server downloaded the pick in about a minute, discovery finished three
    # minutes in, and the swap then returned a backup for a pick already in history as SUCCESS/HEALTH
    from donor_health import Health
    p = _watcher(make_proxy)
    nzbget.history_items.append({"NZBID": 500, "Status": "SUCCESS/HEALTH", "DupeKey": KEY, "DupeScore": PICK})
    doomed = Health(checked=20, present=1, missing=19, error=0)
    got = p.swap_if_failing("/jsonrpc", p.watch_auth(), TITLE, 500, doomed, {501: (80, 1.0, False)})
    assert got == 0
    assert not [e for e in nzbget.edits if e[0] in ("HistoryRedownload", "GroupDelete", "GroupDupeDelete")]


def test_a_pick_almost_downloaded_and_above_critical_health_is_not_swapped(make_proxy, nzbget, hydra, tmp_path):
    # live (Last Seen S01E03 4661): discovery took 2.5 min, the pick had downloaded 95% of its articles (health
    # above critical) when the swap, going by its own 82% sample, deleted it and started a copy from zero
    from donor_health import Health
    p = _watcher(make_proxy)
    nzbget.queue_items.append({"NZBID": 500, "Status": "DOWNLOADING", "DupeKey": KEY, "DupeScore": PICK,
                               "FileSizeMB": 10000, "DownloadedSizeMB": 9600, "Health": 952, "CriticalHealth": 800})
    doomed = Health(checked=100, present=82, missing=18, error=0)
    got = p.swap_if_failing("/jsonrpc", p.watch_auth(), TITLE, 500, doomed, {501: (80, 1.0, False)})
    assert got == 0
    assert not [e for e in nzbget.edits if e[0] in ("HistoryRedownload", "GroupDelete", "GroupDupeDelete")]


def test_a_pick_renamed_after_its_search_is_searched_again_under_its_new_name(make_proxy, nzbget, hydra, tmp_path):
    # live (How to Make a Killing, nzbid 8188): the pick came in under a junk name, found no donors, then nzbget
    # showed its real name; every later look said "handled already", so no search ever used the real name
    old = "Junk.Upload.Name"
    item = _pick(nzbget, tmp_path, release(TITLE, prefix="p"), key="", name=old)
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(20)
    assert not nzbget.appends
    item.update(NZBName=TITLE, DupeKey="dupes:" + ndp.normalize_title(old))
    donor = release(TITLE, prefix="r")
    hydra.add(TITLE, donor)
    p.watch_once()
    p.wait_idle(20)
    (d,) = nzbget.appends
    assert base64.b64decode(d["params"][1]) == donor
    assert d["params"][6] == "dupes:" + ndp.normalize_title(TITLE)
    assert ("GroupSetDupeKey", "dupes:" + ndp.normalize_title(TITLE), [500]) in nzbget.edits



def test_the_search_deadline_leaves_big_nzbs_time_to_arrive():
    # live (How to Make a Killing, nzbid 8188): Hydra's search took 35 s of a 60 s DEADLINE and all five REMUX
    # donor NZBs timed out; discovery runs in the background, so nothing waits on a longer one
    assert ndp.Config().deadline == 600


def test_a_pick_whose_search_found_no_donors_is_searched_again_later(make_proxy, nzbget, hydra, tmp_path, monkeypatch):
    # live (nzbid 8188): one search with no donors, then nothing ever looked again while the pick kept failing
    _pick(nzbget, tmp_path, release(TITLE, prefix="p"))
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(20)
    assert not nzbget.appends
    donor = release(TITLE, prefix="r")
    hydra.add(TITLE, donor)
    p.watch_once()
    p.wait_idle(20)
    assert not nzbget.appends                                       # not before RESEARCH_EMPTY has passed
    monkeypatch.setattr(ndp, "RESEARCH_EMPTY", 0)
    p.watch_once()
    p.wait_idle(20)
    (d,) = nzbget.appends
    assert base64.b64decode(d["params"][1]) == donor
    p.watch_once()
    p.wait_idle(20)
    assert len(nzbget.appends) == 1                                 # donors found: searched no more


RENAMED = "Show S01E01 1080p WEB H264"  # an indexer's name for it: no group, so not "the same release"


def test_a_failing_pick_never_takes_an_obfuscated_posting_it_cannot_look_inside(make_proxy, nzbget, hydra, tmp_path):
    # Codex review (PR 1): as many files of about the same size is no proof: another encode of the episode can
    # look the same from outside. Without the packed file read from both, the pick fails rather than turn
    # into something else
    pick, other = release(TITLE, prefix="p"), release(TITLE, prefix="r", obfuscate=True)
    hydra.add(RENAMED, other)
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + article_ids(other)).config(1)
    _pick(nzbget, tmp_path, pick)
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    assert not nzbget.appends


def test_a_whole_pick_never_takes_an_obfuscated_posting(make_proxy, nzbget, hydra, tmp_path):
    pick, other = release(TITLE, prefix="p"), release(TITLE, prefix="r", obfuscate=True)
    hydra.add(RENAMED, other)
    nzbget.config_entries = FakeNntp(article_ids(pick) + article_ids(other)).config(1)
    _pick(nzbget, tmp_path, pick)
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    assert not nzbget.appends


def test_a_failing_pick_never_takes_an_obfuscated_posting_of_other_files(make_proxy, nzbget, hydra, tmp_path):
    # the looser search still only takes the release asked for: listed at the pick's size, but the NZB holds
    # another set of files, so the pick fails rather than turning into something else
    pick = release(TITLE, prefix="p")
    other = release(TITLE, prefix="r", obfuscate=True, n_files=12, segs_per_file=17)
    hydra.add(RENAMED, other, size=ndp.parse_nzb(pick).total_bytes)
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + article_ids(other)).config(1)
    _pick(nzbget, tmp_path, pick)
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    assert not nzbget.appends
    assert ("GroupDupeDelete", "", [500]) not in nzbget.edits


def _with_pars(name, prefix, par_bytes):
    """release(name) plus a recovery volume: another posting's par2 changes the total, not the packed file."""
    rars = [("%s.part%02d.rar" % (name, i + 1), [700000] * 20) for i in range(10)]
    return make_nzb(rars + [(name + ".vol00+40.par2", [par_bytes] * 4)], prefix=prefix)


def test_a_failing_pick_is_swapped_for_a_posting_of_another_size_holding_the_same_mkv(make_proxy, nzbget, hydra,
                                                                                         tmp_path):
    # what matters is the file inside the archive: another poster's par2 volumes make the posting 40 MB bigger,
    # but a rar holding an .mkv of exactly the pick's size is the same release
    from tests.fakes import rar5_head
    pick, other = release(TITLE, prefix="p"), _with_pars(TITLE, "r", 10000000)
    hydra.add(RENAMED, other)
    payloads = {"p-0-0@x": rar5_head(TITLE + ".mkv", 1399900000), "r-0-0@x": rar5_head("x.mkv", 1399900000)}
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + article_ids(other), payloads=payloads).config(1)
    _pick(nzbget, tmp_path, pick)
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    (d,) = nzbget.appends
    assert base64.b64decode(d["params"][1]) == other
    assert ("HistoryRedownload", "", [d["id"]]) in nzbget.edits


def test_a_failing_pick_never_takes_a_posting_whose_mkv_differs_in_size(make_proxy, nzbget, hydra, tmp_path):
    from tests.fakes import rar5_head
    pick, other = release(TITLE, prefix="p"), release(TITLE, prefix="r")
    hydra.add(RENAMED, other)
    payloads = {"p-0-0@x": rar5_head(TITLE + ".mkv", 1399900000), "r-0-0@x": rar5_head(TITLE + ".mkv", 139989999)}
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + article_ids(other), payloads=payloads).config(1)
    _pick(nzbget, tmp_path, pick)
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    assert not nzbget.appends


def test_a_posting_whose_files_name_another_group_is_never_taken_though_its_mkv_matches(make_proxy, nzbget, hydra,
                                                                                       tmp_path):
    # a name that says another group (or resolution, or HDR/DV format) is trusted: it is another release
    from tests.fakes import rar5_head
    pick, other = release(TITLE, prefix="p"), release("Show.S01E01.1080p.WEB.H264-OTHER", prefix="r")
    hydra.add(RENAMED, other)
    payloads = {"p-0-0@x": rar5_head(TITLE + ".mkv", 1399900000), "r-0-0@x": rar5_head("y.mkv", 1399900000)}
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + article_ids(other), payloads=payloads).config(1)
    _pick(nzbget, tmp_path, pick)
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    assert not nzbget.appends


@pytest.mark.parametrize("listed", ["Show.S01E01.1080p.WEB.H264-OTHER", "Show.S01E01.2160p.WEB.H264-GRP",
                                    "Show.S01E01.1080p.WEB.DV.H264-GRP"])
def test_a_listing_naming_another_group_resolution_or_hdr_is_never_fetched(make_proxy, nzbget, hydra, tmp_path,
                                                                           listed):
    pick = release(TITLE, prefix="p")
    hydra.add(listed, release(TITLE, prefix="r", obfuscate=True))
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + article_ids(release(TITLE, prefix="r"))).config(1)
    _pick(nzbget, tmp_path, pick)
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    assert not hydra.fetches


def _pp(append):
    return {p["Name"]: p["Value"] for p in append["params"][9]}


def test_a_donor_with_the_picks_data_files_is_hinted_dupe_same_size(make_proxy, nzbget, hydra, tmp_path):
    # nzbget's par2-from-a-twin tries hinted duplicates first (it still checks the par2 checksums): a donor
    # with the pick's data files, byte for byte in size, even with its own par2 volumes, gets DupeSameSize=1
    pick = _with_pars(TITLE, "p", 5000000)
    twin, alt = _with_pars(TITLE, "t", 9000000), release(TITLE, prefix="a", segs_per_file=21)
    hydra.add(TITLE, twin)
    hydra.add(TITLE, alt)
    nzbget.config_entries = FakeNntp(article_ids(pick) + article_ids(twin) + article_ids(alt)).config(1)
    _pick(nzbget, tmp_path, pick)
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    hints = {base64.b64decode(a["params"][1]): _pp(a) for a in nzbget.appends}
    assert hints[twin].get("DupeSameSize") == "1"
    assert "DupeSameSize" not in hints[alt]


def test_a_donor_whose_packed_file_matches_is_hinted_dupe_same_inner(make_proxy, nzbget, hydra, tmp_path):
    from tests.fakes import rar5_head
    pick, other = release(TITLE, prefix="p"), _with_pars(TITLE, "r", 10000000)
    hydra.add(RENAMED, other)
    payloads = {"p-0-0@x": rar5_head(TITLE + ".mkv", 139990000), "r-0-0@x": rar5_head("x.mkv", 139990000)}
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + article_ids(other), payloads=payloads).config(1)
    _pick(nzbget, tmp_path, pick)
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    (d,) = nzbget.appends
    assert _pp(d).get("DupeSameInner") == "1"


def test_the_lenient_pass_keeps_max_donors_for_the_whole_release(make_proxy, nzbget, hydra, tmp_path):
    # Codex review (PR 1): the lenient pass counted from 0 again, so MAX_DONORS=1 could add two donors
    from tests.fakes import rar5_head
    pick, half, other = release(TITLE, prefix="p"), release(TITLE, prefix="h"), _with_pars(TITLE, "r", 10000000)
    hydra.add(TITLE, half)
    hydra.add(RENAMED, other)
    payloads = {"p-0-0@x": rar5_head(TITLE + ".mkv", 1399900000), "r-0-0@x": rar5_head("x.mkv", 1399900000)}
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + _part(half, 0.6) + article_ids(other),
                                     payloads=payloads).config(1)
    _pick(nzbget, tmp_path, pick)
    p = _watcher(make_proxy, max_donors=1)
    p.watch_once()
    p.wait_idle(30)
    assert len(nzbget.appends) == 1


def test_a_lenient_donor_never_ties_a_backup_ranked_in_the_first_pass(make_proxy, nzbget, hydra, tmp_path):
    # Codex review (PR 1): the lenient pass drew scores from a fresh Ranks, so it could hand out a score a
    # backup ranked in the first pass already holds: a tie has no failover order
    from tests.fakes import rar5_head
    pick, back, other = release(TITLE, prefix="p"), release(TITLE, prefix="b"), _with_pars(TITLE, "r", 10000000)
    hydra.add(RENAMED, other)
    payloads = {"p-0-0@x": rar5_head(TITLE + ".mkv", 1399900000), "r-0-0@x": rar5_head("x.mkv", 1399900000)}
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + _part(back, 0.9) + article_ids(other),
                                     payloads=payloads).config(1)
    _pick(nzbget, tmp_path, pick)
    _backup(nzbget, tmp_path, back, 502, PICK - 2, TITLE + ".b")
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    scores = nzbget.final_scores()
    ranked = [v for k, v in scores.items() if k != 500]
    assert len(ranked) >= 2 and len(set(ranked)) == len(ranked), scores


MOVIE = "Quiet.Harbour.1080p.WEB.H264-GRP"   # no year or episode: short_query() has nothing to cut at


def test_the_lenient_pass_also_searches_by_title_alone(make_proxy, nzbget, hydra, tmp_path):
    # Codex review (PR 1): with no episode/year marker the lenient pass sent only the full-name query again,
    # so a posting listed under another name could never come back
    from tests.fakes import rar5_head
    pick, other = release(MOVIE, prefix="p"), _with_pars(MOVIE, "r", 10000000)
    hydra.add("Quiet Harbour 1080p WEB", other)
    payloads = {"p-0-0@x": rar5_head(MOVIE + ".mkv", 1399900000), "r-0-0@x": rar5_head("x.mkv", 1399900000)}
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + article_ids(other), payloads=payloads).config(1)
    _pick(nzbget, tmp_path, pick, name=MOVIE)
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    (d,) = nzbget.appends
    assert base64.b64decode(d["params"][1]) == other


def test_a_truncated_pick_is_compared_by_its_whole_size():
    # Codex review (PR 1): a pick whose NZB lists 40% of its parts was compared by its listed bytes, so a whole
    # obfuscated repost (2.5 times as big) fell outside the 1% gate before its rar could be read
    import dataclasses
    info = dataclasses.replace(ndp.parse_nzb(release(TITLE, prefix="p")), listed=0.4)
    whole = type("R", (), {"title": "a8f3c9e1b7d24f60", "size": round(info.total_bytes / 0.4)})()
    assert not ndp.candidate_ok(TITLE, info.total_bytes, whole, 0, True)
    assert ndp.candidate_ok(TITLE, ndp.whole_bytes(info), whole, 0, True)


def test_reading_a_packed_file_stops_at_the_discovery_deadline(make_proxy, monkeypatch):
    # Codex review (PR 1): each head read waited 30 s per server whatever the deadline, so a stalled server
    # kept a failing pick's rescue running for minutes past DEADLINE
    import time as _time
    waits = []
    monkeypatch.setattr(ndp.donor_health, "fetch_body", lambda servers, sid, timeout=30.0, sized=False:
                        waits.append(timeout))
    p = _watcher(make_proxy)
    info = ndp.parse_nzb(release(TITLE, prefix="p"))
    assert p.inner(["srv"], info, _time.time() - 1) is None and waits == []
    p.inner(["srv"], ndp.parse_nzb(release(TITLE, prefix="q")), _time.time() + 5)
    assert waits and max(waits) <= 5


def test_the_lenient_pass_spends_its_fetches_on_postings_the_first_pass_did_not_try(make_proxy, nzbget, hydra,
                                                                                     tmp_path):
    # Codex review (PR 1): with MAX_DONORS=1 three dead postings by the pick's name took all three fetch slots
    # again in the lenient pass, so the whole renamed posting was never fetched
    from tests.fakes import rar5_head
    pick, other = release(TITLE, prefix="p"), _with_pars(TITLE, "r", 10000000)
    for i in range(3):
        hydra.add(TITLE, release(TITLE, prefix="d%d" % i, extra_bytes=i + 1))
    hydra.add(RENAMED, other)
    payloads = {"p-0-0@x": rar5_head(TITLE + ".mkv", 1399900000), "r-0-0@x": rar5_head("x.mkv", 1399900000)}
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + article_ids(other), payloads=payloads).config(1)
    _pick(nzbget, tmp_path, pick)
    p = _watcher(make_proxy, max_donors=2)  # 3 * 2 grabs for the release: 3 dead, then the renamed one
    p.watch_once()
    p.wait_idle(30)
    assert [base64.b64decode(a["params"][1]) for a in nzbget.appends] == [other]


def test_the_lenient_pass_never_grabs_a_posting_twice(make_proxy, nzbget, hydra, tmp_path):
    # Codex review (PR 1): only the first four fetched postings were remembered, so the lenient pass grabbed
    # the rest again
    from tests.fakes import rar5_head
    pick, other = release(TITLE, prefix="p"), _with_pars(TITLE, "r", 10000000)
    for i in range(6):
        hydra.add(TITLE, release(TITLE, prefix="d%d" % i, extra_bytes=i + 1))
    hydra.add(RENAMED, other)
    payloads = {"p-0-0@x": rar5_head(TITLE + ".mkv", 1399900000), "r-0-0@x": rar5_head("x.mkv", 1399900000)}
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + article_ids(other), payloads=payloads).config(1)
    _pick(nzbget, tmp_path, pick)
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    grabs = [f.split("?")[0] for f in hydra.fetches]
    assert len(grabs) == len(set(grabs)), grabs


def test_a_hydra_off_file_in_the_state_dir_stops_every_request_to_hydra(make_proxy, nzbget, hydra, tmp_path):
    # the user's kill switch: with STATE_DIR/hydra.off present nothing is searched or fetched from Hydra
    hydra.add(TITLE, release(TITLE, prefix="r"))
    _pick(nzbget, tmp_path, release(TITLE, prefix="p"))
    p = _watcher(make_proxy)
    os.makedirs(p.cfg.state_dir, exist_ok=True)
    open(os.path.join(p.cfg.state_dir, "hydra.off"), "w").close()
    p.watch_once()
    p.wait_idle(20)
    assert not hydra.queries and not hydra.fetches and not nzbget.appends


def test_a_posting_the_first_pass_rejected_by_name_is_looked_inside_by_the_lenient_pass(make_proxy, nzbget, hydra,
                                                                                         tmp_path):
    # Codex review (PR 1): listed by the pick's own name, its files name no group, so the first pass rejected
    # it as another release; the lenient pass then skipped it, though the mkv inside is the pick's
    from tests.fakes import rar5_head
    pick, other = release(TITLE, prefix="p"), release("Show.S01E01.1080p.WEB.H264", prefix="r")
    hydra.add(TITLE, other)
    payloads = {"p-0-0@x": rar5_head(TITLE + ".mkv", 1399900000), "r-0-0@x": rar5_head("x.mkv", 1399900000)}
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + article_ids(other), payloads=payloads).config(1)
    _pick(nzbget, tmp_path, pick)
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    (d,) = nzbget.appends
    assert base64.b64decode(d["params"][1]) == other
    assert len(hydra.fetches) == 1                                  # looked at again, not grabbed again


def test_a_lenient_donor_less_whole_than_a_backup_ranks_below_it(make_proxy, nzbget, hydra, tmp_path):
    # Codex review (PR 1): the lenient pass ranked the backups again under fresh scores while its donors were
    # raised to their own, so an 85% donor could outrank a 90% backup
    from tests.fakes import rar5_head
    pick, back, other = release(TITLE, prefix="p"), release(TITLE, prefix="b"), _with_pars(TITLE, "r", 10000000)
    hydra.add(RENAMED, other)
    payloads = {"p-0-0@x": rar5_head(TITLE + ".mkv", 1399900000), "r-0-0@x": rar5_head("x.mkv", 1399900000)}
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + _part(back, 0.9) + _part(other, 0.7),
                                     payloads=payloads).config(1)
    _pick(nzbget, tmp_path, pick)
    _backup(nzbget, tmp_path, back, 502, PICK - 2, TITLE + ".b")
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    scores = nzbget.final_scores()
    (d,) = nzbget.appends
    assert scores[502] > scores[d["id"]], scores


def test_reranking_never_lands_a_donor_on_a_backups_score(make_proxy, nzbget, hydra, tmp_path):
    # Codex review (PR 1): rerank() raised a lenient donor to its target score, which a backup ranked in the
    # first pass held: a tie, with no failover order
    from tests.fakes import rar5_head
    pick, back, other = release(TITLE, prefix="p"), release(TITLE, prefix="b"), _with_pars(TITLE, "r", 10000000)
    hydra.add(RENAMED, other)
    payloads = {"p-0-0@x": rar5_head(TITLE + ".mkv", 1399900000), "r-0-0@x": rar5_head("x.mkv", 1399900000)}
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + _part(back, 0.9) + _part(other, 0.85),
                                     payloads=payloads).config(1)
    _pick(nzbget, tmp_path, pick)
    _backup(nzbget, tmp_path, back, 502, PICK - 2, TITLE + ".b")
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    scores = nzbget.final_scores()
    (d,) = nzbget.appends
    assert scores[502] != scores[d["id"]], scores


def test_rerank_keeps_every_score_it_hands_out_reserved(make_proxy):
    # Codex review (PR 1): swapping two donors' scores released each old score one by one, freeing a score
    # the other donor had just taken
    p = _watcher(make_proxy)
    ranks = ndp.Ranks()
    ranks.used |= {80, 81}
    info = ndp.parse_nzb(release(TITLE, prefix="p"))
    r = type("R", (), {"title": TITLE, "indexer": "i", "grabs": 0})()
    a = ndp.Placed(1, 80, 0.95, False, (r, b"", info))
    b = ndp.Placed(2, 81, 0.90, False, (r, b"", info))
    p.set_score = lambda *args, **kw: True
    p.rerank("/jsonrpc", "", [a, b], lambda size: 0, __import__("collections").Counter(), ranks)
    assert {a.score, b.score} <= ranks.used and a.score != b.score


def test_a_failed_head_read_is_tried_again_by_a_later_search(make_proxy, monkeypatch):
    # Codex review (PR 1): a read that failed (server down) was cached as "no inner file" for good
    from tests.fakes import rar5_head
    p = _watcher(make_proxy)
    info = ndp.parse_nzb(release(TITLE, prefix="p"))
    monkeypatch.setattr(ndp.donor_health, "fetch_body", lambda *a, **kw: None)
    assert p.inner(["srv"], info) is None
    monkeypatch.setattr(ndp.donor_health, "fetch_body",
                        lambda *a, **kw: (rar5_head(TITLE + ".mkv", 1399900000), None))
    assert p.inner(["srv"], info)[:2] == ("mkv", 1399900000)


def test_the_lenient_pass_searches_the_episode_without_its_resolution(make_proxy):
    # Codex review (PR 1): every query kept "1080p", so a listing named "Show S01E01 WEB H264" never came back
    p = _watcher(make_proxy)
    info = ndp.parse_nzb(release(TITLE, prefix="p"))
    assert {"t": "search", "q": "show s01e01"} in p.queries(TITLE, info, lenient=True)
    assert {"t": "search", "q": "show s01e01"} not in p.queries(TITLE, info)


def test_a_cached_posting_is_found_by_any_of_its_listings(make_proxy):
    # Codex review (PR 1): a renamed listing that joined an already fetched posting's group led it, so the cache
    # missed and the posting was grabbed again
    p = _watcher(make_proxy)
    L = lambda link: type("R", (), {"link": link})()  # noqa: E731
    fetched = {"a": ("r", "ok", b"", None, None)}
    p.fetch_posting = lambda listings, deadline: (_ for _ in ()).throw(AssertionError("grabbed again"))
    assert p.fetch_once(fetched, [L("b"), L("a")], 0)[1] == "ok"
    assert p.cached(fetched, [L("b"), L("a")])


def test_a_cached_fetch_failure_tries_the_postings_other_listings(make_proxy):
    # Codex review (PR 1): listing A failed in the first pass; B, another listing of that posting, joined in the
    # lenient pass but the cached failure was returned without trying it
    p = _watcher(make_proxy)
    L = lambda link: type("R", (), {"link": link})()  # noqa: E731
    fetched = {"a": ("ra", "fetch", None, None, None)}
    tried = []
    p.fetch_posting = lambda listings, deadline: tried.append([r.link for r in listings]) or ("rb", "ok", b"", 1, None)
    assert p.fetch_once(fetched, [L("b"), L("a")], 0)[1] == "ok"
    assert tried == [["b"]]


def test_a_partly_failed_head_read_is_not_cached(make_proxy, monkeypatch):
    # Codex review (PR 1): one unreadable head plus one failed read was cached as "no inner file" for good
    from tests.fakes import rar5_head
    p = _watcher(make_proxy)
    info = ndp.parse_nzb(release(TITLE, prefix="p"))
    first = info.heads[0][0]
    monkeypatch.setattr(ndp.donor_health, "fetch_body",
                        lambda servers, sid, **kw: (b"\x00" * 64, None) if sid == first else None)
    assert p.inner(["srv"], info) is None
    monkeypatch.setattr(ndp.donor_health, "fetch_body",
                        lambda servers, sid, **kw: (b"\x00" * 64, None) if sid == first else
                        (rar5_head(TITLE + ".mkv", 1399900000), None))
    assert p.inner(["srv"], info)[:2] == ("mkv", 1399900000)


def test_donors_of_both_passes_are_ranked_together(make_proxy, nzbget, hydra, tmp_path):
    # Codex review (PR 1): an 80% first-pass donor kept its score above a 100% lenient donor, whose rerank
    # only stepped around it
    from tests.fakes import rar5_head
    pick, part, other = release(TITLE, prefix="p"), release(TITLE, prefix="h"), _with_pars(TITLE, "r", 10000000)
    hydra.add(TITLE, part)
    hydra.add(RENAMED, other)
    payloads = {"p-0-0@x": rar5_head(TITLE + ".mkv", 1399900000), "r-0-0@x": rar5_head("x.mkv", 1399900000)}
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + _part(part, 0.8) + article_ids(other),
                                     payloads=payloads).config(1)
    _pick(nzbget, tmp_path, pick)
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    scores, ids = nzbget.final_scores(), {base64.b64decode(a["params"][1]): a["id"] for a in nzbget.appends}
    assert scores[ids[other]] > scores[ids[part]], scores


def test_hydra_off_stops_a_search_already_paging(make_proxy, monkeypatch):
    # Codex review (PR 2): the switch was checked once on entry, so a search already running kept paging
    p = _watcher(make_proxy)
    os.makedirs(p.cfg.state_dir, exist_ok=True)
    pages = []

    def page(params):
        pages.append(params["offset"])
        open(os.path.join(p.cfg.state_dir, "hydra.off"), "w").close()  # switched off while the first page runs
        return [object()] * ndp.SEARCH_PAGE
    monkeypatch.setattr(p, "_hydra_page", page)
    p.hydra_search({"t": "search", "q": "x"})
    assert pages == [0]


def test_hydra_off_stops_a_fetch_between_its_attempts(make_proxy, monkeypatch):
    # Codex review (PR 2): a fetch that passed the check kept retrying after the switch was set
    import urllib.request
    p = _watcher(make_proxy)
    os.makedirs(p.cfg.state_dir, exist_ok=True)
    calls = []

    def urlopen(*a, **kw):
        calls.append(1)
        open(os.path.join(p.cfg.state_dir, "hydra.off"), "w").close()
        raise OSError("down")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(ndp, "FETCH_RETRY_DELAY", 0)
    r = type("R", (), {"link": "http://h/getnzb/1", "indexer": "i", "title": TITLE, "size": 0})()
    p._fetch(r, __import__("time").time() + 30, 2)
    assert calls == [1]


def test_a_listing_that_names_another_hdr_format_alone_is_never_fetched_by_size(make_proxy):
    # Codex review (PR 1): "Show S01E01 DV" names no group or resolution, so it read as unreadable and the size
    # fallback let it in, though it says another HDR format
    info = ndp.parse_nzb(release(TITLE, prefix="p"))
    r = type("R", (), {"title": "Show S01E01 DV", "size": info.total_bytes})()
    assert not ndp.candidate_ok(TITLE, info.total_bytes, r, 0, True)
    r.title = "a8f3c9e1b7d24f60"
    assert ndp.candidate_ok(TITLE, info.total_bytes, r, 0, True)


def test_a_cached_nzb_that_did_not_match_its_listing_lets_fresh_listings_be_tried(make_proxy):
    # Codex review (PR 1): the first pass kept a size-mismatched NZB as its "ok" fallback; the lenient pass then
    # returned it for a group with a new listing of the posting, which was never tried
    p = _watcher(make_proxy)
    L = lambda link: type("R", (), {"link": link, "size": 1000})()  # noqa: E731
    wrong = type("I", (), {"total_bytes": 5000})()
    fetched = {"a": (L("a"), "ok", b"", wrong, None)}
    tried = []
    right = type("I", (), {"total_bytes": 1000})()
    p.fetch_posting = lambda listings, deadline: tried.append([r.link for r in listings]) or (listings[0], "ok", b"",
                                                                                            right, None)
    assert p.fetch_once(fetched, [L("b"), L("a")], 0)[3] is right
    assert tried == [["b"]]


@pytest.mark.parametrize("files", ["Show.S01E02", "Show.S01E01.DV"])
def test_a_posting_whose_files_name_another_episode_or_hdr_is_never_taken(make_proxy, nzbget, hydra, tmp_path, files):
    # Codex review (PR 1): with no group or resolution the names read as unreadable, which skipped the conflict
    # check, and an equal-sized mkv let another episode (or DV) in
    from tests.fakes import rar5_head
    pick, other = release(TITLE, prefix="p"), release(files, prefix="r")
    hydra.add(RENAMED, other)
    payloads = {"p-0-0@x": rar5_head(TITLE + ".mkv", 1399900000), "r-0-0@x": rar5_head("x.mkv", 1399900000)}
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + article_ids(other), payloads=payloads).config(1)
    _pick(nzbget, tmp_path, pick)
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    assert not nzbget.appends


def test_the_picks_failed_head_reads_are_tried_once_per_search(make_proxy, monkeypatch):
    # Codex review (PR 1): the pick's head reads failed and were tried again for every candidate
    p = _watcher(make_proxy)
    info = ndp.parse_nzb(release(TITLE, prefix="p"))
    reads = []
    monkeypatch.setattr(ndp.donor_health, "fetch_body", lambda *a, **kw: reads.append(1))
    p.ctx.inner_memo = {}
    for _ in range(5):
        assert p.inner(["srv"], info) is None
    assert len(reads) == len(info.heads)
    p.ctx.inner_memo = {}                                           # a later search reads them again
    p.inner(["srv"], info)
    assert len(reads) == 2 * len(info.heads)


def test_a_packed_file_named_as_another_episode_is_never_taken(make_proxy, nzbget, hydra, tmp_path):
    # Codex review (PR 1): the media name read from the rar header was dropped, so an obfuscated posting whose
    # archive holds S01E02 passed on its size alone
    from tests.fakes import rar5_head
    pick, other = release(TITLE, prefix="p"), release(TITLE, prefix="r", obfuscate=True)
    hydra.add(RENAMED, other)
    payloads = {"p-0-0@x": rar5_head(TITLE + ".mkv", 1399900000),
                "r-0-0@x": rar5_head("Show.S01E02.1080p.WEB.H264-GRP.mkv", 1399900000)}
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + article_ids(other), payloads=payloads).config(1)
    _pick(nzbget, tmp_path, pick)
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    assert not nzbget.appends


def test_a_name_with_another_year_conflicts():
    # Codex review (PR 1): "Quiet.Harbour.2024" against a 2025 pick names no group or resolution
    assert ndp.conflicts("Quiet.Harbour.2025.1080p.WEB.H264-GRP", "Quiet.Harbour.2024.part01.rar")
    assert not ndp.conflicts("Quiet.Harbour.2025.1080p.WEB.H264-GRP", "Quiet.Harbour.2025.part01.rar")


def test_a_whole_donor_outranks_a_damaged_backup(make_proxy, nzbget, hydra, tmp_path):
    # Codex review (PR 1): backups ranked first held 89 and 88, so rerank pushed a 100% donor to 87, behind
    # the 99% backup
    pick, whole, most = release(TITLE, prefix="p"), release(TITLE, prefix="w"), release(TITLE, prefix="m")
    donor = release(TITLE, prefix="d", extra_bytes=7)
    hydra.add(TITLE, donor)
    ids = article_ids(most)
    nzbget.config_entries = FakeNntp(article_ids(pick) + article_ids(whole) + ids[:len(ids) * 99 // 100] +
                                     article_ids(donor)).config(1)
    _pick(nzbget, tmp_path, pick)
    _backup(nzbget, tmp_path, whole, 501, PICK - 1, TITLE + ".w")
    _backup(nzbget, tmp_path, most, 502, PICK - 2, TITLE + ".m")
    p = _watcher(make_proxy, health_min_articles=500)
    p.watch_once()
    p.wait_idle(30)
    scores = nzbget.final_scores()
    (d,) = nzbget.appends
    assert scores[d["id"]] > scores[502], scores


def test_a_packed_file_of_another_show_is_never_taken(make_proxy, nzbget, hydra, tmp_path):
    # Codex review (PR 1): "Other.Show.S01E01..." inside an obfuscated posting passed for "Show.S01E01..."
    from tests.fakes import rar5_head
    pick, other = release(TITLE, prefix="p"), release(TITLE, prefix="r", obfuscate=True)
    hydra.add(RENAMED, other)
    payloads = {"p-0-0@x": rar5_head(TITLE + ".mkv", 1399900000),
                "r-0-0@x": rar5_head("Other.Show.S01E01.1080p.WEB.H264-GRP.mkv", 1399900000)}
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + article_ids(other), payloads=payloads).config(1)
    _pick(nzbget, tmp_path, pick)
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    assert not nzbget.appends


def test_the_final_rerank_moves_a_backups_twins_with_it(make_proxy, nzbget, hydra, tmp_path):
    # Codex review (PR 1): two history entries of one NZB shared a score, but the final rerank moved only the one
    # it ranked, leaving its twin above a whole donor
    pick, most = release(TITLE, prefix="p"), release(TITLE, prefix="m")
    donor = release(TITLE, prefix="d", extra_bytes=7)
    hydra.add(TITLE, donor)
    ids = article_ids(most)
    nzbget.config_entries = FakeNntp(article_ids(pick) + ids[:len(ids) * 99 // 100] + article_ids(donor)).config(1)
    _pick(nzbget, tmp_path, pick)
    _backup(nzbget, tmp_path, most, 501, PICK - 1, TITLE + ".m")
    _backup(nzbget, tmp_path, most, 502, PICK - 2, TITLE + ".t")
    p = _watcher(make_proxy, health_min_articles=500)
    p.watch_once()
    p.wait_idle(30)
    scores = nzbget.final_scores()
    (d,) = nzbget.appends
    assert scores[501] == scores[502] < scores[d["id"]], scores


def test_a_posting_whose_files_name_another_show_is_never_taken(make_proxy, nzbget, hydra, tmp_path):
    # Codex review (PR 1): "Other.Show.S01E01.part01.rar" names no group or resolution, so it slipped past the
    # name checks and an equal-sized mkv inside let it in
    from tests.fakes import rar5_head
    pick, other = release(TITLE, prefix="p"), release("Other.Show.S01E01", prefix="r")
    hydra.add(RENAMED, other)
    payloads = {"p-0-0@x": rar5_head(TITLE + ".mkv", 1399900000), "r-0-0@x": rar5_head("x.mkv", 1399900000)}
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + article_ids(other), payloads=payloads).config(1)
    _pick(nzbget, tmp_path, pick)
    p = _watcher(make_proxy)
    p.watch_once()
    p.wait_idle(30)
    assert not nzbget.appends


def test_a_listing_that_names_another_show_is_never_fetched_by_size():
    info = ndp.parse_nzb(release(TITLE, prefix="p"))
    r = type("R", (), {"title": "Other Show S01E01", "size": info.total_bytes})()
    assert not ndp.candidate_ok(TITLE, info.total_bytes, r, 0, True)


def test_a_packed_file_in_a_folder_is_compared_by_its_own_name():
    # Codex review (PR 1): "Show/Show.S01E01..." read as the title "show show"
    assert not ndp.other_title(TITLE, "Show/Show.S01E01.1080p.WEB.H264-GRP.mkv")
    assert not ndp.other_title(TITLE, "Show\\Show.S01E01.1080p.WEB.H264-GRP.mkv")


def test_backups_keep_their_size_in_the_combined_ranking(make_proxy):
    # Codex review (PR 1): a backup ranked with v=None counted as the pick's own size, so a 420 MB backup sorted
    # ahead of a 147 MB donor beside a 140 MB pick
    p = _watcher(make_proxy)
    r = type("R", (), {"title": TITLE, "indexer": "i", "grabs": 0})()
    near = ndp.Placed(1, 80, 1.0, False, (r, b"", type("I", (), {"total_bytes": 147})()))
    far = ndp.Placed(2, 81, 1.0, False, None, size=420)
    p.set_score = lambda *a, **kw: True
    p.rerank("/jsonrpc", "", [near, far], lambda size: abs(size - 140), __import__("collections").Counter())
    assert near.score > far.score


def test_a_donor_whose_packed_file_names_another_release_is_rejected_even_when_the_picks_cannot_be_read(make_proxy):
    # Codex review (PR 1): with the pick's header unreadable (mine None) the donor's own inner name was ignored
    info = ndp.parse_nzb(release(TITLE, prefix="p"))
    ci = ndp.parse_nzb(release(TITLE, prefix="r"))
    assert ndp.lenient_ok(TITLE, info, ci, None, ("mkv", 1, TITLE + ".mkv"))
    assert not ndp.lenient_ok(TITLE, info, ci, None, ("mkv", 1, "Other.Show.S01E02.1080p.WEB.H264-GRP.mkv"))


def test_an_unchecked_backup_stays_below_checked_ones_in_the_combined_ranking(make_proxy):
    # Codex review (PR 1): the rerank kept an unreadable backup's 87 while it moved checked copies around it, so
    # it ended above one sampled 100%
    p = _watcher(make_proxy)
    r = type("R", (), {"title": TITLE, "indexer": "i", "grabs": 0})()
    v = (r, b"", type("I", (), {"total_bytes": 140})())
    donors = [ndp.Placed(10 + i, 80 - i, 1.0, True, v) for i in range(2)]
    checked = [ndp.Placed(1, 89, 1.0, False, None, 140), ndp.Placed(2, 88, 1.0, False, None, 140)]
    unchecked = ndp.Placed(3, 87, None, False, None, 140, True)
    p.set_score = lambda *a, **kw: True
    everyone = donors + checked + [unchecked]
    p.rerank("/jsonrpc", "", everyone, lambda size: 0, __import__("collections").Counter())
    assert unchecked.score < min(x.score for x in donors + checked)


@pytest.mark.parametrize("name", ["Show.S01E01.1080p.WEB.H265-GRP.mkv", "Show.S01E01.REPACK.1080p.WEB.H264-GRP.mkv",
                                  "Show.S01E01.1080p.BluRay.H264-GRP.mkv"])
def test_another_codec_source_or_repack_conflicts(name):
    # Codex review (PR 1): H265 or REPACK named inside passed for an H264, non-REPACK pick
    assert ndp.conflicts(TITLE, name)


def test_the_same_attributes_named_differently_do_not_conflict():
    assert not ndp.conflicts(TITLE, "Show.S01E01.1080p.WEB-DL.x264.mkv")
    assert not ndp.conflicts(TITLE, "a8f3c9e1b7d24f60.mkv")


def test_a_consistent_listing_that_names_another_codec_is_never_fetched():
    # Codex review (PR 1): consistent() ignores codec/repack, so an H265 listing took the shortcut
    info = ndp.parse_nzb(release(TITLE, prefix="p"))
    r = type("R", (), {"title": "Show S01E01 1080p WEB H265", "size": 1})()
    assert not ndp.candidate_ok(TITLE, info.total_bytes, r, 0, True)


def test_other_audio_conflicts():
    # Codex review (PR 1): AAC2.0 named inside passed for a DDP5.1 pick
    pick = "Show.S01E01.1080p.WEB.DDP5.1.H264-GRP"
    assert ndp.conflicts(pick, "Show.S01E01.1080p.WEB.AAC2.0.H264-GRP.mkv")
    assert not ndp.conflicts(pick, "Show.S01E01.1080p.WEB.H264.mkv")


def test_a_packed_movie_of_another_title_without_a_year_is_another_title():
    # Codex review (PR 1): "Other.Movie.1080p.WEB.H264-GRP.mkv" carries no year, but it is readable: a real title
    assert ndp.other_title(MOVIE, "Other.Movie.1080p.WEB.H264-GRP.mkv")
    assert not ndp.other_title(MOVIE, "Quiet.Harbour.1080p.WEB.H264-GRP.mkv")
    assert not ndp.other_title(MOVIE, "a8f3c9e1b7d24f60.mkv")


def test_rerank_never_raises_an_unchecked_backup(make_proxy):
    # Codex review (PR 1): an unreadable backup at base+20 was raised to base+89 by the combined rerank
    p = _watcher(make_proxy)
    low = ndp.Placed(3, 20, None, False, None, 140, True)
    p.set_score = lambda *a, **kw: True
    p.rerank("/jsonrpc", "", [low], lambda size: 0, __import__("collections").Counter())
    assert low.score == 20


def test_a_dry_run_donor_counts_once_across_both_passes(make_proxy):
    # Codex review (PR 1): in dry run add_donor() recorded nothing, so the lenient pass counted the first pass's
    # donor again and pushed a second one over MAX_DONORS
    p = _watcher(make_proxy, dry_run=True)
    p.ctx.dry_sent = set()
    ci = ndp.parse_nzb(release(TITLE, prefix="r"))
    r = type("R", (), {"title": TITLE, "indexer": "i", "grabs": 0})()
    stats = __import__("collections").Counter()
    assert p.add_donor("k", "", "/jsonrpc", "", (r, b"", ci), 80, {}, "fast", stats) == -1
    assert p.add_donor("k", "", "/jsonrpc", "", (r, b"", ci), 80, {}, "fast", stats) == 0


def test_unchecked_backups_keep_their_score_order(make_proxy):
    # Codex review (PR 1): an unreadable backup (size unknown) sorted ahead of a higher-scored one by size and
    # the no-raise clamp then tied them at 2
    p = _watcher(make_proxy)
    hi = ndp.Placed(1, 89, None, False, None, 300, True)
    lo = ndp.Placed(2, 2, None, False, None, 0, True)
    p.set_score = lambda *a, **kw: True
    p.rerank("/jsonrpc", "", [hi, lo], lambda size: abs(size - 140), __import__("collections").Counter())
    assert hi.score > lo.score


def test_a_dry_run_with_a_whole_donor_never_widens_the_search(make_proxy, nzbget, hydra, tmp_path):
    # Codex review (PR 1): would-be donors weren't swap candidates in dry run, so a whole one still set off the
    # lenient pass: an extra grab, and a preview of a search a real run never makes
    pick, donor = release(TITLE, prefix="p"), release(TITLE, prefix="r")
    hydra.add(TITLE, donor)
    hydra.add(RENAMED, _with_pars(TITLE, "z", 10000000))
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + article_ids(donor)).config(1)
    _pick(nzbget, tmp_path, pick)
    p = _watcher(make_proxy, dry_run=True)
    p.watch_once()
    p.wait_idle(30)
    assert len(hydra.fetches) == 1, hydra.fetches



def test_the_lenient_tv_query_keeps_the_episode_when_the_title_has_a_year(make_proxy):
    # Codex review (PR 1): "Dark.Matter.2024.S02E05..." cut at 2024 and searched "dark matter 2024"
    p = _watcher(make_proxy)
    title = "Dark.Matter.2024.S02E05.Love.and.Be.Loved.2160p.ATVP.WEB-DL.DDP5.1.H.265-FLUX"
    info = ndp.parse_nzb(release(title, prefix="p"))
    assert {"t": "search", "q": "dark matter 2024 s02e05"} in p.queries(title, info, lenient=True)


def test_a_posting_whose_names_conflict_costs_no_head_reads(make_proxy, nzbget, hydra, tmp_path, monkeypatch):
    # Codex review (PR 2): the name checks ran after both inner() reads, so conflicting postings spent the
    # discovery deadline on article reads
    pick, other = release(TITLE, prefix="p"), release("Show.S01E02.1080p.WEB.H264-GRP", prefix="r")
    hydra.add(RENAMED, other)
    nzbget.config_entries = FakeNntp(_part(pick, 0.3) + article_ids(other)).config(1)
    _pick(nzbget, tmp_path, pick)
    p = _watcher(make_proxy)
    reads = []
    monkeypatch.setattr(p, "inner", lambda servers, info, deadline=None: reads.append(info) or None)
    p.watch_once()
    p.wait_idle(30)
    assert not reads and not nzbget.appends


def test_an_obfuscated_listing_with_a_resolution_tag_still_reaches_the_inner_check():
    # Codex review (PR 1): "a8f3c9e1b7d24f60.1080p" reads as readable (a resolution), so its hash title was taken
    # for another title and the size fallback never let it through to lenient_ok()
    info = ndp.parse_nzb(release(TITLE, prefix="p"))
    r = type("R", (), {"title": "a8f3c9e1b7d24f60.1080p", "size": info.total_bytes})()
    assert ndp.candidate_ok(TITLE, info.total_bytes, r, 0, True)
    r.title = "a8f3c9e1b7d24f60.2160p"                               # a resolution it states still conflicts
    assert not ndp.candidate_ok(TITLE, info.total_bytes, r, 0, True)


def test_the_lenient_pass_shares_the_first_passs_deadline_and_fetch_budget(make_proxy, nzbget, hydra, tmp_path):
    # Codex review (PR 1): the lenient pass got a fresh deadline and a fresh 3 * MAX_DONORS fetch allowance, so a
    # failing pick could cost twice the documented grabs
    from tests.fakes import rar5_head
    pick = release(TITLE, prefix="p")
    for i in range(3):
        hydra.add(TITLE, release(TITLE, prefix="d%d" % i, extra_bytes=i + 1))
    for i in range(3):
        hydra.add(RENAMED + " v%d" % i, _with_pars(TITLE, "z%d" % i, 10000000 + i))
    nzbget.config_entries = FakeNntp(_part(pick, 0.3)).config(1)
    _pick(nzbget, tmp_path, pick)
    p = _watcher(make_proxy, max_donors=1)
    p.watch_once()
    p.wait_idle(30)
    assert len(hydra.fetches) <= 3, hydra.fetches
