# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for scripts/update_source.py (no network beyond a local server)."""
import hashlib
import io
import os
import sys
import tarfile
import tempfile
import threading
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["VIDDOWN_ALLOW_INSECURE_SOURCES"] = "1"
import fetch_source as fs  # noqa: E402
import update_source as us  # noqa: E402


def tar_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in files.items():
            i = tarfile.TarInfo(name)
            i.size = len(data)
            t.addfile(i, io.BytesIO(data))
    return buf.getvalue()


class Srv:
    def __init__(self, routes):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = routes.get(self.path)
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class EnsureArchive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = tar_bytes({"pkg-1/pyproject.toml": b"[project]\nname='x'\n"})
        self.srv = Srv({"/a.tar.gz": self.data})
        self.ctx = us.Ctx(Path(self.tmp.name), 2, 1, 0)

    def tearDown(self):
        self.srv.stop()
        self.tmp.cleanup()

    def entry(self, sha, url="/a.tar.gz"):
        return {"version": "1", "commit": "UNPINNED",
                "sources": [{"name": "s", "type": "tarball", "url": self.srv.base + url, "sha256": sha}]}

    def test_downloads_and_verifies_when_pins_already_exist(self):
        # this is the CI failure: every source pinned, nothing downloaded yet
        p = us.ensure_archive(self.ctx, "demo", self.entry(hashlib.sha256(self.data).hexdigest()))
        self.assertTrue(p.is_file())
        self.assertIn("[project]", us._read_member(p, "pyproject.toml"))

    def test_wrong_pin_is_rejected(self):
        with self.assertRaises(us.UpdateError):
            us.ensure_archive(self.ctx, "demo", self.entry("0" * 64))

    def test_unreachable_source_is_an_error_not_a_guess(self):
        with self.assertRaises(us.UpdateError):
            us.ensure_archive(self.ctx, "demo", self.entry(hashlib.sha256(self.data).hexdigest(), "/missing"))

    def test_missing_entry(self):
        with self.assertRaises(us.UpdateError):
            us.ensure_archive(self.ctx, "demo", None)


class Misc(unittest.TestCase):
    def test_checksum_from_sha256sums_and_single(self):
        h = "a" * 64
        self.assertEqual(us.checksum_from(f"{h}  yt-dlp.tar.gz\n{'b' * 64}  other\n", "yt-dlp.tar.gz"), h)
        self.assertEqual(us.checksum_from(h + "\n", "x"), h)
        self.assertIsNone(us.checksum_from(f"{'b' * 64}  other\n", "yt-dlp.tar.gz"))

    def test_emit_lock_round_trips(self):
        comps = {"demo": {"kind": "native-lib", "version": "1.0", "license": "MIT", "commit": "c" * 40,
                          "sources": [{"name": "a", "type": "tarball", "url": "https://x/y.tgz", "sha256": "d" * 64},
                                      {"name": "g", "type": "git", "url": "https://x/r", "ref": "v1.0"}]}}
        back = tomllib.loads(us.emit_lock(comps))["components"]
        self.assertEqual(back["demo"]["sources"][0]["sha256"], "d" * 64)
        self.assertEqual(back["demo"]["sources"][1]["ref"], "v1.0")

    def test_version_ordering_is_numeric(self):
        self.assertEqual(max(["3.14.9", "3.14.10", "3.14.2"], key=us.vkey), "3.14.10")


if __name__ == "__main__":
    unittest.main()
