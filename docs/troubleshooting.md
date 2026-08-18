# Troubleshooting

Problems with the **application**. Mechanical faults — jams, a feed wheel
that overruns its homing sensor, motors losing position — belong to the
machine and its own documentation; what this page can do for those is show
you the traffic ([Serial Monitor](guide/GUIDE.md#serial-monitor)) and the
settings that drive them ([Settings → Serial](guide/GUIDE.md#serial)).

Before reporting anything, take **Help → Export support package…** — it
collects your version, platform, active model, run options and every settings
page into one report, with the API key reported only as "set" or "not set" and
paths relative to the data folder. Attach the logs described below too.

## Where the logs are

Both live in the data folder's `logs/` directory — on Windows that is
`%LOCALAPPDATA%\CaseSorter\logs`, on Linux and macOS
`~/.local/share/CaseSorter/logs`. They answer different questions:

- **`casesorter.log`** — the application itself. It keeps several launches of
  history (rotating at 1 MB, three files back), so a problem you only got
  round to reporting after restarting a few times is still in there. This is
  the one to attach to a bug report.
- **`launch.log`** — the launcher: finding Python, syncing dependencies,
  applying an update, and anything the app printed. Rewritten on every start,
  with the previous one kept as `launch.prev.log`.

To collect more detail, start the app with `CASESORTER_LOG_LEVEL=DEBUG` set in
the environment.

## Nothing happens when I start the app

Read `launch.log` — it holds everything from the launcher onwards, including
the traceback. On Windows the console closes with the process and takes the
error with it, which is what "nothing happened" usually is.

The first launch after an install or an update also syncs dependencies, which
takes minutes rather than seconds. That is in the log too.

## The window won't open on Linux

If the log ends with Qt failing to load the **xcb** platform plugin, install
`libxcb-cursor0` (`xcb-util-cursor` on Fedora and Arch) — see
[Install](install.md#linux-and-macos) for the full table. Without it Qt falls
back to Wayland, where floating panels can't be moved or resized; without
`libGL` or `glib` the app can't start at all.

## The sorter is not detected

Almost always something else is holding the serial port, or the port never
appeared:

- **Close the Arduino IDE.** Its serial monitor keeps the port open and the
  app can't have it. Any other terminal on the port does the same.
- **Press Refresh ports** on [Settings →
  Serial](guide/GUIDE.md#serial) — a board plugged in after the app started
  isn't in the list until you rescan.
- **Re-seat the USB cable**, try a different port, and bypass any USB hub.
- **On Linux, check permissions.** A port you can see but can't open is
  usually group membership — `dialout` on Debian/Ubuntu, `uucp` on Arch — and
  the change takes effect on next login.
- **Watch the handshake.** Open the [Serial
  Monitor](guide/GUIDE.md#serial-monitor); it replays what the failed connect
  attempt actually said, which distinguishes "no port" from "port opened,
  board didn't answer".

To keep working meanwhile, choose the **Emulated** port: everything except
moving brass runs.

## The camera preview is black

- Press **Detect / refresh** on [Settings →
  Camera](guide/GUIDE.md#camera), pick the device again and press **Apply** —
  the selection isn't live until Apply.
- **Close anything else using the camera.** One process at a time.
- **Try a lower resolution.** Not every mode a camera advertises actually
  streams.
- On the Sort dashboard, **Show live camera** is off by default and fetches
  no frames while off — an empty spot there is not a fault.

## The crop is wrong, or the case isn't found

Tune it against a single frame on [Settings → Image
Processing](guide/GUIDE.md#image-processing): **Capture** once, then adjust —
every change re-processes that same frame.

Start with the **minimum and maximum case radius**, which bracket the size of
the case in pixels; a case outside that bracket is simply not detected. If
primer markings are confusing the classifier, **hide the primer**.

Remember these values belong to the **active model**, so a model trained with
one crop and run with another will classify badly even though nothing looks
broken. Tune once, then train.

## Everything lands in the Catch-All

Slot 0 takes anything unrouted, unrecognised, or below the confidence floor.
In order of likelihood:

1. **The headstamps aren't routed.** Click a slot card and tick them —
   nothing is assigned on a fresh layout.
2. **The confidence floor is too high** for this model. It's in **⚙ Run
   options**; the per-case confidence shown under the crop tells you what the
   model is actually scoring.
3. **The crop doesn't match what the model was trained on** — see above.
4. **The model is the wrong one.** Check which row reads **● ACTIVE** on
   [Models](guide/GUIDE.md#models).

## Start is greyed out or refuses

It always says which one it is: no board connected, the API key or model name
unset in AI Config mode, the active model's checkpoint missing, PyTorch not
installed yet for a local model, or an unread [moderator
note](guide/GUIDE.md#community-model-notices).

A **missing checkpoint** means the model row exists but its `.pth` doesn't —
usually a community share that carried images only, or a data folder that
moved. The app will not quietly fall back to sending your images to an HTTP
server; pick a different model, or train this one.

## Training fails or the machine runs out of memory

- **PyTorch isn't installed** — the app offers to install it when you press
  Start training.
- **Out of memory:** lower **batch size** or **image size** in **Training
  settings…**, and close other applications. Training on CPU works and is
  slow; a GPU below compute capability 8.0 is not used.
- **The first training run may need the network** to fetch pretrained
  weights.
- **Label mismatches:** training images are named `{label}__{ticks}.jpg` and
  the label is case-sensitive. Renaming files by hand outside the app is how
  a class ends up split in two — use **Images…** on the Models screen to
  reclassify instead.
- The live console is the only place the run reports to; closing it cancels
  the run, after asking.

## The app says a model update is available every time

The status-bar notice is the *model's* publisher releasing a new version, not
an app update. **Update now** installs it in place and keeps your slot
assignments, sorting templates and the name you gave it. Dismissing it is
fine — it returns next time you open the Sort screen.

## Resetting

Deleting the data folder (**File → Open data folder**) resets the app to a
fresh install — settings, models, training images and all. There is no undo,
so export anything you want to keep first.
