from __future__ import annotations

import argparse
import base64
import json
import os
import random
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, request


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
SCORE_FIELDS = [
    "overall_score",
    "legibility",
    "letter_structure",
    "line_quality",
    "composition",
    "color_harmony",
    "originality",
]

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0 Safari/537.36"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sample random local images, call the live graffiti API, and render a markdown report."
    )
    parser.add_argument(
        "--image-dir",
        type=Path,
        default=Path("images"),
        help="Directory containing source images.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=25,
        help="Number of successful predictions to include.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20260327,
        help="Random seed used to shuffle the image list.",
    )
    parser.add_argument(
        "--output-md",
        type=Path,
        default=Path("exports/api_random_sample_25.md"),
        help="Markdown report output path.",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=Path("exports/api_random_sample_25.json"),
        help="JSON output path containing raw API responses.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=60.0,
        help="Per-request timeout in seconds.",
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


def get_api_config() -> tuple[str, str]:
    api_url = os.environ.get("GRAFFITI_API_URL", "").strip()
    api_token = os.environ.get("GRAFFITI_API_TOKEN", "").strip() or os.environ.get("AUTH_TOKEN", "").strip()
    if not api_url:
        api_url = "https://api.piecerate.me"
    if not api_token:
        raise SystemExit("GRAFFITI_API_TOKEN or AUTH_TOKEN was not found in the environment or .env")
    return api_url.rstrip("/"), api_token


def list_images(image_dir: Path) -> list[Path]:
    if not image_dir.exists():
        raise SystemExit(f"Image directory does not exist: {image_dir}")
    files = [path for path in image_dir.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS]
    if not files:
        raise SystemExit(f"No supported images found in {image_dir}")
    return sorted(files)


def predict_image(api_url: str, api_token: str, image_path: Path, timeout_seconds: float) -> dict[str, Any]:
    create_payload = {
        "image_b64": base64.b64encode(image_path.read_bytes()).decode("ascii"),
        "filename": image_path.name,
        "include_debug": True,
    }
    create_response = request_json(
        api_url=api_url,
        api_token=api_token,
        path="/predictions",
        method="POST",
        payload=create_payload,
        timeout_seconds=timeout_seconds,
    )
    job_id = str(create_response["job_id"])

    while True:
        poll_response = request_json(
            api_url=api_url,
            api_token=api_token,
            path=f"/predictions/{job_id}?wait_ms=8000",
            method="GET",
            payload=None,
            timeout_seconds=timeout_seconds,
        )
        status_value = str(poll_response.get("status"))
        if status_value == "completed":
            result = poll_response.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(f"Completed job {job_id} did not include a result payload.")
            return result
        if status_value == "failed":
            raise RuntimeError(f"Job {job_id} failed: {json.dumps(poll_response, ensure_ascii=True)}")
        if status_value not in {"queued", "processing"}:
            raise RuntimeError(f"Job {job_id} returned an unexpected status: {json.dumps(poll_response, ensure_ascii=True)}")
        time.sleep(0.1)


def request_json(
    *,
    api_url: str,
    api_token: str,
    path: str,
    method: str,
    payload: dict[str, Any] | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    encoded = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(
        url=f"{api_url}{path}",
        data=encoded,
        method=method,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
        },
    )
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"message": body}
        raise RuntimeError(f"HTTP {exc.code}: {json.dumps(parsed, ensure_ascii=True)}") from exc


def log(message: str) -> None:
    try:
        print(message, flush=True)
    except OSError:
        try:
            sys.stderr.write(message + "\n")
            sys.stderr.flush()
        except OSError:
            pass


def summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    usable_count = sum(1 for row in results if row.get("image_usable") is True)
    scored_rows = [row for row in results if row.get("overall_score") is not None]
    medium_counts = Counter(str(row.get("medium")) for row in results)
    score_counts = Counter(int(row["overall_score"]) for row in scored_rows)
    avg_overall = None
    if scored_rows:
        avg_overall = round(sum(int(row["overall_score"]) for row in scored_rows) / len(scored_rows), 2)
    return {
        "successful_predictions": len(results),
        "usable_images": usable_count,
        "scored_images": len(scored_rows),
        "average_overall_score": avg_overall,
        "medium_counts": dict(sorted(medium_counts.items())),
        "overall_score_counts": dict(sorted(score_counts.items())),
    }


def render_summary_lines(summary: dict[str, Any]) -> list[str]:
    lines = [
        f"- successful_predictions: `{summary['successful_predictions']}`",
        f"- usable_images: `{summary['usable_images']}`",
        f"- scored_images: `{summary['scored_images']}`",
        f"- average_overall_score: `{summary['average_overall_score']}`",
    ]

    medium_counts = summary["medium_counts"]
    if medium_counts:
        lines.append("- medium_counts:")
        for key, value in medium_counts.items():
            lines.append(f"  - `{key}`: `{value}`")

    score_counts = summary["overall_score_counts"]
    if score_counts:
        lines.append("- overall_score_counts:")
        for key, value in score_counts.items():
            lines.append(f"  - `{key}`: `{value}`")

    return lines


def render_result_section(index: int, image_path: Path, result: dict[str, Any], md_dir: Path) -> str:
    rel_image = Path(os.path.relpath(image_path, md_dir)).as_posix()
    lines = [
        f"## {index:02d}. {image_path.name}",
        "",
        f"![{image_path.name}]({rel_image})",
        "",
        f"- path: [{image_path.name}]({image_path.resolve().as_posix()})",
        f"- request_id: `{result.get('request_id')}`",
        f"- model_version: `{result.get('model_version')}`",
        f"- image_usable: `{result.get('image_usable')}`",
        f"- medium: `{result.get('medium')}`",
    ]
    for field in SCORE_FIELDS:
        lines.append(f"- {field}: `{result.get(field)}`")

    debug = result.get("debug") or {}
    if debug:
        for key in sorted(debug):
            lines.append(f"- debug.{key}: `{debug[key]}`")

    lines.append("")
    return "\n".join(lines)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def write_markdown(
    path: Path,
    *,
    api_url: str,
    seed: int,
    requested_count: int,
    attempted_count: int,
    summary: dict[str, Any],
    results: list[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat()
    md_dir = path.parent.resolve()

    sections = [
        "# API Random Sample Report",
        "",
        f"- generated_at_utc: `{generated_at}`",
        f"- api_url: `{api_url}`",
        f"- random_seed: `{seed}`",
        f"- requested_count: `{requested_count}`",
        f"- attempted_files: `{attempted_count}`",
        "",
        "## Summary",
        "",
        *render_summary_lines(summary),
        "",
        "## Samples",
        "",
    ]

    for index, row in enumerate(results, start=1):
        image_path = Path(row["absolute_path"])
        sections.append(render_result_section(index, image_path, row["response"], md_dir))

    path.write_text("\n".join(sections).rstrip() + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    load_dotenv(Path(".env"))
    api_url, api_token = get_api_config()

    images = list_images(args.image_dir.resolve())
    if args.count > len(images):
        raise SystemExit(f"Requested count {args.count} is larger than available images {len(images)}")

    rng = random.Random(args.seed)
    shuffled = images[:]
    rng.shuffle(shuffled)

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    attempted_count = 0

    for image_path in shuffled:
        if len(results) >= args.count:
            break
        attempted_count += 1
        try:
            response = predict_image(api_url, api_token, image_path, args.timeout_seconds)
            results.append(
                {
                    "file": image_path.name,
                    "absolute_path": str(image_path.resolve()),
                    "response": response,
                }
            )
            log(f"[ok] {image_path.name} overall={response.get('overall_score')} medium={response.get('medium')}")
        except Exception as exc:
            errors.append(
                {
                    "file": image_path.name,
                    "absolute_path": str(image_path.resolve()),
                    "error": str(exc),
                }
            )
            log(f"[err] {image_path.name} {exc}")

    if len(results) < args.count:
        raise SystemExit(
            f"Only collected {len(results)} successful predictions after attempting {attempted_count} files."
        )

    summary = summarize_results([row["response"] for row in results])
    json_payload = {
        "api_url": api_url,
        "random_seed": args.seed,
        "requested_count": args.count,
        "attempted_files": attempted_count,
        "summary": summary,
        "results": results,
        "errors": errors,
    }

    write_json(args.output_json.resolve(), json_payload)
    write_markdown(
        args.output_md.resolve(),
        api_url=api_url,
        seed=args.seed,
        requested_count=args.count,
        attempted_count=attempted_count,
        summary=summary,
        results=results,
    )
    log(f"Wrote JSON: {args.output_json.resolve()}")
    log(f"Wrote Markdown: {args.output_md.resolve()}")


if __name__ == "__main__":
    main()
