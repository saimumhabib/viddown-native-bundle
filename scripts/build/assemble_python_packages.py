#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Assemble yt-dlp and its PURE-Python dependencies into one site-packages directory.

We copy the package directories out of the hash-verified source archives. We do NOT run
`pip install` because that would pull unpinned build backends from the network. Every
package assembled here is pure Python (no C extension is compiled; optional accelerators
such as websockets' speedups.c are skipped and the pure fallbacks are used).

usage: assemble_python_packages.py <sources-dir> <out-site-packages>
"""
import json
import shutil
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path

LOCK = Path(__file__).resolve().parent.parent.parent / "native" / "sources.lock.toml"
KINDS = {"python-source", "python-pure"}
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.c", "*.h", "*.pyx", "tests", "test", "*.pyi~")


def import_name(comp: str, entry: dict) -> str:
    base = "yt-dlp" if comp == "yt-dlp" else entry.get("pypi", comp)
    return base.replace("-", "_")


def find_pkg(root: Path, pkg: str) -> Path:
    for cand in (root / pkg, root / "src" / pkg, root / "lib" / pkg):
        if (cand / "__init__.py").is_file():
            return cand
    raise SystemExit(f"package '{pkg}' not found in {root}")


def main() -> int:
    src_dir, out = Path(sys.argv[1]), Path(sys.argv[2])
    comps = tomllib.loads(LOCK.read_text())["components"]
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    report = []
    for name, entry in comps.items():
        if entry["kind"] not in KINDS:
            continue
        d = src_dir / f"{name}-{entry['version']}"
        prov = json.loads((d / "provenance.json").read_text())
        pkg = import_name(name, entry)
        with tempfile.TemporaryDirectory() as tmp, tarfile.open(d / prov["archive"]) as t:
            t.extractall(tmp, filter="data")  # refuses absolute paths / path traversal
            tops = [p for p in Path(tmp).iterdir() if p.is_dir()]
            root = tops[0] if len(tops) == 1 else Path(tmp)
            shutil.copytree(find_pkg(root, pkg), out / pkg, ignore=IGNORE)
        dist = out / f"{pkg}-{entry['version']}.dist-info"
        dist.mkdir()
        proj = "yt-dlp" if name == "yt-dlp" else entry["pypi"]
        (dist / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {proj}\nVersion: {entry['version']}\n")
        (dist / "INSTALLER").write_text("viddown-native-bundle\n")
        report.append({"component": name, "version": entry["version"], "package": pkg,
                       "source_used": prov["source_used"]["name"]})
    # packages whose version file is generated at build time by hatch-vcs / setuptools-scm
    if (out / "urllib3").is_dir() and not (out / "urllib3" / "_version.py").exists():
        (out / "urllib3" / "_version.py").write_text(f'__version__ = "{comps["py-urllib3"]["version"]}"\n')
    json.dump(report, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
