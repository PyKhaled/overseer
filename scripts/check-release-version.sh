#!/usr/bin/env bash

set -euo pipefail

script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "$script_directory/.." && pwd)"
cd "$project_root"

release_tag="${1:?Usage: check-release-version.sh vMAJOR.MINOR.PATCH [git-ref]}"
if [[ -n "${2:-}" ]]; then
  version_source="$(git show "$2:overseer/version.py")"
else
  version_source="$(cat overseer/version.py)"
fi
application_version="$(printf '%s\n' "$version_source" | sed -n 's/^__version__ = "\([^"]*\)"$/\1/p')"
if [[ -z "$application_version" || "$release_tag" != "v$application_version" ]]; then
  printf 'Error: release tag %s must match application version v%s.\n' \
    "$release_tag" "$application_version" >&2
  exit 1
fi
printf 'Application version matches %s.\n' "$release_tag"
