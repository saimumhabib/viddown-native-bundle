#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Safety policy for an automated change to native/sources.lock.toml.

usage: check_lock_update.py <old-lock.toml> <new-lock.toml> [--templates native/source-templates.toml]

Exit 0 = the change is the kind a machine may merge on its own.
Exit 1 = a human must look (every reason is printed, one per line).

Rules
  * no component and no source may disappear; no NEW component (needs a human)
  * a version never goes down; licences never change silently
  * a tracked component stays inside its series (e.g. CPython 3.14.x, FFmpeg 8.1.x)
  * SAME version: nothing that was already pinned may change (url, sha256, commit).
    Only UNPINNED -> pinned is allowed. A moved tag or replaced hash is an alarm.
  * NEW version: the release tag commit must be pinned and at least TWO sources must be
    verifiable (so the fallback chain is real, not decorative).
"""
import argparse
import re
import sys
import tomllib
from pathlib import Path

PIN_FIELDS = ("sha256", "sha256_uncompressed")
UNPINNED = {"", "UNPINNED", "PIN_REQUIRED"}
DEFAULT_TEMPLATES = Path(__file__).resolve().parent.parent / "native" / "source-templates.toml"


def vkey(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def pinned(v) -> bool:
    return isinstance(v, str) and v.strip() not in UNPINNED


def usable_sources(comp: dict) -> int:
    n = 0
    for s in comp["sources"]:
        if s["type"] == "git":
            n += pinned(comp.get("commit"))
        else:
            n += any(pinned(s.get(f)) for f in PIN_FIELDS)
    return n


def check(old: dict, new: dict, templates: dict) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    changes: list[str] = []
    for name in old:
        if name not in new:
            problems.append(f"{name}: component was removed")
    for name, n in new.items():
        if name not in old:
            problems.append(f"{name}: NEW component (a human must review a new dependency)")
            continue
        o = old[name]
        if o.get("license") != n.get("license"):
            problems.append(f"{name}: licence changed ({o.get('license')!r} -> {n.get('license')!r})")
        ov, nv = o["version"], n["version"]
        if vkey(nv) < vkey(ov):
            problems.append(f"{name}: version went DOWN ({ov} -> {nv})")
        if ov != nv:
            changes.append(f"{name}: {ov} -> {nv}")
            t = templates.get(name, {})
            tr = t.get("track")
            if tr:
                tag = t["tag"].replace("{version}", nv)
                if not re.match(tr["pattern"], tag):
                    problems.append(f"{name}: {nv} is outside the tracked series ({tr['pattern']})")
            if not pinned(n.get("commit")):
                problems.append(f"{name}: new version has no pinned tag commit")
            if usable_sources(n) < 2:
                problems.append(f"{name}: new version has fewer than 2 verifiable sources ({usable_sources(n)})")
            continue
        # same version: pinned facts must not move
        if pinned(o.get("commit")) and o.get("commit") != n.get("commit"):
            problems.append(f"{name}: tag commit CHANGED for the same version {ov} ({o['commit']} -> {n.get('commit')})")
        nsrc = {s["name"]: s for s in n["sources"]}
        for s in o["sources"]:
            ns = nsrc.get(s["name"])
            if ns is None:
                problems.append(f"{name}/{s['name']}: source was removed")
                continue
            if ns["url"] != s["url"]:
                problems.append(f"{name}/{s['name']}: url changed for the same version")
            for f in PIN_FIELDS:
                if pinned(s.get(f)) and s.get(f) != ns.get(f):
                    problems.append(f"{name}/{s['name']}: {f} CHANGED for the same version {ov}")
            if s["type"] == "git" and s.get("ref") != ns.get("ref"):
                problems.append(f"{name}/{s['name']}: git ref changed for the same version")
        for s in n["sources"]:
            old_s = next((x for x in o["sources"] if x["name"] == s["name"]), None)
            if old_s is None:
                changes.append(f"{name}: added source {s['name']}")
            else:
                filled = [f for f in PIN_FIELDS if not pinned(old_s.get(f)) and pinned(s.get(f))]
                if filled:
                    changes.append(f"{name}/{s['name']}: pinned {', '.join(filled)}")
    return problems, changes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--templates", default=str(DEFAULT_TEMPLATES))
    a = ap.parse_args()
    old = tomllib.loads(Path(a.old).read_text()).get("components", {})
    new = tomllib.loads(Path(a.new).read_text()).get("components", {})
    templates = tomllib.loads(Path(a.templates).read_text()).get("components", {})
    problems, changes = check(old, new, templates)
    print("Changes:" if changes else "Changes: none")
    for c in changes:
        print(f"- {c}")
    if problems:
        print("\nA human must review this update:")
        for p in problems:
            print(f"- {p}")
        return 1
    print("\nNo policy violations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
