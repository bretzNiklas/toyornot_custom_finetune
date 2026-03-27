from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import os
import time
from io import BytesIO
from pathlib import Path
from typing import Any

from openai import OpenAI
from PIL import Image, ImageOps


ALLOWED_MEDIA = {"paper_sketch", "wall_piece", "digital", "other_or_unclear", None}
ALLOWED_PIECE_TYPES = {"tag", "throwie", "straight_letter", "piece", "wildstyle", "mixed", "other", None}
ALLOWED_CONFIDENCE = {"low", "medium", "high"}
SCORE_FIELDS = [
    "legibility",
    "letter_structure",
    "line_quality",
    "composition",
    "color_harmony",
    "originality",
    "overall_score",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch-label graffiti images with an OpenRouter teacher model."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("exports/v1/teacher_pilot_50_v1.jsonl"),
        help="Input manifest JSONL.",
    )
    parser.add_argument(
        "--anchors",
        type=Path,
        default=Path("exports/v1/teacher_prompt_anchors_v1.json"),
        help="Teacher prompt anchor JSON.",
    )
    parser.add_argument(
        "--prompt",
        type=Path,
        default=Path("teacher_prompt_v1.md"),
        help="Teacher prompt markdown.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("exports/openrouter/teacher_pilot_predictions.jsonl"),
        help="Output predictions JSONL.",
    )
    parser.add_argument(
        "--errors",
        type=Path,
        default=Path("exports/openrouter/teacher_pilot_errors.jsonl"),
        help="Error log JSONL.",
    )
    parser.add_argument(
        "--model",
        default="google/gemini-3.1-flash-lite-preview",
        help="OpenRouter model id.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of parallel API workers to use.",
    )
    parser.add_argument(
        "--max-images",
        type=int,
        default=None,
        help="Optional maximum number of new images to label.",
    )
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=None,
        help="Optional hard stop for total accumulated cost in USD.",
    )
    parser.add_argument(
        "--max-edge",
        type=int,
        default=768,
        help="Resize images so the longest edge is at most this many pixels.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.1,
        help="Sampling temperature.",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.0,
        help="Sleep between successful requests.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=4,
        help="How many times to retry 429/5xx failures per image.",
    )
    return parser.parse_args()


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl_row(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def existing_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return load_jsonl(path)


def encode_image(path: Path, max_edge: int) -> str:
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        img.load()

        if max(img.size) > max_edge:
            scale = max_edge / max(img.size)
            resized = (
                max(1, round(img.size[0] * scale)),
                max(1, round(img.size[1] * scale)),
            )
            img = img.resize(resized, Image.Resampling.LANCZOS)

        has_alpha = "A" in img.getbands()
        if has_alpha:
            mime = "image/png"
            fmt = "PNG"
        else:
            if img.mode != "RGB":
                img = img.convert("RGB")
            mime = "image/jpeg"
            fmt = "JPEG"

        buffer = BytesIO()
        save_kwargs = {"format": fmt}
        if fmt == "JPEG":
            save_kwargs.update({"quality": 88, "optimize": True})
        img.save(buffer, **save_kwargs)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def anchor_payload(anchor: dict) -> dict:
    return {
        "anchor_id": anchor["anchor_id"],
        "bucket": anchor["bucket"],
        "medium": anchor["medium"],
        "piece_type": anchor["piece_type"],
        "legibility": anchor["legibility"],
        "letter_structure": anchor["letter_structure"],
        "line_quality": anchor["line_quality"],
        "composition": anchor["composition"],
        "color_harmony": anchor["color_harmony"],
        "originality": anchor["originality"],
        "overall_score": anchor["overall_score"],
        "confidence": anchor["confidence"],
    }


def build_content(
    prompt_text: str,
    anchors: list[dict],
    anchor_images: dict[str, str],
    target_row: dict,
    target_image_data_url: str,
) -> list[dict]:
    content: list[dict] = [
        {
            "type": "text",
            "text": (
                f"{prompt_text}\n\n"
                "Approved calibration examples follow. Use them to match the human scoring scale."
            ),
        }
    ]

    for anchor in anchors:
        content.append(
            {
                "type": "text",
                "text": (
                    f"Approved anchor {anchor['anchor_id']}:\n"
                    f"{json.dumps(anchor_payload(anchor), ensure_ascii=True)}"
                ),
            }
        )
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": anchor_images[anchor["file"]]},
            }
        )

    content.append(
        {
            "type": "text",
            "text": (
                "Now label the next target image.\n"
                f"Use this exact filename in the JSON: {target_row['file']}\n"
                "Return exactly one JSON object and nothing else."
            ),
        }
    )
    content.append(
        {
            "type": "image_url",
            "image_url": {"url": target_image_data_url},
        }
    )
    return content


def response_schema() -> dict[str, Any]:
    nullable_int = {"anyOf": [{"type": "integer", "minimum": 1, "maximum": 10}, {"type": "null"}]}
    nullable_medium = {"anyOf": [{"type": "string", "enum": sorted(v for v in ALLOWED_MEDIA if v is not None)}, {"type": "null"}]}
    nullable_piece = {"anyOf": [{"type": "string", "enum": sorted(v for v in ALLOWED_PIECE_TYPES if v is not None)}, {"type": "null"}]}
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "graffiti_teacher_label",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "image_usable": {"type": "boolean"},
                    "medium": nullable_medium,
                    "piece_type": nullable_piece,
                    "legibility": nullable_int,
                    "letter_structure": nullable_int,
                    "line_quality": nullable_int,
                    "composition": nullable_int,
                    "color_harmony": nullable_int,
                    "originality": nullable_int,
                    "overall_score": nullable_int,
                    "confidence": {"type": "string", "enum": sorted(ALLOWED_CONFIDENCE)},
                    "notes": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": [
                    "file",
                    "image_usable",
                    "medium",
                    "piece_type",
                    "legibility",
                    "letter_structure",
                    "line_quality",
                    "composition",
                    "color_harmony",
                    "originality",
                    "overall_score",
                    "confidence",
                    "notes",
                ],
                "additionalProperties": False,
            },
        },
    }


def maybe_dump(model_obj: Any) -> dict[str, Any] | None:
    if model_obj is None:
        return None
    if hasattr(model_obj, "model_dump"):
        return model_obj.model_dump()
    if isinstance(model_obj, dict):
        return model_obj
    return None


def extract_usage(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    usage_dict = maybe_dump(usage) or {}
    return {
        "prompt_tokens": usage_dict.get("prompt_tokens"),
        "completion_tokens": usage_dict.get("completion_tokens"),
        "total_tokens": usage_dict.get("total_tokens"),
        "cost": usage_dict.get("cost"),
        "usage_raw": usage_dict,
    }


def parse_prediction(content: str) -> dict[str, Any]:
    payload = json.loads(content)
    validate_prediction(payload)
    return payload


def validate_prediction(payload: dict[str, Any]) -> None:
    if not isinstance(payload.get("file"), str) or not payload["file"]:
        raise ValueError("Prediction missing valid file field.")
    if not isinstance(payload.get("image_usable"), bool):
        raise ValueError("Prediction missing boolean image_usable.")
    if payload.get("medium") not in ALLOWED_MEDIA:
        raise ValueError(f"Invalid medium: {payload.get('medium')}")
    if payload.get("piece_type") not in ALLOWED_PIECE_TYPES:
        raise ValueError(f"Invalid piece_type: {payload.get('piece_type')}")
    if payload.get("confidence") not in ALLOWED_CONFIDENCE:
        raise ValueError(f"Invalid confidence: {payload.get('confidence')}")
    if payload.get("notes") is not None and not isinstance(payload["notes"], str):
        raise ValueError("Notes must be string or null.")

    for field in SCORE_FIELDS:
        value = payload.get(field)
        if value is not None and (not isinstance(value, int) or value < 1 or value > 10):
            raise ValueError(f"Invalid score in {field}: {value}")

    if not payload["image_usable"]:
        for field in ["medium", "piece_type", *SCORE_FIELDS]:
            if payload.get(field) is not None:
                raise ValueError(f"Unusable prediction must set {field} to null.")


def prediction_row(
    manifest_row: dict,
    prediction: dict,
    model: str,
    usage: dict[str, Any],
    response_id: str | None,
) -> dict[str, Any]:
    return {
        "file": manifest_row["file"],
        "relative_path": manifest_row.get("relative_path"),
        "absolute_path": manifest_row.get("absolute_path"),
        "pilot_group": manifest_row.get("pilot_group"),
        "locked_split": manifest_row.get("locked_split"),
        "teacher_model": model,
        "image_usable": prediction["image_usable"],
        "medium": prediction["medium"],
        "piece_type": prediction["piece_type"],
        "legibility": prediction["legibility"],
        "letter_structure": prediction["letter_structure"],
        "line_quality": prediction["line_quality"],
        "composition": prediction["composition"],
        "color_harmony": prediction["color_harmony"],
        "originality": prediction["originality"],
        "overall_score": prediction["overall_score"],
        "confidence": prediction["confidence"],
        "notes": prediction["notes"],
        "response_id": response_id,
        "prompt_tokens": usage["prompt_tokens"],
        "completion_tokens": usage["completion_tokens"],
        "total_tokens": usage["total_tokens"],
        "cost_usd": usage["cost"],
        "usage_raw": usage["usage_raw"],
    }


def exception_status_code(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is not None:
        return int(status)
    response = getattr(exc, "response", None)
    if response is not None:
        response_status = getattr(response, "status_code", None)
        if response_status is not None:
            return int(response_status)
    return None


def is_retryable(exc: Exception) -> bool:
    status = exception_status_code(exc)
    return status in {408, 409, 429, 500, 502, 503, 504}


def build_client(api_key: str) -> OpenAI:
    return OpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)


def process_one_row(
    row: dict,
    *,
    api_key: str,
    prompt_text: str,
    anchors: list[dict],
    anchor_images: dict[str, str],
    model: str,
    max_edge: int,
    temperature: float,
    max_retries: int,
) -> dict[str, Any]:
    client = build_client(api_key)
    target_path = Path(row["absolute_path"])
    delay_seconds = 1.0

    for attempt in range(1, max_retries + 1):
        try:
            target_image = encode_image(target_path, max_edge)
            content = build_content(
                prompt_text=prompt_text,
                anchors=anchors,
                anchor_images=anchor_images,
                target_row=row,
                target_image_data_url=target_image,
            )
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": content}],
                temperature=temperature,
                response_format=response_schema(),
                max_tokens=300,
                extra_body={"provider": {"require_parameters": True}},
            )
            message = response.choices[0].message.content
            prediction = parse_prediction(message)
            if prediction["file"] != row["file"]:
                raise ValueError(
                    f"Prediction file mismatch. Expected {row['file']} got {prediction['file']}"
                )
            usage = extract_usage(response)
            result = prediction_row(
                manifest_row=row,
                prediction=prediction,
                model=model,
                usage=usage,
                response_id=getattr(response, "id", None),
            )
            return {"ok": True, "result": result}
        except Exception as exc:
            if attempt < max_retries and is_retryable(exc):
                time.sleep(delay_seconds)
                delay_seconds *= 2
                continue
            return {
                "ok": False,
                "error": {
                    "file": row["file"],
                    "absolute_path": row.get("absolute_path"),
                    "teacher_model": model,
                    "error": str(exc),
                    "status_code": exception_status_code(exc),
                    "attempts": attempt,
                },
            }


def main() -> None:
    args = parse_args()
    load_dotenv(Path(".env"))
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY not found in environment or .env")

    manifest_rows = load_jsonl(args.input.resolve())
    anchors = json.loads(args.anchors.resolve().read_text(encoding="utf-8"))
    prompt_text = args.prompt.resolve().read_text(encoding="utf-8").strip()

    previous_rows = existing_rows(args.output.resolve())
    completed_files = {row["file"] for row in previous_rows}
    accumulated_cost = sum(float(row.get("cost_usd") or 0.0) for row in previous_rows)

    anchor_images = {
        anchor["file"]: encode_image(Path(anchor["absolute_path"]), args.max_edge)
        for anchor in anchors
    }

    queue = [row for row in manifest_rows if row["file"] not in completed_files]
    if args.max_images is not None:
        queue = queue[: args.max_images]

    if not queue:
        print(f"Nothing to do. total_cost=${accumulated_cost:.4f}")
        return

    max_workers = max(1, min(args.workers, len(queue)))
    print(f"Starting {len(queue)} requests with workers={max_workers}")
    print("Budget cap is best-effort with parallel workers and may be exceeded slightly by in-flight requests.")

    processed = 0
    stop_launching = False
    iterator = iter(queue)
    inflight: dict[concurrent.futures.Future, dict] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        while True:
            while not stop_launching and len(inflight) < max_workers:
                if args.max_cost_usd is not None and accumulated_cost >= args.max_cost_usd:
                    stop_launching = True
                    print(f"Stopping launches: accumulated cost ${accumulated_cost:.4f} reached budget cap.")
                    break
                row = next(iterator, None)
                if row is None:
                    stop_launching = True
                    break
                future = executor.submit(
                    process_one_row,
                    row,
                    api_key=api_key,
                    prompt_text=prompt_text,
                    anchors=anchors,
                    anchor_images=anchor_images,
                    model=args.model,
                    max_edge=args.max_edge,
                    temperature=args.temperature,
                    max_retries=args.max_retries,
                )
                inflight[future] = row

            if not inflight:
                break

            done, _ = concurrent.futures.wait(
                inflight.keys(),
                return_when=concurrent.futures.FIRST_COMPLETED,
            )

            for future in done:
                row = inflight.pop(future)
                payload = future.result()
                if payload["ok"]:
                    result = payload["result"]
                    write_jsonl_row(args.output.resolve(), result)
                    completed_files.add(row["file"])
                    processed += 1
                    cost_usd = float(result.get("cost_usd") or 0.0)
                    accumulated_cost += cost_usd
                    print(f"[ok] {row['file']} cost=${cost_usd:.4f} total=${accumulated_cost:.4f}")
                    if args.sleep_seconds > 0:
                        time.sleep(args.sleep_seconds)
                else:
                    write_jsonl_row(args.errors.resolve(), payload["error"])
                    print(f"[err] {row['file']} {payload['error']['error']}")

    print(f"Finished. processed={processed} total_cost=${accumulated_cost:.4f}")


if __name__ == "__main__":
    main()
