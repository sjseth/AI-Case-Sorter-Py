---
name: qt-ui-debugging
description: Debugging playbook for the PySide6 UI (sorter/qtui) — crash classes we have actually hit, their symptoms, and the proven fixes. Load when a qtui test segfaults, crashes at a distance, fails only on one platform/CI leg, or when writing new Qt widget tests.
---

# Qt UI debugging playbook

Hard-won findings from the qtui port (2026-08). Every rule here was paid
for with a multi-run CI saga — check this list before theorizing from
scratch.

## Crash classes we have hit, in symptom order

### Segfault in a LATER test's `processEvents()` (crash at a distance)
The pump delivers everything queued since the last pump, so the test that
crashes is almost never the test that caused it. Known causes, in order of
likelihood:

1. **Unbound `QTimer.singleShot`.** A deferral without a context object
   fires against a destroyed C++ widget. Rule: ALWAYS
   `QTimer.singleShot(ms, owner_qobject, callback)` — the context argument
   makes Qt drop the callback with the owner. Verified: a destroyed context
   drops the callback (PySide6 6.11).
2. **Signals connected to lambdas/closures on short-lived widgets.** A
   QThread/worker signal connected to a lambda executes even after the
   widget died. Rule: connect to bound methods of the receiver so the
   connection dies with it (see dialog_share_model.py).
3. **pytest-cov instrumentation** — see below; check this before blaming
   any specific test.

### Segfault only WITH coverage, full suite only, position varies with noise
pytest-cov's tracer corrupts something under a large PySide6 suite. Pinned
by experiment (2026-08-14): reverting/re-adding one unrelated QAction
flipped the crash; every coverage core (ctrace/sysmon) and branch on/off
crashed; uninstrumented never crashed, 650+ tests, both platforms.
Resolution: `tests/unit/qtui/conftest.py` applies pytest-cov's `no_cover`
marker to the whole directory. Do NOT re-enable coverage for qtui; do NOT
burn time bisecting "the crashing test" — the crash point is allocation
noise. To confirm this class: run the same suite with `--no-cov`; if it
passes every time, it's this.

### Windows-only access violation (0xc0000374) between tests
Forced `gc.collect()` between tests finalizes shiboken wrappers over
half-torn-down C++ trees. There is no forced-gc fixture anywhere (the
repo-wide one went with the Tk UI); what qtui's conftest runs in teardown is
a **DeferredDelete-only flush** (`sendPostedEvents(None,
QEvent.DeferredDelete)`) — never a generic `processEvents()` (delivering
arbitrary events into half-torn windows segfaulted Linux when tried). CI
additionally runs one pytest process per test module (build.yml) as a
bulkhead, on both platforms; keep it.

### Zombie timers from closed windows
`closeEvent` must stop every timer the window owns (`_bus_timer`,
`_preview_timer`); the test window factory calls `close()` +
`deleteLater()` and closes the SQLite DB, or later tests' pumps tick dead
windows and finalizers warn.

## Offscreen layout quirks (tests fail, nothing crashes)

- Only the FIRST `show()`'s layout pass is trustworthy for item geometry
  (`visualItemRect` caches survive later resizes). Set splitter sizes /
  window size BEFORE the first show. A model change (add/remove items)
  does relayout; a pure resize may not. Scrollbar ranges DO update on
  resize even when item rects are stale.
- Hidden widgets don't consume their stretch factor: if a layout is
  [content(1), bar] and content hides, surplus splits between remaining
  items and the bar floats mid-panel. Give the visible alternative the
  same stretch (history_view.py empty_label).
- A QVBoxLayout with insufficient vertical budget OVERLAPS fixed-size
  widgets instead of scrolling — geometry assertions then measure the
  overlap. Resize the test window generously (850+ for pages holding the
  480→320px crop panel).

## Test discipline

- **Never hard-code pixel constants that depend on fonts.** Windows CI
  fonts are wider than Linux: rows-per-column, elision points, column
  fits all differ. Derive everything from measured geometry
  (`sizeHintForColumn`, one cell's `visualItemRect`, `fontMetrics`), then
  compute counts/widths from it. Exact `hbar.maximum() == 0` is
  unattainable on Windows (a few px of contents overhang persists when
  everything fits) — assert a small tolerance relative to a cell.
- Pump with `drain_until(window, predicate)` (bus only) — never sleeps,
  never bare `processEvents()` loops for bus work.
- Anything that would open a native modal is an instance-attribute seam
  (`win.notify`, `confirm`, `ask_save_path`…) a test replaces. A direct
  QMessageBox in a handler is untestable offscreen.
- Assert on the terminal observable (what the user sees: notify titles,
  widget state), not on intermediate worker-thread writes — the latter
  races on Windows.

## Ownership/lifetime rules

- `menuBar().addMenu()` results must be kept (`self.menus[...]`) —
  `action.menu()` returns a Python-owned wrapper that deletes the C++
  menu when collected.
- `run_worker` reply topics are one-shot and unsubscribed on delivery
  (monotonic tokens, never `id(fn)` — addresses recycle).
- Modals raised from bus handlers are queued out of the drain with a
  context-bound `singleShot(0, self, …)`.

## Reproducing CI locally

- Suite, CI-parity: `DISPLAY= WAYLAND_DISPLAY= QT_QPA_PLATFORM=offscreen
  PYTHONPATH=src uv run --no-sync pytest tests/unit/qtui -q`
- A clean-tree repro beats theorizing: `git archive <sha> | tar -x -C
  <scratch>` + the project venv's python, then bisect by module list,
  then by diff hunk. The 2026-08-14 saga was cracked exactly this way.
