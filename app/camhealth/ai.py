"""Camera health: the GPU checks (optional; app/camhealth/analyze.py runs the CPU checks without them).

Three pretrained models on the card (about 1 GB of the T1000's 4 GB):
  - UperNet + ConvNeXt-Base (OpenMMLab, ADE20K 150 classes, MIT licence; HF transformers), FULL precision (in half
    precision on the T1000 it labelled everything "wall" and ran 5x slower, 2026-10-20; ~360 ms per frame): what every
    pixel is (grass, road, wall, tree, box, sky...). Shadow on grass is still grass, so a scene change has to change
    what's there, not how it's lit (2026-10-20: moving sun shadows were the main false alarm).
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
import time
from collections import deque

import numpy as np

DINO = "vit_small_patch14_dinov2.lvd142m"
DINO_W = 518                       # input width, 37 patches of 14 px
SEG = "openmmlab/upernet-convnext-base"
SEG_W = 768                        # input width (multiple of 32)
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
    from transformers import UperNetForSemanticSegmentation
    UperNetForSemanticSegmentation.from_pretrained(SEG)
    torchvision.models.detection.fasterrcnn_mobilenet_v3_large_fpn(weights="DEFAULT")
    print("models downloaded")


def gpu_stats() -> dict:
    """The card's live numbers (NVML: utilisation, memory, temperature, power, clocks); {} without one."""
    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        util = pynvml.nvmlDeviceGetUtilizationRates(h)
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        out = {"name": pynvml.nvmlDeviceGetName(h), "driver": pynvml.nvmlSystemGetDriverVersion(),
               "util": util.gpu, "mem_util": util.memory, "mem_used_mb": mem.used // 2**20, "mem_total_mb": mem.total // 2**20,
               "temp_c": pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)}
        for key, fn in (("power_w", lambda: pynvml.nvmlDeviceGetPowerUsage(h) / 1000),
                        ("power_limit_w", lambda: pynvml.nvmlDeviceGetEnforcedPowerLimit(h) / 1000),
                        ("clock_mhz", lambda: pynvml.nvmlDeviceGetClockInfo(h, pynvml.NVML_CLOCK_GRAPHICS)),
                        ("fan", lambda: pynvml.nvmlDeviceGetFanSpeed(h))):
            try:
                out[key] = round(fn(), 1)
            except Exception:  # noqa: BLE001 - not every card reports every number
                pass
        return out
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:200]}


class AI:
    """Loaded once by the worker; every call is serialized (one card, small batches) and timed (`timings`)."""

    MODELS = {"dino": "DINOv2 ViT-S/14", "segment": "UperNet ConvNeXt-B (ADE20K)", "objects": "Faster R-CNN MobileNetV3"}

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
        from transformers import UperNetForSemanticSegmentation
        self.seg = UperNetForSemanticSegmentation.from_pretrained(SEG).eval().to(device)      # fp32: see the top
        self.seg_labels = [self.seg.config.id2label[i] for i in range(len(self.seg.config.id2label))]
        self.timings: deque = deque(maxlen=5000)           # (model, ms, time) per call
        self.mean = torch.tensor([0.485, 0.456, 0.406], device=device).view(1, 3, 1, 1)
        self.std = torch.tensor([0.229, 0.224, 0.225], device=device).view(1, 3, 1, 1)

    def _tensor(self, bgr: np.ndarray):
        t = self.torch.from_numpy(np.ascontiguousarray(bgr[:, :, ::-1])).to(self.device)
        return t.permute(2, 0, 1).unsqueeze(0).float() / 255.0          # 1x3xHxW RGB 0..1

    def _timed(self, name: str, fn, *args):
        t = time.perf_counter()
        try:
            return fn(*args)
        finally:
            self.timings.append((name, (time.perf_counter() - t) * 1000, time.time()))

    def patch_features(self, bgr: np.ndarray) -> np.ndarray:
        return self._timed("dino", self._patch_features, bgr)

    def segment(self, bgr: np.ndarray) -> np.ndarray:
        return self._timed("segment", self._segment, bgr)

    def objects(self, bgr: np.ndarray) -> list[dict]:
        return self._timed("objects", self._objects, bgr)

    def _patch_features(self, bgr: np.ndarray) -> np.ndarray:
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

    def _segment(self, bgr: np.ndarray) -> np.ndarray:
        """ADE20K class per pixel (uint8), SEG_W wide."""
        torch = self.torch
        h, w = bgr.shape[:2]
        rows = max(32, round(SEG_W * h / w / 32) * 32)
        with self.lock, torch.no_grad():
            x = torch.nn.functional.interpolate(self._tensor(bgr), size=(rows, SEG_W), mode="bilinear", antialias=True,
                                                align_corners=False)
            logits = self.seg(pixel_values=(x - self.mean) / self.std).logits
            out = logits.argmax(1)[0].to(torch.uint8).cpu().numpy()
            del logits, x
            torch.cuda.empty_cache()           # 4 GB card: give the big activations back at once
            return out

    def _objects(self, bgr: np.ndarray) -> list[dict]:
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
