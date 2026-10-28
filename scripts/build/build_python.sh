#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Build, FROM SOURCE, for one ABI:
#   build-machine Python -> OpenSSL, libffi -> CPython (shared libpython + stdlib) -> launcher
# Output: $DIST_DIR/<ABI>/ (jniLibs + assets) and provenance in $SRC_DIR.
#
# usage: build_python.sh <abi>      e.g. arm64-v8a | x86_64 | armeabi-v7a | x86
#
# IMPORTANT (environment hygiene): Android's cross-compiler variables (CC, CFLAGS, ...) come from
# android-env.sh. They must NEVER be set while configuring the *build* Python, which runs on this
# machine: configure would compile with the Android clang and then fail with
# "cannot run C compiled programs". So the top-level shell stays clean, and every step that needs the
# cross toolchain loads android-env.sh inside its own subshell. (android.py loads it itself for its
# configure-host / make-host steps.)
#
# NOTE: CPython's android.py normally downloads PREBUILT OpenSSL/libffi/... binaries from a third
# party. We pre-create its prefix directory with our own from-source builds, which makes android.py
# skip that download (it only unpacks deps when the prefix does not exist).
# STATUS: verified only by CI runs.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

# Start from a clean toolchain environment, whatever the caller exported.
unset CC CXX AR AS LD NM RANLIB READELF STRIP CFLAGS CXXFLAGS CPPFLAGS LDFLAGS \
      PKG_CONFIG_PATH PKG_CONFIG_LIBDIR PKG_CONFIG_SYSROOT_DIR

ABI="${1:?usage: build_python.sh <abi>}"
HOST="$(abi_to_host "$ABI")"
W="$BUILD_DIR/work/$ABI"
OUT="$DIST_DIR/$ABI"
rm -rf "$W" "$OUT"; mkdir -p "$W" "$OUT/jniLibs/$ABI" "$OUT/assets"

PYV="$(manifest_version cpython)"; PYMM="${PYV%.*}"
fetch_extract cpython  "$W/cpython"
fetch_extract openssl  "$W/openssl"
fetch_extract libffi   "$W/libffi"

CPY="$W/cpython"
PREFIX="$CPY/cross-build/$HOST/prefix"
mkdir -p "$PREFIX"

# ---------------------------------------------------------------- 1. build-machine Python (CLEAN env)
say "build Python $PYV (runs on this machine; no cross-compiler variables)"
(
  cd "$CPY" || exit 1
  python3 Android/android.py configure-build
  python3 Android/android.py make-build
)

# ---------------------------------------------------------------- 2. OpenSSL (from source)
say "OpenSSL $(manifest_version openssl) for $HOST"
(
  load_android_env "$HOST" "$CPY" "$PREFIX"
  say "toolchain: $CC (API $ANDROID_API_LEVEL), NDK $NDK_ROOT"
  cd "$W/openssl" || exit 1
  for p in at_secure configuration soname-3; do patch -p1 -s -i "$ROOT/patches/openssl/$p.patch"; done
  CFLAGS+=" -O2"                       # CFLAGS from android-env REPLACES defaults
  export LDLIBS="-latomic"
  case "$HOST" in *64*) bits=64 ;; *) bits=32 ;; esac
  ./Configure "android-python$bits" shared
  make -j "$CPU_COUNT"
  make install_sw DESTDIR="$W/openssl-install"
  cp -a "$W/openssl-install/usr/local/." "$PREFIX/"
)

# ---------------------------------------------------------------- 3. libffi (from source)
say "libffi $(manifest_version libffi) for $HOST"
(
  load_android_env "$HOST" "$CPY" "$PREFIX"
  cd "$W/libffi" || exit 1
  for p in long_double open_temp_exec_file; do patch -p1 -s -i "$ROOT/patches/libffi/$p.patch"; done
  ./configure --host="$HOST" --prefix="$PREFIX" --disable-shared --with-pic
  make -j "$CPU_COUNT"
  make install
)

# ---------------------------------------------------------------- 4. CPython for Android
# android.py sources android-env.sh itself for these two steps.
say "CPython $PYV for $HOST"
(
  cd "$CPY" || exit 1
  python3 Android/android.py configure-host "$HOST"
  python3 Android/android.py make-host "$HOST"
)

# ---------------------------------------------------------------- sanity: the pieces we depend on exist
need() { [ -e "$1" ] || die "expected build output missing: $1"; }
need "$PREFIX/lib/libpython$PYMM.so"
need "$PREFIX/lib/libssl_python.so"
need "$PREFIX/lib/libcrypto_python.so"
DYN="$PREFIX/lib/python$PYMM/lib-dynload"
ls "$DYN"/_ssl.*.so   >/dev/null 2>&1 || die "_ssl extension module was not built (OpenSSL not picked up)"
ls "$DYN"/_ctypes.*.so >/dev/null 2>&1 || echo "WARNING: _ctypes not built (libffi not picked up)" >&2
ls "$DYN"/_hashlib.*.so >/dev/null 2>&1 || die "_hashlib extension module was not built"

# ---------------------------------------------------------------- 5. launcher + stripped runtime files
say "launcher and runtime files"
(
  load_android_env "$HOST" "$CPY" "$PREFIX"
  # CFLAGS/LDFLAGS are intentionally word-split:
  # shellcheck disable=SC2086
  "$CC" $CFLAGS -fPIE -I"$PREFIX/include/python$PYMM" "$ROOT/native/launcher/viddown_python.c" \
    -o "$OUT/jniLibs/$ABI/libviddown_python.so" \
    $LDFLAGS -pie -L"$PREFIX/lib" "-lpython$PYMM" -Wl,-rpath,'$ORIGIN'
  cp "$PREFIX/lib/libpython$PYMM.so" "$PREFIX/lib/libssl_python.so" "$PREFIX/lib/libcrypto_python.so" "$OUT/jniLibs/$ABI/"
  for f in "$OUT/jniLibs/$ABI"/*.so; do "$STRIP" "$f"; done

  STAGE="$W/stage"; rm -rf "$STAGE"; mkdir -p "$STAGE"
  # GNU tar: --exclude options only apply to arguments that come AFTER them, so they go first.
  tar -C "$PREFIX/lib" \
    --exclude='__pycache__' --exclude='*/test' --exclude='*/tests' --exclude='idlelib' --exclude='tkinter' \
    --exclude='turtledemo' --exclude='ensurepip' --exclude='config-*' --exclude='site-packages' \
    -cf - "python$PYMM" \
    | tar -C "$STAGE" -xf -
  find "$STAGE" -name '*.so' -exec "$STRIP" {} +
  (cd "$STAGE/python$PYMM" && zip -qr -X "$OUT/assets/python-lib-$PYMM-$ABI.zip" .)
)
echo "$PYV" > "$OUT/assets/python-version.txt"
say "done: $OUT"
