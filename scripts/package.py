#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Package one ABI's build output into a versioned zip with provenance.

usage: package.py <abi> [--dist dist] [--sources build/sources] [--build build] [--version-tag TAG]

Layout of the zip (consumed by the VidDown app repo):
  jniLibs/<abi>/*.so            executables + shared libs (extractNativeLibs=true in the app)
  assets/*.zip, assets/*.txt    Python stdlib for this ABI + yt-dlp site-packages
  BUILD_INFO.json               every component: version, source ACTUALLY used, verified hashes
  THIRD_PARTY_NOTICES.md        generated from the lock file + provenance
  licenses/<component>/...      licence files taken from the verified source archives
  SHA256SUMS                    hashes of every file in the zip
"""
import argparse
import hashlib
import json
import re
import subprocess
import tarfile
import tomllib
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIC_RX = re.compile(r"^(LICEN[SC]E|COPYING|NOTICE|AUTHORS)([-._].*)?$", re.I)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("abi")
    ap.add_argument("--dist", default=str(ROOT / "dist"))
    ap.add_argument("--sources", default=str(ROOT / "build" / "sources"))
    ap.add_argument("--build", default=str(ROOT / "build"))
    ap.add_argument("--version-tag", default="dev")
    a = ap.parse_args()
    dist, src = Path(a.dist), Path(a.sources)
    comps = tomllib.loads((ROOT / "native" / "sources.lock.toml").read_text())["components"]

    files: dict[str, bytes] = {}
    for part in (dist / a.abi, dist / "shared"):
        if not part.is_dir():
            raise SystemExit(f"missing build output: {part}")
        for f in sorted(part.rglob("*")):
            if f.is_file():
                files[str(f.relative_to(part))] = f.read_bytes()

    # components actually used in this build = those with a provenance.json
    used, notices = {}, ["# Third-party notices", "",
                         "Generated from native/sources.lock.toml and the provenance of this build.", "",
                         "| component | version | licence | source actually used | archive sha256 |", "|---|---|---|---|---|"]
    for name, e in comps.items():
        prov = src / f"{name}-{e['version']}" / "provenance.json"
        if not prov.is_file():
            continue
        p = json.loads(prov.read_text())
        used[name] = {k: p[k] for k in ("version", "license", "commit", "source_used", "evidence", "archive", "archive_sha256", "attempts")}
        notices.append(f"| {name} | {e['version']} | {e['license']} | {p['source_used']['name']} ({p['source_used']['url']}) | `{p['archive_sha256']}` |")
        with tarfile.open(src / f"{name}-{e['version']}" / p["archive"]) as t:
            for m in t:
                parts = Path(m.name).parts
                if m.isfile() and len(parts) == 2 and LIC_RX.match(parts[1]) and m.size < 512_000:
                    files[f"licenses/{name}/{parts[1]}"] = t.extractfile(m).read()
    files["THIRD_PARTY_NOTICES.md"] = ("\n".join(notices) + "\n").encode()
    files["LICENSE"] = (ROOT / "LICENSE").read_bytes() if (ROOT / "LICENSE").exists() else b""

    ndk = None
    env_sh = Path(a.build) / "work" / a.abi / "cpython" / "Android" / "android-env.sh"
    if not env_sh.exists():
        env_sh = Path(a.build) / "work" / a.abi / "cpython-env" / "Android" / "android-env.sh"
    if env_sh.exists():
        m = re.search(r"^ndk_version=(\S+)", env_sh.read_text(), re.M)
        ndk = m.group(1) if m else None

    info = {
        "bundle": "viddown-native-bundle", "version_tag": a.version_tag, "abi": a.abi,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo_commit": git_sha(), "android_ndk": ndk, "android_api_level": 24,
        "components": used,
        "unverified_notes": [
            "Built by CI; an ABI is only claimed as verified in README once its emulator smoke test passed.",
        ],
    }
    files["BUILD_INFO.json"] = (json.dumps(info, indent=2) + "\n").encode()
    files["SHA256SUMS"] = "".join(f"{sha(b)}  {n}\n" for n, b in sorted(files.items())).encode()

    out = dist / "release"
    out.mkdir(parents=True, exist_ok=True)
    zpath = out / f"viddown-native-{a.abi}-{a.version_tag}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in sorted(files.items()):
            zi = zipfile.ZipInfo(n, date_time=(1980, 1, 1, 0, 0, 0))  # reproducible
            zi.external_attr = (0o755 if n.startswith("jniLibs/") else 0o644) << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, b)
    (out / (zpath.name + ".sha256")).write_text(f"{sha(zpath.read_bytes())}  {zpath.name}\n")
    print(f"wrote {zpath} ({zpath.stat().st_size} bytes), {len(files)} files, {len(used)} components with provenance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
