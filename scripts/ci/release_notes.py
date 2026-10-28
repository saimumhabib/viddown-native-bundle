#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Write the markdown notes of an automatic release.

usage: release_notes.py --lock native/sources.lock.toml --tag TAG --assets DIR
                        [--pr N] [--run-url URL] [--repo owner/name]

Only states what is true: x86_64 is the one ABI whose binaries are executed in CI (emulator smoke test);
every other ABI is "built, not run-tested". ABIs are read from the zip names found in --assets.
"""
import argparse
import re
import tomllib
from pathlib import Path

RUN_TESTED = {"x86_64": "emulator smoke test on Android API 24 and 34 passed"}
ABI_RX = re.compile(r"^viddown-native-(arm64-v8a|armeabi-v7a|x86_64|x86)-.+\.zip$")


def build_notes(lock: dict, tag: str, abis: list[str], pr: str | None, run_url: str | None, repo: str | None) -> str:
    out = [f"# {tag}", "",
           "Native bundle for the VidDown yt-dlp engine, **built from source** in GitHub Actions "
           "(CPython, OpenSSL, libffi, FFmpeg/FFprobe, yt-dlp and its pure-Python dependencies). "
           "No prebuilt third-party binaries.", ""]
    src = []
    if pr:
        src.append(f"update PR #{pr}" + (f" (https://github.com/{repo}/pull/{pr})" if repo else ""))
    if run_url:
        src.append(f"build run {run_url}")
    if src:
        out += ["Produced by: " + ", ".join(src) + ".", ""]
    out += ["## Components", "", "| component | version | licence |", "|---|---|---|"]
    for name, c in lock["components"].items():
        out.append(f"| {name} | {c['version']} | {c['license']} |")
    out += ["", "## What was verified", ""]
    for abi in sorted(abis):
        if abi in RUN_TESTED:
            out.append(f"- **{abi}**: built, and executed in CI: {RUN_TESTED[abi]}.")
        else:
            out.append(f"- **{abi}**: built from source, **not run-tested in CI** "
                       "(the CI emulator is x86_64; this ABI needs a run on a device).")
    out += ["", "## Verify your download", "",
            "- `SHA256SUMS.txt` lists the hash of every zip.",
            "- Each zip contains `BUILD_INFO.json`: for every component the source that was ACTUALLY used and its "
            "verified hashes, plus `THIRD_PARTY_NOTICES.md` and the licence texts.", ""]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lock", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--assets", required=True)
    ap.add_argument("--pr")
    ap.add_argument("--run-url")
    ap.add_argument("--repo")
    a = ap.parse_args()
    lock = tomllib.loads(Path(a.lock).read_text())
    abis = [m.group(1) for f in sorted(Path(a.assets).glob("viddown-native-*.zip")) if (m := ABI_RX.match(f.name))]
    print(build_notes(lock, a.tag, abis, a.pr, a.run_url, a.repo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
