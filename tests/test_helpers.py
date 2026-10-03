import pytest

from nzbget_dupe_proxy import mask, normalize_title, parse_nzb, readable, same_release, short_query
from tests.fakes import make_nzb


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


LUC = "Lucifer.S02E14.Candy.Morningstar.1080p.DTS-HD.MA.5.1.AVC.REMUX-FraMeSToR"


def test_same_release_ignores_formatting_and_size():
    assert same_release(LUC, "Lucifer S02E14 Candy Morningstar 1080p DTS-HD MA 5 1 AVC REMUX-FraMeSToR")
    assert same_release(LUC, "lucifer.s02e14.candy.morningstar.1080p.dts-hd.ma.5.1.avc.remux-framestor.mkv")


def test_same_release_rejects_other_group_res_episode_repack_audio():
    assert not same_release(LUC, LUC.replace("FraMeSToR", "EPSiLON"))
    assert not same_release(LUC, LUC.replace("1080p", "2160p"))
    assert not same_release(LUC, LUC.replace("S02E14", "S02E15"))
    assert not same_release(LUC, LUC.replace("1080p", "REPACK.1080p"))
    assert not same_release(LUC, LUC.replace("DTS-HD.MA.5.1", "DDP5.1"))
    assert not same_release("Movie.2020.1080p.BluRay.x264-GRP", "Movie.2021.1080p.BluRay.x264-GRP")
    assert not same_release("Show.S01E01.1080p.NF.WEB-DL.DDP5.1.H.264-GRP", "Show.S01E01.1080p.AMZN.WEB-DL.DDP5.1.H.264-GRP")


def test_same_release_tolerates_missing_attributes_but_requires_group():
    assert same_release(LUC, "Lucifer.S02E14.1080p.BluRay.REMUX.AVC-FraMeSToR")   # fewer tags, compatible
    assert not same_release(LUC, "Lucifer.S02E14.1080p.REMUX")                    # no group


def test_readable_release_name():
    assert readable("Lucifer.S02E14.1080p.WEB.H264-GRP.mkv")
    assert not readable("53a9sd8antphskzkulcik6yrrktj1qft.7z.001")
    assert not readable("e630b420289687dc3c66cc3274207902.part01.rar")


def test_parse_nzb_main_name_is_largest_file():
    i = parse_nzb(make_nzb([("small.nfo", [10]), ("big.mkv", [100, 100]), ("mid.par2", [50])]))
    assert i.main_name == "big.mkv"


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



def test_parse_nzb_main_name_skips_par2():
    i = parse_nzb(make_nzb([("abc.vol063-121.par2", [500]), ("Show.S01E01.1080p.WEB.H264-GRP.mkv", [100])]))
    assert i.main_name == "Show.S01E01.1080p.WEB.H264-GRP.mkv"


def test_same_release_hdr_must_match_exactly():
    a = "Shrinking.S02E06.2160p.ATVP.WEB-DL.DDPA5.1.HDR.DV.HEVC-NTb"
    assert same_release(a, "Shrinking.S02E06.In.a.Lonely.Place.2160p.ATVP.WEB-DL.DDP5.1.DV.HDR.H.265-NTb")
    assert not same_release(a, "Shrinking.S02E06.In.a.Lonely.Place.2160p.ATVP.WEB-DL.DDP5.1.H.265-NTb")      # SDR
    assert not same_release(a, "Shrinking.S02E06.In.a.Lonely.Place.2160p.ATVP.WEB-DL.DDP5.1.DV.H.265-NTb")   # DV only
    assert same_release("Shrinking.S01E10.Closure.2160p.ATVP.WEB-DL.DDP5.1.DoVi.H.265-NTb",
                        "Shrinking S01E10 Closure 2160p ATVP WEB-DL DDP5 1 DV H 265-NTb")


def test_codec_suffix_is_not_a_volume_suffix():
    assert normalize_title("Movie.2020.1080p.WEB.DDP5.1.H.265") == "movie.2020.1080p.web.ddp5.1.h.265"
    assert not same_release("Movie.2020.1080p.WEB.DDP5.1.H.264", "Movie.2020.1080p.WEB.DDP5.1.H.265")
    assert normalize_title("Movie.2020.1080p.WEB-GRP.mkv.001") == "movie.2020.1080p.web.grp"


def test_sketch_same_posting_survives_reuploaded_segments_but_not_other_postings():
    from nzbget_dupe_proxy import same_sketch, sketch
    a = {"a%d@x" % i for i in range(6000)}
    reup = (a - {"a1@x", "a2@x"}) | {"z1@x", "z2@x"}
    other = {"b%d@x" % i for i in range(6000)}
    assert len(sketch(a)) == 64
    assert same_sketch(sketch(a), sketch(reup))
    assert not same_sketch(sketch(a), sketch(other))
    assert same_sketch(sketch({"t1@x", "t2@x"}), sketch({"t1@x", "t2@x"}))          # tiny NZBs too


def test_kept_nzb_sketch_is_cached(tmp_path, monkeypatch):
    import nzbget_dupe_proxy as ndp
    f = tmp_path / "x.nzb.queued"
    f.write_bytes(make_nzb([("a.mkv", [5, 5])]))
    calls = []
    real = ndp.parse_nzb
    monkeypatch.setattr(ndp, "parse_nzb", lambda d: calls.append(1) or real(d))
    assert ndp.kept_sketch(str(f)) == ndp.kept_sketch(str(f))
    assert len(calls) == 1
