"""Run-controller happy/sad paths using the emulator + monkeypatched classify."""

from __future__ import annotations

from unittest.mock import patch

import numpy as np

from sorter.community.feedback import FeedbackService
from sorter.control.events import EventBus
from sorter.control.run_controller import DISCONNECT_ERROR, RunController
from sorter.data.config import Config
from sorter.data.db import Database
from sorter.hardware.serial_emulator import EmulatorBroker


class _FakeCamera:
    def __init__(self) -> None:
        # 480x480 grey frame so cropping is well-behaved.
        self.frame = np.full((480, 480, 3), 128, dtype=np.uint8)

    def capture_frame(self) -> np.ndarray:
        return self.frame


def _feedback(ctrl: RunController) -> FeedbackService:
    """`RunController._feedback` is `FeedbackService | None` (None only when
    built without a db); every controller here is built with one."""
    assert ctrl._feedback is not None
    return ctrl._feedback


def _make_controller(tmp_path) -> tuple[RunController, Config, Database]:
    em = EmulatorBroker(response_delay_s=0.001)
    em.try_open()
    db = Database(tmp_path / "test.db")
    db.ensure_initialized()
    # Activate the auto-seeded model so headstamps have a target.
    from sorter.data.repository import ModelRepo, SettingsRepo

    seed = ModelRepo(db).list()[0]
    SettingsRepo(db).set_active_model_id(seed.id)
    cfg = Config(db).load()
    cfg.api["api_key"] = "fake"
    cfg.save()
    cfg.add_headstamp("WIN", slot=3)
    cfg.add_headstamp("FC", slot=5)
    ctrl = RunController(config=cfg, broker=em, camera=_FakeCamera(), bus=EventBus(), db=db)
    return ctrl, cfg, db


def test_run_once_routes_known_label_to_its_slot(tmp_path) -> None:
    ctrl, _, _ = _make_controller(tmp_path)
    with patch("sorter.ml.classifier.classify_active", return_value=("WIN", 100)):
        result = ctrl.run_once()
    assert result["ok"] is True
    assert result["label"] == "WIN"
    assert result["slot"] == 3


def test_confidence_floor_routes_below_to_catch_all(tmp_path) -> None:
    ctrl, cfg, _ = _make_controller(tmp_path)
    cfg.set_run_confidence_floor(80)
    # WIN is mapped to slot 3, but 50% < 80% floor -> catch-all.
    with patch("sorter.ml.classifier.classify_active", return_value=("WIN", 50)):
        result = ctrl.run_once()
    assert result["slot"] == 0
    # At/above the floor it routes normally.
    with patch("sorter.ml.classifier.classify_active", return_value=("WIN", 80)):
        result = ctrl.run_once()
    assert result["slot"] == 3


def test_confidence_floor_zero_disables_floor(tmp_path) -> None:
    ctrl, cfg, _ = _make_controller(tmp_path)
    cfg.set_run_confidence_floor(0)
    with patch("sorter.ml.classifier.classify_active", return_value=("WIN", 1)):
        result = ctrl.run_once()
    assert result["slot"] == 3  # no floor -> routes by label even at 1%


def _run_image_files(model_id):
    from sorter import paths

    d = paths.model_run_images_dir(model_id)
    return sorted(d.glob("*.jpg")) if d.exists() else []


def test_store_images_above_floor(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    ctrl, cfg, db = _make_controller(tmp_path)
    from sorter.data.repository import SettingsRepo

    mid = SettingsRepo(db).get_active_model_id()
    cfg.set_run_confidence_floor(50)
    cfg.set_run_store_images("above")

    with patch("sorter.ml.classifier.classify_active", return_value=("WIN", 90)):
        ctrl.run_once()
    files = _run_image_files(mid)
    assert len(files) == 1 and files[0].name.startswith("WIN__")

    # A below-floor case is NOT stored in "above" mode.
    with patch("sorter.ml.classifier.classify_active", return_value=("WIN", 10)):
        ctrl.run_once()
    assert len(_run_image_files(mid)) == 1


def test_store_images_below_floor(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    ctrl, cfg, db = _make_controller(tmp_path)
    from sorter.data.repository import SettingsRepo

    mid = SettingsRepo(db).get_active_model_id()
    cfg.set_run_confidence_floor(50)
    cfg.set_run_store_images("below")

    with patch("sorter.ml.classifier.classify_active", return_value=("WIN", 90)):
        ctrl.run_once()
    assert _run_image_files(mid) == []  # above floor -> not stored
    with patch("sorter.ml.classifier.classify_active", return_value=("WIN", 10)):
        ctrl.run_once()
    assert len(_run_image_files(mid)) == 1  # below floor -> stored


def test_store_images_none_and_all(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    ctrl, cfg, db = _make_controller(tmp_path)
    from sorter.data.repository import SettingsRepo

    mid = SettingsRepo(db).get_active_model_id()

    cfg.set_run_store_images("none")
    with patch("sorter.ml.classifier.classify_active", return_value=("WIN", 90)):
        ctrl.run_once()
    assert _run_image_files(mid) == []

    cfg.set_run_store_images("all")
    with patch("sorter.ml.classifier.classify_active", return_value=("FC", 90)):
        ctrl.run_once()
    with patch("sorter.ml.classifier.classify_active", return_value=("WIN", 5)):
        ctrl.run_once()
    assert len(_run_image_files(mid)) == 2  # every case stored


def test_run_once_routes_unknown_label_to_slot_zero(tmp_path) -> None:
    ctrl, _, _ = _make_controller(tmp_path)
    with patch("sorter.ml.classifier.classify_active", return_value=("Mystery", 100)):
        result = ctrl.run_once()
    assert result["ok"] is True
    assert result["slot"] == 0


def test_test_once_skips_sort_step(tmp_path) -> None:
    ctrl, _, _ = _make_controller(tmp_path)
    sort_calls: list[int] = []
    ctrl.broker.sort_and_move = lambda slot: sort_calls.append(slot) or True  # type: ignore[assignment]
    with patch("sorter.ml.classifier.classify_active", return_value=("WIN", 100)):
        result = ctrl.test_once()
    assert result["ok"] is True
    assert result["label"] == "WIN"
    assert result["confidence"] == 100
    assert result["cropped"] is not None
    assert "slot" not in result
    assert sort_calls == []


def _link_win_to_brass(db) -> int:
    """Create a Brass parent, link WIN to it, return the parent id."""
    from sorter.data.repository import HeadstampParentRepo, HeadstampRepo, SettingsRepo

    mid = SettingsRepo(db).get_active_model_id()
    assert mid is not None
    brass = HeadstampParentRepo(db).add(mid, "Brass")
    win = next(h for h in HeadstampRepo(db).list_for_model(mid) if h.name == "WIN")
    HeadstampRepo(db).set_parent(win.id, brass.id)
    return brass.id


def test_run_once_returns_parent_label_in_parent_mode(tmp_path) -> None:
    ctrl, cfg, db = _make_controller(tmp_path)
    brass_id = _link_win_to_brass(db)
    cfg.set_parent_slot(brass_id, 2)
    cfg.set_use_parent_classifications(True)

    events: list[dict] = []
    ctrl.bus.subscribe("run/classified", events.append)
    with patch("sorter.ml.classifier.classify_active", return_value=("WIN", 95)):
        result = ctrl.run_once()
    ctrl.bus.drain()  # bus is queued; the UI's drain timer normally pumps it

    # Both labels + confidence are returned, and routing follows the parent.
    assert result["label"] == "WIN"
    assert result["parent"] == "Brass"
    assert result["confidence"] == 95
    assert result["slot"] == 2
    assert events and events[0]["parent"] == "Brass"


def test_parent_label_omitted_when_mode_disabled(tmp_path) -> None:
    ctrl, cfg, db = _make_controller(tmp_path)
    _link_win_to_brass(db)  # link exists, but the runtime toggle is off

    with patch("sorter.ml.classifier.classify_active", return_value=("WIN", 95)):
        result = ctrl.run_once()

    assert result["label"] == "WIN"
    assert result["parent"] is None
    assert result["slot"] == 3  # WIN's own child-mode slot, unchanged


def _enable_feedback(db, *, floor=95, mode="Instant") -> int:
    """Turn the active seeded model into a feedback-enabled community model."""
    from sorter.data.repository import ModelRepo, SettingsRepo

    mid = SettingsRepo(db).get_active_model_id()
    assert mid is not None
    m = ModelRepo(db).get(mid)
    assert m is not None
    m.community_model_uid = "uid-1"
    m.feedback_loop_enabled = True
    m.feedback_loop_confidence_floor = floor
    m.feedback_loop_upload_mode = mode
    ModelRepo(db).update(m)
    return mid


def test_feedback_capture_below_floor_enqueues_and_posts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    ctrl, _, db = _make_controller(tmp_path)
    mid = _enable_feedback(db, floor=95, mode="Instant")
    from sorter.community.feedback import FeedbackService

    events: list[dict] = []
    ctrl.bus.subscribe("feedback/queued", events.append)
    # 90 < 95 floor → captured for the feedback loop.
    with patch("sorter.ml.classifier.classify_active", return_value=("WIN", 90)):
        ctrl.run_once()
    ctrl.bus.drain()
    assert FeedbackService(db).count_pending(mid) == 1
    assert events and events[0]["model_id"] == mid
    assert events[0]["upload_mode"] == "Instant"


def test_feedback_no_capture_above_floor(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    ctrl, _, db = _make_controller(tmp_path)
    mid = _enable_feedback(db, floor=95)
    from sorter.community.feedback import FeedbackService

    with patch("sorter.ml.classifier.classify_active", return_value=("WIN", 99)):
        ctrl.run_once()
    assert FeedbackService(db).count_pending(mid) == 0


def test_feedback_no_capture_for_non_community_model(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    ctrl, _, db = _make_controller(tmp_path)  # seeded model is not community
    from sorter.community.feedback import FeedbackService
    from sorter.data.repository import SettingsRepo

    mid = SettingsRepo(db).get_active_model_id()
    assert mid is not None
    with patch("sorter.ml.classifier.classify_active", return_value=("WIN", 5)):
        ctrl.run_once()
    assert FeedbackService(db).count_pending(mid) == 0


def test_test_once_returns_parent_label_in_parent_mode(tmp_path) -> None:
    ctrl, cfg, db = _make_controller(tmp_path)
    _link_win_to_brass(db)
    cfg.set_use_parent_classifications(True)

    events: list[dict] = []
    ctrl.bus.subscribe("test/classified", events.append)
    with patch("sorter.ml.classifier.classify_active", return_value=("WIN", 88)):
        result = ctrl.test_once()
    ctrl.bus.drain()

    assert result["label"] == "WIN"
    assert result["parent"] == "Brass"
    assert events and events[0]["parent"] == "Brass"


# ----- wish list (model-balancing feedback) ----------------------------------


def test_wish_list_capture_during_a_run(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    ctrl, _, db = _make_controller(tmp_path)
    mid = _enable_feedback(db, floor=95, mode="Instant")
    from sorter.community.feedback import FeedbackService

    _feedback(ctrl).set_wish_list(mid, ["WIN"])
    events: list[dict] = []
    ctrl.bus.subscribe("feedback/queued", events.append)
    # 99 is well above the 95 floor, but WIN is wanted → captured anyway.
    with patch("sorter.ml.classifier.classify_active", return_value=("WIN", 99)):
        ctrl.run_once()
    ctrl.bus.drain()
    assert FeedbackService(db).count_pending(mid) == 1
    assert events and events[0]["model_id"] == mid


def test_wish_list_not_applied_to_manual_feed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    ctrl, _, db = _make_controller(tmp_path)
    mid = _enable_feedback(db, floor=95)
    from sorter.community.feedback import FeedbackService

    _feedback(ctrl).set_wish_list(mid, ["WIN"])
    with patch("sorter.ml.classifier.classify_active", return_value=("WIN", 99)):
        ctrl.cycle_once()
    assert FeedbackService(db).count_pending(mid) == 0


def test_wish_list_off_leaves_confidence_only_behaviour(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    ctrl, _, db = _make_controller(tmp_path)
    mid = _enable_feedback(db, floor=95)
    from sorter.community.feedback import FeedbackService

    with patch("sorter.ml.classifier.classify_active", return_value=("WIN", 99)):
        ctrl.run_once()
    assert FeedbackService(db).count_pending(mid) == 0


def test_refresh_and_clear_wish_list(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CASESORTER_DATA_DIR", str(tmp_path / "data"))
    ctrl, _, db = _make_controller(tmp_path)
    _enable_feedback(db)

    class _Api:
        def __init__(self, auth=None, **kw) -> None:
            pass

        def fetch_wish_list(self, uid):
            return ["FC"]

    class _Auth:
        def acquire_token_silent(self, scopes=None):
            return object()

    import sorter.community.community_api as ca

    monkeypatch.setattr(ca, "CommunityApi", _Api)
    assert ctrl.refresh_wish_list(auth=_Auth()) == ["FC"]
    assert _feedback(ctrl).wish_list() == ["fc"]
    ctrl.clear_wish_list()
    assert _feedback(ctrl).wish_list() == []


# ----- a dropped link is not a timeout (issue #35) ----------------------------


def test_run_once_names_the_disconnect_instead_of_a_sort_timeout(tmp_path) -> None:
    ctrl, cfg, _ = _make_controller(tmp_path)
    cfg.set_run_confidence_floor(0)
    ctrl.broker.simulate_disconnect("cable pulled")

    with patch("sorter.ml.classifier.classify_active", return_value=("WIN", 90)):
        result = ctrl.run_once()

    assert result["ok"] is False
    assert result["error"] == DISCONNECT_ERROR


def test_cycle_once_names_the_disconnect_instead_of_a_feed_timeout(tmp_path) -> None:
    ctrl, _, _ = _make_controller(tmp_path)
    ctrl.broker.simulate_disconnect()

    result = ctrl.cycle_once()

    assert result["error"] == DISCONNECT_ERROR


def test_a_live_board_still_reports_a_timeout_as_a_timeout(tmp_path) -> None:
    # The distinction only means something if the timeout half survives: a
    # board that is connected but silent must not be blamed on the cable.
    from sorter.hardware.serial_broker import SORT_FAILED

    ctrl, cfg, _ = _make_controller(tmp_path)
    cfg.set_run_confidence_floor(0)
    with (
        patch("sorter.ml.classifier.classify_active", return_value=("WIN", 90)),
        patch.object(ctrl.broker, "sort_and_move_watched", return_value=(SORT_FAILED, "")),
    ):
        result = ctrl.run_once()

    assert result["error"] == "Sort timeout"


# ----- end-of-brass flush -----------------------------------------------------


def test_run_once_reports_a_dry_feeder_instead_of_a_timeout(tmp_path) -> None:
    ctrl, _, _ = _make_controller(tmp_path)
    ctrl.broker.set_hopper(0)
    try:
        with patch("sorter.ml.classifier.classify_active", return_value=("WIN", 100)):
            result = ctrl.run_once()
    finally:
        ctrl.broker.stop()  # kill the emulator's waiting chatter

    assert result["ok"] is False
    assert result["error"] is None, "a dry feeder is a state, not an error"
    assert result["feeder_empty"] is True
    assert result["slot"] == 3  # the classified-but-unsorted case's slot
    assert result["prev_slot"] == 0  # nothing sorted before it


def test_feeder_empty_flushes_the_wheel_and_ends_the_run(tmp_path, monkeypatch) -> None:
    from sorter.hardware import serial_broker

    monkeypatch.setattr(serial_broker, "CANCEL_LISTEN_S", 0.05)
    ctrl, _, _ = _make_controller(tmp_path)
    ctrl.broker.set_hopper(0)
    sent: list[str] = []
    ctrl.broker.on_sent.append(sent.append)
    ended: list[dict] = []
    ctrl.bus.subscribe("run/out_of_brass", ended.append)

    with patch("sorter.ml.classifier.classify_active", return_value=("WIN", 100)):
        result = ctrl.run_once()
        assert result.get("feeder_empty") is True
        # One straggler still in the wheel, then the camera comes up empty.
        with patch(
            "sorter.hardware.image_proc.case_present",
            side_effect=[True, False],
        ):
            action = ctrl._handle_feeder_empty(result)
    ctrl.bus.drain()

    assert action == "stop"
    assert ended and ended[0]["flushed"] == 2  # the pending case + the straggler
    # The pending WIN sort was re-issued as a flush (arm to the previous sort's
    # slot 0, forced feed queuing WIN's slot 3), the straggler flushed after
    # it, and the close-out feed executed the final drop.
    assert "stop" in sent
    flush_cmds = [c for c in sent if c.startswith(("sortto:", "xf:"))]
    assert flush_cmds[:2] == ["sortto:0", "xf:3"]
    assert flush_cmds[2] == "sortto:3"  # the straggler's flush parks on WIN's slot
    assert flush_cmds[-1] == "xf:0"  # final feed: execute the last queued drop


def test_flush_resumes_when_brass_keeps_flowing(tmp_path, monkeypatch) -> None:
    # Case after case arriving during the flush means the dry spell was a
    # delivery gap, not the end of the hopper — the run must resume at full
    # speed instead of walking the whole bowl at flush pace.
    from sorter.control import run_controller as rc
    from sorter.hardware import serial_broker

    monkeypatch.setattr(serial_broker, "CANCEL_LISTEN_S", 0.05)
    ctrl, _, _ = _make_controller(tmp_path)
    ctrl.broker.set_hopper(0)

    with patch("sorter.ml.classifier.classify_active", return_value=("WIN", 100)):
        result = ctrl.run_once()
        assert result.get("feeder_empty") is True
        with patch("sorter.hardware.image_proc.case_present", return_value=True):
            action = ctrl._handle_feeder_empty(result)

    assert action == "resume"
    # The false alarm cost exactly the resume-streak worth of flush cycles.
    assert rc.FLUSH_RESUME_CASES >= 2


def test_stop_during_flush_aborts_without_blind_feeding(tmp_path, monkeypatch) -> None:
    from sorter.hardware import serial_broker

    monkeypatch.setattr(serial_broker, "CANCEL_LISTEN_S", 0.05)
    ctrl, _, _ = _make_controller(tmp_path)
    ctrl.broker.set_hopper(0)

    with patch("sorter.ml.classifier.classify_active", return_value=("WIN", 100)):
        result = ctrl.run_once()
        assert result.get("feeder_empty") is True
        ctrl._stop_event.set()  # operator presses Stop before the flush loop
        with patch("sorter.hardware.image_proc.case_present", return_value=True):
            action = ctrl._handle_feeder_empty(result)

    assert action == "stop"
