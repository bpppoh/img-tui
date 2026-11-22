from __future__ import annotations

import curses
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ModeTheme:
    key: str
    name: str
    tier: str
    badge: str
    colors: dict[str, str]
    description: str = ""


ROLE_NAMES = (
    "border",
    "focus",
    "accent",
    "highlight",
    "selected",
    "muted",
    "ok",
    "warn",
    "error",
    "fill",
)
ROLE_PAIR = {role: index + 1 for index, role in enumerate(ROLE_NAMES)}


MODE_THEMES: tuple[ModeTheme, ...] = (
    ModeTheme(
        key="balanced",
        name="Balanced (iLoveIMG)",
        tier="primary",
        badge="Q80 • Smart Optimize",
        colors={
            "border": "#10B981",
            "focus": "#34D399",
            "accent": "#10B981",
            "highlight": "#FFFFFF",
            "selected": "#10B981",
            "muted": "#94A3B8",
            "ok": "#22C55E",
            "warn": "#F59E0B",
            "error": "#EF4444",
            "fill": "#10B981",
        },
        description="Best quality-to-size balance (MozJPEG Q80, pngquant+oxipng, cwebp, gifsicle)",
    ),
    ModeTheme(
        key="extreme",
        name="Extreme Compression",
        tier="primary",
        badge="Q60 • Max Savings",
        colors={
            "border": "#F43F5E",
            "focus": "#FB7185",
            "accent": "#F43F5E",
            "highlight": "#FFFFFF",
            "selected": "#F43F5E",
            "muted": "#94A3B8",
            "ok": "#22C55E",
            "warn": "#FBBF24",
            "error": "#EF4444",
            "fill": "#F43F5E",
        },
        description="Maximum size reduction (MozJPEG Q60, pngquant, lossy GIF)",
    ),
    ModeTheme(
        key="lossless",
        name="Lossless Optimize",
        tier="primary",
        badge="Q94 / Lossless • Safe",
        colors={
            "border": "#0EA5E9",
            "focus": "#38BDF8",
            "accent": "#0EA5E9",
            "highlight": "#FFFFFF",
            "selected": "#0EA5E9",
            "muted": "#94A3B8",
            "ok": "#22C55E",
            "warn": "#F59E0B",
            "error": "#EF4444",
            "fill": "#0EA5E9",
        },
        description="Zero-loss oxipng/WebP/gifsicle optimization + high-fidelity JPG",
    ),
    ModeTheme(
        key="webp",
        name="WebP Convert",
        tier="convert",
        badge="-> .webp • Modern Web",
        colors={
            "border": "#8B5CF6",
            "focus": "#A78BFA",
            "accent": "#8B5CF6",
            "highlight": "#FFFFFF",
            "selected": "#8B5CF6",
            "muted": "#A1A1AA",
            "ok": "#22C55E",
            "warn": "#FBBF24",
            "error": "#EF4444",
            "fill": "#8B5CF6",
        },
        description="Convert JPG/PNG/GIF/HEIC to compact WebP via cwebp/gif2webp",
    ),
    ModeTheme(
        key="resize",
        name="Resize + Compress",
        tier="convert",
        badge="Max 1920px • Web Ready",
        colors={
            "border": "#F59E0B",
            "focus": "#FBBF24",
            "accent": "#F59E0B",
            "highlight": "#FFFFFF",
            "selected": "#F59E0B",
            "muted": "#94A3B8",
            "ok": "#22C55E",
            "warn": "#FBBF24",
            "error": "#EF4444",
            "fill": "#F59E0B",
        },
        description="Downscale large photos (<=1920px) + smart compression",
    ),
)


def theme_for_key(key: str) -> ModeTheme:
    for theme in MODE_THEMES:
        if theme.key == key:
            return theme
    return MODE_THEMES[0]


def detect_color_tier() -> str:
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") in {"dumb", ""}:
        return "mono"
    if os.environ.get("COLORTERM", "").lower() in {"truecolor", "24bit"}:
        return "truecolor"
    try:
        if curses.has_colors() and curses.COLORS >= 256:
            return "256"
        if curses.has_colors() and curses.COLORS >= 16:
            return "16"
    except curses.error:
        return "mono"
    return "mono"


def parse_hex(value: str) -> tuple[int, int, int]:
    raw = value.strip().lstrip("#")
    return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)


def ansi_256_rgb(code: int) -> tuple[int, int, int]:
    if 16 <= code <= 231:
        code -= 16
        r = code // 36
        g = (code % 36) // 6
        b = code % 6
        steps = (0, 95, 135, 175, 215, 255)
        return steps[r], steps[g], steps[b]
    level = 8 + (code - 232) * 10
    return level, level, level


def nearest_ansi_256(hex_value: str) -> int:
    target = parse_hex(hex_value)
    best = 0
    best_distance = 10**9
    for code in range(16, 256):
        rgb = ansi_256_rgb(code)
        distance = sum((a - b) ** 2 for a, b in zip(target, rgb))
        if distance < best_distance:
            best = code
            best_distance = distance
    return best


ANSI_16 = {
    curses.COLOR_BLACK: (0, 0, 0),
    curses.COLOR_RED: (205, 49, 49),
    curses.COLOR_GREEN: (13, 188, 121),
    curses.COLOR_YELLOW: (229, 229, 16),
    curses.COLOR_BLUE: (36, 114, 200),
    curses.COLOR_MAGENTA: (188, 63, 188),
    curses.COLOR_CYAN: (17, 168, 205),
    curses.COLOR_WHITE: (229, 229, 229),
}


def nearest_ansi_16(hex_value: str) -> int:
    target = parse_hex(hex_value)
    return min(
        ANSI_16,
        key=lambda color: sum((a - b) ** 2 for a, b in zip(target, ANSI_16[color])),
    )


class ThemeManager:
    def __init__(self) -> None:
        self.tier = detect_color_tier()
        self.mode = MODE_THEMES[0]
        self.mono = self.tier == "mono"

    def apply(self, mode: ModeTheme) -> None:
        self.mode = mode
        try:
            has_colors = curses.has_colors()
        except curses.error:
            has_colors = False
        self.mono = self.tier == "mono" or not has_colors
        if self.mono:
            return
        curses.start_color()
        curses.use_default_colors()
        for role, pair_id in ROLE_PAIR.items():
            fg = self.color_number(mode.colors[role], pair_id)
            bg = -1
            if role in {"selected", "fill"}:
                bg = fg
                fg = curses.COLOR_BLACK if self.tier == "16" else 16
            curses.init_pair(pair_id, fg, bg)

    def color_number(self, hex_value: str, pair_id: int) -> int:
        if self.tier == "truecolor" and curses.can_change_color() and curses.COLORS >= 256:
            # Use 233..242 (low grayscale ramp) so truecolor UI themes never
            # overwrite the 6x6x6 RGB cube (16..231) used by image previews.
            color_id = min(255, 232 + pair_id)
            r, g, b = parse_hex(hex_value)
            curses.init_color(
                color_id,
                int(r / 255 * 1000),
                int(g / 255 * 1000),
                int(b / 255 * 1000),
            )
            return color_id
        if self.tier in {"truecolor", "256"} and curses.COLORS >= 256:
            return nearest_ansi_256(hex_value)
        return nearest_ansi_16(hex_value)
