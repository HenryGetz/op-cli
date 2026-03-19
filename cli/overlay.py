"""`omni overlay` command implementation."""

from __future__ import annotations

import time
import argparse
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from config import ProjectConfig


class OverlayCommandError(Exception):
    """Invalid overlay command usage."""

    exit_code = 1


_DEFAULT_REF_COLOR = "#FF4444"
_DEFAULT_TEST_COLOR = "#4444FF"
_DEFAULT_REGION_COLOR = "#00FF00"


def add_overlay_subparser(
    *,
    subparsers: Any,
    add_trailing_global_flags: Any,
    default_box_threshold: float,
) -> Any:
    epilog = (
        "Examples:\n"
        "  omni overlay before.png after.png -o /tmp/overlay.png\n"
        "  omni overlay before.png after.png -o /tmp/overlay.png --bbox-ref --bbox-test\n"
        "  omni overlay before.png after.png -o /tmp/overlay.png --bbox-ref '#FF0000' --bbox-test '#0000FF'"
    )
    parser = subparsers.add_parser(
        "overlay",
        help="Alpha-blend two images with optional parsed bbox overlays.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_trailing_global_flags(parser)
    parser.add_argument("image1", help="Reference image path.")
    parser.add_argument("image2", help="Test image path.")
    parser.add_argument("-o", "--output", required=True, help="Output overlay image path (PNG recommended).")
    parser.add_argument(
        "--opacity",
        type=float,
        default=0.5,
        help="Blend opacity for image2 in [0,1]. 0=image1 only, 1=image2 only (default: 0.5).",
    )
    parser.add_argument(
        "--bbox-ref",
        nargs="?",
        const="__DEFAULT__",
        default=None,
        metavar="COLOR",
        help=f"Draw parsed bboxes from image1. Optional color (#RRGGBB). Default: {_DEFAULT_REF_COLOR}.",
    )
    parser.add_argument(
        "--bbox-test",
        nargs="?",
        const="__DEFAULT__",
        default=None,
        metavar="COLOR",
        help=f"Draw parsed bboxes from image2. Optional color (#RRGGBB). Default: {_DEFAULT_TEST_COLOR}.",
    )
    parser.add_argument(
        "--draw-regions",
        action="store_true",
        help="Draw named regions from project config (if config is loaded).",
    )
    parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=default_box_threshold,
        help="Detection threshold used for bbox overlays (default: 0.05).",
    )
    return parser


def _parse_hex_color(value: str) -> tuple[int, int, int]:
    raw = value.strip()
    if len(raw) != 7 or not raw.startswith("#"):
        raise OverlayCommandError(f"Invalid color '{value}'. Expected #RRGGBB.")
    try:
        return (int(raw[1:3], 16), int(raw[3:5], 16), int(raw[5:7], 16))
    except ValueError as exc:
        raise OverlayCommandError(f"Invalid color '{value}'. Expected #RRGGBB.") from exc


def _resolve_flag_color(value: str | None, default_hex: str) -> tuple[int, int, int] | None:
    if value is None:
        return None
    if value == "__DEFAULT__":
        return _parse_hex_color(default_hex)
    return _parse_hex_color(value)


def _draw_bboxes(
    draw: ImageDraw.ImageDraw,
    elements: list[dict[str, Any]],
    color: tuple[int, int, int],
    *,
    label_prefix: str,
) -> None:
    for element in elements:
        bbox = element["bbox"]
        x1 = int(bbox["x"])
        y1 = int(bbox["y"])
        x2 = x1 + int(bbox["width"])
        y2 = y1 + int(bbox["height"])
        draw.rectangle((x1, y1, x2, y2), outline=color, width=2)
        label = f"{label_prefix}{element['index']}"
        draw.text((x1 + 2, max(0, y1 - 12)), label, fill=color)


def _draw_dashed_rect(
    draw: ImageDraw.ImageDraw,
    *,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    color: tuple[int, int, int],
    width: int = 2,
    dash: int = 8,
    gap: int = 6,
) -> None:
    def _segments(start: int, end: int):
        pos = start
        while pos < end:
            seg_end = min(pos + dash, end)
            yield pos, seg_end
            pos += dash + gap

    for xs, xe in _segments(x1, x2):
        draw.line((xs, y1, xe, y1), fill=color, width=width)
        draw.line((xs, y2, xe, y2), fill=color, width=width)
    for ys, ye in _segments(y1, y2):
        draw.line((x1, ys, x1, ye), fill=color, width=width)
        draw.line((x2, ys, x2, ye), fill=color, width=width)


def run_overlay_command(
    *,
    args: Any,
    runtime: Any,
    image1_path: Path,
    image2_path: Path,
    project_config: ProjectConfig | None,
    meta_builder: Any,
    cli_version: str,
) -> tuple[dict[str, Any], int]:
    started_ms = time.perf_counter() * 1000.0
    if args.opacity < 0.0 or args.opacity > 1.0:
        raise OverlayCommandError(f"--opacity must be in [0,1], got {args.opacity}")

    image1 = Image.open(image1_path).convert("RGBA")
    image2 = Image.open(image2_path).convert("RGBA")
    if image1.size != image2.size:
        raise OverlayCommandError(
            "Image dimensions must match for overlay. "
            f"image1={image1.size[0]}x{image1.size[1]}, image2={image2.size[0]}x{image2.size[1]}."
        )

    blended = Image.blend(image1, image2, float(args.opacity)).convert("RGB")
    draw = ImageDraw.Draw(blended)
    _font = ImageFont.load_default()

    ref_color = _resolve_flag_color(args.bbox_ref, _DEFAULT_REF_COLOR)
    test_color = _resolve_flag_color(args.bbox_test, _DEFAULT_TEST_COLOR)

    ref_elements = 0
    test_elements = 0
    cache_hits: list[bool] = []

    if ref_color is not None:
        parsed_ref, ref_cache_hit, _ = runtime.parse_image(
            image_path=image1_path,
            box_threshold=args.confidence_threshold,
            use_cache=args.cache,
        )
        ref_elements = len(parsed_ref["elements"])
        cache_hits.append(ref_cache_hit)
        _draw_bboxes(draw, parsed_ref["elements"], ref_color, label_prefix="R")

    if test_color is not None:
        parsed_test, test_cache_hit, _ = runtime.parse_image(
            image_path=image2_path,
            box_threshold=args.confidence_threshold,
            use_cache=args.cache,
        )
        test_elements = len(parsed_test["elements"])
        cache_hits.append(test_cache_hit)
        _draw_bboxes(draw, parsed_test["elements"], test_color, label_prefix="T")

    regions_drawn = 0
    if args.draw_regions and project_config is not None:
        region_color = _parse_hex_color(_DEFAULT_REGION_COLOR)
        for region_name, region in project_config.regions.items():
            x1 = int(region["x"])
            y1 = int(region["y"])
            x2 = x1 + int(region["w"])
            y2 = y1 + int(region["h"])
            _draw_dashed_rect(
                draw,
                x1=x1,
                y1=y1,
                x2=x2,
                y2=y2,
                color=region_color,
            )
            draw.text((x1 + 2, max(0, y1 - 12)), region_name, fill=region_color, font=_font)
            regions_drawn += 1

    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    blended.save(output_path)

    processing_time_ms = int(round((time.perf_counter() * 1000.0) - started_ms))
    payload = {
        "status": "success",
        "error": None,
        "output_path": str(output_path),
        "dimensions": {"width": blended.size[0], "height": blended.size[1]},
        "ref_elements": ref_elements,
        "test_elements": test_elements,
        "meta": meta_builder(
            image_path=str(image1_path),
            image_width=blended.size[0],
            image_height=blended.size[1],
            processing_time_ms=processing_time_ms,
            omniparser_version=runtime.omniparser_version,
            cli_version=cli_version,
            cache_hit=all(cache_hits) if cache_hits else False,
            extra={
                "image_path_2": str(image2_path),
                "opacity": float(args.opacity),
                "draw_regions": bool(args.draw_regions),
                "regions_drawn": regions_drawn,
                "config_path": str(project_config.path) if project_config else None,
            },
        ),
    }
    return payload, 0
