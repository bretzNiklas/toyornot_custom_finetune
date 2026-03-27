# Benchmark Suite

If you want to run the main candidate student models without pasting each command manually, use the suite runner.

From the repo root on the pod:

```bash
git pull origin main
python scripts/build_student_dataset_artifacts.py
python scripts/run_all_student_benchmarks.py
```

That sequentially fine-tunes and evaluates:

- `vit_base_384`
- `dinov2_base_224`
- `convnextv2_tiny_224`
- `efficientnet_b0_224`

Each run performs:

1. Stage A fine-tuning
2. Stage B fine-tuning
3. locked human test evaluation

Outputs land in:

```text
runs/benchmarks/<preset>/report.json
```

The suite writes a top-level run summary to:

```text
runs/benchmarks/suite_summary.json
```

## Optional Flags

Include the original baseline too:

```bash
python scripts/run_all_student_benchmarks.py --include-baseline
```

Keep going even if one model fails:

```bash
python scripts/run_all_student_benchmarks.py --continue-on-error
```
