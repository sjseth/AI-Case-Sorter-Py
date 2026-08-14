"""Settings -> Camera and Settings -> Image Proc pages.

Offscreen, against the real SQLite-backed ``Config`` from the shared UI
conftest. No hardware: ``list_cameras_with_metadata``/``camera_names`` and the
``Camera``/image_proc pipeline are monkeypatched at the module level so
nothing here opens a real device or does real HoughCircles work.
"""

from __future__ import annotations

import types

import numpy as np
import pytest

pytest.importorskip("PySide6")

from sorter.data.config import Config
from sorter.ui import settings_camera, settings_imageproc

from .conftest import drain_until

CAMERA_METADATA = [
    {"index": 0, "name": "USB Webcam", "resolutions": [(640, 480), (1280, 720), (1920, 1080)]},
    {"index": 1, "name": "Camera 1", "resolutions": [(320, 240), (640, 480)]},
]


class FakeCamera:
    """Stand-in for ``sorter.hardware.camera.Camera`` — records constructor args."""

    instances: list[FakeCamera] = []
    start_preview_result = True

    def __init__(self, device_index: int, width: int, height: int) -> None:
        self.device_index = device_index
        self.width = width
        self.height = height
        self.stopped = False
        FakeCamera.instances.append(self)

    def start_preview(self) -> bool:
        return FakeCamera.start_preview_result

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture(autouse=True)
def _reset_fake_camera():
    FakeCamera.instances = []
    FakeCamera.start_preview_result = True
    yield


class FakeBroker:
    def __init__(self, *, connected: bool = True) -> None:
        self.is_connected = connected
        self.sent: list[str] = []

    def send_command(self, command: str) -> None:
        self.sent.append(command)


def _stub_camera_frame(window, frame: np.ndarray | None) -> None:
    window.camera = types.SimpleNamespace(
        capture_frame=lambda: frame,
        latest_frame=lambda: None,
        stop=lambda: None,
    )


# ----- Camera page -------------------------------------------------------------


def test_quick_population_uses_name_only_enumeration(window, monkeypatch) -> None:
    monkeypatch.setattr(settings_camera, "camera_names", lambda: {0: "USB Webcam"})

    section = settings_camera.build_camera_section(window)

    assert section.device_combo.count() == 1
    assert "USB Webcam" in section.device_combo.currentText()
    assert "Click Detect" in section.status_label.text()


def test_quick_population_falls_back_when_no_names_available(window, monkeypatch) -> None:
    monkeypatch.setattr(settings_camera, "camera_names", lambda: {})

    section = settings_camera.build_camera_section(window)

    assert section.device_combo.count() == 1
    # Name only — the list position was never the device index, so it is not
    # shown; the real index rides in the item's data.
    assert section.device_combo.currentText() == "Camera 0"
    assert section.device_combo.currentData()["index"] == 0  # default device_index


def test_detect_populates_devices_and_resolutions(window, monkeypatch) -> None:
    monkeypatch.setattr(settings_camera, "camera_names", lambda: {})
    monkeypatch.setattr(settings_camera, "list_cameras_with_metadata", lambda: CAMERA_METADATA)
    section = settings_camera.build_camera_section(window)

    section.detect_devices()

    assert drain_until(window, lambda: section.device_combo.count() == len(CAMERA_METADATA))
    assert "Detected 2 camera(s)" in section.status_label.text()
    # Default config (device_index=0, 640x480) is supported by device 0, so the
    # saved resolution wins over the largest.
    assert section.resolution_combo.currentText() == "640 x 480"


def test_switching_device_falls_back_to_the_largest_resolution(window, monkeypatch) -> None:
    monkeypatch.setattr(settings_camera, "camera_names", lambda: {})
    monkeypatch.setattr(settings_camera, "list_cameras_with_metadata", lambda: CAMERA_METADATA)
    section = settings_camera.build_camera_section(window)
    section.detect_devices()
    assert drain_until(window, lambda: section.device_combo.count() == len(CAMERA_METADATA))

    section.device_combo.setCurrentIndex(1)  # device 1: saved 640x480 doesn't match its index

    assert section.resolution_combo.currentText() == "640 x 480"  # largest of [320x240, 640x480]


def test_detect_failure_reports_status(window, monkeypatch) -> None:
    monkeypatch.setattr(settings_camera, "camera_names", lambda: {})

    def _boom():
        raise RuntimeError("no /dev/video*")

    monkeypatch.setattr(settings_camera, "list_cameras_with_metadata", _boom)
    section = settings_camera.build_camera_section(window)

    section.detect_devices()

    assert drain_until(window, lambda: "Detect failed" in section.status_label.text())


def test_apply_swaps_the_camera_and_persists_config(window, config, monkeypatch) -> None:
    monkeypatch.setattr(settings_camera, "camera_names", lambda: {})
    monkeypatch.setattr(settings_camera, "list_cameras_with_metadata", lambda: CAMERA_METADATA)
    monkeypatch.setattr(settings_camera, "Camera", FakeCamera)
    window.camera.stop()  # release the real preflight camera before swapping in the stub
    old_camera = types.SimpleNamespace(stopped=False, device_index=0, width=640, height=480)
    old_camera.stop = lambda: setattr(old_camera, "stopped", True)
    window.camera = old_camera
    controller = types.SimpleNamespace(camera=None)
    window.run_controller = controller
    section = settings_camera.build_camera_section(window)
    section.detect_devices()
    assert drain_until(window, lambda: section.device_combo.count() == len(CAMERA_METADATA))
    section.device_combo.setCurrentIndex(1)
    section.resolution_combo.setCurrentIndex(0)  # 320 x 240

    section.apply_camera()

    assert old_camera.stopped is True
    assert isinstance(window.camera, FakeCamera)
    assert (window.camera.device_index, window.camera.width, window.camera.height) == (1, 320, 240)
    assert controller.camera is window.camera  # mirrors ui.app.MainWindow.restart_camera
    assert window.config.camera["device_index"] == 1
    assert window.config.camera["width"] == 320
    assert window.config.camera["height"] == 240
    assert window.config.camera["device_chosen"] is True
    # Actually persisted, not just held in the in-memory section.
    reloaded = Config(config.db).load()
    assert reloaded.camera["device_index"] == 1
    assert reloaded.camera["width"] == 320
    assert "connected" in window._camera_state[0]
    assert window._camera_state[1] is True


def test_apply_failure_still_swaps_but_reports_disconnected(window, monkeypatch) -> None:
    monkeypatch.setattr(settings_camera, "camera_names", lambda: {})
    monkeypatch.setattr(settings_camera, "Camera", FakeCamera)
    FakeCamera.start_preview_result = False
    section = settings_camera.build_camera_section(window)

    section.apply_camera()

    assert isinstance(window.camera, FakeCamera)
    assert window._camera_state[1] is False
    assert "failed" in window._camera_state[0].lower()


def test_apply_with_no_selection_touches_nothing(window, monkeypatch) -> None:
    monkeypatch.setattr(settings_camera, "camera_names", lambda: {})
    section = settings_camera.build_camera_section(window)
    section.device_combo.clear()
    original_camera = window.camera

    section.apply_camera()

    assert window.camera is original_camera


# ----- Image Proc page ----------------------------------------------------------


def test_widgets_reflect_config_defaults(window) -> None:
    section = settings_imageproc.build_imageproc_section(window)

    assert section.dp_spin.value() == pytest.approx(2.0)
    assert section.min_dist_spin.value() == 500
    assert section.param1_spin.value() == 100
    assert section.param2_spin.value() == 60
    assert section.min_radius_spin.value() == 150
    assert section.max_radius_spin.value() == 250
    assert section.primer_mode_combo.currentData() == "hide"
    assert section.primer_radius_spin.value() == 135
    assert section.led_slider.value() == 130


def test_changing_a_hough_field_persists_the_key(window, config) -> None:
    section = settings_imageproc.build_imageproc_section(window)

    section.min_dist_spin.setValue(777)

    assert window.config.image_proc["hough"]["min_dist"] == 777
    assert Config(config.db).load().image_proc["hough"]["min_dist"] == 777


def test_changing_primer_mode_persists_the_key(window, config) -> None:
    section = settings_imageproc.build_imageproc_section(window)

    use_index = next(
        i for i in range(section.primer_mode_combo.count()) if section.primer_mode_combo.itemData(i) == "use"
    )
    section.primer_mode_combo.setCurrentIndex(use_index)

    assert window.config.image_proc["primer_mode"] == "use"
    assert Config(config.db).load().image_proc["primer_mode"] == "use"


def test_changing_primer_radius_persists_the_key(window, config) -> None:
    section = settings_imageproc.build_imageproc_section(window)

    section.primer_radius_spin.setValue(200)

    assert window.config.image_proc["primer_radius"] == 200
    assert Config(config.db).load().image_proc["primer_radius"] == 200


def test_capture_runs_the_pipeline_against_the_new_frame(window, monkeypatch) -> None:
    calls = {"detect": 0, "overlay": 0, "crop": 0, "mask": 0}
    frame = np.zeros((480, 640, 3), np.uint8)

    def fake_detect(_frame, _params):
        calls["detect"] += 1
        return (10.0, 20.0, 30.0)

    def fake_overlay(frame_arg, _detection):
        calls["overlay"] += 1
        return frame_arg

    def fake_crop(_frame, _cfg):
        calls["crop"] += 1
        return np.zeros((480, 480, 3), np.uint8)

    def fake_mask(img, _mode, _radius):
        calls["mask"] += 1
        return img

    monkeypatch.setattr(settings_imageproc, "hough_detect", fake_detect)
    monkeypatch.setattr(settings_imageproc, "overlay_detection", fake_overlay)
    monkeypatch.setattr(settings_imageproc, "crop_headstamp", fake_crop)
    monkeypatch.setattr(settings_imageproc, "apply_primer_mask", fake_mask)
    _stub_camera_frame(window, frame)
    section = settings_imageproc.build_imageproc_section(window)

    section.capture()

    assert drain_until(window, lambda: calls["detect"] == 1)
    assert calls == {"detect": 1, "overlay": 1, "crop": 1, "mask": 1}
    assert not section.raw_preview_label.pixmap().isNull()
    assert not section.processed_preview_label.pixmap().isNull()
    assert "Detected circle" in window.statusBar().currentMessage()


def test_capture_with_no_frame_reports_status(window, monkeypatch) -> None:
    monkeypatch.setattr(settings_imageproc, "hough_detect", lambda *a, **k: None)
    _stub_camera_frame(window, None)
    section = settings_imageproc.build_imageproc_section(window)

    section.capture()

    assert drain_until(window, lambda: "No camera frame available" in window.statusBar().currentMessage())


def test_param_change_reprocesses_the_cached_frame_without_recapturing(window, monkeypatch) -> None:
    calls = {"detect": 0}
    frame = np.zeros((480, 640, 3), np.uint8)
    capture_calls = {"n": 0}

    def _capture_frame():
        capture_calls["n"] += 1
        return frame

    def fake_detect(_frame, _params):
        calls["detect"] += 1
        return None

    monkeypatch.setattr(settings_imageproc, "hough_detect", fake_detect)
    monkeypatch.setattr(settings_imageproc, "overlay_detection", lambda f, _d: f)
    monkeypatch.setattr(settings_imageproc, "crop_headstamp", lambda f, _c: f)
    monkeypatch.setattr(settings_imageproc, "apply_primer_mask", lambda img, _m, _r: img)
    window.camera = types.SimpleNamespace(capture_frame=_capture_frame, latest_frame=lambda: None, stop=lambda: None)
    section = settings_imageproc.build_imageproc_section(window)
    section.capture()
    assert drain_until(window, lambda: calls["detect"] == 1)
    assert capture_calls["n"] == 1

    section.min_dist_spin.setValue(999)  # in-process signal, no worker/drain needed

    assert calls["detect"] == 2
    assert capture_calls["n"] == 1  # reprocessed the cached frame, no new capture


def test_reprocess_is_a_noop_before_any_capture(window, monkeypatch) -> None:
    calls = {"detect": 0}
    monkeypatch.setattr(
        settings_imageproc, "hough_detect", lambda *a, **k: (calls.__setitem__("detect", calls["detect"] + 1), None)[1]
    )
    section = settings_imageproc.build_imageproc_section(window)

    section.min_dist_spin.setValue(321)

    assert calls["detect"] == 0
    assert section.raw_preview_label.text() == "No frame"


# ----- LED brightness ------------------------------------------------------------


def test_led_slider_updates_label_immediately_but_defers_the_send(window) -> None:
    window.broker = FakeBroker()
    section = settings_imageproc.build_imageproc_section(window)

    section._on_led_changed(200)

    assert section.led_value_label.text() == "200"
    assert window.broker.sent == []  # debounce hasn't fired yet


def test_led_debounce_persists_and_sends_when_connected(window, config) -> None:
    window.broker = FakeBroker(connected=True)
    section = settings_imageproc.build_imageproc_section(window)

    section._on_led_changed(200)
    section._apply_led()  # simulate the debounce timer elapsing

    assert window.broker.sent == ["cameraledlevel:200"]
    assert window.config.serial["init_settings"]["cameraledlevel"] == 200
    assert Config(config.db).load().serial["init_settings"]["cameraledlevel"] == 200


def test_led_debounce_without_a_broker_still_persists(window) -> None:
    assert window.broker is None
    section = settings_imageproc.build_imageproc_section(window)

    section._on_led_changed(50)
    section._apply_led()

    assert window.config.serial["init_settings"]["cameraledlevel"] == 50
    assert "not connected" in window.statusBar().currentMessage()


def test_led_debounce_skips_a_repeat_of_the_same_value(window) -> None:
    window.broker = FakeBroker()
    section = settings_imageproc.build_imageproc_section(window)
    section._on_led_changed(200)
    section._apply_led()  # first send goes through

    section._on_led_changed(200)  # slider settles back on the same value
    section._apply_led()

    assert window.broker.sent == ["cameraledlevel:200"]


# ----- Image Proc page: model-scoped crop + primer settings ---------------------


def _make_model(config, name, **fields):
    """A second model on the seeded cartridge, with image-processing overrides."""
    from sorter.data.models import Model
    from sorter.data.repository import CartridgeRepo, ModelRepo

    cartridge = CartridgeRepo(config.db).list()[0]
    return ModelRepo(config.db).create(Model(name=name, cartridge_id=cartridge.id, **fields))


def _activate(window, model_id):
    from sorter.data.repository import SettingsRepo

    SettingsRepo(window.config.db).set_active_model_id(model_id)
    window.bus.post("mode/changed", {"active_model_id": model_id})
    window.bus.drain()


def test_switching_models_reloads_the_page(window, config) -> None:
    from sorter.data.models import ImageProcessingConfig

    section = settings_imageproc.build_imageproc_section(window)
    model_a = _make_model(
        config,
        "A",
        use_primer_mask=True,
        hide_primer=False,
        primer_mask_size=90,
        image_processing=ImageProcessingConfig(hough={**ImageProcessingConfig().hough, "min_radius": 111}),
    )
    model_b = _make_model(
        config,
        "B",
        use_primer_mask=False,
        hide_primer=False,
        primer_mask_size=210,
        image_processing=ImageProcessingConfig(hough={**ImageProcessingConfig().hough, "min_radius": 222}),
    )

    _activate(window, model_a.id)
    assert (section.primer_mode_combo.currentData(), section.primer_radius_spin.value()) == ("use", 90)
    assert section.min_radius_spin.value() == 111

    _activate(window, model_b.id)
    assert (section.primer_mode_combo.currentData(), section.primer_radius_spin.value()) == ("none", 210)
    assert section.min_radius_spin.value() == 222
    # The live config is what run_controller crops with, so it moved too.
    assert window.config.image_proc["primer_radius"] == 210
    assert window.config.image_proc["hough"]["min_radius"] == 222


def test_edits_land_on_the_active_model_not_on_the_next_one(window, config) -> None:
    from sorter.data.repository import ModelRepo

    section = settings_imageproc.build_imageproc_section(window)
    model_a = _make_model(config, "A")
    model_b = _make_model(config, "B", use_primer_mask=False, hide_primer=False, primer_mask_size=210)

    _activate(window, model_a.id)
    section.primer_radius_spin.setValue(160)
    section.min_radius_spin.setValue(180)

    stored_a = ModelRepo(config.db).get(model_a.id)
    assert stored_a is not None
    assert (stored_a.primer_mask_size, stored_a.hide_primer, stored_a.use_primer_mask) == (160, True, False)
    assert stored_a.image_processing.hough["min_radius"] == 180
    assert stored_a.image_processing.primer_radius == 160

    # B never inherits A's edit — Seth's report, from the write side.
    stored_b = ModelRepo(config.db).get(model_b.id)
    assert stored_b is not None
    assert stored_b.primer_mask_size == 210
    _activate(window, model_b.id)
    assert section.primer_radius_spin.value() == 210
    _activate(window, model_a.id)
    assert section.primer_radius_spin.value() == 160


def test_a_model_that_was_never_tuned_here_inherits_the_global(window, config) -> None:
    # Existing installs tuned the global before this page was model-aware;
    # activating a model must not reset that to the dataclass defaults.
    window.config.image_proc["primer_radius"] = 175
    window.config.image_proc["primer_mode"] = "none"
    window.config.image_proc["hough"]["min_radius"] = 199
    window.config.save()
    section = settings_imageproc.build_imageproc_section(window)

    _activate(window, _make_model(config, "Untuned").id)

    assert section.primer_radius_spin.value() == 175
    assert section.primer_mode_combo.currentData() == "none"
    assert section.min_radius_spin.value() == 199
    assert window.config.image_proc["primer_radius"] == 175


def test_ai_config_mode_keeps_the_settings_global(window, config) -> None:
    from sorter.data.repository import ModelRepo, SettingsRepo

    section = settings_imageproc.build_imageproc_section(window)
    model = _make_model(config, "Parked")
    SettingsRepo(config.db).clear_active_model()
    window.bus.post("mode/changed", {"active_model_id": None})
    window.bus.drain()

    section.primer_radius_spin.setValue(144)

    assert Config(config.db).load().image_proc["primer_radius"] == 144
    stored = ModelRepo(config.db).get(model.id)
    assert stored is not None
    assert stored.primer_mask_size == 135


def test_reopening_the_page_picks_up_a_model_editor_change(window, config) -> None:
    # The model editor writes the same primer fields and posts nothing, so the
    # page re-reads on show rather than trusting what it loaded last.
    from sorter.data.repository import ModelRepo

    section = settings_imageproc.build_imageproc_section(window)
    model = _make_model(config, "Edited", use_primer_mask=False, hide_primer=True, primer_mask_size=120)
    _activate(window, model.id)
    assert section.primer_radius_spin.value() == 120

    model.primer_mask_size = 175
    with config.db.transaction():
        ModelRepo(config.db).update(model)
    section.show()

    assert section.primer_radius_spin.value() == 175
    section.close()


def test_the_device_list_names_devices_and_the_status_line_carries_the_index(window, monkeypatch) -> None:
    # The "(N)" prefix was the list position while the line below read the real
    # device index — two different numbers for the same camera (JL).
    monkeypatch.setattr(settings_camera, "camera_names", lambda: {4: "Integrated RGB Camera"})
    window.camera = types.SimpleNamespace(device_index=4, width=1280, height=720, latest_frame=lambda: None)

    section = settings_camera.build_camera_section(window)

    assert section.device_combo.currentText() == "Integrated RGB Camera"
    assert section.device_combo.currentData()["index"] == 4
    assert section.current_label.text() == "Current: device 4 @ 1280x720"


def test_the_camera_page_says_what_to_do_about_a_dead_feed(window) -> None:
    section = settings_camera.build_camera_section(window)
    window.camera = types.SimpleNamespace(latest_frame=lambda: None)
    section._win = window
    section.show()

    section._refresh_preview()

    assert section.preview_label.text() == settings_camera.NO_FEED_TEXT


def test_camera_page_shows_a_live_preview(window) -> None:
    # Apply is blind without a feed (JL) — the page renders the live camera.
    section = settings_camera.build_camera_section(window)
    window.camera = types.SimpleNamespace(latest_frame=lambda: np.zeros((480, 640, 3), np.uint8))
    section._win = window
    section.show()

    section._refresh_preview()

    assert section.preview_label.pixmap() is not None
    # Same layout-feedback guard as the Sort preview: never grows the page.
    from PySide6.QtWidgets import QSizePolicy

    assert section.preview_label.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Ignored
    section.close()
