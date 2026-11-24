import threading
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np
import pytest

torch = pytest.importorskip("torch")
from ai.scene_segmentation import SceneDetector


def test_scene_uses_mask_not_bbox_for_corridor():
    detector = SceneDetector.__new__(SceneDetector)
    detector.model = SimpleNamespace(config=SimpleNamespace(id2label={0:"wall", 1:"floor"}))
    detector.confidence, detector.min_area, detector.labels = .65, .01, {"wall"}
    labels = np.zeros((100, 100), dtype=np.int32)
    labels[30:, 29:71] = 1  # a connected wall surrounds an unobstructed central region
    regions = detector.regions(labels, np.full((100, 100), .9), (480, 640))
    assert len(regions) == 1
    assert regions[0]["corridor_overlap"] == 0
    assert regions[0]["confidence_kind"] == "mean_pixel_probability"
