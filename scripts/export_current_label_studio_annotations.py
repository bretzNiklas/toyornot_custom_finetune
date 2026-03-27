from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from convert_label_studio_export import flatten_task


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export the latest Label Studio annotations from the local SQLite DB to JSONL."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("label_studio/.data/label_studio.sqlite3"),
        help="Path to the Label Studio SQLite database.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("exports/current_labels.jsonl"),
        help="Output JSONL path.",
    )
    return parser.parse_args()


def iter_latest_annotated_tasks(con: sqlite3.Connection):
    query = """
        SELECT
            t.id AS task_id,
            t.data AS task_data,
            tc.id AS annotation_id,
            tc.result AS annotation_result,
            tc.created_at AS created_at,
            tc.updated_at AS updated_at
        FROM task AS t
        JOIN (
            SELECT task_id, MAX(updated_at) AS max_updated_at
            FROM task_completion
            GROUP BY task_id
        ) AS latest
            ON latest.task_id = t.id
        JOIN task_completion AS tc
            ON tc.task_id = latest.task_id
           AND tc.updated_at = latest.max_updated_at
        ORDER BY t.id
    """
    for row in con.execute(query):
        task_id, task_data, annotation_id, annotation_result, created_at, updated_at = row
        yield {
            "id": task_id,
            "data": json.loads(task_data),
            "annotations": [
                {
                    "id": annotation_id,
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "result": json.loads(annotation_result),
                }
            ],
        }


def main() -> None:
    args = parse_args()
    db_path = args.db.resolve()
    if not db_path.exists():
        raise SystemExit(f"Label Studio DB not found: {db_path}")

    con = sqlite3.connect(db_path)
    rows = []
    try:
        for task in iter_latest_annotated_tasks(con):
            flattened = flatten_task(task)
            if flattened is not None:
                rows.append(flattened)
    finally:
        con.close()

    output_path = args.output.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")

    print(f"Wrote {len(rows)} labeled rows to {output_path}")


if __name__ == "__main__":
    main()
