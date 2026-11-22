<div align="center">

```text
██╗███╗   ███╗ ██████╗       ████████╗██╗   ██╗██╗
██║████╗ ████║██╔════╝       ╚══██╔══╝██║   ██║██║
██║██╔████╔██║██║  ███╗█████╗   ██║   ██║   ██║██║
██║██║╚██╔╝██║██║   ██║╚════╝   ██║   ██║   ██║██║
██║██║ ╚═╝ ██║╚██████╔╝         ██║   ╚██████╔╝██║
╚═╝╚═╝     ╚═╝ ╚═════╝          ╚═╝    ╚═════╝ ╚═╝
```

<h3>Offline, Keyboard-First Terminal Image Compressor</h3>

<p>
  Batch-optimize JPEG, PNG, WebP, GIF, and HEIC images locally with <strong>iLoveIMG-grade</strong> codec pipelines,<br />
  packaged inside an adaptive <code>curses</code> cockpit inspired by <a href="https://github.com/sebidc/ytd-tui"><strong>ytd-tui</strong></a>.
</p>

<p>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.11+" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-10B981?style=flat-square" alt="MIT License" /></a>
  <img src="https://img.shields.io/badge/100%25%20offline-no%20uploads-0EA5E9?style=flat-square" alt="100% Offline" />
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-64748B?style=flat-square" alt="macOS and Linux" />
</p>

</div>

---

## Why `img-tui`?

Most image optimizers force you to choose between uploading private assets to web tools or memorizing long chains of `cjpeg`, `pngquant`, `oxipng`, `cwebp`, and `gifsicle` flags.

**`img-tui`** brings high-efficiency local compression into a fast, full-screen terminal interface:

- **100% Local & Private** — Images never leave your machine. Zero network calls, zero file size caps.
- **iLoveIMG-Grade Optimization Pipelines** — Automatically orchestrates native CLI encoders (`MozJPEG`, `pngquant`, `oxipng`, `cwebp` / `gif2webp`, `gifsicle`, `sips` / `ffmpeg`) with seamless Pillow fallbacks when any binary is absent.
- **Cockpit TUI & Adaptive Themes** — Mode selector, live codec health dashboard, queue table, progress bar, and per-mode color palettes directly inspired by [`sebidc/ytd-tui`](https://github.com/sebidc/ytd-tui).
- **Non-Destructive by Default** — Writes atomically to `./compressed/` with a smart keep-smaller guard so re-compressing an already optimal file never inflates its size.
- **True Multi-File Drag & Drop** — Handles terminal bracketed pastes of single or multiple dragged files, quoted or backslash-escaped spaces, Unicode/Thai filenames, and mixed extensions.
- **Instant Native Preview** — Inspect compressed vs. original images at full resolution via macOS QuickLook (`qlmanage -p` / `open`), Linux `xdg-open`, or Windows system viewer (`v` / `V`) without terminal pixelation or `curses` rendering glitches.

---

## Interface Overview

```text
+ Engine -------------+ Codecs / HEIC ------+ Active Mode --------+ Savings ------------+
| + MozJPEG: ready    | + gifsicle: ready   | Balanced (iLoveIMG) | selected: 12/12     |
| + PNG: quant+oxi    | + HEIC: sips        | fmt:keep q:80 orig  | input size: 18.4 MB |
| + cwebp: ready      | + Pillow: 11.x      | out: subfolder      | ready to compress   |
+---------------------+---------------------+---------------------+---------------------+
+ Input Path / Folder ------------------------------------------------------------------+
|  > press [b] to browse files/folders, or [/] to paste/drag a path        12/12 files  |
+---------------------------------------------------------------------------------------+
+ Images ------------------------------------+ Details ---------------------------------+
|  # SEL EXT   FILENAME    BEFORE   AFTER    | file  : hero-banner.png                  |
| >1 [x] PNG   hero-ban…  2.40 MB   pending  | path  : /Users/you/Downloads/assets      |
|  2 [x] JPG   product-…  1.85 MB   pending  | info  : 2400x1600  RGBA  2.40 MB         |
|  3 [x] HEIC  IMG_4012…  3.12 MB   pending  | status: pending                          |
|                                            | target: hero-banner.png (subfolder)      |
| Filter: all (< / >)  Sort: name (s)        | Press [v] to open full-res preview       |
+--------------------------------------------+ Compression Queue -----------------------+
|                                            | Ready: 12 image(s) queued (18.4 MB)      |
|                                            | Press [Enter] or [c] to start.           |
+ actions ------------------------------------------------------------------------------+
| [?] help [b] browse [/] path [p] mode [f] options [v] view [Space] toggle [Enter] go |
+---------------------------------------------------------------------------------------+
```

---

## Compression Engines & Format Support

`img-tui` probes your system on startup (`Engine` and `Codecs / HEIC` panels) and uses the best available pipeline per format:

| Input Format | Primary Native Pipeline | Automatic Fallback | Highlight Details |
| :--- | :--- | :--- | :--- |
| **JPEG** (`.jpg`, `.jpeg`) | `MozJPEG` (`cjpeg`) | Pillow `JPEG` | Trellis quantization, progressive Huffman scan, EXIF orientation transpose, ICC preservation |
| **PNG** (`.png`) | `pngquant` + `oxipng` (`-o 4`) | Pillow (`optimize=True`) | Perceptual palette quantization, lossless deflate/chunk optimization, PNG `iCCP`/`sRGB` spec cleanup, 16-bit normalization |
| **WebP** (`.webp` / target) | `cwebp` (`-m 6 -mt -sharp_yuv`) & `gif2webp` | Pillow `libwebp` | Evaluates both `cwebp` and Pillow when lossy and keeps the smaller valid output; supports lossless WebP & animated WebP |
| **GIF** (`.gif`) | `gifsicle` (`-O3 --lossy=...`) | Pillow multi-frame | Preserves animation frame durations, disposal modes, transparency, and loop counts |
| **HEIC / HEIF** (`.heic`, `.heif`) | macOS `sips` / `ffmpeg` | — | Reads dimensions/EXIF and converts/compresses cleanly to `.webp`, `.jpg`, or `.png` |
| **BMP / TIFF** | Pillow decode + target pipeline | Pillow encode | Seamlessly ingested for batch optimization or WebP/JPEG/PNG conversion |

> **Keep-Smaller Guard:** When keeping the original format without resizing, if an already-optimized file turns out larger after re-encoding, `img-tui` safely copies the original bytes instead of bloating your file.

---

## Installation

### Prerequisites

- **Python 3.11+** (only Python runtime dependency is `pillow>=11.0.0`, installed automatically)
- Recommended native compression binaries (optional — `img-tui` falls back cleanly to Pillow if omitted):

**macOS (Homebrew):**

```sh
brew install mozjpeg pngquant oxipng webp gifsicle ffmpeg
```

**Ubuntu / Debian:**

```sh
sudo apt update && sudo apt install -y pngquant webp gifsicle ffmpeg
# Optional: oxipng / mozjpeg for maximum compression efficiency
```

### Install `img-tui`

**Using `uv` (recommended):**

```sh
uv tool install git+https://github.com/bpppoh/img-tui.git
```

**Using `pipx`:**

```sh
pipx install git+https://github.com/bpppoh/img-tui.git
```

**From source (development / editable):**

```sh
git clone https://github.com/bpppoh/img-tui.git
cd img-tui
uv tool install --editable .
```

---

## Usage

Launch the interactive mode selector and cockpit:

```sh
img-tui
```

Preload files or folders directly from your shell:

```sh
img-tui ~/Downloads/photos
img-tui hero.png banner.jpg ./assets
```

Additional CLI flags:

```sh
img-tui --ascii      # Plain ASCII symbols for minimal fonts / terminals
img-tui --version    # Print installed version
```

> **Tip (Drag & Drop):** Drop one or multiple files/folders directly from Finder or your file manager into the terminal while in the cockpit (or after pressing `/`). Both quoted paths, backslash-escaped paths, and spaces/Unicode filenames work out of the box.

---

## Presets & Configuration

Press `p` (or keys `1`–`5`) at any time to switch modes, or press `f` / `o` to fine-tune any setting interactively.

### Built-in Presets

| Key | Preset Mode | JPG / WebP Quality | PNG Quantize | Max Edge | Summary |
| :---: | :--- | :---: | :---: | :---: | :--- |
| `1` | **Balanced (iLoveIMG)** | `Q80` | 256 colors | Original | Default web-ready balance across MozJPEG, pngquant+oxipng, cwebp, and gifsicle |
| `2` | **Extreme Compression** | `Q60` / `Q62` | 256 colors | Original | Aggressive size reduction for tight budgets and fast page loads |
| `3` | **Lossless / Safe** | `Q94` / Lossless WebP | Off (Lossless) | Original | Lossless `oxipng`, lossless WebP/GIF optimization, high-fidelity JPEG |
| `4` | **WebP Convert** | `Q82` | Off | Original | Batch-converts JPG/PNG/GIF/HEIC to modern `.webp` |
| `5` | **Resize + Compress** | `Q80` | 256 colors | `1920px` | Downscales oversized photos (never upscales) + smart compression |

### Output Modes (`f` / `o` Options)

| Output Mode | Destination Behavior | Original Files |
| :--- | :--- | :---: |
| **`subfolder`** *(default)* | Writes to `./compressed/` beside each source image | Untouched |
| **`suffix`** | Writes alongside source as `<name>_compressed.<ext>` | Untouched |
| **`custom`** | Writes all outputs to a chosen directory (default `~/Downloads/img-tui-compressed`) | Untouched |
| **`overwrite`** | Replaces source files in place (shows explicit confirmation warning) | Replaced |

Other toggles in Options (`f` / `o`): **Target Format** (`keep`, `webp`, `jpg`, `png`), **Quality** (`10`–`100`), **Lossless WebP**, **PNG Quantize**, **Max Dimension** (`Original`, `2560px`, `1920px`, `1280px`, `800px`), **Strip Metadata**, **Keep Smaller**, and **Recursive Scan**.

---

## Keyboard Shortcuts

| Key | Action |
| :--- | :--- |
| `?` | Open keyboard shortcuts help |
| `b` | Open interactive file & folder browser (`[1..5]` quick-jump bookmarks, `.` hidden files, `r` recursive) |
| `/` | Focus input bar to type, paste, or drag-and-drop file/folder paths |
| `p` / `1`–`5` | Open mode selector / switch preset immediately |
| `f` / `o` | Open compression options popup |
| `j` / `k` / `↑` `↓` | Move cursor through image list (`PgUp`/`PgDn` & mouse wheel supported) |
| `Space` | Toggle include/exclude on the highlighted image |
| `a` / `n` | Select all / Deselect all images in queue |
| `r` | Toggle recursive folder scanning |
| `<` / `>` (or `←` `→`) | Cycle filter (`all`, `selected`, `pending`, `done`, `failed`) |
| `s` | Cycle sort (`name`, `size`, `ext`, `saved`) |
| `v` | Open highlighted image in native full-res preview (`QuickLook` / `open` / `xdg-open`; opens compressed output when done) |
| `V` | Open original source image in native full-res preview |
| `d` / `Del` | Remove highlighted image from queue |
| `X` | Clear entire queue |
| `Enter` / `c` | Confirm & start batch compression (`q` cancels gracefully mid-batch) |
| `x` | View detailed per-file before/after compression summary |
| `O` | Open output folder in Finder / system file manager |
| `Tab` | Cycle panel focus highlight |
| `q` / `Ctrl+C` | Return to mode selector / quit |

---

## Architecture & Development

```text
img-tui/
├── pyproject.toml
├── src/img_tui/
│   ├── __init__.py
│   ├── __main__.py      # CLI entry point (--ascii, --version, TTY guard)
│   ├── ansi_shadow.py   # Startup splash banner renderer
│   ├── app.py           # Curses cockpit UI, popups, input/paste handling, worker loop
│   ├── compressor.py    # Multi-file path parser, probing, iLoveIMG codec pipelines, batch runner
│   ├── presets.py       # Preset definitions & CompressionConfig state
│   ├── preview.py       # OS native full-resolution QuickLook / viewer launcher
│   └── themes.py        # Adaptive truecolor / 256 / 16 / mono palettes per mode
└── tests/
    ├── test_compressor.py
    └── test_tui_pty.py  # End-to-end PTY interaction tests
```

Run tests and linter locally:

```sh
uv run --with pytest pytest -v
uvx ruff check --select F,E9 src/ tests/
```

---

## Credits & Acknowledgments

- **UI Architecture & Color Theming:** Designed with huge inspiration from [**`sebidc/ytd-tui`**](https://github.com/sebidc/ytd-tui) — the 4-card top dashboard layout, dynamic mode-based color palettes (`truecolor` / `256` / `16` / `NO_COLOR`), keyboard-first cockpit flow, and modal popup ergonomics all draw directly from `ytd-tui`'s thoughtful TUI design.
- **Compression Philosophy:** Inspired by the quality-vs-size pipelines popularized by [iLoveIMG](https://www.iloveimg.com/compress-image).
- **Powered by Open-Source Codecs:** [MozJPEG](https://github.com/mozilla/mozjpeg), [pngquant](https://pngquant.org/), [oxipng](https://github.com/shssoichiro/oxipng), [libwebp / cwebp](https://developers.google.com/speed/webp), [Gifsicle](https://www.lcdf.org/gifsicle/), [Pillow](https://python-pillow.org/), and [FFmpeg](https://ffmpeg.org/).

---

## License

Released under the [MIT License](LICENSE).
