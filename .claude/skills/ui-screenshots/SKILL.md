---
name: ui-screenshots
description: Capture before/after screenshots of the PySide6 app and attach them to a GitHub issue or PR. Use whenever a change touches src/sorter/ui/ — every UI issue and PR gets before/after images. Also use when asked to screenshot the app, illustrate a UI change, or attach an image to an issue or PR.
---

# Before/after screenshots for UI changes

Every change under `src/sorter/ui/` gets before/after screenshots on its issue
and/or PR. A UI change described only in prose can't be reviewed — the reviewer
has to build it to see it.

## Before you upload — mandatory

An upload lands in GitHub-owned storage with **no deletion mechanism**: deleting
the comment that references it does not remove it. Treat every upload as
permanent, and get the user's explicit go-ahead first. Dragging a file into the
browser gave that for free; running from the CLI does not.

- **Offer to open the file in their viewer.** Reading a file renders it for the
  agent, not for the user — a terminal may show nothing, and a description of an
  image is not the image. `xdg-open` on Linux, `open` on macOS, `start` on
  Windows. Video especially: a described clip is the least reviewable of all.
- **Check what is in frame.** No credentials, no customer data, no serial ports
  or file paths that identify the machine, and no models or headstamps that
  aren't staged fixtures. The capture rules below exist mostly for this reason.

The tool refuses a file whose leading bytes don't match its extension, so a
renamed non-image can't be published unread. That is a backstop, not the check —
it cannot tell whether a genuine screenshot contains something private.

## Attaching

```bash
tools/gh_attach_images.py shots/before-statusbar.png shots/after-statusbar.png
```

It prints one markdown line per file. Paste them into a body and apply it with
`gh pr edit <n> --body-file …` or `gh issue comment <n> --body-file …`.

The result is a **real GitHub attachment** — the same
`github.com/user-attachments/assets/<uuid>` you'd get by dragging the file into
the comment box. Nothing is committed to the repo.

`gh` has no command for this, but the endpoint behind it takes an ordinary `gh`
token: `POST https://uploads.github.com/user-attachments/assets` with
`Authorization: Bearer`. Images and video only, on repos the token can push to.
See the tool's docstring for the details and the credit.

Two things that look wrong and aren't:

- **`curl`ing the printed URL gives 404.** It's a handle, not a CDN path.
  GitHub swaps it for a short-lived signed URL at render time — including for
  logged-out visitors — so the image displays for everyone.
- **The asset is orphaned until referenced.** It only becomes reachable once a
  body or comment citing it is saved.

Do **not** reach for a branch of committed PNGs, and do **not** use release
assets: `sorter/update/updater.py` lists every release for the in-app version
picker and its `_TAG_RE` accepts a tag like `pr-78-images`, so a throwaway
release would offer itself to users as an installable version.

## Capturing

Drive the real `QtMainWindow`; don't hand-build widgets, or the screenshot stops
being evidence. It renders fully under `QT_QPA_PLATFORM=offscreen`, so there is
no X display, no window id and no ImageMagick in this path.

- **Point `CASESORTER_DATA_DIR` at a throwaway directory.** Otherwise you
  photograph your own models and headstamps and publish them.
- **Construct with `auto_connect=False`**, or the shell opens a camera and
  probes whatever is plugged into this machine. (An early attempt published a
  monitor reading "82 earlier lines" of real ports.)

```python
import os, tempfile
from pathlib import Path

scratch = Path(tempfile.mkdtemp())
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["CASESORTER_DATA_DIR"] = str(scratch / "data")

from PySide6.QtWidgets import QApplication
from sorter.data.config import Config
from sorter.data.db import Database
from sorter.ui.app import QtMainWindow

app = QApplication.instance() or QApplication([])
db = Database(scratch / "casesorter.db")
db.ensure_initialized()
win = QtMainWindow(Config(db).load(), auto_connect=False)
win.resize(1280, 800)
win.show()
app.processEvents()
win.grab().save("shot.png")
```

- **Any widget grabs on its own** — `win.statusBar().grab()`, a page, a dialog,
  `win.serial_dock.widget().grab()`. No cropping, and none of the old
  "not independently viewable" problem.
- **Call `processEvents()` after anything you want in the frame.**
- **Drop the serial dock with `win.serial_dock.toggleView(False)`** when a page
  is the subject: it opens by default and takes the lower half. `hide()` is the
  wrong call — it leaves the tab bar and the reserved space behind and the page
  never expands. `CDockWidget` has no `closeDock()`.
- **Stage serial traffic on the bus**, no hardware needed — the `serial/*`
  topics are wired by `_attach_serial_listeners` when a broker attaches, but the
  monitor renders whatever the bus carries:

  ```python
  win.bus.post("serial/tx", "version")
  win.bus.post("serial/rx", "CS7.2 Firmware V1.7")
  win.bus.drain()  # the drain QTimer isn't running without an event loop
  app.processEvents()
  ```

For the "before" image, export the base commit rather than switching branches:

```bash
git archive main --prefix=main-tree/ | tar -x -C <scratch>/
PYTHONPATH=<scratch>/main-tree/src uv run --no-sync python capture.py <out> before
```

`uv run --no-sync`, not a bare `python`: the capture needs PySide6 from the
venv, and a bare `uv run` would sync and install the project (see CLAUDE.md §2).

## What makes a useful pair

- **Same window size, same theme, same staged data.** A pair that differs in
  two ways at once shows nothing.
- **Photograph the claim.** If the PR says a failed probe is now diagnosable,
  the "after" must contain the probe lines and the "before" must visibly lack
  them.
- **Don't hide the trade-off.** If a change costs something elsewhere in the
  frame, leave it in the shot and say so in the body.

Put pairs in a two-column table so they're read side by side:

```markdown
| Before | After |
|---|---|
| ![before](https://github.com/user-attachments/assets/…) | ![after](https://github.com/user-attachments/assets/…) |
```
