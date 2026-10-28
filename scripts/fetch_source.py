#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fetch and verify upstream sources through an ordered list of official sources.

Every component in native/sources.lock.toml has an ordered list of sources.
They are tried in order. A source is accepted ONLY if its content verifies
against a pin recorded in the manifest (sha256 of the file, sha256 of the
decompressed tar stream, and/or the git commit of the tag). A source that fails
for any reason, or is not pinned yet, is logged and the next one is tried.
There is no "accept anyway" path.

Used identically on a developer machine and in GitHub Actions.

Exit codes: 0 ok, 1 usage/manifest error, 2 all sources failed.
"""
from __future__ import annotations

import argparse
import bz2
import gzip
import hashlib
import http.client
import json
import lzma
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore

UNPINNED_VALUES = {"", "UNPINNED", "PIN_REQUIRED"}
MAX_BYTES = 2 * 1024**3
CHUNK = 1 << 20
ALLOW_INSECURE_ENV = "VIDDOWN_ALLOW_INSECURE_SOURCES"  # tests only
USER_AGENT = "viddown-native-bundle-fetch/1"
SOURCE_TYPES = {"tarball", "git"}


class SourceError(Exception):
    """This source cannot be used (permanent for this attempt)."""


class RetryableError(SourceError):
    """Transient failure; worth retrying the same source."""


class UnpinnedError(SourceError):
    """The manifest has no usable pin for this source, so it is skipped."""


class ManifestError(Exception):
    pass


class AllSourcesFailed(Exception):
    def __init__(self, name: str, version: str, attempts: list[dict]):
        self.name, self.version, self.attempts = name, version, attempts
        super().__init__(self.render())

    def render(self) -> str:
        lines = [f"all sources failed for '{self.name}' {self.version}:"]
        for a in self.attempts:
            lines.append(f"  [{a['index']}/{a['total']}] {a['name']} ({a['url']}): {a['status']} - {a.get('reason', '')}")
        return "\n".join(lines)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def is_pinned(value) -> bool:
    return isinstance(value, str) and value.strip() not in UNPINNED_VALUES


# ---------------------------------------------------------------- hashing

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _opener(path: Path):
    n = path.name.lower()
    if n.endswith((".gz", ".tgz")):
        return gzip.open
    if n.endswith((".xz", ".txz")):
        return lzma.open
    if n.endswith((".bz2", ".tbz2")):
        return bz2.open
    return open


def sha256_uncompressed(path: Path) -> str:
    """Hash of the decompressed stream, so a recompression by a host (it has
    happened with GitHub archives) does not change the pin."""
    h = hashlib.sha256()
    try:
        with _opener(path)(path, "rb") as f:
            for chunk in iter(lambda: f.read(CHUNK), b""):
                h.update(chunk)
    except (OSError, EOFError, lzma.LZMAError) as e:
        raise SourceError(f"cannot decompress archive: {e}") from e
    return h.hexdigest()


def archive_commit(path: Path) -> str | None:
    """`git archive` stores the commit id in the tar's pax global header."""
    try:
        with tarfile.open(path) as t:
            return t.pax_headers.get("comment")
    except (tarfile.TarError, OSError, EOFError):
        return None


# ---------------------------------------------------------------- manifest

def load_manifest(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ManifestError(f"cannot read manifest {path}: {e}") from e
    comps = data.get("components")
    if not isinstance(comps, dict) or not comps:
        raise ManifestError("manifest has no [components]")
    for name, c in comps.items():
        if not c.get("version"):
            raise ManifestError(f"{name}: missing 'version'")
        srcs = c.get("sources")
        if not isinstance(srcs, list) or not srcs:
            raise ManifestError(f"{name}: needs at least one [[sources]] entry")
        for s in srcs:
            for k in ("name", "type", "url"):
                if not s.get(k):
                    raise ManifestError(f"{name}: source missing '{k}': {s}")
            if s["type"] not in SOURCE_TYPES:
                raise ManifestError(f"{name}/{s['name']}: unknown type {s['type']!r}")
            if s["type"] == "git" and not s.get("ref"):
                raise ManifestError(f"{name}/{s['name']}: git source needs 'ref'")
    return data


# ---------------------------------------------------------------- download

def _check_scheme(url: str) -> None:
    scheme = urllib.parse.urlparse(url).scheme
    if scheme == "https":
        return
    if scheme in ("http", "file") and os.environ.get(ALLOW_INSECURE_ENV) == "1":
        return
    raise SourceError(f"refusing non-https source URL ({scheme or 'no scheme'})")


def _download_once(url: str, dest: Path, timeout: float) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
            expected = r.headers.get("Content-Length")
            for chunk in iter(lambda: r.read(CHUNK), b""):
                total += len(chunk)
                if total > MAX_BYTES:
                    raise SourceError("download exceeds size limit")
                f.write(chunk)
        if expected is not None and int(expected) != total:
            raise RetryableError(f"truncated download ({total} of {expected} bytes)")
    except urllib.error.HTTPError as e:  # before URLError (it is a subclass)
        if e.code >= 500 or e.code in (408, 429):
            raise RetryableError(f"HTTP {e.code}") from e
        raise SourceError(f"HTTP {e.code}") from e
    except (http.client.HTTPException, urllib.error.URLError, OSError) as e:
        raise RetryableError(f"network error: {e}") from e


def download(url: str, dest: Path, timeout: float, retries: int, backoff: float) -> None:
    _check_scheme(url)
    last: SourceError | None = None
    for attempt in range(1, retries + 1):
        try:
            _download_once(url, dest, timeout)
            return
        except RetryableError as e:
            last = e
            if attempt < retries:
                delay = backoff * (2 ** (attempt - 1))
                log(f"    attempt {attempt}/{retries} failed ({e}); retrying in {delay:g}s")
                time.sleep(delay)
    assert last is not None
    raise SourceError(f"{last} (after {retries} attempts)")


# ---------------------------------------------------------------- verification

def verify_tarball(path: Path, src: dict, commit: str | None) -> dict:
    evidence: dict = {}
    declared = False
    if is_pinned(src.get("sha256")):
        got = sha256_file(path)
        if got != src["sha256"].lower():
            raise SourceError(f"sha256 mismatch (expected {src['sha256']}, got {got})")
        evidence["sha256"] = got
        declared = True
    if is_pinned(src.get("sha256_uncompressed")):
        got = sha256_uncompressed(path)
        if got != src["sha256_uncompressed"].lower():
            raise SourceError(f"sha256_uncompressed mismatch (expected {src['sha256_uncompressed']}, got {got})")
        evidence["sha256_uncompressed"] = got
        declared = True
    if not declared:
        raise UnpinnedError("no sha256 pin in manifest for this source")
    pc = archive_commit(path)
    if pc:
        if is_pinned(commit) and pc != commit:
            raise SourceError(f"archive commit {pc} != pinned commit {commit}")
        evidence["archive_commit"] = pc
    return evidence


def _git(args: list[str], cwd: Path | None, timeout: float) -> str:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        p = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RetryableError(f"git timed out after {timeout:g}s") from e
    except FileNotFoundError as e:
        raise SourceError("git is not installed") from e
    if p.returncode != 0:
        raise RetryableError(f"git {args[0]} failed: {p.stderr.strip()[:300]}")
    return p.stdout.strip()


def fetch_git(src: dict, name: str, version: str, commit: str | None, work: Path,
              timeout: float, retries: int, backoff: float) -> tuple[Path, dict]:
    if not is_pinned(commit):
        raise UnpinnedError("component has no pinned 'commit'")
    _check_scheme(src["url"])
    repo = work / "repo"
    last: SourceError | None = None
    for attempt in range(1, retries + 1):
        shutil.rmtree(repo, ignore_errors=True)
        try:
            _git(["clone", "--quiet", "--depth", "1", "--branch", src["ref"], src["url"], str(repo)],
                 None, timeout * 4)
            head = _git(["rev-parse", "HEAD^{commit}"], repo, timeout)
            break
        except RetryableError as e:
            last = e
            if attempt < retries:
                time.sleep(backoff * (2 ** (attempt - 1)))
    else:
        raise SourceError(f"{last} (after {retries} attempts)")
    if head != commit:
        raise SourceError(f"git commit mismatch (expected {commit}, got {head})")
    out = work / f"{name}-{version}.tar"
    _git(["archive", "--format=tar", f"--prefix={name}-{version}/", "-o", str(out), "HEAD"], repo, timeout * 4)
    return out, {"git_commit": head}


# ---------------------------------------------------------------- one source

def try_source(name: str, comp: dict, src: dict, out_dir: Path, cache_dir: Path | None,
               timeout: float, retries: int, backoff: float, use_cache: bool = True) -> dict:
    """Return {'archive': Path, 'evidence': dict, 'cached': bool}; raise SourceError."""
    version, commit = comp["version"], comp.get("commit")
    out_dir.mkdir(parents=True, exist_ok=True)
    if src["type"] == "git":
        with tempfile.TemporaryDirectory(prefix="viddown-git-") as tmp:
            archive, ev = fetch_git(src, name, version, commit, Path(tmp), timeout, retries, backoff)
            final = out_dir / archive.name
            shutil.copy2(archive, final)
        return {"archive": final, "evidence": ev, "cached": False}

    filename = Path(urllib.parse.urlparse(src["url"]).path).name or f"{name}-{version}.tar"
    cached = cache_dir / f"{name}-{version}" / src["name"] / filename if cache_dir else None
    final = out_dir / filename

    if use_cache and cached is not None and cached.is_file():
        try:  # a cache hit is re-verified against the pinned hash
            ev = verify_tarball(cached, src, commit)
            shutil.copy2(cached, final)
            return {"archive": final, "evidence": ev, "cached": True}
        except UnpinnedError:
            raise
        except SourceError as e:
            log(f"    cached copy rejected ({e}); removing it")
            cached.unlink(missing_ok=True)

    # Fail early on an unpinned source instead of downloading something we could never accept.
    if not (is_pinned(src.get("sha256")) or is_pinned(src.get("sha256_uncompressed"))):
        raise UnpinnedError("no sha256 pin in manifest for this source")

    with tempfile.TemporaryDirectory(prefix="viddown-dl-") as tmp:
        tmp_file = Path(tmp) / filename
        download(src["url"], tmp_file, timeout, retries, backoff)
        ev = verify_tarball(tmp_file, src, commit)  # raises on mismatch: file is discarded
        shutil.copy2(tmp_file, final)
        if cached is not None:
            cached.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(tmp_file, cached)
    return {"archive": final, "evidence": ev, "cached": False}


# ---------------------------------------------------------------- component

def fetch_component(manifest: dict, name: str, out_dir: Path, cache_dir: Path | None,
                    timeout: float = 30, retries: int = 3, backoff: float = 2) -> dict:
    comps = manifest["components"]
    if name not in comps:
        raise ManifestError(f"unknown component {name!r}; known: {', '.join(sorted(comps))}")
    comp = comps[name]
    sources = comp["sources"]
    dest = out_dir / f"{name}-{comp['version']}"
    attempts: list[dict] = []
    for i, src in enumerate(sources, 1):
        rec = {"index": i, "total": len(sources), "name": src["name"], "url": src["url"], "type": src["type"]}
        log(f"[{name}] source {i}/{len(sources)}: {src['name']} ({src['url']})")
        try:
            res = try_source(name, comp, src, dest, cache_dir, timeout, retries, backoff)
        except UnpinnedError as e:
            rec.update(status="skipped (unpinned)", reason=str(e))
            attempts.append(rec)
            log(f"    SKIPPED: {e}")
            continue
        except SourceError as e:
            rec.update(status="failed", reason=str(e))
            attempts.append(rec)
            log(f"    FAILED: {e}")
            continue
        rec.update(status="ok", reason="cache hit, re-verified" if res["cached"] else "downloaded and verified")
        attempts.append(rec)
        archive: Path = res["archive"]
        prov = {
            "component": name, "version": comp["version"], "license": comp.get("license", ""),
            "commit": comp.get("commit", ""),
            "source_used": {"index": i, "name": src["name"], "type": src["type"], "url": src["url"]},
            "evidence": res["evidence"], "archive": archive.name, "archive_sha256": sha256_file(archive),
            "attempts": attempts, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        (dest / "provenance.json").write_text(json.dumps(prov, indent=2) + "\n")
        log(f"[{name}] OK via source {i} '{src['name']}' -> {archive}")
        return prov
    raise AllSourcesFailed(name, comp["version"], attempts)


# ---------------------------------------------------------------- health check

def check_all(manifest: dict, cache_dir: Path | None, timeout: float, retries: int, backoff: float,
              only: list[str] | None = None) -> dict:
    """Verify EVERY source of every component independently (never uses the cache)."""
    report: dict = {}
    for name, comp in manifest["components"].items():
        if only and name not in only:
            continue
        rows = []
        for src in comp["sources"]:
            with tempfile.TemporaryDirectory(prefix="viddown-check-") as tmp:
                try:
                    try_source(name, comp, src, Path(tmp), cache_dir, timeout, retries, backoff, use_cache=False)
                    rows.append({"source": src["name"], "url": src["url"], "status": "ok", "reason": ""})
                except UnpinnedError as e:
                    rows.append({"source": src["name"], "url": src["url"], "status": "unpinned", "reason": str(e)})
                except SourceError as e:
                    rows.append({"source": src["name"], "url": src["url"], "status": "broken", "reason": str(e)})
        report[name] = {"version": comp["version"], "sources": rows}
    return report


def render_report_md(report: dict) -> str:
    out = ["# Source health report", "", f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}", ""]
    for name, r in report.items():
        out += [f"## {name} {r['version']}", "", "| # | source | status | detail |", "|---|---|---|---|"]
        for i, s in enumerate(r["sources"], 1):
            out.append(f"| {i} | {s['source']} | {s['status']} | {s['reason'].replace('|', '/')} |")
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", default=str(Path(__file__).resolve().parent.parent / "native" / "sources.lock.toml"))
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--backoff", type=float, default=2)
    ap.add_argument("--cache-dir", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="fetch + verify one component")
    f.add_argument("component")
    f.add_argument("--out", default="build/sources")
    fa = sub.add_parser("fetch-all", help="fetch + verify all (or --only) components")
    fa.add_argument("--out", default="build/sources")
    fa.add_argument("--only", default="")
    c = sub.add_parser("check", help="source health: verify every source of every component")
    c.add_argument("--only", default="")
    c.add_argument("--report-md", default=None)
    c.add_argument("--report-json", default=None)
    c.add_argument("--strict", action="store_true", help="fail if ANY source is broken or unpinned")
    sub.add_parser("list", help="list components and their ordered sources")
    a = ap.parse_args(argv)

    try:
        manifest = load_manifest(Path(a.manifest))
    except ManifestError as e:
        log(f"ERROR: {e}")
        return 1
    cache = Path(a.cache_dir) if a.cache_dir else None

    if a.cmd == "list":
        for n, comp in manifest["components"].items():
            print(f"{n} {comp['version']}  ({comp.get('license', '?')})")
            for i, s in enumerate(comp["sources"], 1):
                pinned = any(is_pinned(s.get(k)) for k in ("sha256", "sha256_uncompressed")) or \
                    (s["type"] == "git" and is_pinned(comp.get("commit")))
                print(f"  {i}. {s['name']:<22} {s['type']:<8} {'pinned' if pinned else 'UNPINNED':<9} {s['url']}")
        return 0

    if a.cmd == "check":
        only = [x for x in a.only.split(",") if x]
        report = check_all(manifest, cache, a.timeout, a.retries, a.backoff, only or None)
        md = render_report_md(report)
        print(md)
        if a.report_md:
            Path(a.report_md).write_text(md)
        if a.report_json:
            Path(a.report_json).write_text(json.dumps(report, indent=2) + "\n")
        dead = [n for n, r in report.items() if not any(s["status"] == "ok" for s in r["sources"])]
        if dead:
            log(f"ERROR: no working source for: {', '.join(dead)}")
            return 2
        if a.strict and any(s["status"] != "ok" for r in report.values() for s in r["sources"]):
            log("ERROR: --strict and at least one source is broken or unpinned")
            return 2
        return 0

    names = [a.component] if a.cmd == "fetch" else \
        ([x for x in a.only.split(",") if x] or list(manifest["components"]))
    rc = 0
    for n in names:
        try:
            fetch_component(manifest, n, Path(a.out), cache, a.timeout, a.retries, a.backoff)
        except AllSourcesFailed as e:
            log("ERROR: " + e.render())
            rc = 2
        except ManifestError as e:
            log(f"ERROR: {e}")
            return 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
