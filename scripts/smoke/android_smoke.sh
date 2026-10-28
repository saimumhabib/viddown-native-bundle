#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Emulator smoke test of a packaged bundle, run from the host with `adb` connected.
#
# usage: android_smoke.sh <abi> <viddown-native-ABI-TAG.zip>
#
# Executes the binaries from /data/local/tmp (shell user). That proves they run on Android's
# bionic libc; it does NOT prove they run from inside an app sandbox (nativeLibraryDir). That
# second check belongs to the instrumented test in the app repo.
# STATUS: written, not yet run; the first CI run decides whether it passes.
set -uo pipefail
ABI="${1:?abi}"; ZIP="${2:?zip}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"; DEV=/data/local/tmp/viddown; PORT=8099
FAIL=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAIL=1; }
cleanup() { kill "${SRV:-0}" 2>/dev/null || true; adb reverse --remove "tcp:$PORT" 2>/dev/null || true; rm -rf "$WORK"; }
trap cleanup EXIT

PYV="$(python3 "$ROOT/scripts/manifest_get.py" version cpython)"; PYMM="${PYV%.*}"
YTV="$(python3 "$ROOT/scripts/manifest_get.py" version yt-dlp)"
FFV="$(python3 "$ROOT/scripts/manifest_get.py" version ffmpeg)"
OSV="$(python3 "$ROOT/scripts/manifest_get.py" version openssl)"

mkdir -p "$WORK/z" "$WORK/py" "$WORK/site" "$WORK/www"
unzip -q "$ZIP" -d "$WORK/z"
# The stdlib zip holds the CONTENTS of python3.X/ at its root, but Python looks for the stdlib in
# $PYTHONHOME/lib/python3.X/ - so it must be extracted there (the app must do the same).
mkdir -p "$WORK/py/lib/python$PYMM"
unzip -q "$WORK/z/assets/python-lib-$PYMM-$ABI.zip" -d "$WORK/py/lib/python$PYMM"
unzip -q "$WORK/z/assets/ytdlp-site-packages.zip" -d "$WORK/site"
adb shell "rm -rf $DEV; mkdir -p $DEV/home $DEV/tmp $DEV/out"
adb push -q "$WORK/z/jniLibs/$ABI" "$DEV/lib" >/dev/null
adb push -q "$WORK/py" "$DEV/py" >/dev/null
adb push -q "$WORK/site" "$DEV/site" >/dev/null
adb shell "chmod 755 $DEV/lib/*"

dev() {  # run a command on the device with the runtime environment
  adb shell "cd $DEV && export LD_LIBRARY_PATH=$DEV/lib PYTHONHOME=$DEV/py PYTHONPATH=$DEV/site HOME=$DEV/home TMPDIR=$DEV/tmp PYTHONDONTWRITEBYTECODE=1 SSL_CERT_FILE=$DEV/site/certifi/cacert.pem; $*" | tr -d '\r'   # stderr is NOT merged: old linkers print harmless warnings there
}
PY="$DEV/lib/libviddown_python.so"; FF="$DEV/lib/libffmpeg.so"; FP="$DEV/lib/libffprobe.so"

# 1. interpreter + native modules
out="$(dev "$PY -c 'import sys,ssl,zlib,hashlib; print(sys.version_info[:2], ssl.OPENSSL_VERSION)'")"
want_ver="(${PYMM%.*}, ${PYMM#*.})"
echo "$out"
[[ "$out" == *"$want_ver"* && "$out" == *"OpenSSL $OSV"* ]] \
  && pass "python $PYMM starts; ssl uses OpenSSL $OSV (built from source)" || fail "python/ssl check (want $want_ver, OpenSSL $OSV): $out"

# 2. yt-dlp version
out="$(dev "$PY -m yt_dlp --version")"; [ "$out" = "$YTV" ] && pass "yt-dlp reports $YTV" || fail "yt-dlp version: '$out' (want $YTV)"

# 3. ffmpeg / ffprobe start, LGPL build
out="$(dev "$FF -version | head -n 3")"; echo "$out"
[[ "$out" == *"ffmpeg version"*"$FFV"* ]] && pass "ffmpeg $FFV starts" || fail "ffmpeg version: $out"
dev "$FF -version" | grep -q -- "--enable-gpl" && fail "ffmpeg was built with --enable-gpl" || pass "ffmpeg built without --enable-gpl"
dev "$FP -version | head -n 1" | grep -q "ffprobe version" && pass "ffprobe starts" || fail "ffprobe did not start"

# 4. fixtures we generate ourselves (host ffmpeg is only a fixture generator, never shipped)
ffmpeg -loglevel error -y -f lavfi -i testsrc=size=320x240:rate=15 -f lavfi -i sine=frequency=440 -t 3 \
  -c:v libx264 -pix_fmt yuv420p -c:a aac -movflags +faststart "$WORK/www/test.mp4"
ffmpeg -loglevel error -y -i "$WORK/www/test.mp4" -c copy -f hls -hls_time 1 -hls_list_size 0 "$WORK/www/test.m3u8"
python3 "$ROOT/scripts/smoke/serve_fixtures.py" "$WORK/www" "$PORT" & SRV=$!
sleep 1; adb reverse "tcp:$PORT" "tcp:$PORT" >/dev/null
BASE="http://127.0.0.1:$PORT"

# 5. yt-dlp inspects URLs served by a server WE control and returns real format metadata
for f in test.mp4 test.m3u8; do
  dev "$PY -m yt_dlp -J --no-playlist --no-warnings $BASE/$f" > "$WORK/$f.json"
  python3 - "$WORK/$f.json" "$f" <<'PY' && pass "yt-dlp -J $f returned formats" || fail "yt-dlp -J $f: $(head -c 300 "$WORK/$f.json")"
import json, sys
d = json.load(open(sys.argv[1]))
fm = d.get("formats") or []
assert fm, "no formats"
print("   formats:", [(x.get("format_id"), x.get("ext"), x.get("height")) for x in fm][:4])
PY
done

# 6. yt-dlp download
# NOTE: no %(ext)s templates here - the device shell (/system/bin/sh) parses this string and rejects bare "(".
dev "$PY -m yt_dlp --no-warnings -o $DEV/out/dl.mp4 $BASE/test.mp4" >/dev/null
[ "$(dev "ls -l $DEV/out/dl.mp4 2>/dev/null | wc -l")" = "1" ] && pass "yt-dlp downloaded test.mp4" || fail "yt-dlp download produced no file"

# 7. ffmpeg: split + merge (remux, stream copy) and validate with ffprobe
dev "$FF -loglevel error -y -i $DEV/out/dl.mp4 -an -c copy $DEV/out/v.mp4 && $FF -loglevel error -y -i $DEV/out/dl.mp4 -vn -c copy $DEV/out/a.m4a && $FF -loglevel error -y -i $DEV/out/v.mp4 -i $DEV/out/a.m4a -c copy $DEV/out/merged.mp4"
dur="$(dev "$FP -v error -show_entries format=duration -of csv=p=0 $DEV/out/merged.mp4")"
streams="$(dev "$FP -v error -show_entries stream=codec_type -of csv=p=0 $DEV/out/merged.mp4" | sort | tr '\n' ' ')"
python3 -c "import sys; sys.exit(0 if float('$dur')>2 else 1)" 2>/dev/null && [[ "$streams" == *audio*video* ]] \
  && pass "ffmpeg merged video+audio (duration ${dur}s, streams: $streams)" || fail "merge validation failed: dur='$dur' streams='$streams'"

# 8. cancellation: kill a running yt-dlp and make sure nothing is left behind
dev "nohup $PY -m yt_dlp --no-warnings -o $DEV/out/slow.bin $BASE/slow.bin >/dev/null 2>&1 & echo started" >/dev/null
sleep 4
# `ps -A` does not exist on old Android (API 24): fall back to plain `ps`. Match the (truncated) process name.
viddown_pids() { adb shell "ps -A 2>/dev/null || ps" | tr -d '\r' | awk '/viddown_pyt/ {print $2}'; }
n_before="$(viddown_pids | wc -l)"
for pid in $(viddown_pids); do adb shell "kill -9 $pid" >/dev/null 2>&1; done; sleep 1
n_after="$(viddown_pids | wc -l)"
[ "${n_before:-0}" -ge 1 ] && [ "${n_after:-1}" -eq 0 ] && pass "cancel: process was running ($n_before) and is gone after kill" || fail "cancel test: before=$n_before after=$n_after"

echo; [ "$FAIL" = 0 ] && echo "SMOKE TEST PASSED for $ABI" || echo "SMOKE TEST FAILED for $ABI"
exit "$FAIL"
