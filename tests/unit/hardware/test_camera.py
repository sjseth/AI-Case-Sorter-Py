"""Unit tests for camera device enumeration — which /dev/videoN nodes get probed.

A UVC camera claims two nodes, only one of which can capture. Everything here
is monkeypatched rather than pointed at real hardware, so it runs the same on a
CI box with no camera at all.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import pytest

from sorter.hardware import camera

# `capabilities` as a real UVC device reports it: the whole physical device,
# so both its nodes advertise capture. Only `device_caps` distinguishes them.
_DEVICE_CAPS_VALID = 0x84A00001
_CAPTURE_NODE_CAPS = 0x04200001  # VIDEO_CAPTURE | STREAMING | EXT_PIX_FORMAT
_METADATA_NODE_CAPS = 0x04A00000  # META_CAPTURE | STREAMING | EXT_PIX_FORMAT

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="V4L2 node filtering is Linux-only (fcntl/ioctl)",
)


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


def test_querycap_struct_matches_the_kernels_layout() -> None:
    """The ioctl request number encodes the struct size; the two must agree."""
    import ctypes

    assert ctypes.sizeof(camera._V4l2Capability) == 104
    assert camera._VIDIOC_QUERYCAP == (2 << 30) | (104 << 16) | (ord("V") << 8) | 0


def test_capture_node_is_a_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_v4l2(monkeypatch, nodes={"/dev/video0": (_DEVICE_CAPS_VALID, _CAPTURE_NODE_CAPS)})
    assert camera._is_v4l2_capture_node("/dev/video0") is True


def test_metadata_node_is_not_a_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug this guards: OpenCV can only fail to open a metadata node, loudly."""
    _fake_v4l2(monkeypatch, nodes={"/dev/video1": (_DEVICE_CAPS_VALID, _METADATA_NODE_CAPS)})
    assert camera._is_v4l2_capture_node("/dev/video1") is False


def test_device_caps_is_ignored_when_the_driver_does_not_set_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the DEVICE_CAPS bit, `capabilities` is all a pre-3.3 driver offers."""
    _fake_v4l2(monkeypatch, nodes={"/dev/video0": (0x04200001, 0)})
    assert camera._is_v4l2_capture_node("/dev/video0") is True


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


def test_candidate_indices_skips_missing_nodes_without_probing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The existence check stays load-bearing: os.open on a missing path raises,
    and an unanswerable probe deliberately answers True."""
    monkeypatch.setattr(camera.sys, "platform", "linux")
    monkeypatch.setattr(os.path, "exists", lambda p: p == "/dev/video0")
    _fake_v4l2(monkeypatch, nodes={"/dev/video0": (_DEVICE_CAPS_VALID, _CAPTURE_NODE_CAPS)})
    assert camera._candidate_indices(10) == [0]
