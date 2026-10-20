"""Camera health: the GPU checks (optional; app/camhealth/analyze.py runs the CPU checks without them).

Two small pretrained models on the card (well under 1 GB of the T1000's 4 GB):
  - DINOv2 ViT-S/14 (timm), half precision: a feature vector per 14 px patch. Comparing the current frame's patches
    with the background's tells real changes in the scene from lighting and noise, which fool pixel comparisons.
  - Faster R-CNN MobileNetV3 (torchvision, COCO): people and vehicles, so they never count as a scene change and are
    never taken into the background.

Tried and dropped (2026-10-20): CLIP zero-shot "dirty / wet lens / spider web" per crop flagged 13 of 29 healthy real
cameras (mostly "spider web" at 85-97 %). Lens defects need a model trained on real examples.

Self-contained (no app imports): the vision image runs `python ai.py --download` at build time, so the weights are
baked in and the worker never downloads anything (HF_HUB_OFFLINE=1).
"""

from __future__ import annotations

import os
import sys
import threading

import numpy as np

DINO = "vit_small_patch14_dinov2.lvd142m"
DINO_W = 518                       # input width, 37 patches of 14 px
PERSON_VEHICLE = {1: "person", 2: "bicycle", 3: "car", 4: "motorcycle", 6: "bus", 8: "truck"}   # COCO ids
OBJECT_MIN_SCORE = 0.5


def available() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:  # noqa: BLE001
        return False


def download() -> None:
    """Fetch every model's weights into the image (HF_HOME / TORCH_HOME)."""
    import timm
    import torchvision
    timm.create_model(DINO, pretrained=True)
    torchvision.models.detection.fasterrcnn_mobilenet_v3_large_fpn(weights="DEFAULT")
    print("models downloaded")


class AI:
    """Loaded once by the worker; every call is serialized (one card, small batches)."""

    def __init__(self, device: str = "cuda"):
        import timm
        import torch
        import torchvision
        self.torch = torch
        self.device = device
        self.lock = threading.Lock()
        self.half = device == "cuda"
        dt = torch.float16 if self.half else torch.float32
        self.dtype = dt
        self.dino = timm.create_model(DINO, pretrained=True, dynamic_img_size=True).eval().to(device, dt)
        self.det = torchvision.models.detection.fasterrcnn_mobilenet_v3_large_fpn(weights="DEFAULT").eval().to(device)
        self.mean = torch.tensor([0.485, 0.456, 0.406], device=device).view(1, 3, 1, 1)
        self.std = torch.tensor([0.229, 0.224, 0.225], device=device).view(1, 3, 1, 1)

    def _tensor(self, bgr: np.ndarray):
        t = self.torch.from_numpy(np.ascontiguousarray(bgr[:, :, ::-1])).to(self.device)
        return t.permute(2, 0, 1).unsqueeze(0).float() / 255.0          # 1x3xHxW RGB 0..1

    def patch_features(self, bgr: np.ndarray) -> np.ndarray:
        """L2-normalised DINOv2 features, shape (rows, cols, 384), one per 14 px patch at DINO_W wide."""
        torch = self.torch
        h, w = bgr.shape[:2]
        rows = max(1, round(DINO_W * h / w / 14))
        with self.lock, torch.no_grad():
            x = torch.nn.functional.interpolate(self._tensor(bgr), size=(rows * 14, DINO_W), mode="bilinear",
                                                antialias=True, align_corners=False)
            x = ((x - self.mean) / self.std).to(self.dtype)
            tokens = self.dino.forward_features(x)[:, self.dino.num_prefix_tokens:, :]
            f = tokens.reshape(rows, DINO_W // 14, -1).float()
            f = f / f.norm(dim=-1, keepdim=True).clamp_min(1e-6)
            return f.cpu().numpy()

    def objects(self, bgr: np.ndarray) -> list[dict]:
        """People and vehicles: [{label, score, box: [x, y, w, h] 0..1}]."""
        torch = self.torch
        h, w = bgr.shape[:2]
        with self.lock, torch.no_grad():
            out = self.det(self._tensor(bgr))[0]
        found = []
        for box, label, score in zip(out["boxes"].cpu().numpy(), out["labels"].cpu().numpy(), out["scores"].cpu().numpy()):
            if score >= OBJECT_MIN_SCORE and int(label) in PERSON_VEHICLE:
                x0, y0, x1, y1 = box
                found.append({"label": PERSON_VEHICLE[int(label)], "score": round(float(score), 2),
                              "box": [x0 / w, y0 / h, (x1 - x0) / w, (y1 - y0) / h]})
        return found


if __name__ == "__main__":
    if "--download" in sys.argv:
        download()
    else:
        print("cuda" if available() else "no cuda", os.environ.get("HF_HOME", ""))
