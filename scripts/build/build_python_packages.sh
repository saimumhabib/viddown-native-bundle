#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# ABI-independent: fetch yt-dlp + its pure-Python dependencies (hash-verified) and assemble
# one site-packages archive. No pip, no compiler, no network beyond the verified fetch.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
OUT="$DIST_DIR/shared"
mkdir -p "$OUT/assets" "$OUT/metadata"
for c in $(python3 "$ROOT/scripts/manifest_get.py" components python-source,python-pure); do
  python3 "$ROOT/scripts/fetch_source.py" --cache-dir "$CACHE_DIR" fetch "$c" --out "$SRC_DIR" >&2 || die "cannot fetch $c"
done
SP="$BUILD_DIR/work/site-packages"
python3 "$ROOT/scripts/build/assemble_python_packages.py" "$SRC_DIR" "$SP" > "$OUT/metadata/python-packages.json"
(cd "$SP" && find . -name '__pycache__' -prune -exec rm -rf {} + ; zip -qr -X "$OUT/assets/ytdlp-site-packages.zip" .)
say "done: $OUT/assets/ytdlp-site-packages.zip"
