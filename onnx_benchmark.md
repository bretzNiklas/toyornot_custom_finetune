# ONNX Benchmark

This benchmark compares the winning student bundle in two CPU modes:

- PyTorch FP32 CPU
- ONNX Runtime FP32 CPU

It exports the trained student model to ONNX, then evaluates both variants on the locked human test set.

## Install

If the environment does not already include ONNX packages:

```bash
pip install onnx onnxruntime
```

## Run

From the repo root on the training box:

```bash
python scripts/benchmark_student_onnx.py \
  --model-dir runs/benchmarks/dinov2_base_224/stage_b/best_bundle \
  --manifest exports/student/v1/human_test_locked_v1.jsonl \
  --output runs/onnx/dinov2_base_224/report.json
```

## Output

The report contains:

- `bundle_sizes.pytorch_safetensors_bytes`
- `bundle_sizes.onnx_bytes`
- `pytorch_fp32_cpu.metrics`
- `pytorch_fp32_cpu.latency`
- `onnx_fp32_cpu.metrics`
- `onnx_fp32_cpu.latency`

## Decision Rule

ONNX FP32 is a good deployment candidate if:

- its metrics stay effectively aligned with PyTorch FP32
- CPU latency is better enough to matter on the intended host
- startup and memory behavior are easier to manage for serving

If ONNX FP32 looks good, the next step is ONNX Runtime INT8 quantization.
