"""ConvNeXt training script.

Stripped-down trainer retaining ConvNeXt Tiny/Small/Base/Large only. ResNet,
Inception, and "custom" branches are removed. Parent classifications are
dropped (legacy feature out of scope).

Progress markers are emitted as JSON-line tokens on stdout for the
`training.manager` to parse:

    [PROGRESS] {"event": "start", "epochs": 15, "classes": 7, "images": 320}
    [PROGRESS] {"event": "epoch", "epoch": 1, "train_loss": 0.42, "train_acc": 0.87, ...}
    [PROGRESS] {"event": "done", "best_val_acc": 0.94}

Run as a subprocess via `manager.spawn(...)`. Can also be invoked directly:

    python -m sorter.training.train_convnext --image_dir=... --output_model=... --epochs=15

IMPORTANT: All Dataset subclasses must be defined at module scope (not
inside helper functions) — Windows multiprocessing `spawn` workers pickle
them when DataLoader spins up, and local classes can't be pickled.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import random
import sys
from typing import Any

# Torch is required at import time so the picklable classes below can be
# defined at module scope. The Train tab gates spawning this script on
# `local_inference.is_available()`, so by the time we run, torch is present.
try:
    # torch/torchvision are the optional `[ml]` extra — genuinely absent from
    # this dev/CI environment by design; the except below is exactly the
    # runtime guard for that (this script only ever runs once the Train tab
    # has confirmed torch is installed).
    import torch  # ty: ignore[unresolved-import]
    import torch.nn as nn  # ty: ignore[unresolved-import]
    import torch.nn.functional as F  # ty: ignore[unresolved-import]
    from PIL import Image
    from torch.optim.swa_utils import SWALR, AveragedModel, update_bn  # ty: ignore[unresolved-import]
    from torch.utils.data import DataLoader, Dataset, random_split  # ty: ignore[unresolved-import]
    from torchvision import models, transforms  # ty: ignore[unresolved-import]
except ImportError as exc:  # pragma: no cover - guarded by Train UI
    sys.stderr.write(f"[train_convnext] PyTorch is required. Install with the Train tab. ({exc})\n")
    raise


SUPPORTED_MODELS = ("convnext_tiny", "convnext_small", "convnext_base", "convnext_large")


def _emit(event: str, **payload: Any) -> None:
    """Write a single [PROGRESS] line to stdout. Always flushes."""
    payload["event"] = event
    sys.stdout.write("[PROGRESS] " + json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ConvNeXt trainer")
    p.add_argument("--image_dir", required=True)
    p.add_argument("--output_model", required=True)

    p.add_argument("--epochs", type=int, default=10)
    p.add_argument("--batch_size", type=int, default=32)
    p.add_argument("--lr", type=float, default=1e-4)
    p.add_argument("--weight_decay", type=float, default=1e-4)
    p.add_argument("--dropout", type=float, default=0.0)

    p.add_argument("--val_split", type=float, default=0.2)
    p.add_argument("--trainall", action="store_true")

    p.add_argument("--model_name", default="convnext_tiny", choices=SUPPORTED_MODELS)
    p.add_argument("--imgsize", type=int, default=232)
    p.add_argument("--freeze_backbone", action="store_true")
    p.add_argument("--max_workers", type=int, default=-1)

    p.add_argument("--stochastic_depth_prob", type=float, default=-1.0)
    p.add_argument("--use_focal_loss", action="store_true")
    p.add_argument("--focal_gamma", type=float, default=1.0)
    p.add_argument("--use_swa", action="store_true")
    p.add_argument("--swa_start", type=float, default=0.75)
    p.add_argument("--swa_mode", default="scheduled", choices=("scheduled", "adaptive"))
    p.add_argument("--swa_acc_threshold", type=float, default=0.96)
    p.add_argument("--swa_patience", type=int, default=5)
    p.add_argument("--swa_min_epoch", type=int, default=10)

    return p.parse_args()


# ----- Dataset (module-level so DataLoader workers can pickle) --------------


class FilenameLabelDataset(Dataset):
    """Reads images from a flat directory; label = filename prefix before '__'."""

    def __init__(self, image_dir: str) -> None:
        self.image_dir = image_dir
        self.image_paths: list[str] = []
        labels_str: list[str] = []
        for fname in os.listdir(image_dir):
            if "__" not in fname:
                continue
            if not fname.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".webp")):
                continue
            self.image_paths.append(os.path.join(image_dir, fname))
            labels_str.append(fname.split("__", 1)[0])
        if not self.image_paths:
            raise RuntimeError(f"No valid images found in {image_dir}")
        self.classes = sorted(set(labels_str))
        self.class_to_idx = {c: i for i, c in enumerate(self.classes)}
        self.labels = [self.class_to_idx[lbl] for lbl in labels_str]

    def __len__(self) -> int:
        return len(self.image_paths)

    def __getitem__(self, idx: int):
        img = Image.open(self.image_paths[idx]).convert("RGB")
        return img, self.labels[idx]


class TransformSubset(Dataset):
    """Apply a torchvision transform pipeline to a (random) Subset on the fly."""

    def __init__(self, subset, transform) -> None:
        self.subset = subset
        self.transform = transform

    def __len__(self) -> int:
        return len(self.subset)

    def __getitem__(self, idx: int):
        img, lbl = self.subset[idx]
        return self.transform(img), lbl


class FocalLoss(nn.Module):
    """Single-class focal loss with label smoothing."""

    def __init__(self, gamma: float = 1.0, alpha: float = 1.0, label_smoothing: float = 0.1) -> None:
        super().__init__()
        self.gamma = gamma
        self.alpha = alpha
        self.label_smoothing = label_smoothing

    def forward(self, inputs, targets):
        ce = F.cross_entropy(inputs, targets, reduction="none", label_smoothing=self.label_smoothing)
        log_pt = F.log_softmax(inputs, dim=-1)
        pt = torch.exp(log_pt.gather(1, targets.unsqueeze(1)).squeeze())
        return (self.alpha * (1 - pt) ** self.gamma * ce).mean()


def _get_transforms(image_size: int):
    train_tf = transforms.Compose(
        [
            transforms.Resize((image_size, image_size)),
            transforms.RandomRotation(degrees=(0, 360)),
            transforms.RandomAffine(degrees=0, translate=(0.1, 0.1), scale=(0.9, 1.1)),
            transforms.GaussianBlur(kernel_size=(3, 3), sigma=(0.1, 2.0)),
            transforms.ColorJitter(brightness=0.3, contrast=0.3, saturation=0.2),
            transforms.ToTensor(),
            transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ]
    )
    val_tf = transforms.Compose(
        [
            transforms.Resize((image_size, image_size)),
            transforms.ToTensor(),
            transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ]
    )
    return train_tf, val_tf


def _get_model_weights(model_name: str):
    weights_map = {
        "convnext_tiny": models.ConvNeXt_Tiny_Weights.IMAGENET1K_V1,
        "convnext_small": models.ConvNeXt_Small_Weights.IMAGENET1K_V1,
        "convnext_base": models.ConvNeXt_Base_Weights.IMAGENET1K_V1,
        "convnext_large": models.ConvNeXt_Large_Weights.IMAGENET1K_V1,
    }
    return getattr(models, model_name), weights_map[model_name]


def _run_epoch(
    model, loader, criterion, optimizer, scaler, device, is_train: bool, use_channels_last: bool
) -> tuple[float, float]:
    if is_train:
        model.train()
    else:
        model.eval()
    running_loss = 0.0
    correct = 0
    total = 0
    with torch.set_grad_enabled(is_train):
        for imgs, labels in loader:
            imgs = imgs.to(device)
            labels = labels.to(device)
            if use_channels_last:
                imgs = imgs.to(memory_format=torch.channels_last)
            with torch.amp.autocast(device_type="cuda", enabled=(device.type == "cuda")):
                outputs = model(imgs)
                loss = criterion(outputs, labels)
            if is_train:
                scaler.scale(loss).backward()
                scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=2.0)
                scaler.step(optimizer)
                scaler.update()
                optimizer.zero_grad()
            running_loss += loss.item() * imgs.size(0)
            correct += (outputs.argmax(1) == labels).sum().item()
            total += labels.size(0)
    return running_loss / max(total, 1), correct / max(total, 1)


def main() -> int:
    args = parse_args()

    torch.backends.cudnn.benchmark = True
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    random.seed(42)
    torch.manual_seed(42)

    model_ctor, model_weights = _get_model_weights(args.model_name)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    dataset = FilenameLabelDataset(args.image_dir)
    _emit(
        "start",
        epochs=args.epochs,
        classes=len(dataset.classes),
        images=len(dataset),
        device=device.type,
        model=args.model_name,
    )

    val_split = float(args.val_split or 0.0)
    if not (0.0 <= val_split < 1.0):
        raise ValueError("--val_split must be in [0.0, 1.0).")
    n_val = int(len(dataset) * val_split)
    n_train = len(dataset) - n_val

    if n_val == 0 or args.trainall:
        train_sub = dataset
        val_sub = None
    else:
        train_sub, val_sub = random_split(
            dataset,
            [n_train, n_val],
            generator=torch.Generator().manual_seed(42),
        )

    train_tf, val_tf = _get_transforms(args.imgsize)

    num_workers = args.max_workers
    if num_workers < 0:
        cpu = os.cpu_count() or 1
        # On Windows, multiprocessing.spawn pays a steep per-worker cost
        # because every worker re-imports the script + torch. Keep the cap
        # conservative on Windows; Linux can handle more.
        if sys.platform == "win32":
            num_workers = min(2, max(0, cpu - 1))
        else:
            num_workers = min(8, max(1, cpu - 1))
    loader_args = {
        "batch_size": args.batch_size,
        "num_workers": num_workers,
        "pin_memory": True,
        "persistent_workers": num_workers > 0,
        "prefetch_factor": 2 if num_workers > 0 else None,
    }

    train_loader = DataLoader(TransformSubset(train_sub, train_tf), shuffle=True, **loader_args)
    val_loader = None
    if val_sub is not None:
        val_loader = DataLoader(TransformSubset(val_sub, val_tf), shuffle=False, **loader_args)

    if args.stochastic_depth_prob >= 0:
        model = model_ctor(weights=model_weights, stochastic_depth_prob=args.stochastic_depth_prob)
    else:
        model = model_ctor(weights=model_weights)
    in_features = model.classifier[2].in_features
    model.classifier[2] = nn.Sequential(
        nn.Dropout(p=args.dropout),
        nn.Linear(in_features, len(dataset.classes)),
    )
    model.to(device)

    use_channels_last = False
    if device.type == "cuda" and torch.cuda.get_device_capability(0)[0] >= 7:
        model = model.to(memory_format=torch.channels_last)
        use_channels_last = True

    if args.freeze_backbone:
        for name, param in model.named_parameters():
            if not name.startswith("classifier"):
                param.requires_grad = False

    optimizer = torch.optim.AdamW(
        [p for p in model.parameters() if p.requires_grad],
        lr=args.lr,
        weight_decay=args.weight_decay,
    )
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs)
    if args.use_focal_loss:
        criterion = FocalLoss(gamma=args.focal_gamma, label_smoothing=0.1)
    else:
        criterion = nn.CrossEntropyLoss(label_smoothing=0.1)

    scaler = torch.amp.GradScaler(enabled=(device.type == "cuda"))

    swa_model = None
    swa_scheduler = None
    swa_active = False
    swa_fallback_epoch = int(args.epochs * args.swa_start)
    if args.use_swa:
        swa_model = AveragedModel(model)
        swa_scheduler = SWALR(optimizer, swa_lr=args.lr * 0.1)

    best_acc = -1.0
    best_loss = float("inf")
    best_plateau_loss = float("inf")
    epochs_no_improve = 0

    for epoch in range(1, args.epochs + 1):
        train_loss, train_acc = _run_epoch(
            model,
            train_loader,
            criterion,
            optimizer,
            scaler,
            device,
            is_train=True,
            use_channels_last=use_channels_last,
        )
        val_loss: float | None = None
        val_acc: float | None = None
        if val_loader is not None:
            val_loss, val_acc = _run_epoch(
                model,
                val_loader,
                criterion,
                None,
                None,
                device,
                is_train=False,
                use_channels_last=use_channels_last,
            )
            if val_loss < best_plateau_loss:
                best_plateau_loss = val_loss
                epochs_no_improve = 0
            else:
                epochs_no_improve += 1

        if args.use_swa and not swa_active:
            should_start = False
            if args.swa_mode == "scheduled":
                should_start = epoch >= swa_fallback_epoch
            else:
                hit_threshold = val_acc is not None and val_acc >= args.swa_acc_threshold
                plateaued = epochs_no_improve >= args.swa_patience and epoch >= args.swa_min_epoch
                fallback = epoch >= swa_fallback_epoch
                should_start = hit_threshold or plateaued or fallback
            if should_start:
                swa_active = True

        if args.use_swa and swa_active:
            # `swa_active` only ever flips True inside `if args.use_swa`
            # above, which is exactly where `swa_model`/`swa_scheduler` were
            # constructed — so both are always set here.
            if swa_model is None or swa_scheduler is None:
                raise RuntimeError("SWA active but swa_model/swa_scheduler were never initialized")
            swa_model.update_parameters(model)
            swa_scheduler.step()
            current_lr = swa_scheduler.get_last_lr()[0]
        else:
            scheduler.step()
            current_lr = scheduler.get_last_lr()[0]

        save_payload = {
            "model_state_dict": model.state_dict(),
            "classes": dataset.classes,
            "base": args.model_name,
            # Record the resize target so inference uses the same scale
            # the model was trained at (default 232; user can override).
            "image_size": int(args.imgsize),
        }
        # val_acc and val_loss are always assigned together from the same
        # `_run_epoch(...)` call above (both stay None when there's no
        # val_loader) — folding both into the guard lets the checker narrow
        # `val_loss` to `float` alongside `val_acc` below.
        if val_acc is None or val_loss is None:
            torch.save(save_payload, args.output_model)
            saved_reason = "no-validation"
        else:
            save_payload["val_acc"] = val_acc
            save_payload["val_loss"] = val_loss
            if val_acc > best_acc or (val_acc == best_acc and val_loss < best_loss):
                best_acc = val_acc
                best_loss = val_loss
                torch.save(save_payload, args.output_model)
                saved_reason = "new-best"
            else:
                saved_reason = "skipped"

        _emit(
            "epoch",
            epoch=epoch,
            total=args.epochs,
            train_loss=float(train_loss),
            train_acc=float(train_acc),
            val_loss=(float(val_loss) if val_loss is not None else None),
            val_acc=(float(val_acc) if val_acc is not None else None),
            lr=float(current_lr),
            swa_active=bool(swa_active),
            saved=saved_reason,
        )

    if args.use_swa and swa_active:
        # Same invariant as above: `swa_active` can only be True here because
        # `swa_model` was constructed under `if args.use_swa`.
        if swa_model is None:
            raise RuntimeError("SWA active but swa_model was never initialized")
        update_bn(train_loader, swa_model, device=device)
        swa_path = args.output_model.replace(".pth", "_swa.pth")
        torch.save(
            {
                "model_state_dict": swa_model.state_dict(),
                "classes": dataset.classes,
                "base": args.model_name,
                "image_size": int(args.imgsize),
            },
            swa_path,
        )

    _emit("done", best_val_acc=best_acc if best_acc >= 0 else None, best_val_loss=best_loss)
    return 0


if __name__ == "__main__":
    # `freeze_support()` is a no-op outside frozen executables but is required
    # for Windows multiprocessing.spawn to work correctly if this script were
    # ever bundled with PyInstaller. Cheap insurance.
    multiprocessing.freeze_support()
    raise SystemExit(main())
