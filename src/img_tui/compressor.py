from __future__ import annotations

import io
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PIL import Image, ImageOps, ImageSequence

from .presets import CompressionConfig


SUPPORTED_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".heic",
    ".heif",
    ".bmp",
    ".tiff",
    ".tif",
}
HEIC_EXTENSIONS = {".heic", ".heif"}


@dataclass
class ImageEntry:
    path: Path
    size_bytes: int
    ext: str
    width: int = 0
    height: int = 0
    mode: str = ""
    selected: bool = True
    status: str = "pending"  # pending | running | done | skipped | failed
    output_path: Path | None = None
    output_bytes: int = 0
    kept_original: bool = False
    error: str = ""
    note: str = ""

    @property
    def saved_bytes(self) -> int:
        if self.status != "done" or self.output_bytes <= 0:
            return 0
        return self.size_bytes - self.output_bytes

    @property
    def saved_percent(self) -> float:
        if self.status != "done" or self.size_bytes <= 0 or self.output_bytes <= 0:
            return 0.0
        return ((self.size_bytes - self.output_bytes) / self.size_bytes) * 100.0


@dataclass
class BatchSummary:
    total_files: int = 0
    succeeded: int = 0
    kept_original: int = 0
    failed: int = 0
    total_before_bytes: int = 0
    total_after_bytes: int = 0
    elapsed_seconds: float = 0.0
    cancelled: bool = False
    errors: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def saved_bytes(self) -> int:
        return self.total_before_bytes - self.total_after_bytes

    @property
    def saved_percent(self) -> float:
        if self.total_before_bytes <= 0:
            return 0.0
        return (self.saved_bytes / self.total_before_bytes) * 100.0


def format_bytes(size: int) -> str:
    if size < 0:
        return f"-{format_bytes(-size)}"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.2f} MB"
    return f"{size / (1024 * 1024 * 1024):.2f} GB"


def parse_user_path(raw: str) -> Path:
    text = raw.strip()
    if not text:
        return Path.cwd()
    direct = Path(text).expanduser()
    if direct.exists():
        return direct
    # Handle quoted paths or Terminal drag-and-drop backslash-escaped spaces
    if (text.startswith("'") and text.endswith("'")) or (text.startswith('"') and text.endswith('"')):
        try:
            parts = shlex.split(text)
            if len(parts) == 1:
                text = parts[0]
            elif parts:
                text = text[1:-1]
        except ValueError:
            text = text[1:-1]
    elif "\\" in text:
        try:
            parts = shlex.split(text)
            if parts:
                text = " ".join(parts)
        except ValueError:
            text = text.replace("\\ ", " ")
    return Path(text).expanduser()


def _parse_line_paths(line: str) -> list[Path]:
    text = line.strip()
    if not text:
        return []

    direct = Path(text).expanduser()
    if direct.exists():
        return [direct]

    abs_parts = [p.strip() for p in re.split(r"\s+(?=(?:/|~/))", text) if p.strip()]
    has_shell_escape = "\\" in text
    has_token_quote = bool(re.search(r'(?:^|\s)[\'"]', text))

    if any(ch in text for ch in ("'", '"', "\\")):
        try:
            parts = [p for p in shlex.split(text) if p.strip()]
            if parts:
                candidates = [Path(p).expanduser() for p in parts]
                if len(abs_parts) > 1:
                    abs_candidates = [parse_user_path(p) for p in abs_parts]
                    shlex_existing = sum(1 for c in candidates if c.exists())
                    abs_existing = sum(1 for c in abs_candidates if c.exists())
                    if abs_existing > shlex_existing:
                        return abs_candidates
                    if shlex_existing == 0 and abs_existing == 0 and not (
                        has_shell_escape or has_token_quote
                    ):
                        return abs_candidates
                if len(candidates) > 1 or any(c.exists() for c in candidates):
                    return candidates
                return candidates
        except ValueError:
            pass

    if len(abs_parts) > 1:
        return [parse_user_path(p) for p in abs_parts]

    tokens = text.split()
    if len(tokens) > 1:
        token_paths = [Path(t).expanduser() for t in tokens]
        if (
            all(p.exists() for p in token_paths)
            or all(t.startswith(("./", "../", "~/")) for t in tokens)
            or (sum(1 for p in token_paths if p.is_file()) >= 1 and all(Path(t).suffix for t in tokens))
        ):
            return token_paths

    return [parse_user_path(text)]


def parse_user_paths(raw: str) -> list[Path]:
    text = raw.strip()
    if not text:
        return []
    if "\n" in text or "\r" in text:
        lines = [
            ln.strip()
            for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            if ln.strip()
        ]
        paths: list[Path] = []
        for ln in lines:
            paths.extend(_parse_line_paths(ln))
        return paths
    return _parse_line_paths(text)


def is_supported_image(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS and not path.name.startswith(".")


def probe_image(path: Path) -> tuple[int, int, str]:
    ext = path.suffix.lower()
    if ext in HEIC_EXTENSIONS:
        if sys.platform == "darwin" and shutil.which("sips"):
            try:
                proc = subprocess.run(
                    ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                w = h = 0
                for line in proc.stdout.splitlines():
                    if "pixelWidth:" in line:
                        w = int(line.split(":", 1)[1].strip())
                    elif "pixelHeight:" in line:
                        h = int(line.split(":", 1)[1].strip())
                if w > 0 and h > 0:
                    return w, h, "HEIC"
            except Exception:
                pass
        return 0, 0, "HEIC"
    try:
        with Image.open(path) as im:
            w, h = im.size
            # Account for EXIF orientation tag 274 (Orientation 5..8 swap width/height)
            exif = im.getexif()
            orientation = exif.get(274, 1) if exif else 1
            if orientation in (5, 6, 7, 8):
                w, h = h, w
            return w, h, im.mode or ext.lstrip(".").upper()
    except Exception:
        return 0, 0, ext.lstrip(".").upper()


def scan_inputs(
    paths: list[Path | str],
    recursive: bool = False,
    skip_dir_names: tuple[str, ...] = ("compressed",),
) -> list[ImageEntry]:
    entries: list[ImageEntry] = []
    seen: set[Path] = set()
    skip_set = {name.lower() for name in skip_dir_names}

    def add_file(file_path: Path) -> None:
        try:
            resolved = file_path.resolve()
        except OSError:
            resolved = file_path
        if resolved in seen or not is_supported_image(file_path):
            return
        seen.add(resolved)
        try:
            size = file_path.stat().st_size
        except OSError:
            return
        w, h, mode = probe_image(file_path)
        entries.append(
            ImageEntry(
                path=file_path,
                size_bytes=size,
                ext=file_path.suffix.lower(),
                width=w,
                height=h,
                mode=mode,
            )
        )

    for raw in paths:
        p = raw if isinstance(raw, Path) else parse_user_path(str(raw))
        if not p.exists():
            continue
        if p.is_file():
            add_file(p)
        elif p.is_dir():
            if recursive:
                for root, dirs, files in os.walk(p):
                    dirs[:] = sorted(
                        d for d in dirs if not d.startswith(".") and d.lower() not in skip_set
                    )
                    for fname in sorted(files):
                        if not fname.startswith("."):
                            add_file(Path(root) / fname)
            else:
                for child in sorted(p.iterdir(), key=lambda item: item.name.lower()):
                    if child.is_file():
                        add_file(child)
    return entries


def resolve_output_ext(src_ext: str, target_format: str) -> str:
    fmt = target_format.lower()
    if fmt == "keep":
        if src_ext in (".bmp", ".tif", ".tiff"):
            return ".png"
        if src_ext == ".jpeg":
            return ".jpg"
        return src_ext
    if fmt == "jpg":
        return ".jpg"
    if fmt == "png":
        return ".png"
    if fmt == "webp":
        return ".webp"
    return src_ext


def resolve_output_path(
    entry: ImageEntry,
    config: CompressionConfig,
    used_paths: set[Path] | None = None,
) -> Path:
    out_ext = resolve_output_ext(entry.ext, config.target_format)
    stem = entry.path.stem
    if config.output_mode == "overwrite":
        if out_ext == entry.ext or (out_ext == ".jpg" and entry.ext == ".jpeg"):
            return entry.path
        candidate_dir = entry.path.parent
    elif config.output_mode == "suffix":
        candidate_dir = entry.path.parent
        stem = f"{stem}{config.suffix}"
    elif config.output_mode == "custom":
        candidate_dir = config.custom_output_dir
    else:
        candidate_dir = entry.path.parent / config.subfolder_name

    candidate = candidate_dir / f"{stem}{out_ext}"
    if used_paths is not None:
        resolved = candidate.resolve()
        counter = 1
        while resolved in used_paths or (
            candidate == entry.path and config.output_mode != "overwrite"
        ):
            candidate = candidate_dir / f"{stem}_{counter}{out_ext}"
            resolved = candidate.resolve()
            counter += 1
        used_paths.add(resolved)
    return candidate


def _atomic_write_bytes(dest: Path, payload: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".imgtui_", suffix=dest.suffix, dir=str(dest.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, dest)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _atomic_copy_file(src: Path, dest: Path) -> None:
    if src.resolve() == dest.resolve():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".imgtui_", suffix=dest.suffix, dir=str(dest.parent))
    os.close(fd)
    try:
        shutil.copy2(src, tmp_name)
        os.replace(tmp_name, dest)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _has_transparency(im: Image.Image) -> bool:
    return im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)


def _normalize_16bit(im: Image.Image) -> Image.Image:
    if im.mode in ("I;16", "I;16B", "I;16L", "I;16N"):
        info = dict(im.info)
        out = im.point(lambda v: v * (255.0 / 65535.0)).convert("L")
        out.info.update(info)
        return out
    if im.mode in ("I", "F"):
        info = dict(im.info)
        extrema = im.getextrema()
        lo, hi = extrema if isinstance(extrema, tuple) else (0, 255)
        if hi > 255 or lo < 0:
            span = max(1e-6, hi - lo)
            scale = 255.0 / span
            out = im.point(lambda v, lo=lo, scale=scale: (v - lo) * scale).convert("L")
        else:
            out = im.convert("L")
        out.info.update(info)
        return out
    return im


def _decode_heic_to_pillow(src: Path) -> Image.Image:
    with tempfile.TemporaryDirectory(prefix="imgtui_heic_") as tmpdir:
        tmp_png = Path(tmpdir) / "decoded.png"
        if sys.platform == "darwin" and shutil.which("sips"):
            proc = subprocess.run(
                ["sips", "-s", "format", "png", str(src), "--out", str(tmp_png)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if proc.returncode == 0 and tmp_png.exists():
                with Image.open(tmp_png) as im:
                    im.load()
                    return im.copy()
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            proc = subprocess.run(
                [ffmpeg, "-y", "-i", str(src), "-frames:v", "1", "-update", "1", str(tmp_png)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if proc.returncode == 0 and tmp_png.exists():
                with Image.open(tmp_png) as im:
                    im.load()
                    return im.copy()
    raise RuntimeError("Cannot decode HEIC: sips or ffmpeg is required.")


def _encode_heic_via_sips(im: Image.Image, quality: int) -> bytes:
    if sys.platform != "darwin" or not shutil.which("sips"):
        raise RuntimeError("HEIC encoding requires macOS sips.")
    with tempfile.TemporaryDirectory(prefix="imgtui_heic_out_") as tmpdir:
        tmp_in = Path(tmpdir) / "in.png"
        tmp_out = Path(tmpdir) / "out.heic"
        im.save(tmp_in, format="PNG")
        proc = subprocess.run(
            [
                "sips",
                "-s",
                "format",
                "heic",
                "-s",
                "formatOptions",
                str(max(10, min(100, quality))),
                str(tmp_in),
                "--out",
                str(tmp_out),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0 or not tmp_out.exists():
            raise RuntimeError(proc.stderr.strip() or "sips failed to encode HEIC")
        return tmp_out.read_bytes()


def _flatten_alpha_for_jpeg(im: Image.Image) -> Image.Image:
    im = _normalize_16bit(im)
    if _has_transparency(im):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[3])
        return bg
    if im.mode != "RGB":
        return im.convert("RGB")
    return im


def _find_binary(name: str, extra_paths: tuple[str, ...] = ()) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for candidate in extra_paths:
        hit = shutil.which(candidate)
        if hit:
            return hit
    return None


def _is_mozjpeg_binary(binary_path: str) -> bool:
    try:
        proc = subprocess.run(
            [binary_path, "-version"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return "mozjpeg" in (proc.stdout + proc.stderr).lower()
    except Exception:
        return False


def _find_mozjpeg() -> str | None:
    for candidate in (
        shutil.which("mozjpeg-cjpeg"),
        shutil.which("cjpeg"),
        shutil.which("/opt/homebrew/opt/mozjpeg/bin/cjpeg"),
        shutil.which("/usr/local/opt/mozjpeg/bin/cjpeg"),
    ):
        if candidate and _is_mozjpeg_binary(candidate):
            return candidate
    return None


def _find_pngquant() -> str | None:
    return _find_binary("pngquant", ("/opt/homebrew/bin/pngquant", "/usr/local/bin/pngquant"))


def _find_oxipng() -> str | None:
    return _find_binary("oxipng", ("/opt/homebrew/bin/oxipng", "/usr/local/bin/oxipng"))


def _find_gifsicle() -> str | None:
    return _find_binary("gifsicle", ("/opt/homebrew/bin/gifsicle", "/usr/local/bin/gifsicle"))


def _find_cwebp() -> str | None:
    return _find_binary("cwebp", ("/opt/homebrew/bin/cwebp", "/usr/local/bin/cwebp"))


def _find_gif2webp() -> str | None:
    return _find_binary("gif2webp", ("/opt/homebrew/bin/gif2webp", "/usr/local/bin/gif2webp"))


def _inject_jpeg_exif(jpeg_bytes: bytes, exif_bytes: bytes) -> bytes:
    if len(jpeg_bytes) < 4 or jpeg_bytes[:2] != b"\xff\xd8" or not exif_bytes:
        return jpeg_bytes
    payload = exif_bytes if exif_bytes.startswith(b"Exif\x00\x00") else (b"Exif\x00\x00" + exif_bytes)
    if len(payload) > 65533:
        # Try re-serializing via Pillow Image.Exif to drop oversized IFD1 thumbnails/padding
        try:
            exif_obj = Image.Exif()
            exif_obj.load(payload)
            shrunk = exif_obj.tobytes()
            payload = shrunk if shrunk.startswith(b"Exif\x00\x00") else (b"Exif\x00\x00" + shrunk)
        except Exception:
            return jpeg_bytes
        if len(payload) > 65533:
            return jpeg_bytes
    pos = 2
    # Skip APP0 (JFIF) if immediately after SOI so JFIF stays first
    if jpeg_bytes[pos : pos + 2] == b"\xff\xe0" and len(jpeg_bytes) >= pos + 4:
        seg_len = struct.unpack(">H", jpeg_bytes[pos + 2 : pos + 4])[0]
        if pos + 2 + seg_len <= len(jpeg_bytes):
            pos += 2 + seg_len
    app1 = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    return jpeg_bytes[:pos] + app1 + jpeg_bytes[pos:]


def _make_png_chunk(chunk_type: bytes, chunk_data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
    return struct.pack(">I", len(chunk_data)) + chunk_type + chunk_data + struct.pack(">I", crc)


def _inject_png_metadata(
    png_bytes: bytes,
    icc_bytes: bytes | None = None,
    exif_bytes: bytes | None = None,
) -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"
    if (not icc_bytes and not exif_bytes) or not png_bytes.startswith(sig) or len(png_bytes) < 33:
        return png_bytes
    pos = 8
    chunks: list[tuple[bytes, bytes]] = []
    has_iccp = False
    has_exif = False
    while pos + 8 <= len(png_bytes):
        chunk_len = struct.unpack(">I", png_bytes[pos : pos + 4])[0]
        chunk_type = png_bytes[pos + 4 : pos + 8]
        chunk_end = pos + 12 + chunk_len
        if chunk_end > len(png_bytes):
            return png_bytes
        raw_chunk = png_bytes[pos:chunk_end]
        if chunk_type == b"iCCP":
            has_iccp = True
        elif chunk_type == b"eXIf":
            has_exif = True
        chunks.append((chunk_type, raw_chunk))
        pos = chunk_end
        if chunk_type == b"IEND":
            break
    if not chunks or chunks[0][0] != b"IHDR":
        return png_bytes

    insert_chunks: list[bytes] = []
    strip_fallback_color = False
    if icc_bytes and not has_iccp:
        iccp_data = b"icc\x00\x00" + zlib.compress(icc_bytes, level=9)
        insert_chunks.append(_make_png_chunk(b"iCCP", iccp_data))
        # PNG spec forbids sRGB alongside iCCP; remove pngquant's fallback sRGB/gAMA/cHRM chunks
        strip_fallback_color = True

    if exif_bytes and not has_exif:
        raw_exif = exif_bytes[6:] if exif_bytes.startswith(b"Exif\x00\x00") else exif_bytes
        if raw_exif:
            insert_chunks.append(_make_png_chunk(b"eXIf", raw_exif))

    if not insert_chunks:
        return png_bytes

    out_parts = [sig, chunks[0][1], *insert_chunks]
    for ctype, raw_chunk in chunks[1:]:
        if strip_fallback_color and ctype in (b"sRGB", b"gAMA", b"cHRM"):
            continue
        out_parts.append(raw_chunk)
    return b"".join(out_parts)


def _encode_webp(im: Image.Image, config: CompressionConfig) -> bytes:
    # Try Pillow libwebp (method=6) and cwebp (-mt -m 6 -sharp_yuv) when lossy; pick smaller valid output.
    buf = io.BytesIO()
    work = _normalize_16bit(im)
    if work.mode not in ("RGB", "RGBA"):
        work = work.convert("RGBA" if _has_transparency(work) else "RGB")
    save_kwargs: dict[str, object] = {
        "format": "WEBP",
        "method": 6,
    }
    if config.lossless_webp:
        save_kwargs["lossless"] = True
        save_kwargs["quality"] = 100
    else:
        save_kwargs["quality"] = max(1, min(100, config.quality))
    if not config.strip_metadata:
        for key in ("exif", "icc_profile"):
            if key in im.info:
                save_kwargs[key] = im.info[key]
    work.save(buf, **save_kwargs)
    best_bytes = buf.getvalue()

    cwebp = _find_cwebp()
    if cwebp and not config.lossless_webp:
        try:
            with tempfile.TemporaryDirectory(prefix="imgtui_webp_") as tmpdir:
                tmp_png = Path(tmpdir) / "src.png"
                tmp_webp = Path(tmpdir) / "out.webp"
                png_kwargs: dict[str, object] = {"format": "PNG"}
                if not config.strip_metadata:
                    for key in ("exif", "icc_profile"):
                        if key in im.info:
                            png_kwargs[key] = im.info[key]
                work.save(tmp_png, **png_kwargs)
                cmd = [
                    cwebp,
                    "-quiet",
                    "-mt",
                    "-m",
                    "6",
                    "-sharp_yuv",
                    "-q",
                    str(max(1, min(100, config.quality))),
                ]
                if not config.strip_metadata:
                    cmd.extend(["-metadata", "icc,exif"])
                cmd.extend([str(tmp_png), "-o", str(tmp_webp)])
                proc = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
                if proc.returncode == 0 and tmp_webp.exists():
                    cwebp_bytes = tmp_webp.read_bytes()
                    if 0 < len(cwebp_bytes) < len(best_bytes):
                        best_bytes = cwebp_bytes
        except Exception:
            pass
    return best_bytes


def _pngquant_quality_range(quality: int) -> tuple[int, int]:
    q = max(10, min(100, quality))
    q_min = max(20, min(85, q - 18))
    q_max = max(q_min + 10, min(98, q + 12))
    return q_min, q_max


def _run_oxipng(
    oxipng_bin: str,
    png_path: Path,
    strip_metadata: bool,
    optimize_alpha: bool,
) -> bool:
    cmd = [oxipng_bin, "-o", "4", "--quiet"]
    if optimize_alpha:
        cmd.append("-a")
    if strip_metadata:
        cmd.extend(["--strip", "all"])
    else:
        cmd.extend(["--keep", "eXIf,display"])
    cmd.append(str(png_path))
    proc = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
    return proc.returncode == 0 and png_path.exists() and png_path.stat().st_size > 0


def _encode_png(im: Image.Image, config: CompressionConfig) -> bytes:
    buf_opt = io.BytesIO()
    work = _normalize_16bit(im)
    if work.mode in ("1", "L", "P", "RGB", "RGBA", "LA"):
        pass
    else:
        work = work.convert("RGBA" if _has_transparency(work) else "RGB")

    save_kwargs: dict[str, object] = {
        "format": "PNG",
        "optimize": True,
        "compress_level": 9,
    }
    if not config.strip_metadata:
        for key in ("icc_profile", "exif"):
            if key in im.info:
                save_kwargs[key] = im.info[key]

    work.save(buf_opt, **save_kwargs)
    base_bytes = buf_opt.getvalue()
    best_bytes = base_bytes

    oxipng_bin = _find_oxipng()
    pngquant_bin = _find_pngquant()
    is_lossless = config.png_quantize_colors <= 0

    with tempfile.TemporaryDirectory(prefix="imgtui_png_") as tmpdir:
        tmp_base = Path(tmpdir) / "base.png"
        tmp_base.write_bytes(base_bytes)

        if oxipng_bin:
            try:
                tmp_lossless = Path(tmpdir) / "lossless.png"
                tmp_lossless.write_bytes(base_bytes)
                if _run_oxipng(
                    oxipng_bin,
                    tmp_lossless,
                    strip_metadata=config.strip_metadata,
                    optimize_alpha=not is_lossless,
                ):
                    ox_bytes = tmp_lossless.read_bytes()
                    if 0 < len(ox_bytes) < len(best_bytes):
                        best_bytes = ox_bytes
            except Exception:
                pass

        if not is_lossless and work.mode != "1":
            quantized_done = False
            if pngquant_bin:
                try:
                    tmp_pq = Path(tmpdir) / "pq.png"
                    q_min, q_max = _pngquant_quality_range(config.quality)
                    colors = max(2, min(256, config.png_quantize_colors))
                    cmd = [
                        pngquant_bin,
                        "--force",
                        "--speed",
                        "1",
                        "--quality",
                        f"{q_min}-{q_max}",
                        str(colors),
                        "--output",
                        str(tmp_pq),
                        "--",
                        str(tmp_base),
                    ]
                    proc = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
                    if proc.returncode == 0 and tmp_pq.exists() and tmp_pq.stat().st_size > 0:
                        pq_bytes = tmp_pq.read_bytes()
                        if not config.strip_metadata and ("icc_profile" in im.info or "exif" in im.info):
                            pq_bytes = _inject_png_metadata(
                                pq_bytes,
                                icc_bytes=im.info.get("icc_profile"),
                                exif_bytes=im.info.get("exif"),
                            )
                            tmp_pq.write_bytes(pq_bytes)
                        if oxipng_bin:
                            if _run_oxipng(
                                oxipng_bin,
                                tmp_pq,
                                strip_metadata=config.strip_metadata,
                                optimize_alpha=True,
                            ):
                                pq_bytes = tmp_pq.read_bytes()
                        if 0 < len(pq_bytes) < len(best_bytes):
                            best_bytes = pq_bytes
                        quantized_done = True
                    elif proc.returncode in (98, 99):
                        # pngquant intentionally refused to go below quality floor or enlarge output
                        quantized_done = True
                except Exception:
                    pass

            if not quantized_done and work.mode in ("RGB", "RGBA"):
                try:
                    quant_method = (
                        Image.Quantize.FASTOCTREE
                        if work.mode == "RGBA"
                        else Image.Quantize.MEDIANCUT
                    )
                    quantized = work.quantize(
                        colors=max(2, min(256, config.png_quantize_colors)),
                        method=quant_method,
                    )
                    buf_q = io.BytesIO()
                    q_kwargs: dict[str, object] = {
                        "format": "PNG",
                        "optimize": True,
                        "compress_level": 9,
                    }
                    if not config.strip_metadata:
                        for key in ("icc_profile", "exif"):
                            if key in im.info:
                                q_kwargs[key] = im.info[key]
                    quantized.save(buf_q, **q_kwargs)
                    q_bytes = buf_q.getvalue()
                    if oxipng_bin:
                        tmp_pil_q = Path(tmpdir) / "pil_q.png"
                        tmp_pil_q.write_bytes(q_bytes)
                        if _run_oxipng(
                            oxipng_bin,
                            tmp_pil_q,
                            strip_metadata=config.strip_metadata,
                            optimize_alpha=True,
                        ):
                            q_bytes = tmp_pil_q.read_bytes()
                    if 0 < len(q_bytes) < len(best_bytes):
                        best_bytes = q_bytes
                except Exception:
                    pass

    return best_bytes


def _encode_jpeg(im: Image.Image, config: CompressionConfig) -> bytes:
    quality = max(1, min(95, config.quality))
    if im.mode == "L" and not _has_transparency(im):
        work = im
    else:
        work = _flatten_alpha_for_jpeg(im)

    cjpeg_bin = _find_mozjpeg()
    if cjpeg_bin:
        try:
            with tempfile.TemporaryDirectory(prefix="imgtui_mozjpeg_") as tmpdir:
                tmp_ppm = Path(tmpdir) / "in.ppm"
                tmp_jpg = Path(tmpdir) / "out.jpg"
                work.save(tmp_ppm, format="PPM")
                cmd = [
                    cjpeg_bin,
                    "-quality",
                    str(quality),
                    "-progressive",
                    "-optimize",
                    "-quant-table",
                    "3",
                ]
                if work.mode == "L":
                    cmd.append("-grayscale")
                if not config.strip_metadata and "icc_profile" in im.info:
                    tmp_icc = Path(tmpdir) / "profile.icc"
                    tmp_icc.write_bytes(im.info["icc_profile"])
                    cmd.extend(["-icc", str(tmp_icc)])
                cmd.extend(["-outfile", str(tmp_jpg), str(tmp_ppm)])
                proc = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
                if proc.returncode == 0 and tmp_jpg.exists():
                    moz_bytes = tmp_jpg.read_bytes()
                    if moz_bytes:
                        if not config.strip_metadata and "exif" in im.info:
                            moz_bytes = _inject_jpeg_exif(moz_bytes, im.info["exif"])
                        return moz_bytes
        except Exception:
            pass

    buf = io.BytesIO()
    save_kwargs: dict[str, object] = {
        "format": "JPEG",
        "quality": quality,
        "optimize": True,
        "progressive": True,
    }
    if not config.strip_metadata:
        for key in ("exif", "icc_profile"):
            if key in im.info:
                save_kwargs[key] = im.info[key]
    work.save(buf, **save_kwargs)
    return buf.getvalue()


def _gifsicle_lossy_level(quality: int) -> int:
    q = max(10, min(95, quality))
    return max(20, min(160, round((95 - q) * 2.5)))


def _quantize_rgba_for_gif(rgba: Image.Image) -> tuple[Image.Image, bool]:
    alpha = rgba.getchannel("A")
    lo, _ = alpha.getextrema()
    if lo < 128:
        q = rgba.convert("RGB").quantize(colors=255, method=Image.Quantize.MEDIANCUT)
        pal = list(q.getpalette() or [])[: 255 * 3]
        if len(pal) < 255 * 3:
            pal.extend([0] * (255 * 3 - len(pal)))
        pal.extend([0, 0, 0])
        q.putpalette(pal)
        mask = alpha.point(lambda a: 255 if a < 128 else 0)
        q.paste(255, mask=mask)
        q.info["transparency"] = 255
        return q, True
    q = rgba.convert("RGB").quantize(colors=256, method=Image.Quantize.MEDIANCUT)
    return q, False


def _compress_gif(
    src: Path,
    config: CompressionConfig,
) -> tuple[bytes, tuple[int, int], tuple[int, int], bool]:
    with Image.open(src) as opened:
        orig_w, orig_h = opened.size
        resized = config.max_dimension > 0 and max(orig_w, orig_h) > config.max_dimension
        is_lossless = config.quality >= 95 or (
            config.preset_key == "lossless" and config.lossless_webp and config.quality >= 94
        )

    gifsicle_bin = _find_gifsicle()
    if gifsicle_bin:
        try:
            with tempfile.TemporaryDirectory(prefix="imgtui_gif_") as tmpdir:
                tmp_out = Path(tmpdir) / "out.gif"
                cmd = [gifsicle_bin, "-O3"]
                if not is_lossless:
                    cmd.append(f"--lossy={_gifsicle_lossy_level(config.quality)}")
                if config.strip_metadata:
                    cmd.extend(["--no-comments", "--no-names"])
                if resized:
                    cmd.extend(["--resize-fit", f"{config.max_dimension}x{config.max_dimension}"])
                cmd.extend([str(src), "-o", str(tmp_out)])
                proc = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
                if proc.returncode == 0 and tmp_out.exists() and tmp_out.stat().st_size > 0:
                    payload = tmp_out.read_bytes()
                    with Image.open(io.BytesIO(payload)) as out_im:
                        new_size = out_im.size
                    return payload, (orig_w, orig_h), new_size, resized
        except Exception:
            pass

    # Pillow fallback preserving animated frames, transparency, disposal, delays, and loop count
    with Image.open(src) as opened:
        loop = opened.info.get("loop", 0)
        frames: list[Image.Image] = []
        durations: list[int] = []
        disposals: list[int] = []
        for frame in ImageSequence.Iterator(opened):
            durations.append(int(frame.info.get("duration", opened.info.get("duration", 100))))
            orig_disposal = int(frame.info.get("disposal", 2))
            if resized:
                rgba = frame.convert("RGBA")
                rgba.thumbnail((config.max_dimension, config.max_dimension), Image.Resampling.LANCZOS)
                q_frame, has_alpha = _quantize_rgba_for_gif(rgba)
                frames.append(q_frame)
                disposals.append(2 if has_alpha else orig_disposal)
            elif frame.mode in ("RGBA", "LA", "PA"):
                q_frame, has_alpha = _quantize_rgba_for_gif(frame.convert("RGBA"))
                frames.append(q_frame)
                disposals.append(2 if has_alpha else orig_disposal)
            else:
                work_frame = frame.copy()
                if "transparency" in frame.info:
                    work_frame.info["transparency"] = frame.info["transparency"]
                    disposals.append(2 if orig_disposal == 0 else orig_disposal)
                else:
                    disposals.append(orig_disposal)
                frames.append(work_frame)
        if not frames:
            raise RuntimeError("GIF contains no readable frames")
        buf = io.BytesIO()
        save_kwargs: dict[str, object] = {
            "format": "GIF",
            "optimize": True,
        }
        if len(frames) > 1:
            save_kwargs["save_all"] = True
            save_kwargs["append_images"] = frames[1:]
            save_kwargs["duration"] = durations
            save_kwargs["loop"] = loop
            save_kwargs["disposal"] = disposals
        frames[0].save(buf, **save_kwargs)
        return buf.getvalue(), (orig_w, orig_h), frames[0].size, resized


def _encode_animated_gif_to_webp(
    src: Path,
    config: CompressionConfig,
) -> tuple[bytes, tuple[int, int], tuple[int, int], bool]:
    with Image.open(src) as opened:
        orig_w, orig_h = opened.size
        resized = config.max_dimension > 0 and max(orig_w, orig_h) > config.max_dimension
        loop = opened.info.get("loop", 0)
        frames: list[Image.Image] = []
        durations: list[int] = []
        for frame in ImageSequence.Iterator(opened):
            durations.append(int(frame.info.get("duration", opened.info.get("duration", 100))))
            rgba = frame.convert("RGBA")
            if resized:
                rgba.thumbnail((config.max_dimension, config.max_dimension), Image.Resampling.LANCZOS)
            frames.append(rgba)
    if not frames:
        raise RuntimeError("GIF contains no readable frames")

    buf = io.BytesIO()
    save_kwargs: dict[str, object] = {
        "format": "WEBP",
        "save_all": True,
        "append_images": frames[1:],
        "duration": durations,
        "loop": loop,
        "method": 6,
    }
    if config.lossless_webp:
        save_kwargs["lossless"] = True
        save_kwargs["quality"] = 100
    else:
        save_kwargs["quality"] = max(1, min(100, config.quality))
    frames[0].save(buf, **save_kwargs)
    best_bytes = buf.getvalue()

    gif2webp_bin = _find_gif2webp()
    if gif2webp_bin and not resized:
        try:
            with tempfile.TemporaryDirectory(prefix="imgtui_gif2webp_") as tmpdir:
                tmp_webp = Path(tmpdir) / "out.webp"
                cmd = [gif2webp_bin, "-quiet", "-mt", "-m", "6"]
                if not config.lossless_webp:
                    cmd.extend(["-lossy", "-q", str(max(1, min(100, config.quality)))])
                cmd.extend([str(src), "-o", str(tmp_webp)])
                proc = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
                if proc.returncode == 0 and tmp_webp.exists():
                    cand = tmp_webp.read_bytes()
                    if 0 < len(cand) < len(best_bytes):
                        best_bytes = cand
        except Exception:
            pass
    return best_bytes, (orig_w, orig_h), frames[0].size, resized


def compress_image(
    entry: ImageEntry,
    config: CompressionConfig,
    dest_path: Path | None = None,
) -> ImageEntry:
    src = entry.path
    out_path = dest_path or resolve_output_path(entry, config)
    out_ext = out_path.suffix.lower()
    src_ext = entry.ext.lower()

    if src_ext == ".gif" and out_ext == ".gif":
        payload, (orig_w, orig_h), new_size, resized = _compress_gif(src, config)
        entry.width = orig_w
        entry.height = orig_h
        if config.keep_smaller and not resized and len(payload) >= entry.size_bytes:
            _atomic_copy_file(src, out_path)
            entry.output_path = out_path
            entry.output_bytes = entry.size_bytes
            entry.kept_original = True
            entry.status = "done"
            entry.error = ""
            entry.note = "already optimal (kept original)"
            return entry
        _atomic_write_bytes(out_path, payload)
        entry.output_path = out_path
        entry.output_bytes = len(payload)
        entry.kept_original = False
        entry.status = "done"
        entry.error = ""
        entry.note = f"{orig_w}x{orig_h} -> {new_size[0]}x{new_size[1]}" if resized else "GIF -> GIF"
        return entry

    if src_ext == ".gif" and out_ext == ".webp":
        with Image.open(src) as probe:
            n_frames = getattr(probe, "n_frames", 1)
        if n_frames > 1:
            payload, (orig_w, orig_h), new_size, resized = _encode_animated_gif_to_webp(src, config)
            entry.width = orig_w
            entry.height = orig_h
            _atomic_write_bytes(out_path, payload)
            entry.output_path = out_path
            entry.output_bytes = len(payload)
            entry.kept_original = False
            entry.status = "done"
            entry.error = ""
            entry.note = f"{orig_w}x{orig_h} -> {new_size[0]}x{new_size[1]}" if resized else "GIF -> WEBP"
            return entry

    if src_ext in HEIC_EXTENSIONS:
        im = _decode_heic_to_pillow(src)
    else:
        with Image.open(src) as opened:
            opened.load()
            im = ImageOps.exif_transpose(opened)
            # Copy info before file closes
            info = dict(opened.info)
            if "exif" in info and not config.strip_metadata:
                # Orientation has been applied via exif_transpose; update tag 274=1 if kept
                try:
                    exif = opened.getexif()
                    if 274 in exif:
                        exif[274] = 1
                        info["exif"] = exif.tobytes()
                except Exception:
                    pass
            im.info.update(info)

    orig_w, orig_h = im.size
    entry.width = orig_w
    entry.height = orig_h
    resized = False
    if config.max_dimension > 0 and max(orig_w, orig_h) > config.max_dimension:
        info = dict(im.info)
        im = _normalize_16bit(im)
        if im.mode == "P":
            im = im.convert("RGBA" if _has_transparency(im) else "RGB")
        else:
            im = im.copy()
        im.info.update(info)
        im.thumbnail((config.max_dimension, config.max_dimension), Image.Resampling.LANCZOS)
        resized = True

    if out_ext in (".jpg", ".jpeg"):
        payload = _encode_jpeg(im, config)
    elif out_ext == ".png":
        payload = _encode_png(im, config)
    elif out_ext == ".webp":
        payload = _encode_webp(im, config)
    elif out_ext in HEIC_EXTENSIONS:
        payload = _encode_heic_via_sips(im, config.quality)
    else:
        payload = _encode_png(im, config)

    same_format = (out_ext == src_ext) or (
        {out_ext, src_ext} <= {".jpg", ".jpeg"}
    )
    if config.keep_smaller and same_format and not resized and len(payload) >= entry.size_bytes:
        _atomic_copy_file(src, out_path)
        entry.output_path = out_path
        entry.output_bytes = entry.size_bytes
        entry.kept_original = True
        entry.status = "done"
        entry.error = ""
        entry.note = "already optimal (kept original)"
        return entry

    _atomic_write_bytes(out_path, payload)
    entry.output_path = out_path
    entry.output_bytes = len(payload)
    entry.kept_original = False
    entry.status = "done"
    entry.error = ""
    if resized:
        entry.note = f"{orig_w}x{orig_h} -> {im.size[0]}x{im.size[1]}"
    else:
        entry.note = f"{src_ext.lstrip('.').upper()} -> {out_ext.lstrip('.').upper()}"
    return entry


def compress_batch(
    entries: list[ImageEntry],
    config: CompressionConfig,
    on_progress: Callable[[int, int, ImageEntry], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> BatchSummary:
    import time

    started = time.monotonic()
    selected_entries = [e for e in entries if e.selected]
    summary = BatchSummary(total_files=len(selected_entries))
    used_paths: set[Path] = set()
    if config.output_mode != "overwrite":
        for item in entries:
            try:
                used_paths.add(item.path.resolve())
            except OSError:
                used_paths.add(item.path)

    for idx, entry in enumerate(selected_entries, start=1):
        if cancel_check and cancel_check():
            summary.cancelled = True
            break
        entry.status = "running"
        entry.error = ""
        dest_path = resolve_output_path(entry, config, used_paths=used_paths)
        if on_progress:
            on_progress(idx - 1, len(selected_entries), entry)
        try:
            compress_image(entry, config, dest_path=dest_path)
            summary.succeeded += 1
            if entry.kept_original:
                summary.kept_original += 1
            summary.total_before_bytes += entry.size_bytes
            summary.total_after_bytes += entry.output_bytes
        except Exception as exc:
            entry.status = "failed"
            entry.error = str(exc)
            entry.output_path = None
            entry.output_bytes = 0
            summary.failed += 1
            summary.errors.append((entry.path, str(exc)))
        if on_progress:
            on_progress(idx, len(selected_entries), entry)

    summary.elapsed_seconds = time.monotonic() - started
    return summary


def _short_version(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=2, check=False)
        out = (proc.stdout or proc.stderr or "").strip().splitlines()
        return out[0] if out else "installed"
    except Exception:
        return "installed"


def health_checks(nerd: bool = True) -> list[tuple[str, str, str]]:
    checks: list[tuple[str, str, str]] = []
    try:
        import PIL

        checks.append(("Pillow", "ok", f"v{PIL.__version__}"))
    except Exception:
        checks.append(("Pillow", "warn", "missing"))

    mozjpeg = _find_mozjpeg()
    if mozjpeg:
        ver = _short_version([mozjpeg, "-version"])
        tag = ver.split()[2] if "version" in ver and len(ver.split()) >= 3 else "installed"
        checks.append(("MozJPEG", "ok", f"v{tag} (trellis)" if tag[0].isdigit() else ver))
    else:
        checks.append(("MozJPEG", "warn", "Pillow JPEG fallback"))

    pq = _find_pngquant()
    ox = _find_oxipng()
    if pq and ox:
        checks.append(("PNG", "ok", "pngquant + oxipng"))
    elif pq:
        checks.append(("PNG", "ok", "pngquant (no oxipng)"))
    elif ox:
        checks.append(("PNG", "ok", "oxipng (no pngquant)"))
    else:
        checks.append(("PNG", "warn", "Pillow PNG fallback"))

    cwebp = _find_cwebp()
    if cwebp:
        out = _short_version([cwebp, "-version"])
        checks.append(("cwebp", "ok", f"v{out} (sharpyuv)" if out != "installed" else "installed"))
    else:
        checks.append(("cwebp", "warn", "Pillow WebP fallback"))

    gifsicle = _find_gifsicle()
    if gifsicle:
        ver = _short_version([gifsicle, "--version"])
        tag = ver.split()[-1] if "Gifsicle" in ver else "installed"
        checks.append(("gifsicle", "ok", f"v{tag} (-O3 lossy)" if tag[0].isdigit() else "installed"))
    else:
        checks.append(("gifsicle", "warn", "Pillow GIF fallback"))

    if sys.platform == "darwin" and shutil.which("sips"):
        checks.append(("HEIC", "ok", "macOS sips native"))
    elif shutil.which("ffmpeg"):
        checks.append(("HEIC", "ok", "ffmpeg decode"))
    else:
        checks.append(("HEIC", "warn", "no sips/ffmpeg"))

    term = os.environ.get("TERM_PROGRAM") or os.environ.get("TERM") or "terminal"
    color = os.environ.get("COLORTERM") or "256/16"
    glyph_mode = "unicode" if nerd else "ascii"
    checks.append(("terminal", "ok", f"{term} ({color}, {glyph_mode})"))
    return checks
