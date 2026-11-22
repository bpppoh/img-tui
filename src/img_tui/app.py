from __future__ import annotations

import curses
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

from .ansi_shadow import ansi_shadow_banner
from .compressor import (
    BatchSummary,
    ImageEntry,
    compress_batch,
    format_bytes,
    health_checks,
    is_supported_image,
    parse_user_path,
    parse_user_paths,
    resolve_output_path,
    scan_inputs,
)
from .presets import (
    MAX_DIMENSIONS,
    OUTPUT_MODES,
    PRESETS,
    TARGET_FORMATS,
    CompressionConfig,
    preset_by_key,
)
from .preview import launch_native_preview
from .themes import MODE_THEMES, ROLE_PAIR, ModeTheme, ThemeManager, theme_for_key


LOGO = (
    "██╗███╗   ███╗ ██████╗       ████████╗██╗   ██╗██╗",
    "██║████╗ ████║██╔════╝       ╚══██╔══╝██║   ██║██║",
    "██║██╔████╔██║██║  ███╗█████╗   ██║   ██║   ██║██║",
    "██║██║╚██╔╝██║██║   ██║╚════╝   ██║   ██║   ██║██║",
    "██║██║ ╚═╝ ██║╚██████╔╝         ██║   ╚██████╔╝██║",
    "╚═╝╚═╝     ╚═╝ ╚═════╝          ╚═╝    ╚═════╝ ╚═╝",
)


class C:
    accent = ROLE_PAIR["accent"]
    accent_fill = ROLE_PAIR["fill"]
    selected = ROLE_PAIR["selected"]
    muted = ROLE_PAIR["muted"]
    ok = ROLE_PAIR["ok"]
    warn = ROLE_PAIR["warn"]
    error = ROLE_PAIR["error"]
    dim = ROLE_PAIR["muted"]
    border = ROLE_PAIR["border"]
    focus = ROLE_PAIR["focus"]


class Glyphs:
    def __init__(self, ascii_only: bool = False) -> None:
        self.unicode = not ascii_only and not os.environ.get("IMG_TUI_ASCII")
        self.run = "▶" if self.unicode else ">"
        self.ok = "✓" if self.unicode else "+"
        self.fail = "✗" if self.unicode else "!"
        self.wait = "·" if self.unicode else "."
        self.queue = "☰" if self.unicode else "#"
        self.check_on = "[x]"
        self.check_off = "[ ]"
        self.search = "⌕" if self.unicode else ">"


def char_width(ch: str) -> int:
    if not ch:
        return 0
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def display_width(value: object) -> int:
    return sum(char_width(ch) for ch in str(value))


def cut(value: object, width: int) -> str:
    text = str(value)
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    if width == 1:
        out: list[str] = []
        used = 0
        for ch in text:
            cw = char_width(ch)
            if used + cw > 1:
                break
            out.append(ch)
            used += cw
        return "".join(out)
    target = width - 1
    out = []
    used = 0
    for ch in text:
        cw = char_width(ch)
        if used + cw > target:
            break
        out.append(ch)
        used += cw
    return "".join(out) + "…"


def pad_right(value: object, width: int) -> str:
    clipped = cut(value, width)
    pad = max(0, width - display_width(clipped))
    return clipped + (" " * pad)


def format_delta_pct(before: int, after: int) -> str:
    if before <= 0 or after <= 0:
        return "0.0%"
    pct = ((before - after) / before) * 100.0
    sign = "-" if pct >= 0 else "+"
    return f"{sign}{abs(pct):.1f}%"


class Cockpit:
    def __init__(
        self,
        screen,
        initial_paths: list[str] | None = None,
        ascii_only: bool = False,
    ) -> None:
        self.screen = screen
        self.g = Glyphs(ascii_only=ascii_only)
        self.theme = ThemeManager()
        self.mode: ModeTheme = MODE_THEMES[0]
        self.config = CompressionConfig.from_preset(preset_by_key(self.mode.key))

        self.entries: list[ImageEntry] = []
        self.selected = 0
        self.list_offset = 0

        self.path_text = ""
        self.path_focused = False
        self.needs_redraw = True
        self.running = True
        self.ignore_global_q_until = 0.0

        self.compressing = False
        self.cancel_requested = False
        self.progress_done = 0
        self.progress_total = 0
        self.current_job_name = ""
        self.last_summary: BatchSummary | None = None

        self.messages: queue.Queue[object] = queue.Queue()
        self.health_cache = health_checks(self.g.unicode)

        self.focus_order = ("pillow", "heic", "mode", "savings", "path", "images", "filters", "preview", "queue")
        self.focus_index = 5
        self.filter_dimensions = ("all", "selected", "pending", "done", "failed")
        self.sort_dimensions = ("name", "size", "ext", "saved")
        self.filter_index = 0
        self.sort_index = 0

        self.notice = "Press b to browse files/folders, / to enter path, Space to toggle, v to view, Enter to compress."

        if initial_paths:
            self.load_paths([parse_user_path(p) for p in initial_paths], append=False)

    def load_paths(self, paths: list[Path], append: bool = True) -> int:
        scanned = scan_inputs(paths, recursive=self.config.recursive)
        if not append:
            self.entries = scanned
            self.selected = 0
            self.list_offset = 0
            added = len(scanned)
        else:
            existing = {e.path.resolve() for e in self.entries if e.path.exists()}
            added = 0
            for item in scanned:
                try:
                    resolved = item.path.resolve()
                except OSError:
                    resolved = item.path
                if resolved not in existing:
                    existing.add(resolved)
                    self.entries.append(item)
                    added += 1
        if added > 0:
            self.last_summary = None
        self.needs_redraw = True
        return added

    def run(self) -> None:
        try:
            curses.set_escdelay(5)
        except (AttributeError, curses.error):
            pass
        self.cursor(0)
        self.screen.nodelay(True)
        self.screen.keypad(True)
        self.screen.timeout(0)
        self.screen.scrollok(False)
        self.screen.idlok(False)
        self.theme.apply(self.mode)
        self.enable_mouse_tracking()

        chosen = self.mode_selector()
        if not chosen or not self.running:
            return
        self.apply_mode(chosen)
        self.enable_bracketed_paste()
        self.loading()
        self.enable_bracketed_paste()

        while self.running:
            self.drain()
            self.handle_pending_keys()
            if not self.running:
                break
            if self.compressing:
                self.needs_redraw = True
            if self.needs_redraw:
                self.draw()
                self.needs_redraw = False
            time.sleep(0.01)

    def apply_mode(self, mode: ModeTheme) -> None:
        self.mode = mode
        self.theme.apply(mode)
        self.config.apply_preset(preset_by_key(mode.key))
        self.needs_redraw = True

    def mode_selector(self, allow_cancel: bool = False) -> ModeTheme | None:
        query = ""
        cursor = next((i for i, m in enumerate(MODE_THEMES) if m.key == self.mode.key), 0)
        while True:
            q = query.lower().strip()
            matches = [
                m
                for m in MODE_THEMES
                if not q
                or q in m.name.lower()
                or q in m.key.lower()
                or q in m.description.lower()
                or q in m.badge.lower()
            ]
            if not matches:
                cursor = 0
            else:
                cursor = max(0, min(cursor, len(matches) - 1))
                self.theme.apply(matches[cursor])

            self.draw_mode_selector(query, matches, cursor, allow_cancel=allow_cancel)
            key = self.read_key(blocking=True)
            if key in (10, 13) and matches:
                return matches[cursor]
            if key == 27 and allow_cancel:
                return None
            if key in (ord("j"), curses.KEY_DOWN, -1002):
                cursor = max(0, min(cursor + 1, len(matches) - 1)) if matches else 0
            elif key in (ord("k"), curses.KEY_UP, -1001):
                cursor = max(0, min(cursor - 1, len(matches) - 1)) if matches else 0
            elif key == curses.KEY_MOUSE:
                delta = self.mouse_wheel_delta()
                if delta and matches:
                    cursor = max(0, min(cursor + delta, len(matches) - 1))
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                query = query[:-1]
            elif key in (ord("q"), ord("Q"), 3):
                if key == 3 or self.confirm_quit_popup():
                    self.running = False
                    return None
            elif isinstance(key, str):
                cleaned = key.replace("\r", "").replace("\n", "")
                if (
                    len(key) > 1
                    and ("/" in key or "\\" in key or "\n" in key or "\r" in key or key.startswith("~"))
                    and any(p.exists() for p in parse_user_paths(key))
                ):
                    self.load_pasted_paths(key)
                else:
                    query += cleaned
            elif isinstance(key, int) and 32 <= key <= 126:
                query += chr(key)

    def draw_mode_selector(
        self,
        query: str,
        matches: list[ModeTheme],
        cursor: int,
        allow_cancel: bool = False,
    ) -> None:
        self.screen.erase()
        h, w = self.screen.getmaxyx()
        panel_w = min(92, max(20, w - 4), w)
        panel_h = min(max(12, h - 2), 32, h)
        x = max(0, (w - panel_w) // 2)
        y = max(0, (h - panel_h) // 2)
        self.box(y, x, panel_h, panel_w, " compression mode ")

        banner_y = y + 2
        if query or panel_h < 18:
            label = "select compression mode"
            self.add(banner_y, x + max(2, (panel_w - len(label)) // 2), cut(label, panel_w - 4), C.accent)
        else:
            logo_w = max(len(line) for line in LOGO)
            if panel_w > logo_w + 4 and panel_h >= 24:
                for row, line in enumerate(LOGO):
                    self.add(banner_y + row, x + (panel_w - logo_w) // 2, line, C.accent)
                banner_y += len(LOGO)
            else:
                self.add(banner_y, x + max(2, (panel_w - 9) // 2), " img-tui ", C.accent)

        sub = "offline image compression cockpit  •  JPG / PNG / WebP / GIF / HEIC"
        if self.entries:
            total_sz = format_bytes(sum(e.size_bytes for e in self.entries))
            sub = f"ready: {len(self.entries)} image(s) preloaded ({total_sz})"
        self.add(banner_y + 1, x + max(2, (panel_w - min(panel_w - 4, display_width(sub))) // 2), cut(sub, panel_w - 4), C.dim)

        search_y = banner_y + 3
        self.add(search_y, x + 3, " " * max(0, panel_w - 6), C.accent_fill)
        hint = query or "type to filter modes (balanced, extreme, lossless, webp, resize)"
        self.add(search_y, x + 5, cut(f"{self.g.search} {hint}", panel_w - 10), C.accent_fill)

        list_y = search_y + 2
        row = list_y
        last_tier = ""
        for idx, mode in enumerate(matches):
            if row >= y + panel_h - 2:
                break
            if mode.tier != last_tier:
                tier_label = "Compression Presets" if mode.tier == "primary" else "Format & Resize Workflows"
                self.add(row, x + 3, cut(tier_label, panel_w - 6), C.accent)
                row += 1
                last_tier = mode.tier
            if row >= y + panel_h - 2:
                break
            marker = ">" if idx == cursor else " "
            line = f"{marker} {mode.name:<24} [{mode.badge}]"
            if idx == cursor:
                self.add(row, x + 4, pad_right(f" {line}", max(1, panel_w - 8)), C.accent, curses.A_REVERSE, curses.A_BOLD)
            else:
                self.add(row, x + 5, cut(line, panel_w - 10), C.muted)
            row += 1
            if idx == cursor and row < y + panel_h - 2:
                self.add(row, x + 7, cut(mode.description, panel_w - 12), C.dim)
                row += 1

        if not matches:
            self.add(list_y, x + 5, cut("No matching compression mode.", panel_w - 8), C.warn)
        foot_hint = "Enter selects   type filters   j/k moves   Esc back   q quits" if allow_cancel else "Enter selects   type filters   j/k moves   q quits"
        self.add(y + panel_h - 2, x + 3, cut(foot_hint, panel_w - 6), C.dim)
        self.screen.refresh()

    def loading(self) -> None:
        self.health_cache = health_checks(self.g.unicode)
        steps = [
            ("checking MozJPEG", "progressive + trellis JPEG encoder"),
            ("checking PNG", "pngquant perceptual + oxipng deflate"),
            ("checking WebP / GIF", "cwebp sharpyuv + gifsicle -O3"),
            ("checking HEIC", "macOS sips / ffmpeg decoder"),
            ("applying preset", f"{self.mode.name} ({self.config.summary_line()})"),
            ("arming cockpit", f"{len(self.entries)} image(s) ready"),
        ]
        for index, (label, detail) in enumerate(steps, 1):
            self.draw_loading(index, len(steps), label, detail)
            if self.wait(0.08):
                return
        self.draw_loading(len(steps), len(steps), "ready", "press b to browse or / to enter path")
        self.wait(0.14)

    def draw_loading(self, done: int, total: int, label: str, detail: str) -> None:
        self.screen.erase()
        h, w = self.screen.getmaxyx()
        bw = min(74, max(44, w - 8))
        banner = ansi_shadow_banner(self.mode.key)
        y = max(1, (h - 20) // 2)
        banner_w = max((len(line) for line in banner), default=0)
        banner_x = max(0, (w - banner_w) // 2)
        for row, line in enumerate(banner):
            self.add(y + row, banner_x, line.ljust(banner_w), C.accent)
        tag = "powered by img-tui"
        self.add(y + len(banner) + 2, max(0, (w - len(tag)) // 2), tag, C.dim)
        text_y = y + len(banner) + 4
        center = max(0, (w - bw) // 2)
        self.add(text_y, center + max(0, (bw - 10) // 2), "[] img-tui", C.dim)
        self.add(text_y + 2, center + max(0, (bw - len(label) - 5) // 2), f">_ {label}...", C.dim)

        bar_w = bw
        fill = max(1, int(bar_w * done / max(1, total)))
        self.add(text_y + 4, center, "⣿" * bar_w, C.accent)
        self.add(text_y + 4, center, " " * min(fill, bar_w), C.accent_fill)
        percent = int(done * 100 / max(1, total))
        self.add(text_y + 5, center + max(0, (bw - 4) // 2), f"{percent:>3}%", C.dim)
        self.add(text_y + 7, center + max(0, (bw - min(bw, len(detail) + 18)) // 2), cut(f"{done:02d}/{total:02d} checks  {detail}", bw), C.dim)
        self.screen.refresh()

    def draw(self) -> None:
        self.screen.erase()
        h, w = self.screen.getmaxyx()
        if h < 24 or w < 86:
            self.add(0, 0, f"Resize terminal to at least 86x24 (now {w}x{h}).", C.warn)
            self.screen.refresh()
            return

        info_h = 6
        path_h = 4
        foot_h = 4
        body_y = info_h + path_h
        body_h = h - body_y - foot_h
        right_min = 40
        left_w = min(max(46, int(w * 0.60)), w - right_min)
        right_w = w - left_w
        details_h = max(9, int(body_h * 0.50))
        queue_h = body_h - details_h

        self.dashboard_blocks(0, 0, info_h, w)
        self.box(info_h, 0, path_h, w, " Input Path / Folder ", self.focused("path") or self.path_focused)
        self.box(body_y, 0, body_h, left_w, " Images ", self.focused("images"))
        self.box(body_y, left_w, details_h, right_w, " Details ", self.focused("preview"))
        self.box(body_y + details_h, left_w, queue_h, right_w, f" {self.g.queue} Compression Queue ", self.focused("queue"))
        self.box(h - foot_h, 0, foot_h, w, " actions ")

        self.path_bar(info_h + 1, 2, w - 4)
        self.image_list(body_y + 1, 2, body_h - 4, left_w - 4)
        self.filter_sort_bar(body_y + body_h - 2, 2, left_w - 4)
        self.details_panel(body_y + 1, left_w + 2, details_h - 2, right_w - 4)
        self.queue_panel(body_y + details_h + 1, left_w + 2, queue_h - 2, right_w - 4)
        self.footer(h - foot_h + 1, 2, w - 4)

        self.screen.noutrefresh()
        curses.doupdate()

    def dashboard_blocks(self, y: int, x: int, height: int, width: int) -> None:
        block_w = max(20, width // 4)
        labels = (" Engine ", " Codecs / HEIC ", " Active Mode ", " Savings ")
        focus_names = ("pillow", "heic", "mode", "savings")
        for idx, label in enumerate(labels):
            bx = x + idx * block_w
            bw = width - bx if idx == 3 else block_w
            self.box(y, bx, height, bw, label, self.focused(focus_names[idx]))

        checks = self.health_cache
        by_name = {name: (state, detail) for name, state, detail in checks}

        def draw_check(row_y: int, col_x: int, key: str) -> None:
            state, detail = by_name.get(key, ("warn", "unknown"))
            mark = self.g.ok if state == "ok" else self.g.fail
            color = C.ok if state == "ok" else C.warn
            self.add(row_y, col_x, cut(f"{mark} {key}: {detail}", block_w - 4), color)

        # Block 0: Core iLoveIMG-grade encoders (MozJPEG, PNG, cwebp)
        draw_check(y + 1, x + 2, "MozJPEG")
        draw_check(y + 2, x + 2, "PNG")
        draw_check(y + 3, x + 2, "cwebp")
        self.add(y + 4, x + 2, cut(f"recursive: {'on' if self.config.recursive else 'off'}", block_w - 4), C.dim)

        # Block 1: GIF, HEIC & Pillow fallback
        bx1 = x + block_w
        draw_check(y + 1, bx1 + 2, "gifsicle")
        draw_check(y + 2, bx1 + 2, "HEIC")
        draw_check(y + 3, bx1 + 2, "Pillow")
        self.add(y + 4, bx1 + 2, cut("formats: JPG PNG WebP GIF HEIC", block_w - 4), C.dim)

        # Block 2: Active mode & settings
        bx2 = x + 2 * block_w
        self.add(y + 1, bx2 + 2, cut(f"{self.mode.name}", block_w - 4), C.accent)
        dim_str = f"{self.config.max_dimension}px" if self.config.max_dimension else "orig"
        self.add(y + 2, bx2 + 2, cut(f"fmt:{self.config.target_format}  q:{self.config.quality}  max:{dim_str}", block_w - 4), C.muted)
        self.add(y + 3, bx2 + 2, cut(f"out: {self.config.output_mode}", block_w - 4), C.dim)

        # Block 3: Savings stats
        bx3 = x + 3 * block_w
        bw3 = width - bx3 - 4
        sel_entries = [e for e in self.entries if e.selected]
        total_sel_bytes = sum(e.size_bytes for e in sel_entries)
        done_entries = [e for e in self.entries if e.status == "done" and e.output_bytes > 0]
        if done_entries:
            before = sum(e.size_bytes for e in done_entries)
            after = sum(e.output_bytes for e in done_entries)
            pct = ((before - after) / before * 100.0) if before > 0 else 0.0
            self.add(y + 1, bx3 + 2, cut(f"saved: {format_bytes(before - after)} ({pct:.1f}%)", bw3), C.ok)
            self.add(y + 2, bx3 + 2, cut(f"{format_bytes(before)} -> {format_bytes(after)}", bw3), C.muted)
            self.add(y + 3, bx3 + 2, cut(f"done: {len(done_entries)}/{len(self.entries)}", bw3), C.dim)
        else:
            self.add(y + 1, bx3 + 2, cut(f"selected: {len(sel_entries)}/{len(self.entries)}", bw3), C.accent)
            self.add(y + 2, bx3 + 2, cut(f"input size: {format_bytes(total_sel_bytes)}", bw3), C.muted)
            self.add(y + 3, bx3 + 2, cut("ready to compress", bw3), C.dim)

    def path_bar(self, y: int, x: int, width: int) -> None:
        cursor = "█" if self.path_focused else ""
        sel_count = sum(1 for e in self.entries if e.selected)
        count = f"{sel_count}/{len(self.entries)} files"
        if self.compressing:
            spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.monotonic() * 12) % 10]
            label = f"{spinner} compressing {self.progress_done}/{self.progress_total}: {self.current_job_name}"
        elif self.path_text or self.path_focused:
            label = re.sub(r"[\r\n]+", " ", self.path_text)
        else:
            label = "press [b] to browse files/folders, or [/] to paste/drag a path"
        field_w = max(10, width - len(count) - 4)
        self.add(y, x, " " * width, C.accent_fill)
        full_label = f"{self.g.search} {label}{cursor}"
        if self.path_focused and not self.compressing and display_width(full_label) > field_w:
            prefix = f"{self.g.search} …"
            avail = max(1, field_w - display_width(prefix))
            tail = f"{label}{cursor}"
            while display_width(tail) > avail and len(tail) > 1:
                tail = tail[1:]
            shown = prefix + tail
        else:
            shown = cut(full_label, field_w)
        self.add(y, x + 2, shown, C.accent_fill)
        self.add(y, x + width - len(count) - 1, count, C.accent_fill)

    def displayed_indices(self) -> list[int]:
        dimension = self.filter_dimensions[self.filter_index]
        indices = list(range(len(self.entries)))
        if dimension == "selected":
            indices = [i for i in indices if self.entries[i].selected]
        elif dimension in {"pending", "done", "failed"}:
            indices = [i for i in indices if self.entries[i].status == dimension]

        sort_key = self.sort_dimensions[self.sort_index]
        if sort_key == "name":
            indices.sort(key=lambda i: self.entries[i].path.name.lower())
        elif sort_key == "size":
            indices.sort(key=lambda i: self.entries[i].size_bytes, reverse=True)
        elif sort_key == "ext":
            indices.sort(key=lambda i: (self.entries[i].ext, self.entries[i].path.name.lower()))
        elif sort_key == "saved":
            indices.sort(key=lambda i: self.entries[i].saved_percent, reverse=True)
        return indices

    def current_entry(self) -> ImageEntry | None:
        active = self.displayed_indices()
        if not active:
            return None
        idx = max(0, min(self.selected, len(active) - 1))
        return self.entries[active[idx]]

    def image_list(self, y: int, x: int, height: int, width: int) -> None:
        active = self.displayed_indices()
        if not active:
            self.add(y, x, cut("No images loaded yet.", width), C.accent)
            self.add(y + 1, x, cut("Press [b] to browse files/folders, or [/] to type/drag a path.", width), C.muted)
            self.add(y + 2, x, cut("Tip: You can also launch `img-tui ~/Downloads/photos` directly.", width), C.dim)
            return

        list_height = max(1, height - 2)
        self.clamp_list_view(list_height, len(active))
        total_rows = len(active)
        visible_indexes = range(self.list_offset, min(total_rows, self.list_offset + list_height))
        num_w = max(2, len(str(total_rows)))
        scroll_w = 3 if total_rows > list_height else 0
        table_w = width - scroll_w

        ext_w = 5
        before_w = 9
        after_w = 16
        name_w = max(10, table_w - num_w - ext_w - before_w - after_w - 11)

        header = f" {'#':>{num_w}} SEL {'EXT':<{ext_w}} {'FILENAME':<{name_w}} {'BEFORE':>{before_w}} {'AFTER / SAVED':>{after_w}}"
        self.add(y, x, cut(header, table_w), C.dim)

        visible_count = 0
        for row, active_pos in enumerate(visible_indexes):
            visible_count += 1
            entry = self.entries[active[active_pos]]
            is_cur = active_pos == self.selected
            prefix = ">" if is_cur else " "
            chk = self.g.check_on if entry.selected else self.g.check_off
            ext_lbl = entry.ext.lstrip(".").upper()[:ext_w]
            fname = pad_right(entry.path.name, name_w)
            before_str = format_bytes(entry.size_bytes)

            if entry.status == "done":
                pct = entry.saved_percent
                sign = "-" if pct >= 0 else "+"
                after_str = f"{format_bytes(entry.output_bytes)} ({sign}{abs(pct):.0f}%)"
                row_color = C.ok
            elif entry.status == "running":
                after_str = "compressing..."
                row_color = C.warn
            elif entry.status == "failed":
                after_str = "FAILED"
                row_color = C.error
            else:
                after_str = "pending" if entry.selected else "skipped"
                row_color = C.muted if not entry.selected else 0

            line = f"{prefix}{active_pos + 1:>{num_w}} {chk} {ext_lbl:<{ext_w}} {fname} {before_str:>{before_w}} {after_str:>{after_w}}"
            color = C.selected if is_cur else row_color
            self.add(y + row + 1, x, cut(line, table_w), color)

        scroll = f"showing {self.list_offset + 1}-{self.list_offset + visible_count} of {total_rows}  ([Space] toggle  [a] all  [n] none)"
        self.add(y + height - 1, x, cut(scroll, table_w), C.accent)
        if scroll_w:
            self.draw_scrollbar(y + 1, x + width - 2, list_height, total_rows, self.list_offset)

    def filter_sort_bar(self, y: int, x: int, width: int) -> None:
        flt = self.filter_dimensions[self.filter_index]
        srt = self.sort_dimensions[self.sort_index]
        rec = "on" if self.config.recursive else "off"
        text = f"Filter: {flt} (< / >)   Sort: {srt} (s)   Recursive: {rec} (r)"
        self.add(y, x, cut(text, width), C.selected if self.focused("filters") else C.accent)

    def details_panel(self, y: int, x: int, height: int, width: int) -> None:
        entry = self.current_entry()
        if not entry:
            self.add(y, x, cut("No image selected.", width), C.muted)
            if height > 2:
                self.add(y + 2, x, cut("Add files with [b] or [/], then move with j/k.", width), C.dim)
            return

        dims = f"{entry.width}x{entry.height}" if entry.width and entry.height else "unknown"
        out_desc = (
            f"{format_bytes(entry.output_bytes)} ({format_delta_pct(entry.size_bytes, entry.output_bytes)})"
            if entry.status == "done"
            else (entry.error or entry.status)
        )
        planned_out = resolve_output_path(entry, self.config)
        if entry.status == "done" and entry.output_path is not None:
            target_line = f"output: {entry.output_path.name} ({entry.output_path.parent})"
        else:
            target_line = f"target: {planned_out.name} ({self.config.output_mode})"

        viewer_name = "QuickLook" if sys.platform == "darwin" else "system viewer"
        has_distinct_output = (
            entry.status == "done"
            and entry.output_path is not None
            and entry.output_path.exists()
            and entry.output_path != entry.path
        )
        view_hint = (
            f"[v] {viewer_name} compressed   [V] {viewer_name} original"
            if has_distinct_output
            else f"Press [v] to open full-res preview ({viewer_name})"
        )

        lines: list[tuple[str, int]] = [
            (f"file  : {entry.path.name}", C.accent),
            (f"path  : {entry.path.parent}", C.muted),
            (f"info  : {dims}  {entry.mode or entry.ext.lstrip('.').upper()}  {format_bytes(entry.size_bytes)}", C.muted),
            (f"status: {out_desc}" + (f"  [{entry.note}]" if entry.note else ""), C.error if entry.status == "failed" else (C.ok if entry.status == "done" else C.muted)),
            (target_line, C.dim),
        ]
        for row, (line, color) in enumerate(lines[:height]):
            self.add(y + row, x, cut(line, width), color)

        hint_y = y + min(len(lines) + 1, max(0, height - 1))
        if hint_y < y + height:
            self.add(hint_y, x, cut(view_hint, width), C.accent)

    def queue_panel(self, y: int, x: int, height: int, width: int) -> None:
        for row in range(height):
            self.add(y + row, x, " " * width)

        if self.compressing:
            pct = (self.progress_done / max(1, self.progress_total)) * 100.0
            bar_w = max(10, min(32, width - 16))
            bar = self.progress_bar(pct, bar_w)
            self.add(y, x, cut(f"Running {self.progress_done}/{self.progress_total}: {self.current_job_name}", width), C.warn)
            if height > 1:
                self.add(y + 1, x, cut(f"{bar} {pct:5.1f}%", width), C.accent)
            recent = [e for e in self.entries if e.status in {"done", "failed"}][-max(0, height - 3) :]
            for idx, item in enumerate(recent):
                color = C.ok if item.status == "done" else C.error
                detail = f"-{item.saved_percent:.1f}%" if item.status == "done" else "ERR"
                self.add(y + 3 + idx, x, cut(f"{item.path.name} -> {detail}", width), color)
            return

        if self.last_summary:
            s = self.last_summary
            bar_w = max(10, min(32, width - 16))
            bar = self.progress_bar(100.0, bar_w)
            self.add(y, x, cut(f"Completed in {s.elapsed_seconds:.2f}s  ({s.succeeded} ok, {s.failed} err)", width), C.ok if s.failed == 0 else C.warn)
            if height > 1:
                self.add(y + 1, x, cut(f"{bar} 100.0%", width), C.ok)
            if height > 2:
                self.add(
                    y + 2,
                    x,
                    cut(f"Total: {format_bytes(s.total_before_bytes)} -> {format_bytes(s.total_after_bytes)}  (saved {format_bytes(s.saved_bytes)} / {s.saved_percent:.1f}%)", width),
                    C.accent,
                )
            if height > 4:
                self.add(y + 4, x, cut("Press [x] for full summary, [O] to open output folder.", width), C.dim)
            return

        sel_count = sum(1 for e in self.entries if e.selected)
        if sel_count == 0:
            self.add(y, x, cut("Queue is empty.", width), C.muted)
            if height > 1:
                self.add(y + 1, x, cut("Load images with [b] or [/], then press Enter or [c] to compress.", width), C.dim)
        else:
            total_b = sum(e.size_bytes for e in self.entries if e.selected)
            self.add(y, x, cut(f"Ready: {sel_count} image(s) queued ({format_bytes(total_b)})", width), C.accent)
            if height > 1:
                self.add(y + 1, x, cut(f"Output: {self.config.output_mode}  Format: {self.config.target_format}", width), C.muted)
            if height > 3:
                self.add(y + 3, x, cut("Press [Enter] or [c] to confirm & start compression.", width), C.ok)

    def progress_bar(self, percent: float, width: int) -> str:
        value = max(0.0, min(percent, 100.0))
        filled = int(width * value / 100)
        if self.g.unicode:
            return "[" + ("█" * filled).ljust(width, "░") + "]"
        return "[" + ("#" * filled).ljust(width, ".") + "]"

    def footer(self, y: int, x: int, width: int) -> None:
        n_lower = self.notice.lower()
        status_color = C.error if ("fail" in n_lower or "error" in n_lower) else (C.ok if "done" in n_lower or "saved" in n_lower else C.muted)
        self.add(y, x, cut(self.notice, width), status_color)
        self.add(y + 1, x, cut(self.config.summary_line(), width), C.dim)
        keys = "[?] help  [b] browse  [/] path  [p] mode  [f] options  [v] view  [Space] toggle  [Enter/c] compress  [x] summary  [O] folder  [q] back/quit"
        self.add(y + 2, x, cut(keys, width), C.accent)

    def handle_pending_keys(self) -> None:
        handled = 0
        while handled < 60 and self.running:
            key = self.read_key()
            if key == -1:
                return
            self.keys(key)
            self.needs_redraw = True
            handled += 1

    def keys(self, key: int | str) -> None:
        if self.path_focused:
            self.path_keys(key)
            return
        if isinstance(key, str):
            if self.compressing:
                self.notice = "Compression running... press [q] to cancel."
                return
            self.load_pasted_paths(key)
            return
        if key == 9:
            self.focus_index = (self.focus_index + 1) % len(self.focus_order)
            return
        if key in (ord("q"), ord("Q")) and time.monotonic() < self.ignore_global_q_until:
            return
        if key in (ord("q"), ord("Q")):
            if self.compressing:
                self.cancel_requested = True
                self.notice = "Cancelling after current image..."
                return
            chosen = self.mode_selector(allow_cancel=True)
            if chosen and self.running:
                self.apply_mode(chosen)
                self.enable_bracketed_paste()
                self.notice = f"Switched mode to {chosen.name}."
            elif self.running:
                self.theme.apply(self.mode)
                self.enable_bracketed_paste()
            return
        if key == 3:
            self.running = False
            return
        if key in (ord("j"), curses.KEY_DOWN):
            self.move_selection(1)
        elif key in (ord("k"), curses.KEY_UP):
            self.move_selection(-1)
        elif key == curses.KEY_NPAGE:
            self.move_selection(10)
        elif key == curses.KEY_PPAGE:
            self.move_selection(-10)
        elif key == curses.KEY_MOUSE:
            self.handle_curses_mouse()
        elif key == -1001:
            self.move_selection(-1)
        elif key == -1002:
            self.move_selection(1)
        elif key == ord("?"):
            self.help_popup()
        elif key in (ord(">"), curses.KEY_RIGHT):
            self.filter_index = (self.filter_index + 1) % len(self.filter_dimensions)
            self.selected = 0
            self.list_offset = 0
        elif key in (ord("<"), curses.KEY_LEFT):
            self.filter_index = (self.filter_index - 1) % len(self.filter_dimensions)
            self.selected = 0
            self.list_offset = 0
        elif key == ord("s"):
            self.sort_index = (self.sort_index + 1) % len(self.sort_dimensions)
            self.selected = 0
            self.list_offset = 0
        elif key == ord("x"):
            self.summary_popup()
        elif key == ord("O"):
            self.open_output_folder()
        elif key == ord("v"):
            self.open_image_preview(prefer_output=True)
        elif key == ord("V"):
            self.open_image_preview(prefer_output=False)
        elif self.compressing:
            if key in (ord("p"), ord("b"), ord("f"), ord("o"), ord("/"), ord(" "), ord("a"), ord("n"), ord("r"), ord("d"), curses.KEY_DC, ord("X"), 10, 13, ord("c"), ord("1"), ord("2"), ord("3"), ord("4"), ord("5")):
                self.notice = "Compression running... press [q] to cancel."
            return
        elif key == ord("p"):
            chosen = self.mode_selector(allow_cancel=True)
            if chosen and self.running:
                self.apply_mode(chosen)
                self.enable_bracketed_paste()
                self.notice = f"Switched mode to {chosen.name}."
            elif self.running:
                self.theme.apply(self.mode)
                self.enable_bracketed_paste()
        elif key in (ord("1"), ord("2"), ord("3"), ord("4"), ord("5")):
            idx = key - ord("1")
            if 0 <= idx < len(MODE_THEMES):
                self.apply_mode(MODE_THEMES[idx])
                self.notice = f"Mode: {MODE_THEMES[idx].name}"
        elif key == ord("/"):
            self.path_focused = True
            self.notice = "Type or paste file/folder path, Enter to load, Esc to cancel, Ctrl+U to clear."
        elif key == ord("b"):
            self.browse_popup()
        elif key in (ord("f"), ord("o")):
            self.options_popup()
        elif key == ord(" "):
            self.toggle_current()
        elif key == ord("a"):
            for e in self.entries:
                e.selected = True
            self.notice = f"Selected all {len(self.entries)} image(s)."
        elif key == ord("n"):
            for e in self.entries:
                e.selected = False
            self.notice = "Deselected all images."
        elif key == ord("r"):
            self.config.recursive = not self.config.recursive
            self.notice = f"Recursive folder scan: {'ON' if self.config.recursive else 'OFF'}"
        elif key in (ord("d"), curses.KEY_DC):
            self.remove_current()
        elif key == ord("X"):
            self.entries = []
            self.selected = 0
            self.list_offset = 0
            self.last_summary = None
            self.notice = "Cleared image list."
        elif key in (10, 13, ord("c")):
            self.confirm_compress_popup()

    def load_pasted_paths(self, raw_text: str) -> bool:
        raw = raw_text.strip()
        if not raw:
            return False
        paths = parse_user_paths(raw)
        existing = [p for p in paths if p.exists()]
        if not existing:
            self.notice = f"Error: Path not found: {paths[0] if paths else raw}"
            return False
        added = self.load_paths(existing, append=True)
        label = existing[0].name or str(existing[0])
        if len(existing) > 1:
            label = f"{len(existing)} paths"
        self.notice = f"Loaded {added} new image(s) from {label}."
        return True

    def path_keys(self, key: int | str) -> None:
        if key in (10, 13):
            raw = self.path_text.strip()
            self.path_focused = False
            if raw and self.load_pasted_paths(raw):
                self.path_text = ""
            return
        if key == 27:
            self.path_focused = False
            self.notice = "Path input cancelled."
            return
        if key == 21:  # Ctrl+U
            self.path_text = ""
            return
        if key == 3:
            self.running = False
            return
        if key in (curses.KEY_BACKSPACE, 127, 8):
            self.path_text = self.path_text[:-1]
            return
        if isinstance(key, str):
            self.path_text += re.sub(r"[\r\n]+", "\n", key)
        elif isinstance(key, int) and 32 <= key <= 126:
            self.path_text += chr(key)

    def toggle_current(self) -> None:
        entry = self.current_entry()
        if not entry:
            return
        entry.selected = not entry.selected
        self.notice = f"{'Selected' if entry.selected else 'Unselected'}: {entry.path.name}"

    def remove_current(self) -> None:
        entry = self.current_entry()
        if not entry:
            return
        self.entries.remove(entry)
        active = self.displayed_indices()
        if self.selected >= len(active):
            self.selected = max(0, len(active) - 1)
        self.notice = f"Removed {entry.path.name} from queue."

    def move_selection(self, delta: int) -> None:
        active = self.displayed_indices()
        if not active:
            return
        self.selected = max(0, min(self.selected + delta, len(active) - 1))
        h, _ = self.screen.getmaxyx()
        list_h = max(1, h - 6 - 4 - 4 - 5)
        self.clamp_list_view(list_h, len(active))

    def clamp_list_view(self, visible_height: int, total: int) -> None:
        padding = min(3, max(1, visible_height // 4))
        if self.selected < self.list_offset + padding:
            self.list_offset = self.selected - padding
        elif self.selected >= self.list_offset + visible_height - padding:
            self.list_offset = self.selected - visible_height + padding + 1
        max_offset = max(0, total - visible_height)
        self.list_offset = max(0, min(self.list_offset, max_offset))

    def draw_scrollbar(self, y: int, x: int, height: int, total: int, offset: int) -> None:
        if height <= 0 or total <= height:
            return
        thumb_h = max(1, int(height * height / total))
        max_offset = max(1, total - height)
        thumb_y = y + int((height - thumb_h) * offset / max_offset)
        self.add(y, x, "↑", C.accent)
        self.add(y + height - 1, x, "↓", C.accent)
        for row in range(1, max(1, height - 1)):
            color = C.accent if thumb_y <= y + row < thumb_y + thumb_h else C.dim
            self.add(y + row, x, "┃", color)

    def browse_popup(self) -> None:
        cwd = Path.cwd()
        try:
            has_cwd_imgs = any(is_supported_image(c) for c in cwd.iterdir() if c.is_file())
        except OSError:
            has_cwd_imgs = False
        start_dir = cwd if has_cwd_imgs else (Path.home() / "Downloads")
        if not start_dir.exists():
            start_dir = Path.home()
        try:
            current_dir = start_dir.resolve()
        except OSError:
            current_dir = Path.home()
        cursor = 0
        show_hidden = False

        def list_dir(directory: Path) -> list[tuple[str, Path]]:
            items: list[tuple[str, Path]] = []
            if directory.parent != directory:
                items.append(("up", directory.parent))
            try:
                children = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except OSError:
                return items
            for child in children:
                if not show_hidden and child.name.startswith("."):
                    continue
                if child.is_dir():
                    items.append(("dir", child))
                elif is_supported_image(child):
                    items.append(("img", child))
            return items

        while True:
            items = list_dir(current_dir)
            cursor = max(0, min(cursor, max(0, len(items) - 1)))
            h, w = self.screen.getmaxyx()
            bw = min(92, max(40, w - 8), w)
            bh = min(max(14, h - 6), 30, h)
            y = max(0, (h - bh) // 2)
            x = max(0, (w - bw) // 2)
            self.draw()
            rec_tag = "rec:ON" if self.config.recursive else "rec:off"
            self.box(y, x, bh, bw, f" browse: {cut(str(current_dir), max(10, bw - 22))} ({rec_tag}) ")
            self.add(y + 1, x + 3, cut("[1] Desktop  [2] Downloads  [3] Pictures  [4] Home  [5] CWD  [.] hidden  [r] rec", bw - 6), C.dim)

            list_y = y + 3
            visible_h = max(3, bh - 6)
            start = max(0, min(cursor - visible_h + 1, max(0, len(items) - visible_h)))
            for row, (kind, p) in enumerate(items[start : start + visible_h]):
                abs_idx = start + row
                marker = ">" if abs_idx == cursor else " "
                if kind == "up":
                    label = f"{marker} [..] Parent Directory"
                    color = C.accent
                elif kind == "dir":
                    label = f"{marker} [DIR] {p.name}/"
                    color = C.accent
                else:
                    try:
                        sz = format_bytes(p.stat().st_size)
                    except OSError:
                        sz = "?"
                    label = f"{marker} [IMG] {p.name}  ({sz})"
                    color = C.ok
                if abs_idx == cursor:
                    color = C.selected
                self.add(list_y + row, x + 3, cut(label, bw - 6), color)

            if len(items) <= 1:
                self.add(list_y + 1, x + 5, cut("(No subfolders or supported images here)", bw - 8), C.muted)

            self.add(y + bh - 2, x + 3, cut("Enter open  Space add item/folder  a add entire folder  Backspace up  q/Esc close", bw - 6), C.dim)
            self.screen.refresh()

            key = self.read_key(blocking=True)
            if key == 3:
                self.running = False
                return
            if key in (ord("q"), ord("Q"), 27):
                self.close_modal_guard()
                self.screen.nodelay(True)
                return
            if key in (ord("j"), curses.KEY_DOWN, -1002):
                cursor = min(cursor + 1, max(0, len(items) - 1))
            elif key in (ord("k"), curses.KEY_UP, -1001):
                cursor = max(cursor - 1, 0)
            elif key == curses.KEY_MOUSE:
                delta = self.mouse_wheel_delta()
                if delta:
                    cursor = max(0, min(cursor + delta, max(0, len(items) - 1)))
            elif key == curses.KEY_NPAGE:
                cursor = min(cursor + 10, max(0, len(items) - 1))
            elif key == curses.KEY_PPAGE:
                cursor = max(cursor - 10, 0)
            elif key == ord("."):
                show_hidden = not show_hidden
                cursor = 0
            elif key == ord("r"):
                self.config.recursive = not self.config.recursive
            elif key == ord("1"):
                d = Path.home() / "Desktop"
                if d.exists():
                    current_dir, cursor = d, 0
            elif key == ord("2"):
                d = Path.home() / "Downloads"
                if d.exists():
                    current_dir, cursor = d, 0
            elif key == ord("3"):
                d = Path.home() / "Pictures"
                if d.exists():
                    current_dir, cursor = d, 0
            elif key == ord("4"):
                current_dir, cursor = Path.home(), 0
            elif key == ord("5"):
                current_dir, cursor = Path.cwd(), 0
            elif key in (curses.KEY_BACKSPACE, 127, 8, ord("h"), curses.KEY_LEFT):
                if current_dir.parent != current_dir:
                    current_dir = current_dir.parent
                    cursor = 0
            elif key in (10, 13, ord("l"), curses.KEY_RIGHT) and items:
                kind, target = items[cursor]
                if kind in ("up", "dir"):
                    current_dir = target
                    cursor = 0
                elif kind == "img":
                    added = self.load_paths([target], append=True)
                    self.notice = f"Added {target.name} ({added} new)."
                    self.close_modal_guard()
                    self.screen.nodelay(True)
                    return
            elif key == ord(" ") and items:
                kind, target = items[cursor]
                if kind == "img":
                    added = self.load_paths([target], append=True)
                    self.notice = f"Added {target.name} ({added} new)."
                elif kind == "dir":
                    added = self.load_paths([target], append=True)
                    rec_hint = "" if added or self.config.recursive else " (recursive is off, press r)"
                    self.notice = f"Added {added} image(s) from {target.name}/.{rec_hint}"
            elif key == ord("a"):
                added = self.load_paths([current_dir], append=True)
                self.notice = f"Added {added} image(s) from {current_dir.name or current_dir}/."
                self.close_modal_guard()
                self.screen.nodelay(True)
                return

    def options_popup(self) -> None:
        cursor = 0
        fields = (
            "Preset",
            "Target Format",
            "Quality (10-100)",
            "Lossless WebP",
            "PNG Quantize",
            "Max Dimension",
            "Output Mode",
            "Custom Folder",
            "Strip Metadata",
            "Keep Smaller",
            "Recursive Scan",
        )
        while True:
            h, w = self.screen.getmaxyx()
            bw = min(84, max(44, w - 8), w)
            bh = min(len(fields) + 6, max(10, h - 4), h)
            y = max(0, (h - bh) // 2)
            x = max(0, (w - bw) // 2)
            self.draw()
            self.box(y, x, bh, bw, " compression options ")
            values = (
                preset_by_key(self.config.preset_key).name,
                self.config.target_format,
                f"{self.config.quality}",
                "yes (lossless)" if self.config.lossless_webp else "no (lossy quality)",
                f"{self.config.png_quantize_colors} colors" if self.config.png_quantize_colors > 0 else "off (full color)",
                f"{self.config.max_dimension}px" if self.config.max_dimension > 0 else "original",
                self.config.output_mode,
                str(self.config.custom_output_dir),
                "yes (smaller)" if self.config.strip_metadata else "no (preserve EXIF/ICC)",
                "yes (never bloat)" if self.config.keep_smaller else "no",
                "yes" if self.config.recursive else "no",
            )
            for row, (field, val) in enumerate(zip(fields, values)):
                if row >= bh - 5:
                    break
                sel = row == cursor
                marker = ">" if sel else " "
                text = f"{marker} {field:<17}: {val}"
                self.add(y + 2 + row, x + 3, cut(text, bw - 6), C.selected if sel else C.muted)
            self.add(y + bh - 3, x + 3, cut("Left/Right or Enter edits field   q/Esc closes", bw - 6), C.dim)
            self.add(y + bh - 2, x + 3, cut("Note: output defaults to ./compressed/ (non-destructive)", bw - 6), C.accent)
            self.screen.refresh()

            key = self.read_key(blocking=True)
            if key == 3:
                self.running = False
                return
            if key in (ord("q"), ord("Q"), 27):
                self.close_modal_guard()
                self.screen.nodelay(True)
                return
            if key in (ord("j"), curses.KEY_DOWN, -1002):
                cursor = min(cursor + 1, len(fields) - 1)
            elif key in (ord("k"), curses.KEY_UP, -1001):
                cursor = max(cursor - 1, 0)
            elif key == curses.KEY_MOUSE:
                delta = self.mouse_wheel_delta()
                if delta:
                    cursor = max(0, min(cursor + delta, len(fields) - 1))
            elif key in (10, 13, curses.KEY_RIGHT, ord("l"), curses.KEY_LEFT, ord("h")):
                step = -1 if key in (curses.KEY_LEFT, ord("h")) else 1
                field = fields[cursor]
                if field == "Preset":
                    keys = [p.key for p in PRESETS]
                    idx = (keys.index(self.config.preset_key) + step) % len(keys)
                    self.apply_mode(theme_for_key(keys[idx]))
                elif field == "Target Format":
                    idx = (TARGET_FORMATS.index(self.config.target_format) + step) % len(TARGET_FORMATS)
                    self.config.target_format = TARGET_FORMATS[idx]
                elif field == "Quality (10-100)":
                    if key in (10, 13):
                        raw = self.prompt("Quality (10-100)", str(self.config.quality))
                        if raw is not None and raw.isdigit():
                            self.config.quality = max(10, min(100, int(raw)))
                            self.config.lossless_webp = False
                    else:
                        self.config.quality = max(10, min(100, self.config.quality + step * 5))
                        self.config.lossless_webp = False
                elif field == "Lossless WebP":
                    self.config.lossless_webp = not self.config.lossless_webp
                elif field == "PNG Quantize":
                    self.config.png_quantize_colors = 0 if self.config.png_quantize_colors > 0 else 256
                elif field == "Max Dimension":
                    cur = self.config.max_dimension
                    idx = (MAX_DIMENSIONS.index(cur) + step) % len(MAX_DIMENSIONS) if cur in MAX_DIMENSIONS else 0
                    self.config.max_dimension = MAX_DIMENSIONS[idx]
                elif field == "Output Mode":
                    idx = (OUTPUT_MODES.index(self.config.output_mode) + step) % len(OUTPUT_MODES)
                    self.config.output_mode = OUTPUT_MODES[idx]
                elif field == "Custom Folder":
                    if key in (10, 13):
                        raw = self.prompt("Custom output folder", str(self.config.custom_output_dir))
                        if raw:
                            self.config.custom_output_dir = parse_user_path(raw)
                            self.config.output_mode = "custom"
                    else:
                        self.config.output_mode = "custom"
                elif field == "Strip Metadata":
                    self.config.strip_metadata = not self.config.strip_metadata
                elif field == "Keep Smaller":
                    self.config.keep_smaller = not self.config.keep_smaller
                elif field == "Recursive Scan":
                    self.config.recursive = not self.config.recursive

    def confirm_compress_popup(self) -> None:
        if self.compressing:
            self.notice = "Compression already running..."
            return
        selected = [e for e in self.entries if e.selected]
        if not selected:
            self.notice = "No images selected. Browse with [b] or toggle with [Space]."
            return
        total_bytes = sum(e.size_bytes for e in selected)
        while True:
            h, w = self.screen.getmaxyx()
            bw = min(78, max(50, w - 8))
            bh = 14
            y = max(1, (h - bh) // 2)
            x = max(0, (w - bw) // 2)
            self.draw()
            self.box(y, x, bh, bw, " confirm compression ")
            lines = [
                (f"Images   : {len(selected)} file(s)  ({format_bytes(total_bytes)})", C.accent),
                (f"Mode     : {self.mode.name}  (quality={self.config.quality})", C.muted),
                (f"Format   : {self.config.target_format}   Max edge: {self.config.max_dimension or 'original'}", C.muted),
                (f"Output   : {self.config.output_mode}", C.warn if self.config.output_mode == "overwrite" else C.ok),
                (f"Metadata : {'strip EXIF/extra' if self.config.strip_metadata else 'preserve'}", C.muted),
                ("", 0),
                ("WARNING: Overwrite mode replaces original files!" if self.config.output_mode == "overwrite" else "Originals stay untouched in subfolder/suffix/custom mode.", C.warn if self.config.output_mode == "overwrite" else C.dim),
                ("", 0),
                ("Press [y] or [Enter] to start   [f] options   [q/Esc] cancel", C.accent),
            ]
            for row, (text, color) in enumerate(lines):
                self.add(y + 2 + row, x + 3, cut(text, bw - 6), color)
            self.screen.refresh()

            key = self.read_key(blocking=True)
            if key == 3:
                self.running = False
                return
            if key in (ord("q"), ord("Q"), 27):
                self.close_modal_guard()
                self.screen.nodelay(True)
                self.notice = "Compression cancelled."
                return
            if key == ord("f"):
                self.options_popup()
                continue
            if key in (ord("y"), ord("Y"), 10, 13):
                self.screen.nodelay(True)
                self.start_compression(selected)
                return

    def start_compression(self, selected: list[ImageEntry]) -> None:
        self.compressing = True
        self.cancel_requested = False
        self.progress_done = 0
        self.progress_total = len(selected)
        self.current_job_name = selected[0].path.name if selected else ""
        self.last_summary = None
        self.notice = f"Compressing {len(selected)} image(s)..."
        self.needs_redraw = True

        config_snapshot = CompressionConfig(
            preset_key=self.config.preset_key,
            target_format=self.config.target_format,
            quality=self.config.quality,
            png_quantize_colors=self.config.png_quantize_colors,
            lossless_webp=self.config.lossless_webp,
            max_dimension=self.config.max_dimension,
            strip_metadata=self.config.strip_metadata,
            keep_smaller=self.config.keep_smaller,
            output_mode=self.config.output_mode,
            custom_output_dir=self.config.custom_output_dir,
            subfolder_name=self.config.subfolder_name,
            suffix=self.config.suffix,
            recursive=self.config.recursive,
        )

        def on_progress(done: int, total: int, entry: ImageEntry) -> None:
            self.messages.put(("progress", done, total, entry.path.name))

        def worker() -> None:
            summary = compress_batch(
                self.entries,
                config_snapshot,
                on_progress=on_progress,
                cancel_check=lambda: self.cancel_requested,
            )
            self.messages.put(("batch_done", summary))

        threading.Thread(target=worker, daemon=True).start()

    def drain(self) -> None:
        while True:
            try:
                msg = self.messages.get_nowait()
            except queue.Empty:
                return
            if isinstance(msg, tuple) and msg:
                kind = msg[0]
                if kind == "progress":
                    _, done, total, fname = msg
                    self.progress_done = done
                    self.progress_total = total
                    self.current_job_name = fname
                    self.needs_redraw = True
                elif kind == "batch_done":
                    _, summary = msg
                    self.compressing = False
                    self.last_summary = summary
                    saved_str = format_bytes(summary.saved_bytes)
                    prefix = "Cancelled!" if summary.cancelled else "Done!"
                    delta_pct = format_delta_pct(summary.total_before_bytes, summary.total_after_bytes)
                    self.notice = (
                        f"{prefix} {summary.succeeded}/{summary.total_files} ok | "
                        f"{format_bytes(summary.total_before_bytes)} -> {format_bytes(summary.total_after_bytes)} "
                        f"(delta {saved_str}, {delta_pct})"
                    )
                    if summary.failed:
                        self.notice += f" | {summary.failed} failed (press x)"
                    self.needs_redraw = True

    def summary_popup(self) -> None:
        if not self.last_summary:
            self.notice = "No compression run completed yet."
            return
        s = self.last_summary
        done_rows = [e for e in self.entries if e.status in {"done", "failed"}]
        scroll = 0
        while True:
            h, w = self.screen.getmaxyx()
            bw = min(92, max(44, w - 6), w)
            bh = min(max(14, len(done_rows) + 11), max(10, h - 4), h)
            y = max(0, (h - bh) // 2)
            x = max(0, (w - bw) // 2)
            self.draw()
            self.box(y, x, bh, bw, " compression summary ")
            self.add(
                y + 2,
                x + 3,
                cut(
                    f"Processed: {s.total_files}  OK: {s.succeeded}  Kept Original: {s.kept_original}  Failed: {s.failed}  Time: {s.elapsed_seconds:.2f}s",
                    bw - 6,
                ),
                C.accent,
            )
            delta_lbl = "Saved" if s.saved_bytes >= 0 else "Delta"
            delta_val = format_bytes(s.saved_bytes) if s.saved_bytes >= 0 else f"+{format_bytes(-s.saved_bytes)}"
            delta_pct = format_delta_pct(s.total_before_bytes, s.total_after_bytes)
            self.add(
                y + 3,
                x + 3,
                cut(
                    f"Before: {format_bytes(s.total_before_bytes)}  ->  After: {format_bytes(s.total_after_bytes)}   {delta_lbl}: {delta_val} ({delta_pct})",
                    bw - 6,
                ),
                C.ok if s.saved_bytes >= 0 else C.warn,
            )
            list_y = y + 5
            list_h = max(3, bh - 8)
            scroll = max(0, min(scroll, max(0, len(done_rows) - list_h)))
            name_col = max(12, min(28, bw - 38))
            for row, entry in enumerate(done_rows[scroll : scroll + list_h]):
                if entry.status == "done":
                    dpct = format_delta_pct(entry.size_bytes, entry.output_bytes)
                    txt = f"{self.g.ok} {pad_right(entry.path.name, name_col)} {format_bytes(entry.size_bytes):>9} -> {format_bytes(entry.output_bytes):>9} ({dpct})"
                    col = C.ok
                else:
                    txt = f"{self.g.fail} {pad_right(entry.path.name, name_col)} ERROR: {entry.error}"
                    col = C.error
                self.add(list_y + row, x + 3, cut(txt, bw - 6), col)
            self.add(y + bh - 2, x + 3, cut("j/k scroll   [O] open output folder   q/Esc/Enter close", bw - 6), C.dim)
            self.screen.refresh()

            key = self.read_key(blocking=True)
            if key == 3:
                self.running = False
                return
            if key in (ord("q"), ord("Q"), 27, 10, 13):
                self.close_modal_guard()
                self.screen.nodelay(True)
                return
            if key in (ord("j"), curses.KEY_DOWN, -1002):
                scroll = min(scroll + 1, max(0, len(done_rows) - list_h))
            elif key in (ord("k"), curses.KEY_UP, -1001):
                scroll = max(0, scroll - 1)
            elif key == curses.KEY_MOUSE:
                delta = self.mouse_wheel_delta()
                if delta:
                    scroll = max(0, min(scroll + delta, max(0, len(done_rows) - list_h)))
            elif key == ord("O"):
                self.open_output_folder()

    def open_output_folder(self) -> None:
        target: Path | None = None
        for entry in self.entries:
            if entry.output_path and entry.output_path.parent.exists():
                target = entry.output_path.parent
                break
        if target is None and self.config.output_mode == "custom" and self.config.custom_output_dir.exists():
            target = self.config.custom_output_dir
        elif target is None and self.entries:
            target = self.entries[0].path.parent
        if target is None:
            self.notice = "No output folder available yet."
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            self.notice = f"Opened in Finder: {target}"
        elif shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            self.notice = f"Opened folder: {target}"
        else:
            self.notice = f"Output folder: {target}"

    def open_image_preview(self, prefer_output: bool = True) -> None:
        entry = self.current_entry()
        if not entry:
            self.notice = "No image selected to preview."
            return
        use_output = (
            prefer_output
            and entry.status == "done"
            and entry.output_path is not None
            and entry.output_path.exists()
        )
        target = entry.output_path if use_output and entry.output_path is not None else entry.path
        kind = "compressed" if use_output else "original"
        try:
            viewer = launch_native_preview(target)
        except Exception as exc:
            self.notice = f"Preview failed: {exc}"
            return
        self.notice = f"Opened {kind} in {viewer}: {target.name}"

    def help_popup(self) -> None:
        lines = [
            "IMG-TUI keyboard shortcuts",
            "",
            "b         open interactive file & folder browser",
            "/         type or paste/drag a file or folder path",
            "p / 1-5   switch compression mode / preset",
            "f / o     open options (format, quality, max px, output, EXIF)",
            "v / V     open full-res preview in QuickLook / viewer (v=output, V=orig)",
            "j / k     move through images (or arrows / mouse wheel)",
            "Space     toggle include/exclude on selected image",
            "a / n     select all / select none",
            "r         toggle recursive folder scan",
            "< / >     cycle filter (all, selected, pending, done, failed)",
            "s         cycle sort (name, size, ext, saved%)",
            "d / Del   remove selected image from queue",
            "X         clear entire image list",
            "Enter / c confirm and start compression",
            "x         view detailed before/after summary",
            "O         open output folder in Finder",
            "q         return to mode selector / quit",
            "",
            "Press any key to close.",
        ]
        h, w = self.screen.getmaxyx()
        bw = min(max(len(line) for line in lines) + 6, max(44, w - 4))
        bh = min(len(lines) + 4, max(10, h - 2))
        y = max(0, (h - bh) // 2)
        x = max(0, (w - bw) // 2)
        self.box(y, x, bh, bw, " help ")
        for row, line in enumerate(lines[: bh - 4]):
            self.add(y + 2 + row, x + 3, cut(line, bw - 6), C.accent if row == 0 else C.muted)
        self.screen.refresh()
        self.screen.nodelay(False)
        try:
            self.read_key(blocking=True)
        finally:
            self.screen.nodelay(True)

    def confirm_quit_popup(self) -> bool:
        cursor = 1
        options = ("Quit", "Cancel")
        while True:
            h, w = self.screen.getmaxyx()
            bw = min(54, max(38, w - 6))
            bh = 9
            y = max(1, (h - bh) // 2)
            x = max(0, (w - bw) // 2)
            self.box(y, x, bh, bw, " quit img-tui? ")
            self.add(y + 2, x + 3, cut("Are you sure you want to exit?", bw - 6), C.accent)
            option_w = 14
            total_w = option_w * len(options) + 2
            start_x = x + max(3, (bw - total_w) // 2)
            for index, option in enumerate(options):
                sel = index == cursor
                text = f" {option.center(option_w - 2)} "
                self.add(
                    y + 4,
                    start_x + index * (option_w + 2),
                    text,
                    C.accent if sel else C.muted,
                    curses.A_REVERSE if sel else 0,
                    curses.A_BOLD if sel else 0,
                )
            self.add(y + 6, x + 3, cut("Left/Right selects   Enter confirms", bw - 6), C.dim)
            self.screen.refresh()
            key = self.read_key(blocking=True)
            if key == 3:
                return True
            if key in (curses.KEY_LEFT, ord("h"), curses.KEY_RIGHT, ord("l"), 9):
                cursor = 1 - cursor
            elif key in (ord("y"), ord("Y")):
                return True
            elif key in (ord("n"), ord("N"), ord("q"), ord("Q"), 27):
                return False
            elif key in (10, 13):
                return cursor == 0

    def prompt(self, label: str, initial: str = "") -> str | None:
        buf = initial
        while True:
            h, w = self.screen.getmaxyx()
            box_w = min(max(36, w - 10), w)
            y = max(1, h // 3)
            x = max(0, (w - box_w) // 2)
            self.box(y, x, 5, box_w, f" {label} ")
            prefix = f"{label}: "
            avail = max(4, box_w - len(prefix) - 7)
            shown = buf
            while display_width(shown) > avail:
                shown = shown[1:]
            self.add(y + 2, x + 3, prefix + shown + "█", C.accent)
            self.add(y + 3, x + 3, cut("Enter confirms   Esc cancels   Ctrl+U clears", box_w - 6), C.dim)
            self.screen.refresh()
            key = self.read_key(blocking=True)
            if key in (10, 13):
                return buf.strip()
            if key == 27:
                return None
            if key == 3:
                self.running = False
                return None
            if key == 21:
                buf = ""
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                buf = buf[:-1]
            elif isinstance(key, str):
                buf += key.replace("\r", "").replace("\n", "")
            elif isinstance(key, int) and 32 <= key <= 126:
                buf += chr(key)

    def close_modal_guard(self) -> None:
        self.ignore_global_q_until = time.monotonic() + 0.05

    def focused(self, name: str) -> bool:
        return self.focus_order[self.focus_index] == name

    def box(self, y: int, x: int, h: int, w: int, title: str, focused: bool = False) -> None:
        try:
            win = self.screen.derwin(h, w, y, x)
            win.scrollok(False)
            win.idlok(False)
            border_attr = self.attr(C.focus if focused else C.border) | (curses.A_BOLD if focused else 0)
            win.attron(border_attr)
            win.box()
            win.attroff(border_attr)
            for row in range(1, max(1, h - 1)):
                win.addnstr(row, 1, " " * max(0, w - 2), max(0, w - 2))
            win.addstr(0, 2, cut(title, max(0, w - 4)), self.attr(C.focus if focused else C.accent))
        except curses.error:
            pass

    def add(self, y: int, x: int, text: str, color: int = 0, *styles: int) -> None:
        try:
            h, w = self.screen.getmaxyx()
            if y < 0 or y >= h or x < 0 or x >= w:
                return
            attr = self.attr(color)
            for style in styles:
                attr |= style
            self.screen.addnstr(y, x, text, max(0, w - x - 1), attr)
        except curses.error:
            pass

    def add_attr(self, y: int, x: int, text: str, attr: int) -> None:
        try:
            h, w = self.screen.getmaxyx()
            if y < 0 or y >= h or x < 0 or x >= w:
                return
            self.screen.addnstr(y, x, text, max(0, w - x - 1), attr)
        except curses.error:
            pass

    def attr(self, pair_id: int) -> int:
        if pair_id == 0:
            return 0
        if self.theme.mono:
            if pair_id in {C.accent, C.focus, C.selected}:
                return curses.A_BOLD
            if pair_id in {C.dim, C.muted}:
                return curses.A_DIM
            return 0
        return curses.color_pair(pair_id)

    def cursor(self, state: int) -> None:
        try:
            curses.curs_set(state)
        except curses.error:
            pass

    def enable_bracketed_paste(self) -> None:
        sys.stdout.write("\x1b[?2004h")
        sys.stdout.flush()

    def enable_mouse_tracking(self) -> None:
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
        except curses.error:
            pass
        sys.stdout.write("\x1b[?1003l\x1b[?1002l\x1b[?1000h\x1b[?1006h\x1b[?2004h")
        sys.stdout.flush()

    def handle_curses_mouse(self) -> None:
        delta = self.mouse_wheel_delta()
        if delta < 0:
            self.move_selection(-1)
        elif delta > 0:
            self.move_selection(1)

    def mouse_wheel_delta(self) -> int:
        try:
            _, _, _, _, state = curses.getmouse()
        except curses.error:
            return 0
        if hasattr(curses, "BUTTON4_PRESSED") and state & curses.BUTTON4_PRESSED:
            return -1
        if hasattr(curses, "BUTTON5_PRESSED") and state & curses.BUTTON5_PRESSED:
            return 1
        return 0

    def read_key(self, blocking: bool = False) -> int | str:
        if blocking:
            self.screen.nodelay(False)
            self.screen.timeout(50)
        try:
            while True:
                try:
                    key = self.screen.get_wch()
                    break
                except curses.error:
                    if not blocking:
                        return -1
                    if not self.running:
                        return 3
            if isinstance(key, str):
                if key == "\x1b":
                    return self.read_escape_sequence()
                code = ord(key)
                if code < 128:
                    return code
                return key
            if key == 27:
                return self.read_escape_sequence()
            return key
        finally:
            if blocking:
                self.screen.timeout(0)
                self.screen.nodelay(True)

    def read_escape_sequence(self) -> int | str:
        self.screen.nodelay(True)
        chars = ["\x1b"]
        deadline = time.monotonic() + 0.015
        is_paste = False
        while time.monotonic() < deadline:
            try:
                raw = self.screen.get_wch()
            except curses.error:
                time.sleep(0.001)
                continue
            if isinstance(raw, int):
                if is_paste:
                    continue
                break
            chars.append(raw)
            if not is_paste:
                seq = "".join(chars)
                if seq == "\x1b[200~":
                    is_paste = True
                    chars = []
                    deadline = time.monotonic() + 0.50
                    continue
                if re.match(r"\x1b\[<\d+;\d+;\d+[mM]$", seq):
                    break
                deadline = max(deadline, time.monotonic() + 0.025)
            else:
                if len(chars) >= 6 and "".join(chars[-6:]) == "\x1b[201~":
                    return "".join(chars[:-6])
                deadline = time.monotonic() + 0.50
        if is_paste:
            return "".join(chars)
        seq = "".join(chars)
        if len(chars) > 1 and not seq.startswith("\x1b["):
            next_key = ord(chars[1])
            if next_key in (ord("q"), ord("Q"), 3):
                return next_key
        match = re.match(r"\x1b\[<(?P<button>\d+);\d+;\d+[mM]", seq)
        if match:
            button = int(match.group("button"))
            if button == 64:
                return -1001
            if button == 65:
                return -1002
            return -1
        return 27

    def wait(self, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            key = self.read_key()
            if key == -1:
                time.sleep(0.02)
                continue
            if key in (ord("q"), ord("Q"), 3):
                self.running = False
                return True
            if key in (10, 13, ord(" ")):
                return True
            if isinstance(key, int):
                try:
                    curses.ungetch(key)
                except curses.error:
                    pass
            return True
        return False


def run(initial_paths: list[str] | None = None, ascii_only: bool = False) -> None:
    sys.stdout.write("\x1b[?1049h\x1b[?2004h\x1b[2J\x1b[H")
    sys.stdout.flush()
    try:
        curses.wrapper(lambda screen: Cockpit(screen, initial_paths=initial_paths, ascii_only=ascii_only).run())
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\x1b[?2004l\x1b[?1006l\x1b[?1002l\x1b[?1000l\x1b[?1049l")
        sys.stdout.flush()
