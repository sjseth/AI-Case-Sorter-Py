#!/usr/bin/env bash
#
# Print the next unused PEP 440 prerelease version for a base version.
#
#   next-prerelease.sh 0.5.0 rc   ->  0.5.0rc1, then 0.5.0rc2, ...
#
# git-cliff only ever emits final versions, so this is what turns its
# --bumped-version into a release candidate. A separate file rather than
# inline workflow bash because it decides a released tag, which is the one
# category of logic in this repo that is always pinned by a test
# (tests/unit/test_next_prerelease.py; cf. tests/integration/test_cliff_config.py).
#
# No separator between base and suffix: that is PEP 440's canonical spelling,
# and the only one that survives the round trip through hatchling's sdist name
# that release.yml asserts on.

set -euo pipefail

BASE="${1:?usage: next-prerelease.sh <base-version> <a|b|rc>}"
KIND="${2:?usage: next-prerelease.sh <base-version> <a|b|rc>}"

highest=0
while IFS= read -r tag; do
  n="${tag#"$BASE$KIND"}"
  # Numeric compare, so rc9 -> rc10 rather than rc9 -> rc2. The guard also
  # drops anything the glob dragged in that isn't a plain number.
  [[ "$n" =~ ^[0-9]+$ ]] || continue
  ((n > highest)) && highest="$n"
done < <(git tag --list "$BASE$KIND*")

echo "$BASE$KIND$((highest + 1))"
