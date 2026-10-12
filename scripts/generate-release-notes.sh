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

for command_name in git gh; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'Error: required command is not installed: %s\n' "$command_name" >&2
    exit 1
  fi
done

gh auth status >/dev/null
git fetch origin main --tags

if git show-ref --verify --quiet "refs/tags/$release_tag"; then
  printf 'Error: tag %s already exists.\n' "$release_tag" >&2
  exit 1
fi

repository="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
release_commit="$(git rev-parse --verify origin/main)"
bash "$script_directory/check-release-version.sh" "$release_tag" "$release_commit"

printf '# Generated notes for %s\n\n' "$release_tag"
gh api \
  --method POST \
  "repos/$repository/releases/generate-notes" \
  -f "tag_name=$release_tag" \
  -f "target_commitish=$release_commit" \
  --jq .body
