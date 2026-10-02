import pytest

from nzbget_dupe_proxy import (NzbInfo, Result, candidate_ok, mask, normalize_title, parse_nzb,
                               short_query, verify)
from tests.fakes import make_nzb


def info(files, total, names=(), ids=None):
    ids = frozenset(ids if ids is not None else ["%d-%d" % (files, total)])
    return NzbInfo(files=files, total_bytes=total, filenames=frozenset(names), poster="p",
                   message_ids=ids, meta={})


def result(title, size):
    return Result(title=title, link="http://h/getnzb/1", size=size, grabs=0, date=0.0, indexer="i")


def test_normalize_title():
    assert normalize_title("Lucifer S02E14 1080p BluRay x264-DEFLATE.nzb") == "lucifer.s02e14.1080p.bluray.x264.deflate"
    assert normalize_title("Some.Movie.2020.1080p.WEB-DL-GRP-xpost [nzbgeek].nzb") == "some.movie.2020.1080p.web.dl.grp"
    assert normalize_title("A_B--C.mkv") == "a.b.c"
    assert normalize_title("Show.S01E01.1080p.WEB.H264-GRP") == "show.s01e01.1080p.web.h264.grp"


def test_short_query():
    assert short_query("Lucifer.S02E14.Candy.Morningstar.1080p.DTS-HD.MA.5.1.AVC.REMUX-FraMeST") == "lucifer s02e14 1080p"
    assert short_query("Some.Movie.2020.Extended.2160p.UHD-GRP") == "some movie 2020 2160p"
    assert short_query("NoMarkers-GRP") is None


def test_parse_nzb_counts_bytes_names_ids():
    data = make_nzb([("rel.part1.rar", [100, 100]), ("rel.par2", [50])], prefix="a", meta={"imdb": "tt123"})
    i = parse_nzb(data)
    assert (i.files, i.total_bytes) == (2, 250)
    assert i.filenames == {"rel.part1.rar", "rel.par2"}
    assert i.message_ids == {"a-0-0@x", "a-0-1@x", "a-1-0@x"}
    assert i.meta == {"imdb": "tt123"}
    assert i.poster == "poster@example.com"
    assert i.fingerprint == parse_nzb(make_nzb([("other", [1, 2]), ("x", [3])], prefix="a")).fingerprint


def test_parse_nzb_malformed():
    with pytest.raises(ValueError):
        parse_nzb(b"<nzb><file")
    with pytest.raises(ValueError):
        parse_nzb(b"<nzb></nzb>")


def test_verify_size_and_count():
    p = info(10, 1000, {"a", "b"})
    assert verify(p, info(11, 1005, {"x"}))
    assert not verify(p, info(20, 1005, {"x"}))
    assert not verify(p, info(10, 1020, {"x"}))


def test_verify_shared_filenames():
    p = info(10, 1000, {"a", "b"})
    assert verify(p, info(20, 1500, {"a", "b", "c"}))
    assert not verify(p, info(20, 1500, {"c", "d"}))


def test_candidate_ok_title_and_size():
    p = "Lucifer.S02E14.1080p.WEB.H264-GRP"
    assert candidate_ok(p, 1000, result("Lucifer.S02E14.1080p.WEB.H264-OTHER", 1010), 0.02)
    assert candidate_ok(p, 1000, result("lucifer s02e14 1080p web h264 grp", 1019), 0.02)
    assert not candidate_ok(p, 1000, result("Lucifer.S02E14.1080p.WEB.H264-OTHER", 1100), 0.02)
    assert not candidate_ok(p, 1000, result("Totally Different Show", 1000), 0.02)


def test_candidate_ok_requires_same_episode_and_year():
    assert not candidate_ok("Lucifer.S02E14.1080p.WEB.H264-GRP", 1000, result("Lucifer.S02E15.1080p.WEB.H264-GRP", 1000), 0.02)
    assert not candidate_ok("Movie.2020.1080p.BluRay.x264-GRP", 1000, result("Movie.2021.1080p.BluRay.x264-GRP", 1000), 0.02)
    assert candidate_ok("Lucifer.S02E14.1080p.WEB.H264-GRP", 1000, result("Lucifer.S02E14.2016.1080p.WEB.H264-GRP", 1000), 0.02)


def test_mask():
    assert mask("http://h/api?t=search&apikey=SECRET&q=x") == "http://h/api?t=search&apikey=***&q=x"
    assert mask("http://u:p@h/jsonrpc /admin:pw/jsonrpc") == "http://***@h/jsonrpc /***/jsonrpc"


def test_parse_nzb_rejects_entity_declarations():
    bomb = b'<?xml version="1.0"?><!DOCTYPE n [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]><nzb><file subject="&b;"><segments><segment bytes="1">x@y</segment></segments></file></nzb>'
    with pytest.raises(ValueError):
        parse_nzb(bomb)


def test_parse_nzb_rejects_entities_after_padding():
    pad = b"<!--" + b" " * 8000 + b"-->"
    bomb = b'<?xml version="1.0"?>' + pad + b'<!DOCTYPE n [<!ENTITY a "aaaa">]><nzb><file subject="&a;"><segments><segment bytes="1">x@y</segment></segments></file></nzb>'
    with pytest.raises(ValueError):
        parse_nzb(bomb)


def test_parse_nzb_accepts_standard_doctype():
    nzb = make_nzb([("a.mkv", [5])]).replace(
        b"\n<nzb", b'\n<!DOCTYPE nzb PUBLIC "-//newzBin//DTD NZB 1.1//EN" "http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd">\n<nzb', 1)
    assert parse_nzb(nzb).files == 1
