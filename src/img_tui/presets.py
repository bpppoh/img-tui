from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


TARGET_FORMATS = ("keep", "webp", "jpg", "png")
MAX_DIMENSIONS = (0, 2560, 1920, 1280, 800)  # 0 = original
OUTPUT_MODES = ("subfolder", "suffix", "custom", "overwrite")


@dataclass(frozen=True)
class CompressionPreset:
    key: str
    name: str
    jpg_quality: int
    webp_quality: int
    png_quantize_colors: int  # 0 = keep full color, >0 = palette quantize
    lossless_webp: bool
    target_format: str
    max_dimension: int
    strip_metadata: bool
    description: str


PRESETS: tuple[CompressionPreset, ...] = (
    CompressionPreset(
        key="balanced",
        name="Balanced (Recommended)",
        jpg_quality=80,
        webp_quality=80,
        png_quantize_colors=256,
        lossless_webp=False,
        target_format="keep",
        max_dimension=0,
        strip_metadata=True,
        description="iLoveIMG balance: MozJPEG Q80, pngquant+oxipng, cwebp, gifsicle.",
    ),
    CompressionPreset(
        key="extreme",
        name="Extreme Compression",
        jpg_quality=60,
        webp_quality=62,
        png_quantize_colors=256,
        lossless_webp=False,
        target_format="keep",
        max_dimension=0,
        strip_metadata=True,
        description="Aggressive size reduction (MozJPEG Q60, pngquant, lossy GIF).",
    ),
    CompressionPreset(
        key="lossless",
        name="Lossless / Safe",
        jpg_quality=94,
        webp_quality=100,
        png_quantize_colors=0,
        lossless_webp=True,
        target_format="keep",
        max_dimension=0,
        strip_metadata=True,
        description="Lossless oxipng/WebP/gifsicle optimization + high-fidelity JPG (Q94).",
    ),
    CompressionPreset(
        key="webp",
        name="Convert to WebP",
        jpg_quality=82,
        webp_quality=82,
        png_quantize_colors=0,
        lossless_webp=False,
        target_format="webp",
        max_dimension=0,
        strip_metadata=True,
        description="Convert JPG/PNG/GIF/HEIC to modern WebP (~25-45% smaller).",
    ),
    CompressionPreset(
        key="resize",
        name="Resize 1920px + Compress",
        jpg_quality=80,
        webp_quality=80,
        png_quantize_colors=256,
        lossless_webp=False,
        target_format="keep",
        max_dimension=1920,
        strip_metadata=True,
        description="Downscale images exceeding 1920px + iLoveIMG-grade compression.",
    ),
)


def preset_by_key(key: str) -> CompressionPreset:
    for preset in PRESETS:
        if preset.key == key:
            return preset
    return PRESETS[0]


@dataclass
class CompressionConfig:
    preset_key: str = "balanced"
    target_format: str = "keep"  # keep | webp | jpg | png
    quality: int = 80
    png_quantize_colors: int = 0
    lossless_webp: bool = False
    max_dimension: int = 0  # 0 = original
    strip_metadata: bool = True
    keep_smaller: bool = True
    output_mode: str = "subfolder"  # subfolder | suffix | custom | overwrite
    custom_output_dir: Path = Path.home() / "Downloads" / "img-tui-compressed"
    subfolder_name: str = "compressed"
    suffix: str = "_compressed"
    recursive: bool = False

    @classmethod
    def from_preset(cls, preset: CompressionPreset) -> "CompressionConfig":
        return cls(
            preset_key=preset.key,
            target_format=preset.target_format,
            quality=preset.webp_quality if preset.target_format == "webp" else preset.jpg_quality,
            png_quantize_colors=preset.png_quantize_colors,
            lossless_webp=preset.lossless_webp,
            max_dimension=preset.max_dimension,
            strip_metadata=preset.strip_metadata,
        )

    def apply_preset(self, preset: CompressionPreset) -> None:
        self.preset_key = preset.key
        self.target_format = preset.target_format
        self.quality = preset.webp_quality if preset.target_format == "webp" else preset.jpg_quality
        self.png_quantize_colors = preset.png_quantize_colors
        self.lossless_webp = preset.lossless_webp
        self.max_dimension = preset.max_dimension
        self.strip_metadata = preset.strip_metadata

    def summary_line(self) -> str:
        dim = f"{self.max_dimension}px" if self.max_dimension > 0 else "orig"
        exif = "strip" if self.strip_metadata else "keep"
        if self.output_mode == "subfolder":
            out = f"./{self.subfolder_name}/"
        elif self.output_mode == "suffix":
            out = f"*{self.suffix}.ext"
        elif self.output_mode == "custom":
            out = str(self.custom_output_dir)
        else:
            out = "OVERWRITE"
        return (
            f"preset={self.preset_key}  fmt={self.target_format}  q={self.quality}  "
            f"max={dim}  exif={exif}  out={out}"
        )
