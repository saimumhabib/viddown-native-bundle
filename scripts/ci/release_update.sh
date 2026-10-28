#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Publish a GitHub Release for an update PR that the gate has just merged.
# Called by update-sources.yml (job "release"). Needs the `gh` CLI, a full git checkout of the merged
# default branch, and the build artifacts already downloaded into ASSETS_DIR.
#
# It publishes the files that were ALREADY built and smoke-tested for the PR head commit. That is only
# honest if the merged code is exactly that code, so it compares the git trees first. If main moved in
# the meantime it does NOT release; it tells the PR and a person decides (push a native-v* tag).
#
# Required env: PR_NUMBER HEAD_SHA MERGE_SHA TAG REPO RUN_ID ASSETS_DIR GH_TOKEN
set -uo pipefail
: "${PR_NUMBER:?}" "${HEAD_SHA:?}" "${MERGE_SHA:?}" "${TAG:?}" "${REPO:?}" "${RUN_ID:?}" "${ASSETS_DIR:?}"
TMP="${RELEASE_TMP:-$(mktemp -d)}"

say_pr() {  # say_pr <markdown>: print it and put it on the PR
  echo "$1"
  gh pr comment "$PR_NUMBER" --repo "$REPO" --body "$1" >/dev/null 2>&1 || echo "warning: could not post the PR comment" >&2
}

# 1. is the merged code exactly the code that was built and smoke-tested?
if ! git fetch --no-tags --quiet origin "refs/pull/$PR_NUMBER/head"; then
  say_pr "Merged, but **no automatic release**: could not fetch the PR head to compare it with the merge. Push a \`native-v*\` tag to release by hand."
  exit 0
fi
merged_tree="$(git rev-parse "$MERGE_SHA^{tree}" 2>/dev/null || echo none)"
built_tree="$(git rev-parse "$HEAD_SHA^{tree}" 2>/dev/null || echo none2)"
if [ "$merged_tree" != "$built_tree" ]; then
  say_pr "Merged, but **no automatic release**: \`main\` changed while this update was being built, so the merged code is not exactly the code that was built and tested. Push a \`native-v*\` tag to build and release it properly."
  exit 0
fi

# 2. the two required ABIs must be among the files we are about to publish
for abi in arm64-v8a x86_64; do
  if ! ls "$ASSETS_DIR"/viddown-native-"$abi"-*.zip >/dev/null 2>&1; then
    say_pr "Merged, but **no automatic release**: the $abi zip is missing from the build artifacts."
    exit 0
  fi
done

# 3. checksums, notes, release
( cd "$ASSETS_DIR" && rm -f ./*.zip.sha256 && sha256sum viddown-native-*.zip > SHA256SUMS.txt ) || exit 1
python3 scripts/ci/release_notes.py --lock native/sources.lock.toml --tag "$TAG" --assets "$ASSETS_DIR" \
  --pr "$PR_NUMBER" --repo "$REPO" --run-url "https://github.com/$REPO/actions/runs/$RUN_ID" > "$TMP/notes.md" || exit 1
gh release create "$TAG" "$ASSETS_DIR"/* --repo "$REPO" --target "$MERGE_SHA" --title "$TAG" \
  --notes-file "$TMP/notes.md" --latest || exit 1
say_pr "Released **$TAG**: https://github.com/$REPO/releases/tag/$TAG"
