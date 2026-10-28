#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Discover new upstream versions and PROPOSE changes to native/sources.lock.toml.

* `discover`  : show current vs. newest version per tracked component.
* `pin`       : resolve source URLs from native/source-templates.toml, download them
                from the first reachable official source, compute SHA-256 pins and
                print a unified diff of the lock file. Nothing is written without
                --write, and nothing is ever merged automatically: a human reviews the
                pull request (see .github/workflows/update-sources.yml).

Trust model: a pin computed here is trust-on-first-use of the upstream host. It is
cross-checked, where the upstream offers it, against (a) the commit of the release tag
(GitHub archives embed it), (b) a checksum file published next to the release, (c) the
PyPI JSON digest. An existing pin is never silently changed for the same version.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_source as fs  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TEMPLATES = ROOT / "native" / "source-templates.toml"
DEFAULT_LOCK = ROOT / "native" / "sources.lock.toml"
FIRST = ["cpython", "yt-dlp"]  # other components may follow versions read from these


class UpdateError(Exception):
    pass


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def vkey(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def fmt(s: str, version: str, tag: str) -> str:
    return s.replace("{version}", version).replace("{tag}", tag)


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# ------------------------------------------------------------------ git helpers

def ls_remote_tags(repo: str, timeout: float = 90) -> dict[str, str]:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        p = subprocess.run(["git", "ls-remote", "--tags", repo], capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        raise UpdateError(f"git ls-remote {repo} timed out") from e
    if p.returncode != 0:
        raise UpdateError(f"git ls-remote {repo} failed: {p.stderr.strip()[:200]}")
    tags: dict[str, str] = {}
    for line in p.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        ref = ref.removeprefix("refs/tags/")
        if ref.endswith("^{}"):
            tags[ref[:-3]] = sha  # peeled commit wins over the tag object
        else:
            tags.setdefault(ref, sha)
    return tags


class Ctx:
    def __init__(self, tmp: Path, timeout: float, retries: int, backoff: float):
        self.tmp, self.timeout, self.retries, self.backoff = tmp, timeout, retries, backoff
        self.files: dict[str, Path] = {}
        self.tags: dict[str, dict[str, str]] = {}
        self.archives: dict[str, Path] = {}  # component -> first downloaded archive

    def tags_for(self, repo: str) -> dict[str, str]:
        if repo not in self.tags:
            self.tags[repo] = ls_remote_tags(repo)
        return self.tags[repo]

    def download(self, url: str) -> Path:
        if url not in self.files:
            dest = self.tmp / f"dl{len(self.files)}-{Path(url.split('?')[0]).name or 'file'}"
            fs.download(url, dest, self.timeout, self.retries, self.backoff)
            self.files[url] = dest
        return self.files[url]

    def get_json(self, url: str) -> dict:
        return json.loads(self.download(url).read_text())


# ------------------------------------------------------------------ discovery / follow

def discover_latest(name: str, t: dict, ctx: Ctx) -> str:
    tr = t.get("track")
    if not tr:
        raise UpdateError(f"{name}: no 'track' rule")
    if tr["kind"] != "git-tags":
        raise UpdateError(f"{name}: unknown track kind {tr['kind']!r}")
    rx = re.compile(tr["pattern"])
    versions = [m.group(1) for tag in ctx.tags_for(tr["repo"]) if (m := rx.match(tag))]
    if not versions:
        raise UpdateError(f"{name}: no tag in {tr['repo']} matches {tr['pattern']}")
    return max(versions, key=vkey)


def _read_member(archive: Path, suffix: str) -> str:
    with tarfile.open(archive) as t:
        for m in t:
            if m.isfile() and m.name.endswith(suffix):
                f = t.extractfile(m)
                if f:
                    return f.read().decode("utf-8", "replace")
    raise UpdateError(f"{suffix} not found in {archive.name}")


def ensure_archive(ctx: Ctx, comp: str, entry: dict | None) -> Path:
    """Return a downloaded, PIN-VERIFIED archive of `comp`.

    When every source of a component is already pinned, build_source() keeps the pins and
    downloads nothing. Following another component's metadata still needs the archive, so
    fetch it now and verify it against the pins in the lock; a mismatch aborts the update.
    """
    if comp in ctx.archives:
        return ctx.archives[comp]
    if not entry:
        raise UpdateError(f"{comp}: no lock entry to fetch an archive from")
    problems = []
    for src in entry["sources"]:
        if src["type"] != "tarball":
            continue
        try:
            path = ctx.download(src["url"])
            fs.verify_tarball(path, src, entry.get("commit"))
        except fs.UnpinnedError:
            problems.append(f"{src['name']}: unpinned")
            continue
        except fs.SourceError as e:
            problems.append(f"{src['name']}: {e}")
            continue
        ctx.archives[comp] = path
        return path
    raise UpdateError(f"{comp}: could not obtain a verified archive ({'; '.join(problems) or 'no tarball source'})")


def follow_ytdlp_pin(ctx: Ctx, cpython_version: str, entry: dict | None) -> dict[str, str]:
    from packaging.markers import default_environment
    from packaging.requirements import Requirement

    arch = ensure_archive(ctx, "yt-dlp", entry)
    data = tomllib.loads(_read_member(arch, "pyproject.toml"))
    pins = data["project"]["optional-dependencies"]["pin"]
    env = default_environment()
    env.update(implementation_name="cpython", sys_platform="android", platform_system="Linux",
               python_full_version=cpython_version, python_version=".".join(cpython_version.split(".")[:2]))
    out: dict[str, str] = {}
    for line in pins:
        r = Requirement(line)
        if r.marker is not None and not r.marker.evaluate(env):
            continue
        for spec in r.specifier:
            if spec.operator == "==":
                out[norm(r.name)] = spec.version
    return out


def follow_cpython_deps(ctx: Ctx, entry: dict | None) -> dict[str, str]:
    arch = ensure_archive(ctx, "cpython", entry)
    text = _read_member(arch, "Android/android.py")
    block = text[text.index("def unpack_deps"):]
    block = block[:block.index("def download")]
    return {m.group(1): m.group(2) for m in re.finditer(r'"([a-z0-9]+)-(\d[\d.]*)-\d+"', block)}


# ------------------------------------------------------------------ pinning

def checksum_from(text: str, filename: str) -> str | None:
    for line in text.splitlines():
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(\S.*?)\s*$", line)
        if m and Path(m.group(2)).name == filename:
            return m.group(1).lower()
    # a file that contains ONLY a hash (e.g. "<file>.sha256"); never accept a hash that
    # is listed next to a different file name
    m = re.fullmatch(r"\s*([0-9a-fA-F]{64})\s*", text)
    return m.group(1).lower() if m else None


def build_source(name: str, comp: str, s: dict, version: str, tag: str, commit: str,
                 old_src: dict | None, ctx: Ctx, repin: bool, notes: list[str]) -> dict | None:
    typ = s["type"]
    if typ == "git":
        return {"name": s["name"], "type": "git", "url": fmt(s["url"], version, tag), "ref": fmt(s["ref"], version, tag)}

    pins = s.get("pin", ["sha256"])
    if typ == "pypi-sdist":
        meta_url = f"https://pypi.org/pypi/{s['project']}/{version}/json"
        try:
            meta = ctx.get_json(meta_url)
        except fs.SourceError as e:
            notes.append(f"{comp}/{s['name']}: PyPI metadata unreachable ({e}); source omitted this run")
            return None
        sd = next((f for f in meta["urls"] if f["packagetype"] == "sdist"), None)
        if not sd:
            notes.append(f"{comp}/{s['name']}: PyPI has no sdist for {s['project']} {version}; source omitted")
            return None
        url, upstream_digest = sd["url"], sd["digests"]["sha256"]
    else:
        url, upstream_digest = fmt(s["url"], version, tag), None

    entry = {"name": s["name"], "type": "tarball", "url": url}
    if old_src and old_src.get("url") == url and not repin and all(fs.is_pinned(old_src.get(p)) for p in pins):
        for p in pins:
            entry[p] = old_src[p]
        return entry  # existing pins are kept as they are

    try:
        path = ctx.download(url)
    except fs.SourceError as e:
        for p in pins:
            entry[p] = "UNPINNED"
        notes.append(f"{comp}/{s['name']}: not reachable from here ({e}); left UNPINNED - run the 'update-sources' "
                     "workflow from GitHub Actions to fill it")
        return entry

    ctx.archives.setdefault(comp, path)
    if "sha256" in pins:
        entry["sha256"] = fs.sha256_file(path)
    if "sha256_uncompressed" in pins:
        entry["sha256_uncompressed"] = fs.sha256_uncompressed(path)
    if upstream_digest and fs.sha256_file(path) != upstream_digest:
        raise UpdateError(f"{comp}/{s['name']}: downloaded file does not match the digest published by PyPI")
    pc = fs.archive_commit(path)
    if pc and fs.is_pinned(commit) and pc != commit:
        raise UpdateError(f"{comp}/{s['name']}: archive commit {pc} != tag commit {commit}")
    vw = s.get("verify_with")
    if vw:
        try:
            text = ctx.download(fmt(vw["url"], version, tag)).read_text(errors="replace")
            want = checksum_from(text, fmt(vw["file"], version, tag))
            if want and want != fs.sha256_file(path):
                raise UpdateError(f"{comp}/{s['name']}: file does not match the checksum published by the project")
            if not want:
                notes.append(f"{comp}/{s['name']}: published checksum file had no entry for the archive")
        except fs.SourceError as e:
            notes.append(f"{comp}/{s['name']}: published checksum file unreachable ({e}); cross-check skipped")
    return entry


def build_component(name: str, t: dict, version: str, old: dict | None, ctx: Ctx, repin: bool,
                    notes: list[str]) -> dict:
    tag = fmt(t["tag"], version, "")
    commit = "UNPINNED"
    try:
        commit = ctx.tags_for(t["commit_repo"]).get(tag, "UNPINNED")
    except UpdateError as e:
        notes.append(f"{name}: cannot resolve tag commit ({e})")
    if commit == "UNPINNED":
        notes.append(f"{name}: tag {tag} not found / not resolvable; commit left UNPINNED")
    same = old and old.get("version") == version
    if same and fs.is_pinned(old.get("commit")) and fs.is_pinned(commit) and old["commit"] != commit:
        raise UpdateError(f"{name}: tag {tag} now points to {commit} but the lock pins {old['commit']} - "
                          "the upstream tag MOVED. Investigate before changing anything.")
    if same and fs.is_pinned(old.get("commit")) and not fs.is_pinned(commit):
        commit = old["commit"]
    entry = {"kind": t["kind"], "version": version, "license": t["license"], "commit": commit}
    if "pypi" in t:
        entry["pypi"] = t["pypi"]
    sources = []
    for s in t["sources"]:
        old_src = next((o for o in (old or {}).get("sources", []) if o["name"] == s["name"]), None) if same else None
        built = build_source(name, name, s, version, tag, commit, old_src, ctx, repin, notes)
        if built:
            sources.append(built)
    if not sources:
        raise UpdateError(f"{name}: no usable source")
    entry["sources"] = sources
    return entry


# ------------------------------------------------------------------ lock file output

def emit_lock(components: dict) -> str:
    q = json.dumps
    out = [
        "# GENERATED by scripts/update_source.py - review the diff, never hand-edit a hash.",
        "# sha256              = hash of the downloaded file",
        "# sha256_uncompressed = hash of the decompressed tar stream (survives re-compression by a host)",
        "# commit              = git commit of the release tag (also checked inside GitHub archives)",
        "# 'UNPINNED' sources are skipped by scripts/fetch_source.py until they are pinned.",
        "",
        "[meta]",
        "schema = 1",
        "",
    ]
    for name, e in components.items():
        out.append(f"[components.{name}]")
        for k in ("kind", "version", "license", "commit", "pypi"):
            if k in e:
                out.append(f"{k} = {q(e[k])}")
        for s in e["sources"]:
            out += ["", f"[[components.{name}.sources]]"]
            for k in ("name", "type", "url", "ref", "sha256", "sha256_uncompressed"):
                if k in s:
                    out.append(f"{k} = {q(s[k])}")
        out.append("")
    return "\n".join(out)


# ------------------------------------------------------------------ main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--templates", default=str(DEFAULT_TEMPLATES))
    ap.add_argument("--lock", default=str(DEFAULT_LOCK))
    ap.add_argument("--timeout", type=float, default=60)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--backoff", type=float, default=2)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover")
    d.add_argument("--json", action="store_true")
    p = sub.add_parser("pin")
    p.add_argument("--component", action="append", default=[], help="limit to these (repeatable)")
    p.add_argument("--latest", action="store_true", help="move tracked components to the newest upstream version")
    p.add_argument("--version", help="explicit version (needs exactly one --component)")
    p.add_argument("--repin", action="store_true", help="recompute pins even for an unchanged version (review carefully!)")
    p.add_argument("--write", action="store_true", help="write the lock file (otherwise: print the diff only)")
    p.add_argument("--summary-md", default=None, help="write a markdown summary (used as PR body)")
    a = ap.parse_args(argv)

    templates = tomllib.loads(Path(a.templates).read_text())["components"]
    lock_path = Path(a.lock)
    old_lock = tomllib.loads(lock_path.read_text()).get("components", {}) if lock_path.exists() else {}
    notes: list[str] = []

    with tempfile.TemporaryDirectory(prefix="viddown-update-") as tmp:
        ctx = Ctx(Path(tmp), a.timeout, a.retries, a.backoff)

        if a.cmd == "discover":
            rows = []
            for name, t in templates.items():
                if "track" not in t:
                    continue
                try:
                    latest = discover_latest(name, t, ctx)
                except UpdateError as e:
                    latest = f"ERROR: {e}"
                cur = old_lock.get(name, {}).get("version", "-")
                rows.append({"component": name, "current": cur, "latest": latest, "update": cur != latest and not latest.startswith("ERROR")})
            if a.json:
                print(json.dumps(rows, indent=2))
            else:
                for r in rows:
                    print(f"{r['component']:<10} current={r['current']:<14} latest={r['latest']}{'   <-- update available' if r['update'] else ''}")
            return 0

        order = [n for n in FIRST if n in templates] + [n for n in templates if n not in FIRST]
        wanted = set(a.component) or set(templates)
        for n in list(wanted):  # a follower needs the archive it follows
            f = templates.get(n, {}).get("follow")
            wanted |= {"yt-dlp-pin": {"yt-dlp", "cpython"}, "cpython-android-deps": {"cpython"}}.get(f, set())
        if a.version and len(a.component) != 1:
            log("ERROR: --version needs exactly one --component")
            return 1
        new = dict(old_lock)
        followed: dict[str, dict[str, str]] = {}
        try:
            for name in order:
                t = templates[name]
                follow = t.get("follow")
                if name not in wanted:
                    continue
                if a.version:
                    version = a.version
                elif follow:
                    if follow not in followed:
                        cpv = new.get("cpython", {}).get("version", "3.14.0")
                        followed[follow] = (follow_ytdlp_pin(ctx, cpv, new.get("yt-dlp")) if follow == "yt-dlp-pin"
                                            else follow_cpython_deps(ctx, new.get("cpython")))
                    key = norm(t.get("pypi", name))
                    version = followed[follow].get(key)
                    if not version:
                        raise UpdateError(f"{name}: '{follow}' gives no version for {key}")
                elif a.latest:
                    version = discover_latest(name, t, ctx)
                elif name in old_lock:
                    version = old_lock[name]["version"]
                else:
                    raise UpdateError(f"{name}: not in the lock yet; run with --latest")
                log(f"== {name} -> {version}")
                new[name] = build_component(name, t, version, old_lock.get(name), ctx, a.repin, notes)
        except (UpdateError, fs.SourceError) as e:
            log(f"ERROR: {e}")
            return 2

    ordered = {n: new[n] for n in templates if n in new}
    text = emit_lock(ordered)
    old_text = lock_path.read_text() if lock_path.exists() else ""
    diff = "".join(difflib.unified_diff(old_text.splitlines(True), text.splitlines(True), "sources.lock.toml (current)", "sources.lock.toml (proposed)"))
    print(diff or "# no changes to the lock file")
    if notes:
        print("\n# NOTES", file=sys.stderr)
        for n in notes:
            print(f"- {n}", file=sys.stderr)
    if a.summary_md:
        lines = ["## Proposed source updates", "", "Generated by `scripts/update_source.py`. **Review before merging**; the new "
                 "version is only official once this PR is merged.", ""]
        for n, e in ordered.items():
            oldv = old_lock.get(n, {}).get("version")
            if oldv != e["version"]:
                lines.append(f"- `{n}`: {oldv or 'new'} -> **{e['version']}**")
        if notes:
            lines += ["", "### Notes", *[f"- {n}" for n in notes]]
        Path(a.summary_md).write_text("\n".join(lines) + "\n")
    if a.write:
        lock_path.write_text(text)
        log(f"wrote {lock_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
