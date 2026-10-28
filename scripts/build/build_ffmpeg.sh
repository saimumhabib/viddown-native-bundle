#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Build FFmpeg + FFprobe FROM SOURCE for one ABI with a minimal, LGPL-only configuration.
# Purpose: remux / merge / audio stream-copy of files that yt-dlp already downloaded.
# It is NOT a downloader: protocols are limited to local files and pipes.
#
# usage: build_ffmpeg.sh <abi>
# STATUS: configure component names were validated against the real FFmpeg 8.1.3 source;
# the cross-build itself is verified only by CI runs.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

ABI="${1:?usage: build_ffmpeg.sh <abi>}"
HOST="$(abi_to_host "$ABI")"
W="$BUILD_DIR/work/$ABI/ffmpeg-build"
OUT="$DIST_DIR/$ABI"
mkdir -p "$OUT/jniLibs/$ABI" "$OUT/metadata"

case "$ABI" in
  arm64-v8a) ARCH=aarch64 ;; armeabi-v7a) ARCH=arm ;; x86) ARCH=x86 ;; x86_64) ARCH=x86_64 ;;
esac

# Only CPython's Android/android-env.sh is needed for the toolchain: extract just that file.
CPY="$BUILD_DIR/work/$ABI/cpython-env"
if [ ! -f "$CPY/Android/android-env.sh" ]; then
  python3 "$ROOT/scripts/fetch_source.py" --cache-dir "$CACHE_DIR" fetch cpython --out "$SRC_DIR" >&2 || die "cannot fetch cpython"
  mkdir -p "$CPY"
  tar -xf "$(python3 "$ROOT/scripts/manifest_get.py" archive cpython "$SRC_DIR")" -C "$CPY" --strip-components=1 --wildcards '*/Android/android-env.sh'
fi
fetch_extract ffmpeg "$W"
load_android_env "$HOST" "$CPY"

DEMUXERS=mov,matroska,mp3,aac,ogg,flac,wav,mpegts
MUXERS=mp4,ipod,matroska,webm,mp3,adts,ogg,opus,flac,wav,mov
PARSERS=aac,aac_latm,h264,hevc,vp9,av1,opus,vorbis,flac,mpegaudio,vp8,mpeg4video
BSFS=aac_adtstoasc,extract_extradata,h264_mp4toannexb,hevc_mp4toannexb,vp9_superframe,vp9_superframe_split,av1_frame_merge,av1_frame_split,opus_metadata
DECODERS=aac,mp3float,vorbis,opus,flac,pcm_s16le
ENCODERS=aac,flac,pcm_s16le
FILTERS=aformat,anull,null,format,aresample,scale,copy,acopy

cd "$W" || exit 1
# Fail with a clear message if FFmpeg renamed a component, instead of an obscure configure error.
for spec in demuxers:$DEMUXERS muxers:$MUXERS parsers:$PARSERS bsfs:$BSFS decoders:$DECODERS encoders:$ENCODERS filters:$FILTERS; do
  kind="${spec%%:*}"; list="${spec#*:}"
  available="$(./configure "--list-$kind" 2>/dev/null | tr ' ' '\n')"
  for n in ${list//,/ }; do
    grep -qx "$n" <<<"$available" || die "FFmpeg $(manifest_version ffmpeg) has no $kind '$n' (update build_ffmpeg.sh)"
  done
done

CONFIG_ARGS=(
  --prefix="$W/install"
  --enable-cross-compile --target-os=android --arch="$ARCH"
  --cc="$CC" --cxx="$CXX" --ar="$AR" --nm="$NM" --ranlib="$RANLIB" --strip="$STRIP"
  --extra-cflags="$CFLAGS" --extra-ldflags="$LDFLAGS" --extra-ldexeflags=-pie
  --enable-pic --disable-asm            # remux speed does not need hand-written asm; avoids per-arch assembler needs
  --disable-everything --disable-autodetect --disable-doc --disable-debug --disable-network
  --disable-ffplay --enable-ffmpeg --enable-ffprobe --enable-small
  --enable-zlib                          # NDK-provided libz (MP4 compressed headers)
  "--enable-protocol=file,pipe"
  --enable-demuxer="$DEMUXERS" --enable-muxer="$MUXERS" --enable-parser="$PARSERS" --enable-bsf="$BSFS"
  --enable-decoder="$DECODERS" --enable-encoder="$ENCODERS" --enable-filter="$FILTERS"
)
say "configure FFmpeg for $ABI"
./configure "${CONFIG_ARGS[@]}"

# License gate: this build must be LGPL. Fail if the configuration silently turned GPL/nonfree on.
for flag in CONFIG_GPL CONFIG_NONFREE CONFIG_VERSION3; do
  grep -Eq "^#define $flag 1" config.h && die "$flag is enabled; this build would not be plain LGPL-2.1+"
done
printf '%s\n' "./configure ${CONFIG_ARGS[*]}" > "$OUT/metadata/ffmpeg-configure-$ABI.txt"
cp config.h "$OUT/metadata/ffmpeg-config-$ABI.h"

make -j "$CPU_COUNT" ffmpeg ffprobe
"$STRIP" -o "$OUT/jniLibs/$ABI/libffmpeg.so"  ffmpeg
"$STRIP" -o "$OUT/jniLibs/$ABI/libffprobe.so" ffprobe
cp COPYING.LGPLv2.1 "$OUT/metadata/FFMPEG-COPYING.LGPLv2.1"
say "done: $OUT"
