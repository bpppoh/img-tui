from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import struct
import subprocess
import termios
import time
from pathlib import Path

from PIL import Image, ImageDraw


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\([A-Za-z0-9]")


def _strip_ansi(data: bytes) -> str:
    return ANSI_RE.sub("", data.decode("utf-8", errors="ignore"))


def _create_sample_image(path: Path) -> None:
    im = Image.new("RGB", (640, 480))
    draw = ImageDraw.Draw(im)
    for y in range(0, 480, 8):
        draw.rectangle([0, y, 640, y + 8], fill=(30, y // 2, 200))
    draw.ellipse([160, 120, 480, 360], fill=(240, 140, 50))
    im.save(path, format="JPEG", quality=96)


def _spawn_pty_img_tui(
    args: list[str],
    cwd: Path | None = None,
    rows: int = 36,
    cols: int = 120,
) -> tuple[subprocess.Popen[bytes], int]:
    master_fd, slave_fd = pty.openpty()
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    def _setup_tty() -> None:
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

    proc = subprocess.Popen(
        [os.sys.executable, "-m", "img_tui", "--ascii", *args],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        cwd=str(cwd) if cwd else None,
        preexec_fn=_setup_tty,
        close_fds=True,
    )
    os.close(slave_fd)
    return proc, master_fd


def _read_until_text(
    fd: int,
    needle: str,
    timeout: float = 5.0,
    raw_out: bytearray | None = None,
) -> str:
    end = time.monotonic() + timeout
    buf = bytearray()
    while time.monotonic() < end:
        r, _, _ = select.select([fd], [], [], 0.05)
        if fd in r:
            try:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                buf.extend(chunk)
                if raw_out is not None:
                    raw_out.extend(chunk)
                plain = _strip_ansi(bytes(buf))
                if needle in plain:
                    return plain
            except OSError:
                break
    return _strip_ansi(bytes(buf))


def _wait_and_drain(proc: subprocess.Popen[bytes], fd: int, timeout: float = 5.0) -> int:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        rc = proc.poll()
        if rc is not None:
            return rc
        r, _, _ = select.select([fd], [], [], 0.05)
        if fd in r:
            try:
                os.read(fd, 4096)
            except OSError:
                pass
    raise TimeoutError("Timed out waiting for img-tui process to exit")


def test_tui_pty_end_to_end_compression(tmp_path: Path) -> None:
    img_path = tmp_path / "sample photo.jpg"
    _create_sample_image(img_path)
    orig_size = img_path.stat().st_size

    proc, master_fd = _spawn_pty_img_tui([str(tmp_path)])
    try:
        # 1. Mode picker renders
        screen1 = _read_until_text(master_fd, "Balanced", timeout=5.0)
        assert "Balanced" in screen1

        # 2. Select Balanced mode -> wait for Cockpit to render
        os.write(master_fd, b"\r")
        screen2 = _read_until_text(master_fd, "Compression Queue", timeout=6.0)
        assert "Compression Queue" in screen2
        assert "sample photo.jpg" in screen2

        # 3. Open confirm compression popup ('c') -> start ('y')
        os.write(master_fd, b"c")
        screen3 = _read_until_text(master_fd, "confirm compression", timeout=4.0)
        assert "confirm compression" in screen3
        os.write(master_fd, b"y")

        # 4. Wait for TUI to show "Done!" and verify compressed file on disk
        done_screen = _read_until_text(master_fd, "Done!", timeout=8.0)
        assert "Done!" in done_screen
        out_file = tmp_path / "compressed" / "sample photo.jpg"
        assert out_file.exists(), "TUI did not create compressed output file"
        assert 0 < out_file.stat().st_size < orig_size

        # 5. Open summary popup ('x') and close ('q')
        os.write(master_fd, b"x")
        summary_screen = _read_until_text(master_fd, "compression summary", timeout=4.0)
        assert "compression summary" in summary_screen
        os.write(master_fd, b"q")
        cockpit_back = _read_until_text(master_fd, "Compression Queue", timeout=4.0)
        assert "Compression Queue" in cockpit_back
        time.sleep(0.1)

        # 6. Exit cleanly: 'q' -> mode picker -> 'q' -> confirm quit -> 'y'
        os.write(master_fd, b"q")
        picker_screen = _read_until_text(master_fd, "compression mode", timeout=4.0)
        assert "compression mode" in picker_screen
        os.write(master_fd, b"q")
        quit_screen = _read_until_text(master_fd, "quit img-tui?", timeout=4.0)
        assert "quit img-tui?" in quit_screen
        os.write(master_fd, b"y")

        rc = _wait_and_drain(proc, master_fd, timeout=5.0)
        assert rc == 0
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


def test_tui_pty_browse_and_options_webp(tmp_path: Path) -> None:
    folder = tmp_path / "photos"
    folder.mkdir()
    img_path = folder / "pic.png"
    _create_sample_image(img_path)

    proc, master_fd = _spawn_pty_img_tui([], cwd=folder)
    try:
        _read_until_text(master_fd, "WebP Convert", timeout=5.0)
        # Switch to WebP Convert mode (down 3 times -> Enter)
        os.write(master_fd, b"jjj\r")
        _read_until_text(master_fd, "Compression Queue", timeout=6.0)

        # Open browser ('b') in CWD (`folder`) and add all ('a')
        os.write(master_fd, b"b")
        browse_screen = _read_until_text(master_fd, "browse:", timeout=4.0)
        assert "pic.png" in browse_screen
        os.write(master_fd, b"a")
        loaded_screen = _read_until_text(master_fd, "Added 1 image", timeout=4.0)
        assert "Added 1 image" in loaded_screen

        # Compress ('c' -> 'y') -> converts pic.png to compressed/pic.webp
        os.write(master_fd, b"c")
        _read_until_text(master_fd, "confirm compression", timeout=4.0)
        os.write(master_fd, b"y")
        _read_until_text(master_fd, "Done!", timeout=8.0)

        out_webp = folder / "compressed" / "pic.webp"
        assert out_webp.exists()
        assert out_webp.stat().st_size > 0

        # Quit cleanly
        os.write(master_fd, b"q")
        _read_until_text(master_fd, "compression mode", timeout=4.0)
        os.write(master_fd, b"q")
        _read_until_text(master_fd, "quit img-tui?", timeout=4.0)
        os.write(master_fd, b"y")

        rc = _wait_and_drain(proc, master_fd, timeout=5.0)
        assert rc == 0
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


def test_tui_pty_thai_path_options_help_no_double_minus_and_ctrl_c(tmp_path: Path) -> None:
    thai_img = tmp_path / "รูปภาพที่ถ่ายไว้.png"
    Image.new("RGB", (64, 64), (20, 40, 60)).save(thai_img, format="PNG", optimize=True, compress_level=9)

    proc, master_fd = _spawn_pty_img_tui([], cwd=tmp_path)
    try:
        _read_until_text(master_fd, "Lossless", timeout=5.0)
        # Choose Lossless mode (index 2 -> jj + Enter)
        os.write(master_fd, b"jj\r")
        _read_until_text(master_fd, "Compression Queue", timeout=6.0)

        # Open help popup ('?') and close it
        os.write(master_fd, b"?")
        help_scr = _read_until_text(master_fd, "IMG-TUI keyboard shortcuts", timeout=4.0)
        assert "IMG-TUI keyboard shortcuts" in help_scr
        os.write(master_fd, b" ")
        _read_until_text(master_fd, "Compression Queue", timeout=4.0)

        # Load Thai path via '/' using bracketed paste
        os.write(master_fd, b"/")
        _read_until_text(master_fd, "Esc to cancel", timeout=4.0)
        payload = f"\x1b[200~{thai_img}\x1b[201~\r".encode("utf-8")
        os.write(master_fd, payload)
        loaded = _read_until_text(master_fd, "Loaded 1 new image", timeout=4.0)
        assert "Loaded 1 new image" in loaded

        # Open options ('f'), switch Target Format -> jpg (down once, right twice), and lower Quality (down once, left)
        os.write(master_fd, b"f")
        _read_until_text(master_fd, "Lossless WebP", timeout=4.0)
        os.write(master_fd, b"jlljh")
        # Wait briefly and verify Lossless WebP auto-disabled when quality changed
        # Note: j (Target Format), l, l (webp -> jpg), j (Quality), h (Quality -5 -> Lossless WebP: no)
        opt_scr = _read_until_text(master_fd, "no (lossy quality)", timeout=4.0)
        assert "no (lossy quality)" in opt_scr
        os.write(master_fd, b"q")
        _read_until_text(master_fd, "actions", timeout=4.0)
        time.sleep(0.08)

        # Compress tiny PNG -> larger JPG and verify no '--' double-minus in summary
        os.write(master_fd, b"c")
        _read_until_text(master_fd, "confirm compression", timeout=4.0)
        os.write(master_fd, b"y")
        _read_until_text(master_fd, "Done!", timeout=6.0)
        os.write(master_fd, b"x")
        sum_scr = _read_until_text(master_fd, "compression summary", timeout=4.0)
        assert "compression summary" in sum_scr
        assert "--" not in sum_scr

        # Close summary and test clean Ctrl+C exit (rc == 0)
        os.write(master_fd, b"q")
        _read_until_text(master_fd, "Compression Queue", timeout=4.0)
        time.sleep(0.08)
        os.write(master_fd, b"\x03")
        rc = _wait_and_drain(proc, master_fd, timeout=5.0)
        assert rc == 0
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


def test_tui_pty_small_terminal_and_unreadable_cwd(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0o111)
    proc, master_fd = _spawn_pty_img_tui([], cwd=locked, rows=20, cols=50)
    try:
        scr = _read_until_text(master_fd, "Balanced", timeout=5.0)
        assert "Balanced" in scr
        # Box border should render cleanly even at 50x20 (with --ascii uses + / - / | or ACS)
        assert "compression mode" in scr
        # Select Balanced -> enter Cockpit -> resize message or open browse ('b') without crashing
        os.write(master_fd, b"\r")
        _read_until_text(master_fd, "Resize terminal", timeout=5.0)
        os.write(master_fd, b"b")
        browse = _read_until_text(master_fd, "browse:", timeout=4.0)
        assert "browse:" in browse
        os.write(master_fd, b"\x03")
        rc = _wait_and_drain(proc, master_fd, timeout=5.0)
        assert rc == 0
    finally:
        os.chmod(locked, 0o755)
        try:
            os.close(master_fd)
        except OSError:
            pass
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


def test_tui_pty_direct_cockpit_and_pathbar_multi_file_drag_drop(tmp_path: Path) -> None:
    img1 = tmp_path / "alpha.jpg"
    img2 = tmp_path / "รูป ที่ 1.jpg"
    sub = tmp_path / "folder with spaces"
    sub.mkdir()
    img3 = sub / "quoted img.jpg"
    img4 = sub / "escaped รูป 4.jpg"
    img5 = tmp_path / "nl one.jpg"
    img6 = tmp_path / "nl สอง.jpg"
    for p in (img1, img2, img3, img4, img5, img6):
        _create_sample_image(p)

    raw_stream = bytearray()
    proc, master_fd = _spawn_pty_img_tui([], cwd=tmp_path)
    try:
        _read_until_text(master_fd, "Balanced", timeout=5.0, raw_out=raw_stream)
        os.write(master_fd, b"\r")
        _read_until_text(master_fd, "Compression Queue", timeout=6.0, raw_out=raw_stream)

        # Bracketed paste mode (?2004h) must remain emitted after curses initializes
        assert raw_stream.count(b"\x1b[?2004h") >= 2

        # 1. Direct Cockpit drop (no '/' pressed): unquoted space-separated + Thai spaces
        drop1 = f"\x1b[200~{img1} {img2}\x1b[201~".encode("utf-8")
        os.write(master_fd, drop1)
        scr1 = _read_until_text(master_fd, "Loaded 2 new image(s) from 2 paths.", timeout=4.0)
        assert "Loaded 2 new image(s) from 2 paths." in scr1
        assert "alpha.jpg" in scr1

        # 2. Direct Cockpit drop: quoted + backslash-escaped Thai path
        esc4 = str(img4).replace(" ", "\\ ")
        drop2 = f"\x1b[200~'{img3}' {esc4}\x1b[201~".encode("utf-8")
        os.write(master_fd, drop2)
        scr2 = _read_until_text(master_fd, "4 of 4", timeout=4.0)
        assert "4 of 4" in scr2
        assert "quoted img.jpg" in scr2
        assert "escaped รูป 4.jpg" in scr2

        # 3. '/' path bar: newline-separated (CRLF + LF) pasted paths
        os.write(master_fd, b"/")
        _read_until_text(master_fd, "Esc to cancel", timeout=4.0)
        drop3 = f"\x1b[200~{img5}\r\n{img6}\n\x1b[201~\r".encode("utf-8")
        os.write(master_fd, drop3)
        scr3 = _read_until_text(master_fd, "6 of 6", timeout=4.0)
        assert "6 of 6" in scr3

        # 4. Compress all 6 loaded images end-to-end
        os.write(master_fd, b"c")
        _read_until_text(master_fd, "confirm compression", timeout=4.0)
        os.write(master_fd, b"y")
        done = _read_until_text(master_fd, "Done! 6/6 ok", timeout=10.0)
        assert "Done! 6/6 ok" in done

        for p in (img1, img2, img3, img4, img5, img6):
            out_file = p.parent / "compressed" / p.name
            assert out_file.exists(), f"Missing compressed output for {p}"

        os.write(master_fd, b"\x03")
        rc = _wait_and_drain(proc, master_fd, timeout=5.0)
        assert rc == 0
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


def test_tui_pty_chunked_bracketed_paste_and_mode_selector_drop(tmp_path: Path) -> None:
    img1 = tmp_path / "John's photo.jpg"
    img2 = tmp_path / "Bob's pic.jpg"
    img3 = tmp_path / "chunked รูป 3.jpg"
    for p in (img1, img2, img3):
        _create_sample_image(p)

    proc, master_fd = _spawn_pty_img_tui([], cwd=tmp_path)
    try:
        _read_until_text(master_fd, "Balanced", timeout=5.0)

        # Drop onto mode selector -> preloads without polluting mode filter query
        os.write(master_fd, f"\x1b[200~{img1} {img2}\x1b[201~".encode("utf-8"))
        preloaded = _read_until_text(master_fd, "ready: 2 image(s) preloaded", timeout=4.0)
        assert "ready: 2 image(s) preloaded" in preloaded

        os.write(master_fd, b"\r")
        scr0 = _read_until_text(master_fd, "2 of 2", timeout=6.0)
        assert "John's photo.jpg" in scr0
        assert "Bob's pic.jpg" in scr0

        # Split CSI 200~ prefix AND pause 70ms mid-paste across PTY chunks
        os.write(master_fd, b"\x1b[")
        time.sleep(0.008)
        os.write(master_fd, f"200~{img1} ".encode("utf-8"))
        time.sleep(0.070)
        os.write(master_fd, f"{img3}\x1b[201~".encode("utf-8"))
        scr1 = _read_until_text(master_fd, "3 of 3", timeout=4.0)
        assert "3 of 3" in scr1
        os.write(master_fd, b"j")
        scr2 = _read_until_text(master_fd, "chunked รูป 3.jpg", timeout=4.0)
        assert "chunked รูป 3.jpg" in scr2

        os.write(master_fd, b"\x03")
        rc = _wait_and_drain(proc, master_fd, timeout=5.0)
        assert rc == 0
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
