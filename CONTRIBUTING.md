# Contributing to AI Case Sorter

Thanks for your interest in improving the AI Case Sorter! This repository is the
cross-platform **desktop application**. The hardware, firmware, and the optional
local model server live in separate repositories (linked from the
[README](README.md)).

By participating in this project you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md).

## Ways to contribute

- Report bugs or request features via [GitHub Issues](https://github.com/sjseth/AI-Case-Sorter-Py/issues/new/choose)
  — pick the bug report or feature request form so we get what we need to reproduce it.
- Ask usage questions in the project **Discord**, not Issues — the invite is on the
  [project page](https://www.reloadingrecipes.com/HeadstampSorter) (free account).
- Improve documentation — including [`CLAUDE.md`](CLAUDE.md), the architecture map.
- Fix bugs or add features via pull requests (the PR template lists the checklist).
- Report **security** issues privately — see [`SECURITY.md`](SECURITY.md). Please
  do not file security problems as public issues.

## Development setup

Requires **some Python 3** already on your machine — new enough to run
`bootstrap.py` itself, which is not a strict requirement, since its whole job
is to provision the app's *actual* interpreter separately via
[uv](https://docs.astral.sh/uv/). If you don't have uv yet, the launch
scripts install it automatically (into a project-local `.uv/`, not
system-wide) on first run.

**Linux / macOS**
```bash
git clone https://github.com/sjseth/AI-Case-Sorter-Py.git
cd AI-Case-Sorter-Py
./start.sh
```

**Windows:** run `start.bat`.

**Prefer to drive `uv` yourself?**
```bash
uv sync              # dependencies + dev tools (pytest, ruff) from uv.lock
uv run python src/sorter/__main__.py
```
`uv sync`/`uv run` resolve against the committed `uv.lock`, so this is
deterministic — no separate "install deps" step to remember or forget.

Local training/inference additionally needs PyTorch (optional):
```bash
uv sync --extra ml      # torch + torchvision
```

### No hardware? Use the emulator

In the **Serial** tab, choose the **`Emulated`** port to exercise the run loop
and the UI without a physical sorter attached.

## Running the tests

```bash
uv run pytest
```

Split into `tests/unit/` and `tests/integration/` — the latter is the two
files that shell out to a real external tool (`uv build`, `git-cliff`)
instead of a synthetic fixture, each carrying `@pytest.mark.integration` and
skipping individually if that tool isn't installed. `pytest -m "not
integration"` skips them outright for a faster inner loop; plain
`pytest`/CI runs everything.

Around 500 tests cover the non-UI logic; please run them before opening a PR.
The torch-dependent tests skip automatically when PyTorch isn't installed. A
handful of tests do exercise real Tk widgets in-process (`test_tab_train_sort.py`,
`test_feedback_ui.py`, `test_dialog_install_torch.py`) — driving them with direct
calls and asserting resulting state, not through any external UI-automation tool
(there isn't a Playwright equivalent for Tkinter; a browser exposes a
remote-debugging protocol an external driver attaches to, Tkinter doesn't). Most
of the UI is not covered this way, though, so smoke-test UI changes by running the
app. CI (`.github/workflows/build.yml`) runs the same suite across
a Python version matrix on every push and PR — treat a red CI run the same as
a local test failure, not as something to wait out.

## Coding guidelines

- **Read [`CLAUDE.md`](CLAUDE.md) first** — it maps the architecture (event bus,
  threading model, persistence, UI tabs). **Keep it current:** if you add a tab,
  change the data model, or move a subsystem boundary, update `CLAUDE.md` in the
  same change.
- **Lint and format with [ruff](https://docs.astral.sh/ruff/)** before pushing:
  ```bash
  uv run ruff check .            # lint
  uv run ruff format .           # format
  ```
  CI runs both (`.github/workflows/lint.yml`) and fails the PR check if either
  would change anything. Match the style of the surrounding code beyond what
  ruff enforces too — naming, type hints, comment density.
- **Type-check with [ty](https://docs.astral.sh/ty/)** before pushing:
  ```bash
  uv run ty check
  ```
  The tree is at **zero diagnostics** and CI (the `ty` job in `lint.yml`) fails
  the PR check on any new one — so a finding you introduce is yours to fix, not
  a pre-existing backlog to ignore. Fix the code rather than silencing the
  checker; a `# ty: ignore[rule]` needs a comment saying why the finding is
  genuinely unfixable. The ones already in the tree are all the same two cases:
  optional dependencies that are absent by design (torch/torchvision are the
  `[ml]` extra; pygrabber/comtypes are Windows-only) and gaps in opencv's
  bundled stubs.
- **Threading rule:** never touch Tk widgets off the main thread. Do blocking
  work in a worker/daemon thread and post results through the event bus.
- **PyTorch is optional and lazily imported** — guard any torch use and add it
  under `[project.optional-dependencies] ml`, not the base dependency list.
- Keep SQL **parameterized**; never build SQL by string interpolation.
- Never commit anything under `data/` (it's gitignored and holds local state,
  including credentials).
- Preserve interop with the legacy Windows app where the code calls it out
  (filename conventions, manifest key spellings, exact serial command strings).

## Pull request flow

1. Branch off `main`.
2. Keep PRs focused and write clear commit messages.
3. Run `uv run pytest`, `uv run ruff check .`, and `uv run ty check` (and
   smoke-test UI changes) before opening the PR — CI runs all three, but
   catching it locally is faster.
4. Describe what changed and why in the PR.

### Commit messages and PR titles: Conventional Commits

Both commit subjects and the PR title must follow
[Conventional Commits](https://www.conventionalcommits.org/):
`type(optional-scope): summary`, e.g. `fix(camera): handle missing device on
enumerate`. A GitHub Action checks the PR title on every push
(`.github/workflows/check-semantic-pr.yml`); allowed types are `feat`, `fix`,
`refactor`, `chore`, `security`, `revert`, `test`, `docs`, `perf`, `style`,
`ci`, `build`. Use `type!:` or a `BREAKING CHANGE:` footer for a breaking
change.

This isn't just a style preference: **commit type drives both the changelog and
the version number**, automatically. `git-cliff` groups commits into changelog
sections by type, and computes the next version from them. A `fix:` that should
have been a `feat:` produces a wrong changelog entry *and* a wrong version
bump. Commits that don't parse as Conventional Commits are dropped from the
changelog entirely.

The mapping:

| Commit | While `0.x` (today) | From `1.0.0` on |
|---|---|---|
| `fix:` | patch | patch |
| `feat:` | minor | minor |
| `type!:` / `BREAKING CHANGE:` | **minor** | major |

**No commit message can take the project to 1.0.0.** git-cliff would do that
by default — a single `feat!:` at `0.1.0` bumps straight to `1.0.0` — which
would let an ordinary breaking change during pre-1.0 development silently
declare the API stable. `cliff.toml` sets `breaking_always_bump_major = false`
to prevent it, so a breaking change below 1.0.0 bumps the minor instead
(`0.1.0` + `feat!:` → `0.2.0`). Releasing 1.0.0 is a deliberate act: pass
`version` explicitly to the Release workflow. Behaviour at 1.0.0 and above is
ordinary semver, unaffected.

Verified against git-cliff 2.13.1 and pinned in `tests/integration/test_cliff_config.py`,
which runs the real binary in CI.

## Releasing

Maintainers only, and fully automated — see [`RELEASING.md`](RELEASING.md) for
the detail. The short version:

- **Nothing to bump by hand.** The git tag is the single source of truth; the
  version is derived from it at build time (hatch-vcs). There is no version
  string in the source to edit.
- **Preview by dry-running the release.** The Release workflow defaults to
  `dry-run`, which shows the version a release would get right now and the
  changelog it would generate — without pushing a tag or creating anything.
  It's the fastest way to notice a mistyped commit type before it becomes a
  wrong version bump.
- **Cutting a release** is the manual **Release** workflow (Actions → Release →
  Run workflow). It defaults to a dry run, auto-detects the version from
  Conventional Commits if you leave `version` empty, and only ever opens a
  **draft** release — publishing is always a deliberate human click.

## Contributions & licensing (DCO)

This project is licensed under **GPL-3.0-or-later**. By submitting a contribution
you agree that it is provided under that same license (inbound = outbound).

We use the **Developer Certificate of Origin (DCO)**: sign off each commit to
certify you have the right to submit it under the project license.

```bash
git commit -s -m "Your message"
```

This appends a `Signed-off-by: Your Name <you@example.com>` line to the commit.
The full DCO text is at <https://developercertificate.org>.

Forgot on a commit you have already made?

```bash
git commit --amend -s                 # the most recent commit
git rebase --signoff origin/main      # every commit on your branch
git push --force-with-lease
```

Enforced by the [DCO app](https://github.com/apps/dco), which posts a `DCO`
status on each pull request and skips bot and merge commits.
