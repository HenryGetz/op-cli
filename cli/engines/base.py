from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class BBox:
    x: int
    y: int
    w: int
    h: int


@dataclass
class DetectedElement:
    element_id: str
    element_type: str
    bbox: BBox
    label: str
    confidence: float
    source_engine: str
    raw: dict


class DetectionEngine(ABC):
    name: str
    display_name: str

    @abstractmethod
    def load(self, model_dir: str, device: str) -> None:
        """Load model weights. Called once per process."""

    @abstractmethod
    def detect(self, image_path: str) -> list[DetectedElement]:
        """Run inference. Return normalized element list."""

