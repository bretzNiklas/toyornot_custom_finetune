# Student Cloud Workflow

## 1. Prepare the cloud training machine

Default target: a single `24 GB` GPU Pod on RunPod.

On the Pod:

```bash
git clone <your-repo-url>
cd custom_model
bash deploy/runpod/bootstrap_train_env.sh
```

## 2. Build the new student splits

```bash
source .venv/bin/activate
python scripts/build_student_dataset_artifacts.py
```

This writes the training manifests to [exports/student/v1/README.md](C:/Users/qwert/Desktop/custom_model/exports/student/v1/README.md) after the script has run.

## 3. Train Stage A

```bash
source .venv/bin/activate
python scripts/train_student_model.py \
  --stage stage_a \
  --train-manifest exports/student/v1/stage_a_train_v1.jsonl \
  --val-manifest exports/student/v1/human_val_locked_v1.jsonl \
  --output-dir runs/student_v1/stage_a
```

Stage A uses human-train plus teacher rows, with teacher rows downweighted.

## 4. Train Stage B

```bash
source .venv/bin/activate
python scripts/train_student_model.py \
  --stage stage_b \
  --train-manifest exports/student/v1/human_train_v1.jsonl \
  --val-manifest exports/student/v1/human_val_locked_v1.jsonl \
  --output-dir runs/student_v1/stage_b \
  --resume-from runs/student_v1/stage_a/best_bundle
```

Stage B refines only on human-train rows.

## 5. Evaluate on locked human test

```bash
source .venv/bin/activate
python scripts/evaluate_student_model.py \
  --model-dir runs/student_v1/stage_b/best_bundle \
  --manifest exports/student/v1/human_test_locked_v1.jsonl \
  --output runs/student_v1/stage_b/test_metrics.json
```

## 6. Package for Hugging Face Inference Endpoints

```bash
source .venv/bin/activate
python scripts/package_hf_endpoint_bundle.py \
  --model-dir runs/student_v1/stage_b/best_bundle \
  --outdir runs/student_v1/hf_bundle
```

## 7. Push to a private Hugging Face model repo

Set `HF_TOKEN` first, then run:

```bash
source .venv/bin/activate
python scripts/push_bundle_to_hub.py \
  --bundle-dir runs/student_v1/hf_bundle \
  --repo-id <your-user-or-org>/graffiti-student-v1 \
  --private
```

## 8. Create the endpoint

Create a protected Hugging Face Inference Endpoint pointing at that model repo. The packaged bundle already includes:

- `handler.py`
- `requirements.txt`
- `model.safetensors`
- `student_config.json`
- `thresholds.json`

Request shape:

```json
{
  "inputs": {
    "image_b64": "<base64-encoded-image>",
    "filename": "example.jpg",
    "include_debug": false
  }
}
```

Response shape:

```json
{
  "filename": "example.jpg",
  "image_usable": true,
  "medium": "paper_sketch",
  "overall_score": 6,
  "legibility": 6,
  "letter_structure": 6,
  "line_quality": 7,
  "composition": 5,
  "color_harmony": null,
  "originality": 5
}
```
