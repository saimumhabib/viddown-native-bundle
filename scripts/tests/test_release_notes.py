# SPDX-License-Identifier: GPL-3.0-or-later
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


class ReleaseNotes(unittest.TestCase):
    def notes(self, names, **extra):
        with tempfile.TemporaryDirectory() as d:
            for n in names:
                (Path(d) / n).write_bytes(b"x")
            cmd = [sys.executable, str(ROOT / "scripts/ci/release_notes.py"), "--lock", str(ROOT / "native/sources.lock.toml"),
                   "--tag", "native-v2026.10.28", "--assets", d]
            for k, v in extra.items():
                cmd += [f"--{k.replace('_', '-')}", v]
            return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout

    def test_only_x86_64_is_claimed_as_executed(self):
        t = self.notes(["viddown-native-arm64-v8a-native-v2026.10.28.zip", "viddown-native-x86_64-native-v2026.10.28.zip",
                        "viddown-native-x86-native-v2026.10.28.zip", "SHA256SUMS.txt"])
        self.assertIn("**x86_64**: built, and executed in CI", t)
        self.assertIn("**arm64-v8a**: built from source, **not run-tested in CI**", t)
        self.assertIn("**x86**: built from source, **not run-tested in CI**", t)
        self.assertNotIn("armeabi-v7a", t)  # not present, so not mentioned

    def test_lists_real_component_versions_and_links(self):
        t = self.notes(["viddown-native-x86_64-native-v2026.10.28.zip"], pr="7", repo="o/r", run_url="https://x/run/1")
        self.assertIn("| yt-dlp |", t)
        self.assertIn("pull/7", t)
        self.assertIn("https://x/run/1", t)
        self.assertIn("BUILD_INFO.json", t)


if __name__ == "__main__":
    unittest.main()
