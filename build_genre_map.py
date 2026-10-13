"""Build a hand-editable genres.yaml from the genre tags in an existing library.

Scans audio files, splits each genre string on "/", and infers:
  roots, parents (specific -> root), aliases (spelling variants -> most common
  spelling), decade_genres (roots tagged with a part like "80s") and decades.
Parts that are not Discogs genres/styles are listed under `unrecognized` and
printed, so personal additions can be turned into explicit rules.
Also provides load/save helpers for genres.yaml used by split_album.py.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import mutagen
import yaml
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".mp4", ".aiff", ".aif", ".wav", ".ogg", ".opus"}
DECADE_RE = re.compile(r"^(\d0s|\d{3}0s)$")

HEADER = """\
# Genre rules for split_album.py. Keys match case-insensitively.
#   roots:         top-level genres
#   parents:       specific -> root (places MusicBrainz-only genres)
#   aliases:       source spelling -> your spelling (may contain "/" to expand)
#   ignore:        parts to drop
#   decade_genres: roots that get a decade prefix with --decade (e.g. 80s/Rock)
#   decades:       decades allowed as a prefix (empty means any)
#   root_priority: optional root order for releases with several Discogs genres
#   unrecognized:  parts not found in the Discogs vocabulary (informational)
"""

DISCOGS_GENRES = [
    "Blues", "Brass & Military", "Children's", "Classical", "Electronic",
    "Folk, World, & Country", "Funk / Soul", "Hip Hop", "Jazz", "Latin",
    "Non-Music", "Pop", "Reggae", "Rock", "Stage & Screen",
]

DISCOGS_STYLES = """
Abstract|Acid|Acid House|Acid Jazz|Ambient|Amapiano|Baltimore Club|Bass Music|Bassline|Beatdown|
Berlin-School|Big Beat|Bleep|Breakbeat|Breakcore|Breaks|Broken Beat|Chillwave|Chiptune|Dance-pop|
Dark Ambient|Darkwave|Deep House|Deep Techno|Disco|Disco Polo|Donk|Downtempo|Drone|Drum n Bass|Dub|
Dub Techno|Dubstep|Dungeon Synth|EBM|Electro|Electro House|Electroclash|Euro House|Euro-Disco|
Eurobeat|Eurodance|Experimental|Footwork|Freestyle|Future Jazz|Gabber|Garage House|Ghetto|
Ghetto House|Glitch|Goa Trance|Gqom|Grime|Halftime|Hands Up|Happy Hardcore|Hard House|Hard Trance|
Hardcore|Hardstyle|Hi NRG|Hip-House|House|IDM|Illbient|Industrial|Italo House|Italo-Disco|
Italodance|Jazzdance|Juke|Jumpstyle|Jungle|Kuduro|Leftfield|Lo-Fi|Makina|Minimal|Minimal Techno|
Modern Classical|Musique Concrète|Neofolk|New Age|New Beat|New Wave|Noise|Nu-Disco|
Power Electronics|Progressive Breaks|Progressive House|Progressive Trance|Psy-Trance|Rhythmic Noise|
Schranz|Sound Collage|Speed Garage|Speedcore|Synth-pop|Synthwave|Tech House|Tech Trance|Techno|
Trance|Tribal|Tribal House|Trip Hop|Tropical House|UK Funky|UK Garage|Vaporwave|Witch House|
Acid Rock|Acoustic|Alternative Rock|AOR|Arena Rock|Art Rock|Atmospheric Black Metal|Avantgarde|Beat|
Black Metal|Blues Rock|Brit Pop|Classic Rock|Coldwave|Country Rock|Crust|Death Metal|Deathcore|
Deathrock|Depressive Black Metal|Doo Wop|Doom Metal|Dream Pop|Emo|Ethereal|Folk Metal|Folk Rock|
Funeral Doom Metal|Funk Metal|Garage Rock|Glam|Goregrind|Goth Rock|Gothic Metal|Grindcore|Grunge|
Hard Rock|Heavy Metal|Horror Rock|Indie Rock|Krautrock|Lounge|Math Rock|Melodic Death Metal|
Melodic Hardcore|Metalcore|Mod|No Wave|Noisecore|Nu Metal|Oi|Parody|Pop Punk|Pop Rock|Pornogrind|
Post Rock|Post-Hardcore|Post-Metal|Post-Punk|Power Metal|Power Pop|Power Violence|Prog Rock|
Progressive Metal|Psychedelic Rock|Psychobilly|Pub Rock|Punk|Rock & Roll|Rockabilly|Shoegaze|Ska|
Sludge Metal|Soft Rock|Southern Rock|Space Rock|Speed Metal|Stoner Rock|Surf|Symphonic Rock|
Technical Death Metal|Thrash|Twist|Viking Metal|Yé-Yé|
Afrobeat|Bayou Funk|Boogie|Contemporary R&B|Free Funk|Funk|Gospel|Minneapolis Sound|Neo Soul|
New Jack Swing|Northern Soul|P.Funk|Psychedelic|Rhythm & Blues|Soul|Swingbeat|UK Street Soul|
Boom Bap|Bounce|Britcore|Cloud Rap|Conscious|Crunk|Cut-up/DJ|DJ Battle Tool|Drill|G-Funk|Gangsta|
Hardcore Hip-Hop|Horrorcore|Instrumental|Jazzy Hip-Hop|Miami Bass|Phonk|Pop Rap|Ragga HipHop|
RnB/Swing|Screw|Thug Rap|Trap|Turntablism|
Afro-Cuban Jazz|Avant-garde Jazz|Big Band|Bop|Bossa Nova|Contemporary Jazz|Cool Jazz|Dixieland|
Easy Listening|Free Improvisation|Free Jazz|Fusion|Gypsy Jazz|Hard Bop|Jazz-Funk|Jazz-Rock|
Latin Jazz|Modal|Post Bop|Ragtime|Smooth Jazz|Soul-Jazz|Space-Age|Swing|
Axé|Bachata|Baião|Batucada|Beguine|Bolero|Boogaloo|Bossanova|Cha-Cha|Charanga|Cubano|Cumbia|
Danzon|Descarga|Forró|Guaguancó|Guajira|Guaracha|MPB|Mambo|Mariachi|Merengue|Norteño|
Nueva Cancion|Pachanga|Porro|Ranchera|Reggaeton|Rumba|Salsa|Samba|Son|Son Montuno|Tango|Tejano|
Vallenato|
Calypso|Dancehall|Lovers Rock|Ragga|Reggae-Pop|Rocksteady|Roots Reggae|Soca|Steel Band|
Ballad|Bollywood|Bubblegum|Chanson|City Pop|Europop|Indie Pop|J-pop|K-pop|Kayōkyoku|Light Music|
Music Hall|Novelty|Schlager|Vocal|
Boogie Woogie|Chicago Blues|Country Blues|Delta Blues|Electric Blues|Harmonica Blues|Jump Blues|
Louisiana Blues|Modern Electric Blues|Piano Blues|Texas Blues|
African|Bhangra|Bluegrass|Cajun|Celtic|Country|Fado|Flamenco|Folk|Gamelan|Highlife|Hillbilly|
Hindustani|Honky Tonk|Klezmer|Mbalax|Nordic|Pacific|Polka|Rai|Soukous|Volksmusik|Western Swing|
Zouk|Zydeco|
Baroque|Choral|Contemporary|Impressionist|Medieval|Modern|Neo-Classical|Neo-Romantic|Opera|
Post-Modern|Renaissance|Romantic|Serial|
Musical|Score|Soundtrack|Theme|
Comedy|Dialogue|Education|Field Recording|Interview|Monolog|Poetry|Political|Promotional|
Radioplay|Religious|Spoken Word
"""

DISCOGS_VOCAB = DISCOGS_GENRES + [s.strip() for s in DISCOGS_STYLES.split("|") if s.strip()]

# Discogs genre labels with " / " or commas, mapped to one clean path.
DEFAULT_ALIASES = {
    "Funk / Soul": "Soul/Funk",
    "Folk, World, & Country": "Folk",
    "Stage & Screen": "Soundtrack",
    "Hip Hop": "Hip-Hop",
}

# Used only when the target already appears in the library.
STYLE_HINTS = {
    "Rhythm & Blues": "R&B",
    "Contemporary R&B": "R&B",
    "RnB/Swing": "R&B",
}


def empty_config() -> dict[str, Any]:
    return {
        "roots": [],
        "parents": {},
        "aliases": {},
        "ignore": [],
        "decade_genres": [],
        "decades": [],
        "root_priority": [],
        "unrecognized": [],
    }


def load_genre_config(path: Path) -> dict[str, Any]:
    cfg = empty_config()
    if not path.exists():
        return cfg
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    for key, default in cfg.items():
        value = data.get(key) or type(default)()
        if not isinstance(value, type(default)):
            raise ValueError(f"{path}: '{key}' must be a {type(default).__name__}")
        cfg[key] = value
    return cfg


def save_genre_config(cfg: dict[str, Any], path: Path) -> None:
    body = yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True, width=1000)
    path.write_text(HEADER + body, encoding="utf-8")


def norm_key(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.lower())


def read_genres(path: Path) -> list[str]:
    """Return genre strings from a file. Multi-value tags are joined with "/"."""
    try:
        audio = mutagen.File(path)
    except Exception as exc:  # mutagen raises many error types on bad files
        print(f"warning: cannot read {path}: {exc}", file=sys.stderr)
        return []
    if audio is None or audio.tags is None:
        return []
    tags = audio.tags
    if isinstance(tags, ID3):
        values = [t for frame in tags.getall("TCON") for t in frame.text]
    elif isinstance(audio, MP4):
        values = list(tags.get("\xa9gen", []))
    else:
        values = list(tags.get("genre", []) or tags.get("GENRE", []))
    values = [str(v).strip() for v in values if str(v).strip()]
    return ["/".join(values)] if values else []


def split_parts(genre: str) -> list[str]:
    return [p.strip() for p in genre.split("/") if p.strip()]


def build_config(genre_counts: Counter[str]) -> tuple[dict[str, Any], list[str]]:
    """Infer genres.yaml content. Returns (config, notes for the console)."""
    notes: list[str] = []
    spellings: dict[str, Counter[str]] = defaultdict(Counter)
    for genre, n in genre_counts.items():
        for part in split_parts(genre):
            spellings[norm_key(part)][part] += n
    canonical = {key: c.most_common(1)[0][0] for key, c in spellings.items()}

    aliases: dict[str, str] = {}
    for key, counter in spellings.items():
        for variant in counter:
            if variant != canonical[key]:
                aliases[variant] = canonical[key]

    vocab_by_key = {norm_key(v): v for v in DISCOGS_VOCAB}
    for key, spelling in canonical.items():
        source = vocab_by_key.get(key)
        if source and source != spelling:
            aliases[source] = spelling

    roots: Counter[str] = Counter()
    pairs: Counter[tuple[str, str]] = Counter()
    parent_votes: dict[str, Counter[str]] = defaultdict(Counter)
    decade_roots: set[str] = set()
    decades: set[str] = set()
    for genre, n in genre_counts.items():
        all_parts = [canonical[norm_key(p)] for p in split_parts(genre)]
        found = [p for p in all_parts if DECADE_RE.match(p)]
        parts = [p for p in all_parts if not DECADE_RE.match(p)]
        decades.update(found)
        if not parts:
            continue
        root = parts[0]
        roots[root] += n
        if found:
            decade_roots.add(root)
        if len(parts) > 1:
            pairs[(root, parts[1])] += n
        for part in parts[1:]:
            if part != root:
                parent_votes[part][root] += n

    for source, target in DEFAULT_ALIASES.items():
        if source in aliases:
            continue
        parts = [canonical.get(norm_key(p), p) for p in split_parts(target)]
        if len(parts) == 2 and pairs[(parts[1], parts[0])] > pairs[(parts[0], parts[1])]:
            parts.reverse()
        aliases[source] = "/".join(parts)

    # e.g. "Folk, World, & Country" -> whichever part is your most used root.
    for source in (g for g in DISCOGS_GENRES if "," in g):
        options = [canonical.get(norm_key(p.strip(" &"))) for p in source.split(",")]
        used = [o for o in options if o in roots]
        if used:
            aliases[source] = max(used, key=lambda r: roots[r])

    for source, target in STYLE_HINTS.items():
        if source not in aliases and norm_key(target) in canonical:
            aliases[source] = canonical[norm_key(target)]

    parents: dict[str, str] = {}
    for part, votes in sorted(parent_votes.items(), key=lambda kv: kv[0].lower()):
        parents[part] = votes.most_common(1)[0][0]
        if len(votes) > 1:
            others = ", ".join(f"{r} ({c})" for r, c in votes.most_common()[1:])
            notes.append(f"'{part}' has several parents. Chose {parents[part]}; also seen under {others}")

    unrecognized = sorted(
        {s for k, s in canonical.items() if k not in vocab_by_key and s not in decades},
        key=str.lower,
    )

    cfg = empty_config()
    cfg["roots"] = [r for r, _ in roots.most_common()]
    cfg["parents"] = parents
    cfg["aliases"] = dict(sorted(aliases.items(), key=lambda kv: kv[0].lower()))
    cfg["decade_genres"] = sorted(decade_roots, key=str.lower)
    cfg["decades"] = sorted(decades)
    cfg["unrecognized"] = unrecognized
    return cfg, notes


def scan_library(library: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    for path in library.rglob("*"):
        if path.suffix.lower() in AUDIO_EXTS and path.is_file():
            counts.update(read_genres(path))
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("library_dir", type=Path)
    parser.add_argument("--out", type=Path, default=Path(__file__).with_name("genres.yaml"))
    parser.add_argument("--force", action="store_true", help="overwrite an existing output file")
    args = parser.parse_args(argv)

    if not args.library_dir.is_dir():
        parser.error(f"not a directory: {args.library_dir}")
    if args.out.exists() and not args.force:
        parser.error(f"{args.out} exists. Use --force to overwrite or --out to pick another path")

    counts = scan_library(args.library_dir)
    if not counts:
        print("No genre tags found.", file=sys.stderr)
        return 1
    cfg, notes = build_config(counts)
    save_genre_config(cfg, args.out)

    print(f"Scanned {sum(counts.values())} tagged files, {len(counts)} distinct genre strings.")
    print(f"Wrote {args.out}: {len(cfg['roots'])} roots, {len(cfg['parents'])} parents, "
          f"{len(cfg['aliases'])} aliases.")
    for note in notes:
        print(f"note: {note}")
    if cfg["decade_genres"]:
        print(f"Decade genres: {', '.join(cfg['decade_genres'])} (decades: {', '.join(cfg['decades'])})")
    if cfg["unrecognized"]:
        print("Not Discogs genres/styles (personal additions?). Consider rules for these:")
        for part in cfg["unrecognized"]:
            print(f"  - {part}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
