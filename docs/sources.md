# Source policy: ordered official sources with automatic fallback

Every upstream component has an **ordered list of official sources** in
`native/sources.lock.toml`. `scripts/fetch_source.py` tries them in order and moves on when a
source fails. A single unreachable host never breaks the build, and a source whose content does
not match its pin is **rejected**; there is no "accept anyway" path.

Only official project hosts, documented official mirrors, or a mirror you create and control
yourself (documented as a mirror of the exact upstream tag) are allowed.

## Files

| file | role |
|---|---|
| `native/source-templates.toml` | policy: where each component may come from, in order, and how new versions are found (hand-edited) |
| `native/sources.lock.toml` | generated pins: exact version, URLs, SHA-256, tag commit (never hand-edit a hash) |
| `scripts/fetch_source.py` | fetch + verify + fallback; also `check` (source health) and `list` |
| `scripts/update_source.py` | discover new versions, compute pins, **propose** a lock change |

## Verification rules (all enforced in `fetch_source.py`, all covered by tests)

1. https only (plain http/file are refused; tests opt in via an env var).
2. A source is accepted only if at least one declared pin verifies:
   `sha256` (the file), `sha256_uncompressed` (the decompressed tar stream, so a host that
   recompresses an archive does not break the pin), or the git commit for `git` sources.
3. If the archive carries a commit id (GitHub/`git archive` store it in the tar header) it must
   equal the commit pinned for the release tag.
4. A source with no pin (`UNPINNED`) is **skipped**, never trusted.
5. Timeouts, retries with exponential backoff for network errors / 5xx / 408 / 429, no retry for 404.
6. A cached copy is **re-verified** against the pin on every use; a corrupted cache entry is deleted.
7. Which source was actually used, and the evidence, is written to `provenance.json` and into
   `BUILD_INFO.json` of every released zip.
8. If all sources fail the build stops and lists every source with its reason.

## Source order and what was verified (2026-10-26, from the development sandbox)

"pinned" = URL reachable from the sandbox and hash/commit recorded. "UNPINNED" = official source,
but the sandbox cannot reach that host; the `update-sources` workflow fills it in from GitHub Actions.

| component | version | 1 | 2 | 3 |
|---|---|---|---|---|
| CPython | 3.14.10 | python.org tarball (UNPINNED) | GitHub tag archive (pinned) | GitHub git (pinned) |
| yt-dlp | 2026.10.21 | GitHub tag archive (pinned) | PyPI sdist (pinned) | GitHub release asset (pinned, cross-checked with project's SHA2-256SUMS) |
| FFmpeg | 8.1.3 | ffmpeg.org tarball (UNPINNED) | git.ffmpeg.org git (pinned commit; host unreachable from sandbox) | GitHub mirror tag archive (pinned) |
| OpenSSL | 3.5.9 | openssl.org tarball (UNPINNED) | GitHub release asset (pinned, cross-checked with published .sha256) | GitHub tag archive (pinned) |
| libffi | 3.4.4 | GitHub release asset (pinned) | GitHub git (pinned) | - |
| certifi, idna, urllib3, requests, charset-normalizer, mutagen, websockets, yt-dlp-ejs | per yt-dlp's `pin` extra | PyPI sdist (pinned, equals PyPI's published digest) | GitHub tag archive (pinned) | - |

Notes
* Versions of the Python dependencies are **not chosen by us**: they follow the `pin` extra of the
  pinned yt-dlp release. OpenSSL and libffi versions follow CPython's own `Android/android.py`.
* The sandbox could not reach `python.org`, `ffmpeg.org`, `openssl.org`, `git.ffmpeg.org`,
  `sourceware.org`. Those entries exist so the primary official host is tried first once pinned.
  Until then they are skipped and the next official source is used, which is exactly the fallback.
* The Android SDK/NDK is a **toolchain**, installed by `sdkmanager` from Google (version fixed by
  CPython's `android-env.sh`). It is not shipped in the bundle. This is the one documented exception
  to "everything from source".
* `bzip2`, `xz`, `zstd`, `sqlite` (also in CPython's prebuilt list) are intentionally **not built**:
  yt-dlp does not need them. The Python modules `bz2`, `lzma`, `_zstd`, `sqlite3` are therefore absent.

## Updating a version

Automatic (default path), see README "Updates": `update-sources` proposes, builds and - only when every
condition of the gate holds and auto-merge is enabled - merges. Otherwise it leaves the PR open with a comment.

Release: after an automatic merge the `release` job publishes the already-built, already-smoke-tested files as a
GitHub Release, but only if the merged git tree equals the built tree (see README). Release assets are created with
the workflow token, which by design does NOT start further workflows, so no second build is triggered by the tag.

Manual:
* `python3 scripts/update_source.py pin --component yt-dlp --version 2026.11.02` prints a diff;
  add `--write` to apply it, commit, open a PR (or push to a branch and run `build-native` on it).
* Or run the `update-sources` workflow by hand with `auto_merge` unticked and merge the PR yourself.

Safety (enforced twice: in `update_source.py` and by the gate via `check_lock_update.py`):
* an existing pin is never recomputed for the same version (`--repin` is explicit);
* if an upstream tag now points to a different commit, or a pinned hash/url changed for the same version,
  the update aborts / the gate refuses to merge ("tag moved");
* a new version needs the tag commit pinned and at least 2 verifiable sources;
* versions never go down, tracked components stay inside their series (CPython 3.14.x, FFmpeg 8.1.x);
* a new or removed component/source, or a licence change, always needs a human.

## Adding a mirror

1. Add a `[[components.NAME.sources]]`-style entry to the component in `native/source-templates.toml`
   at the position you want in the order (`type = "tarball"` with `pin = [...]`, or `type = "git"`).
2. Run `python3 scripts/update_source.py pin --component NAME --write` (add `--repin` only if you are
   sure; otherwise the new source is pinned and existing pins are kept).
3. Review the lock diff, commit. The weekly `source-health` workflow will keep checking it.
