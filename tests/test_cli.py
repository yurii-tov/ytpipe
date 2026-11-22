"""Offline test suite: no network, no real yt-dlp, no real ffmpeg.

Both dependencies are replaced by the shell doubles in tests/fakes/.
Run: python -m unittest discover -s tests -v
"""

import io
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from ytpipe import cli  # noqa: E402

FAKES = Path(__file__).resolve().parent / "fakes"
URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def box(btype: bytes, payload: bytes = b"") -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + btype + payload


# moov ahead of mdat (already faststart) vs moov trailing (needs a remux pass)
MOOV_FIRST = box(b"ftyp", b"isomiso2") + box(b"moov", b"\0" * 24) + box(b"mdat", b"A" * 4096)
MOOV_LAST = box(b"ftyp", b"isomiso2") + box(b"mdat", b"B" * 4096) + box(b"moov", b"\0" * 24)
BIG = box(b"ftyp", b"isomiso2") + box(b"moov", b"\0" * 24) + box(b"mdat", b"C" * (3 << 20))


class Capture:
    """Stands in for sys.stdout: cli only touches `.buffer`."""

    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, _s: str) -> int:
        return 0

    def flush(self) -> None:  # pragma: no cover
        pass


class Base(unittest.TestCase):
    def setUp(self) -> None:
        for fake in ("yt-dlp", "ffmpeg"):
            os.chmod(FAKES / fake, 0o755)
        self.tmp = tempfile.mkdtemp(prefix="ytpipe-test-")
        self.log = os.path.join(self.tmp, "calls.log")
        self.media = os.path.join(self.tmp, "media.bin")
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work, exist_ok=True)
        self.write_media(MOOV_FIRST)
        self._env = unittest.mock.patch.dict(os.environ, {
            "YTDLP": str(FAKES / "yt-dlp"),
            "FFMPEG": str(FAKES / "ffmpeg"),
            "FAKE_MEDIA": self.media,
            "FAKE_LOG": self.log,
            "HOME": self.tmp,
        })
        self._env.start()
        for var in ("FAKE_EXIT", "FAKE_FFMPEG_EXIT"):
            os.environ.pop(var, None)

    def tearDown(self) -> None:
        self._env.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers -------------------------------------------------------------
    def write_media(self, data: bytes) -> None:
        with open(self.media, "wb") as fh:
            fh.write(data)

    def calls(self) -> str:
        return Path(self.log).read_text() if os.path.exists(self.log) else ""

    def run_cli(self, *argv: str):
        cap = Capture()
        with unittest.mock.patch.object(sys, "stdout", cap):
            rc = cli.main([*argv, "--tempdir", self.work])
        return rc, cap.buffer.getvalue()

    # tests ---------------------------------------------------------------
    def test_default_video_streams_bytes_untouched(self):
        rc, out = self.run_cli(URL)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(out, MOOV_FIRST, "stdout must be the file byte for byte")
        self.assertNotIn("ffmpeg", self.calls(), "faststart already fine: no remux")

    def test_trailing_moov_triggers_faststart_remux(self):
        self.write_media(MOOV_LAST)
        rc, out = self.run_cli(URL)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("ffmpeg", self.calls(), "ffmpeg should have been invoked")
        self.assertNotEqual(out, MOOV_LAST)
        kinds = [t for t, _ in cli.top_level_boxes_from_bytes(out)]
        self.assertLess(kinds.index("moov"), kinds.index("mdat"), "moov must lead mdat")

    def test_box_walker_parses_real_offsets(self):
        path = os.path.join(self.tmp, "probe.mp4")
        with open(path, "wb") as fh:
            fh.write(MOOV_LAST)
        self.assertEqual([t for t, _ in cli.top_level_boxes(path)],
                         ["ftyp", "mdat", "moov"])
        self.assertTrue(cli.needs_faststart(path))
        with open(path, "wb") as fh:
            fh.write(MOOV_FIRST)
        self.assertFalse(cli.needs_faststart(path))

    def test_audio_only_pipes_straight_from_ytdlp(self):
        rc, out = self.run_cli("-a", URL)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(out, MOOV_FIRST)
        self.assertIn("-o -", self.calls())
        self.assertIn("ba[ext=m4a]/ba/b", self.calls())
        staged = [n for n in os.listdir(self.work) if n.startswith("ytpipe-")]
        self.assertEqual(staged, [], "audio-only must not stage a temp file")

    def test_ytdlp_failure_exits_1_with_empty_stdout(self):
        os.environ["FAKE_EXIT"] = "3"
        rc, out = self.run_cli(URL)
        self.assertEqual(rc, cli.EXIT_FAIL)
        self.assertEqual(out, b"")

    def test_missing_ytdlp_reports_and_exits_1(self):
        os.environ.pop("YTDLP")
        with unittest.mock.patch("importlib.util.find_spec", return_value=None), \
             unittest.mock.patch("shutil.which", return_value=None):
            rc, out = self.run_cli(URL)
        self.assertEqual(rc, cli.EXIT_FAIL)
        self.assertEqual(out, b"")

    def test_failed_remux_still_streams_source(self):
        self.write_media(MOOV_LAST)
        os.environ["FAKE_FFMPEG_EXIT"] = "1"
        rc, out = self.run_cli(URL)
        self.assertEqual(rc, cli.EXIT_OK, "a dead remux must not lose the download")
        self.assertEqual(out, MOOV_LAST)

    def test_workdir_cleaned_up(self):
        rc, _ = self.run_cli(URL)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(os.listdir(self.work), [])

    def test_mkv_skips_faststart(self):
        rc, out = self.run_cli("-c", "mkv", URL)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertNotIn("ffmpeg", self.calls())
        self.assertIn("--merge-output-format mkv", self.calls())


class SubprocessTests(Base):
    """End-to-end through the real entry point, for pipe semantics and sockets."""

    def spawn(self, *args: str, **kwargs):
        env = dict(os.environ, PYTHONPATH=str(REPO))
        return subprocess.run(
            [sys.executable, "-m", "ytpipe", *args, "--tempdir", self.work],
            cwd=str(REPO), env=env, **kwargs)

    def test_broken_pipe_exits_141_without_traceback(self):
        self.write_media(BIG)  # larger than a pipe buffer, so EPIPE is reachable
        proc = subprocess.run(
            f"set -o pipefail; {sys.executable} -m ytpipe -q -a {URL} | head -c 4",
            shell=True, executable="/bin/bash", cwd=str(REPO), capture_output=True,
            env=dict(os.environ, PYTHONPATH=str(REPO)))
        self.assertEqual(proc.returncode, 141, "expected a clean SIGPIPE death")
        self.assertEqual(len(proc.stdout), 4)
        self.assertNotIn(b"Traceback", proc.stderr)

    def test_keep_prints_path_and_retains_file(self):
        proc = self.spawn("-q", "--keep", URL, capture_output=True)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, MOOV_FIRST)
        self.assertIn("kept", proc.stderr.decode())

    def test_version_flag(self):
        proc = self.spawn("--version", capture_output=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("ytpipe", proc.stdout.decode())


if __name__ == "__main__":
    unittest.main()
