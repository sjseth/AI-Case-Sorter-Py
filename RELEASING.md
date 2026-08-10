# Releasing AI Case Sorter

## How it works

1. Merge whatever should be in the release to `main`. Commit subjects must follow
   [Conventional Commits](CONTRIBUTING.md#commit-messages-and-pr-titles-conventional-commits) --
   `check-semantic-pr.yml` already enforces that on every PR, so this should already be true.
2. **Preview by running Release with `dry-run` left on** (it defaults to on). It resolves the
   version, generates the changelog, and shows both in the job summary alongside the raw
   commits they were derived from -- without pushing a tag or creating anything. That is the
   sanity check: anything in the commit list but missing from the notes was dropped for a
   reason (an unconventional subject, or a skipped `ci:`/`build:` type), which is how a
   mistyped commit type gets caught before it becomes a wrong version bump.
3. When ready, run the same **Release** workflow with `dry-run` unchecked
   (Actions tab -> Release -> Run workflow):
   - `version`: **leave empty to auto-detect.** git-cliff computes the next version from the
     Conventional Commits since the last tag -- a `feat:` bumps the minor, a `fix:` the patch,
     a `!`/`BREAKING CHANGE:` the major. **While the project is pre-1.0 a breaking change
     bumps the minor instead** (`0.1.0` + `feat!:` -> `0.2.0`), so no commit message can push
     the project to 1.0.0 on its own -- see CONTRIBUTING.md. Passing a version explicitly
     always overrides auto-detection.
   - `ref`: branch to release from. Empty means the branch you dispatched on. **Must be
     `main`, `hotfix/*`, or `release/*`** — anything else is rejected before a tag is
     created. A raw commit SHA is rejected for the same reason: release from the branch
     containing it instead, so the release is traceable to a branch.
   - `force`: only needed when you pass a `version` that **disagrees** with the auto-detected
     one. Without it, a mismatch is a hard error naming both numbers -- that's the guard that
     catches "meant 0.3.0, typed 0.2.0" before it becomes a tag.
   - `dry-run`: **defaults to on.** Shows the resolved version and generated notes in the job
     summary without pushing a tag or creating anything. Uncheck it to actually release.
4. The workflow resolves the version and generates changelog notes from commits since the last
   tag via [git-cliff](https://git-cliff.org/) (`cliff.toml`), then puts both in the job
   summary. **A dry run stops here** -- nothing else has happened.
5. Lint and the full test matrix run, so what you approve next is already green.
6. **The run pauses for approval.** The job that tags is bound to the `stable` environment, so
   it sits pending until a required reviewer approves it on the run page. There is no draft to
   publish by hand -- this is the confirmation step.
7. On approval it tags the ref, builds, checks the artifacts (built version matches the tag;
   the sdist is named and stamped as the updater expects), pushes the tag, and creates the
   GitHub Release **as a prerelease with its artifacts already attached**.
8. The Windows installer is run for real against that exact tag, and the resulting install has
   to carry that exact version.
9. Only then is the prerelease promoted to the latest release -- one flag flip, assets already
   in place.
10. Finally TestPyPI, then PyPI. Both are gated on the repo variable `PYPI_PUBLISH_ENABLED`,
    unset by default, and each waits on its environment's reviewer.

### Why a prerelease rather than a draft

The release has to be verifiable before anyone can reach it, and those two pull in opposite
directions. A draft is invisible to the verification too: `/releases/tags/<tag>` returns 404 to
the unauthenticated fetch the installer makes, so there would be nothing to install.

A prerelease is readable by exact tag, but `/releases/latest` excludes it -- and that is the
endpoint both `sorter/update/updater.py` and `install-windows.ps1` use. So the release is fully
testable while staying invisible to every real client until step 9.

If verification fails the release stays a prerelease: nobody was served it, and PyPI never ran.
The tag and prerelease are left in place for a human to delete or supersede -- fixing forward
with a patch version is usually cleaner than deleting a pushed tag.

This also closes a window that used to exist: artifacts were attached *after* publication, so
for a short time (26 seconds, measured on 1.0.0) `/releases/latest` returned a release with no
matching sdist, and clients silently fell back to the source archive and installed something
reporting `0.0.0+unknown`.

## Versioning

**There is no version to bump by hand.** The tag is the single source of truth: hatch-vcs
derives the version from it at build time (`pyproject.toml`'s `[tool.hatch.version] source =
"vcs"`), writing `src/sorter/_version.py`, which `src/sorter/__init__.py` reads. Don't edit a version
string anywhere -- there isn't one to edit.

That's the point of the setup: the old arrangement had a static `__version__` that had to be
bumped in the same commit as the tag, and it drifted in practice. When this was written, the
app's window title said `v2.0.1` while `__version__` said `0.1.0` and no git tag existed at
all -- three different answers to "what version is this?", one of them shown to users.

How the version reaches a user who never has `.git`:

- **A downloaded release** gets `ai_case_sorter-<tag>.tar.gz` -- the project's own sdist,
  the same file `uv build` already produces for every push to `main`, not a separately built
  artifact. hatch-vcs's build hook stamps `src/sorter/_version.py` into it automatically.
  `sorter/update/updater.py` looks for that asset by exact name.
- **A pip/uv install** (if PyPI is ever enabled) reads it from package metadata.
- **A plain `git clone` that was never built** falls back to `0.0.0+unknown`. Expected -- it's
  a contributor path, not a release path.

See `CLAUDE.md` §7 for why `bootstrap.py` passes `--no-install-project`/`--no-sync`: without
them, a launch from a git-less release would silently overwrite the correct baked version.

### Getting to 1.0.0

Auto-detection will never propose it. While the project is at 0.x, `cliff.toml`'s
`breaking_always_bump_major = false` makes a `!`/`BREAKING CHANGE:` commit bump the *minor*
(`0.1.0` -> `0.2.0`) rather than jumping to `1.0.0` -- otherwise a routine breaking change
during pre-1.0 development would declare the API stable as a side effect of a commit message.

So 1.0.0 is cut deliberately: run the Release workflow with `version: 1.0.0` and `force: true`
(`force` is required precisely because the value disagrees with what git-cliff computed --
that guard is what makes this an explicit decision rather than a typo). From the first 1.x tag
onward the mapping is ordinary semver again, with no config change needed.

## Commit-type -> changelog section mapping

Set by `cliff.toml`, matching the types `check-semantic-pr.yml` enforces:

| Commit type | Changelog section |
|---|---|
| `feat` | Features |
| `fix` | Bug Fixes |
| `perf` | Performance |
| `refactor` | Refactoring |
| `chore` | Miscellaneous |
| `docs` | Documentation |
| `test` | Testing |
| `security` | Security |
| `style` | Style |
| `revert` | Reverts |
| `ci`, `build` | (omitted from the changelog -- internal tooling, not user-facing) |

Commits that don't parse as Conventional Commits are skipped entirely (`filter_unconventional
= true`), which is why every commit on this repo needs a properly-typed subject line.

Each line also carries `by @user`, and `in #N` when it resolves: `remote.username` comes from
the commit's author/committer email and works regardless of how a PR was merged, but
`remote.pr_number` only ever resolves for a **squash merge** -- git-cliff matches a commit's
SHA against a closed PR's `merge_commit_sha`, which a real merge commit's constituent commits
never equal. See the comment above `body` in `cliff.toml` for the mechanism.
