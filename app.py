import ctypes
import json
import math
import os
import shutil
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageGrab, ImageTk
from pynput import keyboard
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_NAME = "SnipClicker"
LEGACY_APP_NAME = "Clicker"
DATA_DIR = Path(os.getenv("APPDATA", Path.home())) / APP_NAME
LEGACY_DATA_DIR = Path(os.getenv("APPDATA", Path.home())) / LEGACY_APP_NAME
TARGET_DIR = DATA_DIR / "targets"
DIAGNOSTIC_DIR = DATA_DIR / "diagnostics"
CONFIG_PATH = DATA_DIR / "targets.json"
SETTINGS_PATH = DATA_DIR / "settings.json"
LOG_PATH = DATA_DIR / "snipclicker.log"
SCAN_INTERVAL_MS = 250
DISAPPEAR_MARGIN = 0.08
MOUSE_MOVE_TOLERANCE_PX = 2
COARSE_AREA_THRESHOLD = 250_000
COARSE_SCALE = 0.5
SCALES = (0.80, 0.90, 1.00, 1.10, 1.20)
DEFAULT_HOTKEY = ("f8",)
GA_ROOT = 2
CLICK_TYPES = ("Left", "Double", "Right")
MAIN_PANEL_PADDING = 10
MATCH_AREA_PADDING = 8
CLICK_LOCATION_PADDING = 8
REPEAT_DIAGNOSTIC_THRESHOLD = 5
COLOR_VERIFY_THRESHOLD = 0.72
COLOR_BG = "#0f141b"
COLOR_PANEL = "#151c24"
COLOR_PANEL_2 = "#111820"
COLOR_BORDER = "#263241"
COLOR_TEXT = "#e5edf7"
COLOR_MUTED = "#9aa7b7"
COLOR_ACCENT = "#2d8cff"
COLOR_ACCENT_DARK = "#173b69"
COLOR_DANGER = "#ef4444"
COLOR_SUCCESS = "#22c55e"


def rounded_rect(
    canvas: tk.Canvas,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    radius: int,
    **kwargs: object,
) -> int:
    points = [
        x1 + radius, y1,
        x2 - radius, y1,
        x2, y1,
        x2, y1 + radius,
        x2, y2 - radius,
        x2, y2,
        x2 - radius, y2,
        x1 + radius, y2,
        x1, y2,
        x1, y2 - radius,
        x1, y1 + radius,
        x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=10, **kwargs)


user32 = ctypes.windll.user32


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class POINT(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_long),
        ("y", ctypes.c_long),
    ]


@dataclass
class WindowAnchor:
    hwnd: int
    rect: tuple[int, int, int, int]
    title: str
    class_name: str

    @classmethod
    def from_dict(cls, raw: dict) -> Optional["WindowAnchor"]:
        try:
            return cls(
                hwnd=int(raw.get("hwnd") or 0),
                rect=tuple(raw["rect"]),
                title=str(raw.get("title", "")),
                class_name=str(raw.get("class_name", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def to_dict(self) -> dict:
        return {
            "hwnd": self.hwnd,
            "rect": list(self.rect),
            "title": self.title,
            "class_name": self.class_name,
        }


@dataclass
class Target:
    id: str
    name: str
    image_path: str
    enabled: bool = True
    threshold: float = 0.85
    click_type: str = "Left"
    click_point: Optional[tuple[float, float]] = None
    multi_scale: bool = False
    search_region: Optional[tuple[int, int, int, int]] = None
    search_region_relative: Optional[tuple[int, int, int, int]] = None
    search_window_title: str = ""
    search_window_class: str = ""
    search_window_handle: Optional[int] = None
    last_confidence: float = 0.0
    last_status: str = "Idle"
    visible: bool = False
    last_center: Optional[tuple[int, int]] = None
    repeat_click_count: int = 0
    repeat_diagnostic_reported: bool = False
    template_cache: list[tuple[float, np.ndarray]] = field(default_factory=list, repr=False)
    template_cache_mode: Optional[bool] = field(default=None, repr=False)
    color_template_cache: Optional[np.ndarray] = field(default=None, repr=False)

    @classmethod
    def from_dict(cls, raw: dict) -> "Target":
        return cls(
            id=raw["id"],
            name=raw["name"],
            image_path=raw["image_path"],
            enabled=bool(raw.get("enabled", True)),
            threshold=float(raw.get("threshold", 0.85)),
            click_type=str(raw.get("click_type", "Left")) if raw.get("click_type", "Left") in CLICK_TYPES else "Left",
            click_point=tuple(raw["click_point"]) if raw.get("click_point") else None,
            multi_scale=bool(raw.get("multi_scale", False)),
            search_region=tuple(raw["search_region"]) if raw.get("search_region") else None,
            search_region_relative=tuple(raw["search_region_relative"]) if raw.get("search_region_relative") else None,
            search_window_title=str(raw.get("search_window_title", "")),
            search_window_class=str(raw.get("search_window_class", "")),
            search_window_handle=raw.get("search_window_handle"),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "image_path": self.image_path,
            "enabled": self.enabled,
            "threshold": self.threshold,
            "click_type": self.click_type,
            "click_point": list(self.click_point) if self.click_point else None,
            "multi_scale": self.multi_scale,
            "search_region": list(self.search_region) if self.search_region else None,
            "search_region_relative": list(self.search_region_relative) if self.search_region_relative else None,
            "search_window_title": self.search_window_title,
            "search_window_class": self.search_window_class,
            "search_window_handle": self.search_window_handle,
        }


def ensure_dirs() -> None:
    if not DATA_DIR.exists() and LEGACY_DATA_DIR.exists():
        shutil.copytree(LEGACY_DATA_DIR, DATA_DIR)
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    DIAGNOSTIC_DIR.mkdir(parents=True, exist_ok=True)


def log_event(message: str) -> None:
    ensure_dirs()
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as log_file:
        log_file.write(f"[{timestamp}] {message}\n")


def load_targets() -> list[Target]:
    ensure_dirs()
    if not CONFIG_PATH.exists():
        return []
    try:
        raw_targets = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return [Target.from_dict(item) for item in raw_targets]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        backup = CONFIG_PATH.with_suffix(f".bad-{int(time.time())}.json")
        shutil.copy2(CONFIG_PATH, backup)
        return []


def save_targets(targets: list[Target]) -> None:
    ensure_dirs()
    payload = [target.to_dict() for target in targets]
    CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_settings() -> dict:
    ensure_dirs()
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError, ValueError):
        backup = SETTINGS_PATH.with_suffix(f".bad-{int(time.time())}.json")
        shutil.copy2(SETTINGS_PATH, backup)
        return {}


def save_settings(settings: dict) -> None:
    ensure_dirs()
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def load_bound_window() -> Optional[WindowAnchor]:
    raw = load_settings()
    try:
        bound = raw.get("bound_window")
        return WindowAnchor.from_dict(bound) if bound else None
    except (TypeError, ValueError):
        return None


def save_bound_window(bound_window: Optional[WindowAnchor]) -> None:
    settings = load_settings()
    settings["bound_window"] = bound_window.to_dict() if bound_window else None
    save_settings(settings)


def load_hotkey() -> tuple[str, ...]:
    raw = load_settings()
    hotkey = raw.get("hotkey")
    if isinstance(hotkey, list):
        normalized = tuple(key for key in hotkey if isinstance(key, str) and key)
        if normalized:
            return normalized
    return DEFAULT_HOTKEY


def save_hotkey(hotkey: tuple[str, ...]) -> None:
    settings = load_settings()
    settings["hotkey"] = list(hotkey)
    save_settings(settings)


def active_window_rect(excluded_hwnd: int) -> Optional[tuple[int, int, int, int]]:
    hwnd = user32.GetForegroundWindow()
    if not hwnd or hwnd == excluded_hwnd:
        return None

    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None

    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None

    return rect.left, rect.top, rect.right, rect.bottom


def hwnd_rect(hwnd: int) -> Optional[tuple[int, int, int, int]]:
    rect = RECT()
    if not hwnd or not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None

    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def window_class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def window_anchor_from_hwnd(hwnd: int) -> Optional[WindowAnchor]:
    root_hwnd = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
    rect = hwnd_rect(root_hwnd)
    if not rect:
        return None
    return WindowAnchor(
        hwnd=int(root_hwnd),
        rect=rect,
        title=window_title(root_hwnd),
        class_name=window_class_name(root_hwnd),
    )


def window_anchor_at_point(x: int, y: int) -> Optional[WindowAnchor]:
    point = POINT(int(x), int(y))
    hwnd = user32.WindowFromPoint(point)
    if not hwnd:
        return None
    return window_anchor_from_hwnd(hwnd)


def target_window_matches(target: Target, hwnd: int) -> bool:
    if target.search_window_title and window_title(hwnd) != target.search_window_title:
        return False
    if target.search_window_class and window_class_name(hwnd) != target.search_window_class:
        return False
    return bool(target.search_window_title or target.search_window_class)


def find_window_rect_for_target(target: Target) -> Optional[tuple[int, int, int, int]]:
    if target.search_window_handle and user32.IsWindow(int(target.search_window_handle)):
        hwnd = int(target.search_window_handle)
        if target_window_matches(target, hwnd):
            rect = hwnd_rect(hwnd)
            if rect:
                return rect

    if not (target.search_window_title or target.search_window_class):
        return None

    found: list[int] = []
    enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def enum_proc(hwnd: int, _: int) -> bool:
        if user32.IsWindowVisible(hwnd) and target_window_matches(target, hwnd):
            found.append(int(hwnd))
            return False
        return True

    user32.EnumWindows(enum_proc_type(enum_proc), 0)
    if not found:
        return None

    target.search_window_handle = found[0]
    return hwnd_rect(found[0])


def find_window_rect_for_anchor(anchor: WindowAnchor) -> Optional[tuple[int, int, int, int]]:
    temp_target = Target(
        id="",
        name="",
        image_path="",
        search_window_title=anchor.title,
        search_window_class=anchor.class_name,
        search_window_handle=anchor.hwnd,
    )
    return find_window_rect_for_target(temp_target)


def region_relative_to_window(
    region: tuple[int, int, int, int],
    window_rect: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    return (
        region[0] - window_rect[0],
        region[1] - window_rect[1],
        region[2] - window_rect[0],
        region[3] - window_rect[1],
    )


def region_absolute_from_window(
    relative_region: tuple[int, int, int, int],
    window_rect: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    return (
        window_rect[0] + relative_region[0],
        window_rect[1] + relative_region[1],
        window_rect[0] + relative_region[2],
        window_rect[1] + relative_region[3],
    )


def expand_capture_rect(rect: tuple[int, int, int, int], padding: int = MATCH_AREA_PADDING) -> tuple[int, int, int, int]:
    screen_left = user32.GetSystemMetrics(76)
    screen_top = user32.GetSystemMetrics(77)
    screen_width = user32.GetSystemMetrics(78)
    screen_height = user32.GetSystemMetrics(79)
    screen_right = screen_left + screen_width
    screen_bottom = screen_top + screen_height
    return (
        max(screen_left, rect[0] - padding),
        max(screen_top, rect[1] - padding),
        min(screen_right, rect[2] + padding),
        min(screen_bottom, rect[3] + padding),
    )


def virtual_screen_rect() -> tuple[int, int, int, int]:
    left = user32.GetSystemMetrics(76)
    top = user32.GetSystemMetrics(77)
    width = user32.GetSystemMetrics(78)
    height = user32.GetSystemMetrics(79)
    return left, top, left + width, top + height


def cursor_position() -> tuple[int, int]:
    point = POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def colorref(hex_color: str) -> int:
    hex_value = hex_color.lstrip("#")
    red = int(hex_value[0:2], 16)
    green = int(hex_value[2:4], 16)
    blue = int(hex_value[4:6], 16)
    return red | (green << 8) | (blue << 16)


def apply_window_chrome_style(root: tk.Tk) -> None:
    if os.name != "nt":
        return
    try:
        root.update_idletasks()
        hwnd_value = user32.GetAncestor(int(root.winfo_id()), GA_ROOT) or int(root.winfo_id())
        hwnd = ctypes.c_void_p(hwnd_value)
        dark_mode = ctypes.c_int(1)
        caption_color = ctypes.c_int(colorref(COLOR_BG))
        text_color = ctypes.c_int(colorref(COLOR_TEXT))
        dwmapi = ctypes.windll.dwmapi
        for attr in (20, 19):
            dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(dark_mode), ctypes.sizeof(dark_mode))
        dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption_color), ctypes.sizeof(caption_color))
        dwmapi.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text_color), ctypes.sizeof(text_color))
    except Exception:
        return


def apply_dialog_chrome_style(root: tk.Tk) -> None:
    apply_window_chrome_style(root)
    root.after(80, lambda: apply_window_chrome_style(root))


def normalize_hotkey_key(key: keyboard.Key | keyboard.KeyCode | None) -> Optional[str]:
    if key is None:
        return None
    if isinstance(key, keyboard.KeyCode):
        if key.char:
            return key.char.lower()
        if key.vk:
            return f"vk{key.vk}"
        return None
    name = str(key).replace("Key.", "").lower()
    aliases = {
        "ctrl_l": "ctrl",
        "ctrl_r": "ctrl",
        "ctrl": "ctrl",
        "alt_l": "alt",
        "alt_r": "alt",
        "alt_gr": "alt",
        "shift_l": "shift",
        "shift_r": "shift",
        "shift": "shift",
        "cmd_l": "win",
        "cmd_r": "win",
        "cmd": "win",
        "esc": "escape",
        "del": "delete",
    }
    return aliases.get(name, name)


def hotkey_display(hotkey: tuple[str, ...]) -> str:
    labels = {
        "ctrl": "Ctrl",
        "alt": "Alt",
        "shift": "Shift",
        "win": "Win",
        "delete": "Delete",
        "escape": "Esc",
        "space": "Space",
        "enter": "Enter",
        "tab": "Tab",
    }
    ordered = sorted(
        hotkey,
        key=lambda key: {"ctrl": 0, "alt": 1, "shift": 2, "win": 3}.get(key, 10),
    )
    return " + ".join(labels.get(key, key.upper() if len(key) == 1 else key.title()) for key in ordered)


def safe_filename(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value.strip())
    return cleaned.strip("_") or "target"


def click_point(x: int, y: int, click_type: str = "Left") -> None:
    original_pos = cursor_position()
    user32.SetCursorPos(int(x), int(y))
    if click_type == "Right":
        user32.mouse_event(0x0008, 0, 0, 0, 0)
        user32.mouse_event(0x0010, 0, 0, 0, 0)
        user32.SetCursorPos(int(original_pos[0]), int(original_pos[1]))
        return

    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    if click_type == "Double":
        time.sleep(0.05)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
    user32.SetCursorPos(int(original_pos[0]), int(original_pos[1]))


def click_point_for_match(
    capture_rect: tuple[int, int, int, int],
    match_box: tuple[int, int, int, int],
    target: Target,
) -> tuple[int, int]:
    x, y, width, height = match_box
    x_ratio, y_ratio = target.click_point or (0.5, 0.5)
    return (
        int(capture_rect[0] + x + width * x_ratio),
        int(capture_rect[1] + y + height * y_ratio),
    )


def pil_to_gray(image: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2GRAY)


def build_template_cache(target: Target) -> None:
    if target.template_cache and target.template_cache_mode == target.multi_scale:
        return

    source = cv2.imread(target.image_path, cv2.IMREAD_GRAYSCALE)
    if source is None:
        target.template_cache = []
        target.template_cache_mode = target.multi_scale
        return

    cache = []
    scales = SCALES if target.multi_scale else (1.00,)
    for scale in scales:
        width = max(1, int(source.shape[1] * scale))
        height = max(1, int(source.shape[0] * scale))
        resized = cv2.resize(source, (width, height), interpolation=cv2.INTER_AREA)
        cache.append((scale, resized))
    target.template_cache = cache
    target.template_cache_mode = target.multi_scale


def load_color_template(target: Target) -> Optional[np.ndarray]:
    if target.color_template_cache is not None:
        return target.color_template_cache
    try:
        target.color_template_cache = np.array(Image.open(target.image_path).convert("RGB"))
    except OSError:
        target.color_template_cache = None
    return target.color_template_cache


def color_match_similarity(capture_image: Image.Image, target: Target, match_box: tuple[int, int, int, int]) -> float:
    template = load_color_template(target)
    if template is None:
        return 0.0
    x, y, width, height = match_box
    if width <= 0 or height <= 0:
        return 0.0
    crop = capture_image.crop((x, y, x + width, y + height)).convert("RGB")
    if crop.size != (template.shape[1], template.shape[0]):
        crop = crop.resize((template.shape[1], template.shape[0]), Image.Resampling.BILINEAR)
    crop_array = np.array(crop)
    corner_pixels = np.concatenate(
        [
            template[:2, :2].reshape(-1, 3),
            template[:2, -2:].reshape(-1, 3),
            template[-2:, :2].reshape(-1, 3),
            template[-2:, -2:].reshape(-1, 3),
        ]
    )
    background = np.median(corner_pixels, axis=0)
    foreground_mask = np.linalg.norm(template.astype(np.float32) - background, axis=2) > 30
    if int(foreground_mask.sum()) >= max(8, int(template.shape[0] * template.shape[1] * 0.05)):
        template_pixels = template[foreground_mask]
        crop_pixels = crop_array[foreground_mask]
    else:
        template_pixels = template.reshape(-1, 3)
        crop_pixels = crop_array.reshape(-1, 3)
    difference = np.mean(np.abs(template_pixels.astype(np.int16) - crop_pixels.astype(np.int16)))
    return max(0.0, min(1.0, 1.0 - float(difference) / 255.0))


def best_color_verified_match(
    screenshot: np.ndarray,
    capture_image: Image.Image,
    target: Target,
    minimum_confidence: float,
) -> tuple[float, Optional[tuple[int, int, int, int]], float]:
    build_template_cache(target)
    best_confidence = -math.inf
    best_box = None
    best_color = 0.0
    best_rejected_confidence = 0.0
    best_rejected_color = 0.0

    for _, template in target.template_cache:
        template_height, template_width = template.shape[:2]
        if template_height > screenshot.shape[0] or template_width > screenshot.shape[1]:
            continue

        result = cv2.matchTemplate(screenshot, template, cv2.TM_CCOEFF_NORMED)
        for _ in range(24):
            _, confidence, _, top_left = cv2.minMaxLoc(result)
            confidence = float(confidence)
            if confidence < minimum_confidence:
                break
            x, y = top_left
            box = (x, y, template_width, template_height)
            color_similarity = color_match_similarity(capture_image, target, box)
            if color_similarity >= COLOR_VERIFY_THRESHOLD and confidence > best_confidence:
                best_confidence = confidence
                best_box = box
                best_color = color_similarity
            elif confidence > best_rejected_confidence:
                best_rejected_confidence = confidence
                best_rejected_color = color_similarity

            clear_left = max(0, x - template_width // 2)
            clear_top = max(0, y - template_height // 2)
            clear_right = min(result.shape[1], x + template_width // 2 + 1)
            clear_bottom = min(result.shape[0], y + template_height // 2 + 1)
            result[clear_top:clear_bottom, clear_left:clear_right] = -1.0

    if best_box:
        return best_confidence, best_box, best_color
    return best_rejected_confidence, None, best_rejected_color


def best_match(screenshot: np.ndarray, target: Target) -> tuple[float, Optional[tuple[int, int, int, int]]]:
    build_template_cache(target)
    best_confidence = -math.inf
    best_box = None

    for _, template in target.template_cache:
        template_height, template_width = template.shape[:2]
        if template_height > screenshot.shape[0] or template_width > screenshot.shape[1]:
            continue

        result = cv2.matchTemplate(screenshot, template, cv2.TM_CCOEFF_NORMED)
        _, confidence, _, top_left = cv2.minMaxLoc(result)
        if confidence > best_confidence:
            x, y = top_left
            best_confidence = confidence
            best_box = (x, y, template_width, template_height)

    if best_confidence == -math.inf:
        return 0.0, None
    return float(best_confidence), best_box


def best_match_in_region(
    screenshot: np.ndarray,
    target: Target,
    search_box: Optional[tuple[int, int, int, int]] = None,
) -> tuple[float, Optional[tuple[int, int, int, int]]]:
    if not search_box:
        return best_match(screenshot, target)

    left, top, right, bottom = search_box
    left = max(0, left)
    top = max(0, top)
    right = min(screenshot.shape[1], right)
    bottom = min(screenshot.shape[0], bottom)
    if right <= left or bottom <= top:
        return 0.0, None

    confidence, box = best_match(screenshot[top:bottom, left:right], target)
    if not box:
        return confidence, None
    x, y, width, height = box
    return confidence, (left + x, top + y, width, height)


def coarse_to_fine_match(screenshot: np.ndarray, target: Target) -> tuple[float, Optional[tuple[int, int, int, int]]]:
    area = screenshot.shape[0] * screenshot.shape[1]
    if area < COARSE_AREA_THRESHOLD:
        return best_match(screenshot, target)

    build_template_cache(target)
    coarse = cv2.resize(screenshot, None, fx=COARSE_SCALE, fy=COARSE_SCALE, interpolation=cv2.INTER_AREA)
    best_confidence = -math.inf
    coarse_box = None

    for _, template in target.template_cache:
        coarse_width = max(1, int(template.shape[1] * COARSE_SCALE))
        coarse_height = max(1, int(template.shape[0] * COARSE_SCALE))
        if coarse_height > coarse.shape[0] or coarse_width > coarse.shape[1]:
            continue
        coarse_template = cv2.resize(template, (coarse_width, coarse_height), interpolation=cv2.INTER_AREA)
        result = cv2.matchTemplate(coarse, coarse_template, cv2.TM_CCOEFF_NORMED)
        _, confidence, _, top_left = cv2.minMaxLoc(result)
        if confidence > best_confidence:
            coarse_box = (top_left[0], top_left[1], coarse_width, coarse_height)
            best_confidence = float(confidence)

    if not coarse_box:
        return 0.0, None

    x, y, width, height = coarse_box
    left = int(x / COARSE_SCALE)
    top = int(y / COARSE_SCALE)
    right = int((x + width) / COARSE_SCALE)
    bottom = int((y + height) / COARSE_SCALE)
    pad = max(24, int(max(right - left, bottom - top) * 0.75))
    return best_match_in_region(
        screenshot,
        target,
        (left - pad, top - pad, right + pad, bottom + pad),
    )


class TargetDialog:
    def __init__(
        self,
        parent: tk.Tk,
        title: str,
        default_name: str = "",
        show_use_region: bool = False,
        use_region_default: bool = True,
    ):
        self.parent = parent
        self.title = title
        self.default_name = default_name
        self.name_var = tk.StringVar(value=default_name)
        self.show_use_region = show_use_region
        self.use_region_var = tk.BooleanVar(value=use_region_default)
        self.use_region = False
        self.result: Optional[str] = None
        self.error_var = tk.StringVar(value="")

        self.dialog = tk.Toplevel(parent)
        self.dialog.title(title)
        self.dialog.configure(bg=COLOR_BG)
        self.dialog.transient(parent)
        self.dialog.grab_set()
        self.dialog.resizable(False, False)
        self.dialog.protocol("WM_DELETE_WINDOW", self.cancel)
        apply_dialog_chrome_style(self.dialog)

        body = tk.Frame(self.dialog, bg=COLOR_PANEL, highlightthickness=1, highlightbackground=COLOR_BORDER)
        body.pack(fill="both", expand=True, padx=10, pady=10)
        body.columnconfigure(0, weight=1)

        tk.Label(body, text=title, font=("", 12, "bold"), bg=COLOR_PANEL, fg=COLOR_TEXT).grid(
            row=0,
            column=0,
            sticky="w",
            padx=14,
            pady=(14, 10),
        )
        tk.Label(body, text="Name", font=("", 8, "bold"), bg=COLOR_PANEL, fg=COLOR_TEXT).grid(
            row=1,
            column=0,
            sticky="w",
            padx=14,
            pady=(0, 5),
        )
        self.entry = tk.Entry(
            body,
            textvariable=self.name_var,
            width=38,
            bg=COLOR_PANEL_2,
            fg=COLOR_TEXT,
            insertbackground=COLOR_TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=COLOR_BORDER,
            highlightcolor=COLOR_ACCENT,
            font=("", 9),
        )
        self.entry.grid(row=2, column=0, sticky="ew", padx=14, ipady=4)

        if self.show_use_region:
            checkbox_row = tk.Frame(body, bg=COLOR_PANEL)
            checkbox_row.grid(row=3, column=0, sticky="w", padx=10, pady=(10, 0))
            checkbox = tk.Checkbutton(
                checkbox_row,
                text="Use snipped area as this target's search area",
                variable=self.use_region_var,
                bg=COLOR_PANEL,
                fg=COLOR_TEXT,
                activebackground=COLOR_PANEL,
                activeforeground=COLOR_TEXT,
                selectcolor=COLOR_PANEL_2,
                relief="flat",
                highlightthickness=0,
                font=("", 9),
            )
            checkbox.pack(side="left")
            InfoIcon(
                checkbox_row,
                "After saving, SnipClicker will look for this image only in the part of the screen you just selected.",
                bg=COLOR_PANEL,
            ).pack(side="left", padx=(4, 0))

        tk.Label(body, textvariable=self.error_var, bg=COLOR_PANEL, fg=COLOR_DANGER, font=("", 8)).grid(
            row=4,
            column=0,
            sticky="w",
            padx=14,
            pady=(8, 0),
        )

        button_row = tk.Frame(body, bg=COLOR_PANEL)
        button_row.grid(row=5, column=0, sticky="e", padx=14, pady=(12, 14))
        RoundedButton(button_row, text="Cancel", command=self.cancel, variant="outline_accent", width=104, height=30, bg=COLOR_PANEL).pack(side="left", padx=(0, 8))
        RoundedButton(button_row, text="Save", command=self.confirm, variant="outline_accent", width=104, height=30, bg=COLOR_PANEL).pack(side="left")

        self.entry.bind("<Return>", lambda _: self.confirm())
        self.entry.bind("<Escape>", lambda _: self.cancel())
        self.dialog.bind("<Escape>", lambda _: self.cancel())

        self.dialog.update_idletasks()
        width = max(420, self.dialog.winfo_reqwidth())
        height = self.dialog.winfo_reqheight()
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - height) // 2
        self.dialog.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
        self.entry.focus_set()
        self.entry.selection_range(0, "end")
        parent.wait_window(self.dialog)

    def confirm(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            self.error_var.set("Enter a target name.")
            self.entry.focus_set()
            return
        self.result = name
        self.use_region = bool(self.use_region_var.get())
        self.dialog.destroy()

    def cancel(self) -> None:
        self.result = None
        self.dialog.destroy()


class RegionSelector:
    def __init__(
        self,
        parent: tk.Tk,
        on_selected: Callable[[tuple[int, int, int, int]], None],
        on_cancel: Callable[[], None],
    ):
        self.parent = parent
        self.on_selected = on_selected
        self.on_cancel = on_cancel
        self.start_x = 0
        self.start_y = 0
        self.start_canvas_x = 0
        self.start_canvas_y = 0
        self.rect_id: Optional[int] = None
        self.closed = False
        self.escape_listener: Optional[keyboard.Listener] = None

        self.window = tk.Toplevel(parent)
        self.window.attributes("-fullscreen", True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", 0.28)
        self.window.configure(bg="black")
        self.window.bind("<Escape>", self.cancel)
        self.window.bind_all("<Escape>", self.cancel)
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)

        self.canvas = tk.Canvas(self.window, cursor="crosshair", bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self.begin)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.finish)
        self.canvas.bind("<Escape>", self.cancel)
        self.canvas.focus_set()
        self.start_escape_listener()

    def start_escape_listener(self) -> None:
        def on_press(key: keyboard.Key | keyboard.KeyCode | None) -> None:
            if key == keyboard.Key.esc:
                self.parent.after(0, self.cancel)

        try:
            self.escape_listener = keyboard.Listener(on_press=on_press)
            self.escape_listener.daemon = True
            self.escape_listener.start()
        except Exception as exc:
            log_event(f"Failed to start area cancel listener: {exc}")

    def stop_escape_listener(self) -> None:
        if self.escape_listener:
            self.escape_listener.stop()
            self.escape_listener = None

    def begin(self, event: tk.Event) -> None:
        self.start_x = self.window.winfo_pointerx()
        self.start_y = self.window.winfo_pointery()
        self.start_canvas_x = event.x
        self.start_canvas_y = event.y
        self.rect_id = self.canvas.create_rectangle(
            event.x,
            event.y,
            event.x,
            event.y,
            outline="red",
            width=3,
        )

    def drag(self, event: tk.Event) -> None:
        if self.rect_id is not None:
            self.canvas.coords(self.rect_id, self.start_canvas_x, self.start_canvas_y, event.x, event.y)

    def finish(self, _: tk.Event) -> None:
        if self.closed:
            return
        end_x = self.window.winfo_pointerx()
        end_y = self.window.winfo_pointery()
        left, right = sorted((self.start_x, end_x))
        top, bottom = sorted((self.start_y, end_y))
        self.closed = True
        self.stop_escape_listener()
        self.window.unbind_all("<Escape>")
        self.window.destroy()
        if right - left >= 5 and bottom - top >= 5:
            self.on_selected((left, top, right, bottom))
        else:
            self.on_cancel()

    def cancel(self, _: Optional[tk.Event] = None) -> None:
        if self.closed:
            return
        self.closed = True
        self.stop_escape_listener()
        self.window.unbind_all("<Escape>")
        self.window.destroy()
        self.on_cancel()


class WindowSelector:
    def __init__(
        self,
        parent: tk.Tk,
        on_selected: Callable[[Optional[WindowAnchor]], None],
        on_cancel: Callable[[], None],
    ):
        self.parent = parent
        self.on_selected = on_selected
        self.on_cancel = on_cancel
        self.closed = False
        self.escape_listener: Optional[keyboard.Listener] = None

        self.window = tk.Toplevel(parent)
        self.window.attributes("-fullscreen", True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", 0.22)
        self.window.configure(bg="black")
        self.window.bind("<Escape>", self.cancel)
        self.window.bind_all("<Escape>", self.cancel)
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)

        self.canvas = tk.Canvas(self.window, cursor="hand2", bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(
            self.window.winfo_screenwidth() // 2,
            60,
            text="Click a window to follow it. Press N for no window. Esc cancels.",
            fill="white",
            font=("", 18, "bold"),
        )
        self.canvas.bind("<ButtonRelease-1>", self.select_window)
        self.canvas.focus_set()
        self.start_escape_listener()

    def start_escape_listener(self) -> None:
        def on_press(key: keyboard.Key | keyboard.KeyCode | None) -> None:
            if key == keyboard.Key.esc:
                self.parent.after(0, self.cancel)
            elif normalize_hotkey_key(key) == "n":
                self.parent.after(0, self.clear_window)

        try:
            self.escape_listener = keyboard.Listener(on_press=on_press)
            self.escape_listener.daemon = True
            self.escape_listener.start()
        except Exception as exc:
            log_event(f"Failed to start window cancel listener: {exc}")

    def stop_escape_listener(self) -> None:
        if self.escape_listener:
            self.escape_listener.stop()
            self.escape_listener = None

    def select_window(self, _: tk.Event) -> None:
        if self.closed:
            return
        x, y = cursor_position()
        self.closed = True
        self.stop_escape_listener()
        self.window.unbind_all("<Escape>")
        self.window.destroy()

        def resolve() -> None:
            anchor = window_anchor_at_point(x, y)
            if anchor:
                log_event(
                    f"Selected window hwnd={anchor.hwnd} title={anchor.title!r} "
                    f"class={anchor.class_name!r} rect={anchor.rect}"
                )
                self.on_selected(anchor)
            else:
                self.on_cancel()

        self.parent.after(120, resolve)

    def clear_window(self, _: Optional[tk.Event] = None) -> None:
        if self.closed:
            return
        self.closed = True
        self.stop_escape_listener()
        self.window.unbind_all("<Escape>")
        self.window.destroy()
        self.on_selected(None)

    def cancel(self, _: Optional[tk.Event] = None) -> None:
        if self.closed:
            return
        self.closed = True
        self.stop_escape_listener()
        self.window.unbind_all("<Escape>")
        self.window.destroy()
        self.on_cancel()


class AreaPreview:
    def __init__(self, parent: tk.Tk):
        self.parent = parent
        self.window: Optional[tk.Toplevel] = None
        self.hide_job: Optional[str] = None

    def show(self, region: tuple[int, int, int, int], duration_ms: int = 1500) -> None:
        self.hide()
        left, top, right, bottom = region
        width = max(1, right - left)
        height = max(1, bottom - top)

        self.window = tk.Toplevel(self.parent)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-transparentcolor", "white")
        self.window.configure(bg="white")
        self.window.geometry(f"{width}x{height}+{left}+{top}")

        canvas = tk.Canvas(self.window, bg="white", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        canvas.create_rectangle(1, 1, width - 2, height - 2, outline="red", width=3)
        self.hide_job = self.parent.after(duration_ms, self.hide)

    def hide(self) -> None:
        if self.hide_job:
            self.parent.after_cancel(self.hide_job)
            self.hide_job = None
        if self.window:
            self.window.destroy()
            self.window = None


class TargetList(ttk.Frame):
    def __init__(self, parent: tk.Widget):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, bg=COLOR_PANEL)
        self.canvas.pack(side="left", fill="both", expand=True)

        self.items: list[dict] = []
        self.selected_id: Optional[str] = None
        self.images: dict[str, ImageTk.PhotoImage] = {}
        self.large_images: dict[tuple[str, int, int], ImageTk.PhotoImage] = {}
        self.ui_images: dict[tuple[object, ...], ImageTk.PhotoImage] = {}
        self.bindings: dict[str, Callable[[tk.Event], None]] = {}
        self.row_bounds: dict[str, tuple[int, int]] = {}
        self.cell_bounds: dict[tuple[str, str], tuple[int, int, int, int]] = {}
        self.dragging_iid: Optional[str] = None
        self.scrollbar_active = False
        self.scrollbar_track_bounds: Optional[tuple[int, int, int, int]] = None
        self.scrollbar_thumb_bounds: Optional[tuple[int, int, int, int]] = None
        self.scrollbar_drag_start_y: Optional[int] = None
        self.scrollbar_drag_start_top: Optional[int] = None
        self.scrollbar_content_height = 1

        self.canvas.bind("<Button-1>", self.on_click)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<MouseWheel>", self.on_mousewheel)
        self.canvas.bind("<Configure>", lambda _: self.redraw())

    def heading(self, *_: object, **__: object) -> None:
        return

    def column(self, *_: object, **__: object) -> None:
        return

    def bind(self, sequence: str, func: Callable[[tk.Event], None], add: Optional[str] = None) -> str:
        self.bindings[sequence] = func
        return sequence

    def get_children(self) -> list[str]:
        return [item["iid"] for item in self.items]

    def delete(self, iid: str) -> None:
        self.items = [item for item in self.items if item["iid"] != iid]
        self.images.pop(iid, None)
        self.large_images.pop(iid, None)
        if self.selected_id == iid:
            self.selected_id = None
        self.redraw()

    def exists(self, iid: str) -> bool:
        return any(item["iid"] == iid for item in self.items)

    def selection(self) -> tuple[str, ...]:
        return (self.selected_id,) if self.selected_id else ()

    def selection_set(self, iid: str) -> None:
        if self.exists(iid):
            changed = self.selected_id != iid
            self.selected_id = iid
            self.redraw()
            if changed and "<<TreeviewSelect>>" in self.bindings:
                self.bindings["<<TreeviewSelect>>"](tk.Event())

    def insert(
        self,
        _: str,
        __: str,
        iid: str,
        image: Optional[ImageTk.PhotoImage],
        values: tuple[str, ...],
        image_path: str = "",
    ) -> None:
        self.items.append({"iid": iid, "values": values, "image_path": image_path})
        if image:
            self.images[iid] = image
        self.redraw()

    def item_at_y(self, y: int) -> Optional[str]:
        canvas_y = int(self.canvas.canvasy(y))
        for iid, (top, bottom) in self.row_bounds.items():
            if top <= canvas_y <= bottom:
                return iid
        return None

    def column_at_x(self, iid: str, x: int, y: int) -> Optional[str]:
        canvas_x = int(self.canvas.canvasx(x))
        canvas_y = int(self.canvas.canvasy(y))
        for (target_id, column), (left, top, right, bottom) in self.cell_bounds.items():
            if target_id == iid and left <= canvas_x < right and top <= canvas_y < bottom:
                return column
        return None

    def on_click(self, event: tk.Event) -> None:
        if self.on_scrollbar_click(event):
            return

        iid = self.item_at_y(event.y)
        if not iid:
            changed = self.selected_id is not None
            self.selected_id = None
            self.redraw()
            if changed and "<<TreeviewSelect>>" in self.bindings:
                self.bindings["<<TreeviewSelect>>"](tk.Event())
            return

        self.selection_set(iid)
        column = self.column_at_x(iid, event.x, event.y)
        if column == "drag":
            self.dragging_iid = iid
            return
        event.column_name = column
        if "<<CellClick>>" in self.bindings:
            self.bindings["<<CellClick>>"](event)

    def on_drag(self, event: tk.Event) -> None:
        if self.scrollbar_drag_start_y is not None:
            self.on_scrollbar_drag(event)
            return

        if not self.dragging_iid:
            return
        target_iid = self.item_at_y(event.y)
        if not target_iid or target_iid == self.dragging_iid:
            return

        old_index = next((index for index, item in enumerate(self.items) if item["iid"] == self.dragging_iid), None)
        new_index = next((index for index, item in enumerate(self.items) if item["iid"] == target_iid), None)
        if old_index is None or new_index is None:
            return

        item = self.items.pop(old_index)
        self.items.insert(new_index, item)
        self.redraw()

    def on_release(self, _: tk.Event) -> None:
        if self.scrollbar_drag_start_y is not None:
            self.scrollbar_drag_start_y = None
            self.scrollbar_drag_start_top = None
            return

        if not self.dragging_iid:
            return
        self.dragging_iid = None
        if "<<RowsReordered>>" in self.bindings:
            self.bindings["<<RowsReordered>>"](tk.Event())

    def on_mousewheel(self, event: tk.Event) -> None:
        if not self.items:
            return
        self.canvas.yview_scroll(-1 * int(event.delta / 120), "units")
        self.redraw()

    def on_scrollbar_click(self, event: tk.Event) -> bool:
        if not self.scrollbar_track_bounds:
            return False
        canvas_x = int(self.canvas.canvasx(event.x))
        canvas_y = int(self.canvas.canvasy(event.y))
        track_left, track_top, track_right, track_bottom = self.scrollbar_track_bounds
        if not (track_left - 3 <= canvas_x <= track_right + 3 and track_top <= canvas_y <= track_bottom):
            return False
        if not self.scrollbar_active or not self.scrollbar_thumb_bounds:
            return True

        thumb_left, thumb_top, thumb_right, thumb_bottom = self.scrollbar_thumb_bounds
        if thumb_left - 3 <= canvas_x <= thumb_right + 3 and thumb_top <= canvas_y <= thumb_bottom:
            self.scrollbar_drag_start_y = event.y
            self.scrollbar_drag_start_top = int(self.canvas.canvasy(0))
            return True

        thumb_height = max(1, thumb_bottom - thumb_top)
        track_height = max(1, track_bottom - track_top)
        scrollable = max(1, self.scrollbar_content_height - self.canvas.winfo_height())
        travel = max(1, track_height - thumb_height)
        desired_thumb_top = min(track_bottom - thumb_height, max(track_top, canvas_y - thumb_height // 2))
        desired_view_top = int((desired_thumb_top - track_top) / travel * scrollable)
        self.canvas.yview_moveto(desired_view_top / max(1, self.scrollbar_content_height))
        self.redraw()
        return True

    def on_scrollbar_drag(self, event: tk.Event) -> None:
        if (
            self.scrollbar_drag_start_y is None
            or self.scrollbar_drag_start_top is None
            or not self.scrollbar_track_bounds
            or not self.scrollbar_thumb_bounds
        ):
            return
        _, track_top, _, track_bottom = self.scrollbar_track_bounds
        _, thumb_top, _, thumb_bottom = self.scrollbar_thumb_bounds
        track_height = max(1, track_bottom - track_top)
        thumb_height = max(1, thumb_bottom - thumb_top)
        travel = max(1, track_height - thumb_height)
        scrollable = max(1, self.scrollbar_content_height - self.canvas.winfo_height())
        delta = event.y - self.scrollbar_drag_start_y
        next_top = min(scrollable, max(0, self.scrollbar_drag_start_top + int(delta / travel * scrollable)))
        self.canvas.yview_moveto(next_top / max(1, self.scrollbar_content_height))
        self.redraw()

    def large_image_for(self, item: dict, max_width: int, max_height: int) -> Optional[ImageTk.PhotoImage]:
        iid = item["iid"]
        cache_key = (iid, max_width, max_height)
        if cache_key in self.large_images:
            return self.large_images[cache_key]
        image_path = item.get("image_path")
        if not image_path:
            return self.images.get(iid)
        try:
            image = Image.open(image_path)
            image.thumbnail((max_width, max_height))
            photo = ImageTk.PhotoImage(image)
            self.large_images[cache_key] = photo
            return photo
        except OSError:
            return self.images.get(iid)

    def rounded_image(
        self,
        key: tuple[object, ...],
        width: int,
        height: int,
        radius: int,
        fill: str,
        outline: str = "",
    ) -> ImageTk.PhotoImage:
        if key in self.ui_images:
            return self.ui_images[key]
        scale = 4
        image = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        rect = (0, 0, width * scale - 1, height * scale - 1)
        draw.rounded_rectangle(
            rect,
            radius=radius * scale,
            fill=fill,
            outline=outline or None,
            width=scale if outline else 1,
        )
        image = image.resize((width, height), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(image)
        self.ui_images[key] = photo
        return photo

    def switch_image(self, enabled: bool) -> ImageTk.PhotoImage:
        key = ("switch", enabled)
        if key in self.ui_images:
            return self.ui_images[key]
        width, height, scale = 32, 16, 4
        image = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        fill = COLOR_ACCENT if enabled else "#2d3744"
        knob_fill = "#f8fafc" if enabled else "#cbd5e1"
        draw.rounded_rectangle(
            (0, 0, width * scale - 1, height * scale - 1),
            radius=8 * scale,
            fill=fill,
        )
        knob_radius = 6 * scale
        knob_center_x = (width - 8 if enabled else 8) * scale
        knob_center_y = (height // 2) * scale
        draw.ellipse(
            (
                knob_center_x - knob_radius,
                knob_center_y - knob_radius,
                knob_center_x + knob_radius,
                knob_center_y + knob_radius,
            ),
            fill=knob_fill,
        )
        image = image.resize((width, height), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(image)
        self.ui_images[key] = photo
        return photo

    def redraw(self) -> None:
        self.canvas.delete("all")
        width = max(self.canvas.winfo_width(), 420)
        scrollbar_width = 6
        scrollbar_gutter = MAIN_PANEL_PADDING + 1
        scrollbar_right = width - 1
        scrollbar_left = scrollbar_right - scrollbar_width
        row_right = scrollbar_left - scrollbar_gutter
        row_height = 56
        row_gap = 2
        self.row_bounds = {}
        self.cell_bounds = {}

        y = 0
        for index, item in enumerate(self.items, start=1):
            iid = item["iid"]
            selected = iid == self.selected_id
            fill = "#102033" if selected else "#111922"
            outline = COLOR_ACCENT if selected else "#24303d"
            card_left = 0
            card_top = y + 2
            card_right = row_right
            card_bottom = y + row_height - 2
            rounded_rect(self.canvas, card_left, card_top, card_right, card_bottom, 6, fill=fill, outline=outline)
            self.row_bounds[iid] = (y, y + row_height)

            values = item["values"]
            enabled, name = values[0], values[1]
            is_scan_cursor = len(values) > 7 and values[7] == "scan_cursor"
            text_y = y + row_height // 2

            drag_x = card_left + 18
            for dot_y in (text_y - 6, text_y, text_y + 6):
                self.canvas.create_oval(drag_x - 4, dot_y - 1, drag_x - 1, dot_y + 2, fill=COLOR_MUTED, outline="")
                self.canvas.create_oval(drag_x + 3, dot_y - 1, drag_x + 6, dot_y + 2, fill=COLOR_MUTED, outline="")
            self.canvas.create_text(card_left + 48, text_y, text=str(index), anchor="center", fill="#cbd5e1", font=("", 8))

            image = self.large_image_for(item, 64, 34) or self.images.get(iid)
            image_x = card_left + 100
            if image:
                self.canvas.create_image(image_x, text_y, image=image, anchor="center")

            switch_width = 32
            switch_height = 16
            pill_width = 54
            switch_left = max(card_left + 226, width - 1 - switch_width - pill_width - 60)
            switch_top = text_y - switch_height // 2
            switch_right = switch_left + switch_width
            switch_bottom = switch_top + switch_height
            pill_left = switch_right + 12
            pill_right = min(card_right - 18, pill_left + pill_width)
            name_left = card_left + 154
            name_right = switch_left - 16

            self.canvas.create_text(
                name_left,
                text_y,
                text=name,
                anchor="w",
                fill=COLOR_TEXT,
                font=("", 8, "bold"),
            )

            enabled_bool = enabled == "Yes"
            self.canvas.create_image(switch_left, switch_top, image=self.switch_image(enabled_bool), anchor="nw")

            pill_text = "Enabled" if enabled == "Yes" else "Disabled"
            pill_fill = "#062d1d" if enabled == "Yes" else "#1e293b"
            pill_outline = "#14532d" if enabled == "Yes" else "#334155"
            pill_text_color = "#4ade80" if enabled == "Yes" else "#cbd5e1"
            pill_image = self.rounded_image(("pill", enabled_bool), pill_right - pill_left, 20, 5, pill_fill, pill_outline)
            self.canvas.create_image(pill_left, text_y - 10, image=pill_image, anchor="nw")
            self.canvas.create_text((pill_left + pill_right) // 2, text_y, text=pill_text, fill=pill_text_color, font=("", 7, "bold"))
            light_x = pill_right + 10
            light_fill = COLOR_ACCENT if is_scan_cursor else "#1f2933"
            light_outline = "#60a5fa" if is_scan_cursor else "#334155"
            self.canvas.create_oval(light_x - 4, text_y - 4, light_x + 4, text_y + 4, fill=light_fill, outline=light_outline)

            self.cell_bounds[(iid, "drag")] = (card_left, card_top, card_left + 40, card_bottom)
            self.cell_bounds[(iid, "image")] = (card_left + 64, card_top, name_left - 8, card_bottom)
            self.cell_bounds[(iid, "name")] = (name_left, card_top + 10, name_right, card_bottom - 10)
            self.cell_bounds[(iid, "enabled")] = (switch_left, switch_top, light_x + 6, switch_bottom)
            y += row_height
            if index < len(self.items):
                y += row_gap
        actual_content_height = y
        content_height = max(actual_content_height, self.canvas.winfo_height())
        self.scrollbar_content_height = content_height
        self.canvas.configure(scrollregion=(0, 0, width, content_height))
        self.draw_scrollbar(
            content_height=content_height,
            actual_content_height=actual_content_height,
            scrollbar_left=scrollbar_left,
            scrollbar_right=scrollbar_right,
        )

    def draw_scrollbar(
        self,
        content_height: int,
        actual_content_height: int,
        scrollbar_left: int,
        scrollbar_right: int,
    ) -> None:
        viewport_height = max(1, self.canvas.winfo_height())
        view_top = int(self.canvas.canvasy(0))
        track_margin = 4
        track_top = view_top + track_margin
        track_bottom = view_top + viewport_height - track_margin
        track_height = max(1, track_bottom - track_top)
        self.scrollbar_track_bounds = (scrollbar_left, track_top, scrollbar_right, track_bottom)
        self.scrollbar_active = actual_content_height > viewport_height

        if self.scrollbar_active:
            thumb_height = max(28, int(viewport_height / content_height * track_height))
            scrollable = max(1, content_height - viewport_height)
            thumb_top = track_top + int(view_top / scrollable * (track_height - thumb_height))
            thumb_fill = "#516070"
        else:
            thumb_height = track_height
            thumb_top = track_top
            thumb_fill = "#2a3644"
        thumb_bottom = thumb_top + thumb_height
        self.scrollbar_thumb_bounds = (scrollbar_left, thumb_top, scrollbar_right, thumb_bottom)

        rounded_rect(self.canvas, scrollbar_left, track_top, scrollbar_right, track_bottom, 3, fill="#101720", outline="#263241")
        rounded_rect(self.canvas, scrollbar_left + 1, thumb_top + 1, scrollbar_right - 1, thumb_bottom - 1, 3, fill=thumb_fill, outline="")


class RoundedPanel(tk.Canvas):
    def __init__(self, parent: tk.Widget, padding: int = 12):
        super().__init__(parent, bg=COLOR_BG, highlightthickness=0, bd=0)
        self.padding = padding
        self.body = tk.Frame(self, bg=COLOR_PANEL)
        self.body_id = self.create_window(padding, padding, anchor="nw", window=self.body)
        self.bind("<Configure>", self.redraw)

    def redraw(self, _: Optional[tk.Event] = None) -> None:
        self.delete("panel")
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        rounded_rect(self, 1, 1, width - 1, height - 1, 10, fill=COLOR_PANEL, outline=COLOR_BORDER, tags="panel")
        self.tag_lower("panel")
        self.coords(self.body_id, self.padding, self.padding)
        self.itemconfigure(self.body_id, width=max(1, width - self.padding * 2), height=max(1, height - self.padding * 2))


class RoundedButton(tk.Canvas):
    def __init__(
        self,
        parent: tk.Widget,
        text: str,
        command: Callable[[], None],
        variant: str = "tool",
        width: int = 118,
        height: int = 42,
        bg: str = COLOR_BG,
        long_press_command: Optional[Callable[[], None]] = None,
        long_press_ms: int = 3000,
    ):
        super().__init__(parent, width=width, height=height, bg=bg, highlightthickness=0, bd=0)
        self.text = text
        self.command = command
        self.variant = variant
        self.width_value = width
        self.height_value = height
        self.long_press_command = long_press_command
        self.long_press_ms = long_press_ms
        self.long_press_job: Optional[str] = None
        self.long_press_fired = False
        self.pressed = False
        self.hover = False
        self.bind("<ButtonPress-1>", self.on_press)
        self.bind("<ButtonRelease-1>", self.on_release)
        self.bind("<Enter>", self.on_enter)
        self.bind("<Leave>", self.on_leave)
        self.configure(cursor="hand2")
        self.redraw()

    def colors(self) -> tuple[str, str, str]:
        if self.variant == "accent":
            return (COLOR_ACCENT if not self.hover else "#1f78e0", "white", COLOR_ACCENT)
        if self.variant == "outline_accent":
            return ("#12345a" if self.hover else "#102033", "#60a5fa", "#1d4f85")
        if self.variant == "danger":
            return ("#3a1518" if not self.hover else "#4a1c20", "#fecaca", COLOR_DANGER)
        if self.variant == "outline_danger":
            return ("#3a1518" if self.hover else "#1f1518", "#f87171", "#7f1d1d")
        return (COLOR_PANEL if not self.hover else COLOR_PANEL_2, COLOR_TEXT, COLOR_BORDER)

    def on_enter(self, _: tk.Event) -> None:
        self.hover = True
        self.redraw()

    def on_leave(self, _: tk.Event) -> None:
        self.hover = False
        self.redraw()

    def on_press(self, _: tk.Event) -> None:
        self.pressed = True
        self.long_press_fired = False
        if self.long_press_command:
            self.long_press_job = self.after(self.long_press_ms, self.fire_long_press)

    def on_release(self, _: tk.Event) -> None:
        if self.long_press_job:
            self.after_cancel(self.long_press_job)
            self.long_press_job = None
        should_click = self.pressed and not self.long_press_fired
        self.pressed = False
        if should_click:
            self.command()

    def fire_long_press(self) -> None:
        self.long_press_job = None
        self.long_press_fired = True
        if self.long_press_command:
            self.long_press_command()

    def redraw(self) -> None:
        self.delete("all")
        fill, text_color, outline = self.colors()
        rounded_rect(self, 1, 1, self.width_value - 1, self.height_value - 1, 8, fill=fill, outline=outline)
        self.create_text(self.width_value // 2, self.height_value // 2, text=self.text, fill=text_color, font=("", 9, "bold"))

    def set_text(self, text: str) -> None:
        self.text = text
        self.redraw()

    def set_variant(self, variant: str) -> None:
        self.variant = variant
        self.redraw()


class HoverTooltip:
    def __init__(self, widget: tk.Widget, text: str, wraplength: int = 280):
        self.widget = widget
        self.text = text
        self.wraplength = wraplength
        self.window: Optional[tk.Toplevel] = None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def show(self, event: tk.Event) -> None:
        if self.window:
            return
        self.window = tk.Toplevel(self.widget)
        self.window.overrideredirect(True)
        self.window.configure(bg=COLOR_BORDER)
        frame = tk.Frame(self.window, bg=COLOR_PANEL, highlightthickness=1, highlightbackground=COLOR_BORDER)
        frame.pack(padx=1, pady=1)
        tk.Label(
            frame,
            text=self.text,
            bg=COLOR_PANEL,
            fg=COLOR_TEXT,
            justify="left",
            wraplength=self.wraplength,
            font=("", 8),
        ).pack(padx=9, pady=7)
        self.window.update_idletasks()
        self.window.geometry(f"+{event.x_root + 12}+{event.y_root + 12}")

    def hide(self, _: Optional[tk.Event] = None) -> None:
        if self.window:
            self.window.destroy()
            self.window = None


class InfoIcon(tk.Canvas):
    def __init__(self, parent: tk.Widget, text: str, bg: str = COLOR_PANEL):
        super().__init__(parent, width=15, height=15, bg=bg, highlightthickness=0, bd=0)
        self.tooltip = HoverTooltip(self, text)
        self.configure(cursor="question_arrow")
        self.create_oval(2, 2, 13, 13, outline=COLOR_MUTED, width=1)
        self.create_text(7, 7, text="i", fill=COLOR_MUTED, font=("", 8, "bold"))


class ThresholdSlider(tk.Canvas):
    def __init__(
        self,
        parent: tk.Widget,
        variable: tk.DoubleVar,
        command: Callable[[str], None],
        width: int = 360,
        height: int = 24,
        bg: str = COLOR_PANEL,
    ):
        super().__init__(parent, width=width, height=height, bg=bg, highlightthickness=0, bd=0)
        self.variable = variable
        self.command = command
        self.width_value = width
        self.height_value = height
        self.enabled = True
        self.bind("<Button-1>", self.on_pointer)
        self.bind("<B1-Motion>", self.on_pointer)
        self.configure(cursor="hand2")
        self.redraw()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self.redraw()

    def on_pointer(self, event: tk.Event) -> None:
        if not self.enabled:
            return
        track_left = 0
        track_right = self.width_value
        ratio = min(1.0, max(0.0, (event.x - track_left) / max(1, track_right - track_left)))
        value = 0.50 + ratio * 0.49
        self.variable.set(value)
        self.command(str(value))
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        track_left = 0
        track_right = self.width_value
        track_y = self.height_value // 2
        ratio = (float(self.variable.get()) - 0.50) / 0.49
        ratio = min(1.0, max(0.0, ratio))
        knob_x = track_left + ratio * (track_right - track_left)
        active = COLOR_ACCENT if self.enabled else "#334155"
        inactive = "#334155"
        rounded_rect(self, track_left, track_y - 3, track_right, track_y + 3, 3, fill=inactive, outline="")
        rounded_rect(self, track_left, track_y - 3, int(knob_x), track_y + 3, 3, fill=active, outline="")
        self.create_oval(knob_x - 8, track_y - 8, knob_x + 8, track_y + 8, fill=active, outline="")


class CropEditor:
    def __init__(self, parent: tk.Tk, target: Target):
        self.parent = parent
        self.target = target
        self.result: Optional[tuple[int, int, int, int]] = None
        self.drag_edge: Optional[str] = None
        self.image = Image.open(target.image_path).convert("RGBA")
        self.image_width, self.image_height = self.image.size
        max_width, max_height = 720, 500
        self.scale = min(max_width / self.image_width, max_height / self.image_height, 8.0)
        self.scale = max(1.0, self.scale)
        self.display_width = max(1, int(self.image_width * self.scale))
        self.display_height = max(1, int(self.image_height * self.scale))
        self.crop_rect = [0, 0, self.image_width, self.image_height]

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("Crop Editor")
        self.dialog.configure(bg=COLOR_BG)
        self.dialog.transient(parent)
        self.dialog.grab_set()
        self.dialog.resizable(False, False)
        self.dialog.protocol("WM_DELETE_WINDOW", self.cancel)
        apply_dialog_chrome_style(self.dialog)

        body = tk.Frame(self.dialog, bg=COLOR_PANEL, highlightthickness=1, highlightbackground=COLOR_BORDER)
        body.pack(fill="both", expand=True, padx=10, pady=10)
        body.columnconfigure(0, weight=1)
        tk.Label(body, text="Crop Editor", font=("", 12, "bold"), bg=COLOR_PANEL, fg=COLOR_TEXT).grid(row=0, column=0, sticky="w", padx=14, pady=(14, 6))
        tk.Label(body, text=target.name, bg=COLOR_PANEL, fg=COLOR_MUTED, font=("", 9)).grid(row=1, column=0, sticky="w", padx=14, pady=(0, 10))

        self.canvas = tk.Canvas(body, width=self.display_width, height=self.display_height, bg=COLOR_PANEL_2, highlightthickness=1, highlightbackground=COLOR_BORDER)
        self.canvas.grid(row=2, column=0, padx=14, pady=(0, 10))
        self.display_image = ImageTk.PhotoImage(self.image.resize((self.display_width, self.display_height), Image.Resampling.NEAREST))
        self.canvas.bind("<Motion>", self.on_motion)
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)

        self.size_var = tk.StringVar()
        tk.Label(body, textvariable=self.size_var, bg=COLOR_PANEL, fg=COLOR_MUTED, font=("", 8)).grid(row=3, column=0, sticky="w", padx=14)

        button_row = tk.Frame(body, bg=COLOR_PANEL)
        button_row.grid(row=4, column=0, sticky="e", padx=14, pady=(12, 14))
        RoundedButton(button_row, text="Cancel", command=self.cancel, variant="outline_accent", width=104, height=30, bg=COLOR_PANEL).pack(side="left", padx=(0, 8))
        RoundedButton(button_row, text="Reset", command=self.reset, variant="tool", width=88, height=30, bg=COLOR_PANEL).pack(side="left", padx=(0, 8))
        RoundedButton(button_row, text="Apply Crop", command=self.apply, variant="outline_accent", width=128, height=30, bg=COLOR_PANEL).pack(side="left")

        self.redraw()
        self.dialog.update_idletasks()
        width = self.dialog.winfo_reqwidth()
        height = self.dialog.winfo_reqheight()
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - height) // 2
        self.dialog.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
        parent.wait_window(self.dialog)

    def image_to_canvas(self, value: int) -> int:
        return int(value * self.scale)

    def canvas_to_image(self, value: int) -> int:
        return int(round(value / self.scale))

    def edge_at(self, x: int, y: int) -> Optional[str]:
        left, top, right, bottom = [self.image_to_canvas(value) for value in self.crop_rect]
        tolerance = 10
        if top - tolerance <= y <= bottom + tolerance:
            if abs(x - left) <= tolerance:
                return "left"
            if abs(x - right) <= tolerance:
                return "right"
        if left - tolerance <= x <= right + tolerance:
            if abs(y - top) <= tolerance:
                return "top"
            if abs(y - bottom) <= tolerance:
                return "bottom"
        return None

    def on_motion(self, event: tk.Event) -> None:
        edge = self.edge_at(event.x, event.y)
        if edge in ("left", "right"):
            self.canvas.configure(cursor="sb_h_double_arrow")
        elif edge in ("top", "bottom"):
            self.canvas.configure(cursor="sb_v_double_arrow")
        else:
            self.canvas.configure(cursor="arrow")

    def on_press(self, event: tk.Event) -> None:
        self.drag_edge = self.edge_at(event.x, event.y)

    def on_drag(self, event: tk.Event) -> None:
        if not self.drag_edge:
            return
        min_size = 2
        x = min(self.image_width, max(0, self.canvas_to_image(event.x)))
        y = min(self.image_height, max(0, self.canvas_to_image(event.y)))
        if self.drag_edge == "left":
            self.crop_rect[0] = min(x, self.crop_rect[2] - min_size)
        elif self.drag_edge == "right":
            self.crop_rect[2] = max(x, self.crop_rect[0] + min_size)
        elif self.drag_edge == "top":
            self.crop_rect[1] = min(y, self.crop_rect[3] - min_size)
        elif self.drag_edge == "bottom":
            self.crop_rect[3] = max(y, self.crop_rect[1] + min_size)
        self.redraw()

    def on_release(self, _: tk.Event) -> None:
        self.drag_edge = None

    def reset(self) -> None:
        self.crop_rect = [0, 0, self.image_width, self.image_height]
        self.redraw()

    def apply(self) -> None:
        left, top, right, bottom = self.crop_rect
        if right - left < 2 or bottom - top < 2:
            return
        self.result = (left, top, right, bottom)
        self.dialog.destroy()

    def cancel(self) -> None:
        self.result = None
        self.dialog.destroy()

    def redraw(self) -> None:
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self.display_image, anchor="nw")
        left, top, right, bottom = [self.image_to_canvas(value) for value in self.crop_rect]
        self.canvas.create_rectangle(0, 0, self.display_width, top, fill="#000000", stipple="gray50", outline="")
        self.canvas.create_rectangle(0, bottom, self.display_width, self.display_height, fill="#000000", stipple="gray50", outline="")
        self.canvas.create_rectangle(0, top, left, bottom, fill="#000000", stipple="gray50", outline="")
        self.canvas.create_rectangle(right, top, self.display_width, bottom, fill="#000000", stipple="gray50", outline="")
        self.canvas.create_rectangle(left, top, right, bottom, outline=COLOR_ACCENT, width=2)
        handle = 5
        for x1, y1, x2, y2 in (
            (left - handle, (top + bottom) // 2 - handle, left + handle, (top + bottom) // 2 + handle),
            (right - handle, (top + bottom) // 2 - handle, right + handle, (top + bottom) // 2 + handle),
            ((left + right) // 2 - handle, top - handle, (left + right) // 2 + handle, top + handle),
            ((left + right) // 2 - handle, bottom - handle, (left + right) // 2 + handle, bottom + handle),
        ):
            self.canvas.create_rectangle(x1, y1, x2, y2, fill=COLOR_ACCENT, outline="")
        crop_width = self.crop_rect[2] - self.crop_rect[0]
        crop_height = self.crop_rect[3] - self.crop_rect[1]
        self.size_var.set(f"Original {self.image_width}x{self.image_height}  |  Crop {crop_width}x{crop_height}")


class ClickerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1016x509")
        self.root.minsize(980, 500)
        self.root.configure(bg=COLOR_BG)

        self.targets = load_targets()
        self.bound_window = load_bound_window()
        self.hotkey = load_hotkey()
        self.running = False
        self.status_var = tk.StringVar(value="Stopped")
        self.detail_var = tk.StringVar(value="")
        self.bound_window_var = tk.StringVar()
        self.hotkey_var = tk.StringVar(value=hotkey_display(self.hotkey))
        self.target_count_var = tk.StringVar()
        self.thumbnail_refs: dict[str, ImageTk.PhotoImage] = {}
        self.scan_count = 0
        self.scan_start_index = 0
        self.hotkey_listener: Optional[keyboard.Listener] = None
        self.hotkey_pressed_keys: set[str] = set()
        self.hotkey_triggered = False
        self.area_preview = AreaPreview(root)
        self.last_cursor_pos = cursor_position()
        self.pending_click: Optional[dict] = None
        self.suppress_selection_preview = False
        self.detail_updating = False
        self.detail_target_photo: Optional[ImageTk.PhotoImage] = None
        self.target_preview_image_bounds: Optional[tuple[int, int, int, int]] = None
        self.detail_area_photo: Optional[ImageTk.PhotoImage] = None
        self.initial_selection_done = False
        self.delete_all_mode = False

        self._build_ui()
        self.root.after(250, lambda: apply_window_chrome_style(self.root))
        self.update_bound_window_label()
        self._refresh_target_list()
        self.start_hotkey_listener()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self) -> None:
        root_frame = ttk.Frame(self.root, padding=(8, 4, 8, 8), style="App.TFrame")
        root_frame.pack(fill="both", expand=True)

        toolbar = ttk.Frame(root_frame, style="Toolbar.TFrame")
        toolbar.pack(fill="x", pady=(0, 4))

        self.start_button = RoundedButton(toolbar, text="Start", command=self.toggle_running, variant="outline_accent", width=118, height=30)
        self.start_button.pack(side="left")
        RoundedButton(toolbar, text="Hotkey", command=self.capture_hotkey, variant="outline_accent", width=92, height=30).pack(side="left", padx=(12, 0))
        hotkey_info_row = tk.Frame(toolbar, bg=COLOR_BG)
        hotkey_info_row.pack(side="left", padx=(8, 0))
        tk.Label(hotkey_info_row, textvariable=self.hotkey_var, bg=COLOR_BG, fg=COLOR_MUTED, font=("", 9)).pack(side="left")
        InfoIcon(hotkey_info_row, "Press this key combination to start or stop scanning.", bg=COLOR_BG).pack(side="left", padx=(4, 0))
        RoundedButton(toolbar, text="Window", command=self.change_bound_window, variant="outline_accent", width=92, height=30).pack(side="left", padx=(12, 0))
        window_info_row = tk.Frame(toolbar, bg=COLOR_BG)
        window_info_row.pack(side="left", padx=(8, 0))
        tk.Label(window_info_row, textvariable=self.bound_window_var, bg=COLOR_BG, fg=COLOR_MUTED, font=("", 9)).pack(side="left")
        InfoIcon(window_info_row, "Full-window targets search this window. None searches the whole screen.", bg=COLOR_BG).pack(side="left", padx=(4, 0))

        status_box = tk.Frame(toolbar, bg=COLOR_BG)
        status_box.pack(side="right", fill="y")
        tk.Label(status_box, textvariable=self.status_var, font=("", 10, "bold"), bg=COLOR_BG, fg=COLOR_TEXT).pack(anchor="e")
        tk.Label(status_box, textvariable=self.detail_var, bg=COLOR_BG, fg=COLOR_MUTED, font=("", 8)).pack(anchor="e")

        content = ttk.Frame(root_frame, style="App.TFrame")
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=1, uniform="main_panels")
        content.columnconfigure(1, weight=1, uniform="main_panels")
        content.rowconfigure(0, weight=1)

        targets_shell = RoundedPanel(content, padding=MAIN_PANEL_PADDING)
        targets_shell.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        targets_panel = targets_shell.body
        targets_panel.rowconfigure(1, weight=1)
        targets_panel.columnconfigure(0, weight=1)

        tk.Label(targets_panel, text="Targets", font=("", 13, "bold"), bg=COLOR_PANEL, fg=COLOR_TEXT).grid(row=0, column=0, sticky="w", pady=(0, 7))

        self.tree = TargetList(targets_panel)
        self.tree.grid(row=1, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_selection_changed)
        self.tree.bind("<<CellClick>>", self.on_target_cell_click)
        self.tree.bind("<<RowsReordered>>", self.on_targets_reordered)

        add_row = tk.Frame(targets_panel, bg=COLOR_PANEL)
        add_row.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        add_row.columnconfigure(1, weight=1)
        tk.Label(add_row, textvariable=self.target_count_var, bg=COLOR_PANEL, fg=COLOR_MUTED, font=("", 8)).grid(row=0, column=0, sticky="w", pady=(8, 0))
        RoundedButton(
            add_row,
            text="+ Add Target",
            command=self.show_add_target_menu,
            variant="outline_accent",
            width=138,
            height=30,
            bg=COLOR_PANEL,
        ).grid(row=0, column=2, sticky="e")

        details_shell = RoundedPanel(content, padding=MAIN_PANEL_PADDING)
        details_shell.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        details_panel = details_shell.body
        details_panel.columnconfigure(0, weight=1)
        details_panel.rowconfigure(3, weight=1)

        tk.Label(details_panel, text="Details", font=("", 13, "bold"), bg=COLOR_PANEL, fg=COLOR_TEXT).grid(row=0, column=0, sticky="w", pady=(0, 7))

        top_detail = tk.Frame(details_panel, bg=COLOR_PANEL)
        top_detail.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        top_detail.rowconfigure(0, weight=1)
        top_detail.columnconfigure(0, weight=0)
        top_detail.columnconfigure(1, weight=1)

        preview_frame = tk.Frame(top_detail, bg=COLOR_PANEL)
        preview_frame.grid(row=0, column=0, sticky="nw", padx=(0, 16))
        click_location_header = tk.Frame(preview_frame, bg=COLOR_PANEL)
        click_location_header.pack(anchor="w", pady=(0, 5))
        tk.Label(click_location_header, text="Click Location", font=("", 8, "bold"), bg=COLOR_PANEL, fg=COLOR_TEXT).pack(side="left")
        InfoIcon(click_location_header, "Click the image to choose where SnipClicker clicks after a match.", bg=COLOR_PANEL).pack(side="left", padx=(4, 0))
        self.target_preview_canvas = tk.Canvas(preview_frame, width=230, height=126, bg=COLOR_PANEL_2, highlightthickness=1, highlightbackground=COLOR_BORDER)
        self.target_preview_canvas.pack()
        self.target_preview_canvas.configure(cursor="crosshair")
        self.target_preview_canvas.bind("<Button-1>", self.set_target_click_point_from_preview)
        self.click_point_var = tk.StringVar(value="Click: Center")
        click_point_row = tk.Frame(preview_frame, bg=COLOR_PANEL)
        click_point_row.pack(fill="x", pady=(6, 0))
        RoundedButton(click_point_row, text="Center", command=self.reset_target_click_point, variant="tool", width=72, height=26, bg=COLOR_PANEL).pack(side="left")
        tk.Label(click_point_row, textvariable=self.click_point_var, bg=COLOR_PANEL, fg=COLOR_MUTED, font=("", 8)).pack(side="left", padx=(8, 0))

        right_detail = tk.Frame(top_detail, bg=COLOR_PANEL)
        right_detail.grid(row=0, column=1, sticky="nsew")
        right_detail.columnconfigure(0, weight=1)
        right_detail.rowconfigure(1, weight=1)

        fields_frame = tk.Frame(right_detail, bg=COLOR_PANEL)
        fields_frame.grid(row=0, column=0, sticky="ew")
        fields_frame.columnconfigure(0, weight=1)
        fields_frame.columnconfigure(1, weight=0)
        tk.Label(fields_frame, text="Name", font=("", 8, "bold"), bg=COLOR_PANEL, fg=COLOR_TEXT).grid(row=0, column=0, sticky="w", pady=(0, 5))
        tk.Label(fields_frame, text="Click Type", font=("", 8, "bold"), bg=COLOR_PANEL, fg=COLOR_TEXT).grid(row=0, column=1, sticky="w", padx=(12, 0), pady=(0, 5))
        self.detail_name_var = tk.StringVar()
        self.detail_name_entry = tk.Entry(
            fields_frame,
            textvariable=self.detail_name_var,
            bg=COLOR_PANEL_2,
            fg=COLOR_TEXT,
            insertbackground=COLOR_TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=COLOR_BORDER,
            highlightcolor=COLOR_ACCENT,
            disabledbackground=COLOR_PANEL_2,
            disabledforeground=COLOR_MUTED,
            font=("", 9),
        )
        self.detail_name_entry.grid(row=1, column=0, sticky="ew", ipady=3)
        self.detail_name_entry.bind("<Return>", self.commit_detail_name)
        self.detail_name_entry.bind("<FocusOut>", self.commit_detail_name)
        self.detail_click_var = tk.StringVar()
        self.detail_click_display_var = tk.StringVar()
        self.detail_click_button = tk.Menubutton(
            fields_frame,
            textvariable=self.detail_click_display_var,
            bg=COLOR_PANEL_2,
            fg=COLOR_TEXT,
            activebackground=COLOR_PANEL_2,
            activeforeground=COLOR_TEXT,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=COLOR_BORDER,
            highlightcolor=COLOR_ACCENT,
            width=13,
            anchor="w",
            font=("", 9),
        )
        self.detail_click_menu = tk.Menu(self.detail_click_button, tearoff=False, bg=COLOR_PANEL, fg=COLOR_TEXT, activebackground=COLOR_ACCENT, activeforeground="white")
        for click_type in CLICK_TYPES:
            self.detail_click_menu.add_command(label=click_type, command=lambda value=click_type: self.set_detail_click_type(value))
        self.detail_click_button.configure(menu=self.detail_click_menu)
        self.detail_click_button.grid(row=1, column=1, sticky="ew", padx=(12, 0), ipady=1)

        threshold_slot = tk.Frame(right_detail, bg=COLOR_PANEL)
        threshold_slot.grid(row=1, column=0, sticky="nsew")
        threshold_frame = tk.Frame(threshold_slot, bg=COLOR_PANEL)
        threshold_frame.place(relx=0, rely=0.5, anchor="w", relwidth=1)
        threshold_frame.columnconfigure(0, weight=1)
        self.threshold_label_var = tk.StringVar(value="Match Threshold")
        threshold_header = tk.Frame(threshold_frame, bg=COLOR_PANEL)
        threshold_header.grid(row=0, column=0, sticky="w", pady=(0, 4))
        tk.Label(threshold_header, textvariable=self.threshold_label_var, font=("", 8, "bold"), bg=COLOR_PANEL, fg=COLOR_TEXT).pack(side="left")
        InfoIcon(threshold_header, "Higher values require a closer image match before clicking.", bg=COLOR_PANEL).pack(side="left", padx=(4, 0))
        self.threshold_var = tk.DoubleVar(value=0.85)
        self.threshold_slider = ThresholdSlider(
            threshold_frame,
            variable=self.threshold_var,
            command=self.on_threshold_changed,
            width=300,
            bg=COLOR_PANEL,
        )
        self.threshold_slider.grid(row=1, column=0, sticky="w")
        scale_labels = tk.Frame(threshold_frame, bg=COLOR_PANEL)
        scale_labels.grid(row=2, column=0, sticky="ew", pady=(3, 0))
        tk.Label(scale_labels, text="Low (50%)", bg=COLOR_PANEL, fg=COLOR_MUTED, font=("", 7)).pack(side="left")
        tk.Label(scale_labels, text="Recommended (85%)", bg=COLOR_PANEL, fg=COLOR_MUTED, font=("", 7)).pack(side="left", expand=True)
        tk.Label(scale_labels, text="High (99%)", bg=COLOR_PANEL, fg=COLOR_MUTED, font=("", 7)).pack(side="right")
        RoundedButton(right_detail, text="Crop Editor", command=self.open_crop_editor, variant="tool", width=118, height=26, bg=COLOR_PANEL).grid(row=2, column=0, sticky="w")

        search_section = tk.Frame(details_panel, bg=COLOR_PANEL_2, highlightthickness=1, highlightbackground=COLOR_BORDER)
        search_section.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        search_section.columnconfigure(1, weight=1)
        search_header = tk.Frame(search_section, bg=COLOR_PANEL_2)
        search_header.grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(8, 6))
        tk.Label(search_header, text="Search Area", font=("", 9, "bold"), bg=COLOR_PANEL_2, fg=COLOR_TEXT).pack(side="left")
        InfoIcon(search_header, "Limits where this target is searched to improve speed and accuracy.", bg=COLOR_PANEL_2).pack(side="left", padx=(4, 0))
        self.search_area_canvas = tk.Canvas(search_section, width=230, height=92, bg="#0f1720", highlightthickness=0)
        self.search_area_canvas.grid(row=1, column=0, sticky="w", padx=(10, 16), pady=(0, 10))
        self.search_area_text_var = tk.StringVar(value="Select a target")
        tk.Label(search_section, textvariable=self.search_area_text_var, bg=COLOR_PANEL_2, fg=COLOR_TEXT, justify="left", font=("", 8, "bold")).grid(row=1, column=1, sticky="nw", pady=(6, 0))
        search_button_row = tk.Frame(search_section, bg=COLOR_PANEL_2)
        search_button_row.grid(row=1, column=1, sticky="w", pady=(36, 0))
        RoundedButton(search_button_row, text="Set Area", command=self.select_area_for_target, variant="outline_accent", width=88, height=30, bg=COLOR_PANEL_2).pack(side="left")
        RoundedButton(search_button_row, text="Clear", command=self.clear_area_for_target, variant="tool", width=72, height=30, bg=COLOR_PANEL_2).pack(side="left", padx=(8, 0))

        delete_row = tk.Frame(details_panel, bg=COLOR_PANEL)
        delete_row.grid(row=4, column=0, sticky="ew", pady=(0, 0))
        delete_row.columnconfigure(0, weight=1)
        delete_row.columnconfigure(2, weight=1)
        self.delete_button = RoundedButton(
            delete_row,
            text="Delete",
            command=self.handle_delete_button,
            variant="outline_danger",
            width=132,
            height=34,
            bg=COLOR_PANEL,
            long_press_command=self.arm_delete_all,
            long_press_ms=1500,
        )
        self.delete_button.grid(row=0, column=1, pady=(4, 0))

        self.cell_editor: Optional[tk.Widget] = None

    def commit_detail_name(self, _: Optional[tk.Event] = None) -> None:
        if self.detail_updating:
            return
        target = self.selected_target()
        if not target:
            return
        new_name = self.detail_name_var.get().strip()
        if new_name and new_name != target.name:
            target.name = new_name
            save_targets(self.targets)
            self._refresh_target_list()

    def commit_detail_click_type(self, _: Optional[tk.Event] = None) -> None:
        if self.detail_updating:
            return
        target = self.selected_target()
        click_type = self.detail_click_var.get()
        if target and click_type in CLICK_TYPES and click_type != target.click_type:
            target.click_type = click_type
            save_targets(self.targets)
            self._refresh_target_list()

    def set_detail_click_type(self, click_type: str) -> None:
        self.detail_click_var.set(click_type)
        self.detail_click_display_var.set(click_type)
        self.commit_detail_click_type()

    def update_details_panel(self) -> None:
        target = self.selected_target()
        self.detail_updating = True
        try:
            self.detail_name_var.set(target.name if target else "")
            self.detail_click_var.set(target.click_type if target else "")
            self.detail_click_display_var.set(target.click_type if target else "")
            if target:
                self.threshold_var.set(target.threshold)
                self.threshold_label_var.set(f"Match Threshold ({target.threshold:.0%})")
            else:
                self.threshold_label_var.set("Match Threshold")
            state = "normal" if target else "disabled"
            self.detail_name_entry.configure(state=state)
            self.detail_click_button.configure(state=state)
            self.threshold_slider.set_enabled(bool(target))
            self.threshold_slider.redraw()
            if target and target.click_point:
                self.click_point_var.set(f"Click: {target.click_point[0]:.0%}, {target.click_point[1]:.0%}")
            else:
                self.click_point_var.set("Click: Center")
        finally:
            self.detail_updating = False

        self.draw_target_preview(target)
        self.draw_search_area_preview(target)

    def draw_target_preview(self, target: Optional[Target]) -> None:
        self.target_preview_canvas.delete("all")
        self.detail_target_photo = None
        self.target_preview_image_bounds = None
        width = int(self.target_preview_canvas["width"])
        height = int(self.target_preview_canvas["height"])
        self.target_preview_canvas.create_rectangle(0, 0, width, height, fill=COLOR_PANEL_2, outline=COLOR_BORDER)
        if not target:
            self.target_preview_canvas.create_text(width // 2, height // 2, text="No target selected", fill=COLOR_MUTED, font=("", 8))
            return
        try:
            image = Image.open(target.image_path).convert("RGBA")
            available_width = max(1, width - CLICK_LOCATION_PADDING * 2)
            available_height = max(1, height - CLICK_LOCATION_PADDING * 2)
            scale = min(available_width / image.width, available_height / image.height)
            display_width = max(1, int(image.width * scale))
            display_height = max(1, int(image.height * scale))
            display_image = image.resize((display_width, display_height), Image.Resampling.NEAREST)
            self.detail_target_photo = ImageTk.PhotoImage(display_image)
            image_left = width // 2 - display_width // 2
            image_top = height // 2 - display_height // 2
            image_right = image_left + display_width
            image_bottom = image_top + display_height
            self.target_preview_image_bounds = (image_left, image_top, image_right, image_bottom)
            self.target_preview_canvas.create_image(width // 2, height // 2, image=self.detail_target_photo)
            click_x_ratio, click_y_ratio = target.click_point or (0.5, 0.5)
            marker_x = image_left + click_x_ratio * display_width
            marker_y = image_top + click_y_ratio * display_height
            self.target_preview_canvas.create_line(marker_x - 7, marker_y, marker_x + 7, marker_y, fill=COLOR_ACCENT, width=2)
            self.target_preview_canvas.create_line(marker_x, marker_y - 7, marker_x, marker_y + 7, fill=COLOR_ACCENT, width=2)
            self.target_preview_canvas.create_oval(marker_x - 4, marker_y - 4, marker_x + 4, marker_y + 4, outline=COLOR_ACCENT, width=2)
        except OSError:
            self.target_preview_canvas.create_text(width // 2, height // 2, text="Preview unavailable", fill=COLOR_MUTED, font=("", 8))

    def set_target_click_point_from_preview(self, event: tk.Event) -> None:
        target = self.selected_target()
        if not target or not self.target_preview_image_bounds:
            return
        left, top, right, bottom = self.target_preview_image_bounds
        if not (left <= event.x <= right and top <= event.y <= bottom):
            return
        x_ratio = (event.x - left) / max(1, right - left)
        y_ratio = (event.y - top) / max(1, bottom - top)
        target.click_point = (round(min(1.0, max(0.0, x_ratio)), 4), round(min(1.0, max(0.0, y_ratio)), 4))
        save_targets(self.targets)
        self.update_details_panel()
        log_event(f"Set click point for {target.name}: {target.click_point}")

    def reset_target_click_point(self) -> None:
        target = self.selected_target()
        if not target:
            return
        target.click_point = None
        save_targets(self.targets)
        self.update_details_panel()
        log_event(f"Reset click point for {target.name} to center")

    def open_crop_editor(self) -> None:
        target = self.selected_target()
        if not target:
            messagebox.showinfo(APP_NAME, "Select a target first.")
            return
        try:
            editor = CropEditor(self.root, target)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not open that target image: {exc}")
            return
        if not editor.result:
            return

        left, top, right, bottom = editor.result
        try:
            original = Image.open(target.image_path).convert("RGBA")
            old_width, old_height = original.size
            backup_path = Path(target.image_path).with_suffix(f".backup-{int(time.time())}.png")
            shutil.copy2(target.image_path, backup_path)
            cropped = original.crop((left, top, right, bottom))
            cropped.convert("RGB").save(target.image_path)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not save cropped target image: {exc}")
            return

        if target.click_point:
            old_x = target.click_point[0] * old_width
            old_y = target.click_point[1] * old_height
            if left <= old_x <= right and top <= old_y <= bottom:
                target.click_point = (
                    round((old_x - left) / max(1, right - left), 4),
                    round((old_y - top) / max(1, bottom - top), 4),
                )
            else:
                target.click_point = None

        target.template_cache = []
        target.template_cache_mode = None
        target.color_template_cache = None
        target.visible = False
        target.last_center = None
        target.repeat_click_count = 0
        target.repeat_diagnostic_reported = False
        self.thumbnail_refs.pop(target.id, None)
        self.tree.images.pop(target.id, None)
        self.tree.large_images = {
            cache_key: photo
            for cache_key, photo in self.tree.large_images.items()
            if cache_key[0] != target.id
        }
        save_targets(self.targets)
        self._refresh_target_list()
        self.update_details_panel()
        log_event(f"Cropped target {target.name}: {(left, top, right, bottom)} backup={backup_path}")

    def draw_search_area_preview(self, target: Optional[Target]) -> None:
        self.search_area_canvas.delete("all")
        self.detail_area_photo = None
        width = int(self.search_area_canvas["width"])
        height = int(self.search_area_canvas["height"])
        self.search_area_canvas.create_rectangle(0, 0, width, height, fill="#0f1720", outline="")
        if not target:
            self.search_area_text_var.set("Select a target")
            self.search_area_canvas.create_text(width // 2, height // 2, text="No target selected", fill=COLOR_MUTED, font=("", 8))
            return

        region = self.preview_region_for_target(target)
        if not region:
            self.search_area_text_var.set("Full window")
            self.search_area_canvas.create_text(width // 2, height // 2, text="Full window", fill=COLOR_MUTED, font=("", 8, "bold"))
            return

        left, top, right, bottom = region
        region_width = max(1, right - left)
        region_height = max(1, bottom - top)
        self.search_area_text_var.set(f"Custom Area\n{region_width}x{region_height}")
        pad_x = max(20, region_width // 2)
        pad_y = max(20, region_height // 2)
        screen_left = user32.GetSystemMetrics(76)
        screen_top = user32.GetSystemMetrics(77)
        screen_width = user32.GetSystemMetrics(78)
        screen_height = user32.GetSystemMetrics(79)
        screen_right = screen_left + screen_width
        screen_bottom = screen_top + screen_height
        capture = (
            max(screen_left, left - pad_x),
            max(screen_top, top - pad_y),
            min(screen_right, right + pad_x),
            min(screen_bottom, bottom + pad_y),
        )
        try:
            image = ImageGrab.grab(bbox=capture).convert("RGBA")
        except (OSError, ValueError):
            self.search_area_canvas.create_text(width // 2, height // 2, text="Preview unavailable", fill=COLOR_MUTED, font=("", 8))
            return

        overlay = Image.new("RGBA", image.size, (0, 0, 0, 95))
        image = Image.alpha_composite(image, overlay)
        draw = ImageDraw.Draw(image)
        selected = (left - capture[0], top - capture[1], right - capture[0], bottom - capture[1])
        self.draw_dashed_rect(draw, selected, fill=(226, 246, 255, 255), width=3, dash=10)
        handle = 8
        for x, y in (
            (selected[0], selected[1]),
            (selected[2], selected[1]),
            (selected[0], selected[3]),
            (selected[2], selected[3]),
            ((selected[0] + selected[2]) // 2, selected[1]),
            ((selected[0] + selected[2]) // 2, selected[3]),
        ):
            draw.rectangle((x - handle // 2, y - handle // 2, x + handle // 2, y + handle // 2), fill=(226, 246, 255, 255))
        image.thumbnail((width, height))
        self.detail_area_photo = ImageTk.PhotoImage(image)
        self.search_area_canvas.create_image(width // 2, height // 2, image=self.detail_area_photo)

    def draw_dashed_rect(
        self,
        draw: ImageDraw.ImageDraw,
        rect: tuple[int, int, int, int],
        fill: tuple[int, int, int, int],
        width: int = 2,
        dash: int = 8,
    ) -> None:
        left, top, right, bottom = rect
        for x in range(left, right, dash * 2):
            draw.line((x, top, min(x + dash, right), top), fill=fill, width=width)
            draw.line((x, bottom, min(x + dash, right), bottom), fill=fill, width=width)
        for y in range(top, bottom, dash * 2):
            draw.line((left, y, left, min(y + dash, bottom)), fill=fill, width=width)
            draw.line((right, y, right, min(y + dash, bottom)), fill=fill, width=width)

    def start_hotkey_listener(self) -> None:
        def on_press(key: keyboard.Key | keyboard.KeyCode | None) -> None:
            normalized = normalize_hotkey_key(key)
            if not normalized:
                return
            self.hotkey_pressed_keys.add(normalized)
            if set(self.hotkey).issubset(self.hotkey_pressed_keys) and not self.hotkey_triggered:
                self.hotkey_triggered = True
                self.root.after(0, self.toggle_running)

        def on_release(key: keyboard.Key | keyboard.KeyCode | None) -> None:
            normalized = normalize_hotkey_key(key)
            if not normalized:
                return
            self.hotkey_pressed_keys.discard(normalized)
            if not set(self.hotkey).issubset(self.hotkey_pressed_keys):
                self.hotkey_triggered = False

        try:
            self.hotkey_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
            self.hotkey_listener.daemon = True
            self.hotkey_listener.start()
            log_event(f"Started hotkey listener {hotkey_display(self.hotkey)}")
        except Exception as exc:
            self.hotkey_listener = None
            messagebox.showwarning(APP_NAME, f"Could not start the {hotkey_display(self.hotkey)} hotkey listener: {exc}")
            log_event(f"Failed to start hotkey listener {hotkey_display(self.hotkey)}: {exc}")

    def stop_hotkey_listener(self) -> None:
        if self.hotkey_listener:
            self.hotkey_listener.stop()
            self.hotkey_listener = None
            self.hotkey_pressed_keys.clear()
            self.hotkey_triggered = False
            log_event(f"Stopped hotkey listener {hotkey_display(self.hotkey)}")

    def capture_hotkey(self) -> None:
        self.stop_hotkey_listener()
        result = {"keys": set(), "pressed": set(), "done": False}
        dialog = tk.Toplevel(self.root)
        dialog.title("Set Hotkey")
        dialog.configure(bg=COLOR_BG)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)
        apply_dialog_chrome_style(dialog)

        body = tk.Frame(dialog, bg=COLOR_PANEL, highlightthickness=1, highlightbackground=COLOR_BORDER)
        body.pack(fill="both", expand=True, padx=10, pady=10)
        body.columnconfigure(0, weight=1)
        tk.Label(body, text="Set Hotkey", font=("", 12, "bold"), bg=COLOR_PANEL, fg=COLOR_TEXT).grid(row=0, column=0, sticky="w", padx=14, pady=(14, 8))
        tk.Label(body, text="Press the key combination, then release.", bg=COLOR_PANEL, fg=COLOR_MUTED, font=("", 9)).grid(row=1, column=0, sticky="w", padx=14)
        capture_var = tk.StringVar(value=hotkey_display(self.hotkey))
        tk.Label(body, textvariable=capture_var, bg=COLOR_PANEL_2, fg=COLOR_TEXT, font=("", 12, "bold"), anchor="center", highlightthickness=1, highlightbackground=COLOR_BORDER).grid(row=2, column=0, sticky="ew", padx=14, pady=(12, 0), ipady=10)
        button_row = tk.Frame(body, bg=COLOR_PANEL)
        button_row.grid(row=3, column=0, sticky="e", padx=14, pady=(14, 14))

        def finish(keys: set[str]) -> None:
            if result["done"]:
                return
            result["done"] = True
            listener.stop()
            if keys:
                self.hotkey = tuple(sorted(keys, key=lambda key: {"ctrl": 0, "alt": 1, "shift": 2, "win": 3}.get(key, 10)))
                save_hotkey(self.hotkey)
                self.hotkey_var.set(hotkey_display(self.hotkey))
                log_event(f"Set hotkey to {hotkey_display(self.hotkey)}")
            dialog.destroy()

        def cancel() -> None:
            if result["done"]:
                return
            result["done"] = True
            listener.stop()
            dialog.destroy()

        RoundedButton(button_row, text="Cancel", command=cancel, variant="outline_accent", width=104, height=30, bg=COLOR_PANEL).pack(side="left")

        def on_press(key: keyboard.Key | keyboard.KeyCode | None) -> None:
            normalized = normalize_hotkey_key(key)
            if not normalized:
                return
            result["pressed"].add(normalized)
            result["keys"].add(normalized)
            self.root.after(0, lambda: capture_var.set(hotkey_display(tuple(result["keys"]))))

        def on_release(key: keyboard.Key | keyboard.KeyCode | None) -> None:
            normalized = normalize_hotkey_key(key)
            if normalized:
                result["pressed"].discard(normalized)
            if result["keys"] and not result["pressed"]:
                self.root.after(0, lambda: finish(set(result["keys"])))

        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        listener.daemon = True
        listener.start()

        dialog.update_idletasks()
        width = max(380, dialog.winfo_reqwidth())
        height = dialog.winfo_reqheight()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2
        dialog.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        self.root.wait_window(dialog)
        self.start_hotkey_listener()

    def on_close(self) -> None:
        self.stop_hotkey_listener()
        self.area_preview.hide()
        self.root.destroy()

    def update_bound_window_label(self) -> None:
        if self.bound_window:
            label = self.bound_window.title or self.bound_window.class_name or "Selected window"
            if len(label) > 42:
                label = label[:39] + "..."
            self.bound_window_var.set(label)
        else:
            self.bound_window_var.set("None")

    def set_bound_window(self, anchor: Optional[WindowAnchor]) -> None:
        self.bound_window = anchor
        save_bound_window(anchor)
        self.update_bound_window_label()
        if anchor:
            log_event(
                f"Bound window hwnd={anchor.hwnd} title={anchor.title!r} "
                f"class={anchor.class_name!r} rect={anchor.rect}"
            )
        else:
            log_event("Cleared bound window; scanning whole screen")

    def change_bound_window(self) -> None:
        self.area_preview.hide()
        self.root.withdraw()

        def selected(anchor: Optional[WindowAnchor]) -> None:
            self.set_bound_window(anchor)
            self.root.deiconify()

        WindowSelector(self.root, selected, self.root.deiconify)

    def with_bound_window(self, callback: Callable[[WindowAnchor], None]) -> None:
        if self.bound_window:
            callback(self.bound_window)
            return

        self.root.withdraw()

        def selected(anchor: Optional[WindowAnchor]) -> None:
            if anchor:
                self.set_bound_window(anchor)
                callback(anchor)
            else:
                self.set_bound_window(None)
                self.root.deiconify()

        WindowSelector(self.root, selected, self.root.deiconify)

    def selected_target(self) -> Optional[Target]:
        selected = self.tree.selection()
        if not selected:
            return None
        target_id = selected[0]
        return next((target for target in self.targets if target.id == target_id), None)

    def on_tree_selection_changed(self, _: tk.Event) -> None:
        self.update_details_panel()
        if self.suppress_selection_preview:
            return
        target = self.selected_target()
        preview_region = self.preview_region_for_target(target) if target else None
        if preview_region and not self.running:
            self.area_preview.show(preview_region)
        else:
            self.area_preview.hide()

    def preview_region_for_target(self, target: Target) -> Optional[tuple[int, int, int, int]]:
        if target.search_region_relative:
            window_rect = find_window_rect_for_target(target)
            if window_rect:
                return region_absolute_from_window(target.search_region_relative, window_rect)
        return target.search_region

    def _target_thumbnail(self, target: Target) -> Optional[ImageTk.PhotoImage]:
        if target.id in self.thumbnail_refs:
            return self.thumbnail_refs[target.id]
        try:
            image = Image.open(target.image_path)
            image.thumbnail((72, 40))
            photo = ImageTk.PhotoImage(image)
            self.thumbnail_refs[target.id] = photo
            return photo
        except OSError:
            return None

    def _refresh_target_list(self) -> None:
        selected = self.tree.selection()
        selected_id = selected[0] if selected else None
        if not selected_id and self.targets and not self.initial_selection_done:
            selected_id = self.targets[0].id
            self.initial_selection_done = True
        count = len(self.targets)
        self.target_count_var.set(f"{count} target" if count == 1 else f"{count} targets")

        self.suppress_selection_preview = True
        try:
            for item in self.tree.get_children():
                self.tree.delete(item)

            enabled_targets = [target for target in self.targets if target.enabled]
            if enabled_targets and self.scan_start_index >= len(enabled_targets):
                self.scan_start_index = 0
            scan_cursor_id = enabled_targets[self.scan_start_index].id if enabled_targets else None

            for target in self.targets:
                photo = self._target_thumbnail(target)
                area_text = "Full window"
                if target.search_region:
                    left, top, right, bottom = target.search_region
                    prefix = "Window" if target.search_region_relative else "Screen"
                    area_text = f"{prefix} {right - left}x{bottom - top}"
                self.tree.insert(
                    "",
                    "end",
                    iid=target.id,
                    image=photo,
                    values=(
                        "Yes" if target.enabled else "No",
                        target.name,
                        target.click_type,
                        "Multi" if target.multi_scale else "100%",
                        area_text,
                        f"{target.threshold:.0%}",
                        target.last_status,
                        "scan_cursor" if target.id == scan_cursor_id else "",
                    ),
                    image_path=target.image_path,
                )

            if selected_id and self.tree.exists(selected_id):
                self.tree.selection_set(selected_id)
                selected_target = self.selected_target()
                if selected_target:
                    self.threshold_var.set(selected_target.threshold)
            self.update_details_panel()
        finally:
            self.suppress_selection_preview = False

    def _save_and_refresh(self) -> None:
        save_targets(self.targets)
        self._refresh_target_list()

    def show_add_target_menu(self) -> None:
        if self.running:
            self.toggle_running()
            self.detail_var.set("Scanning stopped to add a target.")
        menu = tk.Menu(self.root, tearoff=False, bg=COLOR_PANEL, fg=COLOR_TEXT, activebackground=COLOR_ACCENT, activeforeground="white")
        menu.add_command(label="Snip Target", command=self.add_from_snip)
        menu.add_command(label="Paste Image", command=self.add_from_clipboard)
        menu.add_command(label="Add File", command=self.add_from_file)
        try:
            menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())
        finally:
            menu.grab_release()

    def on_targets_reordered(self, _: tk.Event) -> None:
        order = self.tree.get_children()
        by_id = {target.id: target for target in self.targets}
        self.targets = [by_id[target_id] for target_id in order if target_id in by_id]
        save_targets(self.targets)
        self._refresh_target_list()

    def close_cell_editor(self) -> None:
        if self.cell_editor:
            self.cell_editor.destroy()
            self.cell_editor = None

    def cell_geometry(self, target_id: str, column: str) -> Optional[tuple[int, int, int, int]]:
        bounds = self.tree.cell_bounds.get((target_id, column))
        if not bounds:
            return None
        left, top, right, bottom = bounds
        y_offset = int(self.tree.canvas.canvasy(0))
        return left + 4, top - y_offset + 4, max(40, right - left - 8), max(24, bottom - top - 8)

    def on_target_cell_click(self, event: tk.Event) -> None:
        self.close_cell_editor()
        target = self.selected_target()
        if not target:
            return

        column = getattr(event, "column_name", None)
        if column == "enabled":
            self.toggle_selected_target()
        elif column == "click":
            self.edit_target_click_type(target)
        elif column == "scale":
            target.multi_scale = not target.multi_scale
            target.template_cache = []
            target.template_cache_mode = None
            target.color_template_cache = None
            save_targets(self.targets)
            self._refresh_target_list()
        elif column == "area":
            self.select_area_for_target()

    def edit_target_name(self, target: Target) -> None:
        geometry = self.cell_geometry(target.id, "name")
        if not geometry:
            return
        x, y, width, height = geometry
        name_var = tk.StringVar(value=target.name)
        editor = ttk.Entry(self.tree.canvas, textvariable=name_var)
        self.cell_editor = editor
        window_id = self.tree.canvas.create_window(x, y, width=width, height=height, anchor="nw", window=editor)

        def commit(_: Optional[tk.Event] = None) -> None:
            new_name = name_var.get().strip()
            if new_name:
                target.name = new_name
                save_targets(self.targets)
            self.tree.canvas.delete(window_id)
            self.cell_editor = None
            self._refresh_target_list()

        def cancel(_: Optional[tk.Event] = None) -> None:
            self.tree.canvas.delete(window_id)
            self.cell_editor = None
            self._refresh_target_list()

        editor.bind("<Return>", commit)
        editor.bind("<FocusOut>", commit)
        editor.bind("<Escape>", cancel)
        editor.focus_set()
        editor.selection_range(0, "end")

    def edit_target_click_type(self, target: Target) -> None:
        geometry = self.cell_geometry(target.id, "click")
        if not geometry:
            return
        x, y, width, height = geometry
        click_var = tk.StringVar(value=target.click_type)
        editor = ttk.Combobox(
            self.tree.canvas,
            textvariable=click_var,
            values=CLICK_TYPES,
            state="readonly",
        )
        self.cell_editor = editor
        window_id = self.tree.canvas.create_window(x, y, width=width, height=height, anchor="nw", window=editor)

        def commit(_: Optional[tk.Event] = None) -> None:
            click_type = click_var.get()
            if click_type in CLICK_TYPES:
                target.click_type = click_type
                save_targets(self.targets)
            self.tree.canvas.delete(window_id)
            self.cell_editor = None
            self._refresh_target_list()

        def cancel(_: Optional[tk.Event] = None) -> None:
            self.tree.canvas.delete(window_id)
            self.cell_editor = None
            self._refresh_target_list()

        editor.bind("<<ComboboxSelected>>", commit)
        editor.bind("<FocusOut>", commit)
        editor.bind("<Escape>", cancel)
        editor.focus_set()
        editor.event_generate("<Button-1>")

    def add_target_image(
        self,
        image: Image.Image,
        name: str,
        search_region: Optional[tuple[int, int, int, int]] = None,
        search_region_relative: Optional[tuple[int, int, int, int]] = None,
        window_anchor: Optional[WindowAnchor] = None,
    ) -> Target:
        ensure_dirs()
        target_id = str(uuid.uuid4())
        image_path = TARGET_DIR / f"{target_id}.png"
        image.convert("RGB").save(image_path)
        target = Target(
            id=target_id,
            name=name,
            image_path=str(image_path),
            search_region=search_region,
            search_region_relative=search_region_relative,
            search_window_title=window_anchor.title if window_anchor else "",
            search_window_class=window_anchor.class_name if window_anchor else "",
            search_window_handle=window_anchor.hwnd if window_anchor else None,
        )
        self.targets.append(target)
        self._save_and_refresh()
        return target

    def add_from_clipboard(self) -> None:
        clipboard = ImageGrab.grabclipboard()
        if not isinstance(clipboard, Image.Image):
            messagebox.showerror(APP_NAME, "The clipboard does not contain an image.")
            return

        dialog = TargetDialog(self.root, "Paste Target")
        if dialog.result:
            self.add_target_image(clipboard, dialog.result)

    def add_from_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose target image",
            filetypes=[
                ("Image files", "*.png *.jpg *.jpeg *.bmp *.webp"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return

        try:
            image = Image.open(path)
        except OSError:
            messagebox.showerror(APP_NAME, "Could not open that image file.")
            return

        default_name = Path(path).stem
        dialog = TargetDialog(self.root, "Add Target", default_name=default_name)
        if dialog.result:
            self.add_target_image(image, dialog.result)

    def add_from_snip(self) -> None:
        def start_region_selector(anchor: WindowAnchor) -> None:
            self.root.withdraw()

            def apply_region(region: tuple[int, int, int, int]) -> None:
                self.root.deiconify()
                try:
                    image = ImageGrab.grab(bbox=region)
                except OSError as exc:
                    messagebox.showerror(APP_NAME, f"Could not capture that screen area: {exc}")
                    return

                dialog = TargetDialog(self.root, "Snip Target", show_use_region=True, use_region_default=True)
                if dialog.result:
                    search_region = region if dialog.use_region else None
                    search_region_relative = None
                    window_anchor = None
                    if search_region:
                        anchor_rect = find_window_rect_for_anchor(anchor) or anchor.rect
                        search_region_relative = region_relative_to_window(region, anchor_rect)
                        window_anchor = anchor
                    target = self.add_target_image(
                        image,
                        dialog.result,
                        search_region=search_region,
                        search_region_relative=search_region_relative,
                        window_anchor=window_anchor,
                    )
                    self.tree.selection_set(target.id)
                    if search_region:
                        self.area_preview.show(search_region)
                    log_event(
                        f"Snipped target {dialog.result}: {region} "
                        f"search_region={bool(search_region)} anchor={find_window_rect_for_anchor(anchor) or anchor.rect} "
                        f"relative={search_region_relative}"
                    )

            RegionSelector(self.root, apply_region, self.root.deiconify)

        self.root.after(100, lambda: self.with_bound_window(start_region_selector))

    def toggle_running(self) -> None:
        self.running = not self.running
        if self.running:
            self.area_preview.hide()
            self.scan_count = 0
            self.scan_start_index = 0
            self.pending_click = None
            self.last_cursor_pos = cursor_position()
            for target in self.targets:
                target.visible = False
                target.last_center = None
                target.repeat_click_count = 0
                target.repeat_diagnostic_reported = False
                target.last_status = "Searching"
            self.start_button.set_text("Stop")
            self.start_button.set_variant("outline_danger")
            self.status_var.set("Searching")
            self.detail_var.set("Switch focus to the window you want scanned.")
            log_event("Started scanning")
            self._refresh_target_list()
            self.root.after(100, self.scan_once)
        else:
            self.pending_click = None
            self.start_button.set_text("Start")
            self.start_button.set_variant("outline_accent")
            self.status_var.set("Stopped")
            self.detail_var.set("Scanning paused.")
            for target in self.targets:
                target.visible = False
                target.last_center = None
                target.repeat_click_count = 0
                target.repeat_diagnostic_reported = False
                target.last_status = "Idle"
            log_event("Stopped scanning")
            self._refresh_target_list()

    def toggle_selected_target(self) -> None:
        target = self.selected_target()
        if not target:
            return
        target.enabled = not target.enabled
        target.visible = False
        target.last_center = None
        target.repeat_click_count = 0
        target.repeat_diagnostic_reported = False
        if self.pending_click and self.pending_click["target_id"] == target.id:
            self.pending_click = None
        target.last_status = "Enabled" if target.enabled else "Disabled"
        self._save_and_refresh()

    def rename_selected_target(self) -> None:
        target = self.selected_target()
        if not target:
            return
        dialog = TargetDialog(self.root, "Rename Target", default_name=target.name)
        if dialog.result:
            target.name = dialog.result
            self._save_and_refresh()

    def select_area_for_target(self) -> None:
        target = self.selected_target()
        if not target:
            messagebox.showinfo(APP_NAME, "Select a target first.")
            return

        def start_region_selector(anchor: WindowAnchor) -> None:
            self.root.withdraw()

            def apply_region(region: tuple[int, int, int, int]) -> None:
                anchor_rect = find_window_rect_for_anchor(anchor) or anchor.rect
                target.search_region = region
                target.search_region_relative = region_relative_to_window(region, anchor_rect)
                target.search_window_title = anchor.title
                target.search_window_class = anchor.class_name
                target.search_window_handle = anchor.hwnd
                target.visible = False
                target.last_center = None
                target.repeat_click_count = 0
                target.repeat_diagnostic_reported = False
                target.last_status = "Area selected"
                log_event(
                    f"Selected area for {target.name}: {region} "
                    f"anchor={anchor_rect} relative={target.search_region_relative}"
                )
                self.root.deiconify()
                self._save_and_refresh()
                if not self.running:
                    self.area_preview.show(region)

            RegionSelector(self.root, apply_region, self.root.deiconify)

        self.root.after(100, lambda: self.with_bound_window(start_region_selector))

    def clear_area_for_target(self) -> None:
        target = self.selected_target()
        if not target:
            return
        target.search_region = None
        target.search_region_relative = None
        target.search_window_title = ""
        target.search_window_class = ""
        target.search_window_handle = None
        target.visible = False
        target.last_center = None
        target.repeat_click_count = 0
        target.repeat_diagnostic_reported = False
        if self.pending_click and self.pending_click["target_id"] == target.id:
            self.pending_click = None
        target.last_status = "Full window"
        log_event(f"Cleared area for {target.name}")
        self.area_preview.hide()
        self._save_and_refresh()

    def ask_dark_confirmation(self, title: str, message: str, confirm_text: str, danger: bool = False) -> bool:
        result = {"confirmed": False}
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.configure(bg=COLOR_BG)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)
        apply_dialog_chrome_style(dialog)

        body = tk.Frame(dialog, bg=COLOR_PANEL, highlightthickness=1, highlightbackground=COLOR_BORDER)
        body.pack(fill="both", expand=True, padx=10, pady=10)
        body.columnconfigure(0, weight=1)
        tk.Label(body, text=title, font=("", 12, "bold"), bg=COLOR_PANEL, fg=COLOR_TEXT).grid(row=0, column=0, sticky="w", padx=14, pady=(14, 7))
        tk.Label(
            body,
            text=message,
            bg=COLOR_PANEL,
            fg=COLOR_MUTED,
            justify="left",
            anchor="w",
            wraplength=374,
            font=("", 9),
        ).grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 14))

        button_row = tk.Frame(body, bg=COLOR_PANEL)
        button_row.grid(row=2, column=0, sticky="e", padx=14, pady=(0, 14))

        def cancel() -> None:
            result["confirmed"] = False
            dialog.destroy()

        def confirm() -> None:
            result["confirmed"] = True
            dialog.destroy()

        RoundedButton(button_row, text="Cancel", command=cancel, variant="outline_accent", width=104, height=30, bg=COLOR_PANEL).pack(side="left", padx=(0, 8))
        RoundedButton(
            button_row,
            text=confirm_text,
            command=confirm,
            variant="outline_danger" if danger else "outline_accent",
            width=128,
            height=30,
            bg=COLOR_PANEL,
        ).pack(side="left")

        dialog.update_idletasks()
        width = max(430, dialog.winfo_reqwidth())
        height = dialog.winfo_reqheight()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2
        dialog.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        self.root.wait_window(dialog)
        return result["confirmed"]

    def arm_delete_all(self) -> None:
        if not self.targets:
            return
        self.delete_all_mode = True
        self.delete_button.set_text("Delete All")
        log_event("Delete All armed by long press")

    def reset_delete_button(self) -> None:
        self.delete_all_mode = False
        self.delete_button.set_text("Delete")

    def handle_delete_button(self) -> None:
        if self.delete_all_mode:
            self.delete_all_targets()
        else:
            self.delete_selected_target()

    def delete_selected_target(self) -> None:
        target = self.selected_target()
        if not target:
            return
        if not self.ask_dark_confirmation(
            "Delete Target",
            f"Delete '{target.name}'?\n\nThis only removes the selected target.",
            "Delete",
            danger=True,
        ):
            return
        self.targets = [item for item in self.targets if item.id != target.id]
        if self.pending_click and self.pending_click["target_id"] == target.id:
            self.pending_click = None
        try:
            Path(target.image_path).unlink(missing_ok=True)
        except OSError:
            pass
        self.thumbnail_refs.pop(target.id, None)
        self._save_and_refresh()

    def delete_all_targets(self) -> None:
        count = len(self.targets)
        try:
            if not self.ask_dark_confirmation(
                "Delete All Targets",
                f"The Delete button is currently in Delete All mode.\n\nThis will delete all {count} targets and their saved images for a fresh start. This cannot be undone.",
                "Delete All",
                danger=True,
            ):
                return
            for target in self.targets:
                try:
                    Path(target.image_path).unlink(missing_ok=True)
                except OSError:
                    pass
            self.targets = []
            self.thumbnail_refs.clear()
            self.pending_click = None
            self.area_preview.hide()
            save_targets(self.targets)
            self._refresh_target_list()
            log_event("Deleted all targets")
        finally:
            self.reset_delete_button()

    def on_threshold_changed(self, _: str) -> None:
        if self.detail_updating:
            return
        target = self.selected_target()
        if not target:
            return
        target.threshold = round(float(self.threshold_var.get()), 2)
        self.threshold_label_var.set(f"Match Threshold ({target.threshold:.0%})")
        save_targets(self.targets)
        self.threshold_slider.redraw()
        self._refresh_target_list()

    def scan_once(self) -> None:
        if not self.running:
            return

        try:
            self._scan_active_window()
        except Exception as exc:
            self.status_var.set("Error")
            self.detail_var.set(str(exc))
            log_event(f"Error: {exc}\n{traceback.format_exc()}")

        self._refresh_target_list()
        self.root.after(SCAN_INTERVAL_MS, self.scan_once)

    def cursor_is_moving(self) -> bool:
        current_pos = cursor_position()
        distance = abs(current_pos[0] - self.last_cursor_pos[0]) + abs(current_pos[1] - self.last_cursor_pos[1])
        self.last_cursor_pos = current_pos
        return distance > MOUSE_MOVE_TOLERANCE_PX

    def perform_pending_click(self) -> Optional[str]:
        if not self.pending_click:
            return None

        target = next((item for item in self.targets if item.id == self.pending_click["target_id"]), None)
        if not target or not target.enabled:
            self.pending_click = None
            return None

        click_x, click_y = self.pending_click["click_point"]
        click_point(click_x, click_y, self.pending_click["click_type"])
        was_visible = target.visible
        target.last_center = self.pending_click["match_center"]
        target.last_confidence = self.pending_click["confidence"]
        target.last_status = "Clicked"
        target.visible = True
        self.update_repeat_click_state(
            target=target,
            was_visible=was_visible,
            confidence=self.pending_click["confidence"],
            match_box=self.pending_click.get("match_box"),
            capture_rect=self.pending_click.get("capture_rect"),
            click_point_xy=(click_x, click_y),
            capture_image=None,
            pending=True,
        )
        clicked_name = target.name
        log_event(
            f"Clicked pending {target.name} at {click_x},{click_y} "
            f"type={self.pending_click['click_type']} confidence={self.pending_click['confidence']:.3f}"
        )
        self.pending_click = None
        return clicked_name

    def update_repeat_click_state(
        self,
        target: Target,
        was_visible: bool,
        confidence: float,
        match_box: Optional[tuple[int, int, int, int]],
        capture_rect: Optional[tuple[int, int, int, int]],
        click_point_xy: tuple[int, int],
        capture_image: Optional[Image.Image],
        pending: bool,
    ) -> None:
        target.repeat_click_count = target.repeat_click_count + 1 if was_visible else 1
        if target.repeat_click_count < REPEAT_DIAGNOSTIC_THRESHOLD:
            target.repeat_diagnostic_reported = False
            return
        if target.repeat_diagnostic_reported:
            return
        target.repeat_diagnostic_reported = True
        self.write_repeat_diagnostic(
            target=target,
            confidence=confidence,
            match_box=match_box,
            capture_rect=capture_rect,
            click_point_xy=click_point_xy,
            capture_image=capture_image,
            pending=pending,
        )

    def write_repeat_diagnostic(
        self,
        target: Target,
        confidence: float,
        match_box: Optional[tuple[int, int, int, int]],
        capture_rect: Optional[tuple[int, int, int, int]],
        click_point_xy: tuple[int, int],
        capture_image: Optional[Image.Image],
        pending: bool,
    ) -> None:
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        folder = DIAGNOSTIC_DIR / f"{timestamp}_{safe_filename(target.name)}"
        folder.mkdir(parents=True, exist_ok=True)

        template_size = None
        try:
            template = Image.open(target.image_path).convert("RGB")
            template_size = template.size
            template.save(folder / "template.png")
        except OSError:
            template = None

        before_path = None
        crop_path = None
        if capture_image and match_box:
            before = capture_image.convert("RGB")
            draw = ImageDraw.Draw(before)
            x, y, width, height = match_box
            draw.rectangle((x, y, x + width, y + height), outline="red", width=3)
            if capture_rect:
                local_click = (click_point_xy[0] - capture_rect[0], click_point_xy[1] - capture_rect[1])
                draw.line((local_click[0] - 8, local_click[1], local_click[0] + 8, local_click[1]), fill="yellow", width=2)
                draw.line((local_click[0], local_click[1] - 8, local_click[0], local_click[1] + 8), fill="yellow", width=2)
            before_path = "capture_before.png"
            before.save(folder / before_path)
            crop = capture_image.crop((
                max(0, x - 24),
                max(0, y - 24),
                min(capture_image.width, x + width + 24),
                min(capture_image.height, y + height + 24),
            ))
            crop_path = "match_crop_before.png"
            crop.save(folder / crop_path)

        after_path = None
        if capture_rect:
            try:
                time.sleep(0.15)
                after = ImageGrab.grab(bbox=capture_rect).convert("RGB")
                after_path = "capture_after.png"
                after.save(folder / after_path)
            except (OSError, ValueError):
                after_path = None

        active_hwnd = user32.GetForegroundWindow()
        payload = {
            "timestamp": timestamp,
            "reason": f"Target clicked {target.repeat_click_count} times in a row without being observed absent.",
            "target": {
                "id": target.id,
                "name": target.name,
                "threshold": target.threshold,
                "confidence": confidence,
                "click_type": target.click_type,
                "custom_click_point": list(target.click_point) if target.click_point else None,
                "repeat_click_count": target.repeat_click_count,
                "pending_click": pending,
                "template_size": list(template_size) if template_size else None,
                "image_path": target.image_path,
            },
            "match": {
                "match_box": list(match_box) if match_box else None,
                "capture_rect": list(capture_rect) if capture_rect else None,
                "capture_size": [capture_rect[2] - capture_rect[0], capture_rect[3] - capture_rect[1]] if capture_rect else None,
                "click_point": list(click_point_xy),
            },
            "window": {
                "bound_title": self.bound_window.title if self.bound_window else None,
                "bound_class": self.bound_window.class_name if self.bound_window else None,
                "bound_handle": self.bound_window.hwnd if self.bound_window else None,
                "active_title": window_title(active_hwnd) if active_hwnd else "",
                "active_class": window_class_name(active_hwnd) if active_hwnd else "",
            },
            "files": {
                "template": "template.png" if template_size else None,
                "capture_before": before_path,
                "match_crop_before": crop_path,
                "capture_after": after_path,
            },
        }
        (folder / "diagnostic.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        log_event(f"Diagnostic saved for {target.name}: {folder}")

    def _scan_active_window(self) -> None:
        self.area_preview.hide()
        enabled_targets = [target for target in self.targets if target.enabled]
        if not enabled_targets:
            self.status_var.set("No enabled targets")
            self.detail_var.set("Enable at least one saved target.")
            return
        if self.scan_start_index >= len(enabled_targets):
            self.scan_start_index = 0

        cursor_moving = self.cursor_is_moving()
        if self.pending_click:
            if cursor_moving:
                self.status_var.set(f"Pending: {self.pending_click['name']}")
                self.detail_var.set(f"Scans: {self.scan_count} | Waiting for cursor to stop")
                return

            clicked_name = self.perform_pending_click()
            if clicked_name:
                self.status_var.set(f"Clicked: {clicked_name}")
                self.detail_var.set(f"Scans: {self.scan_count} | Clicked pending match")
                return

        scan_window_rect = None
        full_window_screenshot_gray = None
        targets_need_full_window = any(
            target.search_region is None
            or (
                target.search_region_relative is not None
                and not (target.search_window_title or target.search_window_class)
            )
            for target in enabled_targets
        )
        if targets_need_full_window:
            if self.bound_window:
                scan_window_rect = find_window_rect_for_anchor(self.bound_window)
            else:
                scan_window_rect = virtual_screen_rect()

        if targets_need_full_window and scan_window_rect is None:
            self.status_var.set("Waiting")
            self.detail_var.set("Selected window unavailable.")
            return

        self.scan_count += 1
        clicked_name = None
        best_overall = 0.0
        target = enabled_targets[self.scan_start_index]
        self.scan_start_index = (self.scan_start_index + 1) % len(enabled_targets)

        if target.search_region_relative:
            window_rect = find_window_rect_for_target(target)
            if window_rect is None and not (target.search_window_title or target.search_window_class):
                window_rect = scan_window_rect
            if window_rect is None:
                target.last_status = "Window missing"
                self.status_var.set("Searching")
                self.detail_var.set(f"Scans: {self.scan_count} | {target.name}: window missing")
                return
            capture_rect = region_absolute_from_window(target.search_region_relative, window_rect)
            if capture_rect[2] <= capture_rect[0] or capture_rect[3] <= capture_rect[1]:
                target.last_status = "Bad area"
                self.status_var.set("Searching")
                self.detail_var.set(f"Scans: {self.scan_count} | {target.name}: bad area")
                return
            capture_rect = expand_capture_rect(capture_rect)
            capture_image = ImageGrab.grab(bbox=capture_rect)
            screenshot_gray = pil_to_gray(capture_image)
        elif target.search_region:
            capture_rect = target.search_region
            if capture_rect[2] <= capture_rect[0] or capture_rect[3] <= capture_rect[1]:
                target.last_status = "Bad area"
                self.status_var.set("Searching")
                self.detail_var.set(f"Scans: {self.scan_count} | {target.name}: bad area")
                return
            capture_rect = expand_capture_rect(capture_rect)
            capture_image = ImageGrab.grab(bbox=capture_rect)
            screenshot_gray = pil_to_gray(capture_image)
        else:
            capture_rect = scan_window_rect
            capture_image = ImageGrab.grab(bbox=scan_window_rect)
            screenshot_gray = pil_to_gray(capture_image)

        confidence, box = coarse_to_fine_match(screenshot_gray, target)
        target.last_confidence = confidence
        best_overall = confidence

        if confidence >= target.threshold and box:
            color_similarity = color_match_similarity(capture_image, target, box)
            if color_similarity < COLOR_VERIFY_THRESHOLD:
                confidence, verified_box, verified_color = best_color_verified_match(
                    screenshot_gray,
                    capture_image,
                    target,
                    target.threshold,
                )
                if not verified_box:
                    target.last_confidence = min(confidence, verified_color)
                    best_overall = target.last_confidence
                    target.visible = False
                    target.last_center = None
                    target.repeat_click_count = 0
                    target.repeat_diagnostic_reported = False
                    target.last_status = "Color mismatch"
                    self.detail_var.set(
                        f"Scans: {self.scan_count} | {target.name}: color {verified_color:.3f}, shape {confidence:.3f}"
                    )
                    self.status_var.set("Searching")
                    return
                box = verified_box
                color_similarity = verified_color
                target.last_confidence = confidence
                best_overall = confidence
            x, y, width, height = box
            center = (capture_rect[0] + x + width // 2, capture_rect[1] + y + height // 2)
            was_visible = target.visible
            target.last_status = "Visible"
            click_x, click_y = click_point_for_match(capture_rect, box, target)
            target.last_center = center
            target.visible = True
            if cursor_moving:
                self.pending_click = {
                    "target_id": target.id,
                    "name": target.name,
                    "match_center": center,
                    "click_point": (click_x, click_y),
                    "confidence": confidence,
                    "click_type": target.click_type,
                    "match_box": box,
                    "capture_rect": capture_rect,
                }
                target.last_status = "Pending"
                clicked_name = f"Pending: {target.name}"
                log_event(
                    f"Pending {target.name} at {click_x},{click_y} "
                    f"type={target.click_type} confidence={confidence:.3f}"
                )
            else:
                click_point(click_x, click_y, target.click_type)
                target.last_status = "Clicked"
                self.update_repeat_click_state(
                    target=target,
                    was_visible=was_visible,
                    confidence=confidence,
                    match_box=box,
                    capture_rect=capture_rect,
                    click_point_xy=(click_x, click_y),
                    capture_image=capture_image,
                    pending=False,
                )
                clicked_name = target.name
                log_event(
                    f"Clicked {target.name} at {click_x},{click_y} "
                    f"type={target.click_type} confidence={confidence:.3f}"
                )
        elif confidence < max(0.0, target.threshold - DISAPPEAR_MARGIN):
            target.visible = False
            target.last_center = None
            target.repeat_click_count = 0
            target.repeat_diagnostic_reported = False
            target.last_status = "Searching"
        else:
            target.last_status = "Near"

        if clicked_name:
            if clicked_name.startswith("Pending: "):
                self.status_var.set(clicked_name)
            else:
                self.status_var.set(f"Clicked: {clicked_name}")
        else:
            self.status_var.set("Searching")
        self.detail_var.set(f"Scans: {self.scan_count} | Best confidence: {best_overall:.3f}")


def main() -> None:
    enable_dpi_awareness()
    ensure_dirs()
    root = tk.Tk()
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    style.configure("App.TFrame", background=COLOR_BG)
    style.configure("Toolbar.TFrame", background=COLOR_BG)
    style.configure("Panel.TFrame", background=COLOR_PANEL, bordercolor=COLOR_BORDER, relief="solid")
    style.configure("TLabel", background=COLOR_BG, foreground=COLOR_TEXT)
    style.configure("Muted.TLabel", background=COLOR_BG, foreground=COLOR_MUTED)
    style.configure("PanelTitle.TLabel", background=COLOR_PANEL, foreground=COLOR_TEXT)
    style.configure("MutedPanel.TLabel", background=COLOR_PANEL, foreground=COLOR_MUTED)
    style.configure("Status.TLabel", background=COLOR_BG, foreground=COLOR_TEXT)
    style.configure("TButton", padding=(12, 8), background=COLOR_PANEL, foreground=COLOR_TEXT, bordercolor=COLOR_BORDER)
    style.map("TButton", background=[("active", COLOR_PANEL_2)], foreground=[("disabled", COLOR_MUTED)])
    style.configure("Tool.TButton", padding=(12, 8), background=COLOR_PANEL, foreground=COLOR_TEXT, bordercolor=COLOR_BORDER)
    style.map("Tool.TButton", background=[("active", COLOR_PANEL_2)])
    style.configure("Accent.TButton", padding=(16, 8), background=COLOR_ACCENT, foreground="white", bordercolor=COLOR_ACCENT)
    style.map("Accent.TButton", background=[("active", "#1f78e0")])
    style.configure("Danger.TButton", padding=(12, 8), background="#3a1518", foreground="#fecaca", bordercolor=COLOR_DANGER)
    style.map("Danger.TButton", background=[("active", "#4a1c20")])
    style.configure("TScale", background=COLOR_BG, troughcolor=COLOR_BORDER)
    style.configure("Vertical.TScrollbar", background=COLOR_PANEL, troughcolor=COLOR_BG, bordercolor=COLOR_BORDER, arrowcolor=COLOR_MUTED)
    style.configure("TEntry", fieldbackground=COLOR_PANEL_2, foreground=COLOR_TEXT, bordercolor=COLOR_BORDER)
    style.configure("TCombobox", fieldbackground=COLOR_PANEL_2, foreground=COLOR_TEXT, background=COLOR_PANEL, bordercolor=COLOR_BORDER)
    ClickerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
