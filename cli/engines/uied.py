from __future__ import annotations

import base64
import hashlib
import importlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

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

COMMON_UIED_ROOTS = (
    "~/ai/UIED",
    "~/UIED",
    "~/src/UIED",
    "~/projects/UIED",
)


def _normalize_text(value: str) -> str:
    return " ".join(value.strip().casefold().split())


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


def _is_uied_root(path: Path) -> bool:
    return (
        (path / "detect_compo" / "ip_region_proposal.py").exists()
        and (path / "detect_merge" / "merge.py").exists()
    )


def _resolve_uied_root() -> Path:
    env_root = os.environ.get("UIED_ROOT")
    if env_root:
        candidate = Path(env_root).expanduser().resolve()
        if _is_uied_root(candidate):
            return candidate

    for candidate_text in COMMON_UIED_ROOTS:
        candidate = Path(candidate_text).expanduser().resolve()
        if _is_uied_root(candidate):
            return candidate

    raise RuntimeError(
        "Unable to resolve UIED root. Set UIED_ROOT or install UIED with detect_compo/ and detect_merge/."
    )


def _load_image_validated(path: Path) -> Image.Image:
    if path.suffix and path.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
        raise RuntimeError(f"Unsupported image extension '{path.suffix}'.")
    try:
        return Image.open(path)
    except (UnidentifiedImageError, OSError) as exc:
        raise RuntimeError(f"Unable to read image file '{path}': {exc}") from exc


def engine_availability() -> tuple[bool, str | None]:
    try:
        root = _resolve_uied_root()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)

    if not _is_uied_root(root):
        return False, f"invalid UIED root: {root}"

    inserted = False
    removed_config_paths: list[str] = []
    existing_config_module = None
    try:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
            inserted = True

        import time as time_module

        if not hasattr(time_module, "clock"):
            setattr(time_module, "clock", time_module.perf_counter)

        existing_config_module = sys.modules.get("config")
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
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    finally:
        for entry in reversed(removed_config_paths):
            sys.path.insert(0, entry)
        if existing_config_module is not None:
            sys.modules["config"] = existing_config_module
        else:
            sys.modules.pop("config", None)
        if inserted:
            try:
                sys.path.remove(str(root))
            except ValueError:
                pass
    return True, None


class UIEDEngine(DetectionEngine):
    name = "uied"
    display_name = "UIED"

    def __init__(self) -> None:
        self._loaded = False
        self._uied_root: Path | None = None
        self._uied_text_engine = "paddle"
        self._uied_ip: Any = None
        self._uied_text: Any = None
        self._uied_merge: Any = None
        self._uied_paddle_ocr: Any = None
        self._last_artifacts: dict[str, Any] = {}

    def load(self, model_dir: str, device: str) -> None:  # noqa: ARG002
        if self._loaded:
            return

        self._uied_root = _resolve_uied_root()
        self._uied_text_engine = str(os.environ.get("CALIPER_UIED_TEXT_ENGINE", "paddle"))
        if str(self._uied_root) not in sys.path:
            sys.path.insert(0, str(self._uied_root))

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

        try:
            self._uied_ip = importlib.import_module("detect_compo.ip_region_proposal")
            self._uied_merge = importlib.import_module("detect_merge.merge")
            if self._uied_text_engine == "google":
                self._uied_text = importlib.import_module("detect_text.text_detection")
        except ModuleNotFoundError as exc:
            raise RuntimeError("Could not import UIED modules.") from exc
        finally:
            for entry in reversed(removed_config_paths):
                sys.path.insert(0, entry)
            if existing_config_module is not None:
                sys.modules["config"] = existing_config_module
            else:
                sys.modules.pop("config", None)

        self._loaded = True

    def _infer_uied_layout_regions(
        self,
        *,
        image_width: int,
        image_height: int,
        elements: list[DetectedElement],
    ) -> list[dict[str, Any]]:
        if image_width <= 0 or image_height <= 0 or not elements:
            return []

        raw_elements = [
            {
                "bbox": {
                    "x": item.bbox.x,
                    "y": item.bbox.y,
                    "width": item.bbox.w,
                    "height": item.bbox.h,
                },
                "element_type": item.element_type,
            }
            for item in elements
        ]

        regions: list[dict[str, Any]] = []
        edge_band_x = max(24, int(round(image_width * 0.12)))
        edge_band_y = max(24, int(round(image_height * 0.12)))

        left_bound = max(edge_band_x * 2, int(round(image_width * 0.3)))
        left_items = []
        for element in raw_elements:
            bbox = element["bbox"]
            x = int(bbox.get("x", 0))
            width = int(bbox.get("width", 0))
            right = x + width
            if x <= edge_band_x and right <= left_bound:
                left_items.append(element)
        sidebar_left_added = False
        left_rail_items = []
        max_rail_width = max(80, int(round(image_width * 0.1)))
        max_rail_height = max(140, int(round(image_height * 0.18)))
        rail_edge_x = max(48, int(round(image_width * 0.08)))
        for item in left_items:
            bbox = item["bbox"]
            x = int(bbox.get("x", 0))
            width = int(bbox.get("width", 0))
            height = int(bbox.get("height", 0))
            if str(item.get("element_type", "")).lower() == "text":
                continue
            if x <= rail_edge_x and width <= max_rail_width and height <= max_rail_height:
                left_rail_items.append(item)

        if len(left_rail_items) >= 4:
            rail_right = max(int(item["bbox"]["x"]) + int(item["bbox"]["width"]) for item in left_rail_items)
            rail_top = min(int(item["bbox"]["y"]) for item in left_rail_items)
            rail_bottom = max(int(item["bbox"]["y"]) + int(item["bbox"]["height"]) for item in left_rail_items)
            rail_vertical_coverage = (rail_bottom - rail_top) / max(1, image_height)
            if rail_vertical_coverage >= 0.55:
                pad = max(10, int(round(image_width * 0.02)))
                inferred_width = min(edge_band_x, rail_right + pad)
                inferred_width = max(inferred_width, rail_right)
                regions.append(
                    {
                        "name": "sidebar-left",
                        "bbox": {
                            "x": 0,
                            "y": 0,
                            "width": int(inferred_width),
                            "height": int(image_height),
                        },
                        "confidence": round(min(1.0, 0.5 + (rail_vertical_coverage * 0.4)), 3),
                        "source": "uied-layout-infer",
                    }
                )
                sidebar_left_added = True

        if not sidebar_left_added and len(left_items) >= 3:
            right = max(int(item["bbox"]["x"]) + int(item["bbox"]["width"]) for item in left_items)
            top = min(int(item["bbox"]["y"]) for item in left_items)
            bottom = max(int(item["bbox"]["y"]) + int(item["bbox"]["height"]) for item in left_items)
            vertical_coverage = (bottom - top) / max(1, image_height)
            if 30 <= right <= int(image_width * 0.35) and vertical_coverage >= 0.55:
                regions.append(
                    {
                        "name": "sidebar-left",
                        "bbox": {
                            "x": 0,
                            "y": 0,
                            "width": int(right),
                            "height": int(image_height),
                        },
                        "confidence": round(min(1.0, 0.45 + (vertical_coverage * 0.5)), 3),
                        "source": "uied-layout-infer",
                    }
                )

        right_bound = image_width - left_bound
        right_items = []
        for element in raw_elements:
            bbox = element["bbox"]
            x = int(bbox.get("x", 0))
            width = int(bbox.get("width", 0))
            right = x + width
            if right >= (image_width - edge_band_x) and x >= right_bound:
                right_items.append(element)
        if len(right_items) >= 3:
            left = min(int(item["bbox"]["x"]) for item in right_items)
            top = min(int(item["bbox"]["y"]) for item in right_items)
            bottom = max(int(item["bbox"]["y"]) + int(item["bbox"]["height"]) for item in right_items)
            vertical_coverage = (bottom - top) / max(1, image_height)
            if int(image_width * 0.65) <= left <= (image_width - 30) and vertical_coverage >= 0.55:
                regions.append(
                    {
                        "name": "sidebar-right",
                        "bbox": {
                            "x": int(left),
                            "y": 0,
                            "width": int(image_width - left),
                            "height": int(image_height),
                        },
                        "confidence": round(min(1.0, 0.45 + (vertical_coverage * 0.5)), 3),
                        "source": "uied-layout-infer",
                    }
                )

        top_items = [
            element
            for element in raw_elements
            if int(element.get("bbox", {}).get("y", 0)) <= edge_band_y
        ]
        if len(top_items) >= 4:
            left = min(int(item["bbox"]["x"]) for item in top_items)
            right = max(int(item["bbox"]["x"]) + int(item["bbox"]["width"]) for item in top_items)
            bottom = max(int(item["bbox"]["y"]) + int(item["bbox"]["height"]) for item in top_items)
            horizontal_coverage = (right - left) / max(1, image_width)
            if horizontal_coverage >= 0.7 and 20 <= bottom <= int(image_height * 0.25):
                regions.append(
                    {
                        "name": "top-bar",
                        "bbox": {
                            "x": 0,
                            "y": 0,
                            "width": int(image_width),
                            "height": int(bottom),
                        },
                        "confidence": round(min(1.0, 0.4 + (horizontal_coverage * 0.5)), 3),
                        "source": "uied-layout-infer",
                    }
                )

        deduped: list[dict[str, Any]] = []
        seen: set[tuple[int, int, int, int, str]] = set()
        for region in regions:
            bbox = region["bbox"]
            key = (bbox["x"], bbox["y"], bbox["width"], bbox["height"], str(region["name"]))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(region)
        return deduped

    def _render_labeled_base64(
        self,
        *,
        image_path: Path,
        elements: list[DetectedElement],
        inferred_regions: list[dict[str, Any]],
    ) -> str:
        image = Image.open(image_path).convert("RGB")
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default()

        for region in inferred_regions:
            bbox = region.get("bbox", {})
            x1 = int(bbox.get("x", 0))
            y1 = int(bbox.get("y", 0))
            x2 = x1 + int(bbox.get("width", 0))
            y2 = y1 + int(bbox.get("height", 0))
            color = (255, 210, 0)
            draw.rectangle((x1, y1, x2, y2), outline=color, width=2)
            confidence = float(region.get("confidence", 0.0))
            name = str(region.get("name", "layout"))
            draw.text((x1 + 2, max(0, y1 + 2)), f"[layout] {name} c={confidence:.3f}", fill=color, font=font)

        for idx, element in enumerate(elements):
            bbox = element.bbox
            x1 = int(bbox.x)
            y1 = int(bbox.y)
            x2 = x1 + int(bbox.w)
            y2 = y1 + int(bbox.h)

            color = (0, 210, 255) if element.element_type == "text" else (30, 200, 60)
            draw.rectangle((x1, y1, x2, y2), outline=color, width=2)
            label = str(element.label).replace("\n", " ").strip()
            if len(label) > 36:
                label = label[:33] + "..."
            draw.text((x1 + 2, max(0, y1 - 12)), f"#{idx} [{element.element_type}] {label}", fill=color, font=font)

        output = io.BytesIO()
        image.save(output, format="PNG")
        return base64.b64encode(output.getvalue()).decode("ascii")

    def _uied_detect_text_paddle(
        self,
        *,
        input_image_path: Path,
        output_root: Path,
        name: str,
        image_width: int,
        image_height: int,
    ) -> dict[str, Any]:
        from paddleocr import PaddleOCR

        if self._uied_paddle_ocr is None:
            self._uied_paddle_ocr = PaddleOCR(use_angle_cls=True, lang="en")

        ocr_result = self._uied_paddle_ocr.ocr(str(input_image_path), cls=True)
        lines_raw: list[Any]
        if isinstance(ocr_result, list) and len(ocr_result) == 1 and isinstance(ocr_result[0], list):
            lines_raw = ocr_result[0]
        elif isinstance(ocr_result, list):
            lines_raw = ocr_result
        else:
            lines_raw = []

        texts: list[dict[str, Any]] = []
        for idx, line in enumerate(lines_raw):
            if not isinstance(line, (list, tuple)) or len(line) < 2:
                continue
            points = line[0]
            content_info = line[1]
            if not isinstance(points, (list, tuple)):
                continue
            try:
                xs = [int(round(float(point[0]))) for point in points]
                ys = [int(round(float(point[1]))) for point in points]
            except Exception:
                continue
            if not xs or not ys:
                continue

            content = ""
            score = None
            if isinstance(content_info, (list, tuple)) and len(content_info) >= 1:
                content = str(content_info[0])
                if len(content_info) > 1:
                    try:
                        score = float(content_info[1])
                    except Exception:
                        score = None
            else:
                content = str(content_info)

            col_min = max(0, min(xs))
            col_max = max(0, max(xs))
            row_min = max(0, min(ys))
            row_max = max(0, max(ys))
            width = max(1, col_max - col_min)
            height = max(1, row_max - row_min)

            texts.append(
                {
                    "id": idx,
                    "content": content,
                    "column_min": col_min,
                    "row_min": row_min,
                    "column_max": col_max,
                    "row_max": row_max,
                    "width": width,
                    "height": height,
                    "score": score,
                }
            )

        ocr_root = output_root / "ocr"
        ocr_root.mkdir(parents=True, exist_ok=True)
        image_shape = [int(image_height), int(image_width), 3]
        payload = {
            "img_shape": image_shape,
            "texts": texts,
        }
        ocr_json_path = ocr_root / f"{name}.json"
        ocr_json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def detect(self, image_path: str) -> list[DetectedElement]:
        if not self._loaded:
            raise RuntimeError("Engine must be loaded before detect().")

        path = Path(image_path).expanduser().resolve()
        pil_image = _load_image_validated(path).convert("RGB")
        image_width, image_height = pil_image.size

        key_params = {
            "min-grad": 3,
            "ffl-block": 5,
            "min-ele-area": 25,
            "merge-contained-ele": True,
            "merge-line-to-paragraph": False,
            "remove-bar": True,
        }

        workspace = Path(tempfile.mkdtemp(prefix="caliper-uied-"))
        input_image_path = workspace / "input.png"
        output_root = workspace / "output"
        output_root.mkdir(parents=True, exist_ok=True)

        pil_image.save(input_image_path)
        name = input_image_path.stem

        raw_text_json: dict[str, Any] | None = None
        raw_merge_json: dict[str, Any] | None = None
        raw_compo_json: dict[str, Any] | None = None
        elements: list[DetectedElement] = []

        try:
            self._uied_ip.compo_detection(
                str(input_image_path),
                str(output_root),
                key_params,
                resize_by_height=image_height,
                classifier=None,
                show=False,
            )

            compo_json_path = output_root / "ip" / f"{name}.json"
            if not compo_json_path.exists():
                raise RuntimeError(f"UIED component output missing: {compo_json_path}")
            raw_compo_json = json.loads(compo_json_path.read_text(encoding="utf-8"))

            merge_json_path = output_root / "merge" / f"{name}.json"
            if self._uied_text_engine != "none":
                if self._uied_text_engine == "paddle":
                    raw_text_json = self._uied_detect_text_paddle(
                        input_image_path=input_image_path,
                        output_root=output_root,
                        name=name,
                        image_width=image_width,
                        image_height=image_height,
                    )
                else:
                    if self._uied_text is None:
                        raise RuntimeError("UIED text module is not loaded.")
                    self._uied_text.text_detection(
                        input_file=str(input_image_path),
                        output_file=str(output_root),
                        show=False,
                        method=self._uied_text_engine,
                    )
                text_json_path = output_root / "ocr" / f"{name}.json"
                if not text_json_path.exists():
                    raise RuntimeError(f"UIED OCR output missing: {text_json_path}")
                if raw_text_json is None:
                    raw_text_json = json.loads(text_json_path.read_text(encoding="utf-8"))

                (output_root / "merge").mkdir(parents=True, exist_ok=True)
                self._uied_merge.merge(
                    str(input_image_path),
                    str(compo_json_path),
                    str(text_json_path),
                    str(output_root / "merge"),
                    is_remove_bar=bool(key_params["remove-bar"]),
                    is_paragraph=bool(key_params["merge-line-to-paragraph"]),
                    show=False,
                )
                if merge_json_path.exists():
                    raw_merge_json = json.loads(merge_json_path.read_text(encoding="utf-8"))

            source_compos = []
            source_tag = "uied-ip"
            if raw_merge_json and isinstance(raw_merge_json.get("compos"), list):
                source_compos = list(raw_merge_json["compos"])
                source_tag = "uied-merge"
            elif raw_compo_json and isinstance(raw_compo_json.get("compos"), list):
                source_compos = list(raw_compo_json["compos"])

            normalized_entries: list[tuple[int, int, int, DetectedElement]] = []
            for raw in source_compos:
                if not isinstance(raw, dict):
                    continue
                if "position" in raw and isinstance(raw["position"], dict):
                    position = raw["position"]
                    x1 = int(position.get("column_min", 0))
                    y1 = int(position.get("row_min", 0))
                    x2 = int(position.get("column_max", x1))
                    y2 = int(position.get("row_max", y1))
                else:
                    x1 = int(raw.get("column_min", 0))
                    y1 = int(raw.get("row_min", 0))
                    x2 = int(raw.get("column_max", x1))
                    y2 = int(raw.get("row_max", y1))

                width = max(1, x2 - x1)
                height = max(1, y2 - y1)
                class_name = str(raw.get("class", "Compo"))
                text_content = str(raw.get("text_content", "")).strip()
                label = text_content if text_content else class_name

                class_lower = class_name.strip().lower()
                if class_lower == "text":
                    element_type = "text"
                elif class_lower == "block":
                    element_type = "region"
                else:
                    element_type = "icon"

                bbox_ratio = [
                    x1 / max(1, image_width),
                    y1 / max(1, image_height),
                    (x1 + width) / max(1, image_width),
                    (y1 + height) / max(1, image_height),
                ]
                detected = DetectedElement(
                    element_id=_element_fingerprint(
                        element_type=element_type,
                        label=label,
                        bbox_ratio=bbox_ratio,
                    ),
                    element_type=element_type,
                    bbox=BBox(x=x1, y=y1, w=width, h=height),
                    label=label,
                    confidence=max(0.0, min(1.0, round(float(raw.get("confidence", 1.0)), 6))),
                    source_engine=self.name,
                    raw={
                        "source": source_tag,
                        "bbox_ratio": [round(v, 8) for v in bbox_ratio],
                        "uied_class": class_name,
                        "interactable": element_type not in {"text", "region"},
                    },
                )
                normalized_entries.append((int(raw.get("id", 0)), y1, x1, detected))

            normalized_entries.sort(key=lambda item: (item[0], item[1], item[2]))
            elements = [entry[3] for entry in normalized_entries]

            inferred_regions = self._infer_uied_layout_regions(
                image_width=image_width,
                image_height=image_height,
                elements=elements,
            )
            annotated_b64 = self._render_labeled_base64(
                image_path=path,
                elements=elements,
                inferred_regions=inferred_regions,
            )

            self._last_artifacts = {
                "image_path": str(path),
                "image_width": image_width,
                "image_height": image_height,
                "annotated_image_base64": annotated_b64,
                "raw_parsed_content_list": source_compos,
                "raw_label_coordinates_ratio_xywh": [item.raw.get("bbox_ratio") for item in elements],
                "raw_ocr": raw_text_json or {"texts": [], "bboxes_xyxy_pixel": [], "scores": []},
                "raw_uied": {
                    "ip": raw_compo_json,
                    "merge": raw_merge_json,
                    "text_engine": self._uied_text_engine,
                    "inferred_regions": inferred_regions,
                },
                "effective_device": "cpu",
            }
            return elements
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
