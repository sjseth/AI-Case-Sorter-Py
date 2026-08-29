"""Image processing pipeline.

Two crop strategies behind a common signature and a separate primer-mask step.
Output is always a 480x480 BGR uint8 frame on a black background.

Ports the relevant pieces of the legacy app's image processing: the line-scan
crop, the HoughCircles crop, the square-to-circle conversion, and the
clip-to-circle / primer-mask steps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

OUTPUT_SIZE = 480


# ----- public params -----------------------------------------------------------


@dataclass
class HoughParams:
    dp: float = 2.0
    min_dist: int = 200
    param1: float = 100.0
    param2: float = 50.0
    min_radius: int = 90
    max_radius: int = 220

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> HoughParams:
        return cls(
            dp=float(d.get("dp", 2.0)),
            min_dist=int(d.get("min_dist", 200)),
            param1=float(d.get("param1", 100.0)),
            param2=float(d.get("param2", 50.0)),
            min_radius=int(d.get("min_radius", 90)),
            max_radius=int(d.get("max_radius", 220)),
        )


@dataclass
class LineScanParams:
    scan_precision: int = 1
    scan_sensitivity: float = 5.0
    padding_pct: float = 5.0
    bg_cliff: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LineScanParams:
        return cls(
            scan_precision=max(1, int(d.get("scan_precision", 1))),
            scan_sensitivity=float(d.get("scan_sensitivity", 5.0)),
            padding_pct=float(d.get("padding_pct", 5.0)),
            bg_cliff=int(d.get("bg_cliff", 0)),
        )


# ----- canvas helpers ----------------------------------------------------------


def _clip_to_circle(src: np.ndarray, center: tuple[int, int], radius: int) -> np.ndarray:
    """Paste a circular ROI from `src` onto a 480x480 black canvas.

    Destination ROI is the full 480x480; source rectangle is the square
    bounding box of the circle, resized to fit.
    """
    if radius <= 0:
        return np.zeros((OUTPUT_SIZE, OUTPUT_SIZE, 3), dtype=np.uint8)

    cx, cy = center
    left = max(0, cx - radius)
    top = max(0, cy - radius)
    right = min(src.shape[1], cx + radius)
    bottom = min(src.shape[0], cy + radius)
    if right <= left or bottom <= top:
        return np.zeros((OUTPUT_SIZE, OUTPUT_SIZE, 3), dtype=np.uint8)

    roi = src[top:bottom, left:right]
    resized = cv2.resize(roi, (OUTPUT_SIZE, OUTPUT_SIZE), interpolation=cv2.INTER_CUBIC)

    canvas = np.zeros((OUTPUT_SIZE, OUTPUT_SIZE, 3), dtype=np.uint8)
    mask = np.zeros((OUTPUT_SIZE, OUTPUT_SIZE), dtype=np.uint8)
    cv2.circle(mask, (OUTPUT_SIZE // 2, OUTPUT_SIZE // 2), OUTPUT_SIZE // 2, 255, -1)
    canvas[mask == 255] = resized[mask == 255]
    return canvas


# ----- Hough strategy ----------------------------------------------------------


def hough_detect(frame_bgr: np.ndarray, params: HoughParams) -> tuple[float, float, float] | None:
    """Run cv2.HoughCircles and return (cx, cy, r) for the LARGEST circle, or None.

    HoughCircles sorts results by accumulator vote count, but for a brass
    headstamp the primer often outvotes the case rim because its inner edge
    has higher local contrast. Since the primer is always smaller than the
    case, taking the largest detection within [min_radius, max_radius]
    reliably picks the brass rim whenever both are detected.
    """
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (9, 9), 2)

    circles = cv2.HoughCircles(
        gray,
        cv2.HOUGH_GRADIENT,
        dp=params.dp,
        minDist=params.min_dist,
        param1=params.param1,
        param2=params.param2,
        minRadius=params.min_radius,
        maxRadius=params.max_radius,
    )
    if circles is None or len(circles[0]) == 0:
        return None
    best = max(circles[0], key=lambda c: c[2])  # c = (x, y, r)
    return float(best[0]), float(best[1]), float(best[2])


def hough_crop(frame_bgr: np.ndarray, params: HoughParams) -> np.ndarray:
    """cv2.HoughCircles based crop."""
    detection = hough_detect(frame_bgr, params)
    if detection is None:
        # Fall back to the middle 30% of the image.
        h, w = frame_bgr.shape[:2]
        return _clip_to_circle(frame_bgr, (w // 2, h // 2), int(min(w, h) * 0.30))
    cx, cy, r = detection
    # 5% radial buffer.
    r_padded = int(r * 1.05)
    return _clip_to_circle(frame_bgr, (int(cx), int(cy)), r_padded)


def overlay_detection(
    frame_bgr: np.ndarray,
    detection: tuple[float, float, float] | None,
) -> np.ndarray:
    """Return a copy of frame_bgr with the detected circle outlined in red.

    Used by the Image Processing tab's "Test on current frame" button so the
    operator can see exactly what HoughCircles picked while tuning params.
    """
    out = frame_bgr.copy()
    if detection is None:
        return out
    cx, cy, r = detection
    cv2.circle(out, (int(cx), int(cy)), int(r), (0, 0, 255), 3)
    cv2.drawMarker(
        out,
        (int(cx), int(cy)),
        (0, 255, 255),
        markerType=cv2.MARKER_CROSS,
        markerSize=20,
        thickness=2,
    )
    return out


# ----- Line-scan strategy ------------------------------------------------------


def _line_means_x(gray_minus_cliff: np.ndarray, col_idx: int, scale: int = 3) -> float:
    """Average brightness of a single vertical line (col_idx) summed over 3 channels.

    The original sums R+G+B per pixel sample (after bg_cliff). Since we already
    work in grayscale, we multiply by 3 so the sensitivity threshold has
    comparable scale to the original.
    """
    if col_idx < 0 or col_idx >= gray_minus_cliff.shape[1]:
        return 0.0
    return float(gray_minus_cliff[:, col_idx].mean() * scale)


def _line_means_y(gray_minus_cliff: np.ndarray, row_idx: int) -> float:
    if row_idx < 0 or row_idx >= gray_minus_cliff.shape[0]:
        return 0.0
    return float(gray_minus_cliff[row_idx, :].mean())


def linescan_crop(frame_bgr: np.ndarray, params: LineScanParams) -> np.ndarray:
    """Numpy port of the legacy app's ScanImageHalves crop.

    The original walks raw bitmap bytes in three-channel order. Here we precompute
    a grayscale and per-row / per-col means, then do the coarse-grid + fine-walk in
    Python (small constant factor of work — the loops iterate over O(width) means,
    not O(width*height) pixels).
    """
    h, w = frame_bgr.shape[:2]
    if h < 12 or w < 12:
        return np.zeros((OUTPUT_SIZE, OUTPUT_SIZE, 3), dtype=np.uint8)

    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY).astype(np.int32)
    if params.bg_cliff > 0:
        gray = np.where(gray - params.bg_cliff > 0, gray, 0)

    # Pre-compute column-mean and row-mean arrays once.
    col_means = gray.mean(axis=0)  # shape (w,)
    row_means = gray.mean(axis=1)  # shape (h,)

    rough_w_step = max(1, w // 12)
    rough_h_step = max(1, h // 12)
    sensitivity = float(params.scan_sensitivity)
    precision = max(1, int(params.scan_precision))

    # Coarse passes mirror the four foreach loops in the original.
    def _coarse_start(samples: list[float], step: int, reverse: bool, limit: int) -> int:
        running_sum = 0.0
        for i, val in enumerate(samples, start=1):
            running_sum += val
            average = running_sum / i
            if val - average > sensitivity:
                idx = (len(samples) - i) * step if reverse else (i - 1) * step
                return max(0, idx - step) if not reverse else min(limit - 1, idx + step)
        return limit - 1 if reverse else 0

    x_samples = [float(col_means[min(i, w - 1)] * 3) for i in range(0, w, rough_w_step)]
    y_samples = [float(row_means[min(i, h - 1)]) for i in range(0, h, rough_h_step)]

    scan_left_start = _coarse_start(x_samples, rough_w_step, reverse=False, limit=w)
    scan_right_start = _coarse_start(list(reversed(x_samples)), rough_w_step, reverse=True, limit=w)
    scan_top_start = _coarse_start(y_samples, rough_h_step, reverse=False, limit=h)
    scan_bottom_start = _coarse_start(list(reversed(y_samples)), rough_h_step, reverse=True, limit=h)

    # Fine-walk: walk from each rough boundary inward until the running mean diverges
    # from the just-read line mean by more than `sensitivity`. Mirrors the four
    # corresponding loops in the original.
    def _fine_walk(start: int, step: int, axis_fn, limit: int, *, from_low: bool) -> int:
        scan_data = [0.0]
        i = start
        last_hit = (limit // 2) if from_low else (limit // 2)
        guard_threshold = 5 if from_low else (limit - 5)
        while (from_low and i < limit) or (not from_low and i > 0):
            avg = sum(scan_data) / len(scan_data)
            point = axis_fn(i)
            scan_data.append(point)
            if (from_low and i > guard_threshold) or (not from_low and i < guard_threshold):
                if abs(point - avg) > sensitivity:
                    last_hit = i
                    return last_hit
            i += step if from_low else -step
        return last_hit

    sqcrp_left = _fine_walk(
        max(0, scan_left_start),
        precision,
        lambda idx: _line_means_x(gray, min(idx, w - 1)),
        w,
        from_low=True,
    )
    sqcrp_right = _fine_walk(
        min(w - 1, scan_right_start),
        precision,
        lambda idx: _line_means_x(gray, max(0, idx)),
        w,
        from_low=False,
    )
    sqcrp_top = _fine_walk(
        max(0, scan_top_start),
        precision,
        lambda idx: _line_means_y(gray, min(idx, h - 1)),
        h,
        from_low=True,
    )
    sqcrp_bottom = _fine_walk(
        min(h - 1, scan_bottom_start),
        precision,
        lambda idx: _line_means_y(gray, max(0, idx)),
        h,
        from_low=False,
    )

    if sqcrp_right <= sqcrp_left or sqcrp_bottom <= sqcrp_top:
        # Fall back to centered 30%.
        return _clip_to_circle(frame_bgr, (w // 2, h // 2), int(min(w, h) * 0.30))

    cx = (sqcrp_left + sqcrp_right) // 2
    cy = (sqcrp_top + sqcrp_bottom) // 2
    radius = (sqcrp_bottom - sqcrp_top) // 2
    radius = int(radius * (1.0 + params.padding_pct / 100.0))
    return _clip_to_circle(frame_bgr, (cx, cy), max(1, radius))


# ----- common entry point ------------------------------------------------------


def crop_headstamp(frame_bgr: np.ndarray, config: dict[str, Any]) -> np.ndarray:
    """Crop the brass headstamp out of `frame_bgr`.

    Currently always uses the OpenCV HoughCircles strategy. The line-scan
    strategy is intentionally dormant — the implementation in linescan_crop()
    is preserved so it can be reinstated later without rewriting it. To bring
    it back: re-add the strategy radio to ImageProcTab and restore the
    dispatch below.
    """
    # strategy = (config.get("strategy") or "hough").lower()
    # if strategy == "linescan":
    #     return linescan_crop(frame_bgr, LineScanParams.from_dict(config.get("linescan", {})))
    return hough_crop(frame_bgr, HoughParams.from_dict(config.get("hough", {})))


# ----- case presence -----------------------------------------------------------

# A real case must be *absolutely* bright, not just circular: a focused but
# EMPTY camera pocket still yields a clean circle (the pocket itself), so
# circle detection alone reports phantom cases at the start and end of a run.
# Calibrated on a live machine over a 250-frame sweep: empty-pocket frames
# read a mean of ~12 inside the disc, the darkest real case 66, median 114 —
# so 40 splits the populations with wide margin on both sides.
CASE_MIN_DISC_BRIGHTNESS = 40


def case_present(frame_bgr: np.ndarray, config: dict[str, Any]) -> bool:
    """Whether a case (not an empty, in-focus pocket) sits at the camera.

    Two gates: a circle must be detected at all, and the mean gray level
    inside 80% of its radius must clear CASE_MIN_DISC_BRIGHTNESS. Used by the
    end-of-brass flush to decide when the wheel has walked dry — deliberately
    not part of the normal capture path, which stays permissive.
    """
    detection = hough_detect(frame_bgr, HoughParams.from_dict(config.get("hough", {})))
    if detection is None:
        return False
    cx, cy, r = detection
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    mask = np.zeros(gray.shape, dtype=np.uint8)
    cv2.circle(mask, (int(cx), int(cy)), max(1, int(r * 0.8)), 255, -1)
    if not mask.any():
        return False
    return float(gray[mask == 255].mean()) >= CASE_MIN_DISC_BRIGHTNESS


# ----- primer mask -------------------------------------------------------------


def apply_primer_mask(img_bgr: np.ndarray, mode: str, radius: int) -> np.ndarray:
    """Apply the primer-mask post-step.

    "use"  → clip to a centered circle
    "hide" → draw a black filled circle over the primer
    "none" → return unchanged
    """
    if radius <= 0 or mode == "none":
        return img_bgr
    mode = mode.lower()
    if mode == "use":
        return _clip_to_circle(img_bgr, (OUTPUT_SIZE // 2, OUTPUT_SIZE // 2), int(radius))
    if mode == "hide":
        out = img_bgr.copy()
        cv2.circle(out, (OUTPUT_SIZE // 2, OUTPUT_SIZE // 2), int(radius), (0, 0, 0), -1)
        return out
    return img_bgr
