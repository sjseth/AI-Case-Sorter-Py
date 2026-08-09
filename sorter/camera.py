"""Cross-platform camera capture via OpenCV.

Uses CAP_DSHOW on Windows and CAP_V4L2 on Linux to avoid backend hangs.
A single grab thread keeps the latest frame in a slot; UI tabs poll it.

Includes a metadata probe (list_cameras_with_metadata) that returns each
detected device's friendly name (from /sys on Linux, pygrabber on Windows
when available) and the resolutions the device accepts.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import TypedDict

import cv2
import numpy as np

# Resolutions probed by `list_cameras_with_metadata`. Add more here if the
# operator's camera advertises something not in the list.
COMMON_RESOLUTIONS: list[tuple[int, int]] = [
    (320, 240),
    (640, 360),
    (640, 480),
    (800, 600),
    (1024, 768),
    (1280, 720),
    (1280, 960),
    (1600, 1200),
    (1920, 1080),
    (2560, 1440),
    (3840, 2160),
]


class CameraInfo(TypedDict):
    """One entry in `list_cameras_with_metadata`'s result."""

    index: int
    name: str
    resolutions: list[tuple[int, int]]


class _ProbeResult(TypedDict):
    """Accumulator a probe worker thread publishes into; read after a timed join."""

    opened: bool
    resolutions: list[tuple[int, int]]


def _preferred_backend() -> int:
    if sys.platform.startswith("win"):
        return cv2.CAP_DSHOW
    if sys.platform.startswith("linux"):
        return cv2.CAP_V4L2
    return cv2.CAP_ANY


# ----- friendly-name lookup ---------------------------------------------------


def _linux_camera_names() -> dict[int, str]:
    """video device index -> human-readable name, from /sys."""
    names: dict[int, str] = {}
    sysroot = "/sys/class/video4linux"
    if not os.path.isdir(sysroot):
        return names
    for entry in sorted(os.listdir(sysroot)):
        if not entry.startswith("video"):
            continue
        try:
            idx = int(entry[5:])
        except ValueError:
            continue
        name_path = os.path.join(sysroot, entry, "name")
        try:
            with open(name_path, encoding="utf-8") as f:
                names[idx] = f.read().strip()
        except OSError:
            pass
    return names


def _windows_camera_names() -> dict[int, str]:
    """device index -> name, via pygrabber's DirectShow enumeration.

    Returns an empty dict if pygrabber is not installed (it's a Windows-only
    optional dep).
    """
    try:
        # pygrabber is Windows-only and not installed in this (Linux) dev/CI
        # environment; the surrounding try/except is exactly the runtime guard
        # for that.
        from pygrabber.dshow_graph import FilterGraph  # type: ignore[import-not-found]  # ty: ignore[unresolved-import]
    except Exception:
        return {}
    try:
        graph = FilterGraph()
        return {i: name for i, name in enumerate(graph.get_input_devices())}
    except Exception:
        return {}


def camera_names() -> dict[int, str]:
    if sys.platform.startswith("linux"):
        return _linux_camera_names()
    if sys.platform.startswith("win"):
        return _windows_camera_names()
    return {}


def _windows_camera_resolutions(indices: list[int]) -> dict[int, list[tuple[int, int]]]:
    """device index -> sorted resolutions via DirectShow's GetStreamCaps.

    OpenCV's DSHOW set/get probe is unreliable — once FOURCC is set during
    probing, the device commonly wedges and reports only 640x480 for every
    width/height query for the rest of the session. pygrabber's
    ``get_formats()`` walks the device's advertised media types directly
    (IAMStreamConfig::GetStreamCaps), so it returns the real list. Returns
    {} if pygrabber isn't installed.
    """
    try:
        # pygrabber is Windows-only and not installed in this (Linux) dev/CI
        # environment; the surrounding try/except is exactly the runtime guard
        # for that.
        from pygrabber.dshow_graph import FilterGraph  # type: ignore[import-not-found]  # ty: ignore[unresolved-import]
    except Exception:
        return {}

    # comtypes only auto-inits COM on the thread that first imports it
    # (the main thread). Worker threads — like the one run_worker hands
    # this off to during the startup auto-detect — start with COM
    # uninitialised, which makes the very first FilterGraph() build a
    # broken graph and get_formats() return nothing. Init/uninit per-call
    # so we work regardless of caller thread.
    try:
        # comtypes is Windows-only and not installed in this (Linux) dev/CI
        # environment; the surrounding try/except is exactly the runtime guard
        # for that.
        import comtypes  # type: ignore[import-not-found]  # ty: ignore[unresolved-import]

        comtypes.CoInitialize()
        com_inited = True
    except Exception:
        comtypes = None  # type: ignore[assignment]
        com_inited = False

    try:
        out: dict[int, list[tuple[int, int]]] = {}
        for idx in indices:
            try:
                graph = FilterGraph()
                graph.add_video_input_device(idx)
                formats = graph.get_input_device().get_formats()
            except Exception:
                continue
            seen: set[tuple[int, int]] = set()
            for f in formats:
                w = int(f.get("width", 0) or 0)
                h = int(f.get("height", 0) or 0)
                if w > 0 and h > 0:
                    seen.add((w, h))
            if seen:
                out[idx] = sorted(seen, key=lambda wh: wh[0] * wh[1])
        return out
    finally:
        if com_inited and comtypes is not None:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass


def _windows_dshow_device_count() -> int | None:
    """Number of currently-attached DirectShow capture devices, via pygrabber.

    Returns None if pygrabber isn't installed (callers should treat as
    "unknown — try every index"). Returns 0 if no devices are connected.
    """
    try:
        # pygrabber is Windows-only and not installed in this (Linux) dev/CI
        # environment; the surrounding try/except is exactly the runtime guard
        # for that.
        from pygrabber.dshow_graph import FilterGraph  # type: ignore[import-not-found]  # ty: ignore[unresolved-import]
    except Exception:
        return None
    try:
        # comtypes is Windows-only and not installed in this (Linux) dev/CI
        # environment; the surrounding try/except is exactly the runtime guard
        # for that.
        import comtypes  # type: ignore[import-not-found]  # ty: ignore[unresolved-import]

        comtypes.CoInitialize()
        com_inited = True
    except Exception:
        comtypes = None  # type: ignore[assignment]
        com_inited = False
    try:
        try:
            graph = FilterGraph()
            return len(graph.get_input_devices())
        except Exception:
            return None
    finally:
        if com_inited and comtypes is not None:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass


# ----- enumeration ------------------------------------------------------------


def _candidate_indices(max_index: int) -> list[int]:
    """Indices worth probing.

    On Linux, skip indices with no /dev/videoN node at all — probing them
    just produces OpenCV's noisy "can't open camera by index" V4L2 warnings.
    On Windows, ask DirectShow how many capture devices are attached so
    we don't fire OpenCV at indices 1-9 when only camera 0 exists (each
    miss prints "VIDEOIO(DSHOW): backend is generally available but can't
    be used to capture by index").
    """
    if sys.platform.startswith("linux"):
        return [i for i in range(max_index) if os.path.exists(f"/dev/video{i}")]
    if sys.platform.startswith("win"):
        count = _windows_dshow_device_count()
        if count is not None:
            return list(range(min(count, max_index)))
    return list(range(max_index))


def enumerate_devices(max_index: int = 10, probe_timeout_s: float = 1.5) -> list[int]:
    """Return camera indices that successfully opened and returned a frame.

    Each probe runs in a worker thread; we move on if it exceeds probe_timeout_s
    (some V4L2 devices hang indefinitely on open).
    """
    backend = _preferred_backend()
    found: list[int] = []
    for idx in _candidate_indices(max_index):
        result: list[bool] = [False]

        def _probe(i: int = idx, res: list[bool] = result) -> None:
            cap = cv2.VideoCapture(i, backend)
            try:
                if cap.isOpened():
                    ok, _ = cap.read()
                    if ok:
                        res[0] = True
            finally:
                cap.release()

        t = threading.Thread(target=_probe, daemon=True)
        t.start()
        t.join(probe_timeout_s)
        if result[0]:
            found.append(idx)
    return found


def _probe_resolutions(cap: cv2.VideoCapture) -> list[tuple[int, int]]:
    """Try each resolution in COMMON_RESOLUTIONS; collect what the device actually serves.

    DirectShow on Windows commonly substitutes the closest supported size when
    you ask for one it doesn't have, so we add the resolution the camera
    *returned* (not the one we asked for) — every returned size is, by
    definition, one the device supports.
    """
    # Match the playback pixel format on V4L2. Many UVC webcams advertise
    # 1080p only under MJPG; the V4L2 default of YUYV refuses (or caps the
    # FPS of) the higher modes, so probing under YUYV under-reports what the
    # camera can actually deliver at runtime. On DirectShow this set() can
    # leave the device stuck reporting 640x480 for every subsequent get(),
    # so the Windows path uses _windows_camera_resolutions instead and only
    # falls back here if pygrabber isn't installed.
    if sys.platform.startswith("linux"):
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))  # ty: ignore[unresolved-attribute]  # opencv-python's bundled stubs omit VideoWriter_fourcc; it exists at runtime
    orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    supported: set[tuple[int, int]] = set()
    if orig_w > 0 and orig_h > 0:
        supported.add((orig_w, orig_h))
    for w, h in COMMON_RESOLUTIONS:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if actual_w > 0 and actual_h > 0:
            supported.add((actual_w, actual_h))
    if orig_w and orig_h:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, orig_w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, orig_h)
    # Sort by total pixel count so callers can pick "highest" as the last item.
    return sorted(supported, key=lambda wh: wh[0] * wh[1])


def list_cameras_with_metadata(max_index: int = 10, probe_timeout_s: float = 2.5) -> list[CameraInfo]:
    """Enumerate cameras and return [{'index': int, 'name': str, 'resolutions': [(w,h), ...]}].

    Resolutions are sorted ascending by pixel count, so `resolutions[-1]` is the
    highest the device accepts among COMMON_RESOLUTIONS.
    """
    backend = _preferred_backend()
    names = camera_names()
    candidates = _candidate_indices(max_index)

    # On Windows, query each device's supported resolutions via DirectShow
    # before any OpenCV capture is opened. The OpenCV DSHOW probe used on
    # Linux (set width/height, read back actual) reliably wedges at 640x480
    # on Windows once FOURCC has been touched, so the native enumeration is
    # both faster and accurate.
    pre_resolutions: dict[int, list[tuple[int, int]]] = {}
    if sys.platform.startswith("win"):
        pre_resolutions = _windows_camera_resolutions(candidates)

    out: list[CameraInfo] = []
    for idx in candidates:
        result: _ProbeResult = {"opened": False, "resolutions": []}

        def _probe(i: int = idx, res: _ProbeResult = result) -> None:
            cap = cv2.VideoCapture(i, backend)
            try:
                if not cap.isOpened():
                    return
                # Probe resolutions *before* the confirmation read below.
                # On V4L2, once a frame has been read the device starts
                # streaming and subsequent set() calls for FOURCC/resolution
                # silently fail (EBUSY while streaming), which would leave
                # every probed size stuck at the initial negotiated default.
                if i in pre_resolutions:
                    resolutions = pre_resolutions[i]
                else:
                    resolutions = _probe_resolutions(cap)
                ok, _ = cap.read()
                if not ok:
                    return
                # Publish `opened` last: the caller reads after a timed join,
                # so it must not observe opened=True before resolutions lands.
                res["resolutions"] = resolutions
                res["opened"] = True
            finally:
                cap.release()

        t = threading.Thread(target=_probe, daemon=True)
        t.start()
        t.join(probe_timeout_s)
        if result["opened"]:
            out.append(
                {
                    "index": idx,
                    "name": names.get(idx, f"Camera {idx}"),
                    "resolutions": result["resolutions"],
                }
            )
    return out


class Camera:
    def __init__(self, device_index: int = 0, width: int = 640, height: int = 480) -> None:
        self.device_index = device_index
        self.width = width
        self.height = height
        self._cap: cv2.VideoCapture | None = None
        self._latest_frame: np.ndarray | None = None
        self._frame_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def open(self) -> bool:
        self._cap = cv2.VideoCapture(self.device_index, _preferred_backend())
        if not self._cap.isOpened():
            self._cap = None
            return False
        # MJPG unlocks the higher resolutions on USB webcams via V4L2 — the
        # default YUYV format often refuses 1920x1080 or caps the FPS so
        # low that the preview stutters. DirectShow on Windows already
        # negotiates MJPG for most webcams; setting it again is harmless.
        self._cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))  # ty: ignore[unresolved-attribute]  # opencv-python's bundled stubs omit VideoWriter_fourcc; it exists at runtime
        if sys.platform.startswith("linux"):
            # V4L2 auto-exposure defaults blow out the image once the LED
            # ring is on — operators end up dropping cameraledlevel to 2-3
            # to compensate. Pin to manual mode (V4L2 value 1) with a
            # moderate exposure so the LED slider scales brightness
            # predictably across the full 1-255 range.
            self._cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)
            self._cap.set(cv2.CAP_PROP_EXPOSURE, 156)
        if self.width:
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        if self.height:
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        return True

    def start_preview(self) -> bool:
        if self._cap is None and not self.open():
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._grab_loop, name="CameraGrab", daemon=True)
        self._thread.start()
        return True

    def _grab_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._cap is None:
                return
            ok, frame = self._cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            with self._frame_lock:
                self._latest_frame = frame
            time.sleep(0.01)

    def latest_frame(self) -> np.ndarray | None:
        with self._frame_lock:
            if self._latest_frame is None:
                return None
            return self._latest_frame.copy()

    def capture_frame(self) -> np.ndarray | None:
        """Return the most recent frame. Opens & grabs once if no preview is running."""
        frame = self.latest_frame()
        if frame is not None:
            return frame
        if self._cap is None and not self.open():
            return None
        # No preview thread → grab a fresh frame inline.
        assert self._cap is not None
        for _ in range(3):
            ok, frame = self._cap.read()
            if ok:
                return frame
            time.sleep(0.05)
        return None

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
