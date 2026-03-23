from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from .base import BBox, DetectedElement, DetectionEngine


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


def _normalize_text(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _safe_label(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _element_fingerprint(*, element_type: str, label: str, bbox_ratio: list[float]) -> str:
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


def _resolve_runtime_root() -> Path:
    for env_key in ("OMNIPARSER_ROOT", "CALIPER_RUNTIME_ROOT"):
        value = os.environ.get(env_key)
        if value:
            candidate = Path(value).expanduser().resolve()
            if (candidate / "util" / "utils.py").exists():
                return candidate

    local_runtime = (Path(__file__).resolve().parents[2] / "OmniParser").resolve()
    if (local_runtime / "util" / "utils.py").exists():
        return local_runtime

    for candidate_text in COMMON_RUNTIME_ROOTS:
        candidate = Path(candidate_text).expanduser().resolve()
        if (candidate / "util" / "utils.py").exists():
            return candidate

    raise RuntimeError(
        "Unable to resolve OmniParser runtime root (expected <root>/util/utils.py). "
        "Set OMNIPARSER_ROOT or CALIPER_RUNTIME_ROOT."
    )


def _load_image_validated(path: Path) -> Image.Image:
    if path.suffix and path.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
        raise RuntimeError(f"Unsupported image extension '{path.suffix}'.")
    try:
        return Image.open(path)
    except (UnidentifiedImageError, OSError) as exc:
        raise RuntimeError(f"Unable to read image file '{path}': {exc}") from exc


def _ratio_xyxy_to_pixel_xywh(
    bbox_ratio_xyxy: list[float],
    *,
    image_width: int,
    image_height: int,
) -> dict[str, int]:
    x1 = int(round(float(bbox_ratio_xyxy[0]) * image_width))
    y1 = int(round(float(bbox_ratio_xyxy[1]) * image_height))
    x2 = int(round(float(bbox_ratio_xyxy[2]) * image_width))
    y2 = int(round(float(bbox_ratio_xyxy[3]) * image_height))
    x1 = max(0, min(image_width - 1, x1))
    y1 = max(0, min(image_height - 1, y1))
    x2 = max(x1 + 1, min(image_width, x2))
    y2 = max(y1 + 1, min(image_height, y2))
    return {
        "x": x1,
        "y": y1,
        "width": max(1, x2 - x1),
        "height": max(1, y2 - y1),
    }


def _compute_iou(a: list[float], b: list[float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    inter_w = max(0.0, inter_x2 - inter_x1)
    inter_h = max(0.0, inter_y2 - inter_y1)
    inter_area = inter_w * inter_h
    area_a = max(0.0, (ax2 - ax1)) * max(0.0, (ay2 - ay1))
    area_b = max(0.0, (bx2 - bx1)) * max(0.0, (by2 - by1))
    denom = area_a + area_b - inter_area
    if denom <= 0:
        return 0.0
    return inter_area / denom


def _match_confidence(target_ratio_xyxy: list[float], candidate_boxes_ratio_xyxy: list[list[float]], candidate_scores: list[float]) -> float:
    best_idx = -1
    best_iou = -1.0
    for idx, candidate_box in enumerate(candidate_boxes_ratio_xyxy):
        iou = _compute_iou(target_ratio_xyxy, candidate_box)
        if iou > best_iou:
            best_iou = iou
            best_idx = idx
    if best_idx < 0 or best_idx >= len(candidate_scores):
        return 0.0
    return float(candidate_scores[best_idx])


def engine_availability() -> tuple[bool, str | None]:
    try:
        _resolve_runtime_root()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)

    missing: list[str] = []
    for module_name in ("PIL", "torch", "ultralytics", "transformers", "paddleocr"):
        try:
            __import__(module_name)
        except Exception:
            missing.append(module_name)
    if missing:
        return False, f"missing python modules: {', '.join(missing)}"
    return True, None


class OmniParserEngine(DetectionEngine):
    name = "omniparser"
    display_name = "OmniParser"

    def __init__(self) -> None:
        self._loaded = False
        self._requested_device = "cpu"
        self._effective_device = "cpu"
        self._runtime_root: Path | None = None
        self._model_dir: Path | None = None
        self._omni_utils: Any = None
        self._som_model: Any = None
        self._caption_model_processor: Any = None
        self._last_artifacts: dict[str, Any] = {}

    def _resolve_model_paths(self) -> tuple[Path, Path]:
        if self._model_dir is None:
            raise RuntimeError("Engine model directory has not been configured")

        som_path = self._model_dir / "icon_detect" / "model.pt"
        caption_path = self._model_dir / "icon_caption_florence"
        if not som_path.exists():
            onnx_path = som_path.with_suffix(".onnx")
            if onnx_path.exists():
                som_path = onnx_path

        if not som_path.exists() or not caption_path.exists():
            raise RuntimeError(
                "Missing OmniParser model files. Expected icon_detect/model.pt (or model.onnx) "
                "and icon_caption_florence directory under model_dir."
            )
        return som_path.resolve(), caption_path.resolve()

    def load(self, model_dir: str, device: str) -> None:
        if self._loaded:
            return

        self._runtime_root = _resolve_runtime_root()
        if str(self._runtime_root) not in sys.path:
            sys.path.insert(0, str(self._runtime_root))

        self._model_dir = Path(model_dir).expanduser().resolve()
        self._requested_device = str(device)

        import importlib

        try:
            self._omni_utils = importlib.import_module("util.utils")
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Could not import OmniParser runtime module 'util.utils'."
            ) from exc

        som_path, caption_path = self._resolve_model_paths()

        if self._requested_device == "cuda":
            import torch

            if not torch.cuda.is_available():
                raise RuntimeError("--device cuda requested, but CUDA is not available on this machine.")

        # OmniParser utility code currently runs on CPU only in this environment.
        self._effective_device = "cpu"
        self._som_model = self._omni_utils.get_yolo_model(model_path=str(som_path))
        self._caption_model_processor = self._omni_utils.get_caption_model_processor(
            model_name="florence2",
            model_name_or_path=str(caption_path),
            device=self._effective_device,
        )
        self._loaded = True

    def detect(self, image_path: str) -> list[DetectedElement]:
        if not self._loaded:
            raise RuntimeError("Engine must be loaded before detect().")

        path = Path(image_path).expanduser().resolve()
        image = _load_image_validated(path).convert("RGB")
        image_width, image_height = image.size

        np_image = self._omni_utils.np.array(image)
        ocr_raw = self._omni_utils.paddle_ocr.ocr(np_image, cls=False)
        ocr_lines = ocr_raw[0] if ocr_raw else []

        ocr_texts: list[str] = []
        ocr_bboxes_xyxy_pixel: list[list[int]] = []
        ocr_scores: list[float] = []

        for line in ocr_lines:
            score = float(line[1][1])
            if score <= 0.8:
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
            BOX_TRESHOLD=0.05,
            output_coord_in_ratio=True,
            ocr_bbox=ocr_bboxes_xyxy_pixel,
            draw_bbox_config=draw_bbox_config,
            caption_model_processor=self._caption_model_processor,
            ocr_text=ocr_texts,
            use_local_semantics=True,
            iou_threshold=0.7,
            scale_img=False,
            batch_size=128,
        )

        yolo_boxes_xyxy_pixel, yolo_conf_tensor, _ = self._omni_utils.predict_yolo(
            model=self._som_model,
            image=image,
            box_threshold=0.05,
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

        elements: list[DetectedElement] = []
        for element in parsed_content_list:
            bbox_ratio = [float(v) for v in element.get("bbox", [0, 0, 0, 0])]
            bbox_xywh_pixel = _ratio_xyxy_to_pixel_xywh(
                bbox_ratio,
                image_width=image_width,
                image_height=image_height,
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

            label = _safe_label(element.get("content"))
            elements.append(
                DetectedElement(
                    element_id=_element_fingerprint(
                        element_type=element_type,
                        label=label,
                        bbox_ratio=bbox_ratio,
                    ),
                    element_type=element_type,
                    bbox=BBox(
                        x=int(bbox_xywh_pixel["x"]),
                        y=int(bbox_xywh_pixel["y"]),
                        w=int(bbox_xywh_pixel["width"]),
                        h=int(bbox_xywh_pixel["height"]),
                    ),
                    label=label,
                    confidence=max(0.0, min(1.0, round(float(confidence), 6))),
                    source_engine=self.name,
                    raw={
                        "interactable": bool(element.get("interactivity", False)),
                        "source": _safe_label(element.get("source")),
                        "bbox_ratio": [round(v, 8) for v in bbox_ratio],
                    },
                )
            )

        self._last_artifacts = {
            "image_path": str(path),
            "image_width": int(image_width),
            "image_height": int(image_height),
            "annotated_image_base64": annotated_b64,
            "raw_parsed_content_list": parsed_content_list,
            "raw_label_coordinates_ratio_xywh": label_coordinates_ratio_xywh,
            "raw_ocr": {
                "texts": ocr_texts,
                "bboxes_xyxy_pixel": ocr_bboxes_xyxy_pixel,
                "scores": [round(v, 6) for v in ocr_scores],
            },
            "effective_device": self._effective_device,
        }

        return elements
