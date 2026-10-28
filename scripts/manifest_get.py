#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tiny helper for shell scripts: read values from native/sources.lock.toml.

  manifest_get.py version <component>
  manifest_get.py archive <component> <sources-dir>   (path of the verified archive)
  manifest_get.py components <kind>[,<kind>...]       (component names of those kinds)
"""
import json
import sys
import tomllib
from pathlib import Path

LOCK = Path(__file__).resolve().parent.parent / "native" / "sources.lock.toml"


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 1
    comps = tomllib.loads(LOCK.read_text())["components"]
    cmd, name = sys.argv[1], sys.argv[2]
    if cmd == "components":
        kinds = set(name.split(","))
        print(" ".join(n for n, e in comps.items() if e["kind"] in kinds))
        return 0
    if name not in comps:
        print(f"unknown component {name}", file=sys.stderr)
        return 1
    ver = comps[name]["version"]
    if cmd == "version":
        print(ver)
    elif cmd == "archive":
        d = Path(sys.argv[3]) / f"{name}-{ver}"
        prov = json.loads((d / "provenance.json").read_text())
        print(d / prov["archive"])
    else:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
