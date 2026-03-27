#!/usr/bin/env bash
set -euo pipefail

source .venv/bin/activate

python scripts/train_student_model.py \
  --stage stage_b \
  --train-manifest exports/student/v1/human_train_v1.jsonl \
  --val-manifest exports/student/v1/human_val_locked_v1.jsonl \
  --output-dir runs/student_v1/stage_b \
  --resume-from runs/student_v1/stage_a/best_bundle

python scripts/evaluate_student_model.py \
  --model-dir runs/student_v1/stage_b/best_bundle \
  --manifest exports/student/v1/human_test_locked_v1.jsonl \
  --output runs/student_v1/stage_b/test_metrics.json

python scripts/package_hf_endpoint_bundle.py \
  --model-dir runs/student_v1/stage_b/best_bundle \
  --outdir runs/student_v1/hf_bundle
