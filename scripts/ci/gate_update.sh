#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Decide whether an automated source-update PR may be merged, then merge it or explain on the PR.
# Called by .github/workflows/update-sources.yml (job "gate"). Needs the `gh` CLI and a git checkout
# of the PR head with the base branch fetched as origin/<BASE_BRANCH>.
#
# Required env: PR_NUMBER HEAD_SHA BUILD_RESULT TESTS_OUTCOME AUTO_MERGE REPO RUN_ID BASE_BRANCH GH_TOKEN
# Never exits non-zero because a CHECK failed (that is a normal outcome); only on unexpected errors.
set -uo pipefail
: "${PR_NUMBER:?}" "${HEAD_SHA:?}" "${BUILD_RESULT:?}" "${TESTS_OUTCOME:?}" "${AUTO_MERGE:?}"
: "${REPO:?}" "${RUN_ID:?}" "${BASE_BRANCH:?}"
TMP="${GATE_TMP:-$(mktemp -d)}"
problems=()

# 1. the build must have run to the end. A plain "failure" can come from an EXPERIMENTAL ABI
#    (armeabi-v7a / x86), which must not block a merge; the required ABIs are proven by step 2.
case "$BUILD_RESULT" in
  success|failure) ;;
  *) problems+=("build-native did not run to the end (result '$BUILD_RESULT')") ;;
esac

# 2. these artifacts are uploaded only AFTER packaging, and for x86_64 only after the emulator smoke
#    tests (API 24 and 34) passed, so their presence is the proof we need.
if names="$(gh api --paginate "repos/$REPO/actions/runs/$RUN_ID/artifacts" --jq '.artifacts[] | select(.expired | not) | .name')"; then
  for need in viddown-native-arm64-v8a viddown-native-x86_64; do
    grep -qx "$need" <<<"$names" || problems+=("artifact '$need' is missing (its build, or the x86_64 emulator smoke test, did not pass)")
  done
else
  problems+=("could not list the build artifacts")
fi

# 3. unit tests + manifest sanity ran in the workflow just before this script
[ "$TESTS_OUTCOME" = "success" ] || problems+=("unit tests / manifest check failed")

# 4. the lock-file policy (no moved tags, no changed hashes, no series jumps, >= 2 verifiable sources)
policy_ok=1
if git show "origin/$BASE_BRANCH:native/sources.lock.toml" > "$TMP/old.lock.toml" 2>"$TMP/git.err"; then
  python3 scripts/check_lock_update.py "$TMP/old.lock.toml" native/sources.lock.toml > "$TMP/policy.txt" 2>&1 || policy_ok=0
else
  policy_ok=0; echo "could not read the base lock file: $(cat "$TMP/git.err")" > "$TMP/policy.txt"
fi
[ "$policy_ok" = 1 ] || problems+=("lock-file safety policy rejected the change (see the policy output above)")

# 5. the PR branch must not have moved while the build was running
current="$(gh pr view "$PR_NUMBER" --repo "$REPO" --json headRefOid --jq .headRefOid 2>/dev/null || echo unknown)"
[ "$current" = "$HEAD_SHA" ] || problems+=("the PR branch moved while building ($HEAD_SHA -> $current)")

# ---------------------------------------------------------------- report + decision
body="$TMP/comment.md"
{
  echo "### Automated gate for this source update"
  echo
  echo "Checked commit: \`$HEAD_SHA\` &middot; build run: https://github.com/$REPO/actions/runs/$RUN_ID"
  echo
  [ "$BUILD_RESULT" = "failure" ] && { echo "_Note: some build job failed. Only the required ABIs (arm64-v8a, x86_64) gate the merge; they are checked through their artifacts._"; echo; }
  echo '```'
  cat "$TMP/policy.txt"
  echo '```'
  echo
} > "$body"

merged=false
merge_sha=""
if [ "${#problems[@]}" -eq 0 ]; then
  if [ "$AUTO_MERGE" = "true" ]; then
    if gh pr merge "$PR_NUMBER" --repo "$REPO" --squash --delete-branch --match-head-commit "$HEAD_SHA" >"$TMP/merge.log" 2>&1; then
      merged=true
      for _ in 1 2 3 4 5; do   # the merge commit can take a moment to appear in the API
        merge_sha="$(gh pr view "$PR_NUMBER" --repo "$REPO" --json mergeCommit --jq .mergeCommit.oid 2>/dev/null || true)"
        [ -n "$merge_sha" ] && break
        sleep 3
      done
      echo "**Merged automatically.** The native build and the x86_64 emulator smoke tests passed and the safety policy was satisfied." >> "$body"
    else
      echo "All checks passed, but the automatic merge was refused: \`$(tr '\n' ' ' < "$TMP/merge.log")\`. Merge it by hand (branch protection may require a review)." >> "$body"
    fi
  else
    echo "**All checks passed.** Automatic merge is off for this run, so merge it by hand when you are happy." >> "$body"
  fi
else
  {
    echo "**Not merged automatically.** A person has to look at this:"
    echo
    for p in "${problems[@]}"; do echo "- $p"; done
  } >> "$body"
fi

cat "$body"
gh pr comment "$PR_NUMBER" --repo "$REPO" --body-file "$body" >/dev/null 2>&1 || echo "warning: could not post the PR comment" >&2
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "merged=$merged" >> "$GITHUB_OUTPUT"
  echo "merge_sha=$merge_sha" >> "$GITHUB_OUTPUT"
fi
exit 0
