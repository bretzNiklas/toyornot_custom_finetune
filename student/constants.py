from __future__ import annotations

from typing import Any


CORE_MEDIA = ("paper_sketch", "wall_piece")
SIDE_MEDIA = ("digital", "other_or_unclear")
ALL_MEDIA = CORE_MEDIA + SIDE_MEDIA

SCORE_FIELDS = (
    "legibility",
    "letter_structure",
    "line_quality",
    "composition",
    "color_harmony",
    "originality",
)

PRIMARY_SCORE_FIELD = "overall_score"
HUMAN_SOURCES = {"human_original", "human_reviewed_teacher"}
TEACHER_SOURCE = "teacher_lite_tuned"


def score_bucket(score: int | None) -> str:
    if score is None:
        return "unknown"
    if score <= 3:
        return "low"
    if score <= 6:
        return "mid"
    return "high"


def is_score_domain(row: dict[str, Any]) -> bool:
    return bool(row.get("image_usable")) and row.get("medium") in CORE_MEDIA


def color_applicable(row: dict[str, Any]) -> bool:
    return is_score_domain(row) and row.get("color_harmony") is not None


def clamp_score(value: float) -> int:
    return max(1, min(10, int(round(value))))
