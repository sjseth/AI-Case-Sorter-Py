# AI Case Sorter — User Guide

A guide to the desktop client, written for the person running the machine. It covers every screen and panel you use day to
day, in the order you meet them.

## Contents

- [The window](#the-window) — the activity sidebar, the status bar and the
  menus that frame everything else.
- [Panels](#panels) — the movable side and bottom panels:
  [Serial Monitor](#serial-monitor), [Classification
  History](#classification-history), the guide you are reading, and
  [Themes](#themes-panel).
- [Sort dashboard](#sort-dashboard) — the main working screen: the current
  case, the slot cards, sorting templates and the run controls.
- [Train](#train) — capture cases, label them, and train a model from them.
- [AI Config](#ai-config) — the other way to classify: an HTTP server instead
  of a local model.
- [Models](#models) — the model library: activate, edit, import, export.
- [Community](#community) — browse and install published models.
- [Settings](#settings) — [Camera](#camera), [Serial](#serial), [Image
  Processing](#image-processing) and [Theme](#theme).
- [Getting help](#getting-help) — this guide, the [support
  package](#support-package) and [updates](#updates).

## How this guide works

This whole guide is one Markdown file, rendered two ways:

- **On GitHub**, browsing straight to `docs/guide/GUIDE.md`.
- **In the app**, in the User Guide panel (`F1`, or Help → User Guide),
  which opens this file and jumps straight to the section for whatever
  screen you're on — like [Settings → Serial](#serial).

Press `F1` again after moving to another screen and the panel follows you.
The **< Back** button at the top of the panel returns to the previous
section.

## The window

The window is the same everywhere: a column of activity buttons down the
left, your working screen in the middle, movable [panels](#panels) around
it, a menu bar on top and a status bar underneath.

### Activities

The sidebar switches between the things you do, one button each. A thin line
splits it in two: the screens that are always live above it, and the pair
that follows the active model below it.

| Button | What it is |
|--------|-----------|
| **Sort** | The [Sort dashboard](#sort-dashboard) — where sorting happens. |
| **Models** | The [model library](#models). |
| **Community** | The [community catalogue](#community). Shown only while signed in. |
| *(separator line)* | Below it, the two ways a classifier is taught. |
| **Train** | The [Train](#train) screen — teaching a local model of your own. |
| **AI Config** | The [AI Config](#ai-config) screen — the equivalent step when an HTTP server does the recognising: teaching *it* what to look for. |
| **Settings** | Pinned at the bottom, separate from the activities above it: everything you configure once and rarely touch again. |

Train and AI Config are always both there, and exactly one of them is in use
at a time — which one follows the **active model** (see [Models](#models)):

- **a model you own** — Train is live, AI Config is dimmed;
- **AI Config mode** ("Use AI Config" on the Models page) — AI Config is live,
  Train is dimmed;
- **a community model** — neither is live: its publisher trained it, and it,
  not a server, does the classifying.

Hover either button and the tooltip says which of those you are in. A dimmed
button still works — it is the screen behind it that explains the state, and
carries a button to the Models page to change it. Never a dead end.

### Status bar

Along the bottom, from left to right:

- **Messages** — what the app just did ("Auto-connected to COM3.", "Run
  stopped."). This is where a refused action explains itself.
- **● Camera** and **● Serial** — connection indicators. Green means
  connected, and the serial one names the port, speed and the firmware
  version it handshook with.
- **Update to *version*** or **Restart to update** — appears only when there
  is an app update to fetch or a downloaded one waiting. See
  [updates](#updates).
- **Model update: v*N*** — appears when the active community model's
  publisher has released a newer version. See [community model
  notices](#community-model-notices).
- **Your name** and **Sign in / Sign out** — the community account. Nothing
  outside the [Community](#community) screen needs one.

### Menus

- **File → Open data folder** opens the folder holding the database, your
  models, their training images and the logs. **Quit** closes the app.
- **View** switches each [panel](#panels) on or off, and holds **Re-dock
  panels**.
- **Help** holds this guide (`F1`), [Check for updates…](#updates), [Export
  support package…](#support-package), About and License.

## Panels

Four panels can sit around your working screen. Each one can be moved,
tabbed together with another, torn off into its own floating window, or
closed:

- **Serial Monitor** — along the bottom, open by default.
- **Classification History** — on the right, closed by default.
- **User Guide** — on the right, closed by default (this guide).
- **Themes** — on the right, closed by default.

**Moving a panel:** drag it by its *tab* — the small labelled tab at the
edge of the panel, not its title. As you drag, blue drop indicators appear
showing where it can land: the four edges of the window, or the middle of
another panel to tab the two together. Drop it outside the window and it
becomes a floating window you can put on a second monitor.

**Getting a panel back:** **View → Re-dock panels** returns every open panel
to where it started, un-floated. Use it any time a panel ends up somewhere
you didn't intend — it always works, which dragging one back does not.

**Closing and re-opening:** the ✕ on a panel closes it; its entry in the
**View** menu switches it back on. Your arrangement is remembered and
restored the next time you start the app.

### Serial Monitor

Live traffic between the app and the board — every line it sends and every
line the board answers, in the order it happened. It is the first place to
look when the machine does something unexpected.

- The header shows the connection state, plus **Autoscroll**, **Timestamps**
  and **Pause**. Pausing holds new lines back and releases them when you
  un-pause; nothing is lost.
- **Zoom** scales the text from 50% to 200%, for reading the log across the
  bench.
- **Clear** empties the view. **Save…** writes what you are currently
  looking at to a text file — filtered lines are not written, so narrow the
  view first if that's what you want to send.
- **Filter** hides every line that doesn't contain what you type, and the
  **RX** / **TX** / **Notes** toggles hide a whole direction (received,
  sent, and the monitor's own commentary). Filtering only hides: clear the
  box and the traffic is all still there.
- The bottom row sends a command by hand — type it, pick the line ending the
  firmware expects, press **Send**. `Up` and `Down` walk back through what
  you have sent before.
- **Baud** changes the port speed and reconnects at it. It is the same
  setting as [Settings → Serial](#serial)'s; change it in either place and
  the other follows.

### Classification History

A grid of tiles, one per sorted case: the cropped image, the headstamp, the
confidence, the bin it went to, and a running case number.

Tiles never scroll or move. When the grid is full the newest case overwrites
the oldest tile in place, and a coloured border trails the most recent few
so you can see where "now" is. That is deliberate — it means you can watch
one position and see cases go past, instead of chasing a scrolling list.

**Zoom** (at the bottom of the panel, 50–200%) sets the tile size. Bigger
tiles are easier to read across a bench; smaller tiles mean more of them fit,
since the panel holds however many tiles fit its area. Click any tile to
open that case's image full size.

### Themes panel

The whole list of themes in one place. Click one and the app repaints
immediately, so you can try them against the room's lighting without leaving
what you were doing. **Edit theme…** opens the theme editor.

This is the same list as [Settings → Theme](#theme); whichever you use, the
other follows.

## Sort dashboard

The Sort activity (the sidebar's top button) is where sorting actually
happens. It combines the current case, the slot layout and the run controls
on one screen.

On a fresh machine — nothing connected and nothing routed to a slot yet —
this screen shows a short panel with buttons straight to [Settings →
Serial](#serial) and [Settings → Camera](#camera) instead of an empty grid.

### The current case

The left-hand panel shows the **last captured and cropped headstamp** —
exactly the image the classifier was given — with what it made of it
underneath: the headstamp it matched and how confident it was. A confidence
below the confidence floor is coloured as a warning, and that case goes to
the Catch-All. Only the current case is shown here; the running record is in
the [Classification History](#classification-history) panel.

**Show live camera** above the panel adds the live camera feed as a smaller
second panel below the crop. It is off by default — the feed is a setup aid,
not what an operator watches during a run — and while it is off no frame is
fetched or drawn. If the camera isn't running, clicking the feed takes you to
[Settings → Camera](#camera).

### Slot cards

Every slot on the machine gets a card, arranged in a grid. Slot 0 is the
**Catch-All**, for anything unclassified, below the confidence floor, or not
routed to a slot; the rest are your bins. A card shows:

- the slot number (or "Catch-All")
- how many cases have landed there this run
- every headstamp currently routed to it, listed in full — the card grows to
  fit the list rather than truncating it

Above the grid: **Sorted this run** counts every case this run has sorted,
**Reset counts** zeroes the counters (the grid's and the per-bin ones)
without touching your assignments, and the [template](#sorting-templates)
picker names the layout the cards are showing.

### Editing an assignment

Click any card except Catch-All to open its assignment editor. Tick a
headstamp to route it to that slot; unticking sends it back to the
Catch-All. Outside of [package mode](#package-mode) a headstamp can only be
assigned to one slot at a time — ticking it here moves it off whichever slot
it was in before, and the row tells you which one that was. A filter box
narrows a long headstamp list by name.

### Sorting templates

The **Template** picker on the slot grid's header row holds the active
**sorting template** — a named snapshot of the whole slot layout, so one
model can carry several bin arrangements ("Range brass" vs. "Match prep")
and switch between them. **+** creates one (optionally copied from the
current layout); **✎** renames or deletes the active one.

You never have to save a template: the active one follows your edits as you
make them. Switching templates replaces every slot assignment at once, so the
whole picker is blocked while a run is in progress — stop first. Standard and package
mode keep separate template lists, because their layouts mean different
things.

### Run options

**⚙ Run options** on the strip at the foot of the page holds everything that
changes how a run behaves:

- **Store images** — keep a copy of each run's captures (none / above the
  floor / below it / all), under the active model's folder, so AI Config mode
  stores nothing. Anything but "None" will use real disk space over a long
  session.
- **Confidence floor** — the percentage a prediction has to reach to be
  trusted. Below it, the case goes to the Catch-All.
- **Automatically select trays** — when a confident prediction has no slot,
  route it to the first empty one and keep the assignment.
- **Package mode** and **Batch size** — see [package mode](#package-mode).

### Start, Stop, and Manual feed

The strip at the foot of the page, with the green **Start** at the far
right:

- **Start** begins the continuous sort loop: capture, classify, sort,
  repeat. It is one button — while the loop runs it reads **Stop**, in red.
- **Stop** ends it. Case counts are kept, not reset, so you can clear a jam
  and pick back up mid-tray.
- **Manual feed** runs exactly one cycle without starting the loop, useful
  for testing a single case.

Without a board connected, Start and Manual feed are greyed out and say so.
Otherwise starting is refused, with a message explaining why, if the AI
Config API key or model name is unset, the active model's checkpoint is
missing, PyTorch isn't installed yet for a local model, or a [moderator
note](#community-model-notices) is waiting to be read.

### Package mode

Turning on **Package mode** in [Run options](#run-options) switches the grid
to batch counting against the one **Batch size** you set, shown as
"count / target" on every slot card. A slot that reaches the target stops
taking that headstamp; once every slot for a given headstamp is full, the run
halts and asks you to empty bins and reset counters. A slot card's **⟲ Reset**
button empties just that bin's counter without stopping the run, so it can
keep filling.

### Community model notices

When the active model came from the [Community](#community) and you are
signed in, the app checks in with its publisher each time you open the Sort
screen. Two things can come back, and neither stops a run in progress:

- **Model update: v*N*** in the status bar — a newer version has been
  published. The button opens a summary of what you have versus what is
  available; **Update now** installs it in place, keeping your slot
  assignments, sorting templates and the name you gave it. Dismissing it is
  fine — it comes back next time you open Sort.
- **Moderator notes (*N*)** on the run strip — messages from the model's
  moderators, usually about the feedback images the model is collecting. A
  note you haven't acknowledged opens by itself and **blocks Start** until
  you have read it. The same button is the history of every note, including
  the ones already acknowledged.

If the check can't reach the server, nothing appears and the app behaves
exactly as it does offline.

## Train

The Train screen is the loop that builds a training set: feed a case,
capture it, label it, save it — and, when you have enough images, train a
model from them.

### When Train isn't available

Train is always in the sidebar, but it needs a local model of *your own* to
work on. When the active model isn't one, the button is dimmed and the page
says which of the two cases you are in:

- **AI Config mode** — classification is running over HTTP, so there is no
  model on this machine to train. Activate or create a local model on the
  [Models](#models) page.
- **A community model** — the checkpoint belongs to its publisher and they
  keep updating it. To build on it, export it from the Models page and
  import the archive back as your own model.

Either way the page carries a button straight to the model library.

The same holds the other way round for [AI Config](#ai-config), which is
dimmed whenever a local model is active: clicking it opens its own page,
where — in place of the server form — a panel names the model doing the
classifying and offers the same jump to the model library. Both buttons stay
in the sidebar in every mode — what changes is which one is live.

### Capturing images

The left column reads top to bottom in the order you work:

1. The **case image** — the cropped headstamp of whatever was last fed.
2. **Feed**, centred beneath it — drops the next case and captures it. It
   needs the board connected.
3. **Label** and **Save image** — the label to file this image under, and
   the button that writes it. The label box remembers every headstamp the
   model knows, and you can type a new one.
4. The readouts — the predicted label (when a trained checkpoint and PyTorch
   are both present), how long image processing took, how long the
   prediction took, and a per-step breakdown. Useful for spotting a setup
   that has become slow.

PyTorch is *offered* here, never required: capturing and labelling images is
exactly the work you do before there is anything to predict with. Declining
costs you only the predicted-label convenience, and you won't be asked
again this session.

### Image counts

The right half lists every headstamp and how many images are on disk for it
— your training set, straight from the folder. Drag the divider to give it
more width and the list reflows into more columns, which is what makes a
model with a hundred-odd headstamps readable.

**Clicking a headstamp in this list saves the captured case under that
label and immediately feeds the next one** — the fastest way to work through
a tray of mixed brass.

### Training a model

The **Training** strip at the foot of the page turns the images above into a
model:

- **Sort while training** routes each case you label into its own bin
  instead of the catch-all, using the label *you* picked — so a training
  session also sorts the tray.
- **Training settings…** opens the hyperparameters (epochs, batch size,
  learning rate, image size, focal loss, SWA and the rest). The defaults are
  sensible; change them when you know why. The ConvNeXt size is the model's
  own property — change it in the model editor, not here.
- **Start training** launches the run in a separate process and opens a live
  console for it. Closing the console mid-run asks first, and confirming
  cancels the run — the console is the only thing it reports to.

Training needs PyTorch. If it isn't installed, the app offers to install it
here.

## AI Config

The alternative to a local model: classification is sent to an
OpenAI-compatible HTTP server, and this screen is where that server — and the
headstamps it may answer with — is set up. It is Train's mirror in the
sidebar: live whenever no local model is active, dimmed when one is.

### When AI Config isn't the one classifying

Activate a local model and this screen swaps its form for a panel naming the
model that is classifying instead, with a button straight to the
[Models](#models) page. Select **Use AI Config** there to come back — the
server settings are still exactly as you left them.

### Setting up the server

- **Server** — endpoint URL, API key, model name, the prompt (use
  `{{headstamps}}` where the list of headstamps should be injected), and the
  JPEG quality and scale used for the image. Press **Save** to apply — these
  fields do not save as you type.
- **Headstamps** — the list this mode routes on, and each one's slot. Add,
  rename, remove, clear, or **Load from server**. These do save immediately,
  and editing a slot here updates the Sort dashboard's cards straight away.
- **Test shot**, under *Test (capture → crop → classify)* — takes one frame
  from the camera, runs it through the same pipeline, and shows the label and
  confidence that came back. No board feed needed; it reads the fields above
  as they stand, so an API key and model name must be filled in.

## Models

The model library. Everything the app knows how to classify with is a row in
this table, plus one synthetic row for AI Config mode.

### The model list

The table lists each model's name, whether it is active, its cartridge, type
(yours or a community model), the ConvNeXt size it was built as, how many
training images it has, whether it has been trained, and when. Click a
column heading to sort by it.

The **Active** column marks the model the app currently classifies with:
exactly one row reads **● ACTIVE** in the theme's action colour, and every
other row's Active cell is blank. To change it, select a row and press
**Activate** at the bottom right.

Above the table: filters by **cartridge** and **type**, a search box, and
**New cartridge**, **New model** and **Import…**.

The **"Use AI Config"** row sits at the top of the list, whenever the filters
and the search box leave it there. Activating it puts the app in AI Config
mode — classification goes to the HTTP server configured on the [AI
Config](#ai-config) screen instead of a local model. It is not a model, so
Edit, Delete, Export and the rest stay greyed out while it is selected.

### Managing a model

Everything on the bar under the table acts on the **selected** row.
**Delete** sits alone on the far left and **Activate** on the far right, so
the destructive one and the primary one can never be neighbours:

- **Delete** — delete the model and its folder, after a confirmation. The
  active model refuses: activate something else first.
- **Edit…** — name, cartridge, model size, primer settings, and the
  community feedback opt-in where it applies.
- **Images…** — browse the model's training images as thumbnails, and
  reclassify or delete them. A single image opens full size with prev/next
  navigation. Models you own only.
- **Headstamps…** — the model's headstamp list: add, rename, delete, set
  each one's slot, and group headstamps under parent classifications.
  Renaming a headstamp also renames its training images, so the label and
  the files stay in step.
- **Evaluate…** — score the model against a folder of labelled images and
  write an interactive HTML report. Only open reports you generated
  yourself, from image folders you trust.
- **Export…** — write the model out as a ZIP (checkpoint, headstamps and
  images), the format the Windows app uses.
- **Activate** — make this the model the app classifies with. Greyed out on
  the row that is already active.

### Importing and exporting

**Import…** reads a model ZIP back in, after a notice that a model file can
execute code — import only from authors you trust. If the archive carries a
community ID you already have installed, the app asks whether to **update the
installed one in place** — keeping its slot assignments, sorting templates and
your name for it — or to **import it as a separate copy**; anything else lands
as a new model. A ZIP you import stays *yours*: importing your own model onto
a new machine leaves it trainable.

Import and export both run in the background; a model with its images can be
large.

## Community

Published models, shared by other users. Signing in is the only thing in the
app that needs an account — everything else works signed out.

The table lists each model with its cartridge, version, what the archive
includes (model, images, or both), headstamp and image counts, size, publish
date, author, and its **State** against your library: Available, Update
available, or Installed.

Select a row and the two buttons under the table follow its state. The
right-hand one is the primary, and it says what the state makes possible:

| Button | Action |
|--------|--------|
| **Download model** | Download and install it, after a notice that a model file can execute code — download only from authors you trust. |
| **Update model** | Update your installed copy in place, or take this version as a separate copy; it asks which. |
| **Already installed** | Nothing to do — this version is the one you have. |
| **Remove** | On the far left, and live only for an installed, current model: delete your local copy. The catalogue entry stays, and the primary goes back to **Download model**. |

Select a second model and press Download while one is still running and it
queues behind the first, with the status showing "(2 of 3)" as it works
through them. Selecting a queued model shows where it sits in the queue, and
the one being fetched reads **Downloading…**.

A model you install this way is **managed by its publisher**: it can't be
trained here, and updates from them install over it cleanly. The Sort screen
tells you when a newer version exists — see [community model
notices](#community-model-notices).

**Share a model…** publishes one of your own models to the community. It
appears only for accounts the server grants the contributor role. Sharing does
not make the model foreign: your copy stays yours and stays trainable.

## Settings

The Settings activity groups everything you configure once and rarely touch
again, behind a section list. Most settings here save as you change them,
with no separate Save step. The exception is the [Camera](#camera) page,
where the device and resolution are committed by **Apply**.

### Camera

Picks which camera the Sort dashboard's preview and the classifier both read
from.

- **Detect / refresh** probes attached cameras (opening each one briefly to
  read its supported resolutions) and fills the **Camera** and
  **Resolution** dropdowns.
- **Apply** swaps the live camera to the selected device and resolution —
  the preview beneath updates immediately, so a bad pick is obvious before
  you leave the page.

Nothing here grabs a camera on its own; both actions are yours. The current
device and resolution are shown beneath the controls.

### Serial

Connects the app to the sorting machine over the board's UART protocol.

- **Port** — pick a detected serial port, or **Emulated** to run against the
  built-in board emulator with no hardware attached.
- **Baud** and **probe timeout** — connection parameters; the defaults match
  the firmware. The baud picker is shared with the [Serial
  Monitor](#serial-monitor)'s.
- **Connect** / **Disconnect** open or close the link; the status bar's
  serial indicator mirrors the result.
- **Refresh ports** re-scans, for a board plugged in after the app started.
- **Open serial monitor ↗** reveals the [Serial Monitor](#serial-monitor)
  panel.
- **Initialize these settings on startup** pushes the board init settings
  below automatically on every connect, instead of only when you press "Push
  to board".

**Board init settings** covers the machine's tunables — feed and sort speed,
homing offsets, motor current, debounce timing and the camera LED level —
with **Get config from board** / **Push to board** to read or write them.
The **Sort arm** group jogs the arm to a slot or homes it, for testing
wiring before a real run. **Airdrop configuration** holds the three timing
values (pre-drop delay, signal duration, post-drop delay), plus the switch
that turns it on, for boards fitted with an airdrop mechanism.

### Image Processing

Tunes how a captured frame becomes the 480×480 headstamp image the
classifier sees. **Capture** takes a frame and shows it beside the processed
result, and every detection or primer change re-processes that same frame — so
you tune against one case instead of re-feeding for every adjustment.

**These settings belong to the active model**, not to the app: case diameter
and primer size are properties of the cartridge, so switching models brings
its own values back. A model that has never been tuned inherits whatever is
currently set, so nothing is lost when you activate one for the first time.

- **Configuration** — the circle-detection parameters: accumulator scale,
  minimum separation between centres, edge strength, detection threshold,
  and the minimum and maximum case radius in pixels. Start with the two
  radius values: they bracket the size of the case in the frame.
- **Primer mask** — leave the primer alone, keep only the primer area, or
  hide it, plus the primer radius. Hiding the primer helps when primer
  markings confuse the classifier.
- **Camera LED brightness** — the board's ring-light level. This one *is*
  global: it is a property of the machine, not of the model. The board is
  updated shortly after you stop moving the slider.

### Theme

Picks the colour theme. The same list is in the [Themes
panel](#themes-panel), which is the easier place to try several.

**Edit theme…** opens the theme editor: it starts from the theme you are
currently using and gives you a colour picker per role with a live preview.
**Save & apply** writes back to a theme you made (renaming it moves it rather
than copying), makes a new one when you started from a built-in, and leaves
the editor open so you can keep adjusting — **Close** ends the session;
**Create new…** saves under a name you pick, and a built-in is never
overwritten either way. A theme can also be exported to a file and imported
on another machine.

## Getting help

### The guide

`F1`, or **Help → User Guide**, opens this guide in a panel at the section
for the screen you are on. It is a normal [panel](#panels): dock it beside
your work while learning something, close it after.

### Support package

**Help → Export support package…** collects your current configuration into
a plain-text report — the app version and platform, the active model, run
options, training configuration, image processing, serial and camera
settings, and the AI Config setup.

**Copy to clipboard** gives you the text to paste on the community Discord
when asking for help. **Save package…** writes a ZIP holding the same report
plus a machine-readable `config.json`.

It is safe to share: the API key is reported only as "set" or "not set",
nothing is read from the sign-in cache, and file paths are shown relative to
the data folder, so your home directory never appears.

### Updates

The app checks for a new release shortly after it starts and, if there is
one, offers it in the status bar. **Help → Check for updates…** asks
immediately.

The dialog shows the release notes and a **Download & install** button.
Downloading only stages the update — nothing is replaced until you restart,
and the button becomes **Restart now** when it is ready. **Choose a
different version…** lists every published release, including older ones,
and optionally pre-releases (which the automatic check never offers). You
can turn the automatic check off in the same dialog.
