"""Split a whole-album audio file into tagged tracks.

Track titles, durations and IDs come from MusicBrainz. Genres come from Discogs
(falling back to MusicBrainz) and are normalized with genres.yaml into one
"General/Specific" string. Split points come from a cue sheet or cumulative
MusicBrainz durations, optionally snapped to detected silence.
Lossless input is re-encoded to FLAC with sample-accurate cuts. MP3/M4A input is
stream-copied.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import mutagen
import requests
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC, ID3, TALB, TCON, TDOR, TDRC, TIT2, TPE1, TPE2, TPOS, TPUB, TRCK, TXXX, UFID,
)
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

from build_genre_map import DEFAULT_ALIASES, load_genre_config, save_genre_config

VERSION = "0.1.0"
MB_API = "https://musicbrainz.org/ws/2"
DISCOGS_API = "https://api.discogs.com"
CAA = "https://coverartarchive.org"
DEFAULT_GENRES_FILE = Path(__file__).with_name("genres.yaml")

DURATION_TOLERANCE = 5.0
SNAP_WINDOW = 3.0
HINT_RANGE = 60.0
MAX_GENRE_LEVELS = 4
HOST_INTERVAL = {"musicbrainz.org": 1.0, "api.discogs.com": 1.0}
LOSSLESS_CODECS = {"flac", "alac", "wavpack", "ape", "tta"}
LOSSY_COPY = {"mp3": "mp3", "aac": "m4a"}
ENCODERS = {
    "flac": ["-c:a", "flac"],
    "mp3": ["-c:a", "libmp3lame", "-b:a", "320k"],
    "m4a_lossless": ["-c:a", "alac"],
    "m4a": ["-c:a", "aac", "-b:a", "256k"],
}


class AppError(Exception):
    pass


def info(msg: str) -> None:
    print(msg, file=sys.stderr)


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


# ---------------------------------------------------------------- HTTP

_last_request: dict[str, float] = {}


def user_agent() -> str:
    contact = os.environ.get("MB_CONTACT", "album-tagger-local")
    return f"album-tagger/{VERSION} ( {contact} )"


def http_get(url: str, params: dict[str, Any] | None = None,
             headers: dict[str, str] | None = None, retries: int = 5) -> requests.Response:
    """GET with per-host rate limiting and retry on network errors, 429 and 5xx."""
    host = urlparse(url).hostname or ""
    hdrs = {"User-Agent": user_agent(), **(headers or {})}
    err = ""
    for attempt in range(retries):
        wait = HOST_INTERVAL.get(host, 0.0) - (time.monotonic() - _last_request.get(host, -1e9))
        if wait > 0:
            time.sleep(wait)
        _last_request[host] = time.monotonic()
        delay = float(2 ** attempt)
        try:
            resp = requests.get(url, params=params, headers=hdrs, timeout=30)
        except (requests.ConnectionError, requests.Timeout) as exc:
            err = type(exc).__name__
        else:
            if resp.status_code not in (429, 500, 502, 503, 504):
                return resp
            err = f"HTTP {resp.status_code}"
            try:
                delay = max(delay, float(resp.headers.get("Retry-After", 0)))
            except ValueError:
                pass
        if attempt < retries - 1:
            warn(f"{host}: {err}. Retrying in {delay:.0f}s")
            time.sleep(delay)
    raise AppError(f"{host}: giving up after {retries} attempts ({err})")


def get_json(url: str, params: dict[str, Any] | None = None,
             headers: dict[str, str] | None = None) -> Any:
    resp = http_get(url, params=params, headers=headers)
    if resp.status_code == 404:
        raise AppError(f"not found: {url}")
    if resp.status_code >= 400:
        raise AppError(f"HTTP {resp.status_code} from {url}: {resp.text[:200]}")
    return resp.json()


# ---------------------------------------------------------------- prompts

def prompt(text: str, default: str = "") -> str:
    try:
        answer = input(text).strip()
    except EOFError:
        answer = ""
    return answer or default


def choose(title: str, options: list[str], assume_yes: bool) -> int:
    """Pick an option by number. With --yes, take the first and log it."""
    if not options:
        raise AppError(f"{title}: no candidates")
    if assume_yes or len(options) == 1:
        info(f"{title}: using {options[0]}")
        return 0
    info(title)
    for i, opt in enumerate(options, 1):
        info(f"  {i}. {opt}")
    while True:
        answer = prompt(f"Choose 1-{len(options)} [1]: ", "1")
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1
        if answer.lower() in ("q", "quit"):
            raise AppError("aborted")


# ---------------------------------------------------------------- MusicBrainz

@dataclass
class Track:
    disc: int
    number: int
    title: str
    artist: str
    artist_ids: list[str]
    length: float | None
    recording_id: str
    track_id: str


@dataclass
class Release:
    mbid: str
    title: str
    artist: str
    artist_ids: list[str]
    date: str
    original_date: str
    label: str
    catno: str
    release_group_id: str
    discogs_ids: list[str]
    media: list[list[Track]]
    genres: list[str] = field(default_factory=list)
    group_genres: list[str] = field(default_factory=list)

    @property
    def year(self) -> int | None:
        for d in (self.original_date, self.date):
            if d[:4].isdigit():
                return int(d[:4])
        return None


def join_credit(credit: list[dict[str, Any]]) -> tuple[str, list[str]]:
    name = "".join(c.get("name", c.get("artist", {}).get("name", "")) + c.get("joinphrase", "")
                   for c in credit)
    return name, [c["artist"]["id"] for c in credit if c.get("artist", {}).get("id")]


def sorted_genres(entries: list[dict[str, Any]] | None) -> list[str]:
    return [g["name"] for g in sorted(entries or [], key=lambda g: -g.get("count", 0))]


def discogs_id_from(text: str) -> str | None:
    text = text.strip()
    if re.fullmatch(r"r?\d+", text):
        return text.lstrip("r")
    m = re.search(r"/release/(\d+)", text)
    return m.group(1) if m else None


def parse_release(data: dict[str, Any]) -> Release:
    artist, artist_ids = join_credit(data.get("artist-credit", []))
    group = data.get("release-group", {})
    labels = data.get("label-info") or [{}]
    media: list[list[Track]] = []
    for disc_index, medium in enumerate(data.get("media", []), 1):
        disc = medium.get("position", disc_index)
        tracks = []
        for i, t in enumerate(medium.get("tracks", []), 1):
            rec = t.get("recording", {})
            t_artist, t_ids = join_credit(t.get("artist-credit") or rec.get("artist-credit") or [])
            length_ms = t.get("length") or rec.get("length")
            tracks.append(Track(
                disc=disc,
                number=t.get("position", i),
                title=t.get("title") or rec.get("title", ""),
                artist=t_artist or artist,
                artist_ids=t_ids or artist_ids,
                length=length_ms / 1000 if length_ms else None,
                recording_id=rec.get("id", ""),
                track_id=t.get("id", ""),
            ))
        media.append(tracks)
    discogs_ids = []
    for rel in data.get("relations", []):
        if rel.get("type") == "discogs":
            found = discogs_id_from(rel.get("url", {}).get("resource", ""))
            if found:
                discogs_ids.append(found)
    return Release(
        mbid=data["id"],
        title=data.get("title", ""),
        artist=artist,
        artist_ids=artist_ids,
        date=data.get("date", ""),
        original_date=group.get("first-release-date", ""),
        label=(labels[0].get("label") or {}).get("name", ""),
        catno=labels[0].get("catalog-number") or "",
        release_group_id=group.get("id", ""),
        discogs_ids=discogs_ids,
        media=media,
        genres=sorted_genres(data.get("genres")),
        group_genres=sorted_genres(group.get("genres")),
    )


def fetch_release(mbid: str) -> Release:
    inc = "recordings+artist-credits+labels+release-groups+url-rels+genres"
    return parse_release(get_json(f"{MB_API}/release/{mbid}", {"inc": inc, "fmt": "json"}))


def fetch_artist_genres(artist_id: str) -> list[str]:
    data = get_json(f"{MB_API}/artist/{artist_id}", {"inc": "genres", "fmt": "json"})
    return sorted_genres(data.get("genres"))


def lucene_quote(text: str) -> str:
    return '"' + re.sub(r'([\\"])', r"\\\1", text) + '"'


def existing_tags(path: Path) -> tuple[str, str]:
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:
        return "", ""
    tags = audio.tags if audio is not None and audio.tags is not None else {}
    artist = (tags.get("albumartist") or tags.get("artist") or [""])[0]
    album = (tags.get("album") or [""])[0]
    return artist, album


def search_musicbrainz(path: Path, assume_yes: bool) -> str:
    artist, album = existing_tags(path)
    if not (artist and album):
        if assume_yes:
            raise AppError("--mbid is required with --yes when the file has no artist/album tags")
        artist = artist or prompt("Artist: ")
        album = album or prompt("Album: ")
    query = f"release:{lucene_quote(album)} AND artist:{lucene_quote(artist)}"
    data = get_json(f"{MB_API}/release/", {"query": query, "fmt": "json", "limit": 10})
    releases = data.get("releases", [])
    options = []
    for r in releases:
        credit, _ = join_credit(r.get("artist-credit", []))
        fmt = "+".join(m.get("format", "?") for m in r.get("media", []))
        count = sum(m.get("track-count", 0) for m in r.get("media", []))
        options.append(f"{credit} - {r.get('title')} ({r.get('date', '?')}, {r.get('country', '?')}, "
                       f"{fmt}, {count} tracks) [{r['id']}]")
    if not options:
        raise AppError(f"no MusicBrainz releases found for {artist} - {album}")
    return releases[choose(f"MusicBrainz matches for {artist} - {album}", options, assume_yes)]["id"]


# ---------------------------------------------------------------- Discogs

def discogs_headers() -> dict[str, str]:
    return {"Authorization": f"Discogs token={os.environ['DISCOGS_TOKEN']}"}


def resolve_discogs(arg: str | None, release: Release, assume_yes: bool) -> dict[str, Any] | None:
    """Discogs release JSON: --discogs-release, then MB URL relation, then search."""
    if not os.environ.get("DISCOGS_TOKEN"):
        warn("DISCOGS_TOKEN not set. Falling back to MusicBrainz genres")
        return None
    if arg:
        release_id = discogs_id_from(arg)
        if not release_id:
            raise AppError(f"cannot parse Discogs release from {arg!r}")
    elif release.discogs_ids:
        release_id = release.discogs_ids[0]
    else:
        release_id = search_discogs(release, assume_yes)
        if not release_id:
            return None
    try:
        return get_json(f"{DISCOGS_API}/releases/{release_id}", headers=discogs_headers())
    except AppError as exc:
        warn(f"Discogs lookup failed ({exc}). Falling back to MusicBrainz genres")
        return None


def search_discogs(release: Release, assume_yes: bool) -> str | None:
    params: dict[str, Any] = {"type": "release", "artist": release.artist,
                              "release_title": release.title, "per_page": 10}
    results = []
    for year in ([release.year, None] if release.year else [None]):
        if year:
            params["year"] = year
        else:
            params.pop("year", None)
        try:
            results = get_json(f"{DISCOGS_API}/database/search", params, discogs_headers()).get("results", [])
        except AppError as exc:
            warn(f"Discogs search failed: {exc}")
            return None
        if results:
            break
    if not results:
        warn(f"no Discogs match for {release.artist} - {release.title}")
        return None
    options = [f"{r.get('title')} ({r.get('year', '?')}, {r.get('country', '?')}, "
               f"{', '.join(r.get('format', []))}, {r.get('catno', '')}) [r{r['id']}]" for r in results]
    return str(results[choose("Discogs matches", options, assume_yes)]["id"])


# ---------------------------------------------------------------- genres

@dataclass
class GenreConfig:
    raw: dict[str, Any]
    aliases: dict[str, str]
    parents: dict[str, str]
    canonical: dict[str, str]
    roots: set[str]
    ignore: set[str]
    decade_genres: set[str]
    decades: set[str]
    root_priority: list[str]

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> GenreConfig:
        aliases = {k.lower(): v for k, v in DEFAULT_ALIASES.items()}
        aliases.update({str(k).lower(): str(v) for k, v in raw["aliases"].items()})
        parents = {str(k).lower(): str(v) for k, v in raw["parents"].items()}
        canonical: dict[str, str] = {}
        names = (list(raw["roots"]) + list(raw["parents"]) + list(raw["parents"].values())
                 + list(raw["decade_genres"]) + list(raw["root_priority"]))
        for target in raw["aliases"].values():
            names.extend(str(target).split("/"))
        for name in names:
            canonical.setdefault(str(name).strip().lower(), str(name).strip())
        roots = {str(r).lower() for r in raw["roots"]} | {v.lower() for v in parents.values()}
        return cls(
            raw=raw,
            aliases=aliases,
            parents=parents,
            canonical=canonical,
            roots=roots | {str(r).lower() for r in raw["root_priority"]},
            ignore={str(i).lower() for i in raw["ignore"]},
            decade_genres={str(d).lower() for d in raw["decade_genres"]},
            decades={str(d) for d in raw["decades"]},
            root_priority=[str(r).lower() for r in raw["root_priority"]],
        )

    def known(self, part: str) -> bool:
        return part.lower() in self.canonical

    def normalize(self, label: str) -> list[str]:
        """Map one source label to path parts via aliases and canonical spelling."""
        label = label.strip()
        target = self.aliases.get(label.lower())
        if target is not None:
            parts = target.split("/")
        else:
            # Unmapped labels must not inject slashes.
            parts = [label.replace(" / ", " & ").replace("/", "-")]
        out = []
        for part in (p.strip() for p in parts):
            if part and part.lower() not in self.ignore:
                out.append(self.canonical.get(part.lower(), part))
        return out


@dataclass
class GenreResult:
    value: str
    unknown: list[str]
    warnings: list[str]


def decade_of(year: int) -> str:
    return f"{year // 10 * 10 % 100:02d}s"


def dedupe(parts: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for p in parts:
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


def place_mb(names: list[str], cfg: GenreConfig, root: str | None = None) -> list[str]:
    """Order MusicBrainz genres as root + specifics using `parents`."""
    parts = dedupe([p for n in names for p in cfg.normalize(n)])
    if root is None:
        for p in parts:
            if p.lower() in cfg.roots:
                root = p
                break
            if p.lower() in cfg.parents:
                root = cfg.parents[p.lower()]
                break
        else:
            return parts
        root = cfg.canonical.get(root.lower(), root)
    specifics = [p for p in parts if cfg.parents.get(p.lower(), "").lower() == root.lower()]
    return [root] + specifics


def resolve_genre(discogs_genres: list[str], discogs_styles: list[str], mb_genres: list[str],
                  cfg: GenreConfig, year: int | None = None, decade: bool = False) -> GenreResult:
    warnings: list[str] = []
    roots = [r for r in (cfg.normalize(g) for g in discogs_genres) if r]
    styles = [p for s in discogs_styles for p in cfg.normalize(s)]
    if roots:
        if cfg.root_priority:
            rank = {name: i for i, name in enumerate(cfg.root_priority)}
            roots.sort(key=lambda r: rank.get(r[0].lower(), len(rank)))
        path = list(roots[0])
        if not styles and mb_genres:
            warnings.append("Discogs has no styles. Adding MusicBrainz genres under the Discogs root")
            styles = place_mb(mb_genres, cfg, root=path[0])[1:]
        path += styles
    elif mb_genres:
        warnings.append("no Discogs genres. Using MusicBrainz genres")
        path = place_mb(mb_genres, cfg)
    else:
        path = []

    path = dedupe(path)
    if decade and year and path and path[0].lower() in cfg.decade_genres:
        tag = decade_of(year)
        if not cfg.decades or tag in cfg.decades:
            path = [tag] + path[:MAX_GENRE_LEVELS - 1]
    path = path[:MAX_GENRE_LEVELS]
    unknown = [p for p in path if not cfg.known(p) and not re.fullmatch(r"\d0s", p)]
    return GenreResult("/".join(path), unknown, warnings)


def confirm_genre(result: GenreResult, cfg: GenreConfig, genres_file: Path, interactive: bool) -> str:
    """Let the user accept/edit a genre with unknown parts and save new rules."""
    if not result.unknown:
        return result.value
    if not interactive:
        info(f"genre: unknown parts {result.unknown}. Using best guess {result.value!r}")
        return result.value
    info(f"Unknown genre parts: {', '.join(result.unknown)}")
    value = prompt(f"Genre [{result.value}]: ", result.value)
    parts = [p.strip() for p in value.split("/") if p.strip()]
    genre_parts = [p for p in parts if not re.fullmatch(r"\d0s", p)]
    new = [p for p in genre_parts[1:] if not cfg.known(p)]
    if new:
        root = genre_parts[0]
        answer = prompt(f"Save {', '.join(f'{p} -> {root}' for p in new)} to {genres_file.name}? [y/N]: ")
        if answer.lower().startswith("y"):
            for p in new:
                cfg.raw["parents"][p] = root
            if root.lower() not in {str(r).lower() for r in cfg.raw["roots"]}:
                cfg.raw["roots"].append(root)
            save_genre_config(cfg.raw, genres_file)
            info(f"saved {genres_file}")
    return "/".join(parts)


# ---------------------------------------------------------------- split points

def parse_cue(text: str) -> list[float]:
    """Return INDEX 01 start times in seconds, one per track."""
    files = re.findall(r'^\s*FILE\s', text, re.MULTILINE | re.IGNORECASE)
    if len(files) > 1:
        raise AppError("cue sheets with several FILE entries are not supported")
    starts: list[float] = []
    in_track = False
    for line in text.splitlines():
        words = line.split()
        if not words:
            continue
        cmd = words[0].upper()
        if cmd == "TRACK":
            in_track = True
            starts.append(-1.0)
        elif cmd == "INDEX" and in_track and len(words) >= 3 and words[1] == "01":
            m = re.fullmatch(r"(\d+):(\d{2}):(\d{2})", words[2])
            if not m:
                raise AppError(f"bad cue INDEX time: {words[2]}")
            mins, secs, frames = map(int, m.groups())
            starts[-1] = mins * 60 + secs + frames / 75
    if not starts or any(s < 0 for s in starts):
        raise AppError("cue sheet has a track without INDEX 01")
    return starts


def starts_from_durations(lengths: list[float]) -> list[float]:
    starts, t = [], 0.0
    for length in lengths:
        starts.append(t)
        t += length
    return starts


def parse_time(text: str) -> float:
    """Parse "2513.7", "41:53.7" or "1:02:03.5" into seconds."""
    if not re.fullmatch(r"\d+(:\d{1,2}){0,2}(\.\d+)?", text.strip()):
        raise ValueError(f"bad time {text!r}. Use m:ss, h:mm:ss or seconds")
    seconds = 0.0
    for part in text.strip().split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def parse_pin(text: str) -> tuple[str, float]:
    """Parse a --start value like "10=41:53.7" or "2-5=3:10"."""
    track, sep, time_text = text.partition("=")
    if not sep or not re.fullmatch(r"(\d+-)?\d+", track.strip()):
        raise argparse.ArgumentTypeError(f"expected TRACK=TIME, got {text!r}")
    try:
        return track.strip(), parse_time(time_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def nearest_silence_end(target: float, silences: list[tuple[float, float]],
                        window: float, after: float = -1.0) -> float | None:
    best: float | None = None
    for s, e in silences:
        dist = 0.0 if s <= target <= e else min(abs(target - s), abs(target - e))
        if dist <= window and e > after and (best is None or abs(e - target) < abs(best - target)):
            best = e
    return best


def snap_boundaries(starts: list[float], silences: list[tuple[float, float]] | None,
                    window: float = SNAP_WINDOW, pins: dict[int, float] | None = None
                    ) -> tuple[list[float], list[str]]:
    """Move each track start (after the first) to the end of the nearest silence.

    Each expected start is shifted by the offset of the previous snap, so drift
    doesn't add up. `pins` maps a track index to a fixed start. A pin fixes one
    boundary and leaves the drift alone. With silences=None, only pins apply."""
    pins = pins or {}
    out: list[float] = []
    notes: list[str] = []
    offset = 0.0
    for i, b in enumerate(starts):
        target = b + offset
        if i in pins:
            out.append(pins[i])
            notes.append("pinned")
            continue
        best = None
        if i > 0 and silences is not None:
            best = nearest_silence_end(target, silences, window, out[-1])
        if best is not None:
            offset = best - b
            out.append(best)
            notes.append(f"{offset:+.2f}s")
            continue
        out.append(target)
        if i == 0 or silences is None:
            notes.append(f"{offset:+.2f}s" if offset else "")
        else:
            notes.append(f"no silence {offset:+.2f}s" if offset else "no silence")
    return out, notes


def detect_silence(path: Path, duration: float, noise_db: int = -50, min_len: float = 0.5
                   ) -> list[tuple[float, float]]:
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path), "-map", "0:a:0",
           "-af", f"silencedetect=noise={noise_db}dB:d={min_len}", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AppError(f"silencedetect failed: {proc.stderr[-500:]}")
    return parse_silences(proc.stderr, duration)


def parse_silences(stderr: str, duration: float) -> list[tuple[float, float]]:
    intervals: list[tuple[float, float]] = []
    start: float | None = None
    for line in stderr.splitlines():
        if m := re.search(r"silence_start: (-?[\d.]+)", line):
            start = max(0.0, float(m.group(1)))
        elif (m := re.search(r"silence_end: ([\d.]+)", line)) and start is not None:
            intervals.append((start, float(m.group(1))))
            start = None
    if start is not None:
        intervals.append((start, duration))
    return intervals


def fmt_time(seconds: float) -> str:
    ms = round(seconds * 1000)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h}:{m:02d}:{s:02d}.{ms:03d}" if h else f"{m}:{s:02d}.{ms:03d}"


# ---------------------------------------------------------------- ffmpeg

@dataclass
class Probe:
    duration: float
    codec: str
    sample_rate: int


def probe_audio(path: Path) -> Probe:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "a:0",
           "-show_entries", "stream=codec_name,sample_rate:format=duration", "-of", "json", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AppError(f"ffprobe failed on {path}: {proc.stderr.strip()}")
    data = json.loads(proc.stdout)
    if not data.get("streams"):
        raise AppError(f"no audio stream in {path}")
    stream = data["streams"][0]
    return Probe(float(data["format"]["duration"]), stream["codec_name"], int(stream["sample_rate"]))


@dataclass
class Mode:
    ext: str
    copy: bool
    encoder: list[str]
    label: str


def choose_mode(codec: str, target: str) -> Mode:
    lossless = codec in LOSSLESS_CODECS or codec.startswith("pcm_")
    source = "lossless" if lossless else LOSSY_COPY.get(codec)
    if target == "same":
        if lossless:
            target = "flac"
        elif source:
            target = source
        else:
            raise AppError(f"unsupported input codec {codec!r}. Pass --format")
    if not lossless and source == target:
        return Mode(target, True, ["-c", "copy"], f"{codec} -> {target} (stream copy)")
    if not lossless:
        warn(f"transcoding lossy {codec} to {target}")
    key = "m4a_lossless" if target == "m4a" and lossless else target
    return Mode(target, False, ENCODERS[key], f"{codec} -> {target} (re-encode, sample-accurate)")


def ffmpeg_cut(src: Path, dst: Path, start: float, end: float | None, mode: Mode, sample_rate: int) -> None:
    base = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
    if mode.copy:
        cmd = base + ["-ss", f"{start:.6f}", "-i", str(src)]
        if end is not None:
            cmd += ["-t", f"{end - start:.6f}"]
    else:
        trim = f"atrim=start_sample={round(start * sample_rate)}"
        if end is not None:
            trim += f":end_sample={round(end * sample_rate)}"
        cmd = base + ["-i", str(src), "-af", f"{trim},asetpts=PTS-STARTPTS"]
    cmd += ["-map", "0:a:0", "-map_metadata", "-1", "-map_chapters", "-1", *mode.encoder, str(dst)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AppError(f"ffmpeg failed for {dst.name}: {proc.stderr.strip()[-500:]}")


# ---------------------------------------------------------------- tagging

def cover_mime(data: bytes) -> str:
    return "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"


def write_tags(path: Path, tags: dict[str, Any], cover: bytes | None) -> None:
    tags = {k: v for k, v in tags.items() if v not in (None, "", [])}
    ext = path.suffix.lower()
    if ext == ".flac":
        tag_flac(path, tags, cover)
    elif ext == ".mp3":
        tag_mp3(path, tags, cover)
    elif ext == ".m4a":
        tag_m4a(path, tags, cover)
    else:
        raise AppError(f"cannot tag {ext} files")


VORBIS_KEYS = {
    "title": "TITLE", "artist": "ARTIST", "album": "ALBUM", "albumartist": "ALBUMARTIST",
    "tracknumber": "TRACKNUMBER", "tracktotal": "TRACKTOTAL", "discnumber": "DISCNUMBER",
    "disctotal": "DISCTOTAL", "date": "DATE", "originaldate": "ORIGINALDATE", "label": "LABEL",
    "catalognumber": "CATALOGNUMBER", "genre": "GENRE",
    "mb_albumid": "MUSICBRAINZ_ALBUMID", "mb_releasegroupid": "MUSICBRAINZ_RELEASEGROUPID",
    "mb_recordingid": "MUSICBRAINZ_TRACKID", "mb_releasetrackid": "MUSICBRAINZ_RELEASETRACKID",
    "mb_artistids": "MUSICBRAINZ_ARTISTID", "mb_albumartistids": "MUSICBRAINZ_ALBUMARTISTID",
    "discogs_release_id": "DISCOGS_RELEASE_ID",
}


def as_list(value: Any) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def tag_flac(path: Path, tags: dict[str, Any], cover: bytes | None) -> None:
    audio = FLAC(path)
    audio.delete()
    for key, value in tags.items():
        audio[VORBIS_KEYS[key]] = as_list(value)
    audio.clear_pictures()
    if cover:
        pic = Picture()
        pic.type, pic.mime, pic.data = 3, cover_mime(cover), cover
        audio.add_picture(pic)
    audio.save()


# Picard's tag names, so Picard and Navidrome read the IDs.
MB_TXXX = {
    "mb_albumid": "MusicBrainz Album Id", "mb_releasegroupid": "MusicBrainz Release Group Id",
    "mb_releasetrackid": "MusicBrainz Release Track Id", "mb_artistids": "MusicBrainz Artist Id",
    "mb_albumartistids": "MusicBrainz Album Artist Id", "catalognumber": "CATALOGNUMBER",
    "discogs_release_id": "DISCOGS_RELEASE_ID",
}


def tag_mp3(path: Path, tags: dict[str, Any], cover: bytes | None) -> None:
    id3 = ID3()
    simple = {"title": TIT2, "artist": TPE1, "album": TALB, "albumartist": TPE2,
              "date": TDRC, "originaldate": TDOR, "label": TPUB, "genre": TCON}
    for key, frame in simple.items():
        if key in tags:
            id3.add(frame(encoding=3, text=[str(tags[key])]))
    if "tracknumber" in tags:
        id3.add(TRCK(encoding=3, text=[f"{tags['tracknumber']}/{tags.get('tracktotal', '')}".rstrip("/")]))
    if "discnumber" in tags:
        id3.add(TPOS(encoding=3, text=[f"{tags['discnumber']}/{tags.get('disctotal', '')}".rstrip("/")]))
    for key, desc in MB_TXXX.items():
        if key in tags:
            id3.add(TXXX(encoding=3, desc=desc, text=as_list(tags[key])))
    if "mb_recordingid" in tags:
        id3.add(UFID(owner="http://musicbrainz.org", data=tags["mb_recordingid"].encode()))
    if cover:
        id3.add(APIC(encoding=3, mime=cover_mime(cover), type=3, desc="Cover", data=cover))
    # Serato reads ID3v2.3 most reliably.
    id3.save(path, v2_version=3)


MP4_KEYS = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "albumartist": "aART",
            "date": "\xa9day", "genre": "\xa9gen"}
MP4_FREEFORM = {**MB_TXXX, "mb_recordingid": "MusicBrainz Track Id", "label": "LABEL",
                "originaldate": "ORIGINALDATE"}


def tag_m4a(path: Path, tags: dict[str, Any], cover: bytes | None) -> None:
    audio = MP4(path)
    audio.delete()
    audio.tags = None
    audio.add_tags()
    for key, atom in MP4_KEYS.items():
        if key in tags:
            audio[atom] = [str(tags[key])]
    if "tracknumber" in tags:
        audio["trkn"] = [(int(tags["tracknumber"]), int(tags.get("tracktotal", 0)))]
    if "discnumber" in tags:
        audio["disk"] = [(int(tags["discnumber"]), int(tags.get("disctotal", 0)))]
    for key, name in MP4_FREEFORM.items():
        if key in tags:
            audio[f"----:com.apple.iTunes:{name}"] = [MP4FreeForm(v.encode()) for v in as_list(tags[key])]
    if cover:
        fmt = MP4Cover.FORMAT_PNG if cover_mime(cover) == "image/png" else MP4Cover.FORMAT_JPEG
        audio["covr"] = [MP4Cover(cover, imageformat=fmt)]
    audio.save()


def fetch_cover(release: Release) -> bytes | None:
    urls = [f"{CAA}/release/{release.mbid}/front-500"]
    if release.release_group_id:
        urls.append(f"{CAA}/release-group/{release.release_group_id}/front-500")
    for url in urls:
        try:
            resp = http_get(url)
        except AppError as exc:
            warn(f"cover art: {exc}")
            return None
        if resp.status_code == 200 and resp.content:
            return resp.content
    warn("no cover art on the Cover Art Archive")
    return None


# ---------------------------------------------------------------- naming

UNSAFE = str.maketrans({"/": "-", "\\": "-", ":": " -", "*": "_", "?": "_", '"': "'",
                        "<": "_", ">": "_", "|": "_"})


def sanitize(name: str, max_bytes: int = 200) -> str:
    name = re.sub(r"[\x00-\x1f\x7f]", "", name.translate(UNSAFE))
    name = re.sub(r"\s+", " ", name).strip().strip(".").strip()
    while len(name.encode("utf-8")) > max_bytes:
        name = name[:-1].rstrip()
    return name or "_"


def track_filename(track: Track, track_count: int, multi_disc: bool, ext: str) -> str:
    width = max(2, len(str(track_count)))
    prefix = f"{track.number:0{width}d}"
    if multi_disc:
        prefix = f"{track.disc}-{prefix}"
    return f"{prefix} - {sanitize(track.title, 180)}.{ext}"


# ---------------------------------------------------------------- main

@dataclass
class Segment:
    track: Track
    start: float
    end: float | None
    note: str
    filename: str


def check_tools() -> None:
    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        raise AppError(f"missing on PATH: {', '.join(missing)}")


def select_tracks(release: Release, disc: int | None) -> list[Track]:
    if disc is None:
        return [t for medium in release.media for t in medium]
    if not 1 <= disc <= len(release.media):
        raise AppError(f"--disc {disc} out of range (release has {len(release.media)} discs)")
    return release.media[disc - 1]


def compute_starts(args: argparse.Namespace, tracks: list[Track], probe: Probe) -> tuple[list[float], list[str]]:
    lengths = [t.length for t in tracks]
    expected = sum(lengths) if all(l is not None for l in lengths) else None  # type: ignore[misc]

    if args.cue:
        starts = parse_cue(Path(args.cue).read_text(encoding="utf-8", errors="replace"))
        if len(starts) != len(tracks):
            raise AppError(f"cue has {len(starts)} tracks, release has {len(tracks)}")
        if starts[0] > 1.0:
            warn(f"cue track 1 starts at {fmt_time(starts[0])}. Pregap audio stays in track 1")
        starts[0] = 0.0
    elif expected is None:
        missing = [str(t.number) for t in tracks if t.length is None]
        raise AppError(f"MusicBrainz has no length for track(s) {', '.join(missing)}. Use --cue")
    else:
        starts = starts_from_durations(lengths)  # type: ignore[arg-type]

    if expected is not None:
        diff = probe.duration - expected
        if abs(diff) > DURATION_TOLERANCE:
            msg = (f"file is {fmt_time(probe.duration)}, release is {fmt_time(expected)} "
                   f"({diff:+.1f}s)")
            if not args.force:
                raise AppError(f"{msg}. Wrong release or disc? Use --force to continue")
            warn(msg)
        if diff > DURATION_TOLERANCE:
            warn("file is longer than the release. The remainder stays in the last track")

    pins = resolve_pins(args.start or [], tracks)
    silences = detect_silence(Path(args.input_file), probe.duration) if args.detect_silence else None
    starts, notes = snap_boundaries(starts, silences, args.snap_window, pins)
    for i, (track, start, note) in enumerate(zip(tracks, starts, notes)):
        if silences is not None and note.startswith("no silence"):
            msg = f"no silence within ±{args.snap_window:g}s of track {track.number} start ({fmt_time(start)})"
            gap = nearest_silence_end(start, silences, HINT_RANGE, starts[i - 1])
            if gap is not None:
                msg += (f". Nearest gap ends at {fmt_time(gap)} ({gap - start:+.1f}s). "
                        f"Pin it with --start {track.number}={fmt_time(gap)}")
            warn(msg)

    for i, (track, start) in enumerate(zip(tracks, starts)):
        if start >= probe.duration:
            raise AppError(f"track {track.number} starts after the end of the file")
        if i and start <= starts[i - 1]:
            raise AppError(f"track {track.number} starts before track {tracks[i - 1].number}. Check --start")
    return starts, notes


def resolve_pins(pins: list[tuple[str, float]], tracks: list[Track]) -> dict[int, float]:
    """Map --start track keys ("10" or "2-10") to indexes in the selected tracks."""
    out: dict[int, float] = {}
    for key, seconds in pins:
        matches = [i for i, t in enumerate(tracks)
                   if key in (str(t.number), f"{t.disc}-{t.number}")]
        if not matches:
            raise AppError(f"--start {key}: no such track")
        if len(matches) > 1:
            raise AppError(f"--start {key}: several discs have this track. Use DISC-TRACK")
        out[matches[0]] = seconds
    return out


def gather_mb_genres(release: Release) -> list[str]:
    if release.genres:
        return release.genres
    if release.group_genres:
        return release.group_genres
    if release.artist_ids:
        try:
            return fetch_artist_genres(release.artist_ids[0])
        except AppError as exc:
            warn(f"artist genres: {exc}")
    return []


def build_tags(release: Release, track: Track, genre: str, discogs_id: str | None) -> dict[str, Any]:
    disc_sizes = {m[0].disc: len(m) for m in release.media if m}
    return {
        "title": track.title, "artist": track.artist, "album": release.title,
        "albumartist": release.artist, "tracknumber": track.number,
        "tracktotal": disc_sizes.get(track.disc),
        "discnumber": track.disc, "disctotal": len(release.media),
        "date": release.date, "originaldate": release.original_date,
        "label": release.label, "catalognumber": release.catno, "genre": genre,
        "mb_albumid": release.mbid, "mb_releasegroupid": release.release_group_id,
        "mb_recordingid": track.recording_id, "mb_releasetrackid": track.track_id,
        "mb_artistids": track.artist_ids, "mb_albumartistids": release.artist_ids,
        "discogs_release_id": discogs_id,
    }


def default_out_dir(release: Release) -> Path:
    album = f"{release.title} ({release.year})" if release.year else release.title
    return Path.cwd() / sanitize(release.artist) / sanitize(album)


def print_plan(release: Release, discogs: dict[str, Any] | None, mb_genres: list[str], genre: str,
               mode: Mode, out_dir: Path, segments: list[Segment], probe: Probe,
               sample_tags: dict[str, Any]) -> None:
    print(f"Release:   {release.artist} - {release.title} ({release.year or '?'})  [{release.mbid}]")
    if discogs:
        print(f"Discogs:   r{discogs.get('id')}  genres: {', '.join(discogs.get('genres', [])) or '-'}"
              f"  |  styles: {', '.join(discogs.get('styles', [])) or '-'}")
    else:
        print("Discogs:   -")
    print(f"MB genres: {', '.join(mb_genres) or '-'}")
    print(f"Genre:     {genre or '-'}")
    print(f"Mode:      {mode.label}")
    print(f"Output:    {out_dir}")
    print(f"File:      {fmt_time(probe.duration)}\n")
    print(f"  {'Start':>11}  {'End':>11}  {'Length':>10}  {'Snap':>17}  File")
    for seg in segments:
        end = seg.end if seg.end is not None else probe.duration
        print(f"  {fmt_time(seg.start):>11}  {fmt_time(end):>11}  {fmt_time(end - seg.start):>10}  "
              f"{seg.note:>17}  {seg.filename}")
    print("\nTags (first track):")
    for key, value in sample_tags.items():
        if value not in (None, "", []):
            print(f"  {key:<20} {', '.join(as_list(value))}")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("input_file")
    p.add_argument("--mbid", help="MusicBrainz release ID (searched from tags if omitted)")
    p.add_argument("--discogs-release", help="Discogs release ID or URL")
    p.add_argument("--cue", help="cue sheet with split points")
    p.add_argument("--detect-silence", action="store_true", help="snap boundaries to detected silence")
    p.add_argument("--snap-window", type=float, default=SNAP_WINDOW,
                   help=f"max seconds to move a boundary (default {SNAP_WINDOW:g})")
    p.add_argument("--start", type=parse_pin, action="append", metavar="TRACK=TIME",
                   help="pin a track start, e.g. 10=41:53.7 or 2-5=3:10. Repeatable")
    p.add_argument("--disc", type=int, help="disc number when the file holds one disc")
    p.add_argument("--genre", help="override genre")
    p.add_argument("--decade", action="store_true", help="prefix decade for decade_genres (e.g. 80s/Rock)")
    p.add_argument("--out", type=Path, help="output directory")
    p.add_argument("--format", choices=["flac", "mp3", "m4a", "same"], default="same")
    p.add_argument("--genres-file", type=Path, default=DEFAULT_GENRES_FILE)
    p.add_argument("--dry-run", action="store_true", help="print the plan and write nothing")
    p.add_argument("--yes", action="store_true", help="non-interactive. Take best guesses")
    p.add_argument("--force", action="store_true", help="proceed despite a duration mismatch")
    return p.parse_args(argv)


def run(args: argparse.Namespace) -> None:
    check_tools()
    src = Path(args.input_file)
    if not src.is_file():
        raise AppError(f"no such file: {src}")
    probe = probe_audio(src)
    mode = choose_mode(probe.codec, args.format)

    if not args.genres_file.exists():
        warn(f"{args.genres_file} not found. Run build_genre_map.py first for consistent genres")
    cfg = GenreConfig.from_raw(load_genre_config(args.genres_file))

    release = fetch_release(args.mbid or search_musicbrainz(src, args.yes))
    tracks = select_tracks(release, args.disc)
    if not tracks:
        raise AppError("release has no tracks")

    discogs = None if args.genre else resolve_discogs(args.discogs_release, release, args.yes)
    mb_genres: list[str] = []
    if args.genre:
        genre = args.genre
    else:
        if discogs is None or not discogs.get("styles"):
            mb_genres = gather_mb_genres(release)
        result = resolve_genre(discogs.get("genres", []) if discogs else [],
                               discogs.get("styles", []) if discogs else [],
                               mb_genres, cfg, release.year or (discogs or {}).get("year"), args.decade)
        for w in result.warnings:
            warn(w)
        genre = confirm_genre(result, cfg, args.genres_file, not (args.yes or args.dry_run))

    starts, notes = compute_starts(args, tracks, probe)
    multi_disc = len(release.media) > 1
    segments = [
        Segment(t, s, starts[i + 1] if i + 1 < len(starts) else None, notes[i],
                track_filename(t, len(tracks), multi_disc, mode.ext))
        for i, (t, s) in enumerate(zip(tracks, starts))
    ]
    out_dir = args.out or default_out_dir(release)
    discogs_id = str(discogs["id"]) if discogs else None
    all_tags = [build_tags(release, seg.track, genre, discogs_id) for seg in segments]
    print_plan(release, discogs, mb_genres, genre, mode, out_dir, segments, probe, all_tags[0])
    if args.dry_run:
        return

    cover = fetch_cover(release)
    out_dir.mkdir(parents=True, exist_ok=True)
    if cover:
        (out_dir / ("cover.png" if cover_mime(cover) == "image/png" else "cover.jpg")).write_bytes(cover)
    for seg, tags in zip(segments, all_tags):
        dst = out_dir / seg.filename
        ffmpeg_cut(src, dst, seg.start, seg.end, mode, probe.sample_rate)
        write_tags(dst, tags, cover)
        info(f"wrote {dst}")


def load_dotenv(path: Path) -> None:
    """Set variables from a .env file. Values already in the environment win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        os.environ.setdefault(key, value.strip().strip("'\""))


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path(__file__).with_name(".env"))
    args = parse_args(argv)
    try:
        run(args)
    except AppError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
