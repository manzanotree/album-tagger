from collections import Counter

import pytest

import split_album as sa
from build_genre_map import build_config, empty_config


def make_cfg(**overrides):
    raw = empty_config()
    raw.update(overrides)
    return sa.GenreConfig.from_raw(raw)


LIB_CFG = dict(
    roots=["Electronic", "Soul", "Rock"],
    parents={"Deep House": "Electronic", "House": "Electronic", "Funk": "Soul",
             "Disco": "Soul", "Indie": "Rock"},
    aliases={"Funk / Soul": "Soul/Funk", "Indie Rock": "Indie", "hip hop": "Hip-Hop"},
    decade_genres=["Rock"],
)


class TestCue:
    def test_index_01_times(self):
        cue = '''FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "One"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    INDEX 00 03:58:10
    INDEX 01 04:00:37
  TRACK 03 AUDIO
    INDEX 01 61:02:74
'''
        assert sa.parse_cue(cue) == pytest.approx([0.0, 240 + 37 / 75, 3662 + 74 / 75])

    def test_missing_index_01(self):
        with pytest.raises(sa.AppError):
            sa.parse_cue('FILE "a.wav" WAVE\n TRACK 01 AUDIO\n  INDEX 00 00:00:00\n')

    def test_multiple_files_rejected(self):
        with pytest.raises(sa.AppError):
            sa.parse_cue('FILE "a.wav" WAVE\n TRACK 01 AUDIO\nFILE "b.wav" WAVE\n')


class TestTiming:
    def test_cumulative_durations(self):
        assert sa.starts_from_durations([61.5, 120.25, 30.0]) == [0.0, 61.5, 181.75]

    @pytest.mark.parametrize("secs,expected", [
        (0, "0:00.000"), (61.5, "1:01.500"), (3725.0004, "1:02:05.000"), (59.9996, "1:00.000"),
    ])
    def test_fmt_time(self, secs, expected):
        assert sa.fmt_time(secs) == expected


class TestNaming:
    @pytest.mark.parametrize("raw,expected", [
        ("AC/DC", "AC-DC"),
        ("Title: Subtitle", "Title - Subtitle"),
        ('What? "Why" <now>|*', "What_ 'Why' _now___"),
        ("  ...hidden.  ", "hidden"),
        ("tab\there", "tabhere"),
        ("", "_"),
    ])
    def test_sanitize(self, raw, expected):
        assert sa.sanitize(raw) == expected

    def test_sanitize_byte_limit(self):
        assert len(sa.sanitize("é" * 300, max_bytes=50).encode()) <= 50

    def test_track_filename(self):
        t = sa.Track(2, 3, "A/B", "x", [], 1.0, "", "")
        assert sa.track_filename(t, 12, False, "flac") == "03 - A-B.flac"
        assert sa.track_filename(t, 12, True, "mp3") == "2-03 - A-B.mp3"
        assert sa.track_filename(t, 120, False, "m4a") == "003 - A-B.m4a"


class TestSnapping:
    def test_snaps_to_silence_end(self):
        starts, notes = sa.snap_boundaries([0, 100, 200], [(98.5, 99.5), (201.0, 202.0)])
        assert starts == [0, 99.5, 202.0]
        assert notes == ["", "-0.50s", "+2.00s"]

    def test_inside_silence(self):
        starts, _ = sa.snap_boundaries([0, 100], [(99.0, 104.0)])
        assert starts == [0, 104.0]

    def test_no_silence_in_window(self):
        starts, notes = sa.snap_boundaries([0, 100], [(90.0, 95.0), (104.0, 105.0)])
        assert starts == [0, 100]
        assert notes[1] == "no silence"

    def test_picks_nearest(self):
        starts, _ = sa.snap_boundaries([0, 100], [(97.0, 98.0), (100.5, 101.0)])
        assert starts == [0, 101.0]

    def test_parse_silences(self):
        stderr = ("[silencedetect @ 0x1] silence_start: -0.01\n"
                  "[silencedetect @ 0x1] silence_end: 0.5 | silence_duration: 0.51\n"
                  "[silencedetect @ 0x1] silence_start: 10.2\n")
        assert sa.parse_silences(stderr, 12.0) == [(0.0, 0.5), (10.2, 12.0)]


class TestGenres:
    def test_discogs_genre_and_styles(self):
        r = sa.resolve_genre(["Electronic"], ["Deep House"], [], make_cfg(**LIB_CFG))
        assert r.value == "Electronic/Deep House"
        assert r.unknown == []

    def test_slash_label_is_one_alias(self):
        r = sa.resolve_genre(["Funk / Soul"], ["Disco"], [], make_cfg(**LIB_CFG))
        assert r.value == "Soul/Funk/Disco"

    def test_unmapped_slash_label_does_not_add_levels(self):
        r = sa.resolve_genre(["Electronic"], ["Cut-up/DJ"], [], make_cfg())
        assert r.value == "Electronic/Cut-up-DJ"

    def test_default_aliases_without_config(self):
        r = sa.resolve_genre(["Folk, World, & Country"], [], [], make_cfg())
        assert r.value == "Folk"

    def test_first_genre_is_root(self):
        r = sa.resolve_genre(["Electronic", "Funk / Soul"], ["House"], [], make_cfg(**LIB_CFG))
        assert r.value == "Electronic/House"

    def test_root_priority(self):
        cfg = make_cfg(**LIB_CFG, root_priority=["Soul", "Electronic"])
        r = sa.resolve_genre(["Electronic", "Funk / Soul"], ["Disco"], [], cfg)
        assert r.value == "Soul/Funk/Disco"

    def test_four_level_cap(self):
        r = sa.resolve_genre(["Electronic"], ["House", "Deep House", "Techno", "Electro"], [],
                             make_cfg(**LIB_CFG))
        assert r.value == "Electronic/House/Deep House/Techno"

    def test_dedupe_case_insensitive(self):
        r = sa.resolve_genre(["Electronic"], ["house", "House"], [], make_cfg(**LIB_CFG))
        assert r.value == "Electronic/House"

    def test_ignore(self):
        cfg = make_cfg(**LIB_CFG, ignore=["Deep House"])
        assert sa.resolve_genre(["Electronic"], ["Deep House"], [], cfg).value == "Electronic"

    def test_decade_only_for_decade_genres(self):
        cfg = make_cfg(**LIB_CFG)
        assert sa.resolve_genre(["Rock"], ["Indie Rock"], [], cfg, 1986, True).value == "80s/Rock/Indie"
        assert sa.resolve_genre(["Electronic"], ["House"], [], cfg, 1986, True).value == "Electronic/House"
        assert sa.resolve_genre(["Rock"], [], [], cfg, 1986, False).value == "Rock"
        assert sa.resolve_genre(["Rock"], [], [], cfg, 2004, True).value == "00s/Rock"

    def test_decade_limited_to_listed_decades(self):
        cfg = make_cfg(**LIB_CFG, decades=["80s"])
        assert sa.resolve_genre(["Rock"], [], [], cfg, 1984, True).value == "80s/Rock"
        assert sa.resolve_genre(["Rock"], [], [], cfg, 1975, True).value == "Rock"

    def test_decade_kept_within_cap(self):
        cfg = make_cfg(**LIB_CFG)
        r = sa.resolve_genre(["Rock"], ["A", "B", "C", "D"], [], cfg, 1975, True)
        assert r.value == "70s/Rock/A/B"

    def test_styles_keep_discogs_order(self):
        cfg = make_cfg(**LIB_CFG)
        r = sa.resolve_genre(["Funk / Soul"], ["Disco", "Soul"], [], cfg)
        assert r.value == "Soul/Funk/Disco"

    def test_mb_fallback_uses_parents(self):
        r = sa.resolve_genre([], [], ["deep house", "house", "ambient"], make_cfg(**LIB_CFG))
        assert r.value == "Electronic/Deep House/House"
        assert r.warnings

    def test_mb_fills_missing_styles(self):
        r = sa.resolve_genre(["Funk / Soul"], [], ["disco", "rock"], make_cfg(**LIB_CFG))
        assert r.value == "Soul/Funk/Disco"
        assert r.warnings

    def test_unknown_parts_reported(self):
        r = sa.resolve_genre(["Electronic"], ["Gqom"], [], make_cfg(**LIB_CFG))
        assert r.value == "Electronic/Gqom"
        assert r.unknown == ["Gqom"]

    def test_canonical_spelling(self):
        r = sa.resolve_genre(["electronic"], ["deep house"], [], make_cfg(**LIB_CFG))
        assert r.value == "Electronic/Deep House"


class TestMode:
    def test_lossless_to_flac(self):
        m = sa.choose_mode("pcm_s16le", "same")
        assert (m.ext, m.copy) == ("flac", False)

    def test_mp3_copy(self):
        m = sa.choose_mode("mp3", "same")
        assert (m.ext, m.copy) == ("mp3", True)

    def test_aac_copy(self):
        assert sa.choose_mode("aac", "m4a").copy

    def test_alac_to_m4a_stays_lossless(self):
        assert sa.choose_mode("alac", "same").ext == "flac"
        assert sa.choose_mode("flac", "m4a").encoder == ["-c:a", "alac"]

    def test_unknown_codec(self):
        with pytest.raises(sa.AppError):
            sa.choose_mode("opus", "same")


def test_discogs_id_from():
    assert sa.discogs_id_from("12345") == "12345"
    assert sa.discogs_id_from("r12345") == "12345"
    assert sa.discogs_id_from("https://www.discogs.com/release/12345-Artist-Title") == "12345"
    assert sa.discogs_id_from("https://www.discogs.com/master/999") is None


def test_build_genre_map_inference():
    counts = Counter({
        "Electronic/Deep House": 10, "Electronic/House": 4, "electronic/house": 1,
        "Rock/Indie/80s": 3, "Rock/Indie": 2, "Hip-Hop/Boom Bap": 2, "Soul/Funk/Disco": 5,
        "Soul/Drum n Bass": 1, "Electronic/Drum n Bass": 4,
    })
    cfg, notes = build_config(counts)
    assert cfg["roots"][0] == "Electronic"
    assert cfg["parents"]["Deep House"] == "Electronic"
    assert cfg["parents"]["Disco"] == "Soul"
    assert cfg["parents"]["Drum n Bass"] == "Electronic"
    assert "80s" not in cfg["parents"]
    assert cfg["aliases"]["electronic"] == "Electronic"
    assert cfg["aliases"]["Hip Hop"] == "Hip-Hop"
    assert cfg["aliases"]["Funk / Soul"] == "Soul/Funk"
    assert cfg["decade_genres"] == ["Rock"]
    assert cfg["decades"] == ["80s"]
    assert "Indie" in cfg["unrecognized"] and "80s" not in cfg["unrecognized"]
    assert "Deep House" not in cfg["unrecognized"]
    assert any("Drum n Bass" in n for n in notes)


def test_build_genre_map_follows_library_layout():
    counts = Counter({
        "80s/Rock": 6, "80s/Rock/Pop Rock": 2, "Funk/Soul": 5, "Funk/Soul/Disco": 3, "Soul/Funk": 1,
        "Soul/R&B": 4, "Country": 3, "Hip Hop/Boom Bap": 2,
    })
    cfg, _ = build_config(counts)
    assert "80s" not in cfg["roots"]
    assert cfg["parents"]["Pop Rock"] == "Rock"
    assert "Rock" not in cfg["parents"]
    assert cfg["decade_genres"] == ["Rock"]
    assert cfg["aliases"]["Funk / Soul"] == "Funk/Soul"
    assert cfg["aliases"]["Folk, World, & Country"] == "Country"
    assert cfg["aliases"]["Contemporary R&B"] == "R&B"
    assert cfg["aliases"]["Rhythm & Blues"] == "R&B"
    assert "Hip Hop" not in cfg["aliases"] or cfg["aliases"]["Hip Hop"] == "Hip Hop"

    rcfg = sa.GenreConfig.from_raw(cfg)
    assert sa.resolve_genre(["Rock"], ["Pop Rock"], [], rcfg, 1985, True).value == "80s/Rock/Pop Rock"
    assert sa.resolve_genre(["Funk / Soul"], ["Disco"], [], rcfg).value == "Funk/Soul/Disco"
    assert sa.resolve_genre(["Funk / Soul"], ["Contemporary R&B"], [], rcfg).value == "Funk/Soul/R&B"
    assert sa.resolve_genre(["Folk, World, & Country"], [], [], rcfg).value == "Country"
    assert sa.resolve_genre(["Hip Hop"], ["Boom Bap"], [], rcfg).value == "Hip Hop/Boom Bap"
