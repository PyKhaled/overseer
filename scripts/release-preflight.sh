#!/usr/bin/env bash

set -euo pipefail

script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "$script_directory/.." && pwd)"
cd "$project_root"

release_tag="${1:-}"
semver_pattern='^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-((0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(\.(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?$'

if [[ ! "$release_tag" =~ $semver_pattern ]]; then
  printf 'Usage: %s vMAJOR.MINOR.PATCH\n' "$0" >&2
  exit 2
fi

for command_name in git gh make; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'Error: required command is not installed: %s\n' "$command_name" >&2
    exit 1
  fi
done

if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
  printf 'Error: the worktree is not clean. Commit or stash changes first.\n' >&2
  git status --short >&2
  exit 1
fi

printf 'Checking GitHub authentication...\n'
gh auth status >/dev/null

printf 'Fetching origin/main and release tags...\n'
git fetch origin main --tags

if git show-ref --verify --quiet "refs/tags/$release_tag"; then
  printf 'Error: tag %s already exists.\n' "$release_tag" >&2
  exit 1
fi

release_commit="$(git rev-parse --verify origin/main)"
bash "$script_directory/check-release-version.sh" "$release_tag" "$release_commit"
run_details="$(
  gh run list \
    --workflow ci.yml \
    --branch main \
    --commit "$release_commit" \
    --limit 1 \
    --json status,conclusion,url \
    --jq '(.[0] // empty) | "\(.status)|\(.conclusion // "")|\(.url)"'
)"

if [[ -z "$run_details" ]]; then
  printf 'Error: no CI run found for origin/main at %s.\n' "$release_commit" >&2
  exit 1
fi

IFS='|' read -r ci_status ci_conclusion ci_url <<< "$run_details"
if [[ "$ci_status" != "completed" || "$ci_conclusion" != "success" ]]; then
  printf 'Error: CI is %s/%s for origin/main.\n%s\n' \
    "$ci_status" "${ci_conclusion:-pending}" "$ci_url" >&2
  exit 1
fi

printf 'CI passed: %s\n' "$ci_url"
if [[ "$(git rev-parse HEAD)" != "$release_commit" ]]; then
  printf 'Error: check out origin/main before running release preflight.\n' >&2
  exit 1
fi
printf 'Running the complete local quality gate...\n'
make check

printf '\nPreflight passed for %s.\n' "$release_tag"
printf 'Release target: origin/main at %s\n' "$release_commit"
