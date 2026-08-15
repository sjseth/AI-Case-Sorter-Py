"""Unit tests for camera device enumeration — which devices survive to the list.

Two ways a real camera used to go missing: a UVC camera claims two nodes and
only one of them can capture, and a slow camera could exceed the probe budget.
Everything here is monkeypatched rather than pointed at real hardware, so it
runs the same on a CI box with no camera at all.
"""

from __future__ import annotations

import inspect
import os
import sys
import time
from typing import Any

import pytest

from sorter.hardware import camera

# `capabilities` as a real UVC device reports it: the whole physical device,
# so both its nodes advertise capture. Only `device_caps` distinguishes them.
_DEVICE_CAPS_VALID = 0x84A00001
_CAPTURE_NODE_CAPS = 0x04200001  # VIDEO_CAPTURE | STREAMING | EXT_PIX_FORMAT
_METADATA_NODE_CAPS = 0x04A00000  # META_CAPTURE | STREAMING | EXT_PIX_FORMAT

linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="V4L2 node filtering is Linux-only (fcntl/ioctl)",
)


@pytest.mark.parametrize(
    ("platform", "backend_name"),
    [
        ("win32", "CAP_DSHOW"),
        ("linux", "CAP_V4L2"),
        ("darwin", "CAP_AVFOUNDATION"),
    ],
)
def test_each_platform_gets_its_explicit_backend(
    monkeypatch: pytest.MonkeyPatch, platform: str, backend_name: str
) -> None:
    """CAP_ANY autoprobing is what the explicit backends exist to avoid (#36)."""
    monkeypatch.setattr(camera.sys, "platform", platform)
    assert camera._preferred_backend() == getattr(camera.cv2, backend_name)


def test_an_unknown_platform_falls_back_to_cap_any(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(camera.sys, "platform", "sunos5")
    assert camera._preferred_backend() == camera.cv2.CAP_ANY


def _fake_v4l2(
    monkeypatch: pytest.MonkeyPatch,
    *,
    nodes: dict[str, tuple[int, int]],
    open_error: OSError | None = None,
    ioctl_error: OSError | None = None,
) -> None:
    """Patch os.open/os.close/fcntl.ioctl to serve `nodes` as fake V4L2 devices.

    `nodes` maps a /dev path to its (capabilities, device_caps) pair.
    """
    import fcntl

    fds: dict[int, str] = {}
    next_fd = [1000]

    def fake_open(path: Any, flags: int, *args: Any) -> int:
        if open_error is not None:
            raise open_error
        next_fd[0] += 1
        fds[next_fd[0]] = str(path)
        return next_fd[0]

    def fake_close(fd: int) -> None:
        fds.pop(fd, None)

    def fake_ioctl(fd: int, request: int, arg: Any = 0, mutate: bool = True) -> int:
        if ioctl_error is not None:
            raise ioctl_error
        assert request == camera._VIDIOC_QUERYCAP
        arg.capabilities, arg.device_caps = nodes[fds[fd]]
        return 0

    monkeypatch.setattr(os, "open", fake_open)
    monkeypatch.setattr(os, "close", fake_close)
    monkeypatch.setattr(fcntl, "ioctl", fake_ioctl)


@linux_only
def test_querycap_struct_matches_the_kernels_layout() -> None:
    """The ioctl request number encodes the struct size; the two must agree."""
    import ctypes

    assert ctypes.sizeof(camera._V4l2Capability) == 104
    assert camera._VIDIOC_QUERYCAP == (2 << 30) | (104 << 16) | (ord("V") << 8) | 0


@linux_only
def test_capture_node_is_a_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_v4l2(monkeypatch, nodes={"/dev/video0": (_DEVICE_CAPS_VALID, _CAPTURE_NODE_CAPS)})
    assert camera._is_v4l2_capture_node("/dev/video0") is True


@linux_only
def test_metadata_node_is_not_a_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug this guards: OpenCV can only fail to open a metadata node, loudly."""
    _fake_v4l2(monkeypatch, nodes={"/dev/video1": (_DEVICE_CAPS_VALID, _METADATA_NODE_CAPS)})
    assert camera._is_v4l2_capture_node("/dev/video1") is False


@linux_only
def test_device_caps_is_ignored_when_the_driver_does_not_set_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the DEVICE_CAPS bit, `capabilities` is all a pre-3.3 driver offers."""
    _fake_v4l2(monkeypatch, nodes={"/dev/video0": (0x04200001, 0)})
    assert camera._is_v4l2_capture_node("/dev/video0") is True


@linux_only
@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"open_error": PermissionError("not in the video group")}, "cannot open"),
        ({"ioctl_error": OSError(25, "Inappropriate ioctl for device")}, "not a V4L2 driver"),
    ],
)
def test_an_unanswerable_probe_keeps_the_device(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any], why: str
) -> None:
    """This filter narrows the list; it is never the reason a real camera vanishes."""
    _fake_v4l2(monkeypatch, nodes={}, **kwargs)
    assert camera._is_v4l2_capture_node("/dev/video0") is True, why


@linux_only
def test_candidate_indices_keeps_only_capture_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two cameras, four nodes — the shape a real UVC pair presents."""
    monkeypatch.setattr(camera.sys, "platform", "linux")
    present = {"/dev/video0", "/dev/video1", "/dev/video2", "/dev/video3"}
    monkeypatch.setattr(os.path, "exists", lambda p: p in present)
    _fake_v4l2(
        monkeypatch,
        nodes={
            "/dev/video0": (_DEVICE_CAPS_VALID, _CAPTURE_NODE_CAPS),
            "/dev/video1": (_DEVICE_CAPS_VALID, _METADATA_NODE_CAPS),
            "/dev/video2": (_DEVICE_CAPS_VALID, _CAPTURE_NODE_CAPS),
            "/dev/video3": (_DEVICE_CAPS_VALID, _METADATA_NODE_CAPS),
        },
    )
    assert camera._candidate_indices(10) == [0, 2]


@linux_only
def test_candidate_indices_skips_missing_nodes_without_probing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The existence check stays load-bearing: os.open on a missing path raises,
    and an unanswerable probe deliberately answers True."""
    monkeypatch.setattr(camera.sys, "platform", "linux")
    monkeypatch.setattr(os.path, "exists", lambda p: p == "/dev/video0")
    _fake_v4l2(monkeypatch, nodes={"/dev/video0": (_DEVICE_CAPS_VALID, _CAPTURE_NODE_CAPS)})
    assert camera._candidate_indices(10) == [0]


# ----- the probe budget -------------------------------------------------------
#
# Platform-independent: every branch below is reached through stubs, so these
# run on Windows and macOS too.


def _fake_captures(monkeypatch: pytest.MonkeyPatch, first_frame_delay_s: dict[int, float]) -> None:
    """Stub cv2.VideoCapture with devices that take a given time to serve a frame."""

    class _FakeCapture:
        def __init__(self, index: int, _backend: int) -> None:
            self.index = index

        def isOpened(self) -> bool:
            return True

        def read(self) -> tuple[bool, Any]:
            time.sleep(first_frame_delay_s[self.index])
            return True, object()

        def release(self) -> None:
            pass

    monkeypatch.setattr(camera.cv2, "VideoCapture", _FakeCapture)
    monkeypatch.setattr(camera, "_probe_resolutions", lambda cap: [(640, 480)])


def test_the_probe_budget_default_is_the_module_constant() -> None:
    """The literal default this replaced was the whole bug; keep them in one place."""
    default = inspect.signature(camera.list_cameras_with_metadata).parameters["probe_timeout_s"]
    assert default.default == camera.PROBE_TIMEOUT_S
    # The slowest camera measured needs ~2.6 s; anything near the old 2.5 s is
    # a regression even if it technically still passes for a faster device.
    assert camera.PROBE_TIMEOUT_S >= 4.0


def test_a_device_that_answers_in_time_is_listed_without_comment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(camera, "_candidate_indices", lambda _max: [0])
    monkeypatch.setattr(camera, "camera_names", lambda: {0: "Webcam"})
    _fake_captures(monkeypatch, {0: 0.0})

    found = camera.list_cameras_with_metadata(probe_timeout_s=2.0)

    assert [c["index"] for c in found] == [0]
    assert found[0]["name"] == "Webcam"
    assert capsys.readouterr().err == "", "a healthy device should say nothing"


def test_a_device_that_blows_the_budget_is_dropped_but_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The silence here is what made a camera 60 ms over budget so hard to explain."""
    monkeypatch.setattr(camera, "_candidate_indices", lambda _max: [0, 4])
    monkeypatch.setattr(camera, "camera_names", lambda: {0: "Built-in", 4: "Vitade AF"})
    _fake_captures(monkeypatch, {0: 0.0, 4: 1.0})

    found = camera.list_cameras_with_metadata(probe_timeout_s=0.2)

    assert [c["index"] for c in found] == [0]
    err = capsys.readouterr().err
    assert "index 4" in err
    assert "Vitade AF" in err, "name it, so the reader knows which camera went missing"
    assert "0.2" in err, "quote the budget it missed"
    assert "index 0" not in err
