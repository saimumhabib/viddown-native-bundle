#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Shared helpers. Source this file; do not execute it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD_DIR="${BUILD_DIR:-$ROOT/build}"
SRC_DIR="${SRC_DIR:-$BUILD_DIR/sources}"
CACHE_DIR="${SOURCE_CACHE_DIR:-$ROOT/.cache/sources}"
DIST_DIR="${DIST_DIR:-$ROOT/dist}"
export ANDROID_API_LEVEL="${ANDROID_API_LEVEL:-24}"   # = minSdk of anything that consumes this bundle

die() { echo "ERROR: $*" >&2; exit 1; }
say() { echo "==> $*" >&2; }

abi_to_host() {
  case "$1" in
    arm64-v8a)   echo aarch64-linux-android ;;
    armeabi-v7a) echo arm-linux-androideabi ;;
    x86)         echo i686-linux-android ;;
    x86_64)      echo x86_64-linux-android ;;
    *) die "unknown ABI '$1' (use arm64-v8a, armeabi-v7a, x86, x86_64)" ;;
  esac
}

manifest_version() { python3 "$ROOT/scripts/manifest_get.py" version "$1"; }

# fetch_extract <component> <dest-dir>
# Fetches through the ordered, hash-verified source list (scripts/fetch_source.py),
# then unpacks with the top-level directory stripped. Provenance goes to $SRC_DIR.
fetch_extract() {
  local name="$1" dest="$2" archive
  say "fetching $name"
  python3 "$ROOT/scripts/fetch_source.py" --cache-dir "$CACHE_DIR" fetch "$name" --out "$SRC_DIR" >&2 \
    || die "could not fetch '$name' from any verified source (see log above)"
  archive="$(python3 "$ROOT/scripts/manifest_get.py" archive "$name" "$SRC_DIR")"
  rm -rf "$dest"; mkdir -p "$dest"
  tar -xf "$archive" -C "$dest" --strip-components=1
}

# load_android_env <HOST> <cpython-source-tree> [prefix]
# Re-uses CPython's own Android/android-env.sh so every component is built with exactly
# the NDK, API level and flags CPython is tested with. Source it ONCE per shell.
load_android_env() {
  export HOST="$1"
  export ANDROID_HOME="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
  [ -n "$ANDROID_HOME" ] || die "set ANDROID_HOME to an Android SDK (with cmdline-tools/latest)"
  if [ -n "${3:-}" ]; then export PREFIX="$3"; fi
  # shellcheck disable=SC1091
  source "$2/Android/android-env.sh"
  # ndk and toolchain are defined by the sourced android-env.sh
  # shellcheck disable=SC2154
  export NDK_ROOT="$ndk" NDK_TOOLCHAIN="$toolchain"
}
