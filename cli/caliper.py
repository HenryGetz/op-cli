#!/usr/bin/env python3
"""Production CLI wrapper for OmniParser and UIED."""

from __future__ import annotations

import argparse
import base64
import contextlib
import csv
import difflib
import hashlib
import importlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from check import CheckCommandError, add_check_subparser, run_check_command
from config import OmniConfigError, ProjectConfig, ProjectConfigManager
from engines.base import DetectedElement
from engines.registry import get_engine, list_engine_status, list_engines
from overlay import OverlayCommandError, add_overlay_subparser, run_overlay_command
from resolution import EDGE_CHOICES, ResolutionError, rank_reference_candidates, resolve_reference_spec, region_to_bbox


CLI_VERSION = "1.0.0"
SCHEMA_VERSION = "1.1"
SCHEMA_RELATIVE_PATHS: dict[str, str] = {
    "parse": "cli/schemas/parse.v1.json",
    "debug": "cli/schemas/debug.v1.json",
    "locate": "cli/schemas/locate.v1.json",
    "match": "cli/schemas/match.v1.json",
    "doctor": "cli/schemas/doctor.v1.json",
    "measure": "cli/schemas/measure.v1.json",
    "crop": "cli/schemas/crop.v1.json",
    "diff": "cli/schemas/diff.v1.json",
    "info": "cli/schemas/info.v1.json",
    "check": "cli/schemas/check.v1.json",
    "overlay": "cli/schemas/overlay.v1.json",
    "baseline": "cli/schemas/baseline.v1.json",
    "engines": "cli/schemas/engines.v1.json",
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

COMMON_RUNTIME_ROOTS = (
    "~/ai/omni-parser/OmniParser",
    "~/ai/caliper-parser/OmniParser",
    "~/OmniParser",
    "~/src/OmniParser",
    "~/projects/OmniParser",
)

COMMON_UIED_ROOTS = (
    "~/ai/UIED",
    "~/UIED",
    "~/src/UIED",
    "~/projects/UIED",
)


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


def _element_fingerprint(
    *,
    element_type: str,
    label: str,
    bbox_ratio: list[float],
) -> str:
    normalized_label = _normalize_text(label)
    rounded_ratio = [round(float(v), 4) for v in bbox_ratio]
    raw = json.dumps(
        {
            "type": str(element_type),
            "label": normalized_label,
            "bbox_ratio": rounded_ratio,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"e_{digest}"


def _ensure_elements_have_ids(parsed_data: dict[str, Any]) -> bool:
    elements = parsed_data.get("elements")
    if not isinstance(elements, list):
        return False

    image_width = int(parsed_data.get("image_width", 0) or 0)
    image_height = int(parsed_data.get("image_height", 0) or 0)
    changed = False

    for element in elements:
        if not isinstance(element, dict):
            continue
        existing = str(element.get("element_id", "")).strip()
        if existing:
            continue

        bbox_ratio = element.get("bbox_ratio")
        if not isinstance(bbox_ratio, list) or len(bbox_ratio) != 4:
            bbox = element.get("bbox", {}) if isinstance(element.get("bbox"), dict) else {}
            x = float(bbox.get("x", 0.0))
            y = float(bbox.get("y", 0.0))
            w = float(bbox.get("width", 0.0))
            h = float(bbox.get("height", 0.0))
            if image_width > 0 and image_height > 0:
                bbox_ratio = [
                    x / image_width,
                    y / image_height,
                    (x + w) / image_width,
                    (y + h) / image_height,
                ]
            else:
                bbox_ratio = [0.0, 0.0, 0.0, 0.0]

        element["element_id"] = _element_fingerprint(
            element_type=str(element.get("element_type", "unknown")),
            label=str(element.get("label", "")),
            bbox_ratio=[float(v) for v in bbox_ratio],
        )
        changed = True

    return changed


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
    return Path.home() / ".cache" / "caliper"


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


def _default_model_dir(runtime_root: Path) -> Path:
    env_override = os.environ.get("CALIPER_MODEL_DIR")
    if env_override:
        return Path(env_override).expanduser().resolve()
    return (runtime_root / "weights").resolve()


def _is_runtime_root(path: Path) -> bool:
    return (path / "util" / "utils.py").is_file()


def _is_uied_root(path: Path) -> bool:
    return (path / "detect_compo" / "ip_region_proposal.py").is_file() and (path / "detect_merge" / "merge.py").is_file()


def _resolve_runtime_root(cli_root: Path, runtime_override: str | None) -> Path:
    if runtime_override:
        candidate = Path(runtime_override).expanduser().resolve()
        if not _is_runtime_root(candidate):
            raise UserInputError(
                "Invalid --runtime-root. Expected a directory containing util/utils.py; "
                f"got: {candidate}"
            )
        return candidate

    env_override = os.environ.get("OMNIPARSER_ROOT") or os.environ.get("CALIPER_RUNTIME_ROOT")
    if env_override:
        candidate = Path(env_override).expanduser().resolve()
        if not _is_runtime_root(candidate):
            raise UserInputError(
                "OMNIPARSER_ROOT/CALIPER_RUNTIME_ROOT is set but invalid. "
                "Expected util/utils.py under that directory; "
                f"got: {candidate}"
            )
        return candidate

    candidates: list[Path] = []

    if _is_runtime_root(cli_root):
        candidates.append(cli_root)

    candidates.extend(
        [
            (cli_root.parent / "OmniParser").resolve(),
            (cli_root / "OmniParser").resolve(),
            (cli_root.parent / "omni-parser" / "OmniParser").resolve(),
            (cli_root.parent / "caliper-parser" / "OmniParser").resolve(),
        ]
    )
    candidates.extend(Path(raw).expanduser().resolve() for raw in COMMON_RUNTIME_ROOTS)

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if _is_runtime_root(candidate):
            return candidate

    return cli_root


def _resolve_uied_root(cli_root: Path, uied_override: str | None) -> Path:
    if uied_override:
        candidate = Path(uied_override).expanduser().resolve()
        if not _is_uied_root(candidate):
            raise UserInputError(
                "Invalid --uied-root. Expected a directory containing detect_compo/ip_region_proposal.py; "
                f"got: {candidate}"
            )
        return candidate

    env_override = os.environ.get("UIED_ROOT")
    if env_override:
        candidate = Path(env_override).expanduser().resolve()
        if not _is_uied_root(candidate):
            raise UserInputError(
                "UIED_ROOT is set but invalid. Expected detect_compo/ip_region_proposal.py under that directory; "
                f"got: {candidate}"
            )
        return candidate

    candidates: list[Path] = []
    candidates.extend(
        [
            (cli_root.parent / "UIED").resolve(),
            (cli_root / "UIED").resolve(),
            (Path.home() / "ai" / "UIED").resolve(),
        ]
    )
    candidates.extend(Path(raw).expanduser().resolve() for raw in COMMON_UIED_ROOTS)

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if _is_uied_root(candidate):
            return candidate

    raise UserInputError(
        "Unable to locate UIED root. Install UIED and set UIED_ROOT or pass --uied-root <path>."
    )


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


def _default_install_env_path() -> Path:
    return Path(os.environ.get("CALIPER_INSTALL_ENV", "~/.config/caliper/install.env")).expanduser().resolve()


def _parse_install_env_file(path: Path) -> dict[str, str]:
    if not path.exists() or not path.is_file():
        return {}
    parsed: dict[str, str] = {}
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, raw_value = line.split("=", 1)
            key = key.strip()
            value = raw_value.strip()
            if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
                value = value[1:-1]
            parsed[key] = value
    except Exception:
        return {}
    return parsed


def _guess_command_from_argv(raw_argv: list[str]) -> str:
    for token in raw_argv:
        if token in SCHEMA_RELATIVE_PATHS:
            return token
    return "global"


def _error_hint(exc: Exception) -> str | None:
    if isinstance(exc, FileMissingError):
        return "Verify the file path is absolute or relative to the current directory and that it exists."
    if isinstance(exc, ModelNotFoundError):
        return "Download model weights into the OmniParser weights directory or set CALIPER_MODEL_DIR."
    if isinstance(exc, OmniConfigError):
        return "Fix .caliper.json validation errors, or pass --config <path> to a valid config file."
    if isinstance(exc, ResolutionError):
        return "Use coordinates (x,y), element:<index>, id:<element_id>, fuzzy label + hints, region:<name>, or target:<name> with a loaded config."
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
        engine: str,
        uied_root: Path | None,
        uied_text_engine: str,
    ) -> None:
        self.repo_root = repo_root
        self.model_dir = model_dir
        self.requested_device = requested_device
        self.logger = logger
        self.omniparser_version = omniparser_version
        self.engine = engine
        self.uied_root = uied_root
        self.uied_text_engine = uied_text_engine

        self._engine_impl = get_engine(self.engine)
        self.effective_device = str(requested_device)

    def _captured_call(self, fn: Any, *args: Any, **kwargs: Any) -> tuple[Any, str]:
        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()
        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
            result = fn(*args, **kwargs)
        logs = stdout_buffer.getvalue() + stderr_buffer.getvalue()
        return result, logs

    def _build_structured_elements(
        self,
        *,
        detected: list[DetectedElement],
        image_width: int,
        image_height: int,
    ) -> list[dict[str, Any]]:
        structured: list[dict[str, Any]] = []
        for idx, element in enumerate(detected):
            raw = element.raw if isinstance(element.raw, dict) else {}
            bbox_ratio = raw.get("bbox_ratio")
            if not isinstance(bbox_ratio, list) or len(bbox_ratio) != 4:
                bbox_ratio = [
                    float(element.bbox.x) / max(1, image_width),
                    float(element.bbox.y) / max(1, image_height),
                    float(element.bbox.x + element.bbox.w) / max(1, image_width),
                    float(element.bbox.y + element.bbox.h) / max(1, image_height),
                ]

            structured.append(
                {
                    "index": idx,
                    "element_id": str(element.element_id),
                    "bbox": {
                        "x": int(element.bbox.x),
                        "y": int(element.bbox.y),
                        "width": int(element.bbox.w),
                        "height": int(element.bbox.h),
                    },
                    "label": str(element.label),
                    "element_type": str(element.element_type),
                    "confidence": max(0.0, min(1.0, round(float(element.confidence), 6))),
                    "interactable": bool(raw.get("interactable", str(element.element_type) not in {"text", "region"})),
                    "source": str(raw.get("source", element.source_engine)),
                    "bbox_ratio": [round(float(v), 8) for v in bbox_ratio],
                }
            )
        return structured

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
            "engine": self.engine,
            "omniparser_version": self.omniparser_version,
            "model_dir": str(self.model_dir),
            "requested_device": self.requested_device,
            "uied_root": str(self.uied_root) if self.uied_root else None,
            "uied_text_engine": self.uied_text_engine,
        }
        cache_key = hashlib.sha256(
            json.dumps(cache_payload_key, sort_keys=True).encode("utf-8")
        ).hexdigest()
        cache_path = cache_dir / f"{cache_key}.json"

        if use_cache and cache_path.exists():
            with cache_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if _ensure_elements_have_ids(data):
                with cache_path.open("w", encoding="utf-8") as handle:
                    json.dump(data, handle)
            return data, True, ""

        os.environ["OMNIPARSER_ROOT"] = str(self.repo_root)
        if self.uied_root:
            os.environ["UIED_ROOT"] = str(self.uied_root)
        os.environ["CALIPER_UIED_TEXT_ENGINE"] = str(self.uied_text_engine)

        _, load_logs = self._captured_call(
            self._engine_impl.load,
            model_dir=str(self.model_dir),
            device=str(self.requested_device),
        )
        detected, detect_logs = self._captured_call(
            self._engine_impl.detect,
            str(image_path),
        )
        if not isinstance(detected, list):
            raise ProcessingError("Detection engine returned invalid payload.")

        artifacts = getattr(self._engine_impl, "_last_artifacts", {})
        if not isinstance(artifacts, dict):
            artifacts = {}

        image_width = int(artifacts.get("image_width", 0) or 0)
        image_height = int(artifacts.get("image_height", 0) or 0)
        if image_width <= 0 or image_height <= 0:
            image = _load_image_validated(image_path).convert("RGB")
            image_width, image_height = image.size

        self.effective_device = str(artifacts.get("effective_device", self.requested_device))

        parsed_data = {
            "image_path": str(image_path),
            "image_width": image_width,
            "image_height": image_height,
            "elements": self._build_structured_elements(
                detected=detected,
                image_width=image_width,
                image_height=image_height,
            ),
            "annotated_image_base64": artifacts.get("annotated_image_base64", ""),
            "raw_parsed_content_list": artifacts.get("raw_parsed_content_list", []),
            "raw_label_coordinates_ratio_xywh": artifacts.get("raw_label_coordinates_ratio_xywh", []),
            "raw_ocr": artifacts.get("raw_ocr", {"texts": [], "bboxes_xyxy_pixel": [], "scores": []}),
            "box_threshold": box_threshold,
            "effective_device": self.effective_device,
        }
        if "raw_uied" in artifacts:
            parsed_data["raw_uied"] = artifacts["raw_uied"]

        _ensure_elements_have_ids(parsed_data)
        logs = load_logs + detect_logs

        if use_cache:
            with cache_path.open("w", encoding="utf-8") as handle:
                json.dump(parsed_data, handle)

        return parsed_data, False, logs


def _save_annotated_image(annotated_b64: str, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(base64.b64decode(annotated_b64))
    return output_path.resolve()


def _save_locate_debug_image(
    *,
    image_path: Path,
    candidates: list[dict[str, Any]],
    output_path: Path,
    top_k: int,
) -> Path:
    image = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    for rank, candidate in enumerate(candidates[: max(1, int(top_k))], start=1):
        bbox = candidate["bbox"]
        x1 = int(bbox["x"])
        y1 = int(bbox["y"])
        x2 = x1 + int(bbox["width"])
        y2 = y1 + int(bbox["height"])

        if rank == 1:
            color = (30, 175, 60)
        elif rank == 2:
            color = (200, 140, 20)
        else:
            color = (190, 40, 40)

        draw.rectangle((x1, y1, x2, y2), outline=color, width=3)
        text_label = str(candidate.get("label") or "").replace("\n", " ").strip()
        if len(text_label) > 42:
            text_label = text_label[:39] + "..."
        label = (
            f"#{rank} idx={candidate['index']} score={candidate['score']:.3f} "
            f"[{candidate.get('element_type')}] {text_label}"
        )
        draw.text((x1 + 2, max(0, y1 - 12)), label, fill=color, font=font)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)
    return output_path.resolve()


def _save_label_debug_image(
    *,
    image_path: Path,
    elements: list[dict[str, Any]],
    output_path: Path,
    max_elements: int,
    inferred_regions: list[dict[str, Any]] | None = None,
) -> Path:
    image = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    for region in inferred_regions or []:
        bbox = region.get("bbox", {})
        x1 = int(bbox.get("x", 0))
        y1 = int(bbox.get("y", 0))
        x2 = x1 + int(bbox.get("width", 0))
        y2 = y1 + int(bbox.get("height", 0))
        color = (255, 210, 0)
        draw.rectangle((x1, y1, x2, y2), outline=color, width=2)
        label = str(region.get("name", "layout")).strip()
        confidence = float(region.get("confidence", 0.0))
        draw.text((x1 + 2, max(0, y1 + 2)), f"[layout] {label} c={confidence:.3f}", fill=color, font=font)

    for element in elements[: max(1, int(max_elements))]:
        bbox = element["bbox"]
        x1 = int(bbox["x"])
        y1 = int(bbox["y"])
        x2 = x1 + int(bbox["width"])
        y2 = y1 + int(bbox["height"])
        color = (35, 110, 215)
        draw.rectangle((x1, y1, x2, y2), outline=color, width=2)

        label = str(element.get("label", "")).replace("\n", " ").strip()
        if len(label) > 48:
            label = label[:45] + "..."
        caption = (
            f"#{element['index']} [{element.get('element_type', 'unknown')}] "
            f"c={float(element.get('confidence', 0.0)):.3f} {label}"
        )
        draw.text((x1 + 2, max(0, y1 - 12)), caption, fill=color, font=font)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)
    return output_path.resolve()


def _normalized_center(bbox: dict[str, int], image_width: int, image_height: int) -> tuple[float, float]:
    cx, cy = _bbox_point_for_edge(bbox, edge="center")
    width = max(1, int(image_width))
    height = max(1, int(image_height))
    return float(cx) / width, float(cy) / height


def _size_similarity(a_bbox: dict[str, int], b_bbox: dict[str, int]) -> float:
    aw = max(1.0, float(a_bbox["width"]))
    ah = max(1.0, float(a_bbox["height"]))
    bw = max(1.0, float(b_bbox["width"]))
    bh = max(1.0, float(b_bbox["height"]))
    width_ratio = min(aw, bw) / max(aw, bw)
    height_ratio = min(ah, bh) / max(ah, bh)
    return max(0.0, min(1.0, (width_ratio + height_ratio) / 2.0))


def _ambiguity_summary(
    *,
    ranked: list[dict[str, Any]],
    score_key: str,
    ambiguity_gap_threshold: float = 0.08,
) -> dict[str, Any]:
    if not ranked:
        return {
            "ambiguous": True,
            "top_score": None,
            "second_score": None,
            "top2_gap": None,
            "reason": "no_candidates",
        }

    top = float(ranked[0].get(score_key, 0.0))
    second = float(ranked[1].get(score_key, 0.0)) if len(ranked) > 1 else 0.0
    gap = top - second if len(ranked) > 1 else top
    ambiguous = len(ranked) > 1 and gap < float(ambiguity_gap_threshold)
    return {
        "ambiguous": bool(ambiguous),
        "top_score": top,
        "second_score": second if len(ranked) > 1 else None,
        "top2_gap": gap if len(ranked) > 1 else None,
        "reason": "close_scores" if ambiguous else "clear_winner",
    }


def _match_debug_visual(
    *,
    image1_path: Path,
    image2_path: Path,
    source: dict[str, Any],
    candidates: list[dict[str, Any]],
    output_path: Path,
    top_k: int,
) -> Path:
    image1 = Image.open(image1_path).convert("RGB")
    image2 = Image.open(image2_path).convert("RGB")
    gap = 24
    canvas_width = image1.width + gap + image2.width
    canvas_height = max(image1.height, image2.height)
    canvas = Image.new("RGB", (canvas_width, canvas_height), (255, 255, 255))
    canvas.paste(image1, (0, 0))
    canvas.paste(image2, (image1.width + gap, 0))

    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()

    source_bbox = source["bbox"]
    sx1 = int(source_bbox["x"])
    sy1 = int(source_bbox["y"])
    sx2 = sx1 + int(source_bbox["width"])
    sy2 = sy1 + int(source_bbox["height"])
    draw.rectangle((sx1, sy1, sx2, sy2), outline=(30, 150, 40), width=3)
    s_label = str(source.get("label", "")).replace("\n", " ").strip()
    if len(s_label) > 40:
        s_label = s_label[:37] + "..."
    draw.text((sx1 + 2, max(0, sy1 - 12)), f"SOURCE idx={source['index']} {s_label}", fill=(30, 150, 40), font=font)

    right_offset = image1.width + gap
    for rank, candidate in enumerate(candidates[: max(1, int(top_k))], start=1):
        bbox = candidate["bbox"]
        x1 = int(bbox["x"]) + right_offset
        y1 = int(bbox["y"])
        x2 = x1 + int(bbox["width"])
        y2 = y1 + int(bbox["height"])
        color = (30, 175, 60) if rank == 1 else (185, 65, 45)
        draw.rectangle((x1, y1, x2, y2), outline=color, width=3)
        label = str(candidate.get("label", "")).replace("\n", " ").strip()
        if len(label) > 36:
            label = label[:33] + "..."
        caption = f"#{rank} idx={candidate['index']} s={candidate['match_score']:.3f} {label}"
        draw.text((x1 + 2, max(0, y1 - 12)), caption, fill=color, font=font)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path)
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
        targets=project_config.targets if project_config else None,
    )


def _print_parse_table(elements: list[dict[str, Any]]) -> None:
    headers = ["idx", "id", "type", "conf", "x", "y", "w", "h", "label"]
    rows = []
    for element in elements:
        rows.append(
            [
                str(element["index"]),
                str(element.get("element_id", "")),
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
    writer.writerow(["index", "element_id", "element_type", "confidence", "x", "y", "width", "height", "label"])
    for element in elements:
        writer.writerow(
            [
                element["index"],
                element.get("element_id", ""),
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


def _slugify_token(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "item"


def _bbox_bounds(bbox: dict[str, Any]) -> tuple[int, int, int, int]:
    x = int(bbox.get("x", 0))
    y = int(bbox.get("y", 0))
    width = int(bbox.get("width", 0))
    height = int(bbox.get("height", 0))
    return x, y, x + width, y + height


def _ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return min(a_end, b_end) > max(a_start, b_start)


def _pick_horizontal_neighbor(
    *,
    source_idx: int,
    elements: list[dict[str, Any]],
) -> tuple[int, int] | None:
    src_bbox = elements[source_idx]["bbox"]
    src_left, src_top, src_right, src_bottom = _bbox_bounds(src_bbox)
    src_center_y = (src_top + src_bottom) / 2.0

    best_idx: int | None = None
    best_key: tuple[int, float, int] | None = None

    for candidate_idx, candidate in enumerate(elements):
        if candidate_idx == source_idx:
            continue
        cand_left, cand_top, _cand_right, cand_bottom = _bbox_bounds(candidate["bbox"])
        if cand_left < src_right:
            continue
        if not _ranges_overlap(src_top, src_bottom, cand_top, cand_bottom):
            continue

        gap = cand_left - src_right
        cand_center_y = (cand_top + cand_bottom) / 2.0
        key = (gap, abs(cand_center_y - src_center_y), cand_left)
        if best_key is None or key < best_key:
            best_key = key
            best_idx = candidate_idx

    if best_idx is None or best_key is None:
        return None
    return best_idx, int(best_key[0])


def _pick_vertical_neighbor(
    *,
    source_idx: int,
    elements: list[dict[str, Any]],
) -> tuple[int, int] | None:
    src_bbox = elements[source_idx]["bbox"]
    src_left, src_top, src_right, src_bottom = _bbox_bounds(src_bbox)
    src_center_x = (src_left + src_right) / 2.0

    best_idx: int | None = None
    best_key: tuple[int, float, int] | None = None

    for candidate_idx, candidate in enumerate(elements):
        if candidate_idx == source_idx:
            continue
        cand_left, cand_top, cand_right, _cand_bottom = _bbox_bounds(candidate["bbox"])
        if cand_top < src_bottom:
            continue
        if not _ranges_overlap(src_left, src_right, cand_left, cand_right):
            continue

        gap = cand_top - src_bottom
        cand_center_x = (cand_left + cand_right) / 2.0
        key = (gap, abs(cand_center_x - src_center_x), cand_top)
        if best_key is None or key < best_key:
            best_key = key
            best_idx = candidate_idx

    if best_idx is None or best_key is None:
        return None
    return best_idx, int(best_key[0])


def _build_baseline_config(
    *,
    image_path: Path,
    image_width: int,
    image_height: int,
    elements: list[dict[str, Any]],
    project_name: str | None,
    tolerance: int,
) -> tuple[dict[str, Any], int, int]:
    regions: dict[str, dict[str, Any]] = {}
    region_name_by_index: dict[int, str] = {}
    used_region_names: set[str] = set()

    for idx, element in enumerate(elements):
        bbox = element["bbox"]
        base_name = str(element.get("element_id") or f"element-{idx}")
        region_name = _slugify_token(base_name)
        suffix = 2
        while region_name in used_region_names:
            region_name = f"{_slugify_token(base_name)}-{suffix}"
            suffix += 1
        used_region_names.add(region_name)

        regions[region_name] = {
            "x": int(bbox["x"]),
            "y": int(bbox["y"]),
            "w": int(bbox["width"]),
            "h": int(bbox["height"]),
            "description": str(element.get("label", "")).strip() or None,
        }
        region_name_by_index[idx] = region_name

    assertions: list[dict[str, Any]] = []
    used_assertion_ids: set[str] = set()

    def add_assertion(assertion: dict[str, Any]) -> None:
        base_id = _slugify_token(str(assertion.get("id", "assertion")))
        assertion_id = base_id
        suffix = 2
        while assertion_id in used_assertion_ids:
            assertion_id = f"{base_id}-{suffix}"
            suffix += 1
        assertion["id"] = assertion_id
        used_assertion_ids.add(assertion_id)
        assertions.append(assertion)

    for idx, element in enumerate(elements):
        region_name = region_name_by_index[idx]
        bbox = element["bbox"]
        add_assertion(
            {
                "id": f"region-{region_name}-width",
                "type": "region_dimension",
                "region": region_name,
                "property": "width",
                "expected": int(bbox["width"]),
                "tolerance": int(tolerance),
                "description": f"{region_name} width baseline",
            }
        )
        add_assertion(
            {
                "id": f"region-{region_name}-height",
                "type": "region_dimension",
                "region": region_name,
                "property": "height",
                "expected": int(bbox["height"]),
                "tolerance": int(tolerance),
                "description": f"{region_name} height baseline",
            }
        )

    horizontal_pairs: set[tuple[int, int]] = set()
    vertical_pairs: set[tuple[int, int]] = set()

    for idx in range(len(elements)):
        horizontal_neighbor = _pick_horizontal_neighbor(source_idx=idx, elements=elements)
        if horizontal_neighbor is not None:
            horizontal_pairs.add((idx, horizontal_neighbor[0]))
        vertical_neighbor = _pick_vertical_neighbor(source_idx=idx, elements=elements)
        if vertical_neighbor is not None:
            vertical_pairs.add((idx, vertical_neighbor[0]))

    for from_idx, to_idx in sorted(horizontal_pairs):
        from_region = region_name_by_index[from_idx]
        to_region = region_name_by_index[to_idx]
        from_bbox = elements[from_idx]["bbox"]
        to_bbox = elements[to_idx]["bbox"]
        expected_gap = max(0, int(to_bbox["x"]) - (int(from_bbox["x"]) + int(from_bbox["width"])))

        add_assertion(
            {
                "id": f"gap-x-{from_region}-to-{to_region}",
                "type": "measurement",
                "from": {"ref": f"region:{from_region}", "edge": "right"},
                "to": {"ref": f"region:{to_region}", "edge": "left"},
                "axis": "x",
                "expected": expected_gap,
                "tolerance": int(tolerance),
                "description": f"horizontal gap from {from_region} to {to_region}",
            }
        )

    for from_idx, to_idx in sorted(vertical_pairs):
        from_region = region_name_by_index[from_idx]
        to_region = region_name_by_index[to_idx]
        from_bbox = elements[from_idx]["bbox"]
        to_bbox = elements[to_idx]["bbox"]
        expected_gap = max(0, int(to_bbox["y"]) - (int(from_bbox["y"]) + int(from_bbox["height"])))

        add_assertion(
            {
                "id": f"gap-y-{from_region}-to-{to_region}",
                "type": "measurement",
                "from": {"ref": f"region:{from_region}", "edge": "bottom"},
                "to": {"ref": f"region:{to_region}", "edge": "top"},
                "axis": "y",
                "expected": expected_gap,
                "tolerance": int(tolerance),
                "description": f"vertical gap from {from_region} to {to_region}",
            }
        )

    config_payload: dict[str, Any] = {
        "version": 1,
        "project_name": project_name or image_path.stem,
        "reference_image": str(image_path.resolve()),
        "viewport": {
            "width": int(image_width),
            "height": int(image_height),
            "device_pixel_ratio": 1,
        },
        "regions": regions,
        "targets": {},
        "assertions": assertions,
    }

    return config_payload, len(horizontal_pairs), len(vertical_pairs)


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


def _cmd_doctor(
    args: argparse.Namespace,
    logger: Console,
    response_context: ResponseContext,
    *,
    cli_root: Path,
) -> int:
    start_ms = _perf_ms()

    def add_check(
        *,
        check_id: str,
        status: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        checks.append(
            {
                "id": check_id,
                "status": status,
                "message": message,
                "details": details or {},
            }
        )

    checks: list[dict[str, Any]] = []
    install_env_path = _default_install_env_path()
    install_env_values = _parse_install_env_file(install_env_path)

    if install_env_path.exists():
        add_check(
            check_id="install-env-file",
            status="pass",
            message="Persistent install env file found.",
            details={"path": str(install_env_path)},
        )
    else:
        add_check(
            check_id="install-env-file",
            status="warn",
            message="Persistent install env file is missing.",
            details={
                "path": str(install_env_path),
                "hint": "Run `caliper setup --cli-root <path> --runtime-root <path>` once.",
            },
        )

    cli_entry = (cli_root / "cli" / "caliper.py").resolve()
    if cli_entry.exists():
        add_check(
            check_id="cli-root",
            status="pass",
            message="CLI root is valid.",
            details={"cli_root": str(cli_root), "cli_entry": str(cli_entry)},
        )
    else:
        add_check(
            check_id="cli-root",
            status="fail",
            message="CLI root is invalid (missing cli/caliper.py).",
            details={"cli_root": str(cli_root), "cli_entry": str(cli_entry)},
        )

    runtime_root: Path | None = None
    runtime_resolution_error: str | None = None
    try:
        runtime_root = _resolve_runtime_root(cli_root, runtime_override=getattr(args, "runtime_root", None))
    except Exception as exc:
        runtime_resolution_error = str(exc)

    if runtime_root is not None and _is_runtime_root(runtime_root):
        add_check(
            check_id="runtime-root",
            status="pass",
            message="OmniParser runtime root is valid.",
            details={
                "runtime_root": str(runtime_root),
                "source": "--runtime-root"
                if getattr(args, "runtime_root", None)
                else (
                    "env"
                    if (os.environ.get("OMNIPARSER_ROOT") or os.environ.get("CALIPER_RUNTIME_ROOT"))
                    else "auto-discovery"
                ),
            },
        )

    uied_root: Path | None = None
    uied_resolution_error: str | None = None
    try:
        uied_root = _resolve_uied_root(cli_root, uied_override=getattr(args, "uied_root", None))
    except Exception as exc:
        uied_resolution_error = str(exc)

    if uied_root is not None and _is_uied_root(uied_root):
        add_check(
            check_id="uied-root",
            status="pass",
            message="UIED root is valid.",
            details={
                "uied_root": str(uied_root),
                "source": "--uied-root" if getattr(args, "uied_root", None) else (
                    "env" if os.environ.get("UIED_ROOT") else "auto-discovery"
                ),
            },
        )
    else:
        add_check(
            check_id="uied-root",
            status="fail",
            message="UIED root is invalid or not discoverable.",
            details={
                "uied_root": str(uied_root) if uied_root else None,
                "error": uied_resolution_error,
                "hint": "Install UIED and set UIED_ROOT or pass --uied-root.",
            },
        )

    uied_import_ok = False
    if uied_root is not None and _is_uied_root(uied_root):
        inserted = False
        try:
            if str(uied_root) not in sys.path:
                sys.path.insert(0, str(uied_root))
                inserted = True
            import time as time_module

            if not hasattr(time_module, "clock"):
                setattr(time_module, "clock", time_module.perf_counter)
            existing_config_module = sys.modules.get("config")
            removed_config_paths: list[str] = []
            if existing_config_module is not None and not hasattr(existing_config_module, "__path__"):
                sys.modules.setdefault("caliper_cli_config", existing_config_module)
                config_file = getattr(existing_config_module, "__file__", None)
                if config_file:
                    config_dir = str(Path(config_file).resolve().parent)
                    removed_config_paths = [entry for entry in sys.path if entry == config_dir]
                    sys.path[:] = [entry for entry in sys.path if entry != config_dir]
                del sys.modules["config"]
            importlib.import_module("detect_compo.ip_region_proposal")
            importlib.import_module("detect_merge.merge")
            importlib.import_module("detect_text.text_detection")
            uied_import_ok = True
            add_check(
                check_id="uied-imports",
                status="pass",
                message="UIED module imports succeeded.",
                details={"uied_root": str(uied_root)},
            )
        except Exception as exc:
            add_check(
                check_id="uied-imports",
                status="fail",
                message="UIED module imports failed.",
                details={"uied_root": str(uied_root), "error": str(exc)},
            )
        finally:
            if inserted:
                try:
                    sys.path.remove(str(uied_root))
                except ValueError:
                    pass
            for entry in reversed(removed_config_paths):
                sys.path.insert(0, entry)
    else:
        add_check(
            check_id="runtime-root",
            status="fail",
            message="OmniParser runtime root is invalid or not discoverable.",
            details={
                "runtime_root": str(runtime_root) if runtime_root else None,
                "error": runtime_resolution_error,
                "hint": "Set OMNIPARSER_ROOT/CALIPER_RUNTIME_ROOT or pass --runtime-root.",
            },
        )

    engine_statuses = list_engine_status()
    for engine_status in engine_statuses:
        engine_name = str(engine_status.get("name", "unknown"))
        available = bool(engine_status.get("available"))
        reason = engine_status.get("reason")
        add_check(
            check_id=f"engine:{engine_name}",
            status="pass" if available else "fail",
            message=(
                f"Detection engine '{engine_name}' is available."
                if available
                else f"Detection engine '{engine_name}' is unavailable."
            ),
            details={
                "display_name": engine_status.get("display_name"),
                "available": available,
                "reason": reason,
            },
        )

    python_path = Path(sys.executable).resolve()
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    add_check(
        check_id="python",
        status="pass" if python_path.exists() else "fail",
        message="Python interpreter detected." if python_path.exists() else "Python interpreter path is invalid.",
        details={"python": str(python_path), "version": python_version},
    )

    import_checks = [
        "PIL",
        "torch",
        "torchvision",
        "ultralytics",
        "transformers",
        "easyocr",
        "paddleocr",
    ]
    import_failures = 0
    for module_name in import_checks:
        try:
            module = importlib.import_module(module_name)
            version = getattr(module, "__version__", None)
            add_check(
                check_id=f"import:{module_name}",
                status="pass",
                message=f"Module '{module_name}' import succeeded.",
                details={"version": str(version) if version is not None else None},
            )
        except Exception as exc:
            import_failures += 1
            add_check(
                check_id=f"import:{module_name}",
                status="fail",
                message=f"Module '{module_name}' import failed.",
                details={"error": str(exc)},
            )

    inferred_runtime_root = runtime_root or cli_root
    model_dir = Path(args.model_dir).expanduser().resolve() if args.model_dir else _default_model_dir(inferred_runtime_root)
    som_pt = model_dir / "icon_detect" / "model.pt"
    som_onnx = model_dir / "icon_detect" / "model.onnx"
    caption_dir = model_dir / "icon_caption_florence"

    model_ok = (som_pt.exists() or som_onnx.exists()) and caption_dir.exists()
    add_check(
        check_id="model-files",
        status="pass" if model_ok else "fail",
        message="Model files are present." if model_ok else "Model files are missing.",
        details={
            "model_dir": str(model_dir),
            "icon_detect_model_pt": str(som_pt),
            "icon_detect_model_onnx": str(som_onnx),
            "icon_caption_dir": str(caption_dir),
            "icon_detect_exists": bool(som_pt.exists() or som_onnx.exists()),
            "caption_dir_exists": bool(caption_dir.exists()),
        },
    )

    parse_smoke: dict[str, Any] | None = None
    parse_smoke_uied: dict[str, Any] | None = None
    if getattr(args, "image", None):
        try:
            image_path = _resolve_image_path(args.image)
        except Exception as exc:
            add_check(
                check_id="parse-smoke",
                status="fail",
                message="Parse smoke check image is invalid.",
                details={"image": str(args.image), "error": str(exc)},
            )
        else:
            can_run_parse = runtime_root is not None and _is_runtime_root(runtime_root) and import_failures == 0 and model_ok
            if not can_run_parse:
                add_check(
                    check_id="parse-smoke",
                    status="warn",
                    message="Parse smoke check skipped due to prerequisite failures.",
                    details={"image": str(image_path)},
                )
            else:
                try:
                    smoke_start = _perf_ms()
                    smoke_cmd = [
                        sys.executable,
                        str((cli_root / "cli" / "caliper.py").resolve()),
                        "parse",
                        str(image_path),
                        "--engine",
                        "omniparser",
                        "--runtime-root",
                        str(runtime_root),
                        "--model-dir",
                        str(model_dir),
                        "--confidence-threshold",
                        str(args.confidence_threshold),
                        "--quiet",
                        "--cache" if args.cache else "--no-cache",
                    ]
                    smoke_proc = subprocess.run(
                        smoke_cmd,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    parsed_data = json.loads(smoke_proc.stdout.strip().splitlines()[-1]) if smoke_proc.stdout.strip() else {}
                    if smoke_proc.returncode != 0 or parsed_data.get("status") != "success":
                        raise ProcessingError(
                            f"omniparser parse smoke command failed with code {smoke_proc.returncode}: {smoke_proc.stdout.strip()}"
                        )
                    smoke_meta = parsed_data.get("meta", {})
                    parse_smoke = {
                        "image_path": str(image_path),
                        "element_count": len(parsed_data.get("elements", [])),
                        "image_width": int(parsed_data.get("image_width", 0)),
                        "image_height": int(parsed_data.get("image_height", 0)),
                        "cache_hit": bool(smoke_meta.get("cache_hit", False)),
                        "processing_time_ms": int(round(_perf_ms() - smoke_start)),
                    }
                    add_check(
                        check_id="parse-smoke",
                        status="pass",
                        message="Parse smoke check succeeded.",
                        details=parse_smoke,
                    )
                except Exception as exc:
                    add_check(
                        check_id="parse-smoke",
                        status="fail",
                        message="Parse smoke check failed.",
                        details={"image": str(image_path), "error": str(exc)},
                    )

            can_run_uied_parse = uied_root is not None and _is_uied_root(uied_root) and uied_import_ok
            if not can_run_uied_parse:
                add_check(
                    check_id="parse-smoke-uied",
                    status="warn",
                    message="UIED parse smoke check skipped due to prerequisite failures.",
                    details={"image": str(image_path)},
                )
            else:
                try:
                    smoke_start_uied = _perf_ms()
                    smoke_cmd_uied = [
                        sys.executable,
                        str((cli_root / "cli" / "caliper.py").resolve()),
                        "parse",
                        str(image_path),
                        "--engine",
                        "uied",
                        "--uied-root",
                        str(uied_root),
                        "--uied-text-engine",
                        "paddle",
                        "--confidence-threshold",
                        str(args.confidence_threshold),
                        "--quiet",
                        "--cache" if args.cache else "--no-cache",
                    ]
                    smoke_proc_uied = subprocess.run(
                        smoke_cmd_uied,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    parsed_uied = (
                        json.loads(smoke_proc_uied.stdout.strip().splitlines()[-1])
                        if smoke_proc_uied.stdout.strip()
                        else {}
                    )
                    if smoke_proc_uied.returncode != 0 or parsed_uied.get("status") != "success":
                        raise ProcessingError(
                            f"uied parse smoke command failed with code {smoke_proc_uied.returncode}: {smoke_proc_uied.stdout.strip()}"
                        )
                    smoke_meta_uied = parsed_uied.get("meta", {})
                    parse_smoke_uied = {
                        "image_path": str(image_path),
                        "element_count": len(parsed_uied.get("elements", [])),
                        "image_width": int(parsed_uied.get("image_width", 0)),
                        "image_height": int(parsed_uied.get("image_height", 0)),
                        "cache_hit": bool(smoke_meta_uied.get("cache_hit", False)),
                        "processing_time_ms": int(round(_perf_ms() - smoke_start_uied)),
                    }
                    add_check(
                        check_id="parse-smoke-uied",
                        status="pass",
                        message="UIED parse smoke check succeeded.",
                        details=parse_smoke_uied,
                    )
                except Exception as exc:
                    add_check(
                        check_id="parse-smoke-uied",
                        status="fail",
                        message="UIED parse smoke check failed.",
                        details={"image": str(image_path), "error": str(exc)},
                    )

    failed = [item for item in checks if item.get("status") == "fail"]
    warned = [item for item in checks if item.get("status") == "warn"]
    passed = [item for item in checks if item.get("status") == "pass"]

    result = "pass" if not failed else "fail"
    recommendations: list[str] = []
    if failed:
        recommendations.append("Run `caliper setup --cli-root <path> --runtime-root <path>` to persist install paths.")
    if any(item.get("id") == "runtime-root" and item.get("status") == "fail" for item in checks):
        recommendations.append("Set `OMNIPARSER_ROOT` or pass `--runtime-root` to commands and re-run `caliper doctor`.")
    if any(item.get("id") == "uied-root" and item.get("status") == "fail" for item in checks):
        recommendations.append("Set `UIED_ROOT` or pass `--uied-root` to commands and re-run `caliper doctor`.")
    if any(item.get("id") == "model-files" and item.get("status") == "fail" for item in checks):
        recommendations.append("Set `CALIPER_MODEL_DIR` to the correct weights folder or download OmniParser-v2.0 weights.")

    runtime_for_meta = runtime_root if runtime_root is not None else cli_root
    processing_time_ms = int(round(_perf_ms() - start_ms))
    payload = {
        "status": "success",
        "error": None,
        "warnings": [item["message"] for item in warned],
        "meta": {
            "command": response_context.command,
            "request_id": response_context.request_id,
            "timestamp_utc": response_context.timestamp_utc,
            "processing_time_ms": processing_time_ms,
            "omniparser_version": _omniparser_version(runtime_for_meta),
            "cli_version": CLI_VERSION,
            "cli_root": str(cli_root),
            "runtime_root": str(runtime_root) if runtime_root else None,
            "install_env_path": str(install_env_path),
            "config_path": str(Path(args.config).expanduser().resolve()) if getattr(args, "config", None) else None,
        },
        "doctor": {
            "result": result,
            "summary": {
                "passed": len(passed),
                "warned": len(warned),
                "failed": len(failed),
                "total": len(checks),
            },
            "checks": checks,
            "install_env": {
                "path": str(install_env_path),
                "values": install_env_values,
            },
            "engines": engine_statuses,
            "parse_smoke": parse_smoke,
            "parse_smoke_uied": parse_smoke_uied,
            "recommendations": recommendations,
        },
    }
    _write_json_stdout(payload, response_context=response_context)
    return 0 if result == "pass" else 2


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
                    "engine": runtime.engine,
                    "uied_root": str(runtime.uied_root) if runtime.uied_root else None,
                    "uied_text_engine": runtime.uied_text_engine if runtime.engine == "uied" else None,
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
                    "element_id": element.get("element_id"),
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
            if "raw_uied" in parsed_data:
                payload["raw"]["uied"] = parsed_data["raw_uied"]
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


def _cmd_engines(
    args: argparse.Namespace,
    response_context: ResponseContext,
    *,
    omniparser_version: str,
) -> int:
    del args  # currently unused; reserved for future output modes
    start_ms = _perf_ms()
    engines = list_engine_status()
    payload = {
        "status": "success",
        "error": None,
        "warnings": [],
        "meta": {
            "command": response_context.command,
            "request_id": response_context.request_id,
            "timestamp_utc": response_context.timestamp_utc,
            "processing_time_ms": int(round(_perf_ms() - start_ms)),
            "omniparser_version": omniparser_version,
            "cli_version": CLI_VERSION,
        },
        "engines": engines,
    }
    _write_json_stdout(payload, response_context=response_context)
    return 0


def _cmd_locate(
    args: argparse.Namespace,
    runtime: OmniRuntime,
    logger: Console,
    project_config: ProjectConfig | None,
    response_context: ResponseContext,
    config_sha256: str | None,
) -> int:
    start_ms = _perf_ms()
    if int(args.top_k) <= 0:
        raise UserInputError("--top-k must be >= 1.")
    image_path = _resolve_image_path(args.image)
    image_sha256 = _sha256_file(image_path)

    parsed_data, cache_hit, logs = runtime.parse_image(
        image_path=image_path,
        box_threshold=args.confidence_threshold,
        use_cache=args.cache,
    )
    if args.verbose and logs:
        logger.debug(logs.rstrip())

    elements = list(parsed_data["elements"])
    ranked_raw = rank_reference_candidates(
        spec=args.query,
        elements=elements,
        image_width=int(parsed_data["image_width"]),
        image_height=int(parsed_data["image_height"]),
        role="--query",
        targets=project_config.targets if project_config else None,
    )

    resolved = _resolve_element_by_spec(
        spec=args.query,
        elements=elements,
        image_width=int(parsed_data["image_width"]),
        image_height=int(parsed_data["image_height"]),
        edge=args.edge,
        role="--query",
        project_config=project_config,
    )

    candidates: list[dict[str, Any]] = []
    for candidate in ranked_raw[: max(1, int(args.top_k))]:
        element = candidate["element"]
        candidates.append(
            {
                "index": int(element["index"]),
                "element_id": str(element.get("element_id", "")),
                "label": str(element.get("label", "")),
                "element_type": str(element.get("element_type", "unknown")),
                "confidence": float(element.get("confidence", 0.0)),
                "bbox": element["bbox"],
                "score": round(float(candidate["score"]), 6),
                "label_score": round(float(candidate["label_score"]), 6),
                "near_score": round(float(candidate["near_score"]), 6)
                if candidate.get("near_score") is not None
                else None,
                "side_score": round(float(candidate["side_score"]), 6)
                if candidate.get("side_score") is not None
                else None,
                "distance_to_near_px": round(float(candidate["distance_to_near_px"]), 3)
                if candidate.get("distance_to_near_px") is not None
                else None,
            }
        )

    if not candidates and resolved.get("mode") in {"element", "element_id", "region", "coordinates", "label"}:
        candidates.append(
            {
                "index": int(resolved.get("element_index", -1)),
                "element_id": str(resolved.get("element_id", "")),
                "label": str(resolved.get("element_label", "")),
                "element_type": None,
                "confidence": None,
                "bbox": resolved.get("resolved_bbox"),
                "score": float(resolved.get("matched_score", 1.0)),
                "label_score": float(resolved.get("matched_label_score", 1.0))
                if resolved.get("matched_label_score") is not None
                else None,
                "near_score": resolved.get("matched_near_score"),
                "side_score": resolved.get("matched_side_score"),
                "distance_to_near_px": resolved.get("distance_to_near_px"),
            }
        )

    ambiguity = _ambiguity_summary(ranked=candidates, score_key="score")
    warnings: list[str] = []
    if ambiguity["ambiguous"]:
        warnings.append(
            "locate result is ambiguous: top candidates have close scores; use near/side/within hints or target selectors."
        )
        if args.require_unambiguous:
            raise ProcessingError(
                "locate result is ambiguous and --require-unambiguous was set. "
                f"top_score={ambiguity['top_score']}, second_score={ambiguity['second_score']}, "
                f"gap={ambiguity['top2_gap']}."
            )

    debug_path = None
    if args.save_annotated:
        debug_path = _save_locate_debug_image(
            image_path=image_path,
            candidates=[candidate for candidate in candidates if candidate.get("bbox")],
            output_path=Path(args.save_annotated).expanduser().resolve(),
            top_k=args.top_k,
        )

    processing_time_ms = int(round(_perf_ms() - start_ms))
    payload = {
        "status": "success",
        "error": None,
        "warnings": warnings,
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
                "top_k": int(args.top_k),
                "device": runtime.effective_device,
                "config_path": str(project_config.path) if project_config else None,
                "image_sha256": image_sha256,
                "config_sha256": config_sha256,
                "annotated_path": str(debug_path) if debug_path else None,
            },
        ),
        "locate": {
            "query": str(args.query),
            "edge": str(args.edge),
            "resolved": resolved,
            "candidates": candidates,
            "total_candidates": len(ranked_raw),
            "ambiguity": ambiguity,
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
                "--region-name requires a project config. Provide --config <path> or create .caliper.json."
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


def _cmd_debug(
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

    elements = list(parsed_data["elements"])
    if args.max_elements > 0:
        elements = elements[: int(args.max_elements)]

    output_path = Path(args.output).expanduser().resolve()
    debug_path = _save_label_debug_image(
        image_path=image_path,
        elements=elements,
        output_path=output_path,
        max_elements=max(1, int(args.max_elements or len(elements))),
        inferred_regions=list(parsed_data.get("raw_uied", {}).get("inferred_regions", [])),
    )

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
                "config_path": str(project_config.path) if project_config else None,
                "image_sha256": image_sha256,
                "config_sha256": config_sha256,
                "output_path": str(debug_path),
                "elements_rendered": len(elements),
            },
        ),
        "debug": {
            "output_path": str(debug_path),
            "elements_rendered": len(elements),
            "image_width": int(parsed_data["image_width"]),
            "image_height": int(parsed_data["image_height"]),
        },
    }
    _write_json_stdout(payload, response_context=response_context)
    return 0


def _cmd_match(
    args: argparse.Namespace,
    runtime: OmniRuntime,
    logger: Console,
    project_config: ProjectConfig | None,
    response_context: ResponseContext,
    config_sha256: str | None,
) -> int:
    start_ms = _perf_ms()
    if int(args.top_k) <= 0:
        raise UserInputError("--top-k must be >= 1.")

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

    elements1 = list(parsed1["elements"])
    elements2 = list(parsed2["elements"])

    source_resolved = _resolve_element_by_spec(
        spec=args.query,
        elements=elements1,
        image_width=int(parsed1["image_width"]),
        image_height=int(parsed1["image_height"]),
        edge="center",
        role="--query (image1)",
        project_config=project_config,
    )
    source_index = int(source_resolved.get("element_index", -1))
    if source_index < 0 or source_index >= len(elements1):
        raise UserInputError(
            "--query must resolve to an element in image1 for match mode."
        )
    source = elements1[source_index]

    query_scores_image2 = {
        int(candidate["element"]["index"]): float(candidate["score"])
        for candidate in rank_reference_candidates(
            spec=args.query,
            elements=elements2,
            image_width=int(parsed2["image_width"]),
            image_height=int(parsed2["image_height"]),
            role="--query (image2)",
            targets=project_config.targets if project_config else None,
        )
    }

    anchor_specs = list(args.anchor or [])
    anchor_pairs: list[tuple[tuple[float, float], tuple[float, float]]] = []
    warnings: list[str] = []
    for anchor_spec in anchor_specs:
        try:
            anchor1 = _resolve_element_by_spec(
                spec=anchor_spec,
                elements=elements1,
                image_width=int(parsed1["image_width"]),
                image_height=int(parsed1["image_height"]),
                edge="center",
                role=f"--anchor {anchor_spec} (image1)",
                project_config=project_config,
            )
            anchor2 = _resolve_element_by_spec(
                spec=anchor_spec,
                elements=elements2,
                image_width=int(parsed2["image_width"]),
                image_height=int(parsed2["image_height"]),
                edge="center",
                role=f"--anchor {anchor_spec} (image2)",
                project_config=project_config,
            )
            a1 = (float(anchor1["resolved_point"]["x"]), float(anchor1["resolved_point"]["y"]))
            a2 = (float(anchor2["resolved_point"]["x"]), float(anchor2["resolved_point"]["y"]))
            anchor_pairs.append((a1, a2))
        except Exception as exc:
            warnings.append(f"anchor '{anchor_spec}' could not be resolved in both images: {exc}")

    source_label_norm = _normalize_text(str(source.get("label", "")))
    source_type = str(source.get("element_type", "unknown"))
    source_bbox = source["bbox"]
    src_nx, src_ny = _normalized_center(
        source_bbox,
        int(parsed1["image_width"]),
        int(parsed1["image_height"]),
    )

    scored_candidates: list[dict[str, Any]] = []
    for candidate in elements2:
        candidate_label_norm = _normalize_text(str(candidate.get("label", "")))
        label_similarity = difflib.SequenceMatcher(None, source_label_norm, candidate_label_norm).ratio()
        if source_label_norm and source_label_norm in candidate_label_norm:
            label_similarity = max(label_similarity, 0.95)

        type_score = 1.0 if str(candidate.get("element_type", "unknown")) == source_type else 0.0
        size_score = _size_similarity(source_bbox, candidate["bbox"])
        cand_nx, cand_ny = _normalized_center(
            candidate["bbox"],
            int(parsed2["image_width"]),
            int(parsed2["image_height"]),
        )
        position_score = max(0.0, 1.0 - math.hypot(cand_nx - src_nx, cand_ny - src_ny))

        anchor_score = None
        if anchor_pairs:
            anchor_dist_scores: list[float] = []
            src_cx, src_cy = _bbox_point_for_edge(source_bbox, edge="center")
            cand_cx, cand_cy = _bbox_point_for_edge(candidate["bbox"], edge="center")
            for (a1x, a1y), (a2x, a2y) in anchor_pairs:
                vec1 = (float(src_cx) - a1x, float(src_cy) - a1y)
                vec2 = (float(cand_cx) - a2x, float(cand_cy) - a2y)
                delta = math.hypot(vec2[0] - vec1[0], vec2[1] - vec1[1])
                max_dim = math.hypot(max(1, int(parsed2["image_width"])), max(1, int(parsed2["image_height"])))
                anchor_dist_scores.append(max(0.0, 1.0 - (delta / max_dim)))
            anchor_score = sum(anchor_dist_scores) / max(1, len(anchor_dist_scores))

        query_score = float(query_scores_image2.get(int(candidate["index"]), 0.0))

        weighted = 0.0
        total_weight = 0.0

        weighted += query_score * 0.30
        total_weight += 0.30

        weighted += label_similarity * 0.25
        total_weight += 0.25

        if anchor_score is not None:
            weighted += anchor_score * 0.20
            total_weight += 0.20

        weighted += position_score * 0.15
        total_weight += 0.15

        weighted += size_score * 0.05
        total_weight += 0.05

        weighted += type_score * 0.05
        total_weight += 0.05

        match_score = weighted / total_weight if total_weight > 0 else 0.0

        scored_candidates.append(
            {
                "index": int(candidate["index"]),
                "element_id": str(candidate.get("element_id", "")),
                "label": str(candidate.get("label", "")),
                "element_type": str(candidate.get("element_type", "unknown")),
                "confidence": float(candidate.get("confidence", 0.0)),
                "bbox": candidate["bbox"],
                "match_score": float(match_score),
                "query_score": float(query_score),
                "label_similarity": float(label_similarity),
                "type_score": float(type_score),
                "size_score": float(size_score),
                "position_score": float(position_score),
                "anchor_score": float(anchor_score) if anchor_score is not None else None,
            }
        )

    scored_candidates.sort(
        key=lambda item: (-item["match_score"], -item["query_score"], int(item["index"]))
    )

    ambiguity = _ambiguity_summary(ranked=scored_candidates, score_key="match_score")
    if ambiguity["ambiguous"]:
        warnings.append(
            "match result is ambiguous: top candidates have close match_score values; add anchors or stronger query hints."
        )
        if args.require_unambiguous:
            raise ProcessingError(
                "match result is ambiguous and --require-unambiguous was set. "
                f"top_score={ambiguity['top_score']}, second_score={ambiguity['second_score']}, "
                f"gap={ambiguity['top2_gap']}."
            )

    if not scored_candidates:
        raise ProcessingError("Unable to score candidates in image2.")

    best = scored_candidates[0]
    if float(best["match_score"]) < float(args.min_score):
        raise ProcessingError(
            f"Best candidate score {best['match_score']:.3f} is below --min-score {float(args.min_score):.3f}."
        )

    source_center = _bbox_point_for_edge(source_bbox, edge="center")
    best_center = _bbox_point_for_edge(best["bbox"], edge="center")
    delta = {
        "x": int(best_center[0] - source_center[0]),
        "y": int(best_center[1] - source_center[1]),
        "width": int(best["bbox"]["width"] - source_bbox["width"]),
        "height": int(best["bbox"]["height"] - source_bbox["height"]),
    }

    debug_path = None
    if args.save_annotated:
        debug_path = _match_debug_visual(
            image1_path=image1_path,
            image2_path=image2_path,
            source=source,
            candidates=scored_candidates,
            output_path=Path(args.save_annotated).expanduser().resolve(),
            top_k=int(args.top_k),
        )

    processing_time_ms = int(round(_perf_ms() - start_ms))
    payload = {
        "status": "success",
        "error": None,
        "warnings": warnings,
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
                "query": str(args.query),
                "top_k": int(args.top_k),
                "min_score": float(args.min_score),
                "anchors_requested": anchor_specs,
                "anchors_resolved": len(anchor_pairs),
                "config_path": str(project_config.path) if project_config else None,
                "image_sha256": image1_sha256,
                "image_sha256_2": image2_sha256,
                "config_sha256": config_sha256,
                "annotated_path": str(debug_path) if debug_path else None,
            },
        ),
        "match": {
            "query": str(args.query),
            "source": {
                "image": str(image1_path),
                "index": int(source["index"]),
                "element_id": str(source.get("element_id", "")),
                "label": str(source.get("label", "")),
                "element_type": str(source.get("element_type", "unknown")),
                "confidence": float(source.get("confidence", 0.0)),
                "bbox": source_bbox,
            },
            "target": {
                "image": str(image2_path),
                **best,
            },
            "delta": delta,
            "candidates": scored_candidates[: max(1, int(args.top_k))],
            "total_candidates": len(scored_candidates),
            "ambiguity": ambiguity,
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


def _cmd_baseline(
    args: argparse.Namespace,
    runtime: OmniRuntime,
    logger: Console,
    project_config: ProjectConfig | None,
    response_context: ResponseContext,
    config_sha256: str | None,
) -> int:
    start_ms = _perf_ms()
    if int(args.tolerance) < 0:
        raise UserInputError("--tolerance must be >= 0.")

    image_path = _resolve_image_path(args.image)
    image_sha256 = _sha256_file(image_path)

    parsed_data, cache_hit, logs = runtime.parse_image(
        image_path=image_path,
        box_threshold=DEFAULT_BOX_THRESHOLD,
        use_cache=args.cache,
    )
    if args.verbose and logs:
        logger.debug(logs.rstrip())

    image_width = int(parsed_data["image_width"])
    image_height = int(parsed_data["image_height"])
    elements = list(parsed_data.get("elements", []))

    config_payload, horizontal_pair_count, vertical_pair_count = _build_baseline_config(
        image_path=image_path,
        image_width=image_width,
        image_height=image_height,
        elements=elements,
        project_name=args.project_name,
        tolerance=int(args.tolerance),
    )

    config_path = (
        Path(args.save_config).expanduser().resolve()
        if args.save_config
        else (Path.cwd() / ".caliper.json").resolve()
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(config_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

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
                "config_path": str(config_path),
                "image_sha256": image_sha256,
                "config_sha256": config_sha256,
                "detected_elements": len(elements),
                "horizontal_pairs": horizontal_pair_count,
                "vertical_pairs": vertical_pair_count,
                "tolerance": int(args.tolerance),
            },
        ),
        "baseline": {
            "config_path": str(config_path),
            "project_name": config_payload.get("project_name"),
            "reference_image": config_payload.get("reference_image"),
            "viewport": config_payload.get("viewport"),
            "region_count": len(config_payload.get("regions", {})),
            "assertion_count": len(config_payload.get("assertions", [])),
            "config": config_payload,
        },
    }
    _write_json_stdout(payload, response_context=response_context)
    return 0


class OmniArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # pragma: no cover - argparse dispatch
        raise UserInputError(message)


def _build_parser(
    *,
    cli_root: Path,
    runtime_root: Path,
) -> tuple[OmniArgumentParser, dict[str, argparse.ArgumentParser]]:
    engine_choices = list_engines()
    if not engine_choices:
        raise UserInputError("No detection engines are registered.")
    default_engine = "omniparser" if "omniparser" in engine_choices else engine_choices[0]

    description = (
        "caliper: Production CLI wrapper for OmniParser + UIED\n\n"
        "Subcommands:\n"
        "  parse    Parse a screenshot into structured UI elements. Example: caliper parse screen.png --quiet\n"
        "  debug    Render a labeled debug image of detections. Example: caliper debug screen.png -o /tmp/debug.png\n"
        "  locate   Resolve a label/selector to element candidates. Example: caliper locate screen.png --query \"save|side:right\"\n"
        "  match    Match one logical UI element across two screenshots. Example: caliper match before.png after.png --query \"save\"\n"
        "  doctor   Diagnose environment/runtime/model health. Example: caliper doctor --image screen.png\n"
        "  measure  Measure pixel distances between points/elements. Example: caliper measure screen.png --from element:0 --to element:1\n"
        "  crop     Extract a screenshot region. Example: caliper crop screen.png --region 0,0,200,200 -o crop.png\n"
        "  diff     Compare two screenshots structurally. Example: caliper diff before.png after.png --tolerance 5\n"
        "  info     Show image metadata and UI summary. Example: caliper info screen.png\n"
        "  check    Run project assertions from .caliper.json. Example: caliper check screen.png --quiet\n"
        "  overlay  Blend two screenshots with optional overlays. Example: caliper overlay a.png b.png -o overlay.png\n"
        "  baseline Auto-generate .caliper.json assertions from a reference image. Example: caliper baseline screen.png\n"
        "  engines  List registered detection engines and dependency availability. Example: caliper engines --json\n"
        "  help     Show top-level or subcommand help. Example: caliper help parse"
    )

    parser = OmniArgumentParser(
        prog="caliper",
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
        default=None,
        help=(
            "Override model directory. Default: CALIPER_MODEL_DIR if set, "
            f"otherwise {str(_default_model_dir(runtime_root))}."
        ),
    )
    parser.add_argument(
        "--runtime-root",
        default=None,
        help=(
            "Path to OmniParser runtime root (must contain util/utils.py). "
            "Overrides OMNIPARSER_ROOT / CALIPER_RUNTIME_ROOT."
        ),
    )
    parser.add_argument(
        "--engine",
        choices=engine_choices,
        default=default_engine,
        help=f"Detection engine (default: {default_engine}).",
    )
    parser.add_argument(
        "--uied-root",
        default=None,
        help="Path to UIED root (must contain detect_compo/ and detect_merge/).",
    )
    parser.add_argument(
        "--uied-text-engine",
        choices=["paddle", "google", "none"],
        default="paddle",
        help="UIED text engine when --engine uied (default: paddle).",
    )
    parser.add_argument(
        "--config",
        help="Explicit path to project config file (.caliper.json). Overrides auto-discovery.",
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
        help="Enable/disable parse result caching in ~/.cache/caliper (default: enabled).",
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
            "--runtime-root",
            default=argparse.SUPPRESS,
            help=argparse.SUPPRESS,
        )
        command_parser.add_argument(
            "--engine",
            choices=engine_choices,
            default=argparse.SUPPRESS,
            help=argparse.SUPPRESS,
        )
        command_parser.add_argument(
            "--uied-root",
            default=argparse.SUPPRESS,
            help=argparse.SUPPRESS,
        )
        command_parser.add_argument(
            "--uied-text-engine",
            choices=["paddle", "google", "none"],
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
        "    {\"index\": int, \"element_id\": str, \"bbox\": {\"x\": int, \"y\": int, \"width\": int, \"height\": int},\n"
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

    debug_help = (
        "Create a visual debug artifact with element boxes and text labels.\n\n"
        "Examples:\n"
        "  caliper debug screen.png -o /tmp/debug.png --quiet\n"
        "  caliper debug screen.png -o /tmp/debug.png --max-elements 120\n"
    )
    debug_parser = subparsers.add_parser(
        "debug",
        help="Render labeled visual debug output for OmniParser detections.",
        epilog=debug_help,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_trailing_global_flags(debug_parser)
    debug_parser.add_argument("image", help="Path to screenshot image.")
    debug_parser.add_argument(
        "-o",
        "--output",
        required=True,
        help="Output path for debug image.",
    )
    debug_parser.add_argument(
        "--max-elements",
        type=int,
        default=200,
        help="Max number of elements to render (default: 200).",
    )
    debug_parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=DEFAULT_BOX_THRESHOLD,
        help="Detection threshold for debug parse.",
    )

    locate_help = (
        "Resolve an element reference with optional proximity hints.\n\n"
        "Query examples:\n"
        "  caliper locate screen.png --query \"save\"\n"
        "  caliper locate screen.png --query \"label:save|side:right|near:1400,900\"\n"
        "  caliper locate screen.png --query \"*|near:300,200|within:250\"\n\n"
        "Hints:\n"
        "  near:x,y     Prefer elements near this point\n"
        "  side:<side>  Prefer elements near left|right|top|bottom|center\n"
        "  within:px    Require candidate center within this radius from near point\n"
    )
    locate_parser = subparsers.add_parser(
        "locate",
        help="Find best element matches by label and proximity hints.",
        epilog=locate_help,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_trailing_global_flags(locate_parser)
    locate_parser.add_argument("image", help="Path to screenshot image.")
    locate_parser.add_argument(
        "--query",
        required=True,
        help="Reference query (label, element:<idx>, region:<name>, x,y, with optional |near:|side:|within: hints).",
    )
    locate_parser.add_argument(
        "--edge",
        choices=list(EDGE_CHOICES),
        default="center",
        help="Edge/anchor used for resolved coordinates (default: center).",
    )
    locate_parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Max candidate matches to return (default: 5).",
    )
    locate_parser.add_argument(
        "--save-annotated",
        help="Save candidate-ranked debug image to this path.",
    )
    locate_parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=DEFAULT_BOX_THRESHOLD,
        help="Detection threshold used while resolving matches.",
    )
    locate_parser.add_argument(
        "--require-unambiguous",
        action="store_true",
        help="Fail when top candidates are too close in score (automation-safe mode).",
    )

    match_help = (
        "Match one logical UI element across two screenshots using label + proximity + geometry scoring.\n\n"
        "Examples:\n"
        "  caliper match before.png after.png --query \"save\" --quiet\n"
        "  caliper match before.png after.png --query \"save|side:right|near:1400,900\" --anchor \"region:sidebar\"\n"
        "  caliper match before.png after.png --query \"label:*|near:300,200\" --top-k 10 --save-annotated /tmp/match.png\n"
    )
    match_parser = subparsers.add_parser(
        "match",
        help="Match the same UI element across two screenshots.",
        epilog=match_help,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_trailing_global_flags(match_parser)
    match_parser.add_argument("image1", help="Reference screenshot path.")
    match_parser.add_argument("image2", help="Comparison screenshot path.")
    match_parser.add_argument(
        "--query",
        required=True,
        help="Selector used to resolve source in image1 and bias candidates in image2.",
    )
    match_parser.add_argument(
        "--anchor",
        action="append",
        default=[],
        help="Optional repeatable anchor selector resolved in both images to improve matching robustness.",
    )
    match_parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Number of ranked candidates to return (default: 5).",
    )
    match_parser.add_argument(
        "--min-score",
        type=float,
        default=0.25,
        help="Fail if best match score is below this threshold (default: 0.25).",
    )
    match_parser.add_argument(
        "--save-annotated",
        help="Save side-by-side match debug image.",
    )
    match_parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=DEFAULT_BOX_THRESHOLD,
        help="Detection threshold used for both images.",
    )
    match_parser.add_argument(
        "--require-unambiguous",
        action="store_true",
        help="Fail when top candidates are too close in score (automation-safe mode).",
    )

    doctor_help = (
        "Run environment diagnostics for wrapper/runtime/dependencies/models.\n\n"
        "Examples:\n"
        "  caliper doctor --quiet\n"
        "  caliper doctor --runtime-root /path/to/OmniParser --quiet\n"
        "  caliper doctor --image screenshot.png --quiet\n"
    )
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Diagnose CaliperUI CLI runtime health.",
        epilog=doctor_help,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_trailing_global_flags(doctor_parser)
    doctor_parser.add_argument(
        "--image",
        help="Optional image path for parse smoke test.",
    )
    doctor_parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=DEFAULT_BOX_THRESHOLD,
        help="Detection threshold used by optional parse smoke test.",
    )

    measure_help = (
        "Examples:\n"
        "  caliper measure screen.png --from 120,300 --to 450,300\n"
        "  caliper measure screen.png --from element:3 --to element:7 --edge center\n"
        "  caliper measure screen.png --from id:e_abc123 --to id:e_def456 --axis x\n"
        "  caliper measure screen.png --from \"sidebar\" --to \"main content\" --axis x"
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
        "  caliper crop screen.png --region 0,0,300,1080 -o left.png\n"
        "  caliper crop screen.png --element 5 --padding 20 -o button.png\n"
        "  caliper crop screen.png --region 0,0,200,200 > crop.png"
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
        "  caliper diff before.png after.png\n"
        "  caliper diff before.png after.png --tolerance 8 --save-diff /tmp/diff.png\n"
        "  caliper diff before.png after.png --focus 0,0,800,500"
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

    baseline_help = (
        "Generate a starter .caliper.json using detected element boxes and adjacency gaps.\n\n"
        "Examples:\n"
        "  caliper baseline screen.png --quiet\n"
        "  caliper baseline screen.png --save-config ./baseline.json --project-name my-ui\n"
        "  caliper baseline screen.png --tolerance 8\n"
    )
    baseline_parser = subparsers.add_parser(
        "baseline",
        help="Auto-generate a baseline .caliper.json from a reference image.",
        epilog=baseline_help,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_trailing_global_flags(baseline_parser)
    baseline_parser.add_argument("image", help="Reference image path.")
    baseline_parser.add_argument(
        "--save-config",
        help="Output config path (default: .caliper.json in current working directory).",
    )
    baseline_parser.add_argument(
        "--project-name",
        help="Project name stored in generated config (default: image filename stem).",
    )
    baseline_parser.add_argument(
        "--tolerance",
        type=int,
        default=DEFAULT_DIFF_TOLERANCE,
        help=f"Default tolerance (pixels) for generated assertions (default: {DEFAULT_DIFF_TOLERANCE}).",
    )

    engines_parser = subparsers.add_parser(
        "engines",
        help="List registered detection engines and availability.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_trailing_global_flags(engines_parser)

    help_parser = subparsers.add_parser("help", help="Show help for top-level or a subcommand.")
    help_parser.add_argument(
        "help_command",
        nargs="?",
        choices=[
            "parse",
            "debug",
            "locate",
            "match",
            "doctor",
            "measure",
            "crop",
            "diff",
            "info",
            "check",
            "overlay",
            "baseline",
            "engines",
            "help",
        ],
        help="Subcommand to show help for.",
    )

    subparser_map = {
        "parse": parse_parser,
        "debug": debug_parser,
        "locate": locate_parser,
        "match": match_parser,
        "doctor": doctor_parser,
        "measure": measure_parser,
        "crop": crop_parser,
        "diff": diff_parser,
        "info": info_parser,
        "check": check_parser,
        "overlay": overlay_parser,
        "baseline": baseline_parser,
        "engines": engines_parser,
        "help": help_parser,
    }
    return parser, subparser_map


def main(argv: list[str] | None = None) -> int:
    cli_root = Path(__file__).resolve().parents[1]
    raw_argv = list(argv) if argv is not None else list(sys.argv[1:])
    try:
        initial_runtime_root = _resolve_runtime_root(cli_root, runtime_override=None)
    except UserInputError:
        initial_runtime_root = cli_root
    omniparser_version = _omniparser_version(initial_runtime_root)

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
            schema_path = _schema_path_for_command(cli_root, command_token)
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
                    "omniparser_version": _omniparser_version(initial_runtime_root),
                    "cli_version": CLI_VERSION,
                },
            }
            _write_json_stdout(payload, response_context=response_context)
            return 0

    parser, subparser_map = _build_parser(cli_root=cli_root, runtime_root=initial_runtime_root)

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
            print("Run `caliper --help` for usage.", file=sys.stderr)
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
        print(f"caliper {CLI_VERSION} (omniparser {omniparser_version})")
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
        schema_path = _schema_path_for_command(cli_root, str(args.command))
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

        if args.command == "doctor":
            return _cmd_doctor(
                args,
                logger,
                response_context,
                cli_root=cli_root,
            )

        if args.command == "engines":
            return _cmd_engines(
                args,
                response_context,
                omniparser_version=omniparser_version,
            )

        runtime_root = _resolve_runtime_root(cli_root, runtime_override=getattr(args, "runtime_root", None))
        omniparser_version = _omniparser_version(runtime_root)

        engine = str(getattr(args, "engine", "omniparser"))
        uied_root: Path | None = None
        if engine == "uied":
            uied_root = _resolve_uied_root(cli_root, uied_override=getattr(args, "uied_root", None))

        model_dir = Path(args.model_dir).expanduser().resolve() if args.model_dir else _default_model_dir(runtime_root)
        runtime = OmniRuntime(
            repo_root=runtime_root,
            model_dir=model_dir,
            requested_device=args.device,
            logger=logger,
            omniparser_version=omniparser_version,
            engine=engine,
            uied_root=uied_root,
            uied_text_engine=str(getattr(args, "uied_text_engine", "paddle")),
        )

        if args.command == "parse":
            return _cmd_parse(args, runtime, logger, project_config, response_context, config_sha256)
        if args.command == "debug":
            return _cmd_debug(args, runtime, logger, project_config, response_context, config_sha256)
        if args.command == "locate":
            return _cmd_locate(args, runtime, logger, project_config, response_context, config_sha256)
        if args.command == "match":
            return _cmd_match(args, runtime, logger, project_config, response_context, config_sha256)
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
        if args.command == "baseline":
            return _cmd_baseline(args, runtime, logger, project_config, response_context, config_sha256)
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
            "engine": str(getattr(args, "engine", "omniparser")),
        }
        explicit_config = getattr(args, "config", None)
        if explicit_config:
            error_meta["config_path"] = str(Path(explicit_config).expanduser().resolve())
        runtime_root_arg = getattr(args, "runtime_root", None)
        if runtime_root_arg:
            try:
                error_meta["runtime_root"] = str(Path(runtime_root_arg).expanduser().resolve())
            except Exception:
                error_meta["runtime_root"] = str(runtime_root_arg)
        uied_root_arg = getattr(args, "uied_root", None)
        if uied_root_arg:
            try:
                error_meta["uied_root"] = str(Path(uied_root_arg).expanduser().resolve())
            except Exception:
                error_meta["uied_root"] = str(uied_root_arg)
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
