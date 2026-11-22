#!/usr/bin/env bash
# Prepares a release: sets the version, dates the changelog, runs the tests, commits and tags.
# Publishing is done by GitHub Actions (.github/workflows/release.yml) through PyPI Trusted
# Publishing when the tag is pushed: no token lives on this machine or in the repository.
#
#   scripts/release.sh            next version: X.Y.ZaN -> X.Y.Za(N+1), X.Y.Z -> X.Y.(Z+1)
#   scripts/release.sh 0.2.0      explicit version
#
# Needs a clean working tree and a "## Unreleased" section at the top of CHANGELOG.md.
# Stable versions (no aN/bN/rcN suffix) are only released from main.
set -euo pipefail
cd "$(dirname "$0")/.."

INIT=src/scipq_mcp/__init__.py
current=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$INIT")

if [[ $# -ge 1 ]]; then
  new=$1
elif [[ $current =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)(a|b|rc)([0-9]+)$ ]]; then
  new="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}.${BASH_REMATCH[3]}${BASH_REMATCH[4]}$((BASH_REMATCH[5] + 1))"
elif [[ $current =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
  new="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}.$((BASH_REMATCH[3] + 1))"
else
  echo "error: cannot bump version '$current'; pass one explicitly" >&2
  exit 1
fi

if [[ ! $new =~ ^[0-9]+\.[0-9]+\.[0-9]+((a|b|rc)[0-9]+)?$ ]]; then
  echo "error: '$new' is not X.Y.Z or X.Y.Z{a,b,rc}N" >&2
  exit 1
fi
if [[ -n $(git status --porcelain) ]]; then
  echo "error: the working tree is not clean" >&2
  exit 1
fi
branch=$(git rev-parse --abbrev-ref HEAD)
if [[ $new =~ ^[0-9]+\.[0-9]+\.[0-9]+$ && $branch != main ]]; then
  echo "error: stable version $new from branch '$branch'; release stable versions from main" >&2
  exit 1
fi
if git rev-parse -q --verify "refs/tags/v$new" >/dev/null; then
  echo "error: tag v$new already exists" >&2
  exit 1
fi
if ! grep -q '^## Unreleased' CHANGELOG.md; then
  echo "error: CHANGELOG.md has no '## Unreleased' section to release" >&2
  exit 1
fi

echo "Releasing $current -> $new"
sed -i.bak "s/^__version__ = \".*\"/__version__ = \"$new\"/" "$INIT" && rm -f "$INIT.bak"
sed -i.bak "s/^## Unreleased.*/## $new — $(date +%Y-%m-%d)/" CHANGELOG.md && rm -f CHANGELOG.md.bak

uv run pytest -q

git add "$INIT" CHANGELOG.md
git commit -q -m "Release v$new"
git tag -a "v$new" -m "scipq-mcp $new"

echo
echo "Committed and tagged v$new. Pushing the tag publishes it to PyPI:"
echo "  git push origin $branch v$new"
