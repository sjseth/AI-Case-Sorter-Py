"""SQLite-backed configuration shim.

Preserves the original `Config` public surface (`config.api`, `config.serial`,
`config.image_proc`, `config.camera`, `config.headstamps`, `config.save()`)
so the existing tab code does not need to change. App-level settings live in
the `settings` table; headstamps live in their own table and are scoped to
the currently active model.

The DEFAULTS structure stays here as the canonical fallback when no settings
row exists yet.
"""

from __future__ import annotations

import copy
from typing import Any

from .db import Database
from .models import SLOT_TEMPLATE_MODES, SlotTemplate
from .repository import (
    HeadstampParentRepo,
    HeadstampRepo,
    SettingsRepo,
    SlotTemplateRepo,
)

DEFAULT_INIT_SETTINGS: dict[str, int | str] = {
    "feedhomingoffset": 0,
    "sorthomingoffset": 0,
    "feedspeed": 90,
    "sortspeed": 90,
    "feedsteps": 70,
    "sortsteps": 20,
    "slotdropdelay": 300,
    "notificationdelay": 160,
    "automotorstandbytimeout": 0,
    "feedmotorcurrent": 900,
    "sortmotorcurrent": 900,
    "fan": 100,
    "debounceTimeout": 500,
    "debounceTime": 300,
    "cameraledlevel": 130,
    "airdropenabled": 0,
    "airdroppredelay": 50,
    "airdropdsignalduration": 70,
    "airdroppostdelay": 50,
}


DEFAULTS: dict[str, Any] = {
    "api": {
        "endpoint_url": "http://localhost:8000",
        "api_key": "nokey",
        "model": "9mm",
        "prompt": "Not used for local AI Server",
        "image_quality": 100,
        "image_scale": 100,
    },
    "serial": {
        "port": "",
        "baud": 9600,
        "slot_quantity": 8,
        "handshake_timeout_s": 4.0,
        "init_on_startup": False,
        "init_settings": dict(DEFAULT_INIT_SETTINGS),
    },
    "image_proc": {
        "strategy": "hough",
        "primer_mode": "hide",
        "primer_radius": 135,
        "hough": {
            "dp": 2.0,
            "min_dist": 500,
            "param1": 100,
            "param2": 60,
            "min_radius": 150,
            "max_radius": 250,
        },
        "linescan": {
            "scan_precision": 1,
            "scan_sensitivity": 5.0,
            "padding_pct": 5,
            "bg_cliff": 0,
        },
    },
    "camera": {
        "device_index": 0,
        "device_chosen": False,
        "width": 640,
        "height": 480,
    },
}


_SECTIONS = ("api", "serial", "image_proc", "camera")
# AI Config mode (no active model) keeps its own headstamp list. The DB
# `headstamps` table requires a real model_id FK, so we stash AI Config
# headstamps in the key/value settings table instead.
_AI_HEADSTAMPS_KEY = "ai_config_headstamps"
# Per-model runtime toggle for parent-classification routing. Keyed by model
# id (``use_parent_runtime:<id>``); a per-installation preference, not exported.
_USE_PARENT_RUNTIME_KEY = "use_parent_runtime"

# App-level run options.
_RUN_CONFIDENCE_FLOOR_KEY = "run_confidence_floor"
_RUN_STORE_IMAGES_KEY = "run_store_images"
# Valid "store images" modes (internal value -> meaning):
#   none   never store
#   above  store only when confidence >= floor
#   below  store only when confidence < floor
#   all    store every classified case
STORE_IMAGES_MODES = ("none", "above", "below", "all")
DEFAULT_CONFIDENCE_FLOOR = 30

# Package mode (batch sorting). When on, the same headstamp may be assigned to
# several slots; the run fills one slot to `run_package_size` then advances to
# the next configured slot, halting when every slot for a headstamp is full.
# Package assignments are kept separate from the single-slot routing so a
# headstamp can live in multiple bins at once.
_RUN_PACKAGE_MODE_KEY = "run_package_mode"
_RUN_PACKAGE_SIZE_KEY = "run_package_size"
_PACKAGE_SLOTS_KEY = "package_slots"
DEFAULT_PACKAGE_SIZE = 50

# Auto-select trays: when on, an above-floor headstamp that isn't assigned to
# any slot is auto-routed to the first empty slot.
_RUN_AUTO_SELECT_KEY = "run_auto_select_trays"

# Sorting templates: named layouts of the Run tab's slot assignments, so one
# model can carry several bin arrangements. The live assignments (the
# `headstamps`/`headstamp_parents` slot columns and the package slot map) stay
# the single source of truth a run reads; the *active* template is kept in
# lock-step with them (see `sync_active_slot_template`) so switching templates
# is a straight save-current / load-next swap. Templates are per model AND per
# run mode — package mode's many-to-many assignments are a different shape.
# Key: `active_slot_template:<model id|ai>:<mode>` -> template row id.
_ACTIVE_TEMPLATE_KEY = "active_slot_template"
DEFAULT_SLOT_TEMPLATE_NAME = "Default"

# Sort While Training: send xf:<slot> for a labelled case instead of xf:0
# during training.
_SORT_WHILE_TRAINING_KEY = "sort_while_training"


def _merge_defaults(defaults: Any, loaded: Any) -> Any:
    """Recursive default merge: any key missing in `loaded` falls back to defaults."""
    if isinstance(defaults, dict) and isinstance(loaded, dict):
        out: dict[str, Any] = {}
        for k, v in defaults.items():
            out[k] = _merge_defaults(v, loaded.get(k, v))
        for k in loaded:
            if k not in out:
                out[k] = loaded[k]
        return out
    return loaded if loaded is not None else defaults


class Config:
    """In-memory mirror of the persisted app settings.

    Headstamps live in their own SQLite table (managed by HeadstampRepo) and
    are read fresh on every access — they are NOT part of the cached
    settings snapshot, because they get mutated through several call paths
    (Models tab editor, Train tab Save, Community import) and caching a
    stale snapshot here used to silently wipe rows whenever any other tab
    happened to call ``config.save()``.
    """

    def __init__(self, db: Database) -> None:
        self.db = db
        self.settings = SettingsRepo(db)
        self.headstamps_repo = HeadstampRepo(db)
        self.parents_repo = HeadstampParentRepo(db)
        self.templates_repo = SlotTemplateRepo(db)
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)

    def load(self) -> Config:
        for key in _SECTIONS:
            stored = self.settings.get(key)
            if stored is not None:
                self.data[key] = _merge_defaults(copy.deepcopy(DEFAULTS[key]), stored)
            else:
                self.data[key] = copy.deepcopy(DEFAULTS[key])
        return self

    def reload_headstamps_for_active_model(self) -> None:
        """No-op kept for callers (`tab_models._activate`).

        Headstamps are now always read fresh from the DB so there is no
        cached copy to invalidate. The method stays so existing call sites
        don't need to change.
        """
        return

    def save(self) -> None:
        """Persist the cached settings sections. Does NOT touch headstamps."""
        with self.db.transaction() as _:
            for key in _SECTIONS:
                self.settings.set(key, self.data[key])

    # --- public surface (matches old JSON Config) ---------------------------

    @property
    def api(self) -> dict[str, Any]:
        return self.data["api"]

    @property
    def headstamps(self) -> list[dict[str, Any]]:
        """Always returns a freshly-read list of headstamps for the active model.

        In AI Config mode (no active local model) headstamps live in a
        settings entry instead of the model-scoped headstamps table.
        """
        active_id = self.settings.get_active_model_id()
        if active_id is None:
            return list(self._read_ai_headstamps())
        rows = self.headstamps_repo.list_for_model(active_id)
        return [{"name": r.name, "slot": r.slot} for r in rows]

    # ----- AI Config-mode headstamp storage ---------------------------------

    def _read_ai_headstamps(self) -> list[dict[str, Any]]:
        raw = self.settings.get(_AI_HEADSTAMPS_KEY) or []
        out: list[dict[str, Any]] = []
        for entry in raw:
            name = (entry or {}).get("name") if isinstance(entry, dict) else None
            if not name:
                continue
            out.append({"name": str(name), "slot": int((entry or {}).get("slot", 0))})
        return out

    def _write_ai_headstamps(self, entries: list[dict[str, Any]]) -> None:
        self.settings.set(_AI_HEADSTAMPS_KEY, entries)

    # ----- headstamp mutations (write straight through to the repo) ---------

    def add_headstamp(self, name: str, slot: int = 0) -> bool:
        """Add a headstamp for the active context. Returns False if `name`
        is empty or already present.
        """
        if not name:
            return False
        active_id = self.settings.get_active_model_id()
        if active_id is None:
            current = self._read_ai_headstamps()
            if any(e["name"] == name for e in current):
                return False
            current.append({"name": name, "slot": int(slot)})
            self._write_ai_headstamps(current)
            if int(slot) > 0:
                self.sync_active_slot_template("standard")
            return True
        existing = {h.name for h in self.headstamps_repo.list_for_model(active_id)}
        if name in existing:
            return False
        try:
            self.headstamps_repo.add(active_id, name, slot)
        except Exception:
            return False
        if int(slot) > 0:
            self.sync_active_slot_template("standard")
        return True

    def remove_headstamp(self, name: str) -> bool:
        active_id = self.settings.get_active_model_id()
        if active_id is None:
            current = self._read_ai_headstamps()
            remaining = [e for e in current if e["name"] != name]
            if len(remaining) == len(current):
                return False
            self._write_ai_headstamps(remaining)
            return True
        for h in self.headstamps_repo.list_for_model(active_id):
            if h.name == name:
                self.headstamps_repo.delete(h.id)
                return True
        return False

    def clear_headstamps(self) -> None:
        active_id = self.settings.get_active_model_id()
        if active_id is None:
            self._write_ai_headstamps([])
            return
        self.headstamps_repo.clear_for_model(active_id)

    def set_headstamps(self, entries: list[dict[str, Any]]) -> None:
        """Replace all headstamps for the active context (model or AI Config)."""
        active_id = self.settings.get_active_model_id()
        if active_id is None:
            normalised = [{"name": str(e["name"]), "slot": int(e.get("slot", 0))} for e in entries if e.get("name")]
            self._write_ai_headstamps(normalised)
            return
        self.headstamps_repo.replace_for_model(active_id, entries)

    def set_headstamp_slot(self, name: str, slot: int) -> bool:
        """Update the slot assignment for a single headstamp. Returns False if
        the headstamp doesn't exist for the active context. Used by the Run
        tab's slot-details checkboxes — those used to mutate the dicts
        returned by ``config.headstamps`` directly, but that's a no-op now
        that the property reads fresh on every access.
        """
        active_id = self.settings.get_active_model_id()
        if active_id is None:
            current = self._read_ai_headstamps()
            for entry in current:
                if entry["name"] == name:
                    entry["slot"] = int(slot)
                    self._write_ai_headstamps(current)
                    self.sync_active_slot_template("standard")
                    return True
            return False
        for h in self.headstamps_repo.list_for_model(active_id):
            if h.name == name:
                self.headstamps_repo.update_slot(h.id, int(slot))
                self.sync_active_slot_template("standard")
                return True
        return False

    @property
    def serial(self) -> dict[str, Any]:
        return self.data["serial"]

    @property
    def image_proc(self) -> dict[str, Any]:
        return self.data["image_proc"]

    @property
    def camera(self) -> dict[str, Any]:
        return self.data["camera"]

    def slot_for_headstamp(self, name: str) -> int | None:
        """Resolve the physical bin for a classified label.

        In parent-classification mode a child label routes to *its parent's*
        slot, while an orphan (parentless) headstamp routes to its own slot.
        Otherwise routing is the standard per-headstamp lookup. Returns None
        when the label maps to nothing (caller falls back to catch-all).
        """
        mid = self.settings.get_active_model_id()
        if mid is not None and self.use_parent_classifications:
            parents = {p.id: p for p in self.parents_repo.list_for_model(mid)}
            if parents:
                headstamps = self.headstamps_repo.list_for_model(mid)
                hs = next((h for h in headstamps if h.name == name), None)
                if hs is not None:
                    if hs.parent_id is not None and hs.parent_id in parents:
                        return int(parents[hs.parent_id].slot)
                    return int(hs.slot)
                # The label may already be a parent name (parent-trained model).
                parent = next((p for p in parents.values() if p.name == name), None)
                return int(parent.slot) if parent is not None else None

        for entry in self.headstamps:
            if entry.get("name") == name:
                return int(entry.get("slot", 0))
        return None

    # ----- parent classifications --------------------------------------------

    def model_has_parents(self) -> bool:
        """True when the active local model has at least one parent defined.

        Drives whether the "Use Parent Classifications" run option is shown.
        Always False in AI Config mode (those headstamps have no parents).
        """
        mid = self.settings.get_active_model_id()
        if mid is None:
            return False
        return bool(self.parents_repo.list_for_model(mid))

    @property
    def use_parent_classifications(self) -> bool:
        """Per-model runtime preference. False in AI Config mode."""
        mid = self.settings.get_active_model_id()
        if mid is None:
            return False
        return bool(self.settings.get(f"{_USE_PARENT_RUNTIME_KEY}:{mid}", False))

    def set_use_parent_classifications(self, value: bool) -> bool:
        mid = self.settings.get_active_model_id()
        if mid is None:
            return False
        self.settings.set(f"{_USE_PARENT_RUNTIME_KEY}:{mid}", bool(value))
        return True

    def parents_with_slots(self) -> list[dict[str, Any]]:
        """[{id, name, slot}] for the active model's parents (empty in AI mode)."""
        mid = self.settings.get_active_model_id()
        if mid is None:
            return []
        return [{"id": p.id, "name": p.name, "slot": int(p.slot)} for p in self.parents_repo.list_for_model(mid)]

    def headstamps_with_parents(self) -> list[dict[str, Any]]:
        """[{name, slot, parent_id}] for the active model (parent_id None in AI mode)."""
        mid = self.settings.get_active_model_id()
        if mid is None:
            return [
                {"name": e["name"], "slot": int(e.get("slot", 0)), "parent_id": None}
                for e in self._read_ai_headstamps()
            ]
        return [
            {"name": h.name, "slot": int(h.slot), "parent_id": h.parent_id}
            for h in self.headstamps_repo.list_for_model(mid)
        ]

    def set_parent_slot(self, parent_id: int, slot: int) -> bool:
        """Assign a parent classification to a physical slot. Local models only."""
        mid = self.settings.get_active_model_id()
        if mid is None:
            return False
        self.parents_repo.update_slot(int(parent_id), int(slot))
        self.sync_active_slot_template("standard")
        return True

    def parent_for_headstamp(self, name: str) -> str | None:
        """The parent classification name for a child headstamp, or None.

        Returns None in AI Config mode, for orphan (parentless) headstamps, or
        for unknown labels. Independent of the runtime toggle — callers decide
        whether to surface it.
        """
        mid = self.settings.get_active_model_id()
        if mid is None:
            return None
        hs = next(
            (h for h in self.headstamps_repo.list_for_model(mid) if h.name == name),
            None,
        )
        if hs is None or hs.parent_id is None:
            return None
        parent = self.parents_repo.get(hs.parent_id)
        return parent.name if parent else None

    # ----- run options (app-level) -------------------------------------------

    @property
    def run_confidence_floor(self) -> int:
        """Minimum confidence (%) a prediction must reach to leave the catch-all.

        Predictions below this route to slot 0. 0 disables the floor.
        """
        try:
            return int(self.settings.get(_RUN_CONFIDENCE_FLOOR_KEY, DEFAULT_CONFIDENCE_FLOOR))
        except (TypeError, ValueError):
            return DEFAULT_CONFIDENCE_FLOOR

    def set_run_confidence_floor(self, value: int) -> None:
        self.settings.set(_RUN_CONFIDENCE_FLOOR_KEY, max(0, min(100, int(value))))

    @property
    def run_store_images(self) -> str:
        """One of STORE_IMAGES_MODES; controls run-image capture."""
        value = self.settings.get(_RUN_STORE_IMAGES_KEY, "none")
        return value if value in STORE_IMAGES_MODES else "none"

    def set_run_store_images(self, mode: str) -> None:
        if mode in STORE_IMAGES_MODES:
            self.settings.set(_RUN_STORE_IMAGES_KEY, mode)

    # ----- package mode (batch sorting) --------------------------------------

    @property
    def run_package_mode(self) -> bool:
        return bool(self.settings.get(_RUN_PACKAGE_MODE_KEY, False))

    def set_run_package_mode(self, value: bool) -> None:
        self.settings.set(_RUN_PACKAGE_MODE_KEY, bool(value))

    @property
    def run_package_size(self) -> int:
        try:
            value = int(self.settings.get(_RUN_PACKAGE_SIZE_KEY, DEFAULT_PACKAGE_SIZE))
        except (TypeError, ValueError):
            value = DEFAULT_PACKAGE_SIZE
        return value if value > 0 else DEFAULT_PACKAGE_SIZE

    def set_run_package_size(self, value: int) -> None:
        try:
            value = int(value)
        except (TypeError, ValueError):
            return
        self.settings.set(_RUN_PACKAGE_SIZE_KEY, max(1, value))

    def _package_slots_key(self) -> str:
        """Settings key for the active context's package slot assignments.

        Package assignments are model-scoped (each model has its own bins),
        with a single shared bucket for AI Config mode where there is no model.
        """
        mid = self.settings.get_active_model_id()
        return f"{_PACKAGE_SLOTS_KEY}:{mid if mid is not None else 'ai'}"

    def package_slot_map(self) -> dict[int, list[str]]:
        """slot -> [headstamp names] for the active context's package config."""
        raw = self.settings.get(self._package_slots_key()) or {}
        out: dict[int, list[str]] = {}
        if isinstance(raw, dict):
            for k, names in raw.items():
                try:
                    slot = int(k)
                except (TypeError, ValueError):
                    continue
                out[slot] = [str(n) for n in (names or []) if n]
        return out

    def headstamps_in_package_slot(self, slot: int) -> list[str]:
        return list(self.package_slot_map().get(int(slot), []))

    def slots_for_headstamp_package(self, name: str) -> list[int]:
        """Every (non-catch-all) slot the headstamp is assigned to in package mode."""
        return sorted(s for s, names in self.package_slot_map().items() if s > 0 and name in names)

    def set_package_slot_headstamp(self, slot: int, name: str, enabled: bool) -> None:
        """Add/remove a headstamp from a package slot's assignment list.

        Unlike the single-slot routing this is many-to-many: a headstamp may be
        ticked into several slots so the run can fill them in batches.
        """
        if int(slot) <= 0 or not name:
            return
        raw = self.settings.get(self._package_slots_key()) or {}
        if not isinstance(raw, dict):
            raw = {}
        key = str(int(slot))
        names = [str(n) for n in (raw.get(key) or []) if n]
        if enabled:
            if name not in names:
                names.append(name)
        else:
            names = [n for n in names if n != name]
        raw[key] = names
        self.settings.set(self._package_slots_key(), raw)
        self.sync_active_slot_template("package")

    # ----- sorting templates --------------------------------------------------

    def slot_template_mode(self) -> str:
        """Which template list applies right now: 'package' or 'standard'."""
        return "package" if self.run_package_mode else "standard"

    @staticmethod
    def _template_key_for(model_id: int | None, mode: str) -> str:
        return f"{_ACTIVE_TEMPLATE_KEY}:{model_id if model_id is not None else 'ai'}:{mode}"

    def _active_template_key(self, mode: str) -> str:
        return self._template_key_for(self.settings.get_active_model_id(), mode)

    def _resolve_template_mode(self, mode: str | None) -> str:
        if mode is None:
            return self.slot_template_mode()
        if mode not in SLOT_TEMPLATE_MODES:
            raise ValueError(f"Unsupported slot-template mode: {mode!r}")
        return mode

    def capture_slot_assignments(self, mode: str | None = None) -> dict[str, Any]:
        """Snapshot the live slot assignments for `mode` into a template payload.

        Only non-catch-all assignments are stored; anything not mentioned is
        restored to slot 0 (unassigned) when the payload is applied.
        """
        mode = self._resolve_template_mode(mode)
        if mode == "package":
            return {
                "slots": {
                    str(int(slot)): list(names)
                    for slot, names in sorted(self.package_slot_map().items())
                    if int(slot) > 0 and names
                }
            }
        return {
            "headstamps": {str(e["name"]): int(e.get("slot", 0)) for e in self.headstamps if int(e.get("slot", 0)) > 0},
            "parents": {str(p["name"]): int(p["slot"]) for p in self.parents_with_slots() if int(p["slot"]) > 0},
        }

    def apply_slot_assignments(
        self,
        assignments: dict[str, Any] | None,
        mode: str | None = None,
    ) -> None:
        """Write a template payload back onto the live assignments.

        Names the payload doesn't mention are cleared to slot 0, so applying a
        template fully replaces the layout rather than merging into it. Unknown
        names (a headstamp deleted since the template was saved) are ignored.
        """
        mode = self._resolve_template_mode(mode)
        data = assignments if isinstance(assignments, dict) else {}

        if mode == "package":
            raw = data.get("slots")
            cleaned: dict[str, list[str]] = {}
            if isinstance(raw, dict):
                for key, names in raw.items():
                    try:
                        slot = int(key)
                    except (TypeError, ValueError):
                        continue
                    if slot <= 0:
                        continue
                    cleaned[str(slot)] = [str(n) for n in (names or []) if n]
            self.settings.set(self._package_slots_key(), cleaned)
            return

        raw_hs_slots = data.get("headstamps")
        hs_slots: dict[str, Any] = raw_hs_slots if isinstance(raw_hs_slots, dict) else {}
        raw_parent_slots = data.get("parents")
        parent_slots: dict[str, Any] = raw_parent_slots if isinstance(raw_parent_slots, dict) else {}

        def _slot_of(table: dict[str, Any], name: str) -> int:
            try:
                return max(0, int(table.get(name, 0)))
            except (TypeError, ValueError):
                return 0

        mid = self.settings.get_active_model_id()
        with self.db.transaction() as _:
            if mid is None:
                entries = self._read_ai_headstamps()
                for entry in entries:
                    entry["slot"] = _slot_of(hs_slots, entry["name"])
                self._write_ai_headstamps(entries)
                return
            for h in self.headstamps_repo.list_for_model(mid):
                desired = _slot_of(hs_slots, h.name)
                if int(h.slot) != desired:
                    self.headstamps_repo.update_slot(h.id, desired)
            for p in self.parents_repo.list_for_model(mid):
                desired = _slot_of(parent_slots, p.name)
                if int(p.slot) != desired:
                    self.parents_repo.update_slot(p.id, desired)

    def list_slot_templates(self, mode: str | None = None) -> list[SlotTemplate]:
        """Templates for the active model + `mode`, newest scope seeded lazily.

        The first read of a scope with no templates creates "Default" holding
        whatever is currently assigned, so upgrading users keep their layout
        and land on a named template without doing anything.
        """
        mode = self._resolve_template_mode(mode)
        mid = self.settings.get_active_model_id()
        rows = self.templates_repo.list_for_scope(mid, mode)
        if rows:
            return rows
        with self.db.transaction() as _:
            template = self.templates_repo.create(
                mid,
                mode,
                DEFAULT_SLOT_TEMPLATE_NAME,
                self.capture_slot_assignments(mode),
            )
            self.settings.set(self._active_template_key(mode), template.id)
        return [template]

    def active_slot_template(self, mode: str | None = None) -> SlotTemplate:
        """The template currently driving the live assignments for `mode`."""
        mode = self._resolve_template_mode(mode)
        rows = self.list_slot_templates(mode)
        stored = self.settings.get(self._active_template_key(mode))
        for row in rows:
            if row.id == stored:
                return row
        # Pointer missing or stale (template deleted elsewhere): adopt the first
        # one without applying it — the live assignments are what the user last
        # worked on and the next sync writes them into it.
        self.settings.set(self._active_template_key(mode), rows[0].id)
        return rows[0]

    def sync_active_slot_template(self, mode: str | None = None) -> None:
        """Persist the live assignments into the active template.

        Called after every slot-assignment mutation so the active template
        never drifts from what the Run tab shows — there is no explicit "save
        template" step.
        """
        mode = self._resolve_template_mode(mode)
        template = self.active_slot_template(mode)
        self.templates_repo.update_assignments(
            template.id,
            self.capture_slot_assignments(mode),
        )

    def activate_slot_template(
        self,
        template_id: int,
        mode: str | None = None,
    ) -> SlotTemplate | None:
        """Switch to another template: save the outgoing one, load the incoming.

        Returns the newly active template, or None when `template_id` doesn't
        belong to the active model + mode.
        """
        mode = self._resolve_template_mode(mode)
        mid = self.settings.get_active_model_id()
        target = self.templates_repo.get(int(template_id))
        if target is None or target.mode != mode or target.model_id != mid:
            return None
        current = self.active_slot_template(mode)
        if current.id == target.id:
            return current
        with self.db.transaction() as _:
            self.templates_repo.update_assignments(
                current.id,
                self.capture_slot_assignments(mode),
            )
            self.apply_slot_assignments(target.assignments, mode)
            self.settings.set(self._active_template_key(mode), target.id)
        return target

    def create_slot_template(
        self,
        name: str,
        *,
        copy_current: bool = True,
        mode: str | None = None,
    ) -> SlotTemplate:
        """Create a template and make it active.

        With `copy_current` the new template starts as a copy of the live
        assignments (the outgoing template keeps its own copy); without it the
        slots are cleared so the user starts from a blank layout.

        Raises ValueError on an empty or duplicate name.
        """
        mode = self._resolve_template_mode(mode)
        name = (name or "").strip()
        if not name:
            raise ValueError("Enter a name for the template.")
        mid = self.settings.get_active_model_id()
        if self.templates_repo.find_by_name(mid, mode, name) is not None:
            raise ValueError(f"A template named “{name}” already exists.")
        current = self.active_slot_template(mode)
        payload = (
            self.capture_slot_assignments(mode)
            if copy_current
            else ({"slots": {}} if mode == "package" else {"headstamps": {}, "parents": {}})
        )
        with self.db.transaction() as _:
            # Flush the outgoing template first so nothing unsaved is lost.
            self.templates_repo.update_assignments(
                current.id,
                self.capture_slot_assignments(mode),
            )
            template = self.templates_repo.create(mid, mode, name, payload)
            self.apply_slot_assignments(payload, mode)
            self.settings.set(self._active_template_key(mode), template.id)
        return template

    def rename_slot_template(self, template_id: int, name: str) -> SlotTemplate | None:
        """Rename a template. Raises ValueError on an empty or duplicate name."""
        template = self.templates_repo.get(int(template_id))
        if template is None:
            return None
        name = (name or "").strip()
        if not name:
            raise ValueError("Enter a name for the template.")
        if name == template.name:
            return template
        clash = self.templates_repo.find_by_name(template.model_id, template.mode, name)
        if clash is not None and clash.id != template.id:
            raise ValueError(f"A template named “{name}” already exists.")
        self.templates_repo.rename(template.id, name)
        template.name = name
        return template

    def delete_slot_template(self, template_id: int) -> SlotTemplate | None:
        """Delete a template and return whichever one is active afterwards.

        Deleting the active template loads the next remaining one. The last
        template in a scope can't be deleted — there is always somewhere for
        the current assignments to live.
        """
        template = self.templates_repo.get(int(template_id))
        if template is None:
            return None
        mode = template.mode
        remaining = [t for t in self.templates_repo.list_for_scope(template.model_id, mode) if t.id != template.id]
        if not remaining:
            raise ValueError("A model needs at least one sorting template.")
        key = self._template_key_for(template.model_id, mode)
        was_active = self.settings.get(key) == template.id
        # Only touch the live assignments when the template being deleted is
        # the one currently loaded for the active model.
        loaded = was_active and template.model_id == self.settings.get_active_model_id()
        with self.db.transaction() as _:
            self.templates_repo.delete(template.id)
            if was_active:
                if loaded:
                    self.apply_slot_assignments(remaining[0].assignments, mode)
                self.settings.set(key, remaining[0].id)
        return remaining[0] if was_active else self.active_slot_template(mode)

    # ----- auto-select / sort-while-training toggles -------------------------

    @property
    def run_auto_select_trays(self) -> bool:
        return bool(self.settings.get(_RUN_AUTO_SELECT_KEY, False))

    def set_run_auto_select_trays(self, value: bool) -> None:
        self.settings.set(_RUN_AUTO_SELECT_KEY, bool(value))

    @property
    def sort_while_training(self) -> bool:
        return bool(self.settings.get(_SORT_WHILE_TRAINING_KEY, False))

    def set_sort_while_training(self, value: bool) -> None:
        self.settings.set(_SORT_WHILE_TRAINING_KEY, bool(value))

    # ----- empty-slot discovery (auto-select trays) --------------------------

    def first_empty_slot(self, *, package: bool | None = None) -> int | None:
        """The lowest slot number (>0) with no headstamp/parent assigned.

        Honours `serial.slot_quantity` for the upper bound. In package mode the
        package assignment map is consulted; otherwise the single-slot routing
        plus any parent-slot assignments are considered "occupied".
        """
        if package is None:
            package = self.run_package_mode
        slot_count = int(self.serial.get("slot_quantity", 8))
        occupied: set[int] = set()
        if package:
            for s, names in self.package_slot_map().items():
                if names:
                    occupied.add(int(s))
        elif self.use_parent_classifications:
            # Parent mode: parent groups and ungrouped headstamps occupy slots.
            for p in self.parents_with_slots():
                if int(p["slot"]) > 0:
                    occupied.add(int(p["slot"]))
            for h in self.headstamps_with_parents():
                if h["parent_id"] is None and int(h["slot"]) > 0:
                    occupied.add(int(h["slot"]))
        else:
            # Child mode: only per-headstamp slots matter. Parent-slot
            # assignments belong to the other runtime mode and must not push
            # auto-select past empty child slots.
            for entry in self.headstamps:
                slot = int(entry.get("slot", 0))
                if slot > 0:
                    occupied.add(slot)
        for slot in range(1, max(1, slot_count)):
            if slot not in occupied:
                return slot
        return None

    def assign_headstamp_to_empty_slot(self, name: str) -> int | None:
        """Route an unassigned headstamp to the first empty slot. Returns the
        slot it landed in, or None when there is no free slot.

        Respects existing assignments and only ever places one headstamp into
        an empty slot.
        """
        if not name:
            return None
        package = self.run_package_mode
        if package:
            if self.slots_for_headstamp_package(name):
                return None  # already assigned somewhere
        elif self.slot_for_headstamp(name):
            return None
        slot = self.first_empty_slot(package=package)
        if slot is None:
            return None
        if package:
            self.set_package_slot_headstamp(slot, name, True)
        else:
            self.set_headstamp_slot(name, slot)
        return slot
