"""The theme palettes and the custom-theme registry — toolkit-neutral.

Every colour in the app comes from here: the shipped palettes, the live
registry, and the functions that read and write user-made themes into the
``ui.custom_themes`` settings row. Nothing in this module touches a widget.

The Qt stylesheet renderer that consumes these palettes is ``ui/theme.py``.
"""

from __future__ import annotations

# Settings key holding the user's chosen theme name (see repository.SettingsRepo).
SETTING_THEME = "ui.theme"


# The original theme, and the reference for every other palette's key set.
#
# Everything above the "Action colors" block is neutral gray (R == G == B):
# surfaces, borders, text, and the selection/focus tints. Only the action
# and status entries carry hue. If you add a color here, ask which of the
# two groups it belongs to — a new tinted surface breaks the contrast the
# colored buttons rely on.
_DARK = {
    # Backgrounds — neutral gray, darkest to lightest.
    "bg_window": "#131313",  # app background / gradient bottom
    "bg_gradient_a": "#2f2f2f",  # title bar left
    "bg_gradient_b": "#0c0c0c",  # title bar right
    "bg_surface": "#1c1c1c",  # panels — one step up from the window
    "bg_card": "#272727",  # raised surfaces (slot cards, monitor log)
    "bg_card_hover": "#333333",  # card hover
    "bg_card_sel": "#474747",  # selected card — lifted, still neutral
    "bg_input": "#0b0b0b",  # deeper than window — entries, listbox, text
    # Borders / separators.
    "border": "#3a3a3a",
    "border_focus": "#8f8f8f",  # focus ring — brightness, not hue
    # Text.
    "text": "#d4d4d4",
    "text_highlight": "#ffffff",  # emphasised values (Accent.TLabel)
    "text_muted": "#9a9a9a",
    "text_subtle": "#6f6f6f",
    "text_inverse": "#121212",  # for text on light/action backgrounds
    # Neutral "accent" — section titles, focus, indicators, selection fills.
    # Kept gray on purpose: emphasis here comes from brightness so that the
    # action colors below stay the only saturated thing on screen.
    "accent": "#e0e0e0",
    "accent_hover": "#f2f2f2",
    "accent_press": "#c2c2c2",
    "accent_dim": "#2e2e2e",  # subtle button rest state
    # Action colors — buttons only. Green = primary/go, blue = update an
    # existing thing, red = stop/destructive. Picked at similar brightness so
    # they read as one family and carry dark text.
    "action": "#22c55e",
    "action_hover": "#4ade80",
    "action_press": "#16a34a",
    "update": "#60a5fa",
    "update_hover": "#93c5fd",
    "update_press": "#3b82f6",
    "danger": "#ef4444",
    "danger_hover": "#f87171",
    "danger_press": "#dc2626",
    # Status colors — text and small indicators. `success` tracks `action`
    # and `error` tracks `danger` in every theme: they are the same idea
    # rendered as text instead of a button.
    "success": "#22c55e",
    "success_dim": "#14331f",  # muted green fill — e.g. applied auto-suggestions
    "warning": "#f59e0b",
    "error": "#ef4444",
}

# Daylight theme: the gray ladder inverted, so "lifted" reads as *darker*
# rather than brighter. The action colors are the darker 700-weight shades
# because they now sit against white and carry light text.
_LIGHT = {
    "bg_window": "#eaeaea",
    "bg_gradient_a": "#fbfbfb",
    "bg_gradient_b": "#d4d4d4",
    "bg_surface": "#f4f4f4",
    "bg_card": "#ffffff",
    "bg_card_hover": "#ededed",
    "bg_card_sel": "#d8d8d8",
    "bg_input": "#fdfdfd",
    "border": "#c2c2c2",
    "border_focus": "#5b5b5b",
    "text": "#1c1c1c",
    "text_highlight": "#000000",
    "text_muted": "#565656",
    "text_subtle": "#868686",
    "text_inverse": "#fafafa",
    "accent": "#3d3d3d",
    "accent_hover": "#292929",
    "accent_press": "#101010",
    "accent_dim": "#e2e2e2",
    "action": "#15803d",
    "action_hover": "#16a34a",
    "action_press": "#0f5f2e",
    "update": "#1d4ed8",
    "update_hover": "#2563eb",
    "update_press": "#1e40af",
    "danger": "#b91c1c",
    "danger_hover": "#dc2626",
    "danger_press": "#991b1b",
    "success": "#15803d",
    "success_dim": "#d5f0de",
    "warning": "#b45309",
    "error": "#b91c1c",
}

# Warm paper — the light theme's twin for people who find pure white harsh.
_SEPIA = {
    "bg_window": "#e8dfcf",
    "bg_gradient_a": "#f6efe0",
    "bg_gradient_b": "#d3c6ae",
    "bg_surface": "#f2ebdc",
    "bg_card": "#fdf8ee",
    "bg_card_hover": "#eee5d3",
    "bg_card_sel": "#dccfb6",
    "bg_input": "#fffdf7",
    "border": "#c3b498",
    "border_focus": "#6b5b43",
    "text": "#3a2f22",
    "text_highlight": "#1c150c",
    "text_muted": "#6d5f4c",
    "text_subtle": "#948673",
    "text_inverse": "#fdf9f0",
    "accent": "#5b4a34",
    "accent_hover": "#453727",
    "accent_press": "#2e2419",
    "accent_dim": "#e0d6c1",
    "action": "#46733a",
    "action_hover": "#558a45",
    "action_press": "#35592c",
    "update": "#2f6690",
    "update_hover": "#3d7fb0",
    "update_press": "#255273",
    "danger": "#a33232",
    "danger_hover": "#c04040",
    "danger_press": "#872828",
    "success": "#46733a",
    "success_dim": "#dbe8cf",
    "warning": "#9a6b12",
    "error": "#a33232",
}

# Deep navy chrome. The surfaces carry the hue, so the accent is pulled
# almost to white to keep selection fills legible.
_MIDNIGHT_BLUE = {
    "bg_window": "#0b1220",
    "bg_gradient_a": "#1b2a4a",
    "bg_gradient_b": "#060b16",
    "bg_surface": "#121c2e",
    "bg_card": "#1a2740",
    "bg_card_hover": "#22334f",
    "bg_card_sel": "#31456a",
    "bg_input": "#070d18",
    "border": "#2c3b57",
    "border_focus": "#7ea2d8",
    "text": "#d6e2f5",
    "text_highlight": "#ffffff",
    "text_muted": "#93a6c4",
    "text_subtle": "#6a7c99",
    "text_inverse": "#0a1120",
    "accent": "#cfe0fb",
    "accent_hover": "#e8f1ff",
    "accent_press": "#a9c4ea",
    "accent_dim": "#22314d",
    "action": "#22c55e",
    "action_hover": "#4ade80",
    "action_press": "#16a34a",
    "update": "#60a5fa",
    "update_hover": "#93c5fd",
    "update_press": "#3b82f6",
    "danger": "#ef4444",
    "danger_hover": "#f87171",
    "danger_press": "#dc2626",
    "success": "#22c55e",
    "success_dim": "#123a2a",
    "warning": "#fbbf24",
    "error": "#ef4444",
}

# Black and blood red. The one theme whose accent carries hue: section
# titles, selection fills and the focus ring are crimson, and the surfaces
# are near-black with just enough red in them to belong. Green still means
# go and the danger red stays a step brighter than the accent, so a Stop or
# a Delete button is still the loudest thing on screen.
_GOTHIC = {
    "bg_window": "#0c0708",
    "bg_gradient_a": "#4a1116",
    "bg_gradient_b": "#080405",
    "bg_surface": "#150e10",
    "bg_card": "#1f1416",
    "bg_card_hover": "#2b1a1d",
    "bg_card_sel": "#46181e",
    "bg_input": "#070303",
    "border": "#3a2226",
    "border_focus": "#c0464e",
    "text": "#e0d6d7",
    "text_highlight": "#ffffff",
    "text_muted": "#a89597",
    "text_subtle": "#7a686a",
    "text_inverse": "#0b0607",
    "accent": "#d13c45",
    "accent_hover": "#e85860",
    "accent_press": "#a52a31",
    "accent_dim": "#2a1518",
    "action": "#3fb950",
    "action_hover": "#56d364",
    "action_press": "#2ea043",
    "update": "#5b82c4",
    "update_hover": "#7ba0dd",
    "update_press": "#45689f",
    "danger": "#e5484d",
    "danger_hover": "#ef6b6f",
    "danger_press": "#c93c41",
    "success": "#3fb950",
    "success_dim": "#1b2a1e",
    "warning": "#d29922",
    "error": "#e5484d",
}

# A comic page: yellow newsprint, white panels, black ink outlines, and the
# two comic inks — red for headings and stops, blue for go. The one theme
# where **blue, not green, means go**: a comic palette has no green in it,
# and `success` tracks `action` by rule, so the connected indicator is blue
# here while the disconnected one stays red.
_COMIC = {
    "bg_window": "#f2b800",  # the page's darker edge, behind the tabs
    "bg_gradient_a": "#ffe14d",  # title bar left — bright newsprint yellow
    "bg_gradient_b": "#ffb61f",
    "bg_surface": "#ffd21f",  # the page itself
    "bg_card": "#ffffff",  # white panels sit on it
    "bg_card_hover": "#eef4ff",
    "bg_card_sel": "#9ec5ff",  # selection goes comic blue
    "bg_input": "#fffdf2",
    "border": "#111318",  # ink — every outline in this theme
    "border_focus": "#2352cc",
    "text": "#14161c",
    "text_highlight": "#000000",
    "text_muted": "#4b4740",
    "text_subtle": "#7d7566",
    "text_inverse": "#fefefe",  # white, but not the panels' white
    "accent": "#cf1b1b",  # headings, selection fills, the caret
    "accent_hover": "#ea2a2a",
    "accent_press": "#a41313",
    "accent_dim": "#ffe58f",
    "action": "#1a4fc4",
    "action_hover": "#2f66e0",
    "action_press": "#123a96",
    "update": "#1f2530",
    "update_hover": "#333c4d",
    "update_press": "#12171f",
    "danger": "#e8302a",
    "danger_hover": "#f65046",
    "danger_press": "#bf1f1a",
    "success": "#1a4fc4",
    "success_dim": "#cfe0ff",
    "warning": "#e07a00",
    "error": "#e8302a",
}

# Display name → palette. Insertion order drives the picker's order.
BUILTIN_THEMES: dict[str, dict[str, str]] = {
    "Dark": _DARK,
    "Light": _LIGHT,
    "Sepia": _SEPIA,
    "Midnight Blue": _MIDNIGHT_BLUE,
    "Gothic": _GOTHIC,
    "Comic Book": _COMIC,
}

# The live registry: the built-ins above plus whatever the user has saved in
# the theme editor, appended in load order. Mutated in place by
# `register_custom_theme` (the module is imported all over the UI).
THEMES: dict[str, dict[str, str]] = dict(BUILTIN_THEMES)

DEFAULT_THEME = "Dark"

# Settings key holding the user's saved themes: {name: payload}, where the
# payload is what `custom_theme_payload` returns.
SETTING_CUSTOM_THEMES = "ui.custom_themes"
# Bumped if the payload shape ever changes; import refuses anything newer.
CUSTOM_THEME_VERSION = 1
# Theme names ride in the title-bar dropdown, which is sized to the longest of
# them — so they stay short enough not to crowd the title out.
MAX_THEME_NAME = 12

# Themes whose title bar gets a ben-day dot field printed over the gradient,
# and the ink to print it in. A theme that isn't listed here gets a plain
# gradient — the dots are a comic-book device, not a default. Pick an ink
# close to the gradient's dark end so the field fades out as the background
# darkens under it.
HALFTONE_INK = {
    "Comic Book": "#1b1e24",
}

# Themes drawn with comic-book ink outlines: how many pixels of border to put
# around panels, cards, buttons and fields. 0 (the default) keeps the flat,
# borderless look the other themes are built on.
INK_OUTLINE = {
    "Comic Book": 2,
}


def theme_names() -> list[str]:
    """Selectable theme names, in display order."""
    return list(THEMES)


def resolve_theme(name: str | None) -> str:
    """Map a stored/user-supplied name onto a real theme, case-insensitively."""
    if name:
        for known in THEMES:
            if known.casefold() == str(name).casefold():
                return known
    return DEFAULT_THEME


# ----- user-made themes -------------------------------------------------------
#
# The editor (ui/dialog_theme_editor.py) builds these from an existing theme,
# and the app registers whatever the settings store holds at startup. They are
# ordinary entries in THEMES from then on — nothing downstream knows or cares
# that a palette was made by a user.

# Roles the editor never asks about because they mirror another one. Keeping
# them derived is what lets `retheme_widgets` resolve a colour to one role.
DERIVED_ROLES = {"success": "action", "error": "danger"}

_custom_themes: dict[str, dict] = {}


def is_custom_theme(name: str) -> bool:
    return name in _custom_themes


def custom_theme_names() -> list[str]:
    return list(_custom_themes)


def editable_roles() -> list[str]:
    """Palette roles a user can set; the rest are derived from these."""
    return [key for key in _DARK if key not in DERIVED_ROLES]


def normalize_palette(values: dict | None, base: dict[str, str] | None = None) -> dict[str, str]:
    """Coerce arbitrary input into a full, well-formed palette.

    Unknown keys are dropped, missing ones come from `base` (the default theme
    if not given), non-colours are ignored, and the derived roles are forced
    back into step with the ones they mirror. An imported file can therefore be
    partial or slightly wrong without the app ever seeing a broken palette.
    """
    palette = dict(base or _DARK)
    for key, value in (values or {}).items():
        if key in palette and isinstance(value, str) and _is_hex_color(value):
            palette[key] = value.lower()
    for derived, source in DERIVED_ROLES.items():
        palette[derived] = palette[source]
    return palette


def _is_hex_color(value: str) -> bool:
    if not isinstance(value, str) or len(value) != 7 or not value.startswith("#"):
        return False
    try:
        int(value[1:], 16)
    except ValueError:
        return False
    return True


def unique_theme_name(name: str) -> str:
    """A free theme name within the length limit (adds " (2)", … as needed)."""
    stem = (name or "").strip()[:MAX_THEME_NAME].strip() or "My theme"
    candidate, n = stem, 2
    existing = {known.casefold() for known in THEMES}
    while candidate.casefold() in existing:
        suffix = f" ({n})"
        candidate = f"{stem[: MAX_THEME_NAME - len(suffix)].strip()}{suffix}"
        n += 1
    return candidate


def register_custom_theme(
    name: str,
    palette: dict[str, str],
    *,
    halftone: str | None = None,
    outline: int = 0,
    base: str | None = None,
) -> str:
    """Add (or replace) a user-made theme. Returns the name it registered under."""
    name = (name or "").strip()
    if not name:
        raise ValueError("A theme needs a name.")
    if len(name) > MAX_THEME_NAME:
        raise ValueError(f"Theme names are at most {MAX_THEME_NAME} characters.")
    if name in BUILTIN_THEMES:
        raise ValueError(f"{name!r} is a built-in theme — pick another name.")
    THEMES[name] = normalize_palette(palette)
    _custom_themes[name] = {
        "version": CUSTOM_THEME_VERSION,
        "based_on": base,
        "halftone": halftone if (halftone and _is_hex_color(halftone)) else None,
        "outline": max(0, min(4, int(outline or 0))),
    }
    if _custom_themes[name]["halftone"]:
        HALFTONE_INK[name] = _custom_themes[name]["halftone"]
    else:
        HALFTONE_INK.pop(name, None)
    if _custom_themes[name]["outline"]:
        INK_OUTLINE[name] = _custom_themes[name]["outline"]
    else:
        INK_OUTLINE.pop(name, None)
    return name


def rename_custom_theme(old: str, new: str) -> str:
    """Rename a user-made theme in place. Returns the new name.

    A rename is not "save a copy and delete the original": the theme keeps its
    position in the picker and its options, and only the key changes.
    """
    if old not in _custom_themes:
        raise ValueError(f"{old!r} isn't a theme you can rename.")
    new = (new or "").strip()
    if new == old:
        return old
    meta = _custom_themes[old]
    palette = dict(THEMES[old])
    # Register first: a bad new name must leave the original untouched.
    register_custom_theme(
        new,
        palette,
        halftone=meta.get("halftone"),
        outline=meta.get("outline", 0),
        base=meta.get("based_on"),
    )
    unregister_custom_theme(old)
    return new


def unregister_custom_theme(name: str) -> None:
    """Forget a user-made theme. Built-ins are left alone."""
    if name not in _custom_themes:
        return
    _custom_themes.pop(name, None)
    THEMES.pop(name, None)
    HALFTONE_INK.pop(name, None)
    INK_OUTLINE.pop(name, None)


def custom_theme_payload(name: str) -> dict:
    """The storable / exportable form of a user-made theme."""
    meta = _custom_themes[name]
    return {
        "version": CUSTOM_THEME_VERSION,
        "name": name,
        "based_on": meta.get("based_on"),
        "halftone": meta.get("halftone"),
        "outline": meta.get("outline", 0),
        "palette": dict(THEMES[name]),
    }


def custom_themes_payload() -> dict[str, dict]:
    """Every user-made theme, keyed by name — what the app persists."""
    return {name: custom_theme_payload(name) for name in _custom_themes}


def load_custom_themes(stored: dict | None) -> list[str]:
    """Replace the registered custom themes with `stored`. Returns their names.

    Anything unreadable is skipped rather than raising: a corrupt settings row
    must not stop the app from starting.
    """
    for name in list(_custom_themes):
        unregister_custom_theme(name)
    loaded: list[str] = []
    if not isinstance(stored, dict):
        return loaded
    for key, payload in stored.items():
        if not isinstance(payload, dict):
            continue
        try:
            loaded.append(
                register_custom_theme(
                    payload.get("name") or key,
                    normalize_palette(payload.get("palette")),
                    halftone=payload.get("halftone"),
                    outline=payload.get("outline", 0),
                    base=payload.get("based_on"),
                )
            )
        except (ValueError, TypeError):
            continue
    return loaded
