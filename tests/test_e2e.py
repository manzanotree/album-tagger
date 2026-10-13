import math
import struct
import subprocess
import wave
from collections import Counter
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

import split_album as sa
from build_genre_map import empty_config, save_genre_config, scan_library

RATE = 44100
TONES = [(440, 5.0), (660, 4.0), (880, 6.0)]
GAP = 1.0
MBID = "11111111-1111-1111-1111-111111111111"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def write_album_wav(path: Path) -> None:
    frames = bytearray()
    for i, (freq, secs) in enumerate(TONES):
        for n in range(int(secs * RATE)):
            frames += struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * n / RATE)))
        if i < len(TONES) - 1:
            frames += b"\x00\x00" * int(GAP * RATE)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(frames))


def mb_release(lengths_ms):
    tracks = [{
        "id": f"track-{i}", "position": i, "title": f"Tone {chr(64 + i)}", "length": ms,
        "artist-credit": [{"name": "Sine Wave", "joinphrase": "", "artist": {"id": "artist-1"}}],
        "recording": {"id": f"rec-{i}", "title": f"Tone {chr(64 + i)}", "length": ms},
    } for i, ms in enumerate(lengths_ms, 1)]
    return {
        "id": MBID, "title": "Test Tones", "date": "1999-05-01",
        "artist-credit": [{"name": "Sine Wave", "joinphrase": "", "artist": {"id": "artist-1"}}],
        "release-group": {"id": "rg-1", "first-release-date": "1987-03-02", "genres": []},
        "label-info": [{"catalog-number": "TONE001", "label": {"name": "Test Label"}}],
        "media": [{"position": 1, "tracks": tracks}],
        "relations": [{"type": "discogs", "url": {"resource": "https://www.discogs.com/release/4242"}}],
        "genres": [{"name": "house", "count": 2}],
    }


class FakeResponse:
    def __init__(self, status=200, payload=None, content=b""):
        self.status_code = status
        self._payload = payload
        self.content = content
        self.text = ""
        self.headers = {}

    def json(self):
        return self._payload


@pytest.fixture
def mocked(monkeypatch, tmp_path):
    calls = []
    # MB lengths are off by up to 0.8s so --detect-silence must fix them.
    release = mb_release([5600, 5400, 6000])

    def fake_get(url, params=None, headers=None, retries=5):
        calls.append(url)
        if url.startswith(f"{sa.MB_API}/release/{MBID}"):
            return FakeResponse(payload=release)
        if url == f"{sa.DISCOGS_API}/releases/4242":
            assert headers["Authorization"] == "Discogs token=test-token"
            return FakeResponse(payload={"id": 4242, "year": 1987, "genres": ["Electronic"],
                                         "styles": ["Deep House"]})
        if url.startswith(sa.CAA):
            return FakeResponse(content=JPEG)
        raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr(sa, "http_get", fake_get)
    monkeypatch.setenv("DISCOGS_TOKEN", "test-token")
    genres = tmp_path / "genres.yaml"
    cfg = empty_config()
    cfg.update(roots=["Electronic"], parents={"Deep House": "Electronic"})
    save_genre_config(cfg, genres)
    src = tmp_path / "album.wav"
    write_album_wav(src)
    return src, genres, calls


def durations(paths):
    out = []
    for p in paths:
        proc = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                               "-of", "csv=p=0", str(p)], capture_output=True, text=True, check=True)
        out.append(float(proc.stdout))
    return out


def test_split_flac_with_silence_snap(mocked, tmp_path):
    src, genres, _ = mocked
    out = tmp_path / "out"
    rc = sa.main([str(src), "--mbid", MBID, "--detect-silence", "--yes",
                  "--genres-file", str(genres), "--out", str(out)])
    assert rc == 0
    files = sorted(out.glob("*.flac"))
    assert [f.name for f in files] == ["01 - Tone A.flac", "02 - Tone B.flac", "03 - Tone C.flac"]
    assert (out / "cover.jpg").read_bytes() == JPEG
    assert durations(files) == pytest.approx([6.0, 5.0, 6.0], abs=0.01)

    tags = FLAC(files[1])
    assert tags["TITLE"] == ["Tone B"]
    assert tags["GENRE"] == ["Electronic/Deep House"]
    assert tags["TRACKNUMBER"] == ["2"] and tags["TRACKTOTAL"] == ["3"]
    assert tags["DATE"] == ["1999-05-01"] and tags["ORIGINALDATE"] == ["1987-03-02"]
    assert tags["LABEL"] == ["Test Label"] and tags["CATALOGNUMBER"] == ["TONE001"]
    assert tags["MUSICBRAINZ_ALBUMID"] == [MBID]
    assert tags["MUSICBRAINZ_TRACKID"] == ["rec-2"]
    assert tags["MUSICBRAINZ_RELEASETRACKID"] == ["track-2"]
    assert tags["DISCOGS_RELEASE_ID"] == ["4242"]
    assert tags.pictures[0].data == JPEG


def test_split_mp3_stream_copy(mocked, tmp_path):
    src, genres, _ = mocked
    mp3 = tmp_path / "album.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-c:a", "libmp3lame", "-b:a", "192k",
                    str(mp3)], check=True)
    out = tmp_path / "out"
    rc = sa.main([str(mp3), "--mbid", MBID, "--detect-silence", "--yes", "--decade",
                  "--genres-file", str(genres), "--out", str(out)])
    assert rc == 0
    files = sorted(out.glob("*.mp3"))
    assert len(files) == 3
    assert durations(files) == pytest.approx([6.0, 5.0, 6.0], abs=0.1)
    id3 = ID3(files[0])
    assert id3.version[:2] == (2, 3)
    assert id3["TCON"].text == ["Electronic/Deep House"]
    assert id3["TRCK"].text == ["1/3"]
    assert id3["UFID:http://musicbrainz.org"].data == b"rec-1"
    assert id3["TXXX:MusicBrainz Album Id"].text == [MBID]
    assert id3.getall("APIC")[0].data == JPEG
    assert scan_library(out) == Counter({"Electronic/Deep House": 3})


def test_split_m4a_stream_copy(mocked, tmp_path):
    src, genres, _ = mocked
    m4a = tmp_path / "album.m4a"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-c:a", "aac", "-b:a", "192k",
                    str(m4a)], check=True)
    out = tmp_path / "out"
    rc = sa.main([str(m4a), "--mbid", MBID, "--detect-silence", "--yes",
                  "--genres-file", str(genres), "--out", str(out)])
    assert rc == 0
    files = sorted(out.glob("*.m4a"))
    assert durations(files) == pytest.approx([6.0, 5.0, 6.0], abs=0.1)
    tags = MP4(files[2])
    assert tags["\xa9gen"] == ["Electronic/Deep House"]
    assert tags["trkn"] == [(3, 3)]
    assert bytes(tags["----:com.apple.iTunes:MusicBrainz Track Id"][0]) == b"rec-3"
    assert bytes(tags["covr"][0]) == JPEG
    assert scan_library(out) == Counter({"Electronic/Deep House": 3})


def test_dry_run_writes_nothing(mocked, tmp_path, capsys):
    src, genres, calls = mocked
    out = tmp_path / "out"
    rc = sa.main([str(src), "--mbid", MBID, "--dry-run", "--genres-file", str(genres),
                  "--out", str(out)])
    assert rc == 0
    assert not out.exists()
    assert not any(c.startswith(sa.CAA) for c in calls)
    text = capsys.readouterr().out
    assert "Electronic/Deep House" in text
    assert "styles: Deep House" in text
    assert "01 - Tone A.flac" in text


def test_duration_mismatch_aborts(mocked, tmp_path, monkeypatch, capsys):
    src, genres, _ = mocked
    monkeypatch.setattr(sa, "fetch_release", lambda mbid: sa.parse_release(mb_release([5000, 4000, 2000])))
    rc = sa.main([str(src), "--mbid", MBID, "--yes", "--dry-run", "--genres-file", str(genres)])
    assert rc == 1
    assert "--force" in capsys.readouterr().err
    rc = sa.main([str(src), "--mbid", MBID, "--yes", "--dry-run", "--force", "--genres-file", str(genres)])
    assert rc == 0
