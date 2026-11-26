"""A synthetic camera picture with switchable defects, for the camera health tests and the fake NX (tools/fake_nx.py).

scene(mode) -> JPEG bytes, 1280x720. Each call adds fresh sensor noise (like a real camera), except "frozen".
Modes: normal, blur, covered, dark, glare, moved, change, blocked, frozen, dusk (darker + a light gradient: must NOT
count as a problem), night (black-and-white infrared).
"""

import time

import cv2
import numpy as np

W, H = 1280, 720
MODES = ("normal", "blur", "covered", "dark", "glare", "moved", "change", "blocked", "frozen", "dusk", "night")
CHANGE_BOX = (820, 330, 300, 260)        # x, y, w, h of the object "change" puts in the scene (px)
BLOCKED_BOX = (0, 0, 400, 720)           # what "blocked" covers
_base: list = []
_frozen: list = []


def base() -> np.ndarray:
    if _base:
        return _base[0]
    rng = np.random.default_rng(7)
    img = np.zeros((H, W, 3), np.uint8)
    for y in range(H // 2):                                   # sky
        img[y, :] = (235 - y // 3, 200 - y // 5, 150 - y // 6)
    ground = rng.normal(0, 18, (H // 2, W)).astype(np.float32)
    ground = cv2.GaussianBlur(ground, (0, 0), 1.2)
    img[H // 2:] = np.clip(np.stack([70 + ground, 105 + ground, 80 + ground], -1), 0, 255).astype(np.uint8)
    for i, (x, w, h, col) in enumerate([(60, 220, 260, (90, 90, 160)), (330, 180, 330, (150, 140, 130)),
                                        (560, 260, 220, (60, 120, 170)), (900, 240, 300, (120, 100, 90))]):
        y0 = H // 2 + 60 - h
        cv2.rectangle(img, (x, y0), (x + w, H // 2 + 60), col, -1)
        for wy in range(y0 + 20, H // 2 + 40, 34):              # windows
            for wx in range(x + 16, x + w - 24, 30):
                cv2.rectangle(img, (wx, wy), (wx + 14, wy + 18), (40, 40, 40) if (wx + wy + i) % 3 else (210, 220, 230), -1)
    for x in range(0, W, 26):                                    # fence
        cv2.line(img, (x, H - 170), (x, H - 90), (200, 200, 200), 3)
    cv2.line(img, (0, H - 160), (W, H - 160), (200, 200, 200), 3)
    cv2.line(img, (0, H - 110), (W, H - 110), (200, 200, 200), 3)
    cv2.putText(img, "TWG LOT B", (980, H - 40), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (20, 20, 220), 4)
    _base.append(img)
    return img


def render(mode: str = "normal", seed: int | None = None) -> np.ndarray:
    rng = np.random.default_rng(seed if seed is not None else time.time_ns() % 2**32)
    img = base().astype(np.float32)
    if mode == "change":
        x, y, w, h = CHANGE_BOX
        box = rng.normal(0, 25, (h, w)).astype(np.float32)
        img[y:y + h, x:x + w] = np.clip(np.stack([60 + box, 140 + box, 200 + box], -1), 0, 255)
        cv2.rectangle(img, (x, y), (x + w, y + h), (20, 20, 20), 6)
        cv2.line(img, (x, y), (x + w, y + h), (20, 20, 20), 6)
    if mode == "moved":
        img = cv2.warpAffine(img, np.float32([[1, 0, 90], [0, 1, 40]]), (W, H), borderMode=cv2.BORDER_REFLECT)
    if mode == "blur":
        img = cv2.GaussianBlur(img, (0, 0), 6)
    if mode == "covered":
        img[:] = 92
    if mode == "blocked":
        x, y, w, h = BLOCKED_BOX
        img[y:y + h, x:x + w] = 38
    if mode == "dusk":                                           # darker, warmer, a lamp on the right
        ramp = np.linspace(0.45, 0.8, W, dtype=np.float32)[None, :, None]
        img = img * ramp
    img = img + rng.normal(0, 2.5, img.shape).astype(np.float32)  # sensor noise: never the same twice
    if mode == "dark":
        img *= 0.04
    if mode == "glare":
        img = img * 3.5 + 60
    img = np.clip(img, 0, 255).astype(np.uint8)
    if mode == "night":
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        img = cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)
    return img


def scene(mode: str = "normal", seed: int | None = None) -> bytes:
    if mode == "frozen":
        if not _frozen:
            _frozen.append(scene("normal", 1))
        return _frozen[0]
    ok, buf = cv2.imencode(".jpg", render(mode, seed), [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes()
