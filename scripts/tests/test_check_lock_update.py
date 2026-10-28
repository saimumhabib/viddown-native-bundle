# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the auto-merge safety policy (scripts/check_lock_update.py)."""
import copy
import sys
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import check_lock_update as cl  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES = tomllib.loads((ROOT / "native" / "source-templates.toml").read_text())["components"]
H = lambda c: c * 64  # noqa: E731


def comp(version, commit="a" * 40, lic="MIT", sha="1", extra_git=True):
    srcs = [{"name": "rel", "type": "tarball", "url": "https://x/rel.tgz", "sha256": H(sha)},
            {"name": "tag", "type": "tarball", "url": "https://x/tag.tgz", "sha256_uncompressed": H("2")}]
    if extra_git:
        srcs.append({"name": "git", "type": "git", "url": "https://x/r", "ref": "v" + version})
    return {"kind": "native-lib", "version": version, "license": lic, "commit": commit, "sources": srcs}


def lock(**comps):
    return comps


class Policy(unittest.TestCase):
    def run_check(self, old, new):
        return cl.check(old, new, TEMPLATES)

    def test_identical_passes(self):
        o = lock(libffi=comp("3.4.4"))
        self.assertEqual(self.run_check(o, copy.deepcopy(o))[0], [])

    def test_filling_unpinned_is_allowed(self):
        o = lock(libffi=comp("3.4.4"))
        o["libffi"]["sources"][0]["sha256"] = "UNPINNED"
        n = lock(libffi=comp("3.4.4"))
        problems, changes = self.run_check(o, n)
        self.assertEqual(problems, [])
        self.assertTrue(any("pinned sha256" in c for c in changes))

    def test_version_bump_with_pins_is_allowed(self):
        o = lock(**{"yt-dlp": comp("2026.10.21")})
        n = lock(**{"yt-dlp": comp("2026.11.02", commit="b" * 40, sha="9")})
        self.assertEqual(self.run_check(o, n)[0], [])

    def test_downgrade_is_blocked(self):
        o = lock(**{"yt-dlp": comp("2026.11.02")})
        n = lock(**{"yt-dlp": comp("2026.10.21", commit="b" * 40)})
        self.assertTrue(any("DOWN" in p for p in self.run_check(o, n)[0]))

    def test_series_jump_is_blocked(self):
        o = lock(cpython=comp("3.14.10"))
        n = lock(cpython=comp("3.15.0", commit="b" * 40))
        self.assertTrue(any("outside the tracked series" in p for p in self.run_check(o, n)[0]))

    def test_patch_bump_inside_series_is_allowed(self):
        o = lock(cpython=comp("3.14.10"))
        n = lock(cpython=comp("3.14.11", commit="b" * 40))
        self.assertEqual(self.run_check(o, n)[0], [])

    def test_changed_hash_for_same_version_is_blocked(self):
        o = lock(libffi=comp("3.4.4"))
        n = lock(libffi=comp("3.4.4", sha="7"))
        self.assertTrue(any("CHANGED for the same version" in p for p in self.run_check(o, n)[0]))

    def test_moved_tag_commit_is_blocked(self):
        o = lock(libffi=comp("3.4.4"))
        n = lock(libffi=comp("3.4.4", commit="c" * 40))
        self.assertTrue(any("tag commit CHANGED" in p for p in self.run_check(o, n)[0]))

    def test_url_change_is_blocked(self):
        o = lock(libffi=comp("3.4.4"))
        n = copy.deepcopy(o)
        n["libffi"]["sources"][0]["url"] = "https://evil.example/rel.tgz"
        self.assertTrue(any("url changed" in p for p in self.run_check(o, n)[0]))

    def test_removed_source_and_component_blocked(self):
        o = lock(libffi=comp("3.4.4"), openssl=comp("3.5.9"))
        n = copy.deepcopy(o)
        del n["openssl"]
        n["libffi"]["sources"].pop(0)
        probs = self.run_check(o, n)[0]
        self.assertTrue(any("component was removed" in p for p in probs))
        self.assertTrue(any("source was removed" in p for p in probs))

    def test_new_component_blocked(self):
        o = lock(libffi=comp("3.4.4"))
        n = lock(libffi=comp("3.4.4"), brotli=comp("1.2.0"))
        self.assertTrue(any("NEW component" in p for p in self.run_check(o, n)[0]))

    def test_new_version_needs_two_verifiable_sources(self):
        o = lock(**{"yt-dlp": comp("2026.10.21")})
        n = lock(**{"yt-dlp": comp("2026.11.02", commit="UNPINNED", sha="UNPINNED")})
        n["yt-dlp"]["sources"][1]["sha256_uncompressed"] = "UNPINNED"
        probs = self.run_check(o, n)[0]
        self.assertTrue(any("no pinned tag commit" in p for p in probs))
        self.assertTrue(any("fewer than 2" in p for p in probs))

    def test_licence_change_blocked(self):
        o = lock(libffi=comp("3.4.4"))
        n = lock(libffi=comp("3.4.4", lic="GPL-3.0"))
        self.assertTrue(any("licence changed" in p for p in self.run_check(o, n)[0]))

    def test_real_lock_against_itself_passes(self):
        real = tomllib.loads((ROOT / "native" / "sources.lock.toml").read_text())["components"]
        self.assertEqual(cl.check(real, copy.deepcopy(real), TEMPLATES)[0], [])


if __name__ == "__main__":
    unittest.main()
