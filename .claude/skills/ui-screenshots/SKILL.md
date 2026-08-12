---
name: ui-screenshots
description: Capture before/after screenshots of the Tkinter app and attach them to a GitHub issue or PR. Use whenever a change touches src/sorter/ui/ — every UI issue and PR gets before/after images. Also use when asked to screenshot the app, illustrate a UI change, or attach an image to an issue or PR.
---

# Before/after screenshots for UI changes

Every change under `src/sorter/ui/` gets before/after screenshots on its issue
and/or PR. A UI change described only in prose can't be reviewed — the reviewer
has to build it to see it.

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

Drive the real `MainWindow`; don't hand-build widgets, or the screenshot stops
being evidence. Two things this app needs:

- **Point `CASESORTER_DATA_DIR` at a throwaway directory.** Otherwise you
  photograph your own models and headstamps and publish them.
- **Stub `serial_broker.list_serial_ports` to `[]` before constructing
  `MainWindow`.** It auto-connects on startup and will probe whatever is
  plugged into the machine, burying the traffic you staged. (First attempt at
  these shots came out reading "82 earlier lines" of this machine's real ports.)

`MainWindow.run()` subscribes the `serial/*` topics to the log and then enters
the mainloop, so a harness that can't call it has to repeat that wiring itself.

Grab a whole toplevel by its X window id, and crop for anything smaller:

```python
subprocess.run(["import", "-window", hex(widget.winfo_toplevel().winfo_id()), out])
```

Grabbing a child widget's own id fails for anything inside a `ScrollableFrame`
— it isn't independently viewable — so capture the toplevel and crop to
`winfo_rootx/rooty/width/height` with `convert -crop`. Scroll the widget into
view first (`ScrollableFrame._canvas.yview_moveto(1.0)`).

For the "before" image, export the base commit rather than switching branches:

```bash
git archive main --prefix=main-tree/ | tar -x -C <scratch>/
PYTHONPATH=<scratch>/main-tree/src python capture.py <out> before
```

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
