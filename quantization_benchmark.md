# Quantization Benchmark

This benchmark compares the winning student bundle in two CPU modes:

- FP32 CPU
- dynamic INT8 CPU (`torch.ao.quantization.quantize_dynamic` on `nn.Linear`)

It measures:

- locked human test metrics
- single-image CPU latency
- approximate model size reduction

## Run

From the repo root on the training box:

```bash
python scripts/benchmark_student_quantization.py \
  --model-dir runs/benchmarks/dinov2_base_224/stage_b/best_bundle \
  --manifest exports/student/v1/human_test_locked_v1.jsonl \
  --output runs/quantization/dinov2_base_224/report.json
```

## Output

The report contains:

- `bundle_sizes.fp32_safetensors_bytes`
- `bundle_sizes.dynamic_int8_state_dict_bytes`
- `fp32_cpu.metrics`
- `fp32_cpu.latency`
- `dynamic_int8_cpu.metrics`
- `dynamic_int8_cpu.latency`

## Decision Rule

Quantization is a viable deployment path if:

- `overall_score_mae` stays close to the FP32 result
- `image_usable.recall` stays high
- warm single-image CPU latency lands in the target range for the intended host

If INT8 is materially faster with only a small metric drop, the next step is to package a CPU-serving deployment around the quantized variant.
