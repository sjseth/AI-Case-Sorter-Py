"""Single-image classify→sort loop.

The happy path: feed one → capture frame → crop → optional primer mask →
classify → look up slot (below floor or unknown → slot 0) → sort to slot →
post UI update. Loops until Stop pressed.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from typing import Any

from .. import paths
from ..community.feedback import FeedbackService
from ..data.config import Config
from ..data.repository import ModelRepo
from ..hardware import image_proc, serial_broker
from ..ml import classifier
from ..training.dataset import save_training_image
from .events import EventBus

log = logging.getLogger(__name__)

SlotCallback = Callable[[int], None]

DISCONNECT_ERROR = "Serial disconnected"

# End-of-brass flush. The wheel holds a pipeline (feed at position 1, camera
# at 3, drop at 5), so when the hopper runs dry up to three cases still sit
# past the feed sensor — cases the run has fed but not yet dropped. The flush
# walks each of them through the camera with forced feeds and sorts every one
# to its true slot, so a run ends with the wheel empty and nothing stranded.
#
# If this many CONSECUTIVE flush cycles each deliver a real case, the hopper
# is provably still feeding — the end-of-brass call was a false alarm (a
# delivery gap while a case fell from the collator), and the run resumes at
# full speed instead of walking the whole hopper at flush pace.
FLUSH_RESUME_CASES = 6


class RunController:
    def __init__(self, *, config: Config, broker, camera, bus: EventBus, db: Any = None) -> None:
        self.config = config
        self.broker = broker
        self.camera = camera
        self.bus = bus
        self.db = db
        self._feedback = FeedbackService(db) if db is not None else None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # Package-mode batch counters: slot -> cases dropped this run. Owned by
        # the run thread but read/reset from the UI thread (live reset button),
        # so every access is guarded. The catch-all (0) is never batched.
        self._package_counts: dict[int, int] = {}
        self._package_lock = threading.Lock()
        # Slot to send on the NEXT wheel-rotate command. The 5-hole wheel
        # holds a pipeline: feed at position 1, camera at 3, drop at 5; each
        # rotation drops position 5's case (which we classified one or two
        # rotations ago) and brings a new case to position 3. We pass the
        # previously-classified slot so the case that's about to drop lands
        # in the right slot. Default 0 (catch-all) on a fresh controller —
        # the first Manual feed click or continuous Run prime sends that.
        self._last_classified_slot = 0

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event.clear()
        # NB: package batch counters are intentionally NOT reset here — a run
        # that's stopped and restarted resumes its batches where it left off.
        # They clear only via the Reset counters button or a per-slot reset.
        self._thread = threading.Thread(target=self._loop, name="RunController", daemon=True)
        self._thread.start()
        self.bus.post("run/started", None)

    # ----- package-mode counters ---------------------------------------------

    def reset_package_counts(self) -> None:
        with self._package_lock:
            self._package_counts.clear()

    def reset_package_slot(self, slot: int) -> None:
        """Zero one slot's batch counter (live reset button on a slot card)."""
        with self._package_lock:
            self._package_counts[int(slot)] = 0

    def package_count(self, slot: int) -> int:
        with self._package_lock:
            return self._package_counts.get(int(slot), 0)

    def stop(self) -> None:
        self._stop_event.set()
        try:
            self.broker.stop_run()
        except Exception:
            pass

    def _parent_label(self, label: str) -> str | None:
        """Resolve a prediction's parent group, but only in parent mode.

        Returns None when parent classifications are disabled, the label is an
        orphan headstamp, or there's no active local model.
        """
        if label and self.config.use_parent_classifications:
            return self.config.parent_for_headstamp(label)
        return None

    def _board_error(self, timeout_message: str) -> str:
        """What to report when a board command didn't complete.

        A dropped link answers nothing, so every command "times out" on one —
        and a timeout is also what a merely slow board produces. Ask the broker
        which it was rather than making the operator guess (issue #35).
        """
        if not getattr(self.broker, "is_connected", True):
            return DISCONNECT_ERROR
        return timeout_message

    def _above_floor(self, confidence: float) -> bool:
        floor = self.config.run_confidence_floor
        return floor <= 0 or float(confidence) >= floor

    def _route_slot(self, label: str, confidence: float) -> tuple[int, bool]:
        """Destination slot for a prediction, honoring the confidence floor.

        Below-floor predictions are forced to the catch-all (slot 0). Returns
        ``(slot, above_floor)``.
        """
        slot, above_floor, _halt = self._resolve_destination(label, confidence)
        return slot, above_floor

    def _resolve_destination(self, label: str, confidence: float) -> tuple[int, bool, bool]:
        """Full routing decision for a prediction: ``(slot, above_floor, halt)``.

        Order:
          1. confidence floor (below → catch-all),
          2. auto-select trays (assign an unmapped headstamp to an empty slot),
          3. package-mode batch routing OR plain single-slot routing.

        ``halt`` is only ever True in package mode when every slot configured
        for the headstamp is already full.
        """
        above_floor = self._above_floor(confidence)
        if above_floor:
            self._maybe_auto_select(label)
        if self.config.run_package_mode:
            slot, halt = self._route_slot_package(label, above_floor)
            return slot, above_floor, halt
        if not above_floor:
            return 0, False, False
        slot = self.config.slot_for_headstamp(label)
        return (slot if slot is not None else 0), above_floor, False

    def _maybe_auto_select(self, label: str) -> None:
        """Assign an above-floor, unmapped headstamp to the first empty slot.

        No-op unless the run option is on. Posts ``run/assignment_changed`` so
        the Run tab re-renders the slot cards + details with the new mapping.
        """
        if not (label and self.config.run_auto_select_trays):
            return
        slot = self.config.assign_headstamp_to_empty_slot(label)
        if slot is not None:
            self.bus.post("run/assignment_changed", {"label": label, "slot": slot})

    def _route_slot_package(self, label: str, above_floor: bool) -> tuple[int, bool]:
        """Package-mode routing: fill the fullest non-full slot for ``label``.

        Returns ``(slot, halt)``. Below-floor or unmapped headstamps land in the
        catch-all without halting. When the headstamp IS configured but all its
        slots have reached the batch size, returns ``(0, True)`` to stop the run.
        """
        if not above_floor:
            return 0, False
        slots = self.config.slots_for_headstamp_package(label)
        if not slots:
            return 0, False
        batch = self.config.run_package_size
        with self._package_lock:
            candidates = [s for s in slots if self._package_counts.get(s, 0) < batch]
            if not candidates:
                return 0, True
            # Highest current count first: top off the bin nearest a full batch
            # before starting the next one (mirrors OrderByDescending).
            candidates.sort(key=lambda s: self._package_counts.get(s, 0), reverse=True)
            return candidates[0], False

    def _commit_package_count(self, slot: int, label: str, above_floor: bool) -> int | None:
        """Tally one case into a package slot after a successful sort.

        Returns the slot's new count, or None when nothing was counted (catch-all,
        below floor, or package mode off). Posts ``run/package_full`` once the
        batch size is reached so the UI can ring the non-blocking bell.
        """
        if not (self.config.run_package_mode and above_floor and slot and slot > 0):
            return None
        batch = self.config.run_package_size
        with self._package_lock:
            count = self._package_counts.get(slot, 0) + 1
            self._package_counts[slot] = count
        if count >= batch:
            self.bus.post("run/package_full", {"slot": slot, "label": label, "count": count})
        return count

    def _post_history(self, result: dict[str, Any]) -> None:
        """Feed the Monitor window one record (image + classification)."""
        if result.get("cropped") is None:
            return
        self.bus.post(
            "run/history",
            {
                "image": result.get("cropped"),
                "label": result.get("label", ""),
                "parent": result.get("parent"),
                "confidence": result.get("confidence", 0),
                "slot": result.get("slot", 0),
            },
        )

    def _maybe_store_run_image(self, image_bgr, label: str, above_floor: bool) -> None:
        """Persist the run image per the Store Images preference.

        Files land in ``data/models/<id>/run_images`` using the training
        filename convention, so a run can be folded back into a dataset.
        Best-effort — a storage failure never interrupts a run.
        """
        mode = self.config.run_store_images
        if mode == "none" or image_bgr is None:
            return
        if mode == "all":
            store = True
        elif mode == "above":
            store = above_floor
        else:  # "below"
            store = not above_floor
        if not store:
            return
        model_id = self.config.settings.get_active_model_id()
        if model_id is None:
            return  # AI Config mode has no per-model folder
        try:
            save_training_image(
                image_bgr,
                paths.model_run_images_dir(model_id),
                label or "unknown",
            )
        except Exception:
            log.exception("storing the run image failed")

    # ----- community feedback loop -------------------------------------------

    def refresh_wish_list(self, *, auth: Any) -> list[str]:
        """Fetch the active model's wish list for the run about to start.

        Blocking (one HTTPS round-trip) — the UI calls this from a worker
        thread, never from the run loop, so a slow or unreachable server can't
        delay a run. Fails open to an empty list.
        """
        if self._feedback is None:
            return []
        model_id = self.config.settings.get_active_model_id()
        model = ModelRepo(self.db).get(model_id) if model_id is not None else None
        return self._feedback.refresh_wish_list(model, auth=auth)

    def clear_wish_list(self) -> None:
        """Forget the current wish list (and its per-run capture quotas)."""
        if self._feedback is not None:
            self._feedback.clear_wish_list()

    def apply_community_settings(
        self,
        model_id: int | None,
        *,
        confidence_floor: int = 0,
        feedback_enabled: bool = True,
        blocked: bool = False,
        wish_list: Sequence[str] = (),
    ) -> None:
        """Install the server's settings for the active community model.

        Called by the UI after a ``FetchModelSettings`` round-trip; delegates to
        the feedback service, which owns the capture decision. An empty
        ``wish_list`` leaves the installed one alone — the Start-time fetch is
        still the authority on that.
        """
        if self._feedback is None:
            return
        self._feedback.apply_server_settings(
            model_id,
            confidence_floor=confidence_floor,
            feedback_enabled=feedback_enabled,
            blocked=blocked,
        )
        if wish_list:
            self._feedback.set_wish_list(model_id, wish_list)

    def clear_community_settings(self) -> None:
        """Drop the server's settings — the local floor and opt-in apply again."""
        if self._feedback is not None:
            self._feedback.clear_server_settings()

    def _maybe_capture_feedback(
        self,
        image_bgr,
        label: str,
        confidence: float,
        *,
        wish_list: bool = False,
    ) -> None:
        """Stage a prediction for the community feedback loop.

        Captured when confidence falls below the publisher's own threshold
        (``model.feedback_loop_confidence_floor``, independent of the run
        confidence floor) or — during a continuous run only — when the label is
        on the model's wish list. Best-effort; a failure here never interrupts
        a run. Posts ``feedback/queued`` so the Run tab (which holds the auth
        token) can drive the upload.
        """
        if self._feedback is None or image_bgr is None:
            log.debug(
                "run-hook skipped (service=%s, image=%s)",
                self._feedback is not None,
                image_bgr is not None,
            )
            return
        model_id = self.config.settings.get_active_model_id()
        if model_id is None:
            log.debug("run-hook: no active model (AI Config mode) — feedback not applicable")
            return
        log.debug("run-hook: model_id=%s label=%r confidence=%s", model_id, label, confidence)
        try:
            model = ModelRepo(self.db).get(model_id)
            if model is None:
                # The active-model id points at a row that no longer exists
                # (e.g. deleted between the settings read and here) — nothing
                # to capture feedback against.
                log.debug("run-hook: active model %s not found in DB — feedback not applicable", model_id)
                return
            if not self._feedback.should_capture(
                model,
                confidence,
                label,
                wish_list=wish_list,
            ):
                return
            if self._feedback.capture(model, image_bgr, label, confidence):
                log.debug(
                    "posting feedback/queued for model %s (upload_mode=%s)",
                    model_id,
                    model.feedback_loop_upload_mode,
                )
                self.bus.post(
                    "feedback/queued",
                    {"model_id": model_id, "upload_mode": model.feedback_loop_upload_mode},
                )
        except Exception:
            log.debug("run-hook EXCEPTION", exc_info=True)

    # ----- one iteration ------------------------------------------------------

    def test_once(self) -> dict[str, Any]:
        """Feed → capture → crop → classify. No sort, no slot routing.

        Posts incremental progress on the bus so the UI can paint the cropped
        frame the moment it's available, rather than waiting for the (slower)
        HTTP classify call to come back. Topics:
          - test/status     (str) — human-readable stage label
          - test/cropped    (np.ndarray) — 480x480 BGR cropped frame
          - test/classified ({"label", "confidence"}) — final result
          - test/error      (str)
        """
        result: dict[str, Any] = {
            "ok": False,
            "label": "",
            "parent": None,
            "confidence": 0,
            "cropped": None,
            "error": None,
        }

        def _fail(msg: str) -> dict[str, Any]:
            result["error"] = msg
            self.bus.post("test/error", msg)
            return result

        try:
            self.bus.post("test/status", "Feeding…")
            if not self.broker.feed_one():
                return _fail(self._board_error("Feed timeout"))

            self.bus.post("test/status", "Capturing & cropping…")
            frame = self.camera.capture_frame()
            if frame is None:
                return _fail("Camera capture failed")

            cropped = image_proc.crop_headstamp(frame, self.config.image_proc)
            primer = self.config.image_proc
            cropped = image_proc.apply_primer_mask(
                cropped,
                primer.get("primer_mode", "none"),
                int(primer.get("primer_radius", 0)),
            )
            result["cropped"] = cropped
            self.bus.post("test/cropped", cropped)

            self.bus.post("test/status", "Classifying…")
            label, confidence = classifier.classify_active(
                cropped,
                [h["name"] for h in self.config.headstamps if "name" in h],
                self.config.api,
                self.db,
            )
            result["label"] = label
            result["parent"] = self._parent_label(label)
            result["confidence"] = confidence
            result["ok"] = True
            self.bus.post(
                "test/classified",
                {"label": label, "parent": result["parent"], "confidence": confidence},
            )
            return result
        except Exception as exc:
            log.exception("test_once failed")
            return _fail(str(exc) or exc.__class__.__name__)

    def run_once(self, *, flush_prev: int | None = None) -> dict[str, Any]:
        """One iteration of the continuous run: capture, classify, sort.

        IMPORTANT: This does NOT call feed_one(). The continuous loop primes
        a case into the imaging area once at startup via _loop, and each
        sort_and_move(slot) command both drops the current case AND advances
        the next case from the hopper before returning 'done'. Calling
        feed_one() here in addition would queue an extra case per cycle (the
        "double feed" symptom).

        With ``flush_prev`` set this is one END-OF-BRASS FLUSH cycle instead:
        the camera is first checked for an actual case (an empty, in-focus
        pocket still crops to a plausible circle — see image_proc.case_present),
        and the sort is issued as a forced flush cycle that cannot wait on the
        dry feed sensor. ``flush_prev`` is the previous cycle's slot — where
        the case now at the drop port belongs.

        Bus topics emitted during the cycle:
          run/status     (str) human-readable stage label
          run/cropped    (np.ndarray) cropped frame ready to show
          run/classified ({"label","confidence","slot"}) result before the sort step

        Extra result keys beyond the happy path: ``feeder_empty`` (the bare
        sort saw a dry feed gate; ``prev_slot``/``above_floor`` accompany it
        so _loop can hand off to the flush), and ``wheel_empty`` (a flush
        cycle found no case at the camera — the wheel has walked dry).
        """
        result: dict[str, Any] = {
            "ok": False,
            "label": "",
            "parent": None,
            "confidence": 0,
            "slot": None,
            "cropped": None,
            "error": None,
        }
        try:
            # The slot the previous sort command carried — the arm target the
            # firmware's queue holds for the case now at the drop port. Needed
            # only when this cycle turns out to be the end of the brass.
            prev_slot = self._last_classified_slot
            self.bus.post("run/status", "Capturing & cropping…")
            frame = self.camera.capture_frame()
            if frame is None:
                result["error"] = "Camera capture failed"
                return result

            if flush_prev is not None and not image_proc.case_present(frame, self.config.image_proc):
                result["wheel_empty"] = True
                return result

            cropped = image_proc.crop_headstamp(frame, self.config.image_proc)
            primer = self.config.image_proc
            cropped = image_proc.apply_primer_mask(
                cropped,
                primer.get("primer_mode", "none"),
                int(primer.get("primer_radius", 0)),
            )
            result["cropped"] = cropped
            self.bus.post("run/cropped", cropped)

            self.bus.post("run/status", "Classifying…")
            label, confidence = classifier.classify_active(
                cropped,
                [h["name"] for h in self.config.headstamps if "name" in h],
                self.config.api,
                self.db,
            )
            result["label"] = label
            result["parent"] = self._parent_label(label)
            result["confidence"] = confidence

            # Floor → auto-select → package/plain routing (see _resolve_destination).
            slot, above_floor, halt = self._resolve_destination(label, confidence)
            result["slot"] = slot
            result["halt"] = halt
            self._maybe_store_run_image(cropped, label, above_floor)
            # Continuous run: wish-list capture is in play (see _loop).
            self._maybe_capture_feedback(cropped, label, confidence, wish_list=True)
            # Track the latest classification so a follow-up Manual feed or
            # the next continuous-run prime can route the case still queued
            # at position 5 instead of defaulting back to 0.
            self._last_classified_slot = slot
            self.bus.post(
                "run/classified",
                {"label": label, "parent": result["parent"], "confidence": confidence, "slot": slot},
            )
            self._post_history(result)

            if flush_prev is not None:
                self.bus.post("run/status", f"Flushing to slot {slot}…")
                # Forced flush cycle: the arm makes the same move the bare
                # sort would have, but the feed cannot wait on the (dry)
                # proximity gate. A failure here (jam, link loss) must stop
                # the flush before anything drops blind.
                if not self.broker.flush_sort_and_move(flush_prev, slot):
                    result["error"] = self._board_error("Flush failed")
                    return result
            else:
                self.bus.post("run/status", f"Sorting to slot {slot}…")
                # sort_and_move drops the current case at `slot` AND feeds the
                # next case into the imaging area before responding 'done'. The
                # next iteration's capture will see that next case.
                outcome, detail = self.broker.sort_and_move_watched(slot)
                if outcome == serial_broker.SORT_EMPTY:
                    # The feed half of the sort is waiting on a dry proximity
                    # gate — the hopper looks empty, with this case's sort
                    # still pending. _loop owns what happens next.
                    result["feeder_empty"] = True
                    result["prev_slot"] = prev_slot
                    result["above_floor"] = above_floor
                    return result
                if outcome == serial_broker.SORT_ERROR:
                    result["error"] = f"Board error: {detail}" if detail else "Board error"
                    return result
                if outcome != serial_broker.SORT_DONE:
                    result["error"] = self._board_error("Sort timeout")
                    return result
            # Tally the batch only once the case has physically dropped.
            self._commit_package_count(slot, label, above_floor)

            result["ok"] = True
            return result
        except Exception as exc:
            result["error"] = str(exc) or exc.__class__.__name__
            log.exception("run_once failed")
            return result

    # ----- continuous loop ----------------------------------------------------

    def cycle_once(self) -> dict[str, Any]:
        """Manual feed: rotate the wheel, then capture + classify the new case.

        One physical wheel rotation per click. The rotation drops position
        5's case (classified on a previous click) into the slot we stored
        last; then position 3 holds a fresh case to capture. After
        classification we stash the new slot for the next click.

        First click after a fresh connect sends slot 0 (catch-all) since
        nothing has been classified yet. The firmware handles the queue
        delay between camera and drop positions — we just pass whatever
        was last classified.
        """
        result: dict[str, Any] = {
            "ok": False,
            "label": "",
            "parent": None,
            "confidence": 0,
            "slot": None,
            "cropped": None,
            "error": None,
        }
        try:
            slot_to_send = self._last_classified_slot
            self.bus.post("run/status", f"Feeding (slot {slot_to_send})…")
            # force_sort_and_move (xf:<slot>) rotates the wheel and drops
            # position 5's case at the given slot. Handles both the
            # empty-wheel first call and the normal primed state.
            if not self.broker.force_sort_and_move(slot_to_send):
                result["error"] = self._board_error("Feed timeout")
                self.bus.post("run/result", result)
                self.bus.post("run/error", result["error"])
                return result

            self.bus.post("run/status", "Capturing & cropping…")
            frame = self.camera.capture_frame()
            if frame is None:
                result["error"] = "Camera capture failed"
                self.bus.post("run/result", result)
                self.bus.post("run/error", result["error"])
                return result
            cropped = image_proc.crop_headstamp(frame, self.config.image_proc)
            primer = self.config.image_proc
            cropped = image_proc.apply_primer_mask(
                cropped,
                primer.get("primer_mode", "none"),
                int(primer.get("primer_radius", 0)),
            )
            result["cropped"] = cropped
            self.bus.post("run/cropped", cropped)

            self.bus.post("run/status", "Classifying…")
            label, confidence = classifier.classify_active(
                cropped,
                [h["name"] for h in self.config.headstamps if "name" in h],
                self.config.api,
                self.db,
            )
            result["label"] = label
            result["parent"] = self._parent_label(label)
            result["confidence"] = confidence
            slot, above_floor, _halt = self._resolve_destination(label, confidence)
            result["slot"] = slot
            result["ok"] = True
            self._maybe_store_run_image(cropped, label, above_floor)
            # Manual feed isn't a run: confidence-only feedback, no wish list.
            self._maybe_capture_feedback(cropped, label, confidence)
            self.bus.post(
                "run/classified",
                {"label": label, "parent": result["parent"], "confidence": confidence, "slot": slot},
            )
            self._post_history(result)
            # Stash for the next Manual feed click or continuous Run prime.
            self._last_classified_slot = slot
        except Exception as exc:
            log.exception("cycle_once failed")
            result["error"] = str(exc) or exc.__class__.__name__
        self.bus.post("run/result", result)
        if result.get("error"):
            self.bus.post("run/error", result["error"])
        return result

    def _handle_feeder_empty(self, result: dict[str, Any]) -> str:
        """End-of-brass: confirm the hopper is dry, then flush the wheel.

        Entered when a bare sort's feed half answered "waiting for brass"
        repeatedly. First cancel the pending feed — a case may have landed on
        the sensor just before the stop, in which case the sort completed for
        real ("resumed") and the run simply continues. On a clean cancel the
        classified-but-unsorted case is re-issued as a forced flush cycle, and
        the loop keeps photographing, classifying, and flushing whatever each
        forced feed walks into the camera — including the tail cases the run
        never saw — until the camera comes up empty. One final forced feed
        then executes the last queued drop, and the wheel ends empty with
        every case in its true slot.

        Returns "resume" when the run should continue at full speed (the
        end-of-brass call was a false alarm), or "stop" when the run is over
        (out of brass, a jam, or the operator pressed Stop mid-flush). Jams
        during the flush never home-and-refeed blind — remaining stragglers
        stay in the wheel for the operator.
        """
        label = result.get("label", "")
        slot = int(result["slot"])
        prev_slot = int(result["prev_slot"])
        above_floor = bool(result.get("above_floor"))

        self.bus.post("run/status", "Feeder empty — cancelling the pending feed…")
        outcome = self.broker.cancel_pending_feed()
        if outcome == "resumed":
            # Brass landed just before the stop; the waiting feed fired and
            # the sort completed for real. Count it and keep sorting.
            self._commit_package_count(slot, label, above_floor)
            self.bus.post("run/status", "Brass arrived — resuming.")
            return "resume"
        if outcome != "clean":
            self.bus.post("run/error", self._board_error("Board error while cancelling the pending feed"))
            return "stop"

        # The wait died dry. Re-issue the pending sort as the first flush
        # cycle: the arm makes the move the bare sort would have made, so the
        # case at the drop port falls into its true slot.
        self.bus.post("run/status", f"Out of brass — flushing the wheel (slot {slot})…")
        if not self.broker.flush_sort_and_move(prev_slot, slot):
            self.bus.post("run/error", self._board_error("Flush failed"))
            return "stop"
        self._commit_package_count(slot, label, above_floor)
        flushed = 1
        prev_slot = slot

        present_streak = 0
        while True:
            if self._stop_event.is_set():
                self.bus.post("run/status", "Stopped mid-flush — cases may remain in the wheel.")
                return "stop"
            flush_result = self.run_once(flush_prev=prev_slot)
            self.bus.post("run/result", flush_result)
            if flush_result.get("wheel_empty"):
                # Every straggler has been photographed and flushed. One last
                # forced feed executes the final queued drop; the wheel is
                # now empty. Best-effort — the cases are already counted.
                self.broker.feed_one()
                self.bus.post("run/out_of_brass", {"flushed": flushed})
                self.bus.post(
                    "run/status",
                    f"Out of brass — {flushed} in-flight case(s) flushed to their slots.",
                )
                return "stop"
            if flush_result.get("error"):
                self.bus.post("run/error", flush_result["error"])
                return "stop"
            flushed += 1
            prev_slot = int(flush_result["slot"])
            present_streak += 1
            if present_streak >= FLUSH_RESUME_CASES:
                # Case after case keeps arriving: the hopper is still feeding
                # and the dry spell was a delivery gap. Resume at full speed —
                # re-entry is safe because every flush cycle leaves the
                # firmware's queue holding this case's slot, exactly the arm
                # target the drop-port case needs on the next bare sort.
                self.bus.post("run/status", "Brass still flowing — resuming the run.")
                return "resume"

    def _loop(self) -> None:
        try:
            # Prime: rotate the wheel once with the last-known classification
            # slot. If Manual feed (or an earlier run) left a stored slot,
            # we use it so the case still queued at position 5 gets routed
            # correctly instead of being dumped into the catch-all. On a
            # fresh controller this is 0, matching the original xf:0 prime.
            prime_slot = self._last_classified_slot
            self.bus.post("run/status", f"Priming feed (slot {prime_slot})…")
            if not self.broker.force_sort_and_move(prime_slot):
                self.bus.post("run/error", self._board_error("Initial feed timeout"))
                return

            while not self._stop_event.is_set():
                result = self.run_once()
                self.bus.post("run/result", result)
                if result.get("feeder_empty"):
                    if self._handle_feeder_empty(result) == "resume":
                        continue
                    break
                if result.get("error"):
                    self.bus.post("run/error", result["error"])
                    break
                if result.get("halt"):
                    # Every slot for this headstamp is full — stop and notify.
                    self.bus.post(
                        "run/package_halt",
                        {"label": result.get("label", "")},
                    )
                    break
                # Brief gap between cycles to let UI breathe.
                if self._stop_event.wait(timeout=0.05):
                    break
        finally:
            self.bus.post("run/stopped", None)
