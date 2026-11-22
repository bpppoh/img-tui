from __future__ import annotations

import argparse
import sys

from . import __version__
from .app import run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="img-tui",
        description="Offline keyboard-first image compression TUI inspired by ytd-tui and iLoveIMG.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Optional image files or folders to preload into the compression queue.",
    )
    parser.add_argument(
        "--ascii",
        action="store_true",
        help="Use plain ASCII glyphs instead of Unicode block/status symbols.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("img-tui requires an interactive terminal (TTY).")
    run(initial_paths=args.paths, ascii_only=args.ascii)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
