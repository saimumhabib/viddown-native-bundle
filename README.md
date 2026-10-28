# viddown-native-bundle

Builds, **from source** and per Android ABI, the native/Python runtime used by the VidDown app's
yt-dlp engine: CPython, OpenSSL, libffi, FFmpeg/FFprobe, and yt-dlp with its pure-Python
dependencies. No prebuilt wrappers, no third-party binaries (`youtubedl-android`, `ffmpeg-kit`, etc.
are not used). Every upstream source is pinned and hash-verified and fetched through an ordered
list of official sources with automatic fallback ([docs/sources.md](docs/sources.md)).

The VidDown app repo consumes the zips this repo publishes.

## Status (honest)

| part | state |
|---|---|
| `fetch_source.py` (fallback, hash/commit verification, cache, health check) | **implemented and tested** locally: 31 unit tests (fetch script + update helper) against a local server that simulates refused connection, 404, 500, timeout, truncated file, wrong checksum, tampered cache, moved tag |
| `update_source.py`, `sources.lock.toml` | **run against the real upstreams**; 13 components in the lock; sources the sandbox could not reach are `UNPINNED` (skipped until pinned); the main hashes were cross-checked against independent downloads |
| yt-dlp + pure dependencies bundle | **assembled and imported on a host Python** (1754 extractors load) |
| OpenSSL/libffi patches | **dry-run applied** to the pinned sources |
| FFmpeg configure component names | **validated** against the real FFmpeg 8.1.3 source |
| `package.py` (BUILD_INFO, notices, licence files, checksums) | **run** with real provenance |
| CPython / OpenSSL / libffi / FFmpeg **cross-builds for Android** | **written, NOT yet run.** Needs the Android NDK; first CI run decides |
| emulator smoke test | **written, NOT yet run** |
| auto-update pipeline | gate and release logic **tested locally** (10 gate + 4 release scenarios with a fake `gh` and real git repos, 14 policy tests, actionlint clean). The GitHub wiring itself (reusable-workflow call, PR refresh, real merge) is **not yet run** |
| ABIs verified | **none yet.** This table will list only ABIs that passed the smoke test |

## Layout

```
native/source-templates.toml   where each component may come from (ordered) + how to find new versions
native/sources.lock.toml       generated pins (versions, URLs, sha256, tag commits)
native/launcher/               tiny C launcher that runs the interpreter as a process
patches/                       vendored OpenSSL/libffi patches (MIT, provenance in patches/README.md)
scripts/fetch_source.py        fetch + verify + fallback + health check
scripts/update_source.py       propose version/hash updates
scripts/check_lock_update.py   safety policy a machine-made lock change must satisfy
scripts/ci/gate_update.sh      decides: merge the update PR or explain why not
scripts/ci/release_update.sh   publishes the release for a merged update (tree-equality check)
scripts/ci/release_notes.py    release notes that state only what was verified
scripts/build/                 build_python.sh, build_ffmpeg.sh, build_python_packages.sh
scripts/package.py             zip + BUILD_INFO.json + THIRD_PARTY_NOTICES.md + licences + SHA256SUMS
scripts/smoke/                 emulator smoke test + local fixture server
.github/workflows/             ci, build-native, source-health (weekly), update-sources (weekly)
```

## Use

```sh
python3 -m unittest discover -s scripts/tests -v     # fallback tests, no network needed
python3 scripts/fetch_source.py list                 # components, order, pin status
python3 scripts/fetch_source.py fetch yt-dlp         # fetch + verify one component
python3 scripts/fetch_source.py check                # is every source reachable and matching its hash?
python3 scripts/update_source.py discover            # newer upstream versions?
```

Native builds run in GitHub Actions (`build-native`): Actions -> build-native -> Run workflow.
Locally you need Linux, `ANDROID_HOME` with `cmdline-tools/latest` (see CPython's `Android/README.md` for
the exact requirements), and the packages listed in the workflow, then `scripts/build/build_python.sh <abi>`.

## Output (per ABI)

`viddown-native-<abi>-<tag>.zip` containing `jniLibs/<abi>/*.so`, `assets/*`, `BUILD_INFO.json`
(version, **source actually used** and verified hashes per component), `THIRD_PARTY_NOTICES.md`,
`licenses/`, `SHA256SUMS`; plus a `.sha256` next to the zip. Releases are created only for tags
named `native-v*`; nothing is published from ordinary commits.

## Updates (automatic, with a manual option)

`update-sources` (Mondays, or "Run workflow") does the whole chain:

1. finds newer yt-dlp / CPython (3.14.x) / FFmpeg (8.1.x) versions; dependency versions come from
   yt-dlp's own `pin` list and CPython's Android dependency list; computes the hashes;
2. opens (or refreshes) the PR `auto/update-sources`;
3. **builds that exact commit** for all ABIs (`build-native` called as a reusable workflow) including the
   x86_64 emulator smoke tests (API 24 and 34);
4. a **gate** merges the PR only if ALL hold: both required ABIs (arm64-v8a, x86_64) produced their
   artifact (x86_64 only after the smoke tests passed), unit tests pass, the lock change satisfies the
   safety policy (`scripts/check_lock_update.py`: no moved tag, no changed hash for an existing version,
   no series jump, no downgrade, no new/removed component or source, at least 2 verifiable sources),
   and the PR branch did not move while building. Otherwise the PR stays open and a comment lists why.

Switches (default is SAFE = never merge by itself):

| how it runs | auto-merge is on when |
|---|---|
| scheduled | repository variable `AUTO_MERGE_SOURCES` = `true` (Settings > Secrets and variables > Actions > Variables) |
| manual "Run workflow" | the `auto_merge` box is ticked |

5. **Automatic release.** After an automatic merge, the `release` job publishes a GitHub Release
   (`native-vYYYY.MM.DD`, then `.2`, `.3` for several in one day) from the files that were **already built and
   smoke-tested** for that PR, with `SHA256SUMS.txt` and honest notes (only x86_64 is stated as executed in CI;
   other ABIs as "built, not run-tested"). It does this only if the merged code's git tree is identical to the
   tree that was built; if `main` moved meanwhile it does not release and says so on the PR (then push a
   `native-v*` tag to build and release properly). It reuses the build, so no second hour-long build.
   Switch: scheduled runs release unless the repository variable `AUTO_RELEASE_SOURCES` is `false`; manual runs
   use the `auto_release` box (default on). A release happens only after an automatic merge; if you merge by
   hand, release by hand with a tag.

Manual updating stays possible at any time: run `update-sources` without `auto_merge` (it opens the PR and
builds it, you merge), or use `python3 scripts/update_source.py pin --component NAME --version X --write`.
Manual release: push a tag `native-vX.Y.Z` (builds all ABIs, then releases if they pass).

Caveats: arm64-v8a is built but not run in CI (the emulator is x86_64), so auto-merge proves arm64 compiles,
not that it runs. If `main` has branch protection that requires reviews, GitHub refuses the merge and the
gate says so in a comment.

## Known limitations

See [docs/architecture.md](docs/architecture.md): no JavaScript runtime bundled yet, `brotli` and
`pycryptodomex` deferred, some stdlib modules absent, FFmpeg is remux-only, unverified ABIs.

## Licence

GPL-3.0-or-later (see `LICENSE`). Third-party licences: `docs/architecture.md` and, per build,
`THIRD_PARTY_NOTICES.md` inside each zip.
