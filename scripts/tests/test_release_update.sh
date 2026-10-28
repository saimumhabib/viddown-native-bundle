#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Scenario tests for scripts/ci/release_update.sh with a fake `gh` and real local git repositories.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "$HERE/../.." && pwd)"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
GIT=(git -c user.name=t -c user.email=t@t)
export PATH="$HERE/fakebin:$PATH" FAKE_LOG="$W/gh.log" FAKE_COMMENT="$W/comment.md" FAKE_NOTES="$W/notes.md" GH_TOKEN=x
fails=0

# remote with: base commit, PR head commit B (refs/pull/7/head)
git init -q --bare "$W/origin.git"
git init -q -b main "$W/repo" && cd "$W/repo" || exit 1
tar -C "$SRC" --exclude=build --exclude=dist --exclude=.cache --exclude=__pycache__ --exclude=.git -cf - . | tar -xf -
git remote add origin "$W/origin.git"
"${GIT[@]}" add -A && "${GIT[@]}" commit -qm base
BASE="$(git rev-parse HEAD)"
echo "lock change" > pin-change.txt && "${GIT[@]}" add -A && "${GIT[@]}" commit -qm "PR head"
B="$(git rev-parse HEAD)"
git push -q origin "$B:refs/pull/7/head" "$BASE:refs/heads/main"
SQUASH_SAME="$("${GIT[@]}" commit-tree "$B^{tree}" -p "$BASE" -m squash)"            # same tree as the PR head
echo "someone else pushed" > other.txt && "${GIT[@]}" add -A
TREE_DIFF="$(git write-tree)"
SQUASH_DIFF="$("${GIT[@]}" commit-tree "$TREE_DIFF" -p "$BASE" -m squash2)"           # tree differs from the PR head
git push -q origin "$SQUASH_SAME:refs/heads/same" "$SQUASH_DIFF:refs/heads/diff"
git fetch -q origin && git reset -q --hard "$B"

mkassets() { rm -rf "$W/assets"; mkdir "$W/assets"; for a in "$@"; do echo z > "$W/assets/viddown-native-$a-native-v2026.10.28.zip"; echo h > "$W/assets/viddown-native-$a-native-v2026.10.28.zip.sha256"; done; }
run() {  # run <name> <yes|no> <merge-sha>
  : > "$FAKE_LOG"; rm -f "$FAKE_COMMENT" "$FAKE_NOTES"
  ( export PR_NUMBER=7 HEAD_SHA="$B" MERGE_SHA="$3" TAG=native-v2026.10.28 REPO=o/r RUN_ID=99 ASSETS_DIR="$W/assets" RELEASE_TMP="$W/tmp"
    mkdir -p "$W/tmp"; bash "$SRC/scripts/ci/release_update.sh" >/dev/null 2>&1 )
  local called ok=FAIL; called="$(grep -c 'release create' "$FAKE_LOG")"
  { [ "$2" = yes ] && [ "$called" = 1 ]; } && ok=PASS
  { [ "$2" = no ] && [ "$called" = 0 ]; } && ok=PASS
  [ "$ok" = PASS ] || fails=$((fails + 1))
  printf '%-4s %-58s release-created=%s\n' "$ok" "$1" "$called"
}

mkassets arm64-v8a x86_64 x86
run "merged tree == built tree: release is created" yes "$SQUASH_SAME"
grep -q -- "--target $SQUASH_SAME" "$FAKE_LOG" || { echo "FAIL release is not pinned to the merge commit"; fails=$((fails + 1)); }
grep -q -- "--latest" "$FAKE_LOG" || { echo "FAIL not marked latest"; fails=$((fails + 1)); }
grep -q "SHA256SUMS.txt" "$FAKE_LOG" || { echo "FAIL SHA256SUMS.txt is not among the release assets"; fails=$((fails + 1)); }
grep -q "\.zip\.sha256" "$FAKE_LOG" && { echo "FAIL stale per-zip .sha256 files were uploaded"; fails=$((fails + 1)); }
grep -q "executed in CI" "$FAKE_NOTES" || { echo "FAIL notes do not say what was run"; fails=$((fails + 1)); }
grep -q "Released \*\*native-v2026.10.28\*\*" "$FAKE_COMMENT" || { echo "FAIL PR was not told about the release"; fails=$((fails + 1)); }

mkassets arm64-v8a x86_64
run "main moved meanwhile (trees differ): NO release" no "$SQUASH_DIFF"
grep -q "main\` changed" "$FAKE_COMMENT" || { echo "FAIL the PR comment does not explain why"; fails=$((fails + 1)); }

mkassets arm64-v8a
run "x86_64 zip missing: NO release" no "$SQUASH_SAME"
grep -q "x86_64 zip is missing" "$FAKE_COMMENT" || { echo "FAIL missing-zip reason not shown"; fails=$((fails + 1)); }

mkassets arm64-v8a x86_64
run "unknown merge commit: NO release" no "0000000000000000000000000000000000000000"

[ "$fails" = 0 ] && echo "ALL RELEASE SCENARIOS OK" || echo "$fails RELEASE SCENARIO(S) FAILED"
exit "$fails"
