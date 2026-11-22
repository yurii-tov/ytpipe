"""ytpipe CLI: fetch media with yt-dlp and write the bytes to stdout.

Why not `yt-dlp -o -`?  Anything that needs merging breaks on a pipe: ffmpeg cannot
seek a non-seekable stream, so the merger silently emits MPEG-TS (AV1 demoted to a
private data stream, ~25% muxing overhead) even when `--merge-output-format mp4` is
set.  `--ppa "Merger:+movflags +faststart"` is worse -- the arguments get split and
the merge dies with `Postprocessing: Error opening output files: Invalid argument`.

So: video is downloaded into a temp directory, remuxed with a stream copy when the
moov atom is not already ahead of mdat (faststart), then copied byte-for-byte to
stdout.  Audio-only has no merge step, so yt-dlp pipes it directly.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import tempfile

from . import __version__

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_INT = 130
EXIT_PIPE = 141

# Container -> yt-dlp format selector. mp4 prefers m4a audio: opus-in-mp4 is legal
# but QuickTime/safari-class players will not touch it.
SELECTORS = {
    "mp4": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
    "mkv": "bv*+ba/b",
    "webm": "bv*[ext=webm]+ba[ext=webm]/bv*+ba/b",
}
AUDIO_SELECTOR = "ba[ext=m4a]/ba/b"

CHUNK = 256 * 1024


class YtpipeError(RuntimeError):
    """Fatal, message-worthy failure."""


# --------------------------------------------------------------------------- tools


def find_ytdlp() -> list[str]:
    """Locate a yt-dlp invocation, most-specific first.

    $YTDLP wins; then the interpreter's own yt_dlp module (what `pip install ytpipe`
    gives you); then yt-dlp on PATH; finally the conventional isolated venv.
    """
    explicit = os.environ.get("YTDLP")
    candidates: list[list[str]] = []
    if explicit:
        candidates.append([explicit])
    if importlib.util.find_spec("yt_dlp") is not None:
        candidates.append([sys.executable, "-m", "yt_dlp"])
    onpath = shutil.which("yt-dlp")
    if onpath:
        candidates.append([onpath])
    legacy = os.path.expanduser("~/.venvs/ytdlp/bin/yt-dlp")
    candidates.append([legacy])

    for cmd in candidates:
        exe = cmd[0]
        if len(cmd) > 1:
            return cmd
        if os.path.isfile(exe) and os.access(exe, os.X_OK):
            return cmd
    raise YtpipeError(
        "yt-dlp not found. Install it with `pip install -U yt-dlp` (recommended: "
        "`pipx install ytpipe`, which pulls it in), or point $YTDLP at the binary."
    )


def find_ffmpeg() -> str | None:
    explicit = os.environ.get("FFMPEG")
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    return shutil.which("ffmpeg")


# ------------------------------------------------------------------- mp4 inspection


def top_level_boxes_from_bytes(data: bytes, limit: int = 32) -> list[tuple[str, int]]:
    """Walk top-level ISO-BMFF boxes in a buffer: [(type, size), ...].

    Parsed rather than searched: `moov`/`mdat` can appear inside a payload, and an
    accidental match would send a stream-copy pass down the wrong branch.
    """
    boxes: list[tuple[str, int]] = []
    total = len(data)
    off = 0
    while off + 8 <= total and len(boxes) < limit:
        head = data[off:off + 16]
        if len(head) < 8:
            break
        size = int.from_bytes(head[0:4], "big")
        btype = head[4:8].decode("latin-1")
        if size == 1:  # 64-bit largesize follows the type
            if len(head) < 16:
                break
            size = int.from_bytes(head[8:16], "big")
        elif size == 0:  # box runs to EOF
            size = total - off
        if size < 8 or off + size > total:
            break
        boxes.append((btype, size))
        off += size
    return boxes


def top_level_boxes(path: str, limit: int = 32) -> list[tuple[str, int]]:
    """Same walk, reading from a file (head of it is enough for the first boxes)."""
    total = os.path.getsize(path)
    with open(path, "rb") as fh:
        data = fh.read(min(total, 4 * 1024 * 1024))
    return top_level_boxes_from_bytes(data, limit=limit)


def needs_faststart(path: str) -> bool:
    """True when the moov atom is missing or sits behind mdat."""
    boxes = top_level_boxes(path)
    kinds = [t for t, _ in boxes]
    if "moov" not in kinds:
        return False  # fragmented mp4 / not ISO-BMFF: streams fine as-is
    moov = kinds.index("moov")
    return "mdat" in kinds and kinds.index("mdat") < moov


def ensure_faststart(path: str) -> str:
    """Return a pipe-playable copy of `path` (moov first), remuxing only if needed."""
    if not needs_faststart(path):
        return path
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("ytpipe: warning: ffmpeg not found, stream may not be seekable/playable "
              "from a pipe", file=sys.stderr)
        return path
    out = path + ".faststart.mp4"
    proc = subprocess.run(
        [ffmpeg, "-nostdin", "-v", "error", "-y", "-i", path,
         "-c", "copy", "-movflags", "+faststart", out],
        stdout=sys.stderr, stderr=sys.stderr,
    )
    if proc.returncode != 0 or not os.path.exists(out):
        print("ytpipe: warning: faststart remux failed, streaming as-is", file=sys.stderr)
        return path
    os.unlink(path)
    return out


# ------------------------------------------------------------------------ plumbing


def _child_preexec() -> None:  # pragma: no cover - runs in the forked child
    """Give children the default SIGPIPE disposition.

    CPython starts with SIGPIPE ignored so it can raise BrokenPipeError. Inherited,
    that turns a normal downstream close into an EPIPE write error, and yt-dlp prints
    `ERROR: unable to write data: [Errno 32] Broken pipe` on every `| head`. Resetting
    it in the child makes yt-dlp and friends die like any other Unix filter.
    """
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)


def _popen_kwargs() -> dict:
    return {"preexec_fn": _child_preexec} if os.name == "posix" else {}


def _stream(src_path: str, dest) -> None:
    with open(src_path, "rb") as fh:
        shutil.copyfileobj(fh, dest, length=CHUNK)
    dest.flush()


def _drain(proc: subprocess.Popen, dest) -> int:
    """Copy a child's stdout into `dest` (used when dest has no real fd)."""
    try:
        assert proc.stdout is not None
        shutil.copyfileobj(proc.stdout, dest, length=CHUNK)
        dest.flush()
    finally:
        proc.stdout.close() if proc.stdout else None
    return proc.wait()


def _pipe_through(cmd: list[str], dest) -> int:
    """Run `cmd` with its stdout going to our stdout, zero-copy where possible."""
    try:
        fileno = dest.fileno()
    except (AttributeError, OSError, ValueError):
        fileno = None
    if fileno is not None:
        return subprocess.Popen(cmd, stdout=dest, stderr=sys.stderr,
                                **_popen_kwargs()).wait()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=sys.stderr,
                            **_popen_kwargs())
    return _drain(proc, dest)


def _run_quiet(cmd: list[str]) -> int:
    """Run yt-dlp with all of its chatter on stderr; stdout stays clean."""
    return subprocess.run(cmd, stdout=sys.stderr, stderr=sys.stderr,
                          **_popen_kwargs()).returncode


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ytpipe",
        description="Download a video (or its audio) and write it to stdout.",
        epilog="Exit codes: 0 ok, 1 failure, 130 interrupted, 141 downstream closed "
               "the pipe (normal with `head`, `ffprobe`, early-quitting players).",
    )
    ap.add_argument("url", help="video URL (any yt-dlp-supported site) or bare video ID")
    ap.add_argument("-f", "--format", help="raw yt-dlp format selector (overrides -c)")
    ap.add_argument("-a", "--audio-only", action="store_true",
                    help=f"stream bestaudio only, no re-encode (default {AUDIO_SELECTOR})")
    ap.add_argument("-c", "--container", choices=sorted(SELECTORS), default="mp4",
                    help="video container (default: mp4)")
    ap.add_argument("-q", "--quiet", action="store_true", help="suppress progress output")
    ap.add_argument("--cookies-from-browser", metavar="BROWSER",
                    help="pass through to yt-dlp (age/member-gated videos)")
    ap.add_argument("--playlist", action="store_true",
                    help="allow a playlist (default: single video only)")
    ap.add_argument("--keep", action="store_true",
                    help="keep the temp file and print its path to stderr")
    ap.add_argument("--tempdir", help="directory for the working file (default: system temp)")
    ap.add_argument("-V", "--version", action="version",
                    version=f"%(prog)s {__version__}")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        ytdlp = find_ytdlp()
    except YtpipeError as exc:
        print(f"ytpipe: {exc}", file=sys.stderr)
        return EXIT_FAIL

    base = ytdlp + ["--newline"]
    if not args.playlist:
        base.append("--no-playlist")
    if args.quiet:
        base.append("--no-progress")
    if args.cookies_from_browser:
        base += ["--cookies-from-browser", args.cookies_from_browser]

    out = sys.stdout.buffer

    if args.audio_only:
        # One stream, no merge, no post-processing: yt-dlp can write straight out.
        cmd = base + ["-f", args.format or AUDIO_SELECTOR, "-o", "-", args.url]
        rc = _pipe_through(cmd, out)
        return EXIT_PIPE if rc < 0 else rc

    fmt = args.format or SELECTORS[args.container]
    tmpdir = tempfile.mkdtemp(prefix="ytpipe-", dir=args.tempdir)
    try:
        target = os.path.join(tmpdir, f"media.{args.container}")
        cmd = base + [
            "-f", fmt,
            "--merge-output-format", args.container,
            "-o", target,
            args.url,
        ]
        rc = _run_quiet(cmd)
        if rc < 0:
            return EXIT_PIPE
        if rc != 0:
            print(f"ytpipe: yt-dlp failed (exit {rc})", file=sys.stderr)
            return EXIT_FAIL

        if not os.path.exists(target):
            leftovers = [n for n in os.listdir(tmpdir)
                         if not n.endswith((".part", ".ytdl", ".temp"))]
            if not leftovers:
                print("ytpipe: yt-dlp reported success but produced no file",
                      file=sys.stderr)
                return EXIT_FAIL
            target = os.path.join(tmpdir, leftovers[0])

        if target.endswith(".mp4"):
            target = ensure_faststart(target)
        if args.keep:
            print(f"ytpipe: kept {target}", file=sys.stderr)
            _stream(target, out)
            return EXIT_OK
        _stream(target, out)
        return EXIT_OK
    finally:
        if not args.keep:
            shutil.rmtree(tmpdir, ignore_errors=True)


def entrypoint() -> None:
    """Console-script wrapper: maps exceptions to exit codes, honours SIGPIPE."""
    if hasattr(signal, "SIGPIPE"):  # not on Windows; the BrokenPipeError path covers it
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    try:
        sys.exit(main())
    except BrokenPipeError:
        # Downstream (`head`, a player) walked away. Stay quiet and exit like a pipe.
        try:
            sys.stdout.buffer.close()
        except Exception:
            pass
        os._exit(EXIT_PIPE)
    except KeyboardInterrupt:
        sys.exit(EXIT_INT)
    except YtpipeError as exc:
        print(f"ytpipe: {exc}", file=sys.stderr)
        sys.exit(EXIT_FAIL)


if __name__ == "__main__":  # pragma: no cover
    entrypoint()
