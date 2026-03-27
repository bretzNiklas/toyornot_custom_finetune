#!/usr/bin/env bash
set -euo pipefail

source .venv/bin/activate

python scripts/build_student_dataset_artifacts.py

python scripts/train_student_model.py \
  --stage stage_a \
  --train-manifest exports/student/v1/stage_a_train_v1.jsonl \
  --val-manifest exports/student/v1/human_val_locked_v1.jsonl \
  --output-dir runs/student_v1/stage_a
