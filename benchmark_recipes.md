# Benchmark Recipes

This file contains the quickest way to benchmark stronger student-model variants on the existing RunPod box.

## Before You Start

From the repo root on the pod:

```bash
git pull origin main
```

Make sure the student manifests already exist:

```bash
python scripts/build_student_dataset_artifacts.py
```

## Recommended Candidate Set

Once the pod is up, the main cost is time on the same GPU. That means you can benchmark multiple backbones without paying a separate model fee.

These are the candidate runs worth trying:

- `vit_base_224`
- `vit_base_384`
- `dinov2_base_224`
- `convnextv2_tiny_224`
- `efficientnet_b0_224`

## Recommended First Two Experiments

### 1. Higher-resolution ViT baseline

This keeps the same backbone but gives the model more detail to work with.

```bash
python scripts/run_student_experiment.py \
  --preset vit_base_384 \
  --name vit_base_384
```

### 2. DINOv2 base

This is the strongest alternative worth trying first for transfer quality.

```bash
python scripts/run_student_experiment.py \
  --preset dinov2_base_224 \
  --name dinov2_base_224
```

## Additional Candidates

### 3. ConvNeXt V2 tiny

```bash
python scripts/run_student_experiment.py \
  --preset convnextv2_tiny_224 \
  --name convnextv2_tiny_224
```

### 4. EfficientNet B0

```bash
python scripts/run_student_experiment.py \
  --preset efficientnet_b0_224 \
  --name efficientnet_b0_224
```

## Optional Baseline Re-run

If you want a clean benchmark produced by the new one-command runner:

```bash
python scripts/run_student_experiment.py \
  --preset vit_base_224 \
  --name vit_base_224
```

## Where Results Land

Each run writes to:

```text
runs/benchmarks/<experiment_name>/
```

Important files:

- `stage_a/best_bundle`
- `stage_b/best_bundle`
- `report.json`

## How To Compare

After each run, inspect:

```bash
cat runs/benchmarks/vit_base_384/report.json
cat runs/benchmarks/dinov2_base_224/report.json
```

Focus on:

- `image_usable.recall`
- `overall_score_mae`
- `overall_score_by_medium`
- `rubric_mae`

## Decision Rule

For this project, the better model is the one that:

1. keeps `image_usable` recall at or above the current model
2. lowers `overall_score_mae`
3. does not get much worse on wall pieces

If `medium_accuracy` changes but `overall_score_mae` improves, prefer the better scoring model.
