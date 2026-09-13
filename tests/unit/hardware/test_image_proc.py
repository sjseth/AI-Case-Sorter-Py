"""Unit tests for image_proc — both crop strategies + primer mask."""

from __future__ import annotations

import cv2
import numpy as np

from sorter.hardware import image_proc


def _synthetic_headstamp(size: int = 640, radius: int = 180) -> np.ndarray:
    """A black canvas with a centered bright circle, simulating a brass case."""
    img = np.zeros((size, size, 3), dtype=np.uint8)
    cv2.circle(img, (size // 2, size // 2), radius, (200, 200, 200), -1)
    # Add a darker ring to mimic the rim.
    cv2.circle(img, (size // 2, size // 2), radius, (120, 120, 120), 4)
    return img


def _is_480x480_bgr(img: np.ndarray) -> bool:
    return img.shape == (480, 480, 3) and img.dtype == np.uint8


def test_hough_crop_shape_and_content() -> None:
    frame = _synthetic_headstamp()
    out = image_proc.hough_crop(frame, image_proc.HoughParams())
    assert _is_480x480_bgr(out)
    assert out.mean() > 5, "Hough crop produced an all-black canvas"


def test_linescan_crop_shape_and_content() -> None:
    frame = _synthetic_headstamp()
    out = image_proc.linescan_crop(frame, image_proc.LineScanParams())
    assert _is_480x480_bgr(out)
    assert out.mean() > 5, "Line-scan crop produced an all-black canvas"


def test_crop_dispatch_always_uses_hough() -> None:
    """crop_headstamp ignores `strategy` while linescan is hidden in the UI.

    Old configs with strategy='linescan' should silently route through Hough
    so the user doesn't keep running the dormant code path. linescan_crop
    remains callable directly for when we bring the UI back.
    """
    frame = _synthetic_headstamp()
    out_hough = image_proc.crop_headstamp(frame, {"strategy": "hough"})
    out_line_legacy = image_proc.crop_headstamp(frame, {"strategy": "linescan"})
    assert _is_480x480_bgr(out_hough)
    assert _is_480x480_bgr(out_line_legacy)
    # Both routes produce the same output now.
    assert np.array_equal(out_hough, out_line_legacy)


def test_primer_mask_hide_reduces_center_brightness() -> None:
    bright = np.full((480, 480, 3), 200, dtype=np.uint8)
    out = image_proc.apply_primer_mask(bright, "hide", 60)
    center = out[220:260, 220:260].mean()
    assert center < 5, "hide mode should leave the center black"


def test_primer_mask_use_zeros_outside_radius() -> None:
    bright = np.full((480, 480, 3), 200, dtype=np.uint8)
    out = image_proc.apply_primer_mask(bright, "use", 80)
    # Corners should be black because we clipped to a small centered circle.
    assert out[0, 0].sum() == 0
    assert out[-1, -1].sum() == 0
    # Center should still be bright.
    assert out[240, 240].mean() > 150


def test_primer_mask_none_passes_through() -> None:
    bright = np.full((480, 480, 3), 123, dtype=np.uint8)
    out = image_proc.apply_primer_mask(bright, "none", 80)
    assert np.array_equal(out, bright)


def test_crop_handles_tiny_image_gracefully() -> None:
    frame = np.zeros((10, 10, 3), dtype=np.uint8)
    out = image_proc.linescan_crop(frame, image_proc.LineScanParams())
    assert _is_480x480_bgr(out)


# ----- case presence (end-of-brass flush) -------------------------------------


def test_case_present_accepts_a_bright_case() -> None:
    frame = _synthetic_headstamp()
    assert image_proc.case_present(frame, {}) is True


def test_case_present_rejects_a_frame_with_no_circle() -> None:
    # Featureless frame: nothing for HoughCircles to find.
    frame = np.zeros((640, 640, 3), dtype=np.uint8)
    assert image_proc.case_present(frame, {}) is False


def test_case_present_rejects_a_dark_pocket_that_still_circles() -> None:
    """An empty, in-focus pocket yields a clean circle but a dark interior.

    This is the phantom-capture failure mode the brightness gate exists for:
    circle detection alone cannot tell an empty pocket from a case, because
    the pocket itself is round. The gate demands absolute brightness inside
    the disc, which only real brass provides.
    """
    size, radius = 640, 180
    frame = np.zeros((size, size, 3), dtype=np.uint8)
    # A crisp bright RING (the pocket edge) around a near-black interior —
    # plenty of gradient for Hough, nowhere near the brightness floor inside.
    cv2.circle(frame, (size // 2, size // 2), radius, (200, 200, 200), 6)
    cv2.circle(frame, (size // 2, size // 2), radius - 6, (12, 12, 12), -1)
    detection = image_proc.hough_detect(frame, image_proc.HoughParams())
    assert detection is not None, "the pocket ring must circle-detect for this test to mean anything"
    assert image_proc.case_present(frame, {}) is False


def test_case_present_floor_is_configurable_per_rig() -> None:
    # A shiny worn seat ring can read over 100 empty — the floor has to be
    # movable above whatever this rig's empty nest reads.
    frame = _synthetic_headstamp()  # disc reads ~200
    assert image_proc.case_present(frame, {"case_min_brightness": 150}) is True
    assert image_proc.case_present(frame, {"case_min_brightness": 250}) is False


def test_hough_detect_prefers_largest_circle_when_primer_is_also_present() -> None:
    """Synthetic frame with a brass disk and a smaller primer-like disk inside.

    HoughCircles is allowed to detect both; hough_detect must pick the brass
    (larger) circle, not the primer (smaller, higher-contrast inner edge).
    """
    size = 640
    frame = np.zeros((size, size, 3), dtype=np.uint8)
    brass_r = 180
    primer_r = 60
    cx, cy = size // 2, size // 2
    # Brass disk: medium grey on black background.
    cv2.circle(frame, (cx, cy), brass_r, (140, 140, 140), -1)
    cv2.circle(frame, (cx, cy), brass_r, (180, 180, 180), 3)
    # Primer: darker, high-contrast inner circle to mimic the real case where
    # the primer wins by accumulator vote.
    cv2.circle(frame, (cx, cy), primer_r, (40, 40, 40), -1)
    cv2.circle(frame, (cx, cy), primer_r, (10, 10, 10), 3)

    detection = image_proc.hough_detect(
        frame,
        image_proc.HoughParams(min_radius=40, max_radius=220, param2=30),
    )
    assert detection is not None
    _, _, r = detection
    # Must pick the brass disk, not the primer.
    assert r > 120, f"hough_detect picked the smaller circle (r={r})"


def test_overlay_detection_draws_circle_when_detection_present() -> None:
    frame = np.zeros((200, 200, 3), dtype=np.uint8)
    out = image_proc.overlay_detection(frame, (100.0, 100.0, 50.0))
    # Red pixels must exist on the ring (BGR red = (0, 0, 255)).
    red_count = int(((out[:, :, 0] == 0) & (out[:, :, 1] == 0) & (out[:, :, 2] == 255)).sum())
    assert red_count > 0


def test_overlay_detection_no_op_when_no_detection() -> None:
    frame = np.full((200, 200, 3), 42, dtype=np.uint8)
    out = image_proc.overlay_detection(frame, None)
    assert np.array_equal(out, frame)
