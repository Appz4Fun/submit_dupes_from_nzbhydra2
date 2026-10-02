from tests.fakes import release
from tools.replay_append import replay

TITLE = "Show.S01E01.1080p.WEB.H264-GRP"


def test_replay_reports_would_be_donors_without_touching_nzbget(hydra):
    hydra.add(TITLE, release(TITLE, prefix="p"), grabs=9)
    hydra.add("Show.S01E01.1080p.WEB.H264-OTHER", release(TITLE, prefix="r"), grabs=3)
    out = replay("Show S01E01", hydra.url, "KEY", pick="GRP @")
    assert out["primary"] == TITLE
    assert len(out["donors"]) == 1 and "Show.S01E01.1080p.WEB.H264-OTHER" in out["donors"][0]
    assert "dry_run would_add=1" in out["summary"]
    assert out["fake_appends"] == 1                  # only the primary, and only into the fake nzbget


def test_replay_verify_count_flag(hydra):
    hydra.add(TITLE, release(TITLE, prefix="p"), grabs=9)
    hydra.add(TITLE + "-7Z", release("x", prefix="q", n_files=20, segs_per_file=10), grabs=1)
    assert replay("Show S01E01", hydra.url, "KEY", pick="GRP @")["donors"] == []
    assert len(replay("Show S01E01", hydra.url, "KEY", pick="GRP @", verify_count=False)["donors"]) == 1
