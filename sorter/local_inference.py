"""In-process PyTorch inference for locally-trained ConvNeXt models.

Used by `RunController` whenever the active model has a `model_path` set.
`torch` and `torchvision` are imported lazily on first call so users running
in pure OpenAI-API mode don't pay the multi-second torch import at startup.

Models are cached by `(absolute path, mtime)` so a re-train (which rewrites
the .pth file) is automatically picked up.

Checkpoint format expected (produced by the in-tree training script):
    {
      "model_state_dict": ...,    # state dict for the modified classifier
      "classes": ["FC", "WIN", ...],
      "base": "convnext_tiny",    # or _small / _base / _large
      "val_acc": float | None,    # optional
      "val_loss": float | None    # optional
    }

DESIGN: This mirrors the standalone CaseSorter AI server, which processes
the same ConvNeXt-Tiny checkpoints at single-digit ms; aligning this path
with it gives us the same steady-state performance.

Key invariants borrowed from the server:
  * Device picked exactly ONCE on first _torch() call, stored in
    module-level `_device_cache`.
  * Model moved to the device exactly ONCE inside _load() right after
    load_state_dict — `net.to(device).eval()`. classify() does NOT
    re-move the model.
  * classify() moves only the per-call tensor to the device.
  * No cuDNN flag tweaks (benchmark/TF32/etc) — defaults work for
    ConvNeXt-Tiny on sm_120.

Diagnostic stderr prints (env dump, per-call timing breakdown, model
device confirmation) are intentionally retained.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

_lock = threading.Lock()
_cache: dict[tuple[str, int], _LoadedModel] = {}
_torch_mod: Any = None
_models_mod: Any = None
_F_mod: Any = None
_env_dumped = False
# Module-level device cache, mirroring the standalone AI server's
# `DEVICE = _select_device()` pattern. Picked once on first _torch()
# call and reused everywhere; classify() does NOT re-probe per call.
_device_cache: Any = None
# Persistent single-threaded executor that owns ALL GPU work.
# `app.run_worker` creates a fresh daemon thread per call, so consecutive
# classify() invocations would each land on a different thread. PyTorch
# CUDA streams and cuDNN algorithm/workspace caches are per-thread, so
# every fresh thread forces cuDNN to re-tune or fall back to a slow
# algorithm (measured ~825 ms forward vs. ~5 ms when reusing a thread).
# Routing classify through this executor pins all forward passes to one
# thread that stays warm for the life of the process.
_executor: concurrent.futures.ThreadPoolExecutor | None = None

# Per-step timings populated by the most recent classify() call.
# Diagnostic only — read directly when investigating perf regressions.
last_timings: dict[str, float] = {}


@dataclass
class _LoadedModel:
    net: Any  # Resident on the cached device after _load()
    classes: list[str]
    base: str
    image_size: int


class LocalInferenceError(Exception):
    pass


def _torch():
    """Lazy importer. Raises LocalInferenceError with a friendly message if missing."""
    global _torch_mod, _models_mod, _F_mod, _env_dumped
    if _torch_mod is None:
        # torch is usually installed by the in-app dialog *while this process
        # is running*, so the import-system's cached directory listing for
        # site-packages predates it. Without this the first post-install
        # import fails and the user is told to install what they just did.
        import importlib

        importlib.invalidate_caches()
        try:
            # torch/torchvision are the optional `[ml]` extra — genuinely
            # absent from this dev/CI environment by design; the except below
            # is exactly the runtime guard for that.
            import torch  # ty: ignore[unresolved-import]
            import torch.nn.functional as F  # ty: ignore[unresolved-import]
            from torchvision import models  # ty: ignore[unresolved-import]
        except ImportError as exc:
            raise LocalInferenceError("PyTorch is not installed. Install with `pip install .[ml]`.") from exc
        _torch_mod = torch
        _models_mod = models
        _F_mod = F
        # All cuDNN/perf flags intentionally left at defaults — the
        # reference AI server runs at single-digit ms
        # without any flag tweaks, so cuDNN's default heuristic is fine
        # for ConvNeXt on sm_120 when the rest of the pipeline matches
        # the server.
    if _device_cache is None:
        # Pick device exactly once, mirroring the reference AI server.
        _pick_device(_torch_mod)
    if not _env_dumped:
        _env_dumped = True
        _dump_environment(_torch_mod)
    return _torch_mod, _models_mod, _F_mod


def _pick_device(torch_mod: Any) -> Any:
    """One-shot device selection, mirroring the reference AI server.

    Stores the chosen torch.device in module-level `_device_cache` so
    classify() never has to re-probe.
    """
    global _device_cache
    import sys

    if not torch_mod.cuda.is_available():
        _device_cache = torch_mod.device("cpu")
        print("[device] CUDA unavailable; using CPU", file=sys.stderr, flush=True)
        return _device_cache
    try:
        probe = torch_mod.randn(1, 3, 8, 8, device="cuda")
        _ = probe.sum().item()
        _device_cache = torch_mod.device("cuda")
        print(f"[device] CUDA ok: {torch_mod.cuda.get_device_name(0)}", file=sys.stderr, flush=True)
    except Exception as exc:
        _device_cache = torch_mod.device("cpu")
        print(f"[device] CUDA probe failed ({exc}); using CPU", file=sys.stderr, flush=True)
    return _device_cache


def _dump_environment(torch_mod: Any) -> None:
    """Print a one-time summary of torch/CUDA configuration to stderr."""
    import sys

    try:
        cuda_avail = torch_mod.cuda.is_available()
        cudnn_v = None
        arch_list: list[str] = []
        if cuda_avail:
            try:
                cudnn_v = torch_mod.backends.cudnn.version()
            except Exception:
                pass
            try:
                arch_list = list(torch_mod.cuda.get_arch_list())
            except Exception:
                pass
        print(
            f"[env] torch={torch_mod.__version__} "
            f"cuda_available={cuda_avail} "
            f"cuda_version={torch_mod.version.cuda} "
            f"cudnn={cudnn_v} "
            f"threads={torch_mod.get_num_threads()}",
            file=sys.stderr,
            flush=True,
        )
        if cuda_avail:
            print(f"[env] arch_list={arch_list}", file=sys.stderr, flush=True)
            for i in range(torch_mod.cuda.device_count()):
                name = torch_mod.cuda.get_device_name(i)
                cap = torch_mod.cuda.get_device_capability(i)
                props = torch_mod.cuda.get_device_properties(i)
                mem_gb = props.total_memory / (1024**3)
                sm_tag = f"sm_{cap[0]}{cap[1]}"
                # If arch_list doesn't include this sm tag, PyTorch
                # JIT-compiles kernels from PTX on every launch and
                # performance is unusable. We still report it but make
                # the situation obvious.
                supported = any(sm_tag in a or f"compute_{cap[0]}{cap[1]}" in a for a in arch_list)
                marker = "" if supported else "  ⚠  NOT in arch_list — PTX JIT fallback"
                print(
                    f"[env] device[{i}]={name!r} {sm_tag} vram={mem_gb:.1f}GB mp={props.multi_processor_count}{marker}",
                    file=sys.stderr,
                    flush=True,
                )
                if not supported:
                    print(
                        f"[env] FIX: install a PyTorch build that bakes {sm_tag} "
                        "in its arch list (e.g. the nightly cu128 build).",
                        file=sys.stderr,
                        flush=True,
                    )
            # Synthetic benchmarks decouple raw GPU / cuDNN throughput
            # from anything in our classify pipeline.
            try:
                torch_mod.cuda.synchronize()
                x = torch_mod.randn(1024, 1024, device="cuda")
                for _ in range(2):  # warm-up
                    _ = x @ x
                torch_mod.cuda.synchronize()
                t = time.perf_counter()
                iters = 10
                for _ in range(iters):
                    _ = x @ x
                torch_mod.cuda.synchronize()
                ms_per = (time.perf_counter() - t) * 1000.0 / iters
                print(f"[env] matmul_1024x1024_fp32: {ms_per:.2f} ms/iter", file=sys.stderr, flush=True)
            except Exception as exc:
                print(f"[env] matmul benchmark failed: {exc}", file=sys.stderr, flush=True)

            # ConvNeXt-Tiny forward — same model class our classify uses.
            # If this matches our 800+ ms classify time, the bottleneck is
            # purely cuDNN / Blackwell conv kernels. If it's much faster,
            # the issue is something in our pipeline (.to(device) walks,
            # tensor strides, etc.).
            try:
                # torchvision is the optional `[ml]` extra — genuinely absent
                # from this dev/CI environment by design.
                from torchvision import models as tv_models  # ty: ignore[unresolved-import]

                bench_net = tv_models.convnext_tiny(weights=None).cuda().eval()
                bench_x = torch_mod.randn(1, 3, 224, 224, device="cuda")
                with torch_mod.inference_mode():
                    for _ in range(2):  # warm-up
                        _ = bench_net(bench_x)
                torch_mod.cuda.synchronize()
                t = time.perf_counter()
                iters = 5
                with torch_mod.inference_mode():
                    for _ in range(iters):
                        _ = bench_net(bench_x)
                torch_mod.cuda.synchronize()
                ms_per = (time.perf_counter() - t) * 1000.0 / iters
                print(
                    f"[env] convnext_tiny_fp32_synthetic: {ms_per:.1f} ms/iter "
                    f"(expected ~10-30 on sm_80+, 30-80 on sm_120 with cuDNN 9.x)",
                    file=sys.stderr,
                    flush=True,
                )
                del bench_net, bench_x
                torch_mod.cuda.empty_cache()
            except Exception as exc:
                print(f"[env] convnext benchmark failed: {exc}", file=sys.stderr, flush=True)
    except Exception as exc:
        print(f"[env] dump failed: {exc}", file=sys.stderr, flush=True)


def _build_base(model_name: str) -> Any:
    """Construct an empty (no weights) ConvNeXt backbone matching `model_name`."""
    _, models, _ = _torch()
    if model_name == "convnext_tiny":
        return models.convnext_tiny(weights=None)
    if model_name == "convnext_small":
        return models.convnext_small(weights=None)
    if model_name == "convnext_base":
        return models.convnext_base(weights=None)
    if model_name == "convnext_large":
        return models.convnext_large(weights=None)
    raise LocalInferenceError(f"Unsupported model_mode: {model_name!r}")


def _replace_classifier(
    net: Any,
    num_classes: int,
    *,
    layout: str,
    dropout: float = 0.0,
) -> Any:
    """Swap the ConvNeXt classifier head to match the trained checkpoint.

    `layout` selects which classifier head shape to build:
      * "seq_at_2"    — Sequential(Dropout, Linear) at classifier[2]   (this app's trainer)
      * "linear_at_2" — bare Linear at classifier[2]                   (older checkpoints)
      * "seq_at_3"    — Sequential(LayerNorm, Flatten, Dropout, Linear) (newer checkpoints)
    """
    torch, _, _ = _torch()
    nn = torch.nn
    if layout == "linear_at_2":
        in_features = net.classifier[2].in_features
        net.classifier[2] = nn.Linear(in_features, num_classes)
        return net
    if layout == "seq_at_3":
        original_linear = net.classifier[2]
        in_features = original_linear.in_features
        net.classifier = nn.Sequential(
            net.classifier[0],  # LayerNorm
            net.classifier[1],  # Flatten
            nn.Dropout(p=dropout),
            nn.Linear(in_features, num_classes),
        )
        return net
    # default: seq_at_2
    in_features = net.classifier[2].in_features
    net.classifier[2] = nn.Sequential(
        nn.Dropout(p=dropout),
        nn.Linear(in_features, num_classes),
    )
    return net


def _detect_classifier_layout(state_dict: dict[str, Any]) -> str:
    """Inspect the checkpoint's state_dict keys to decide which head shape
    to build, so models trained by any vintage of the legacy app or our own
    trainer load correctly.
    """
    if "classifier.3.weight" in state_dict:
        return "seq_at_3"
    if "classifier.2.1.weight" in state_dict:
        return "seq_at_2"
    if "classifier.2.weight" in state_dict:
        return "linear_at_2"
    # Unknown — fall back to our trainer's layout; load_state_dict(strict=True)
    # below will raise loudly instead of silently keeping random head weights.
    return "seq_at_2"


def _load(model_path: str) -> _LoadedModel:
    """Load a checkpoint, move the model onto the cached device, cache it.

    Detects the classifier-head layout from the state_dict keys so
    checkpoints trained by either the legacy app (Linear at classifier[2],
    or Sequential at classifier[3]) or our own trainer (Sequential at
    classifier[2]) all load with the right head.
    """
    torch, _, _ = _torch()
    device = _device_cache
    path = Path(model_path)
    if not path.exists():
        raise LocalInferenceError(f"Model file missing: {model_path}")

    key = (str(path.resolve()), int(path.stat().st_mtime))
    with _lock:
        cached = _cache.get(key)
        if cached is not None:
            return cached

        # weights_only=True restricts unpickling to tensors + plain Python
        # containers/primitives, so a malicious .pth (community download or
        # imported ZIP) cannot execute code on load. Our checkpoints only carry
        # model_state_dict / classes / base / image_size, all of which load
        # under this mode. A checkpoint that carries a non-allowlisted type
        # fails closed (refuses to load) rather than running code; allowlist the
        # specific safe type with torch.serialization.add_safe_globals(...) only
        # if a real file needs it.
        ckpt = torch.load(str(path), map_location="cpu", weights_only=True)
        classes = list(ckpt.get("classes") or [])
        base = ckpt.get("base") or "convnext_tiny"
        if not classes:
            raise LocalInferenceError(f"{model_path}: no 'classes' in checkpoint")

        state = ckpt.get("model_state_dict") or ckpt.get("state_dict") or ckpt
        # Tolerate DataParallel-wrapped checkpoints and strip SWA bookkeeping.
        cleaned = {k.removeprefix("module."): v for k, v in state.items() if k != "n_averaged"}
        layout = _detect_classifier_layout(cleaned)
        dropout = float(ckpt.get("dropout", 0.0) or 0.0)

        net = _build_base(base)
        net = _replace_classifier(
            net,
            num_classes=len(classes),
            layout=layout,
            dropout=dropout,
        )
        # strict=True so a head/backbone mismatch raises here instead of
        # silently leaving random head weights (which produces garbage
        # predictions at confident-looking probabilities).
        net.load_state_dict(cleaned, strict=True)
        net.to(device).eval()

        # The trainer (both ours and the legacy app's) writes the image size it
        # trained at into the checkpoint when it can. Fall back to a
        # ConvNeXt-sensible default (224) only when nothing else is known —
        # the caller (classify()) gets to override this from the model's
        # training_config when the checkpoint is silent.
        image_size = int(ckpt.get("image_size") or 224)

        loaded = _LoadedModel(net=net, classes=classes, base=base, image_size=image_size)
        _cache[key] = loaded
        # Diagnostic: confirm where the model parameters live after load.
        import sys

        try:
            param = next(net.parameters())
            print(
                f"[model] loaded {base!r} from {path.name} "
                f"device={param.device} dtype={param.dtype} "
                f"classifier_layout={layout} image_size={image_size}",
                file=sys.stderr,
                flush=True,
            )
        except Exception:
            pass
        return loaded


def _get_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Lazily create the dedicated single-threaded inference executor.

    All classify() work — load, preprocess, forward, postprocess — runs
    on this one thread so cuDNN's per-thread state stays warm across
    consecutive calls.
    """
    global _executor
    if _executor is None:
        _executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-inference")
    return _executor


def classify(
    image_bgr: np.ndarray,
    model_path: str,
    *,
    image_size: int | None = None,
) -> tuple[str, float]:
    """Run a single image through the local model.

    Returns `(label, confidence_pct)` matching `api_client.classify`'s signature.
    `confidence_pct` is 0..100; -1 if the model produced no usable output.

    `image_size` lets the caller override the resize target — used to feed
    the model the resolution it was trained at (e.g. legacy-app ConvNeXts
    default to 480, our trainer defaults to 232). When None we fall back to
    whatever `_load()` picked from the checkpoint.

    Routed through `_get_executor()` so every call lands on the same
    thread, keeping cuDNN's per-thread algorithm cache warm. The caller
    blocks on the future; on a CPU-only build this is functionally
    identical to running the work inline.
    """
    return (
        _get_executor()
        .submit(
            _classify_impl,
            image_bgr,
            model_path,
            image_size,
        )
        .result()
    )


def _classify_impl(
    image_bgr: np.ndarray,
    model_path: str,
    image_size: int | None = None,
) -> tuple[str, float]:
    """Inner classify implementation. Always runs on the inference thread.

    Populates `local_inference.last_timings` with a per-step breakdown
    (`load`, `preprocess`, `forward`, `postprocess`, in ms) and mirrors
    that breakdown to stderr so the operator can read it from the console.
    """
    import sys

    timings: dict[str, float] = {}

    # _torch() lazily imports torch + torchvision and runs the env-dump
    # synthetic benchmarks on first call — that work is on the user-visible
    # critical path of the *first* classify(), so we time it here. After
    # the first call this is a fast cache-hit (<0.01 ms) and disappears
    # from the breakdown. Without this row the UI-shown predict_ms can be
    # several seconds higher than the [classify] log total on cold start.
    t0 = time.perf_counter()
    torch, _, F = _torch()
    timings["torch_init"] = (time.perf_counter() - t0) * 1000.0

    t0 = time.perf_counter()
    loaded = _load(model_path)
    timings["load"] = (time.perf_counter() - t0) * 1000.0

    import cv2

    # Use the device the model already lives on. _load moved it there
    # exactly once; no per-call .to(device) on the model and no fresh
    # device probe — that's exactly what the reference AI server does.
    device = _device_cache

    # Caller's override wins (typically `model.training_config.image_size`
    # so the input matches what the model was trained at); fall back to
    # whatever _load() picked from the checkpoint.
    effective_size = int(image_size) if image_size and image_size > 0 else loaded.image_size

    t0 = time.perf_counter()
    rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    resized = cv2.resize(rgb, (effective_size, effective_size), interpolation=cv2.INTER_AREA)
    arr = resized.astype(np.float32) / 255.0
    arr = (arr - np.array([0.485, 0.456, 0.406], dtype=np.float32)) / np.array([0.229, 0.224, 0.225], dtype=np.float32)
    tensor = torch.from_numpy(arr.transpose(2, 0, 1)).unsqueeze(0).to(device)
    timings["preprocess"] = (time.perf_counter() - t0) * 1000.0

    t0 = time.perf_counter()
    with torch.inference_mode():
        logits = loaded.net(tensor)
        if device.type == "cuda":
            torch.cuda.synchronize()
    timings["forward"] = (time.perf_counter() - t0) * 1000.0

    t0 = time.perf_counter()
    probs = F.softmax(logits, dim=-1).squeeze(0)
    top_idx = int(torch.argmax(probs).item())
    top_prob = float(probs[top_idx].item())
    timings["postprocess"] = (time.perf_counter() - t0) * 1000.0

    last_timings.clear()
    last_timings.update(timings)
    parts = " ".join(f"{k}:{v:.0f}" for k, v in timings.items())
    print(f"[classify] device={device.type} {parts}  total:{sum(timings.values()):.0f}ms", file=sys.stderr, flush=True)

    label = loaded.classes[top_idx] if 0 <= top_idx < len(loaded.classes) else ""
    return label, top_prob * 100.0


def current_device_label() -> str:
    """Short human-readable summary of where classify() will run."""
    try:
        _torch()
    except LocalInferenceError:
        return "n/a"
    return _device_cache.type if _device_cache is not None else "n/a"


def is_installed() -> bool:
    """Is torch present, *without* importing it?

    This is the check the install gate wants. `is_available()` fully imports
    torch and, on the first call, probes the device and runs the environment
    dump's synthetic benchmarks — seconds of work on a CUDA box. That is fine
    on the inference thread, where it's paid once and the user is already
    waiting on a classification, but it must never run on a button handler
    just to decide whether to offer the install: it would freeze the UI every
    time the user presses Start.

    `find_spec` answers the same question for free. A torch that is present
    but broken still gets caught downstream by `_torch()`, which raises
    `LocalInferenceError` as it always has.
    """
    import importlib.util

    # Same rationale as _torch(): a just-installed torch is invisible to a
    # stale finder cache.
    importlib.invalidate_caches()
    try:
        return importlib.util.find_spec("torch") is not None and importlib.util.find_spec("torchvision") is not None
    except (ImportError, ValueError):
        # ImportError: a parent package is missing. ValueError: the module is
        # in sys.modules with a None __spec__. Both mean "not usable".
        return False


def is_available() -> bool:
    """Does importing torch succeed?

    Imports torch as a side effect — see `is_installed()` for the cheap
    presence check to use from the UI thread.
    """
    try:
        _torch()
        return True
    except LocalInferenceError:
        return False


def clear_cache() -> None:
    """Drop the loaded-model cache.

    Resets the GPU-resident models so a subsequent call reloads from disk
    onto the cached device. The device choice itself is intentionally not
    reset — that would force another probe on the next classify().
    """
    with _lock:
        _cache.clear()
