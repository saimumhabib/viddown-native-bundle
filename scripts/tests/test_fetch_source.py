# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for scripts/fetch_source.py using a LOCAL server that simulates failures.

Run:  python3 -m unittest discover -s scripts/tests -v
"""
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["VIDDOWN_ALLOW_INSECURE_SOURCES"] = "1"  # tests use http://127.0.0.1
import fetch_source as fs  # noqa: E402

COMMIT = "a" * 40


def make_tar_gz(payload: bytes = b"hello", commit: str | None = None, mtime: int = 0) -> bytes:
    buf = io.BytesIO()
    kw = {"pax_headers": {"comment": commit}} if commit else {}
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT, **kw) as t:
        info = tarfile.TarInfo("pkg-1.0/file.txt")
        info.size = len(payload)
        info.mtime = 0
        t.addfile(info, io.BytesIO(payload))
    raw = buf.getvalue()
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=mtime) as g:
        g.write(raw)
    return out.getvalue()


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Server:
    def __init__(self):
        self.routes = {}
        self.hits = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits[self.path] = outer.hits.get(self.path, 0) + 1
                outer.routes.get(self.path, lambda h: h.send_error(404))(self)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def serve(self, path, body: bytes):
        def h(req):
            req.send_response(200)
            req.send_header("Content-Length", str(len(body)))
            req.end_headers()
            req.wfile.write(body)
        self.routes[path] = h

    def serve_status(self, path, code):
        self.routes[path] = lambda req: req.send_error(code)

    def serve_slow(self, path, body: bytes, delay: float):
        def h(req):
            time.sleep(delay)
            try:
                req.send_response(200)
                req.send_header("Content-Length", str(len(body)))
                req.end_headers()
                req.wfile.write(body)
            except OSError:
                pass
        self.routes[path] = h

    def serve_truncated(self, path, body: bytes):
        def h(req):
            req.send_response(200)
            req.send_header("Content-Length", str(len(body) + 1000))
            req.end_headers()
            req.wfile.write(body)
            req.close_connection = True
        self.routes[path] = h


def manifest(sources, version="1.0", commit=None):
    comp = {"version": version, "license": "MIT", "sources": sources}
    if commit:
        comp["commit"] = commit
    return {"components": {"demo": comp}}


def src(name, url, **pins):
    return {"name": name, "type": "tarball", "url": url, **pins}


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.srv = Server()
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "out"
        self.cache = Path(self.tmp.name) / "cache"
        self.good = make_tar_gz(b"the real content")
        self.srv.serve("/good.tar.gz", self.good)
        self.pin = {"sha256": sha(self.good)}
        self.fast = dict(timeout=2, retries=2, backoff=0)

    def tearDown(self):
        self.srv.stop()
        self.tmp.cleanup()

    def fetch(self, m, cache=False, **kw):
        args = {**self.fast, **kw}
        return fs.fetch_component(m, "demo", self.out, self.cache if cache else None, **args)

    # ---- fallback behaviour -------------------------------------------------
    def test_first_source_ok_is_used(self):
        m = manifest([src("one", self.srv.url("/good.tar.gz"), **self.pin),
                      src("two", self.srv.url("/good.tar.gz"), **self.pin)])
        prov = self.fetch(m)
        self.assertEqual(prov["source_used"]["name"], "one")
        self.assertEqual(prov["source_used"]["index"], 1)

    def test_connection_refused_falls_through(self):
        dead = Server()
        dead_url = dead.url("/good.tar.gz")
        dead.stop()  # nothing listens on this port any more
        m = manifest([src("dead", dead_url, **self.pin), src("live", self.srv.url("/good.tar.gz"), **self.pin)])
        prov = self.fetch(m)
        self.assertEqual(prov["source_used"]["name"], "live")
        self.assertEqual(prov["attempts"][0]["status"], "failed")

    def test_http_404_falls_through_without_retry(self):
        self.srv.serve_status("/missing", 404)
        m = manifest([src("gone", self.srv.url("/missing"), **self.pin), src("live", self.srv.url("/good.tar.gz"), **self.pin)])
        prov = self.fetch(m, retries=3)
        self.assertEqual(prov["source_used"]["name"], "live")
        self.assertEqual(self.srv.hits["/missing"], 1)  # 404 is permanent: no retries

    def test_http_500_is_retried_then_falls_through(self):
        self.srv.serve_status("/boom", 500)
        m = manifest([src("boom", self.srv.url("/boom"), **self.pin), src("live", self.srv.url("/good.tar.gz"), **self.pin)])
        prov = self.fetch(m, retries=3)
        self.assertEqual(prov["source_used"]["name"], "live")
        self.assertEqual(self.srv.hits["/boom"], 3)

    def test_timeout_falls_through(self):
        self.srv.serve_slow("/slow", self.good, delay=3)
        m = manifest([src("slow", self.srv.url("/slow"), **self.pin), src("live", self.srv.url("/good.tar.gz"), **self.pin)])
        prov = self.fetch(m, timeout=0.5, retries=1)
        self.assertEqual(prov["source_used"]["name"], "live")

    def test_truncated_download_falls_through(self):
        self.srv.serve_truncated("/trunc", self.good)
        m = manifest([src("trunc", self.srv.url("/trunc"), **self.pin), src("live", self.srv.url("/good.tar.gz"), **self.pin)])
        prov = self.fetch(m, retries=1)
        self.assertEqual(prov["source_used"]["name"], "live")
        self.assertIn("truncated", prov["attempts"][0]["reason"])

    def test_wrong_checksum_is_rejected_and_next_source_used(self):
        evil = make_tar_gz(b"tampered content")
        self.srv.serve("/evil.tar.gz", evil)
        m = manifest([src("evil", self.srv.url("/evil.tar.gz"), **self.pin), src("live", self.srv.url("/good.tar.gz"), **self.pin)])
        prov = self.fetch(m)
        self.assertEqual(prov["source_used"]["name"], "live")
        self.assertIn("sha256 mismatch", prov["attempts"][0]["reason"])
        # the tampered file must not be left behind as an output
        archived = (self.out / "demo-1.0" / "evil.tar.gz")
        self.assertFalse(archived.exists())

    def test_only_wrong_checksum_source_fails_build(self):
        evil = make_tar_gz(b"tampered content")
        self.srv.serve("/evil.tar.gz", evil)
        m = manifest([src("evil", self.srv.url("/evil.tar.gz"), **self.pin)])
        with self.assertRaises(fs.AllSourcesFailed):
            self.fetch(m)

    # ---- all fail -----------------------------------------------------------
    def test_all_sources_fail_lists_every_reason(self):
        self.srv.serve_status("/a", 404)
        self.srv.serve_status("/b", 500)
        m = manifest([src("alpha", self.srv.url("/a"), **self.pin), src("beta", self.srv.url("/b"), **self.pin)])
        with self.assertRaises(fs.AllSourcesFailed) as cm:
            self.fetch(m, retries=1)
        text = str(cm.exception)
        self.assertIn("alpha", text)
        self.assertIn("beta", text)
        self.assertIn("HTTP 404", text)
        self.assertIn("HTTP 500", text)

    def test_cli_exits_nonzero_with_clear_message(self):
        self.srv.serve_status("/a", 404)
        mpath = Path(self.tmp.name) / "m.toml"
        mpath.write_text(
            '[components.demo]\nversion = "1.0"\nlicense = "MIT"\n'
            f'[[components.demo.sources]]\nname = "alpha"\ntype = "tarball"\nurl = "{self.srv.url("/a")}"\nsha256 = "{"0" * 64}"\n')
        p = subprocess.run([sys.executable, str(Path(fs.__file__)), "--manifest", str(mpath), "--retries", "1",
                            "fetch", "demo", "--out", str(self.out)],
                           capture_output=True, text=True, env=dict(os.environ))
        self.assertEqual(p.returncode, 2)
        self.assertIn("all sources failed for 'demo'", p.stderr)
        self.assertIn("alpha", p.stderr)

    # ---- pinning rules ------------------------------------------------------
    def test_unpinned_source_is_skipped_not_trusted(self):
        m = manifest([src("unpinned", self.srv.url("/good.tar.gz"), sha256="UNPINNED"),
                      src("live", self.srv.url("/good.tar.gz"), **self.pin)])
        prov = self.fetch(m)
        self.assertEqual(prov["source_used"]["name"], "live")
        self.assertTrue(prov["attempts"][0]["status"].startswith("skipped"))
        # only the pinned "live" source was downloaded; the unpinned one never was
        self.assertEqual(self.srv.hits["/good.tar.gz"], 1)

    def test_only_unpinned_sources_fail(self):
        m = manifest([src("unpinned", self.srv.url("/good.tar.gz"))])
        with self.assertRaises(fs.AllSourcesFailed):
            self.fetch(m)

    def test_uncompressed_hash_survives_recompression(self):
        a = make_tar_gz(b"same tar", mtime=1)
        b = make_tar_gz(b"same tar", mtime=99999)
        self.assertNotEqual(a, b)  # different gzip bytes
        self.srv.serve("/b.tar.gz", b)
        unc = hashlib.sha256(gzip.decompress(a)).hexdigest()
        m = manifest([src("gh", self.srv.url("/b.tar.gz"), sha256_uncompressed=unc)])
        self.assertEqual(self.fetch(m)["source_used"]["name"], "gh")

    def test_archive_commit_mismatch_rejected(self):
        data = make_tar_gz(b"x", commit="b" * 40)
        self.srv.serve("/c.tar.gz", data)
        m = manifest([src("gh", self.srv.url("/c.tar.gz"), sha256=sha(data)), src("live", self.srv.url("/good.tar.gz"), **self.pin)],
                     commit=COMMIT)
        prov = self.fetch(m)
        self.assertEqual(prov["source_used"]["name"], "live")
        self.assertIn("archive commit", prov["attempts"][0]["reason"])

    def test_archive_commit_match_recorded(self):
        data = make_tar_gz(b"x", commit=COMMIT)
        self.srv.serve("/c.tar.gz", data)
        m = manifest([src("gh", self.srv.url("/c.tar.gz"), sha256=sha(data))], commit=COMMIT)
        self.assertEqual(self.fetch(m)["evidence"]["archive_commit"], COMMIT)

    def test_plain_http_refused_without_test_override(self):
        os.environ.pop("VIDDOWN_ALLOW_INSECURE_SOURCES")
        try:
            m = manifest([src("plain", self.srv.url("/good.tar.gz"), **self.pin)])
            with self.assertRaises(fs.AllSourcesFailed) as cm:
                self.fetch(m)
            self.assertIn("non-https", str(cm.exception))
        finally:
            os.environ["VIDDOWN_ALLOW_INSECURE_SOURCES"] = "1"

    # ---- cache --------------------------------------------------------------
    def test_cache_survives_outage_and_is_reverified(self):
        m = manifest([src("one", self.srv.url("/good.tar.gz"), **self.pin)])
        self.fetch(m, cache=True)
        self.srv.stop()  # upstream outage
        prov = self.fetch(m, cache=True)
        self.assertIn("cache hit", prov["attempts"][0]["reason"])

    def test_tampered_cache_is_rejected(self):
        m = manifest([src("one", self.srv.url("/good.tar.gz"), **self.pin)])
        self.fetch(m, cache=True)
        cached = self.cache / "demo-1.0" / "one" / "good.tar.gz"
        cached.write_bytes(b"corrupted")
        prov = self.fetch(m, cache=True)  # server is still up: re-downloaded
        self.assertIn("downloaded", prov["attempts"][0]["reason"])
        self.assertEqual(sha(cached.read_bytes()), self.pin["sha256"])

    def test_tampered_cache_with_upstream_down_fails(self):
        m = manifest([src("one", self.srv.url("/good.tar.gz"), **self.pin)])
        self.fetch(m, cache=True)
        (self.cache / "demo-1.0" / "one" / "good.tar.gz").write_bytes(b"corrupted")
        self.srv.stop()
        with self.assertRaises(fs.AllSourcesFailed):
            self.fetch(m, cache=True)

    # ---- provenance + health check -----------------------------------------
    def test_provenance_file_records_source(self):
        m = manifest([src("one", self.srv.url("/good.tar.gz"), **self.pin)])
        self.fetch(m)
        prov = json.loads((self.out / "demo-1.0" / "provenance.json").read_text())
        self.assertEqual(prov["source_used"]["name"], "one")
        self.assertEqual(prov["evidence"]["sha256"], self.pin["sha256"])

    def test_health_check_reports_each_source(self):
        self.srv.serve_status("/gone", 404)
        m = manifest([src("ok", self.srv.url("/good.tar.gz"), **self.pin),
                      src("gone", self.srv.url("/gone"), **self.pin),
                      src("unp", self.srv.url("/good.tar.gz"), sha256="UNPINNED")])
        rep = fs.check_all(m, None, 2, 1, 0)
        st = [r["status"] for r in rep["demo"]["sources"]]
        self.assertEqual(st, ["ok", "broken", "unpinned"])


class GitSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "upstream"
        self.repo.mkdir()
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        run = lambda *a: subprocess.run(["git", *a], cwd=self.repo, env=env, check=True, capture_output=True, text=True).stdout.strip()
        run("init", "-q", "-b", "main")
        (self.repo / "f.txt").write_text("hello")
        run("add", ".")
        run("commit", "-q", "-m", "init")
        run("tag", "-a", "v1.0", "-m", "v1.0")
        self.commit = run("rev-parse", "v1.0^{commit}")
        self.url = f"file://{self.repo}"
        self.out = Path(self.tmp.name) / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def m(self, commit):
        return {"components": {"demo": {"version": "1.0", "commit": commit, "sources": [
            {"name": "git", "type": "git", "url": self.url, "ref": "v1.0"}]}}}

    def test_git_commit_verified(self):
        prov = fs.fetch_component(self.m(self.commit), "demo", self.out, None, 5, 1, 0)
        self.assertEqual(prov["evidence"]["git_commit"], self.commit)
        with tarfile.open(self.out / "demo-1.0" / "demo-1.0.tar") as t:
            self.assertIn("demo-1.0/f.txt", t.getnames())

    def test_git_commit_mismatch_rejected(self):
        with self.assertRaises(fs.AllSourcesFailed) as cm:
            fs.fetch_component(self.m("c" * 40), "demo", self.out, None, 5, 1, 0)
        self.assertIn("commit mismatch", str(cm.exception))

    def test_git_without_pinned_commit_is_skipped(self):
        with self.assertRaises(fs.AllSourcesFailed) as cm:
            fs.fetch_component(self.m("UNPINNED"), "demo", self.out, None, 5, 1, 0)
        self.assertIn("skipped", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
