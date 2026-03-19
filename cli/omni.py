#!/usr/bin/env python3
"""Production CLI wrapper for OmniParser."""

from __future__ import annotations

import argparse
import base64
import contextlib
import csv
import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from check import CheckCommandError, add_check_subparser, run_check_command
from config import OmniConfigError, ProjectConfig, ProjectConfigManager
from overlay import OverlayCommandError, add_overlay_subparser, run_overlay_command
from resolution import EDGE_CHOICES, ResolutionError, resolve_reference_spec, region_to_bbox


CLI_VERSION = "1.0.0"
SCHEMA_VERSION = "1.1"
SCHEMA_RELATIVE_PATHS: dict[str, str] = {
    "parse": "cli/schemas/parse.v1.json",
    "measure": "cli/schemas/measure.v1.json",
    "crop": "cli/schemas/crop.v1.json",
    "diff": "cli/schemas/diff.v1.json",
    "info": "cli/schemas/info.v1.json",
    "check": "cli/schemas/check.v1.json",
    "overlay": "cli/schemas/overlay.v1.json",
}
DEFAULT_BOX_THRESHOLD = 0.05
DEFAULT_OCR_TEXT_THRESHOLD = 0.8
DEFAULT_IOU_THRESHOLD = 0.7
DEFAULT_BATCH_SIZE = 128
DEFAULT_DIFF_TOLERANCE = 5
SUPPORTED_IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".webp",
    ".tif",
    ".tiff",
}


class OmniCLIError(Exception):
    """Base exception for CLI errors."""

    exit_code = 2


class UserInputError(OmniCLIError):
    """Invalid user input."""

    exit_code = 1


class FileMissingError(UserInputError):
    """Missing input file."""

    exit_code = 3


class ProcessingError(OmniCLIError):
    """Runtime processing error."""

    exit_code = 2


class ModelNotFoundError(ProcessingError):
    """Model path resolution failure."""


@dataclass
class CLIContext:
    verbose: bool
    quiet: bool
    no_color: bool


@dataclass(frozen=True)
class ResponseContext:
    command: str
    request_id: str
    timestamp_utc: str


class Console:
    def __init__(self, ctx: CLIContext) -> None:
        self.ctx = ctx

    def info(self, message: str) -> None:
        if not self.ctx.quiet:
            print(message, file=sys.stderr)

    def debug(self, message: str) -> None:
        if self.ctx.verbose and not self.ctx.quiet:
            print(message, file=sys.stderr)

    def warn(self, message: str) -> None:
        if not self.ctx.quiet:
            print(f"Warning: {message}", file=sys.stderr)

    def error(self, message: str) -> None:
        if not self.ctx.quiet:
            print(f"Error: {message}", file=sys.stderr)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _new_request_id() -> str:
    return uuid.uuid4().hex


def _perf_ms() -> float:
    return time.perf_counter() * 1000.0


def _normalize_text(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _safe_label(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _cache_root() -> Path:
    return Path.home() / ".cache" / "omni"


def _compute_iou_xyxy(a: list[float], b: list[float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    denom = area_a + area_b - inter
    if denom <= 0:
        return 0.0
    return inter / denom


def _match_confidence(
    bbox_xyxy_ratio: list[float],
    candidates_xyxy_ratio: list[list[float]],
    candidate_scores: list[float],
) -> float:
    if not candidates_xyxy_ratio:
        return 0.0
    best_iou = -1.0
    best_score = 0.0
    for idx, candidate in enumerate(candidates_xyxy_ratio):
        score = candidate_scores[idx] if idx < len(candidate_scores) else 0.0
        iou = _compute_iou_xyxy(bbox_xyxy_ratio, candidate)
        if iou > best_iou:
            best_iou = iou
            best_score = score
    return float(best_score)


def _ratio_xyxy_to_pixel_xywh(
    bbox_xyxy_ratio: list[float],
    image_width: int,
    image_height: int,
) -> dict[str, int]:
    x1 = int(round(bbox_xyxy_ratio[0] * image_width))
    y1 = int(round(bbox_xyxy_ratio[1] * image_height))
    x2 = int(round(bbox_xyxy_ratio[2] * image_width))
    y2 = int(round(bbox_xyxy_ratio[3] * image_height))

    x1 = max(0, min(image_width, x1))
    x2 = max(0, min(image_width, x2))
    y1 = max(0, min(image_height, y1))
    y2 = max(0, min(image_height, y2))

    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1

    return {
        "x": x1,
        "y": y1,
        "width": max(0, x2 - x1),
        "height": max(0, y2 - y1),
    }


def _xywh_to_xyxy(bbox_xywh: dict[str, int]) -> tuple[int, int, int, int]:
    x1 = int(bbox_xywh["x"])
    y1 = int(bbox_xywh["y"])
    x2 = x1 + int(bbox_xywh["width"])
    y2 = y1 + int(bbox_xywh["height"])
    return x1, y1, x2, y2


def _bbox_point_for_edge(bbox_xywh: dict[str, int], edge: str) -> tuple[int, int]:
    x = bbox_xywh["x"]
    y = bbox_xywh["y"]
    w = bbox_xywh["width"]
    h = bbox_xywh["height"]
    cx = x + w // 2
    cy = y + h // 2
    if edge == "left":
        return x, cy
    if edge == "right":
        return x + w, cy
    if edge == "top":
        return cx, y
    if edge == "bottom":
        return cx, y + h
    if edge == "top-left":
        return x, y
    if edge == "top-right":
        return x + w, y
    if edge == "bottom-left":
        return x, y + h
    if edge == "bottom-right":
        return x + w, y + h
    return cx, cy


def _parse_coord_pair(spec: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\s*(-?\d+)\s*,\s*(-?\d+)\s*", spec)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _parse_region_xywh(spec: str) -> dict[str, int]:
    match = re.fullmatch(r"\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*", spec)
    if not match:
        raise UserInputError(
            "Region must be x,y,width,height (for example: 0,0,300,1080)."
        )
    x = int(match.group(1))
    y = int(match.group(2))
    w = int(match.group(3))
    h = int(match.group(4))
    if w <= 0 or h <= 0:
        raise UserInputError("Region width and height must be > 0.")
    return {"x": x, "y": y, "width": w, "height": h}


def _clamp_region(region_xywh: dict[str, int], image_width: int, image_height: int) -> dict[str, int]:
    x = max(0, region_xywh["x"])
    y = max(0, region_xywh["y"])
    x2 = min(image_width, region_xywh["x"] + region_xywh["width"])
    y2 = min(image_height, region_xywh["y"] + region_xywh["height"])
    if x2 <= x or y2 <= y:
        raise UserInputError("Region is outside image bounds.")
    return {"x": x, "y": y, "width": x2 - x, "height": y2 - y}


def _element_intersects_region(element: dict[str, Any], region_xywh: dict[str, int]) -> bool:
    ex1, ey1, ex2, ey2 = _xywh_to_xyxy(element["bbox"])
    rx1 = region_xywh["x"]
    ry1 = region_xywh["y"]
    rx2 = rx1 + region_xywh["width"]
    ry2 = ry1 + region_xywh["height"]
    ix1 = max(ex1, rx1)
    iy1 = max(ey1, ry1)
    ix2 = min(ex2, rx2)
    iy2 = min(ey2, ry2)
    return ix2 > ix1 and iy2 > iy1


def _resolve_image_path(path_like: str) -> Path:
    path = Path(path_like).expanduser().resolve()
    if not path.exists():
        raise FileMissingError(f"File not found: {path}")
    if not path.is_file():
        raise UserInputError(f"Input path is not a file: {path}")
    return path


def _load_image_validated(path: Path) -> Image.Image:
    if path.suffix and path.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
        raise UserInputError(
            f"Unsupported image extension '{path.suffix}'. Supported: {', '.join(sorted(SUPPORTED_IMAGE_EXTENSIONS))}"
        )
    try:
        image = Image.open(path)
    except (UnidentifiedImageError, OSError) as exc:
        raise UserInputError(f"Unable to read image file '{path}': {exc}") from exc
    return image


def _finalize_payload(
    payload: dict[str, Any],
    *,
    response_context: ResponseContext,
) -> dict[str, Any]:
    payload["schema_version"] = SCHEMA_VERSION
    payload["command"] = response_context.command
    payload["request_id"] = response_context.request_id
    payload["timestamp_utc"] = response_context.timestamp_utc
    payload.setdefault("warnings", [])
    return payload


def _write_json_stdout(
    payload: dict[str, Any],
    *,
    response_context: ResponseContext | None = None,
) -> None:
    payload_to_print = dict(payload)
    if response_context is not None:
        payload_to_print = _finalize_payload(payload_to_print, response_context=response_context)
    print(json.dumps(payload_to_print, ensure_ascii=False, sort_keys=True))


def _omniparser_version(repo_root: Path) -> str:
    try:
        sha = (
            subprocess.check_output(
                ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
                stderr=subprocess.DEVNULL,
                text=True,
            )
            .strip()
            .lower()
        )
        dirty = subprocess.check_output(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return f"{sha}-dirty" if dirty else sha
    except Exception:
        return "unknown"


def _default_model_dir(repo_root: Path) -> Path:
    env_override = os.environ.get("OMNI_MODEL_DIR")
    if env_override:
        return Path(env_override).expanduser().resolve()
    return (repo_root / "weights").resolve()


def _config_sha256(project_config: ProjectConfig | None) -> str | None:
    if project_config is None:
        return None
    return _sha256_file(project_config.path)


def _schema_path_for_command(repo_root: Path, command: str) -> Path:
    relative = SCHEMA_RELATIVE_PATHS.get(command)
    if relative is None:
        raise UserInputError(
            f"--schema is not supported for command '{command}'."
        )
    schema_path = (repo_root / relative).resolve()
    if not schema_path.exists():
        raise ProcessingError(
            f"Schema file not found for '{command}': {schema_path}"
        )
    return schema_path


def _guess_command_from_argv(raw_argv: list[str]) -> str:
    for token in raw_argv:
        if token in SCHEMA_RELATIVE_PATHS:
            return token
    return "global"


def _error_hint(exc: Exception) -> str | None:
    if isinstance(exc, FileMissingError):
        return "Verify the file path is absolute or relative to the current directory and that it exists."
    if isinstance(exc, ModelNotFoundError):
        return "Download model weights into the OmniParser weights directory or set OMNI_MODEL_DIR."
    if isinstance(exc, OmniConfigError):
        return "Fix .omni.json validation errors, or pass --config <path> to a valid config file."
    if isinstance(exc, ResolutionError):
        return "Use coordinates (x,y), element:<index>, a fuzzy label, or region:<name> with a loaded config."
    if isinstance(exc, UserInputError):
        return "Run the subcommand with --help and correct the provided arguments."
    if isinstance(exc, CheckCommandError):
        return "Validate assertion IDs passed to --only/--skip and ensure referenced regions exist."
    if isinstance(exc, OverlayCommandError):
        return "Ensure both images exist, have the same dimensions, and color values use #RRGGBB format."
    if isinstance(exc, ProcessingError):
        return "Retry once. If it persists, run with --verbose and inspect model availability and logs."
    return "Retry with --verbose and inspect stderr for additional context."


def _error_retryable(exc: Exception) -> bool:
    if isinstance(exc, (FileMissingError, UserInputError, ResolutionError, OmniConfigError, CheckCommandError, OverlayCommandError)):
        return False
    if isinstance(exc, ModelNotFoundError):
        return False
    return True


def _meta(
    *,
    response_context: ResponseContext,
    image_path: str,
    image_width: int,
    image_height: int,
    processing_time_ms: int,
    omniparser_version: str,
    cli_version: str,
    cache_hit: bool | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "command": response_context.command,
        "request_id": response_context.request_id,
        "timestamp_utc": response_context.timestamp_utc,
        "image_path": image_path,
        "image_width": image_width,
        "image_height": image_height,
        "processing_time_ms": processing_time_ms,
        "omniparser_version": omniparser_version,
        "cli_version": cli_version,
    }
    if cache_hit is not None:
        payload["cache_hit"] = cache_hit
    if extra:
        payload.update(extra)
    return payload


class OmniRuntime:
    def __init__(
        self,
        *,
        repo_root: Path,
        model_dir: Path,
        requested_device: str,
        logger: Console,
        omniparser_version: str,
    ) -> None:
        self.repo_root = repo_root
        self.model_dir = model_dir
        self.requested_device = requested_device
        self.logger = logger
        self.omniparser_version = omniparser_version

        self._omni_utils: Any = None
        self._som_model: Any = None
        self._caption_model_processor: Any = None
        self.effective_device = "cpu"

    def _resolve_model_paths(self) -> tuple[Path, Path]:
        som_path = self.model_dir / "icon_detect" / "model.pt"
        caption_path = self.model_dir / "icon_caption_florence"

        if not som_path.exists():
            onnx_path = som_path.with_suffix(".onnx")
            if onnx_path.exists():
                som_path = onnx_path

        if not som_path.exists() or not caption_path.exists():
            hint = (
                "Missing OmniParser model files. Expected paths:\n"
                f"- {self.model_dir / 'icon_detect' / 'model.pt'} (or model.onnx)\n"
                f"- {self.model_dir / 'icon_caption_florence'}\n"
                "Download instructions (from OmniParser README):\n"
                "for f in icon_detect/{train_args.yaml,model.pt,model.yaml} "
                "icon_caption/{config.json,generation_config.json,model.safetensors}; "
                "do huggingface-cli download microsoft/OmniParser-v2.0 \"$f\" --local-dir weights; done\n"
                "mv weights/icon_caption weights/icon_caption_florence"
            )
            raise ModelNotFoundError(hint)

        return som_path.resolve(), caption_path.resolve()

    def _captured_call(self, fn: Any, *args: Any, **kwargs: Any) -> tuple[Any, str]:
        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()
        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
            result = fn(*args, **kwargs)
        logs = stdout_buffer.getvalue() + stderr_buffer.getvalue()
        return result, logs

    def _ensure_loaded(self) -> str:
        if self._som_model is not None and self._caption_model_processor is not None:
            return ""

        if str(self.repo_root) not in sys.path:
            sys.path.insert(0, str(self.repo_root))

        def _load() -> None:
            import importlib

            self._omni_utils = importlib.import_module("util.utils")
            som_path, caption_path = self._resolve_model_paths()

            if self.requested_device == "cuda":
                import torch

                if not torch.cuda.is_available():
                    raise UserInputError(
                        "--device cuda requested, but CUDA is not available on this machine."
                    )
                self.logger.warn(
                    "OmniParser utility code currently forces CPU execution. Running on CPU."
                )

            self.effective_device = "cpu"
            self._som_model = self._omni_utils.get_yolo_model(model_path=str(som_path))
            self._caption_model_processor = self._omni_utils.get_caption_model_processor(
                model_name="florence2",
                model_name_or_path=str(caption_path),
                device=self.effective_device,
            )

        _, logs = self._captured_call(_load)
        return logs

    def parse_image(
        self,
        *,
        image_path: Path,
        box_threshold: float,
        use_cache: bool,
    ) -> tuple[dict[str, Any], bool, str]:
        cache_dir = _cache_root() / "parse"
        cache_dir.mkdir(parents=True, exist_ok=True)

        image_hash = _sha256_file(image_path)
        cache_payload_key = {
            "image_hash": image_hash,
            "box_threshold": round(box_threshold, 6),
            "model_dir": str(self.model_dir),
            "requested_device": self.requested_device,
            "omniparser_version": self.omniparser_version,
        }
        cache_key = hashlib.sha256(
            json.dumps(cache_payload_key, sort_keys=True).encode("utf-8")
        ).hexdigest()
        cache_path = cache_dir / f"{cache_key}.json"

        if use_cache and cache_path.exists():
            with cache_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data, True, ""

        model_logs = self._ensure_loaded()

        def _run_parse() -> dict[str, Any]:
            image = _load_image_validated(image_path).convert("RGB")
            image_width, image_height = image.size

            np_image = self._omni_utils.np.array(image)
            ocr_raw = self._omni_utils.paddle_ocr.ocr(np_image, cls=False)
            ocr_lines = ocr_raw[0] if ocr_raw else []

            ocr_texts: list[str] = []
            ocr_bboxes_xyxy_pixel: list[list[int]] = []
            ocr_scores: list[float] = []

            for line in ocr_lines:
                score = float(line[1][1])
                if score <= DEFAULT_OCR_TEXT_THRESHOLD:
                    continue
                ocr_texts.append(str(line[1][0]))
                bbox_xyxy = self._omni_utils.get_xyxy(line[0])
                ocr_bboxes_xyxy_pixel.append([bbox_xyxy[0], bbox_xyxy[1], bbox_xyxy[2], bbox_xyxy[3]])
                ocr_scores.append(score)

            overlay_ratio = max(image.size) / 3200.0
            draw_bbox_config = {
                "text_scale": 0.8 * overlay_ratio,
                "text_thickness": max(int(2 * overlay_ratio), 1),
                "text_padding": max(int(3 * overlay_ratio), 1),
                "thickness": max(int(3 * overlay_ratio), 1),
            }

            annotated_b64, label_coordinates_ratio_xywh, parsed_content_list = self._omni_utils.get_som_labeled_img(
                image,
                self._som_model,
                BOX_TRESHOLD=box_threshold,
                output_coord_in_ratio=True,
                ocr_bbox=ocr_bboxes_xyxy_pixel,
                draw_bbox_config=draw_bbox_config,
                caption_model_processor=self._caption_model_processor,
                ocr_text=ocr_texts,
                use_local_semantics=True,
                iou_threshold=DEFAULT_IOU_THRESHOLD,
                scale_img=False,
                batch_size=DEFAULT_BATCH_SIZE,
            )

            yolo_boxes_xyxy_pixel, yolo_conf_tensor, _ = self._omni_utils.predict_yolo(
                model=self._som_model,
                image=image,
                box_threshold=box_threshold,
                imgsz=(image_height, image_width),
                scale_img=False,
                iou_threshold=0.1,
            )

            yolo_boxes_xyxy_pixel_list = yolo_boxes_xyxy_pixel.cpu().tolist()
            yolo_scores = [float(value) for value in yolo_conf_tensor.cpu().tolist()]

            yolo_boxes_xyxy_ratio = [
                [
                    box[0] / image_width,
                    box[1] / image_height,
                    box[2] / image_width,
                    box[3] / image_height,
                ]
                for box in yolo_boxes_xyxy_pixel_list
            ]
            ocr_boxes_xyxy_ratio = [
                [
                    box[0] / image_width,
                    box[1] / image_height,
                    box[2] / image_width,
                    box[3] / image_height,
                ]
                for box in ocr_bboxes_xyxy_pixel
            ]

            structured_elements: list[dict[str, Any]] = []
            for idx, element in enumerate(parsed_content_list):
                bbox_ratio = [float(v) for v in element.get("bbox", [0, 0, 0, 0])]
                bbox_xywh_pixel = _ratio_xyxy_to_pixel_xywh(
                    bbox_ratio, image_width=image_width, image_height=image_height
                )

                element_type = str(element.get("type", "unknown"))
                if element_type == "text":
                    confidence = _match_confidence(
                        bbox_ratio,
                        ocr_boxes_xyxy_ratio,
                        ocr_scores,
                    )
                else:
                    confidence = _match_confidence(
                        bbox_ratio,
                        yolo_boxes_xyxy_ratio,
                        yolo_scores,
                    )

                structured_elements.append(
                    {
                        "index": idx,
                        "bbox": bbox_xywh_pixel,
                        "label": _safe_label(element.get("content")),
                        "element_type": element_type,
                        "confidence": round(float(confidence), 6),
                        "interactable": bool(element.get("interactivity", False)),
                        "source": _safe_label(element.get("source")),
                        "bbox_ratio": [round(v, 8) for v in bbox_ratio],
                    }
                )

            return {
                "image_path": str(image_path),
                "image_width": image_width,
                "image_height": image_height,
                "elements": structured_elements,
                "annotated_image_base64": annotated_b64,
                "raw_parsed_content_list": parsed_content_list,
                "raw_label_coordinates_ratio_xywh": label_coordinates_ratio_xywh,
                "raw_ocr": {
                    "texts": ocr_texts,
                    "bboxes_xyxy_pixel": ocr_bboxes_xyxy_pixel,
                    "scores": [round(v, 6) for v in ocr_scores],
                },
                "box_threshold": box_threshold,
                "effective_device": self.effective_device,
            }

        parsed_data, parse_logs = self._captured_call(_run_parse)
        logs = model_logs + parse_logs

        if use_cache:
            with cache_path.open("w", encoding="utf-8") as handle:
                json.dump(parsed_data, handle)

        return parsed_data, False, logs


def _save_annotated_image(annotated_b64: str, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(base64.b64decode(annotated_b64))
    return output_path.resolve()


def _resolve_element_by_spec(
    *,
    spec: str,
    elements: list[dict[str, Any]],
    image_width: int,
    image_height: int,
    edge: str,
    role: str,
    project_config: ProjectConfig | None = None,
) -> dict[str, Any]:
    return resolve_reference_spec(
        spec=spec,
        elements=elements,
        image_width=image_width,
        image_height=image_height,
        edge=edge,
        role=role,
        regions=project_config.regions if project_config else None,
    )


def _print_parse_table(elements: list[dict[str, Any]]) -> None:
    headers = ["idx", "type", "conf", "x", "y", "w", "h", "label"]
    rows = []
    for element in elements:
        rows.append(
            [
                str(element["index"]),
                str(element["element_type"]),
                f"{float(element['confidence']):.3f}",
                str(element["bbox"]["x"]),
                str(element["bbox"]["y"]),
                str(element["bbox"]["width"]),
                str(element["bbox"]["height"]),
                element["label"].replace("\n", " "),
            ]
        )

    widths = [len(h) for h in headers]
    for row in rows:
        for i, value in enumerate(row):
            widths[i] = max(widths[i], len(value))

    def _format(values: list[str]) -> str:
        return "  ".join(value.ljust(widths[i]) for i, value in enumerate(values))

    print(_format(headers))
    print(_format(["-" * width for width in widths]))
    for row in rows:
        print(_format(row))


def _print_parse_csv(elements: list[dict[str, Any]]) -> None:
    writer = csv.writer(sys.stdout)
    writer.writerow(["index", "element_type", "confidence", "x", "y", "width", "height", "label"])
    for element in elements:
        writer.writerow(
            [
                element["index"],
                element["element_type"],
                element["confidence"],
                element["bbox"]["x"],
                element["bbox"]["y"],
                element["bbox"]["width"],
                element["bbox"]["height"],
                element["label"],
            ]
        )


def _diff_structural(
    elements_a: list[dict[str, Any]],
    elements_b: list[dict[str, Any]],
    tolerance_px: int,
) -> dict[str, Any]:
    groups_a: dict[tuple[str, str], list[dict[str, Any]]] = {}
    groups_b: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def _group_key(element: dict[str, Any]) -> tuple[str, str]:
        return (
            str(element.get("element_type", "unknown")),
            _normalize_text(_safe_label(element.get("label"))),
        )

    for element in elements_a:
        groups_a.setdefault(_group_key(element), []).append(element)
    for element in elements_b:
        groups_b.setdefault(_group_key(element), []).append(element)

    matched_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    removed: list[dict[str, Any]] = []
    added: list[dict[str, Any]] = []

    for key in sorted(set(groups_a.keys()) | set(groups_b.keys())):
        a_list = sorted(groups_a.get(key, []), key=lambda item: int(item["index"]))
        b_list = sorted(groups_b.get(key, []), key=lambda item: int(item["index"]))
        used_b: set[int] = set()

        for a_item in a_list:
            best_j = None
            best_distance = float("inf")
            ax, ay = _bbox_point_for_edge(a_item["bbox"], edge="center")
            for j, b_item in enumerate(b_list):
                if j in used_b:
                    continue
                bx, by = _bbox_point_for_edge(b_item["bbox"], edge="center")
                distance = abs(ax - bx) + abs(ay - by)
                if distance < best_distance:
                    best_distance = distance
                    best_j = j
            if best_j is None:
                removed.append(a_item)
            else:
                used_b.add(best_j)
                matched_pairs.append((a_item, b_list[best_j]))

        for j, b_item in enumerate(b_list):
            if j not in used_b:
                added.append(b_item)

    moved_or_resized: list[dict[str, Any]] = []
    unchanged = 0

    for a_item, b_item in matched_pairs:
        delta_x = b_item["bbox"]["x"] - a_item["bbox"]["x"]
        delta_y = b_item["bbox"]["y"] - a_item["bbox"]["y"]
        delta_w = b_item["bbox"]["width"] - a_item["bbox"]["width"]
        delta_h = b_item["bbox"]["height"] - a_item["bbox"]["height"]
        changed = any(
            abs(delta) > tolerance_px
            for delta in (delta_x, delta_y, delta_w, delta_h)
        )
        if changed:
            moved_or_resized.append(
                {
                    "label": b_item["label"],
                    "element_type": b_item["element_type"],
                    "image1_index": a_item["index"],
                    "image2_index": b_item["index"],
                    "image1_bbox": a_item["bbox"],
                    "image2_bbox": b_item["bbox"],
                    "delta": {
                        "x": delta_x,
                        "y": delta_y,
                        "width": delta_w,
                        "height": delta_h,
                    },
                }
            )
        else:
            unchanged += 1

    denominator = max(1, max(len(elements_a), len(elements_b)))
    similarity = round(unchanged / denominator, 6)

    return {
        "removed_elements": removed,
        "added_elements": added,
        "moved_or_resized": moved_or_resized,
        "matched_pairs": len(matched_pairs),
        "unchanged_pairs": unchanged,
        "similarity_score": similarity,
    }


def _save_diff_visual(
    *,
    image_a_path: Path,
    image_b_path: Path,
    output_path: Path,
    removed_elements: list[dict[str, Any]],
    added_elements: list[dict[str, Any]],
    moved_or_resized: list[dict[str, Any]],
) -> Path:
    image_a = Image.open(image_a_path).convert("RGB")
    image_b = Image.open(image_b_path).convert("RGB")

    gap = 24
    canvas_width = image_a.width + gap + image_b.width
    canvas_height = max(image_a.height, image_b.height)
    canvas = Image.new("RGB", (canvas_width, canvas_height), (255, 255, 255))
    canvas.paste(image_a, (0, 0))
    canvas.paste(image_b, (image_a.width + gap, 0))

    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()

    def draw_box(box: dict[str, int], offset_x: int, color: tuple[int, int, int], label: str) -> None:
        x1, y1, x2, y2 = _xywh_to_xyxy(box)
        draw.rectangle(
            [x1 + offset_x, y1, x2 + offset_x, y2],
            outline=color,
            width=3,
        )
        draw.text((x1 + offset_x + 2, max(0, y1 - 12)), label, fill=color, font=font)

    right_offset = image_a.width + gap
    for element in removed_elements:
        draw_box(element["bbox"], 0, (210, 30, 30), f"- {element['index']}")
    for element in added_elements:
        draw_box(element["bbox"], right_offset, (30, 160, 30), f"+ {element['index']}")
    for change in moved_or_resized:
        draw_box(change["image1_bbox"], 0, (190, 140, 0), f"~ {change['image1_index']}")
        draw_box(
            change["image2_bbox"],
            right_offset,
            (190, 140, 0),
            f"~ {change['image2_index']}",
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path)
    return output_path.resolve()


def _layout_summary(elements: list[dict[str, Any]], image_width: int, image_height: int) -> list[str]:
    if not elements:
        return ["No UI elements were detected."]

    summaries: list[str] = []
    left = [
        element
        for element in elements
        if element["bbox"]["x"] + element["bbox"]["width"] / 2 < image_width * 0.35
    ]
    right = [
        element
        for element in elements
        if element["bbox"]["x"] + element["bbox"]["width"] / 2 > image_width * 0.65
    ]

    min_sidebar_count = max(3, int(len(elements) * 0.18))
    sidebar_width = None

    if len(left) >= min_sidebar_count:
        sidebar_width = max(item["bbox"]["x"] + item["bbox"]["width"] for item in left)
        summaries.append(f"Sidebar likely on left, approximately {sidebar_width}px wide.")

    if len(right) >= min_sidebar_count:
        right_start = min(item["bbox"]["x"] for item in right)
        summaries.append(
            f"Right rail detected, starting near x={right_start}px (about {image_width - right_start}px wide)."
        )

    top_bars = [
        element
        for element in elements
        if element["bbox"]["y"] < image_height * 0.12
        and element["bbox"]["width"] > image_width * 0.45
    ]
    if top_bars:
        max_header = max(item["bbox"]["y"] + item["bbox"]["height"] for item in top_bars)
        summaries.append(f"Header/navigation strip likely occupies top ~{max_header}px.")

    if sidebar_width is not None:
        summaries.append(
            f"Main content area likely begins around x={sidebar_width}px and spans ~{image_width - sidebar_width}px."
        )

    if not summaries:
        summaries.append("Layout appears multi-region but no dominant sidebar/header pattern was detected.")
    return summaries


def _output_error_json(
    *,
    response_context: ResponseContext,
    message: str,
    exit_code: int,
    error_type: str,
    hint: str | None,
    retryable: bool,
    context_meta: dict[str, Any] | None,
) -> None:
    payload = {
        "status": "error",
        "error": {
            "code": exit_code,
            "type": error_type,
            "message": message,
            "hint": hint,
            "retryable": retryable,
        },
        "meta": context_meta or {},
    }
    _write_json_stdout(payload, response_context=response_context)


def _cmd_parse(
    args: argparse.Namespace,
    runtime: OmniRuntime,
    logger: Console,
    project_config: ProjectConfig | None,
    response_context: ResponseContext,
    config_sha256: str | None,
) -> int:
    start_ms = _perf_ms()
    image_path = _resolve_image_path(args.image)
    image_sha256 = _sha256_file(image_path)

    parsed_data, cache_hit, logs = runtime.parse_image(
        image_path=image_path,
        box_threshold=args.confidence_threshold,
        use_cache=args.cache,
    )

    if args.verbose and logs:
        logger.debug(logs.rstrip())

    processing_time_ms = int(round(_perf_ms() - start_ms))

    if args.save_annotated:
        save_path = Path(args.save_annotated).expanduser().resolve()
        save_path = _save_annotated_image(parsed_data["annotated_image_base64"], save_path)
        logger.info(f"Saved annotated image: {save_path}")

    if args.format == "json":
        payload: dict[str, Any] = {
            "status": "success",
            "error": None,
            "meta": _meta(
                response_context=response_context,
                image_path=str(image_path),
                image_width=int(parsed_data["image_width"]),
                image_height=int(parsed_data["image_height"]),
                processing_time_ms=processing_time_ms,
                omniparser_version=runtime.omniparser_version,
                cli_version=CLI_VERSION,
                cache_hit=cache_hit,
                extra={
                    "box_threshold": args.confidence_threshold,
                    "device": runtime.effective_device,
                    "config_path": str(project_config.path) if project_config else None,
                    "image_sha256": image_sha256,
                    "config_sha256": config_sha256,
                },
            ),
            "image_width": int(parsed_data["image_width"]),
            "image_height": int(parsed_data["image_height"]),
            "elements": [
                {
                    "index": element["index"],
                    "bbox": element["bbox"],
                    "label": element["label"],
                    "element_type": element["element_type"],
                    "confidence": element["confidence"],
                    "interactable": element["interactable"],
                }
                for element in parsed_data["elements"]
            ],
        }
        if args.raw:
            payload["raw"] = {
                "annotated_image_base64": parsed_data["annotated_image_base64"],
                "parsed_content_list": parsed_data["raw_parsed_content_list"],
                "label_coordinates_ratio_xywh": parsed_data[
                    "raw_label_coordinates_ratio_xywh"
                ],
                "ocr": parsed_data["raw_ocr"],
                "elements_with_ratio": parsed_data["elements"],
            }
        _write_json_stdout(payload, response_context=response_context)
        return 0

    if args.raw:
        logger.warn("--raw is only included in JSON output; ignored for table/csv.")

    if args.format == "table":
        _print_parse_table(parsed_data["elements"])
        return 0

    _print_parse_csv(parsed_data["elements"])
    return 0


def _cmd_measure(
    args: argparse.Namespace,
    runtime: OmniRuntime,
    logger: Console,
    project_config: ProjectConfig | None,
    response_context: ResponseContext,
    config_sha256: str | None,
) -> int:
    start_ms = _perf_ms()
    image_path = _resolve_image_path(args.image)
    image_sha256 = _sha256_file(image_path)

    parsed_data, cache_hit, logs = runtime.parse_image(
        image_path=image_path,
        box_threshold=args.confidence_threshold,
        use_cache=args.cache,
    )
    if args.verbose and logs:
        logger.debug(logs.rstrip())

    from_resolved = _resolve_element_by_spec(
        spec=args.from_spec,
        elements=parsed_data["elements"],
        image_width=parsed_data["image_width"],
        image_height=parsed_data["image_height"],
        edge=args.edge,
        role="--from",
        project_config=project_config,
    )
    to_resolved = _resolve_element_by_spec(
        spec=args.to_spec,
        elements=parsed_data["elements"],
        image_width=parsed_data["image_width"],
        image_height=parsed_data["image_height"],
        edge=args.edge,
        role="--to",
        project_config=project_config,
    )

    fx = int(from_resolved["resolved_point"]["x"])
    fy = int(from_resolved["resolved_point"]["y"])
    tx = int(to_resolved["resolved_point"]["x"])
    ty = int(to_resolved["resolved_point"]["y"])

    delta_x = tx - fx
    delta_y = ty - fy
    horizontal_distance = abs(delta_x)
    vertical_distance = abs(delta_y)
    euclidean_distance = round(math.hypot(delta_x, delta_y), 4)

    if args.axis == "x":
        selected_distance = horizontal_distance
    elif args.axis == "y":
        selected_distance = vertical_distance
    else:
        selected_distance = euclidean_distance

    processing_time_ms = int(round(_perf_ms() - start_ms))
    payload = {
        "status": "success",
        "error": None,
        "meta": _meta(
            response_context=response_context,
            image_path=str(image_path),
            image_width=int(parsed_data["image_width"]),
            image_height=int(parsed_data["image_height"]),
            processing_time_ms=processing_time_ms,
            omniparser_version=runtime.omniparser_version,
            cli_version=CLI_VERSION,
            cache_hit=cache_hit,
            extra={
                "box_threshold": args.confidence_threshold,
                "edge": args.edge,
                "axis": args.axis,
                "config_path": str(project_config.path) if project_config else None,
                "image_sha256": image_sha256,
                "config_sha256": config_sha256,
            },
        ),
        "measurement": {
            "from": from_resolved,
            "to": to_resolved,
            "delta_px": {"x": delta_x, "y": delta_y},
            "horizontal_distance_px": horizontal_distance,
            "vertical_distance_px": vertical_distance,
            "euclidean_distance_px": euclidean_distance,
            "selected_axis": args.axis,
            "selected_distance_px": selected_distance,
        },
    }
    _write_json_stdout(payload, response_context=response_context)
    return 0


def _cmd_crop(
    args: argparse.Namespace,
    runtime: OmniRuntime,
    logger: Console,
    project_config: ProjectConfig | None,
    response_context: ResponseContext,
    config_sha256: str | None,
) -> int:
    start_ms = _perf_ms()
    image_path = _resolve_image_path(args.image)
    image_sha256 = _sha256_file(image_path)
    image = _load_image_validated(image_path).convert("RGB")
    image_width, image_height = image.size

    region_xywh: dict[str, int]
    cache_hit = None

    if args.region:
        region_xywh = _parse_region_xywh(args.region)
    elif args.region_name:
        if project_config is None:
            raise OmniConfigError(
                "--region-name requires a project config. Provide --config <path> or create .omni.json."
            )
        if args.region_name not in project_config.regions:
            available = ", ".join(sorted(project_config.regions.keys())) or "<none>"
            raise UserInputError(
                f"Unknown region '{args.region_name}'. Available regions: {available}"
            )
        region = project_config.regions[args.region_name]
        region_bbox = region_to_bbox(region)
        region_xywh = {
            "x": int(region_bbox["x"] - args.padding),
            "y": int(region_bbox["y"] - args.padding),
            "width": int(region_bbox["width"] + (2 * args.padding)),
            "height": int(region_bbox["height"] + (2 * args.padding)),
        }
    else:
        parsed_data, cache_hit, logs = runtime.parse_image(
            image_path=image_path,
            box_threshold=args.confidence_threshold,
            use_cache=args.cache,
        )
        if args.verbose and logs:
            logger.debug(logs.rstrip())

        idx = int(args.element)
        if idx < 0 or idx >= len(parsed_data["elements"]):
            raise UserInputError(
                f"--element {idx} is out of range. Valid indexes: 0..{len(parsed_data['elements']) - 1}."
            )
        bbox = parsed_data["elements"][idx]["bbox"]
        region_xywh = {
            "x": bbox["x"] - args.padding,
            "y": bbox["y"] - args.padding,
            "width": bbox["width"] + (2 * args.padding),
            "height": bbox["height"] + (2 * args.padding),
        }

    region_xywh = _clamp_region(region_xywh, image_width, image_height)

    crop_box = (
        region_xywh["x"],
        region_xywh["y"],
        region_xywh["x"] + region_xywh["width"],
        region_xywh["y"] + region_xywh["height"],
    )
    cropped = image.crop(crop_box)

    output_path: Path | None = None
    wrote_stdout_bytes = False

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cropped.save(output_path, format="PNG")
    else:
        if sys.stdout.isatty():
            output_path = Path.cwd() / f"{image_path.stem}.crop.png"
            output_path = output_path.resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            cropped.save(output_path, format="PNG")
        else:
            buffer = io.BytesIO()
            cropped.save(buffer, format="PNG")
            sys.stdout.buffer.write(buffer.getvalue())
            sys.stdout.buffer.flush()
            wrote_stdout_bytes = True

    if wrote_stdout_bytes:
        return 0

    processing_time_ms = int(round(_perf_ms() - start_ms))
    payload = {
        "status": "success",
        "error": None,
        "meta": _meta(
            response_context=response_context,
            image_path=str(image_path),
            image_width=image_width,
            image_height=image_height,
            processing_time_ms=processing_time_ms,
            omniparser_version=runtime.omniparser_version,
            cli_version=CLI_VERSION,
            cache_hit=cache_hit,
            extra={
                "padding": args.padding,
                "config_path": str(project_config.path) if project_config else None,
                "region_name": args.region_name,
                "image_sha256": image_sha256,
                "config_sha256": config_sha256,
            },
        ),
        "crop": {
            "region": region_xywh,
            "output_path": str(output_path) if output_path else None,
            "output_width": cropped.size[0],
            "output_height": cropped.size[1],
        },
    }
    _write_json_stdout(payload, response_context=response_context)
    return 0


def _cmd_diff(
    args: argparse.Namespace,
    runtime: OmniRuntime,
    logger: Console,
    project_config: ProjectConfig | None,
    response_context: ResponseContext,
    config_sha256: str | None,
) -> int:
    start_ms = _perf_ms()
    image1_path = _resolve_image_path(args.image1)
    image2_path = _resolve_image_path(args.image2)
    image1_sha256 = _sha256_file(image1_path)
    image2_sha256 = _sha256_file(image2_path)

    parsed1, cache_hit1, logs1 = runtime.parse_image(
        image_path=image1_path,
        box_threshold=args.confidence_threshold,
        use_cache=args.cache,
    )
    parsed2, cache_hit2, logs2 = runtime.parse_image(
        image_path=image2_path,
        box_threshold=args.confidence_threshold,
        use_cache=args.cache,
    )
    if args.verbose and logs1:
        logger.debug(logs1.rstrip())
    if args.verbose and logs2:
        logger.debug(logs2.rstrip())

    elements1 = parsed1["elements"]
    elements2 = parsed2["elements"]
    focus_region = None

    if args.focus:
        focus_text = args.focus.strip()
        if focus_text.startswith("element:"):
            idx_text = focus_text.split(":", 1)[1].strip()
            try:
                focus_idx = int(idx_text)
            except ValueError as exc:
                raise UserInputError(f"Invalid --focus value '{args.focus}'.") from exc
            if focus_idx < 0 or focus_idx >= len(elements1):
                raise UserInputError(
                    f"--focus element:{focus_idx} out of range for first image."
                )
            focus_region = elements1[focus_idx]["bbox"]
        else:
            focus_region = _parse_region_xywh(focus_text)

        focus_region = _clamp_region(
            focus_region,
            min(parsed1["image_width"], parsed2["image_width"]),
            min(parsed1["image_height"], parsed2["image_height"]),
        )
        elements1 = [el for el in elements1 if _element_intersects_region(el, focus_region)]
        elements2 = [el for el in elements2 if _element_intersects_region(el, focus_region)]

    diff_result = _diff_structural(
        elements_a=elements1,
        elements_b=elements2,
        tolerance_px=args.tolerance,
    )

    saved_diff_path = None
    if args.save_diff:
        saved_diff_path = _save_diff_visual(
            image_a_path=image1_path,
            image_b_path=image2_path,
            output_path=Path(args.save_diff).expanduser().resolve(),
            removed_elements=diff_result["removed_elements"],
            added_elements=diff_result["added_elements"],
            moved_or_resized=diff_result["moved_or_resized"],
        )

    processing_time_ms = int(round(_perf_ms() - start_ms))
    payload = {
        "status": "success",
        "error": None,
        "meta": _meta(
            response_context=response_context,
            image_path=str(image1_path),
            image_width=int(parsed1["image_width"]),
            image_height=int(parsed1["image_height"]),
            processing_time_ms=processing_time_ms,
            omniparser_version=runtime.omniparser_version,
            cli_version=CLI_VERSION,
            cache_hit=bool(cache_hit1 and cache_hit2),
            extra={
                "image_path_2": str(image2_path),
                "image_width_2": int(parsed2["image_width"]),
                "image_height_2": int(parsed2["image_height"]),
                "tolerance_px": args.tolerance,
                "focus_region": focus_region,
                "config_path": str(project_config.path) if project_config else None,
                "image_sha256": image1_sha256,
                "image_sha256_2": image2_sha256,
                "config_sha256": config_sha256,
            },
        ),
        "diff": {
            "removed_elements": diff_result["removed_elements"],
            "added_elements": diff_result["added_elements"],
            "moved_or_resized": diff_result["moved_or_resized"],
            "similarity_score": diff_result["similarity_score"],
            "matched_pairs": diff_result["matched_pairs"],
            "unchanged_pairs": diff_result["unchanged_pairs"],
            "save_diff_path": str(saved_diff_path) if saved_diff_path else None,
        },
    }
    _write_json_stdout(payload, response_context=response_context)
    return 0


def _cmd_info(
    args: argparse.Namespace,
    runtime: OmniRuntime,
    logger: Console,
    project_config: ProjectConfig | None,
    response_context: ResponseContext,
    config_sha256: str | None,
) -> int:
    start_ms = _perf_ms()
    image_path = _resolve_image_path(args.image)
    image_sha256 = _sha256_file(image_path)
    image = _load_image_validated(image_path)
    image_width, image_height = image.size

    parsed_data, cache_hit, logs = runtime.parse_image(
        image_path=image_path,
        box_threshold=args.confidence_threshold,
        use_cache=args.cache,
    )
    if args.verbose and logs:
        logger.debug(logs.rstrip())

    elements = parsed_data["elements"]
    count_by_type: dict[str, int] = {}
    for element in elements:
        count_by_type[element["element_type"]] = count_by_type.get(element["element_type"], 0) + 1

    processing_time_ms = int(round(_perf_ms() - start_ms))
    payload = {
        "status": "success",
        "error": None,
        "meta": _meta(
            response_context=response_context,
            image_path=str(image_path),
            image_width=image_width,
            image_height=image_height,
            processing_time_ms=processing_time_ms,
            omniparser_version=runtime.omniparser_version,
            cli_version=CLI_VERSION,
            cache_hit=cache_hit,
            extra={
                "format": image.format,
                "mode": image.mode,
                "dpi": list(image.info.get("dpi", (None, None))),
                "config_path": str(project_config.path) if project_config else None,
                "image_sha256": image_sha256,
                "config_sha256": config_sha256,
            },
        ),
        "info": {
            "image": {
                "path": str(image_path),
                "width": image_width,
                "height": image_height,
                "format": image.format,
                "mode": image.mode,
                "dpi": list(image.info.get("dpi", (None, None))),
            },
            "detected_element_count": len(elements),
            "detected_element_count_by_type": count_by_type,
            "layout_summary": _layout_summary(elements, image_width, image_height),
        },
    }
    _write_json_stdout(payload, response_context=response_context)
    return 0


class OmniArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # pragma: no cover - argparse dispatch
        raise UserInputError(message)


def _build_parser(repo_root: Path) -> tuple[OmniArgumentParser, dict[str, argparse.ArgumentParser]]:
    description = (
        "omni: Production CLI wrapper for OmniParser\n\n"
        "Subcommands:\n"
        "  parse    Parse a screenshot into structured UI elements. Example: omni parse screen.png --quiet\n"
        "  measure  Measure pixel distances between points/elements. Example: omni measure screen.png --from element:0 --to element:1\n"
        "  crop     Extract a screenshot region. Example: omni crop screen.png --region 0,0,200,200 -o crop.png\n"
        "  diff     Compare two screenshots structurally. Example: omni diff before.png after.png --tolerance 5\n"
        "  info     Show image metadata and UI summary. Example: omni info screen.png\n"
        "  check    Run project assertions from .omni.json. Example: omni check screen.png --quiet\n"
        "  overlay  Blend two screenshots with optional overlays. Example: omni overlay a.png b.png -o overlay.png\n"
        "  help     Show top-level or subcommand help. Example: omni help parse"
    )

    parser = OmniArgumentParser(
        prog="omni",
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="Shorthand for --format json (where --format is available).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed status and timing to stderr.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress all stderr output.",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output (reserved; current output is plain text).",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print CLI version and OmniParser version, then exit.",
    )
    parser.add_argument(
        "--schema",
        action="store_true",
        help="Print JSON schema path for the selected subcommand and exit.",
    )
    parser.add_argument(
        "--model-dir",
        default=str(_default_model_dir(repo_root)),
        help=f"Override model directory (default: {_default_model_dir(repo_root)}).",
    )
    parser.add_argument(
        "--config",
        help="Explicit path to project config file (.omni.json). Overrides auto-discovery.",
    )
    parser.add_argument(
        "--device",
        choices=["cpu", "cuda"],
        default="cpu",
        help="Inference device (default: cpu).",
    )
    parser.add_argument(
        "--cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable/disable parse result caching in ~/.cache/omni (default: enabled).",
    )

    subparsers = parser.add_subparsers(dest="command")
    subparsers.required = False

    def add_trailing_global_flags(command_parser: argparse.ArgumentParser) -> None:
        """Allow global flags both before and after the subcommand token."""

        command_parser.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help=argparse.SUPPRESS,
        )
        command_parser.add_argument(
            "--verbose",
            action="store_true",
            default=argparse.SUPPRESS,
            help=argparse.SUPPRESS,
        )
        command_parser.add_argument(
            "--quiet",
            action="store_true",
            default=argparse.SUPPRESS,
            help=argparse.SUPPRESS,
        )
        command_parser.add_argument(
            "--no-color",
            action="store_true",
            default=argparse.SUPPRESS,
            help=argparse.SUPPRESS,
        )
        command_parser.add_argument(
            "--model-dir",
            default=argparse.SUPPRESS,
            help=argparse.SUPPRESS,
        )
        command_parser.add_argument(
            "--config",
            default=argparse.SUPPRESS,
            help=argparse.SUPPRESS,
        )
        command_parser.add_argument(
            "--device",
            choices=["cpu", "cuda"],
            default=argparse.SUPPRESS,
            help=argparse.SUPPRESS,
        )
        command_parser.add_argument(
            "--schema",
            action="store_true",
            default=argparse.SUPPRESS,
            help=argparse.SUPPRESS,
        )
        command_parser.add_argument(
            "--cache",
            dest="cache",
            action="store_true",
            default=argparse.SUPPRESS,
            help=argparse.SUPPRESS,
        )
        command_parser.add_argument(
            "--no-cache",
            dest="cache",
            action="store_false",
            default=argparse.SUPPRESS,
            help=argparse.SUPPRESS,
        )

    parse_help = (
        "JSON output schema (status=success):\n"
        "{\n"
        "  \"status\": \"success\",\n"
        "  \"error\": null,\n"
        "  \"meta\": {\"image_path\": str, \"image_width\": int, \"image_height\": int, \"processing_time_ms\": int,\n"
        "            \"omniparser_version\": str, \"cli_version\": str, ...},\n"
        "  \"image_width\": int,\n"
        "  \"image_height\": int,\n"
        "  \"elements\": [\n"
        "    {\"index\": int, \"bbox\": {\"x\": int, \"y\": int, \"width\": int, \"height\": int},\n"
        "     \"label\": str, \"element_type\": str, \"confidence\": float, \"interactable\": bool}\n"
        "  ]\n"
        "}"
    )
    parse_parser = subparsers.add_parser(
        "parse",
        help="Run OmniParser and return structured detection results.",
        epilog=parse_help,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_trailing_global_flags(parse_parser)
    parse_parser.add_argument("image", help="Path to PNG/JPG screenshot.")
    parse_parser.add_argument(
        "--format",
        choices=["json", "table", "csv"],
        default="json",
        help="Output format (default: json).",
    )
    parse_parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=DEFAULT_BOX_THRESHOLD,
        help=(
            "Detection confidence threshold for element boxes "
            f"(default: {DEFAULT_BOX_THRESHOLD}, matching OmniParser server default)."
        ),
    )
    parse_parser.add_argument(
        "--save-annotated",
        help="Save OmniParser annotated image to this path.",
    )
    parse_parser.add_argument(
        "--raw",
        action="store_true",
        help="Include raw OmniParser output fields in JSON response.",
    )

    measure_help = (
        "Examples:\n"
        "  omni measure screen.png --from 120,300 --to 450,300\n"
        "  omni measure screen.png --from element:3 --to element:7 --edge center\n"
        "  omni measure screen.png --from \"sidebar\" --to \"main content\" --axis x"
    )
    measure_parser = subparsers.add_parser(
        "measure",
        help="Measure distances between points or detected elements.",
        epilog=measure_help,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_trailing_global_flags(measure_parser)
    measure_parser.add_argument("image", help="Path to screenshot image.")
    measure_parser.add_argument("--from", dest="from_spec", required=True, help="Source coordinates/element/label.")
    measure_parser.add_argument("--to", dest="to_spec", required=True, help="Target coordinates/element/label.")
    measure_parser.add_argument(
        "--edge",
        choices=list(EDGE_CHOICES),
        default="center",
        help="Which edge/anchor to measure from element boxes (default: center).",
    )
    measure_parser.add_argument(
        "--axis",
        choices=["x", "y", "both"],
        default="both",
        help="Restrict primary measurement axis (default: both).",
    )
    measure_parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=DEFAULT_BOX_THRESHOLD,
        help="Detection threshold used while resolving element references.",
    )

    crop_help = (
        "Examples:\n"
        "  omni crop screen.png --region 0,0,300,1080 -o left.png\n"
        "  omni crop screen.png --element 5 --padding 20 -o button.png\n"
        "  omni crop screen.png --region 0,0,200,200 > crop.png"
    )
    crop_parser = subparsers.add_parser(
        "crop",
        help="Crop image regions by coordinates or element index.",
        epilog=crop_help,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_trailing_global_flags(crop_parser)
    crop_parser.add_argument("image", help="Path to screenshot image.")
    crop_group = crop_parser.add_mutually_exclusive_group(required=True)
    crop_group.add_argument("--region", help="Region as x,y,width,height.")
    crop_group.add_argument("--region-name", help="Named region from config (without the region: prefix).")
    crop_group.add_argument("--element", type=int, help="Element index from parse output.")
    crop_parser.add_argument(
        "--padding",
        type=int,
        default=0,
        help="Extra padding in pixels around element crop (default: 0).",
    )
    crop_parser.add_argument("-o", "--output", help="Output PNG path. If omitted, writes PNG bytes to stdout when piped.")
    crop_parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=DEFAULT_BOX_THRESHOLD,
        help="Detection threshold used while resolving --element.",
    )

    diff_help = (
        "Examples:\n"
        "  omni diff before.png after.png\n"
        "  omni diff before.png after.png --tolerance 8 --save-diff /tmp/diff.png\n"
        "  omni diff before.png after.png --focus 0,0,800,500"
    )
    diff_parser = subparsers.add_parser(
        "diff",
        help="Structurally compare two screenshots.",
        epilog=diff_help,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_trailing_global_flags(diff_parser)
    diff_parser.add_argument("image1", help="First screenshot path.")
    diff_parser.add_argument("image2", help="Second screenshot path.")
    diff_parser.add_argument(
        "--tolerance",
        type=int,
        default=DEFAULT_DIFF_TOLERANCE,
        help=f"Position/size tolerance in pixels (default: {DEFAULT_DIFF_TOLERANCE}).",
    )
    diff_parser.add_argument("--save-diff", help="Save visual side-by-side diff with annotations.")
    diff_parser.add_argument(
        "--focus",
        help="Optional focus region: x,y,width,height or element:<index> (from first image parse).",
    )
    diff_parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=DEFAULT_BOX_THRESHOLD,
        help="Detection threshold used for both images.",
    )

    info_help = (
        "Returns image metadata, detected element counts, and heuristic layout summary.\n\n"
        "JSON output schema (status=success):\n"
        "{\n"
        "  \"status\": \"success\",\n"
        "  \"error\": null,\n"
        "  \"meta\": {...},\n"
        "  \"info\": {\n"
        "    \"image\": {...},\n"
        "    \"detected_element_count\": int,\n"
        "    \"detected_element_count_by_type\": {str: int},\n"
        "    \"layout_summary\": [str, ...]\n"
        "  }\n"
        "}"
    )
    info_parser = subparsers.add_parser(
        "info",
        help="Show image metadata and UI structure summary.",
        epilog=info_help,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_trailing_global_flags(info_parser)
    info_parser.add_argument("image", help="Path to screenshot image.")
    info_parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=DEFAULT_BOX_THRESHOLD,
        help="Detection threshold used for the summary parse.",
    )

    check_parser = add_check_subparser(
        subparsers=subparsers,
        add_trailing_global_flags=add_trailing_global_flags,
        default_box_threshold=DEFAULT_BOX_THRESHOLD,
    )

    overlay_parser = add_overlay_subparser(
        subparsers=subparsers,
        add_trailing_global_flags=add_trailing_global_flags,
        default_box_threshold=DEFAULT_BOX_THRESHOLD,
    )

    help_parser = subparsers.add_parser("help", help="Show help for top-level or a subcommand.")
    help_parser.add_argument(
        "help_command",
        nargs="?",
        choices=["parse", "measure", "crop", "diff", "info", "check", "overlay", "help"],
        help="Subcommand to show help for.",
    )

    subparser_map = {
        "parse": parse_parser,
        "measure": measure_parser,
        "crop": crop_parser,
        "diff": diff_parser,
        "info": info_parser,
        "check": check_parser,
        "overlay": overlay_parser,
        "help": help_parser,
    }
    return parser, subparser_map


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    raw_argv = list(argv) if argv is not None else list(sys.argv[1:])
    omniparser_version = _omniparser_version(repo_root)

    if "--schema" in raw_argv:
        command_token = next(
            (
                token
                for token in raw_argv
                if not token.startswith("-") and token in SCHEMA_RELATIVE_PATHS
            ),
            None,
        )
        if command_token is None:
            schema_flag_idx = raw_argv.index("--schema")
            if (schema_flag_idx + 1) < len(raw_argv):
                candidate = raw_argv[schema_flag_idx + 1]
                if candidate in SCHEMA_RELATIVE_PATHS:
                    command_token = candidate

        if command_token is not None:
            response_context = ResponseContext(
                command=command_token,
                request_id=_new_request_id(),
                timestamp_utc=_utc_timestamp(),
            )
            schema_path = _schema_path_for_command(repo_root, command_token)
            payload = {
                "status": "success",
                "error": None,
                "warnings": [],
                "schema": {
                    "command": command_token,
                    "schema_version": SCHEMA_VERSION,
                    "path": str(schema_path),
                },
                "meta": {
                    "command": command_token,
                    "request_id": response_context.request_id,
                    "timestamp_utc": response_context.timestamp_utc,
                    "processing_time_ms": 0,
                    "omniparser_version": _omniparser_version(repo_root),
                    "cli_version": CLI_VERSION,
                },
            }
            _write_json_stdout(payload, response_context=response_context)
            return 0

    parser, subparser_map = _build_parser(repo_root)

    try:
        args = parser.parse_args(argv)
    except UserInputError as exc:
        command = _guess_command_from_argv(raw_argv)
        response_context = ResponseContext(
            command=command,
            request_id=_new_request_id(),
            timestamp_utc=_utc_timestamp(),
        )
        if "--quiet" not in raw_argv:
            print(f"Error: {exc}", file=sys.stderr)
            print("Run `omni --help` for usage.", file=sys.stderr)
        _output_error_json(
            response_context=response_context,
            message=str(exc),
            exit_code=1,
            error_type="UserInputError",
            hint="Run the command with --help and provide the required arguments.",
            retryable=False,
            context_meta={
                "command": command,
                "request_id": response_context.request_id,
                "timestamp_utc": response_context.timestamp_utc,
                "processing_time_ms": 0,
                "omniparser_version": omniparser_version,
                "cli_version": CLI_VERSION,
            },
        )
        return 1

    ctx = CLIContext(verbose=bool(args.verbose), quiet=bool(args.quiet), no_color=bool(args.no_color))
    logger = Console(ctx)

    if args.version:
        print(f"omni {CLI_VERSION} (omniparser {omniparser_version})")
        return 0

    if args.command in (None, "help"):
        if args.command == "help" and args.help_command:
            subparser_map[args.help_command].print_help()
        else:
            parser.print_help()
        return 0

    if args.schema:
        response_context = ResponseContext(
            command=str(args.command),
            request_id=_new_request_id(),
            timestamp_utc=_utc_timestamp(),
        )
        schema_path = _schema_path_for_command(repo_root, str(args.command))
        payload = {
            "status": "success",
            "error": None,
            "warnings": [],
            "schema": {
                "command": str(args.command),
                "schema_version": SCHEMA_VERSION,
                "path": str(schema_path),
            },
            "meta": {
                "command": str(args.command),
                "request_id": response_context.request_id,
                "timestamp_utc": response_context.timestamp_utc,
                "processing_time_ms": 0,
                "omniparser_version": omniparser_version,
                "cli_version": CLI_VERSION,
            },
        }
        _write_json_stdout(payload, response_context=response_context)
        return 0

    if hasattr(args, "format") and args.json:
        args.format = "json"
    command_start = _perf_ms()
    response_context = ResponseContext(
        command=str(args.command),
        request_id=_new_request_id(),
        timestamp_utc=_utc_timestamp(),
    )
    try:
        config_manager = ProjectConfigManager(
            cwd=Path.cwd(),
            explicit_path=getattr(args, "config", None),
        )
        project_config = config_manager.get(required=(args.command == "check"))
        config_sha256 = _config_sha256(project_config)

        model_dir = Path(args.model_dir).expanduser().resolve()
        runtime = OmniRuntime(
            repo_root=repo_root,
            model_dir=model_dir,
            requested_device=args.device,
            logger=logger,
            omniparser_version=omniparser_version,
        )

        if args.command == "parse":
            return _cmd_parse(args, runtime, logger, project_config, response_context, config_sha256)
        if args.command == "measure":
            return _cmd_measure(args, runtime, logger, project_config, response_context, config_sha256)
        if args.command == "crop":
            return _cmd_crop(args, runtime, logger, project_config, response_context, config_sha256)
        if args.command == "diff":
            return _cmd_diff(args, runtime, logger, project_config, response_context, config_sha256)
        if args.command == "info":
            return _cmd_info(args, runtime, logger, project_config, response_context, config_sha256)
        if args.command == "check":
            image_path = _resolve_image_path(args.image)
            payload, exit_code = run_check_command(
                args=args,
                runtime=runtime,
                project_config=project_config,
                image_path=image_path,
                response_context=response_context,
                config_sha256=config_sha256,
                meta_builder=_meta,
                cli_version=CLI_VERSION,
            )
            _write_json_stdout(payload, response_context=response_context)
            return exit_code
        if args.command == "overlay":
            image1_path = _resolve_image_path(args.image1)
            image2_path = _resolve_image_path(args.image2)
            payload, exit_code = run_overlay_command(
                args=args,
                runtime=runtime,
                image1_path=image1_path,
                image2_path=image2_path,
                project_config=project_config,
                response_context=response_context,
                config_sha256=config_sha256,
                meta_builder=_meta,
                cli_version=CLI_VERSION,
            )
            _write_json_stdout(payload, response_context=response_context)
            return exit_code
        raise UserInputError(f"Unknown command: {args.command}")
    except (
        OmniCLIError,
        OmniConfigError,
        ResolutionError,
        CheckCommandError,
        OverlayCommandError,
    ) as exc:
        processing_time_ms = int(round(_perf_ms() - command_start))
        logger.error(str(exc))

        error_meta = {
            "processing_time_ms": processing_time_ms,
            "omniparser_version": omniparser_version,
            "cli_version": CLI_VERSION,
            "command": response_context.command,
            "request_id": response_context.request_id,
            "timestamp_utc": response_context.timestamp_utc,
        }
        explicit_config = getattr(args, "config", None)
        if explicit_config:
            error_meta["config_path"] = str(Path(explicit_config).expanduser().resolve())
        if getattr(args, "image", None):
            try:
                image_path = str(Path(getattr(args, "image")).expanduser().resolve())
            except Exception:
                image_path = str(getattr(args, "image"))
            error_meta["image_path"] = image_path

        _output_error_json(
            response_context=response_context,
            message=str(exc),
            exit_code=exc.exit_code,
            error_type=exc.__class__.__name__,
            hint=_error_hint(exc),
            retryable=_error_retryable(exc),
            context_meta=error_meta,
        )
        return exc.exit_code
    except KeyboardInterrupt:
        logger.error("Interrupted by user.")
        _output_error_json(
            response_context=response_context,
            message="Interrupted by user.",
            exit_code=2,
            error_type="KeyboardInterrupt",
            hint="Retry the command. If interruption is recurring, run with --quiet in automation.",
            retryable=True,
            context_meta={
                "processing_time_ms": int(round(_perf_ms() - command_start)),
                "omniparser_version": omniparser_version,
                "cli_version": CLI_VERSION,
                "command": response_context.command,
                "request_id": response_context.request_id,
                "timestamp_utc": response_context.timestamp_utc,
            },
        )
        return 2
    except Exception as exc:
        logger.error(f"Unexpected processing failure: {exc}")
        _output_error_json(
            response_context=response_context,
            message=f"Unexpected processing failure: {exc}",
            exit_code=2,
            error_type=exc.__class__.__name__,
            hint=_error_hint(exc),
            retryable=True,
            context_meta={
                "processing_time_ms": int(round(_perf_ms() - command_start)),
                "omniparser_version": omniparser_version,
                "cli_version": CLI_VERSION,
                "command": response_context.command,
                "request_id": response_context.request_id,
                "timestamp_utc": response_context.timestamp_utc,
            },
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
