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
