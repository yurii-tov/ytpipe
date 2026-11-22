#!/usr/bin/env bash
# Live smoke test: small real downloads, verified with ffprobe.
# Hits the network and YouTube (~½ MB).
#
# The tolerated 141s are expected: ffprobe stops reading once it has the metadata, so
# ytpipe dies of SIGPIPE exactly like `cat` would. Don't "fix" that.
set -euo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
URL="${1:-https://www.youtube.com/watch?v=jNQXAC9IVRw}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

run() {
  if [[ -n "${YTPIPE:-}" ]]; then
    "$YTPIPE" "$@"
  elif command -v ytpipe >/dev/null 2>&1; then
    ytpipe "$@"
  else
    PYTHONPATH="$REPO" python3 -m ytpipe "$@"
  fi
}

# probe_stdin <label> <args...> — pipe media into ffprobe, then assert on the report.
probe_stdin() {
  local label="$1"; shift
  local report="$WORK/$(echo "$label" | tr -cs 'a-z0-9' '_').txt"
  echo "== $label"
  run "$@" 2>/dev/null | ffprobe -v error -show_entries format=duration \
    -show_entries stream=codec_type,codec_name -of default=nw=1 -i - > "$report" || true
  sed 's/^/   /' "$report"
  grep -q '^duration=' "$report" || { echo "   FAIL: no duration from the pipe" >&2; return 1; }
}

echo "== video -> mp4 on stdout"
run -q "$URL" > "$WORK/out.mp4"
awk -v s="$(stat -c%s "$WORK/out.mp4")" 'BEGIN{print "   size:", s, "bytes"}'
ffprobe -v error -show_entries format=format_name,duration \
        -show_entries stream=codec_type,codec_name,width,height \
        -of default=nw=1 "$WORK/out.mp4" | sed 's/^/   /'

echo "== faststart check (moov before mdat)"
python3 - "$WORK/out.mp4" <<'PY'
import sys
d = open(sys.argv[1], "rb").read(400_000)
moov, mdat = d.find(b"moov"), d.find(b"mdat")
print(f"   moov@{moov} mdat@{mdat}")
if moov == -1 or (mdat != -1 and mdat < moov):
    sys.exit("   FAIL: moov must lead mdat for pipe playback")
PY

probe_stdin "readable from a pipe, no seeking" -q "$URL"
probe_stdin "audio-only streamed straight through" -q -a "$URL"

echo "== smoke ok"
