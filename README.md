# album-tagger

Split a whole-album audio file into tagged tracks using MusicBrainz (titles, timings, IDs),
Discogs (genres) and the Cover Art Archive. Tags target Navidrome and Serato.

## Setup

Needs [uv](https://docs.astral.sh/uv/) and `ffmpeg`/`ffprobe` on PATH.

```sh
uv sync
cp .env.example .env
```

Fill in `.env`. `split_album.py` loads it automatically. Shell variables take precedence.
- `DISCOGS_TOKEN`: from https://www.discogs.com/settings/developers
- `MB_CONTACT`: an email or URL sent in the MusicBrainz User-Agent
- `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`: optional, for `--spotify`. Create an app at
  https://developer.spotify.com/dashboard (the owner needs Premium)

## Build the genre map once

```sh
uv run build_genre_map.py ~/Audio
```

Writes `genres.yaml` from your existing genre tags. Edit it by hand. It is the source of truth.
Parts listed under `unrecognized` are not Discogs genres/styles. Turn them into rules.

## Split an album

```sh
# Preview split points and tags
uv run split_album.py album.flac --mbid 1c1f2b0e-... --dry-run

# Snap to silence, add decade prefix, non-interactive
uv run split_album.py album.flac --mbid 1c1f2b0e-... --detect-silence --decade --yes

# Recording that drifts from MusicBrainz lengths (captures, streams)
uv run split_album.py capture.mp3 --mbid 1c1f2b0e-... --detect-silence --snap-window 8 --dry-run

# Pin a start that silence detection can't find (the dry run suggests nearby gaps)
uv run split_album.py capture.mp3 --mbid 1c1f2b0e-... --detect-silence --start 10=41:54

# Recorded from Spotify: take track lengths from the same Spotify album
uv run split_album.py capture.flac --mbid 1c1f2b0e-... --spotify https://open.spotify.com/album/...

# Count gaps as short as 0.3s (default 0.5s)
uv run split_album.py album.flac --mbid 1c1f2b0e-... --detect-silence --min-silence 0.3

# Cue sheet, one disc of a 2-disc release
uv run split_album.py disc2.flac --mbid 1c1f2b0e-... --cue disc2.cue --disc 2

# Search MusicBrainz from the file's tags, set genre by hand
uv run split_album.py album.mp3 --genre "Soul/Funk/Disco"
```

### Args files

For many attempts, keep the options in a file. Use one or more options per line,
with shell quoting. Lines starting with `#` are skipped and `~` is expanded.

```
# album.args
~/Audio/Capture/album.flac
--mbid 1c1f2b0e-...
--genre "Hip Hop"
--detect-silence --min-silence 0.3 --force
--start 10=26:02.2
--out "~/Audio/Staging/Artist/Album"
```

```sh
uv run split_album.py @album.args
uv run split_album.py @album.args --dry-run
```

Options after the file are added. Later values win, and `--start` pins add up.
The plan lists each pin with the start it replaced, so you can adjust it by hand.
Files named `*.args` are ignored by git.

Snapped cuts land 0.1s before the end of a gap, so the next track's first sound stays whole.

`--spotify` keeps MusicBrainz for tags and IDs and only swaps in Spotify's track lengths.
Tracks match by disc and number. Lengths that differ by more than 2s are listed.
Turn off Crossfade and Automix in Spotify when recording.

Output goes to `./<Album Artist>/<Album> (<Year>)/NN - Title.ext` plus `cover.jpg`.
Multi-disc releases use `D-NN - Title.ext`.

## Formats

| Input | `--format same` | Cut |
|---|---|---|
| FLAC, WAV, AIFF, ALAC | FLAC | re-encode, sample-accurate |
| MP3 | MP3 | stream copy |
| AAC (M4A) | M4A | stream copy |

`--format m4a` from lossless input writes ALAC. MP3 tags are ID3v2.3 for Serato.

## Genres

One string like `Electronic/Deep House`. Built from the first Discogs genre (or the first
in `root_priority`), then Discogs styles, normalized with `genres.yaml`. Max 4 levels.
MusicBrainz genres are used only when Discogs has no genres or styles.

`--decade` puts the decade first (`80s/Rock`). It only applies to roots in `decade_genres`
and decades in `decades`. Both are learned from your library.

## Tests

```sh
uv run pytest
```
