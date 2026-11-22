# ytpipe

Download a video and write it to **stdout**. Pipe it into a player, an encoder, `ssh`,
or another process — no temp file to clean up yourself.

```bash
ytpipe 'https://www.youtube.com/watch?v=dQw4w9WgXcQ' > rick.mp4
ytpipe -a 'https://youtu.be/dQw4w9WgXcQ' | mpv -
ytpipe '$URL' | ffmpeg -i - -vf scale=640:-2 out.mp4
```

Works with anything `yt-dlp` supports, not just YouTube.

## Why this exists

`yt-dlp -o -` looks like it should do this. It doesn't, for any video that needs
video+audio merged:

- ffmpeg cannot seek a non-seekable pipe, so the merger silently emits **MPEG-TS** —
  `format_name=mpegts`, AV1 demoted to a private data stream, ~25% muxing overhead —
  even with `--merge-output-format mp4`. You get a `.mp4` that isn't an mp4.
- `--ppa "Merger:+movflags +faststart"` is worse: the postprocessor args get split and
  the merge dies with `Postprocessing: Error opening output files: Invalid argument`.

`ytpipe` downloads video into a temp directory, remuxes with a `-c copy` pass only when
the `moov` atom is not already ahead of `mdat` (faststart), then copies the finished
bytes to stdout. The result is a real mp4 that plays from a pipe. Audio-only has no
merge step, so `-a` streams straight out of yt-dlp with no intermediate file at all.

## Install

Needs `ffmpeg` on `PATH` for merged video (audio-only does not).

```bash
# recommended: isolated, pulls yt-dlp with it
pipx install .

# or a plain venv
python3 -m venv ~/.venvs/ytpipe && ~/.venvs/ytpipe/bin/pip install .
ln -sf ~/.venvs/ytpipe/bin/ytpipe ~/.local/bin/ytpipe

# or the scripted way (creates the venv, installs, links, checks ffmpeg)
./install.sh
```

## Usage

```
ytpipe [-a] [-c {mp4,mkv,webm}] [-f SELECTOR] [-q] [--keep] URL
```

| Flag | Meaning |
| --- | --- |
| `-c mp4\|mkv\|webm` | container for merged video (default `mp4`, `m4a` audio preferred) |
| `-a, --audio-only` | bestaudio only, no video track, no re-encode, streamed live |
| `-f SELECTOR` | raw yt-dlp format selector; overrides `-c` |
| `-q, --quiet` | no progress output |
| `--cookies-from-browser BROWSER` | for age/member-gated videos |
| `--playlist` | allow a playlist (default: one video) |
| `--keep` | keep the working file, print its path to stderr |
| `--tempdir DIR` | where the working file lives (default: system temp) |

stdout carries media bytes and nothing else. Progress, warnings and errors go to
stderr, so `> out.mp4` and `| player` both stay clean.

### Exit codes

`0` ok · `1` failure (bad URL, yt-dlp error, no yt-dlp) · `130` interrupted ·
`141` downstream closed the pipe — normal with `head`, `ffprobe`, or a player you quit.

### Verifying output

```bash
ytpipe -q "$URL" | ffprobe -v error -show_entries format=duration -of default=nw=1 -i -
```

Faststart output is readable from a pipe without seeking. Without it (`-c mkv`, or a
`-f` selector that yields a fragmented file) `ffprobe -i -` may only manage the head.

## Tests

Offline suite — no network, no real yt-dlp or ffmpeg (both are stubbed):

```bash
make test          # python -m unittest discover -s tests -v
```

Live smoke test (hits YouTube, downloads ~½ MB):

```bash
./scripts/smoke.sh
```

## Notes and sharp edges

- An explicit `-f "bv*+ba"` lets **opus** land inside an mp4. Legal, but QuickTime and
  browsers may refuse it; the built-in selectors prefer `m4a` for mp4.
- YouTube increasingly wants a JavaScript runtime for full format coverage. yt-dlp warns
  on stderr (`No supported JavaScript runtime…`) — install `deno` if formats go missing.
- Playlists are ignored unless `--playlist` is passed; one run is one file on stdout.
- 4K/8K VP9 or AV1 at long durations can be multi-GB; the whole thing is staged on disk
  before streaming, so check `df` on a small VPS.

## License

MIT.
