from __future__ import annotations

import math
import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageCms, ImageDraw, ImageOps

from img_tui import compressor as compressor_mod
from img_tui.app import cut, display_width, format_delta_pct, pad_right
from img_tui.compressor import (
    compress_batch,
    compress_image,
    format_bytes,
    health_checks,
    parse_user_path,
    parse_user_paths,
    probe_image,
    resolve_output_path,
    scan_inputs,
)
from img_tui.presets import CompressionConfig, preset_by_key
from img_tui.preview import launch_native_preview


def _make_photo_image(size: tuple[int, int] = (800, 600)) -> Image.Image:
    im = Image.new("RGB", size)
    draw = ImageDraw.Draw(im)
    w, h = size
    for y in range(0, h, 4):
        r = int(40 + (y / max(1, h)) * 180)
        g = int(90 + ((h - y) / max(1, h)) * 110)
        b = 160
        draw.rectangle([0, y, w, min(h, y + 4)], fill=(r, g, b))
    draw.ellipse([w // 4, h // 4, 3 * w // 4, 3 * h // 4], fill=(230, 120, 40))
    return im


def test_parse_user_path_variants(tmp_path: Path) -> None:
    spaced = tmp_path / "รูป รถ สวย.jpg"
    assert parse_user_path(f"'{spaced}'") == spaced
    assert parse_user_path(f'"{spaced}"') == spaced
    escaped = str(tmp_path / "my\\ folder\\ img.png")
    assert parse_user_path(escaped) == tmp_path / "my folder img.png"


def test_scan_inputs_recursive_and_exclusions(tmp_path: Path) -> None:
    img1 = tmp_path / "a.jpg"
    img2 = tmp_path / "รูปภาพ ไทย.png"
    hidden = tmp_path / ".secret.jpg"
    txt = tmp_path / "notes.txt"
    sub = tmp_path / "nested"
    sub.mkdir()
    img3 = sub / "b.webp"
    comp = tmp_path / "compressed"
    comp.mkdir()
    already = comp / "a.jpg"

    _make_photo_image((120, 80)).save(img1)
    _make_photo_image((120, 80)).save(img2)
    _make_photo_image((120, 80)).save(hidden)
    _make_photo_image((120, 80)).save(img3)
    _make_photo_image((120, 80)).save(already)
    txt.write_text("not an image")

    non_rec = scan_inputs([tmp_path, img1], recursive=False)
    names_non_rec = sorted(e.path.name for e in non_rec)
    assert names_non_rec == ["a.jpg", "รูปภาพ ไทย.png"]

    rec = scan_inputs([tmp_path], recursive=True)
    names_rec = sorted(e.path.name for e in rec)
    assert names_rec == ["a.jpg", "b.webp", "รูปภาพ ไทย.png"]


def test_jpg_compression_and_exif_orientation(tmp_path: Path) -> None:
    # Create 400x200 landscape image with EXIF orientation 6 (90 deg CW -> displayed 200x400)
    src = tmp_path / "portrait_exif.jpg"
    im = _make_photo_image((600, 300))
    exif = im.getexif()
    exif[274] = 6
    im.save(src, format="JPEG", quality=98, exif=exif.tobytes())

    w, h, _ = probe_image(src)
    assert (w, h) == (300, 600)

    entries = scan_inputs([src])
    cfg = CompressionConfig.from_preset(preset_by_key("balanced"))
    summary = compress_batch(entries, cfg)

    assert summary.succeeded == 1
    assert summary.failed == 0
    out_path = entries[0].output_path
    assert out_path is not None and out_path.exists()
    assert out_path.parent == tmp_path / "compressed"
    assert entries[0].output_bytes < entries[0].size_bytes

    with Image.open(out_path) as out_im:
        assert out_im.size == (300, 600)


def test_png_alpha_and_extreme_quantize(tmp_path: Path) -> None:
    src = tmp_path / "logo_alpha.png"
    im = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    draw = ImageDraw.Draw(im)
    for r in range(20, 180, 15):
        draw.ellipse([200 - r, 200 - r, 200 + r, 200 + r], fill=(r, 255 - r, 120, 200))
    im.save(src, format="PNG", compress_level=1)

    entries = scan_inputs([src])
    cfg = CompressionConfig.from_preset(preset_by_key("extreme"))
    summary = compress_batch(entries, cfg)

    assert summary.succeeded == 1
    out_path = entries[0].output_path
    assert out_path is not None and out_path.exists()
    assert entries[0].output_bytes <= entries[0].size_bytes
    with Image.open(out_path) as out_im:
        assert "A" in out_im.getbands() or out_im.mode == "P"


def test_webp_conversion_and_collision_disambiguation(tmp_path: Path) -> None:
    src1 = tmp_path / "shot.jpg"
    src2 = tmp_path / "shot.png"
    _make_photo_image((500, 400)).save(src1, format="JPEG", quality=95)
    _make_photo_image((500, 400)).save(src2, format="PNG")

    entries = scan_inputs([src1, src2])
    cfg = CompressionConfig.from_preset(preset_by_key("webp"))
    summary = compress_batch(entries, cfg)

    assert summary.succeeded == 2
    assert summary.failed == 0
    out_paths = [e.output_path for e in entries]
    assert out_paths[0] is not None and out_paths[1] is not None
    assert out_paths[0] != out_paths[1]
    assert out_paths[0].suffix == ".webp"
    assert out_paths[1].suffix == ".webp"
    assert out_paths[0].exists() and out_paths[1].exists()


def test_resize_no_upscale(tmp_path: Path) -> None:
    big = tmp_path / "big.jpg"
    small = tmp_path / "small.jpg"
    _make_photo_image((2800, 1400)).save(big, format="JPEG", quality=92)
    _make_photo_image((400, 300)).save(small, format="JPEG", quality=92)

    entries = scan_inputs([big, small])
    cfg = CompressionConfig.from_preset(preset_by_key("resize"))
    summary = compress_batch(entries, cfg)

    assert summary.succeeded == 2
    with Image.open(entries[0].output_path) as im_big:
        assert im_big.size == (1920, 960)
    with Image.open(entries[1].output_path) as im_small:
        assert im_small.size == (400, 300)


def test_keep_smaller_guard_prevents_bloat(tmp_path: Path) -> None:
    src = tmp_path / "tiny.png"
    # Already maximally compressed minimal PNG
    Image.new("RGB", (64, 64), (12, 34, 56)).save(src, format="PNG", optimize=True, compress_level=9)

    entries = scan_inputs([src])
    cfg = CompressionConfig.from_preset(preset_by_key("lossless"))
    cfg.keep_smaller = True
    summary = compress_batch(entries, cfg)

    assert summary.succeeded == 1
    assert entries[0].output_bytes <= entries[0].size_bytes


def test_corrupted_image_reports_error_and_continues(tmp_path: Path) -> None:
    bad = tmp_path / "broken.jpg"
    good = tmp_path / "good.jpg"
    bad.write_bytes(b"not a real jpeg payload")
    _make_photo_image((300, 200)).save(good, format="JPEG", quality=95)

    entries = scan_inputs([bad, good])
    cfg = CompressionConfig.from_preset(preset_by_key("balanced"))
    summary = compress_batch(entries, cfg)

    assert summary.total_files == 2
    assert summary.failed == 1
    assert summary.succeeded == 1
    bad_entry = next(e for e in entries if e.path == bad)
    good_entry = next(e for e in entries if e.path == good)
    assert bad_entry.status == "failed"
    assert bad_entry.error
    assert good_entry.status == "done"
    # Ensure no orphaned .imgtui_ temp files remain
    comp_dir = tmp_path / "compressed"
    assert not any(p.name.startswith(".imgtui_") for p in comp_dir.iterdir())


@pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("sips"),
    reason="HEIC test requires macOS sips",
)
def test_heic_probe_and_compress(tmp_path: Path) -> None:
    png_src = tmp_path / "seed.png"
    heic_src = tmp_path / "photo.heic"
    _make_photo_image((600, 400)).save(png_src)
    proc = subprocess.run(
        ["sips", "-s", "format", "heic", "-s", "formatOptions", "95", str(png_src), "--out", str(heic_src)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0 and heic_src.exists()
    png_src.unlink()

    w, h, mode = probe_image(heic_src)
    assert (w, h) == (600, 400)
    assert mode == "HEIC"

    # Convert HEIC -> WebP
    entries = scan_inputs([heic_src])
    cfg = CompressionConfig.from_preset(preset_by_key("webp"))
    summary = compress_batch(entries, cfg)
    assert summary.succeeded == 1
    assert entries[0].output_path is not None
    assert entries[0].output_path.suffix == ".webp"
    assert entries[0].output_path.exists()


def test_output_modes_suffix_custom_overwrite(tmp_path: Path) -> None:
    src = tmp_path / "pic.jpg"
    _make_photo_image((400, 300)).save(src, format="JPEG", quality=95)
    entry = scan_inputs([src])[0]

    cfg_suffix = CompressionConfig(output_mode="suffix", suffix="_min")
    assert resolve_output_path(entry, cfg_suffix) == tmp_path / "pic_min.jpg"

    custom_dir = tmp_path / "out_custom"
    cfg_custom = CompressionConfig(output_mode="custom", custom_output_dir=custom_dir)
    assert resolve_output_path(entry, cfg_custom) == custom_dir / "pic.jpg"

    cfg_over = CompressionConfig(output_mode="overwrite")
    assert resolve_output_path(entry, cfg_over) == src


def test_format_bytes_and_health_checks() -> None:
    assert format_bytes(512) == "512 B"
    assert format_bytes(2048) == "2.0 KB"
    assert format_bytes(3 * 1024 * 1024) == "3.00 MB"
    checks = health_checks(True)
    assert any(name == "Pillow" and state == "ok" for name, state, _ in checks)


def test_launch_native_preview_platforms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "missing.png"
    with pytest.raises(FileNotFoundError):
        launch_native_preview(missing)

    img = tmp_path / "sample.png"
    Image.new("RGB", (10, 10), (20, 40, 60)).save(img)

    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_popen(cmd, **kwargs):
        calls.append((list(cmd), dict(kwargs)))
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}" if name == "qlmanage" else None)
    assert launch_native_preview(img) == "QuickLook"
    assert calls[-1][0] == ["qlmanage", "-p", str(img)]
    assert calls[-1][1]["stdout"] is subprocess.DEVNULL
    assert calls[-1][1]["stderr"] is subprocess.DEVNULL
    assert calls[-1][1]["start_new_session"] is True

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/open" if name == "open" else None)
    assert launch_native_preview(img) == "Preview"
    assert calls[-1][0] == ["open", str(img)]

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None)
    assert launch_native_preview(img) == "xdg-open"
    assert calls[-1][0] == ["xdg-open", str(img)]


def test_palette_png_with_transparency_preserves_alpha_in_webp(tmp_path: Path) -> None:
    rgba = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
    draw = ImageDraw.Draw(rgba)
    draw.rectangle([30, 30, 90, 90], fill=(255, 40, 40, 255))
    pal = rgba.quantize(colors=16, method=Image.Quantize.FASTOCTREE)
    src = tmp_path / "pal_alpha.png"
    pal.save(src, format="PNG")

    entry = scan_inputs([src])[0]
    cfg = CompressionConfig.from_preset(preset_by_key("webp"))
    compress_image(entry, cfg)
    assert entry.output_path is not None and entry.output_path.exists()
    with Image.open(entry.output_path) as out_im:
        assert out_im.convert("RGBA").getpixel((0, 0))[3] == 0
        assert out_im.convert("RGBA").getpixel((60, 60))[3] == 255


def test_16bit_png_not_clamped_white(tmp_path: Path) -> None:
    i16 = Image.new("I;16", (64, 64))
    for y in range(64):
        for x in range(64):
            i16.putpixel((x, y), int((x / 63) * 65535))
    src = tmp_path / "grad16.png"
    i16.save(src, format="PNG")

    entry = scan_inputs([src])[0]
    cfg = CompressionConfig.from_preset(preset_by_key("balanced"))
    compress_image(entry, cfg)
    assert entry.output_path is not None
    with Image.open(entry.output_path) as out_im:
        gray = out_im.convert("L")
        assert gray.getpixel((0, 32)) < 15
        assert 100 <= gray.getpixel((32, 32)) <= 155
        assert gray.getpixel((63, 32)) > 240


def test_suffix_mode_protects_other_queued_inputs(tmp_path: Path) -> None:
    s1 = tmp_path / "photo.jpg"
    s2 = tmp_path / "photo_compressed.jpg"
    Image.new("RGB", (200, 200), (220, 20, 20)).save(s1, format="JPEG", quality=95)
    Image.new("RGB", (200, 200), (20, 220, 20)).save(s2, format="JPEG", quality=95)

    entries = scan_inputs([s1, s2])
    cfg = CompressionConfig(output_mode="suffix", suffix="_compressed")
    summary = compress_batch(entries, cfg)
    assert summary.succeeded == 2
    assert entries[0].output_path != s2
    with Image.open(s2) as orig_s2:
        r, g, b = orig_s2.getpixel((100, 100))
        assert g > r and g > b


def test_parse_user_paths_multi_drag_drop(tmp_path: Path) -> None:
    f1 = tmp_path / "รูป 1.jpg"
    f2 = tmp_path / "img 2.png"
    paths = parse_user_paths(f"'{f1}' '{f2}'")
    assert paths == [f1, f2]


def test_parse_user_paths_unquoted_quoted_escaped_newline_and_spaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f1 = tmp_path / "alpha.jpg"
    f2 = tmp_path / "beta.png"
    s1 = tmp_path / "รูป ที่ 1.jpg"
    s2 = tmp_path / "my photo 2.png"
    sub = tmp_path / "folder with spaces"
    sub.mkdir()
    s3 = sub / "deep img.webp"
    for p in (f1, f2, s1, s2, s3):
        p.write_bytes(b"img")

    assert parse_user_paths("   \n  ") == []
    assert parse_user_paths(str(s1)) == [s1]

    missing_spaced = tmp_path / "missing dir" / "nope.jpg"
    assert parse_user_paths(str(missing_spaced)) == [missing_spaced]

    # Unquoted space-separated (both simple and spaced absolute paths)
    assert parse_user_paths(f"{f1} {f2}") == [f1, f2]
    assert parse_user_paths(f"{s1} {s3}") == [s1, s3]

    # Single-quoted & double-quoted existing files (must not stop at first file)
    assert parse_user_paths(f"'{s1}' '{s2}'") == [s1, s2]
    assert parse_user_paths(f'"{s1}" "{s2}"') == [s1, s2]

    # Backslash-escaped Terminal drag-and-drop
    esc1 = str(s1).replace(" ", "\\ ")
    esc2 = str(s2).replace(" ", "\\ ")
    assert parse_user_paths(f"{esc1} {esc2}") == [s1, s2]

    # Newline-separated (LF and CRLF) with unquoted spaces & Thai names
    assert parse_user_paths(f"{s1}\n{s2}\n") == [s1, s2]
    assert parse_user_paths(f"{s1}\r\n{s3}\r\n") == [s1, s3]

    # Newline-separated relative paths with spaces
    monkeypatch.chdir(tmp_path)
    assert parse_user_paths(f"{s1.name}\n{s2.name}") == [Path(s1.name), Path(s2.name)]

    # Unquoted space-separated filenames containing apostrophes / double-spaces / mixed quoting
    apos1 = tmp_path / "John's photo.jpg"
    apos2 = tmp_path / "Bob's pic.png"
    dbl_sp = tmp_path / "two  spaces.jpg"
    for p in (apos1, apos2, dbl_sp):
        p.write_bytes(b"img")
    assert parse_user_paths(f"{apos1} {apos2}") == [apos1, apos2]
    assert parse_user_paths(f"{dbl_sp} {f1}") == [dbl_sp, f1]
    assert parse_user_paths(f'"{s1}" {esc2}') == [s1, s2]
    assert parse_user_paths("/nope/a'b.jpg /nope/c'd.jpg") == [
        Path("/nope/a'b.jpg"),
        Path("/nope/c'd.jpg"),
    ]
    assert parse_user_paths('"/nope/a b.jpg" "/nope/c d.png"') == [
        Path("/nope/a b.jpg"),
        Path("/nope/c d.png"),
    ]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg fallback test requires ffmpeg")
def test_heic_ffmpeg_fallback_when_sips_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform != "darwin" or not shutil.which("sips"):
        pytest.skip("Need sips once to synthesize test .heic fixture")
    png = tmp_path / "seed.png"
    heic = tmp_path / "sample.heic"
    _make_photo_image((320, 240)).save(png)
    subprocess.run(["sips", "-s", "format", "heic", str(png), "--out", str(heic)], check=True, capture_output=True)
    png.unlink()

    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "sips" else real_which(name))

    entry = scan_inputs([heic])[0]
    cfg = CompressionConfig.from_preset(preset_by_key("webp"))
    compress_image(entry, cfg)
    assert entry.output_path is not None and entry.output_path.exists()


def test_display_width_thai_alignment_and_delta_format() -> None:
    thai = "รูปภาพที่ถ่ายไว้_01.jpg"
    assert display_width(thai) == 18
    assert display_width(pad_right(thai, 22)) == 22
    assert display_width(cut(thai, 10)) <= 10
    assert format_delta_pct(100, 80) == "-20.0%"
    assert format_delta_pct(100, 150) == "+50.0%"


def _psnr_rgb(im1: Image.Image, im2: Image.Image) -> float:
    if _has_alpha(im1) or _has_alpha(im2):
        bg = Image.new("RGBA", im1.size, (255, 255, 255, 255))
        c1 = Image.alpha_composite(bg, im1.convert("RGBA")).convert("RGB")
        c2 = Image.alpha_composite(bg, im2.convert("RGBA")).convert("RGB")
    else:
        c1 = im1.convert("RGB")
        c2 = im2.convert("RGB")
    b1 = c1.tobytes()
    b2 = c2.tobytes()
    mse = sum((a - b) ** 2 for a, b in zip(b1, b2)) / len(b1)
    if mse == 0:
        return 99.9
    return 10.0 * math.log10((255.0 ** 2) / mse)


def _has_alpha(im: Image.Image) -> bool:
    return im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)


def _make_rich_png(size: tuple[int, int] = (640, 480)) -> Image.Image:
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(im)
    w, h = size
    for y in range(0, h, 2):
        r = int(20 + (y / h) * 200)
        g = int(120 + 80 * math.sin(y / 40))
        b = int(220 - (y / h) * 140)
        draw.rectangle([0, y, w, min(h, y + 2)], fill=(r, g, b, 255))
    for i in range(10):
        draw.ellipse(
            [40 + i * 50, 80, 180 + i * 50, 320],
            fill=(240 - i * 15, 60 + i * 18, 90 + i * 10, 210),
        )
    return im


def _make_animated_gif(path: Path, size: tuple[int, int] = (240, 180), n_frames: int = 6) -> None:
    frames: list[Image.Image] = []
    for idx in range(n_frames):
        im = Image.new("RGB", size, (25, 35, 55))
        draw = ImageDraw.Draw(im)
        for y in range(0, size[1], 4):
            draw.line([(0, y), (size[0], y)], fill=(40 + y % 120, 90, 180 - y % 90))
        x0 = 20 + idx * 25
        draw.ellipse([x0, 40, x0 + 80, 120], fill=(245, 150 - idx * 15, 45))
        frames.append(im)
    frames[0].save(
        path,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=80,
        loop=0,
    )


def test_mozjpeg_compression_exif_icc_and_strip(tmp_path: Path) -> None:
    if not compressor_mod._find_mozjpeg():
        pytest.skip("MozJPEG cjpeg binary not installed")

    src = tmp_path / "photo_meta.jpg"
    im = _make_photo_image((800, 600))
    icc_bytes = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    exif = im.getexif()
    exif[274] = 6  # 90 CW rotated -> output must be 600x800
    exif[271] = "ImgTuiCam"
    im.save(src, format="JPEG", quality=98, exif=exif.tobytes(), icc_profile=icc_bytes)

    # 1. Preserve EXIF + ICC when strip_metadata=False
    entry_keep = scan_inputs([src])[0]
    cfg_keep = CompressionConfig.from_preset(preset_by_key("balanced"))
    cfg_keep.strip_metadata = False
    compress_image(entry_keep, cfg_keep, dest_path=tmp_path / "keep.jpg")
    assert entry_keep.output_path is not None
    assert entry_keep.output_bytes < entry_keep.size_bytes
    with Image.open(entry_keep.output_path) as out_keep:
        assert out_keep.size == (600, 800)
        assert out_keep.info.get("icc_profile") == icc_bytes
        out_exif = out_keep.getexif()
        assert out_exif.get(271) == "ImgTuiCam"
        assert out_exif.get(274) == 1
        assert _psnr_rgb(ImageOps.exif_transpose(im), out_keep) >= 34.0

    # 2. Strip metadata when strip_metadata=True
    entry_strip = scan_inputs([src])[0]
    cfg_strip = CompressionConfig.from_preset(preset_by_key("balanced"))
    cfg_strip.strip_metadata = True
    compress_image(entry_strip, cfg_strip, dest_path=tmp_path / "strip.jpg")
    with Image.open(entry_strip.output_path) as out_strip:
        assert out_strip.size == (600, 800)
        assert "icc_profile" not in out_strip.info
        assert not out_strip.getexif().get(271)


def test_jpeg_pillow_fallback_when_mozjpeg_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "fallback.jpg"
    _make_photo_image((500, 400)).save(src, format="JPEG", quality=96)
    monkeypatch.setattr(compressor_mod, "_find_mozjpeg", lambda: None)

    entry = scan_inputs([src])[0]
    cfg = CompressionConfig.from_preset(preset_by_key("balanced"))
    compress_image(entry, cfg)
    assert entry.output_path is not None and entry.output_path.exists()
    assert entry.output_bytes < entry.size_bytes
    with Image.open(entry.output_path) as out_im:
        assert out_im.size == (500, 400)


def test_png_pngquant_oxipng_and_icc_preservation(tmp_path: Path) -> None:
    if not (compressor_mod._find_pngquant() and compressor_mod._find_oxipng()):
        pytest.skip("pngquant and oxipng required")

    src = tmp_path / "rich_ui.png"
    im = _make_rich_png((640, 480))
    icc_bytes = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    im.save(src, format="PNG", compress_level=3, icc_profile=icc_bytes)

    entry = scan_inputs([src])[0]
    cfg = CompressionConfig.from_preset(preset_by_key("balanced"))
    cfg.strip_metadata = False
    compress_image(entry, cfg)
    assert entry.output_path is not None and entry.output_path.exists()
    assert entry.output_bytes < entry.size_bytes * 0.70  # substantial iLoveIMG-grade reduction
    with Image.open(entry.output_path) as out_im:
        assert out_im.size == (640, 480)
        assert out_im.info.get("icc_profile") == icc_bytes
        assert _psnr_rgb(im, out_im) >= 34.0


def test_png_lossless_preset_is_pixel_exact(tmp_path: Path) -> None:
    src = tmp_path / "exact.png"
    im = _make_rich_png((240, 180)).convert("RGB")
    im.save(src, format="PNG", compress_level=1)

    entry = scan_inputs([src])[0]
    cfg = CompressionConfig.from_preset(preset_by_key("lossless"))
    compress_image(entry, cfg)
    assert entry.output_path is not None
    assert entry.output_bytes <= entry.size_bytes
    with Image.open(entry.output_path) as out_im:
        assert out_im.convert("RGB").tobytes() == im.tobytes()


def test_png_pillow_fallback_when_cli_tools_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "png_fb.png"
    _make_rich_png((320, 240)).save(src, format="PNG", compress_level=1)
    monkeypatch.setattr(compressor_mod, "_find_pngquant", lambda: None)
    monkeypatch.setattr(compressor_mod, "_find_oxipng", lambda: None)

    entry = scan_inputs([src])[0]
    cfg = CompressionConfig.from_preset(preset_by_key("balanced"))
    compress_image(entry, cfg)
    assert entry.output_path is not None and entry.output_path.exists()
    assert entry.output_bytes < entry.size_bytes


def test_gif_animated_compression_gifsicle_and_resize(tmp_path: Path) -> None:
    if not compressor_mod._find_gifsicle():
        pytest.skip("gifsicle required")

    src = tmp_path / "anim.gif"
    _make_animated_gif(src, size=(320, 240), n_frames=6)

    entry = scan_inputs([src])[0]
    cfg = CompressionConfig.from_preset(preset_by_key("balanced"))
    compress_image(entry, cfg, dest_path=tmp_path / "anim_opt.gif")
    assert entry.output_path is not None and entry.output_path.exists()
    assert entry.output_bytes <= entry.size_bytes
    with Image.open(entry.output_path) as out_gif:
        assert getattr(out_gif, "n_frames", 1) == 6
        assert out_gif.size == (320, 240)

    # Resize animated GIF preserving all frames
    entry_r = scan_inputs([src])[0]
    cfg_r = CompressionConfig.from_preset(preset_by_key("balanced"))
    cfg_r.max_dimension = 160
    compress_image(entry_r, cfg_r, dest_path=tmp_path / "anim_small.gif")
    with Image.open(entry_r.output_path) as out_small:
        assert getattr(out_small, "n_frames", 1) == 6
        assert max(out_small.size) <= 160


def test_gif_pillow_fallback_and_webp_conversion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "anim_fb.gif"
    _make_animated_gif(src, size=(200, 150), n_frames=4)

    monkeypatch.setattr(compressor_mod, "_find_gifsicle", lambda: None)
    entry_fb = scan_inputs([src])[0]
    cfg_fb = CompressionConfig.from_preset(preset_by_key("balanced"))
    compress_image(entry_fb, cfg_fb, dest_path=tmp_path / "out_fb.gif")
    assert entry_fb.output_path is not None and entry_fb.output_path.exists()
    with Image.open(entry_fb.output_path) as out_fb:
        assert getattr(out_fb, "n_frames", 1) == 4

    entry_webp = scan_inputs([src])[0]
    cfg_webp = CompressionConfig.from_preset(preset_by_key("webp"))
    compress_image(entry_webp, cfg_webp, dest_path=tmp_path / "out_anim.webp")
    assert entry_webp.output_path is not None and entry_webp.output_path.exists()
    with Image.open(entry_webp.output_path) as out_webp:
        assert getattr(out_webp, "n_frames", 1) == 4


def test_health_checks_reports_all_iloveimg_engines() -> None:
    checks = health_checks(True)
    names = {name for name, _, _ in checks}
    assert {"Pillow", "MozJPEG", "PNG", "cwebp", "gifsicle", "HEIC", "terminal"} <= names
    if compressor_mod._find_mozjpeg():
        assert any(n == "MozJPEG" and s == "ok" for n, s, _ in checks)
    if compressor_mod._find_pngquant() and compressor_mod._find_oxipng():
        assert any(n == "PNG" and s == "ok" for n, s, _ in checks)
    if compressor_mod._find_gifsicle():
        assert any(n == "gifsicle" and s == "ok" for n, s, _ in checks)


def _ssim_luma(im1: Image.Image, im2: Image.Image) -> float:
    if _has_alpha(im1) or _has_alpha(im2):
        bg = Image.new("RGBA", im1.size, (255, 255, 255, 255))
        g1 = Image.alpha_composite(bg, im1.convert("RGBA")).convert("L").tobytes()
        g2 = Image.alpha_composite(bg, im2.convert("RGBA")).convert("L").tobytes()
    else:
        g1 = im1.convert("L").tobytes()
        g2 = im2.convert("L").tobytes()
    n = len(g1)
    if n == 0:
        return 1.0
    mu1 = sum(g1) / n
    mu2 = sum(g2) / n
    var1 = sum((x - mu1) ** 2 for x in g1) / n
    var2 = sum((y - mu2) ** 2 for y in g2) / n
    cov = sum((x - mu1) * (y - mu2) for x, y in zip(g1, g2)) / n
    c1 = (0.01 * 255.0) ** 2
    c2 = (0.03 * 255.0) ** 2
    return ((2 * mu1 * mu2 + c1) * (2 * cov + c2)) / ((mu1 ** 2 + mu2 ** 2 + c1) * (var1 + var2 + c2))


def test_png_pngquant_preserves_exif_display_p3_and_alpha(tmp_path: Path) -> None:
    if not (compressor_mod._find_pngquant() and compressor_mod._find_oxipng()):
        pytest.skip("pngquant and oxipng required")

    rng = random.Random(42)
    im = Image.new("RGBA", (320, 240))
    px = im.load()
    for y in range(240):
        for x in range(320):
            alpha = 200 if (x - 160) ** 2 + (y - 120) ** 2 < 90 ** 2 else 0
            px[x, y] = (
                max(0, min(255, (x * 255) // 320 + rng.randint(-4, 4))),
                max(0, min(255, (y * 255) // 240 + rng.randint(-4, 4))),
                max(0, min(255, 130 + rng.randint(-4, 4))),
                alpha,
            )

    p3_path = Path("/System/Library/ColorSync/Profiles/Display P3.icc")
    icc_bytes = p3_path.read_bytes() if p3_path.exists() else ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    exif = im.getexif()
    exif[271] = "PngQuantCam"
    src = tmp_path / "noisy_meta.png"
    im.save(src, format="PNG", compress_level=3, exif=exif.tobytes(), icc_profile=icc_bytes)

    entry = scan_inputs([src])[0]
    cfg = CompressionConfig.from_preset(preset_by_key("balanced"))
    cfg.strip_metadata = False
    compress_image(entry, cfg, dest_path=tmp_path / "noisy_meta_out.png")

    assert entry.output_bytes < entry.size_bytes * 0.40
    with Image.open(entry.output_path) as out_im:
        assert out_im.mode == "P"
        assert out_im.info.get("icc_profile") == icc_bytes
        assert out_im.getexif().get(271) == "PngQuantCam"
        rgba_out = out_im.convert("RGBA")
        assert rgba_out.getpixel((5, 5))[3] == 0
        assert rgba_out.getpixel((160, 120))[3] == 200
        assert _psnr_rgb(im, out_im) >= 31.0
        assert _ssim_luma(im, out_im) >= 0.97


def test_gif_pillow_fallback_transparent_animation_and_resize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frames: list[Image.Image] = []
    for idx in range(4):
        f = Image.new("RGBA", (200, 150), (0, 0, 0, 0))
        ImageDraw.Draw(f).ellipse([20 + idx * 25, 30, 80 + idx * 25, 90], fill=(245, 50, 40, 255))
        frames.append(f)
    src = tmp_path / "trans_anim.gif"
    frames[0].save(
        src,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=80,
        loop=0,
        disposal=2,
    )

    # Optimize first with gifsicle if available so Pillow sees mixed P + RGBA iterator frames
    if compressor_mod._find_gifsicle():
        opt = tmp_path / "trans_opt.gif"
        subprocess.run([compressor_mod._find_gifsicle(), "-O3", str(src), "-o", str(opt)], check=True)
        src_for_fb = opt
    else:
        src_for_fb = src

    monkeypatch.setattr(compressor_mod, "_find_gifsicle", lambda: None)

    # 1. Unresized Pillow fallback preserves transparency & disposal (no ghosting)
    entry_nr = scan_inputs([src_for_fb])[0]
    cfg_nr = CompressionConfig.from_preset(preset_by_key("balanced"))
    compress_image(entry_nr, cfg_nr, dest_path=tmp_path / "fb_unresized.gif")
    with Image.open(entry_nr.output_path) as out_nr:
        assert getattr(out_nr, "n_frames", 1) == 4
        out_nr.seek(0)
        assert out_nr.convert("RGBA").getpixel((0, 0))[3] == 0
        out_nr.seek(2)
        assert out_nr.convert("RGBA").getpixel((0, 0))[3] == 0
        # Area occupied in frame 0 (x=30, y=60) must clear back to transparent in frame 2
        assert out_nr.convert("RGBA").getpixel((30, 60))[3] == 0

    # 2. Resized Pillow fallback preserves transparency & disposal
    entry_r = scan_inputs([src_for_fb])[0]
    cfg_r = CompressionConfig.from_preset(preset_by_key("balanced"))
    cfg_r.max_dimension = 100
    compress_image(entry_r, cfg_r, dest_path=tmp_path / "fb_resized.gif")
    with Image.open(entry_r.output_path) as out_r:
        assert getattr(out_r, "n_frames", 1) == 4
        assert out_r.size == (100, 75)
        out_r.seek(0)
        assert out_r.convert("RGBA").getpixel((0, 0))[3] == 0
        out_r.seek(2)
        assert out_r.convert("RGBA").getpixel((0, 0))[3] == 0
        assert out_r.convert("RGBA").getpixel((15, 30))[3] == 0


def test_gif_animated_to_webp_gif2webp_and_resize(tmp_path: Path) -> None:
    src = tmp_path / "anim_w.gif"
    _make_animated_gif(src, size=(240, 180), n_frames=5)

    # Direct unresized animated GIF -> WebP (exercises gif2webp + Pillow comparison)
    entry = scan_inputs([src])[0]
    cfg = CompressionConfig.from_preset(preset_by_key("webp"))
    compress_image(entry, cfg, dest_path=tmp_path / "anim_w.webp")
    assert entry.output_path is not None and entry.output_path.exists()
    with Image.open(entry.output_path) as out_w:
        assert getattr(out_w, "n_frames", 1) == 5
        assert out_w.size == (240, 180)

    # Resized animated GIF -> WebP
    entry_r = scan_inputs([src])[0]
    cfg_r = CompressionConfig.from_preset(preset_by_key("webp"))
    cfg_r.max_dimension = 120
    compress_image(entry_r, cfg_r, dest_path=tmp_path / "anim_w_small.webp")
    with Image.open(entry_r.output_path) as out_wr:
        assert getattr(out_wr, "n_frames", 1) == 5
        assert out_wr.size == (120, 90)


def test_lossless_preset_options_override_honored(tmp_path: Path) -> None:
    if not (compressor_mod._find_pngquant() and compressor_mod._find_gifsicle()):
        pytest.skip("pngquant and gifsicle required")

    # 1. Starting from lossless preset and enabling PNG Quantize (256) should quantize PNG
    rng = random.Random(99)
    im = Image.new("RGB", (240, 180))
    px = im.load()
    for y in range(180):
        for x in range(240):
            px[x, y] = (
                max(0, min(255, (x * 255) // 240 + rng.randint(-10, 10))),
                max(0, min(255, (y * 255) // 180 + rng.randint(-10, 10))),
                128,
            )
    png_src = tmp_path / "override.png"
    im.save(png_src, format="PNG", compress_level=3)

    cfg_png = CompressionConfig.from_preset(preset_by_key("lossless"))
    cfg_png.png_quantize_colors = 256
    cfg_png.quality = 80
    e_png = scan_inputs([png_src])[0]
    compress_image(e_png, cfg_png, dest_path=tmp_path / "override_q.png")
    with Image.open(e_png.output_path) as out_p:
        assert out_p.mode == "P"

    # 2. Starting from lossless preset and lowering quality + disabling lossless_webp uses lossy GIF
    gif_src = tmp_path / "override.gif"
    gif_frames: list[Image.Image] = []
    for idx in range(5):
        fr = Image.new("RGB", (240, 180))
        fpx = fr.load()
        for yy in range(180):
            for xx in range(240):
                fpx[xx, yy] = (
                    (xx + idx * 7 + rng.randint(-6, 6)) % 256,
                    (yy * 255 // 180 + rng.randint(-6, 6)) % 256,
                    140,
                )
        ImageDraw.Draw(fr).ellipse([20 + idx * 30, 40, 90 + idx * 30, 110], fill=(245, 110, 40))
        gif_frames.append(fr.convert("P", palette=Image.Palette.ADAPTIVE))
    gif_frames[0].save(
        gif_src,
        format="GIF",
        save_all=True,
        append_images=gif_frames[1:],
        duration=80,
        loop=0,
    )
    cfg_lossless_gif = CompressionConfig.from_preset(preset_by_key("lossless"))
    e_ll = scan_inputs([gif_src])[0]
    compress_image(e_ll, cfg_lossless_gif, dest_path=tmp_path / "gif_ll.gif")

    cfg_lossy_gif = CompressionConfig.from_preset(preset_by_key("lossless"))
    cfg_lossy_gif.quality = 60
    cfg_lossy_gif.lossless_webp = False
    e_lo = scan_inputs([gif_src])[0]
    compress_image(e_lo, cfg_lossy_gif, dest_path=tmp_path / "gif_lo.gif")
    assert e_lo.output_bytes < e_ll.output_bytes


def test_iloveimg_pipeline_benchmark_and_integrity(tmp_path: Path) -> None:
    if not (
        compressor_mod._find_mozjpeg()
        and compressor_mod._find_pngquant()
        and compressor_mod._find_oxipng()
        and compressor_mod._find_gifsicle()
        and compressor_mod._find_cwebp()
    ):
        pytest.skip("Full iLoveIMG benchmark requires mozjpeg, pngquant, oxipng, gifsicle, and cwebp")

    import io

    # 1. JPEG: MozJPEG Q80 vs Pillow baseline Q80 + PSNR/SSIM
    jpg_src = tmp_path / "bench_photo.jpg"
    jpg_im = _make_photo_image((800, 600))
    jpg_im.save(jpg_src, format="JPEG", quality=96)
    e_jpg = scan_inputs([jpg_src])[0]
    cfg_bal = CompressionConfig.from_preset(preset_by_key("balanced"))
    compress_image(e_jpg, cfg_bal, dest_path=tmp_path / "bench_moz.jpg")
    pil_buf = io.BytesIO()
    jpg_im.save(pil_buf, format="JPEG", quality=80, optimize=True, progressive=True)
    assert e_jpg.output_bytes <= len(pil_buf.getvalue())
    assert e_jpg.output_bytes < e_jpg.size_bytes * 0.60
    with Image.open(e_jpg.output_path) as out_j:
        assert _psnr_rgb(jpg_im, out_j) >= 35.0
        assert _ssim_luma(jpg_im, out_j) >= 0.95

    # 2. PNG: pngquant+oxipng achieves strong size reduction vs lossless Pillow AND higher PSNR than FASTOCTREE
    png_src = tmp_path / "bench_ui.png"
    png_im = _make_rich_png((640, 480))
    png_im.save(png_src, format="PNG", optimize=True, compress_level=9)
    e_png = scan_inputs([png_src])[0]
    compress_image(e_png, cfg_bal, dest_path=tmp_path / "bench_pq.png")
    pil_png_buf = io.BytesIO()
    png_im.quantize(colors=256, method=Image.Quantize.FASTOCTREE).save(pil_png_buf, format="PNG", optimize=True)
    assert e_png.output_bytes < e_png.size_bytes * 0.70
    with Image.open(e_png.output_path) as out_p, Image.open(io.BytesIO(pil_png_buf.getvalue())) as out_pil_q:
        assert _psnr_rgb(png_im, out_p) >= _psnr_rgb(png_im, out_pil_q)
        assert _psnr_rgb(png_im, out_p) >= 38.0
        assert _ssim_luma(png_im, out_p) >= 0.98

    # 3. GIF: gifsicle -O3 --lossy on animated GIF with smooth gradient background + moving object
    gif_src = tmp_path / "bench_anim.gif"
    gframes: list[Image.Image] = []
    for idx in range(6):
        fr = Image.new("RGB", (240, 180))
        fpx = fr.load()
        for yy in range(180):
            for xx in range(240):
                fpx[xx, yy] = (
                    int(128 + 110 * math.sin(xx / 22.0)),
                    int(128 + 110 * math.cos(yy / 26.0)),
                    150,
                )
        ImageDraw.Draw(fr).ellipse([20 + idx * 25, 40, 90 + idx * 25, 110], fill=(245, 130, 45))
        gframes.append(fr.convert("P", palette=Image.Palette.ADAPTIVE))
    gframes[0].save(gif_src, format="GIF", save_all=True, append_images=gframes[1:], duration=80, loop=0)
    e_gif = scan_inputs([gif_src])[0]
    compress_image(e_gif, cfg_bal, dest_path=tmp_path / "bench_out.gif")
    assert e_gif.output_bytes < e_gif.size_bytes * 0.70
    with Image.open(e_gif.output_path) as out_g:
        assert getattr(out_g, "n_frames", 1) == 6

    # 4. WebP: cwebp -sharp_yuv Q80 + PSNR/SSIM
    webp_src = tmp_path / "bench_pic.webp"
    jpg_im.save(webp_src, format="WEBP", quality=96, method=6)
    e_webp = scan_inputs([webp_src])[0]
    compress_image(e_webp, cfg_bal, dest_path=tmp_path / "bench_out.webp")
    assert e_webp.output_bytes < e_webp.size_bytes * 0.70
    with Image.open(e_webp.output_path) as out_w:
        assert _psnr_rgb(jpg_im, out_w) >= 35.0
        assert _ssim_luma(jpg_im, out_w) >= 0.95
