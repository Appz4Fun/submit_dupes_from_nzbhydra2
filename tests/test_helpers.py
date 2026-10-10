import json

import pytest

import nzbget_dupe_proxy as ndp
from nzbget_dupe_proxy import mask, normalize_title, parse_nzb, readable, same_posting, same_release, short_query
from tests.fakes import make_nzb, par2_main


def test_state_load_tolerates_valid_json_of_wrong_shape(tmp_path):
    # state.json that is valid JSON but not the expected dict-of-entries must not crash save()/group_for()
    for content in ("[]", "42", '"x"', "null", '{"k": "notadict"}', '{"k": {"title": "X"}}', '{"k": {"t": "soon"}}'):
        (tmp_path / "state.json").write_text(content)
        s = ndp.State(str(tmp_path))
        s.save()  # must not raise
        assert s.group_for("Show.S01E01.2160p.ATVP.WEB-DL-GRP", 1e12) is None


def test_state_load_keeps_valid_entries(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"k": {"t": 1e12, "title": "X", "fps": {}}}))
    s = ndp.State(str(tmp_path))
    assert "k" in s.data


def test_clean_name_no_redos_on_adversarial_junk_suffix():
    # JUNK_RE's rakuv\w* overlaps the "_" separator: "rakuv_rakuv_..." + a non-matching tail
    # triggered catastrophic backtracking (seconds to minutes) on untrusted indexer titles.
    import signal

    evil = "Show.S01E01.rakuv" + "_rakuv" * 40 + "!"

    def _timeout(*_):
        raise TimeoutError("clean_name took too long — ReDoS")

    old = signal.signal(signal.SIGALRM, _timeout)
    signal.setitimer(signal.ITIMER_REAL, 2.0)
    try:
        result = ndp.clean_name(evil)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
    assert isinstance(result, str) and result


def _nzb_with_segments(segment_ids):
    """Raw NZB whose single file has one segment per entry in segment_ids (text inserted verbatim)."""
    segs = "".join('<segment bytes="100" number="%d">%s</segment>' % (n + 1, t) for n, t in enumerate(segment_ids))
    return ('<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">'
            '<file poster="p@x" subject="&quot;a.mkv&quot;"><groups><group>a.b</group></groups>'
            '<segments>%s</segments></file></nzb>' % segs).encode()


def test_parse_nzb_measures_how_much_of_each_file_is_listed():
    # live (Puppy Place S02E08 7223): NZBIndex's NZB declared "yEnc (1/3709)" but listed segments 1..1510 only
    full = make_nzb([("a.mkv", [100] * 20)])
    assert parse_nzb(full).listed == 1.0
    assert parse_nzb(full.replace(b"yEnc (1/20)", b"yEnc (1/150)")).listed == pytest.approx(20 / 150)
    assert parse_nzb(_nzb_with_segments(["x@x"])).listed == 1.0  # no part count declared: taken as whole


def test_parse_nzb_counts_parts_missing_below_the_last_listed_one():
    # live (Silo S01E06 7541, NZBIndex): one file, one segment numbered 9374 of "(1/9384)"; its article could be
    # on the servers, so a sample read 100% for an NZB lacking 9,373 of its parts
    frag = ('<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"><file poster="p@x" '
            'subject="{a.rar} {x} yEnc (1/9384)"><groups><group>a.b</group></groups><segments>'
            '<segment bytes="1056953" number="9374">f@x</segment></segments></file></nzb>').encode()
    assert parse_nzb(frag).listed == pytest.approx(1 / 9374)
    gap = make_nzb([("a.mkv", [100] * 150)]).replace(b'number="75">', b'number="0075x">')
    gap = gap.replace(b'<segment bytes="100" number="0075x">a-0-74@x</segment>', b"")
    assert parse_nzb(gap).listed == pytest.approx(149 / 150)    # one inner part missing
    odd = make_nzb([("a.mkv", [100] * 3)]).replace(b'number="3"', b'number="0"')
    assert parse_nzb(odd).listed == 1.0                          # unusable numbering: not judged


def test_parse_nzb_does_not_trust_padded_part_counts():
    # live (NTb, FLUX, TEPES obfuscated postings): every rar listed ~137 parts while subjects declared 139..165,
    # and a one-segment par2 declared (1/26); the releases downloaded fine, the counts are padding
    padded = make_nzb([("a.rar", [100] * 20)]).replace(b"yEnc (1/20)", b"yEnc (1/25)")
    assert parse_nzb(padded).listed == 1.0          # 80% listed: within what padding produces
    tiny = make_nzb([("a.par2", [100])]).replace(b"yEnc (1/1)", b"yEnc (1/26)")
    assert parse_nzb(tiny).listed == 1.0            # one segment: nothing to judge by
    ended = make_nzb([("a.rar", [100] * 19 + [40])]).replace(b"yEnc (1/20)", b"yEnc (1/150)")
    assert parse_nzb(ended).listed == 1.0           # its last listed part is short: the file ends there
    # nzbget's corpus run (~4,900 NZBs): rar sets listing exactly 20 full parts against N 34..48 (d3g, NTb, SiQ,
    # playWEB), and par2 volumes whose short last part is 92..99.8% of full, were still read as cut short
    rars = make_nzb([("a.part01.rar", [768000] * 20)]).replace(b"yEnc (1/20)", b"yEnc (1/48)")
    assert parse_nzb(rars).listed == 1.0            # 28 parts off: padding is tens, truncation thousands
    vol = make_nzb([("a.vol07+08.par2", [768000] * 59 + [760000])]).replace(b"yEnc (1/60)", b"yEnc (1/400)")
    assert parse_nzb(vol).listed == 1.0            # a par2's tail is never judged
    near = make_nzb([("a.mkv", [768000] * 59 + [750000])]).replace(b"yEnc (1/60)", b"yEnc (1/400)")
    assert parse_nzb(near).listed == 1.0           # 97.7% of the common size is a short (last) part


def test_parse_nzb_skips_empty_and_whitespace_segment_ids():
    i = parse_nzb(_nzb_with_segments(["", "   ", "real@x"]))
    assert i.message_ids == {"real@x"}
    assert "" not in i.message_ids


def test_parse_nzb_empty_ids_do_not_cause_false_same_posting():
    a = parse_nzb(_nzb_with_segments(["", "aaa@x"])).message_ids
    b = parse_nzb(_nzb_with_segments(["", "bbb@x"])).message_ids
    assert not same_posting(a, b)  # distinct postings, shared only a blank id


def test_parse_nzb_rejects_nzb_whose_segments_are_all_empty():
    with pytest.raises(ValueError):
        parse_nzb(_nzb_with_segments(["", "   "]))


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


def test_config_listen_host_option():
    # a LISTEN_HOST knob lets the user restrict the proxy's bind (it listens on 0.0.0.0 by default);
    # the default preserves the current behaviour.
    assert ndp.Config.from_env({}).listen_host == "0.0.0.0"
    assert ndp.Config.from_env({"LISTEN_HOST": "127.0.0.1"}).listen_host == "127.0.0.1"


def test_par2_block_size_reads_the_main_packet():
    assert ndp.par2_block_size(b"junk" + par2_main(15000000) + b"tail") == 15000000
    assert ndp.par2_block_size(b"not a par2 file") is None


def test_parse_nzb_records_par2_geometry():
    nzb = make_nzb([("a.mkv", [768000] * 100), ("a.vol-06.par2", [10372]), ("a.vol-01.par2", [768000] * 4),
                    ("a.vol-02.par2", [768000] * 8)], prefix="g")
    i = parse_nzb(nzb)
    assert i.par_index == "g-1-0@x"                     # the smallest par2 file: the index, no recovery data
    assert sorted(i.vol_bytes) == [768000 * 4, 768000 * 8]
    assert i.data_bytes == 768000 * 100 and i.article_bytes == 768000
    assert parse_nzb(make_nzb([("a.mkv", [100] * 3)])).par_index == ""


def _geometry_7896(alive):
    """House of the Dragon S03E04 FUZEER (7896): 15 MB blocks, vols vol-01..08 per its NZB."""
    vols = (61945741, 123871958, 479901006, 247716068, 15488562, 30979183, 495389262)
    info = ndp.NzbInfo(10, 7234074745, frozenset(), "", frozenset({"x"}), {}, "a.mkv", 1.0,
                       par_index="i@x", vol_bytes=vols, data_bytes=7234074745, article_bytes=739614)
    return ndp.par_doomed(info, alive, 15000000)


def test_par_doomed_predicts_last_nights_par_failures():
    # live: 7896 failed par at health 98.2% (153 of 468 blocks bad, ~97 recovery blocks); a whole one is fine
    doomed, damaged, recovery = _geometry_7896(0.985)
    assert doomed and 100 < damaged < 160 and 85 < recovery < 105
    assert not _geometry_7896(1.0)[0]
    assert not _geometry_7896(0.998)[0]                # 2 of 1,000 missing: ~4% of blocks, well inside the pars
    info = ndp.NzbInfo(1, 100, frozenset(), "", frozenset({"x"}), {}, "a.mkv")
    assert ndp.par_doomed(info, 0.5, 15000000)[0] is False   # no recovery volumes known: no verdict


def test_normalize_title_drops_a_par2_subject_s_part_tag_and_dashed_volume():
    # live (How to Make a Killing, nzbid 8188): an NZB named after its par2 subject searched for
    # "...triton.vol03.07" and matched none of 258 Hydra results
    raw = "[04_10]_-_How.to.Make.a.Killing.2026.2160p.UHD.BluRay.Hybrid.REMUX.DV.HDR10+.HEVC.Atmos-TRiToN.vol03-07.par2"
    assert normalize_title(raw) == normalize_title(
        "How.to.Make.a.Killing.2026.2160p.UHD.BluRay.Hybrid.REMUX.DV.HDR10+.HEVC.Atmos-TRiToN")


def test_inner_file_reads_the_packed_file_of_a_rar5_volume():
    from tests.fakes import rar5_head
    assert ndp.inner_file(rar5_head("Movie.2026.2160p-GRP.mkv", 81234567890)) == ("mkv", 81234567890)


def test_inner_file_reads_the_packed_file_of_a_rar4_volume_past_4_gb():
    from tests.fakes import rar4_head
    assert ndp.inner_file(rar4_head("Movie.2026.2160p-GRP.mkv", 5000000000)) == ("mkv", 5000000000)


def test_inner_file_of_a_bare_media_file_is_its_yenc_size():
    assert ndp.inner_file(b"\x1aE\xdf\xa3" + b"\x00" * 60, "a8f3c9e1.mkv", 7340032123) == ("mkv", 7340032123)


def test_inner_file_of_anything_else_is_unknown():
    assert ndp.inner_file(b"\x00" * 64, "a8f3c9e1.bin", 1000) is None
    assert ndp.inner_file(b"Rar!\x1a\x07\x01\x00garbage") is None
