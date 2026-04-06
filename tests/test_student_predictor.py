from __future__ import annotations

import sys
import types
import unittest

from PIL import Image

fake_torch = types.ModuleType("torch")
fake_torch.device = lambda value: value
fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False)
fake_torch.inference_mode = lambda: None
sys.modules.setdefault("torch", fake_torch)

fake_checkpoint = types.ModuleType("student.checkpoint")
fake_checkpoint.load_student_bundle = object
sys.modules.setdefault("student.checkpoint", fake_checkpoint)

fake_data = types.ModuleType("student.data")
fake_data.decode_image_b64 = object
sys.modules.setdefault("student.data", fake_data)

from student.predictor import StudentPredictor


class StudentPredictorTests(unittest.TestCase):
    def test_predict_image_returns_scores_for_digital(self) -> None:
        predictor = object.__new__(StudentPredictor)
        predictor.thresholds = {"usable": 0.5, "color_applicable": 0.5}
        predictor._predict_raw = lambda image: {
            "usable_probability": 0.99,
            "medium_prediction": "digital",
            "color_applicable_probability": 0.9,
            "overall_score": 6.4,
            "legibility": 7.2,
            "letter_structure": 5.6,
            "line_quality": 6.1,
            "composition": 5.4,
            "color_harmony": 4.8,
            "originality": 5.2,
        }

        payload = StudentPredictor.predict_image(
            predictor,
            Image.new("RGB", (32, 32)),
            filename="digital.png",
        )

        self.assertEqual(payload["filename"], "digital.png")
        self.assertEqual(payload["image_usable"], True)
        self.assertEqual(payload["medium"], "digital")
        self.assertEqual(payload["overall_score"], 6)
        self.assertEqual(payload["legibility"], 7)
        self.assertEqual(payload["letter_structure"], 6)
        self.assertEqual(payload["line_quality"], 6)
        self.assertEqual(payload["composition"], 5)
        self.assertEqual(payload["color_harmony"], 5)
        self.assertEqual(payload["originality"], 5)

    def test_predict_image_keeps_other_or_unclear_unscored(self) -> None:
        predictor = object.__new__(StudentPredictor)
        predictor.thresholds = {"usable": 0.5, "color_applicable": 0.5}
        predictor._predict_raw = lambda image: {
            "usable_probability": 0.99,
            "medium_prediction": "other_or_unclear",
            "color_applicable_probability": 0.9,
            "overall_score": 9.0,
            "legibility": 9.0,
            "letter_structure": 9.0,
            "line_quality": 9.0,
            "composition": 9.0,
            "color_harmony": 9.0,
            "originality": 9.0,
        }

        payload = StudentPredictor.predict_image(
            predictor,
            Image.new("RGB", (32, 32)),
            filename="unclear.png",
        )

        self.assertEqual(payload["image_usable"], True)
        self.assertEqual(payload["medium"], "other_or_unclear")
        self.assertIsNone(payload["overall_score"])
        self.assertIsNone(payload["legibility"])
        self.assertIsNone(payload["letter_structure"])
        self.assertIsNone(payload["line_quality"])
        self.assertIsNone(payload["composition"])
        self.assertIsNone(payload["color_harmony"])
        self.assertIsNone(payload["originality"])


if __name__ == "__main__":
    unittest.main()
