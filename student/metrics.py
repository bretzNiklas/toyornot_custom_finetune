from __future__ import annotations

from collections import defaultdict
from typing import Any

from .constants import SCORE_FIELDS, clamp_score


def binary_stats(targets: list[int], predictions: list[int]) -> dict[str, float]:
    tp = sum(1 for target, prediction in zip(targets, predictions) if target == 1 and prediction == 1)
    tn = sum(1 for target, prediction in zip(targets, predictions) if target == 0 and prediction == 0)
    fp = sum(1 for target, prediction in zip(targets, predictions) if target == 0 and prediction == 1)
    fn = sum(1 for target, prediction in zip(targets, predictions) if target == 1 and prediction == 0)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    accuracy = (tp + tn) / len(targets) if targets else 0.0
    return {
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "tp": tp,
        "tn": tn,
        "fp": fp,
        "fn": fn,
    }


def accuracy(targets: list[Any], predictions: list[Any]) -> float:
    if not targets:
        return 0.0
    correct = sum(1 for target, prediction in zip(targets, predictions) if target == prediction)
    return correct / len(targets)


def mae(targets: list[float], predictions: list[float]) -> float:
    if not targets:
        return 0.0
    return sum(abs(target - prediction) for target, prediction in zip(targets, predictions)) / len(targets)


def score_band(score: int) -> str:
    if score <= 3:
        return "low"
    if score <= 6:
        return "mid"
    return "high"


def tune_binary_threshold(
    targets: list[int],
    probabilities: list[float],
    *,
    min_recall: float | None = None,
) -> float:
    best_threshold = 0.5
    best_score = (-1.0, -1.0, -1.0)
    for index in range(1, 20):
        threshold = index / 20
        predictions = [1 if probability >= threshold else 0 for probability in probabilities]
        stats = binary_stats(targets, predictions)
        if min_recall is not None and stats["recall"] < min_recall:
            candidate = (stats["recall"], stats["f1"], -abs(threshold - 0.5))
        else:
            candidate = (1.0, stats["f1"], -abs(threshold - 0.5))
        if candidate > best_score:
            best_score = candidate
            best_threshold = threshold
    return best_threshold


def compute_multitask_metrics(
    records: list[dict[str, Any]],
    *,
    usable_threshold: float,
    color_threshold: float,
) -> dict[str, Any]:
    usable_targets = [record["image_usable_target"] for record in records]
    usable_predictions = [1 if record["usable_probability"] >= usable_threshold else 0 for record in records]
    payload: dict[str, Any] = {
        "image_usable": binary_stats(usable_targets, usable_predictions),
        "thresholds": {
            "usable": usable_threshold,
            "color_applicable": color_threshold,
        },
    }

    usable_medium_targets: list[str] = []
    usable_medium_predictions: list[str] = []
    overall_targets: list[int] = []
    overall_predictions: list[int] = []
    band_targets: list[str] = []
    band_predictions: list[str] = []
    rubric_targets: dict[str, list[int]] = defaultdict(list)
    rubric_predictions: dict[str, list[int]] = defaultdict(list)
    stratified: dict[str, dict[str, list[int]]] = defaultdict(lambda: {"targets": [], "predictions": []})

    for record in records:
        if record["image_usable_target"] == 1:
            usable_medium_targets.append(record["medium_target"])
            usable_medium_predictions.append(record["medium_prediction"])

        if record["score_domain_target"] == 1:
            overall_targets.append(record["overall_score_target"])
            overall_predictions.append(record["overall_score_prediction"])
            band_targets.append(score_band(record["overall_score_target"]))
            band_predictions.append(score_band(record["overall_score_prediction"]))
            medium_key = record["medium_target"]
            stratified[medium_key]["targets"].append(record["overall_score_target"])
            stratified[medium_key]["predictions"].append(record["overall_score_prediction"])
            for field in SCORE_FIELDS:
                target_key = f"{field}_target"
                prediction_key = f"{field}_prediction"
                if record[target_key] is None or record[prediction_key] is None:
                    continue
                rubric_targets[field].append(record[target_key])
                rubric_predictions[field].append(record[prediction_key])

    payload["medium_accuracy"] = accuracy(usable_medium_targets, usable_medium_predictions)
    payload["overall_score_mae"] = mae(overall_targets, overall_predictions)
    payload["overall_band_accuracy"] = accuracy(band_targets, band_predictions)
    payload["overall_score_by_medium"] = {
        medium: {
            "count": len(values["targets"]),
            "mae": mae(values["targets"], values["predictions"]),
        }
        for medium, values in stratified.items()
    }
    payload["rubric_mae"] = {
        field: mae(rubric_targets[field], rubric_predictions[field])
        for field in SCORE_FIELDS
        if rubric_targets[field]
    }
    return payload


def build_prediction_record(
    *,
    usable_probability: float,
    usable_threshold: float,
    medium_target: str | None,
    medium_prediction: str,
    score_domain_target: bool,
    raw_scores: dict[str, float],
    row: dict[str, Any],
    color_threshold: float,
) -> dict[str, Any]:
    color_probability = raw_scores.get("color_applicable_probability", 0.0)
    color_is_applicable = color_probability >= color_threshold
    record: dict[str, Any] = {
        "file": row["file"],
        "image_usable_target": 1 if row.get("image_usable") else 0,
        "usable_probability": usable_probability,
        "usable_prediction": 1 if usable_probability >= usable_threshold else 0,
        "medium_target": medium_target,
        "medium_prediction": medium_prediction,
        "score_domain_target": 1 if score_domain_target else 0,
        "color_applicable_probability": color_probability,
        "color_applicable_prediction": 1 if color_is_applicable else 0,
    }
    record["overall_score_target"] = row.get("overall_score")
    record["overall_score_prediction"] = clamp_score(raw_scores["overall_score"])
    for field in SCORE_FIELDS:
        target = row.get(field)
        prediction = clamp_score(raw_scores[field]) if field != "color_harmony" or color_is_applicable else None
        record[f"{field}_target"] = target
        record[f"{field}_prediction"] = prediction
    return record
