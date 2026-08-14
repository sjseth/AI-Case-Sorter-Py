# AI Case Sorter

Desktop app for Windows and Linux that sorts spent brass cartridge casings by
**headstamp**. A camera photographs each case, an image classifier predicts the
headstamp, and a serial-connected sorting machine drops the case into the right
bin.

Two ways to classify:

- **Local model** — a PyTorch ConvNeXt model you trained yourself, downloaded
  from the community, or imported from a ZIP. Nothing leaves the machine.
- **AI Config** — send the cropped image to an OpenAI-compatible HTTP server.

Community features (model sharing, downloads, the feedback loop) are the only
part that needs an account. Everything else runs signed out.

## Start here

The [**User Guide**](guide/GUIDE.md) covers every screen, in the order you meet
them. It is also the guide the app itself shows on `F1`.

- [The window](guide/GUIDE.md#the-window) — activity sidebar, status bar, menus
- [Panels](guide/GUIDE.md#panels) — serial monitor, classification history, themes
- [Sort dashboard](guide/GUIDE.md#sort-dashboard) — slot cards, templates, running a sort
- [Train](guide/GUIDE.md#train) — capture, label, train
- [Models](guide/GUIDE.md#models) — activate, edit, import, export
- [Community](guide/GUIDE.md#community) — browse and install published models
- [Settings](guide/GUIDE.md#settings) — camera, serial, image processing, AI config, theme
- [Getting help](guide/GUIDE.md#getting-help) — support package and updates

## Version dropdown

This site is published per release. The selector in the header switches
between them; `latest` always points at the newest stable release, and a
release candidate is published under its own version without moving it.

## Download as PDF

<!-- Raw HTML, not a Markdown link: the PDF only exists on the published,
     versioned site (the deploy builds it), so MkDocs' link validation must
     not try to resolve it against the docs tree. -->
<a href="pdf/ai-case-sorter-docs.pdf">This documentation as a single PDF</a>
— built per version, so the PDF you download always matches the version
you're reading.

## Elsewhere

- [Source, issues and releases](https://github.com/sjseth/AI-Case-Sorter-Py) on GitHub
- [Download the latest release](https://github.com/sjseth/AI-Case-Sorter-Py/releases/latest)
- [UI Modernization](ui-modernization.md) — the research and decisions behind the Qt client
- [Contributing](https://github.com/sjseth/AI-Case-Sorter-Py/blob/main/CONTRIBUTING.md)
  and [Releasing](https://github.com/sjseth/AI-Case-Sorter-Py/blob/main/RELEASING.md)
