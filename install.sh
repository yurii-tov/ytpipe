#!/usr/bin/env bash
# Install ytpipe into an isolated venv and link it onto PATH.
# Usage: ./install.sh [-e]     (-e: editable install, for development)
set -euo pipefail

VENV="${YTPIPE_VENV:-$HOME/.venvs/ytpipe}"
BINDIR="${YTPIPE_BINDIR:-$HOME/.local/bin}"
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PIP_ARGS=()
[[ "${1:-}" == "-e" || "${1:-}" == "--editable" ]] && PIP_ARGS+=(-e)

echo "==> venv: $VENV"
python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install -q -U pip

echo "==> installing ytpipe ${PIP_ARGS[*]:-}"
"$VENV/bin/python" -m pip install -q ${PIP_ARGS[*]:-} "$HERE"

mkdir -p "$BINDIR"
ln -sf "$VENV/bin/ytpipe" "$BINDIR/ytpipe"

echo "==> linked $BINDIR/ytpipe"
"$VENV/bin/ytpipe" --version

"$VENV/bin/python" -m yt_dlp --version | sed 's/^/==> yt-dlp /'

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "!!  ffmpeg not on PATH: audio-only (-a) works, merged video will fail." >&2
  echo "!!  install it (pacman -S ffmpeg / apt install ffmpeg) and re-run." >&2
else
  echo "==> ffmpeg: $(command -v ffmpeg)"
fi

case ":$PATH:" in
  *":$BINDIR:"*) ;;
  *) echo "!!  $BINDIR is not on your PATH; add it or call ytpipe by full path." >&2 ;;
esac

echo "==> done. try: ytpipe -q 'https://youtu.be/jNQXAC9IVRw' | ffprobe -i -"
