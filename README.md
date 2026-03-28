# Graffiti Scoring Model

This repository contains an end-to-end pipeline for building a custom vision model that scores graffiti and graffiti sketches.

The project started from a raw folder of unlabeled images and ended with:

- a cleaned and reviewed training dataset
- a teacher-labeling pipeline using an LLM for scalable prefill
- a smaller student vision model fine-tuned for this task
- a cloud deployment path with a protected API

The core goal was to build a graffiti-specific scoring model that is cheaper and faster to run than a large multimodal model, while still reflecting a human-defined rubric.

## What The Model Does

Given an image, the model predicts:

- `image_usable`
- `medium`
- `overall_score`
- `legibility`
- `letter_structure`
- `line_quality`
- `composition`
- `color_harmony`
- `originality`

The deployed API uses `image_usable` as a gate:

- if an image is unusable, no scores are returned
- if an image is usable but out of scoring scope, scores are returned as `null`
- if an image is a usable paper sketch or wall piece, the full score bundle is returned

## Problem Framing

This was not approached as a generic art-rating problem.

The pipeline was designed around graffiti-specific judging criteria, with emphasis on:

- technical execution
- lettering and structure
- composition
- color use when applicable
- originality

The rubric was deliberately simplified into repeatable visual categories so that it could be applied consistently by both humans and the teacher model.

## Dataset And Labeling

The raw dataset contained `1496` images.

Final merged dataset:

- `1496` total rows
- `1460` usable
- `36` unusable
- `573` human-quality rows
- `923` teacher-labeled rows

Usable image mix:

- `871` paper sketches
- `461` wall pieces
- `105` digital
- `23` other / unclear

The labeling workflow had three stages:

1. Human rubric definition and initial manual labels in Label Studio
2. Teacher-model batch labeling with human review of risky cases
3. Merge of original human labels, reviewed teacher labels, and raw teacher predictions

Character-only pieces were explicitly removed from the core scoring scope because they did not fit the letter-centric rubric cleanly.

## Teacher Phase

The teacher phase used OpenRouter with a tuned Gemini Lite vision model to prefill labels at scale.

Why a teacher was used:

- manual labeling of the full dataset would be slow
- the teacher can provide dense rubric labels for every image
- human review can then focus on high-risk cases instead of every single row

Teacher pilot findings on locked human-eval rows:

- `100%` usable-image accuracy
- `92.5%` medium accuracy
- `62.5%` piece-type accuracy
- `47.5%` overall bucket accuracy
- `1.10` overall-score MAE

That was strong enough to use for prefill, but not strong enough to trust blindly. The risky subset was therefore reviewed by hand before merging.

## Student Model

The final student is a multi-head vision model, not a set of separate one-task models.

Architecture:

- winning backbone: `DINOv2 Base`
- LoRA-based fine-tuning during training
- shared backbone with multiple prediction heads

Training strategy:

- Stage A: weak-supervision training on human-train plus teacher-labeled rows
- Stage B: refinement on human-only training rows
- locked validation and test splits are human-only

Training split used for the student:

- `401` human train
- `86` human validation
- `86` human test
- `923` teacher rows available for Stage A

Digital and unclear images remain in the dataset for `medium` learning, but score losses are masked outside the core scoring domain.

## Final Result

The originally deployed baseline was a `ViT-base` student, but further benchmarking found a clearly better production model:

- winning model: `student-v2-dinov2`
- backbone: `facebook/dinov2-base`

Locked human test metrics for the winning model:

- `image_usable` accuracy: `0.988`
- `image_usable` precision: `0.988`
- `image_usable` recall: `1.000`
- `image_usable` F1: `0.994`
- `medium` accuracy: `0.815`
- `overall_score` MAE: `0.710`
- `overall_band_accuracy`: `0.710`
- `paper_sketch` overall-score MAE: `0.622`
- `wall_piece` overall-score MAE: `0.840`

Rubric MAE:

- `legibility`: `1.242`
- `letter_structure`: `0.887`
- `line_quality`: `1.194`
- `composition`: `1.016`
- `color_harmony`: `1.103`
- `originality`: `0.887`

Model ranking from the benchmark run:

1. `dinov2_base_224`
2. `vit_base_384`
3. original `vit_base_224`
4. `convnextv2_tiny_224`

The strongest outputs remain:

- `image_usable`
- `overall_score`

`medium` is much better in the DINOv2 model than in the original baseline, but it is still secondary to the main score.

## Deployment

Training was done on a rented cloud GPU box, but the final production deployment does not require cloud GPU hosting.

The live production path is:

- model host: local Ubuntu server
- inference stack: FastAPI + uvicorn
- reverse proxy: nginx
- public exposure: Cloudflare named tunnel
- public hostname: `https://api.piecerate.me`
- deployment flow: GitHub push -> SSH deploy -> server-side `git checkout` + service restart

This replaced the earlier Modal deployment path after benchmarking showed the local CPU host was fast enough and dramatically cheaper.

The supported integration path is direct and synchronous:

1. frontend uploads an image to your backend
2. your backend sends `POST /predict` with `image_b64`
3. the API returns the rating JSON immediately

This repo does not include an async rating queue or status-polling layer anymore.

Measured local CPU inference on the production host:

- first hit: about `805 ms`
- warm average: about `807 ms`
- p95 warm: about `810 ms`

Current endpoint hardening includes:

- bearer-token authentication
- request size limits
- image validation
- structured error responses
- health endpoint
- request ids
- model version tagging

## Repository Structure

High-signal files and directories:

- [graffiti_sketch_rubric_v1.md](C:/Users/qwert/Desktop/custom_model/graffiti_sketch_rubric_v1.md)  
  Human rubric used for scoring
- [teacher_prompt_v1.md](C:/Users/qwert/Desktop/custom_model/teacher_prompt_v1.md)  
  Teacher-model prompt specification
- [label_studio](C:/Users/qwert/Desktop/custom_model/label_studio)  
  Label Studio config and review task generation
- [exports/final/training_pool_v1.jsonl](C:/Users/qwert/Desktop/custom_model/exports/final/training_pool_v1.jsonl)  
  Final merged dataset
- [exports/student/v1](C:/Users/qwert/Desktop/custom_model/exports/student/v1)  
  Human-only locked splits and Stage A training manifests
- [student](C:/Users/qwert/Desktop/custom_model/student)  
  Student model, trainer, inference, and metrics code
- [scripts](C:/Users/qwert/Desktop/custom_model/scripts)  
  Data prep, conversion, training, evaluation, and packaging scripts
- [deploy/local_api.py](C:/Users/qwert/Desktop/custom_model/deploy/local_api.py)  
  Local Ubuntu API entrypoint
- [deploy/ubuntu/deploy_remote.sh](C:/Users/qwert/Desktop/custom_model/deploy/ubuntu/deploy_remote.sh)  
  Server-side pull deploy script invoked by GitHub Actions
- [.github/workflows/deploy-production.yml](C:/Users/qwert/Desktop/custom_model/.github/workflows/deploy-production.yml)  
  Production deploy pipeline for pushes to `main`
- [api_spec.md](C:/Users/qwert/Desktop/custom_model/api_spec.md)  
  API contract for frontend/backend integration

## Reproducing The Pipeline

At a high level:

1. Label an initial seed set in Label Studio
2. Build anchor examples and a locked human evaluation set
3. Run teacher-model batch labeling
4. Review risky teacher outputs
5. Merge human and teacher labels
6. Build student manifests
7. Train Stage A and Stage B
8. Evaluate on the human-only locked test set
9. Package and deploy the model

Operational docs:

- [modal_deployment.md](C:/Users/qwert/Desktop/custom_model/modal_deployment.md)
- [deploy_local_ubuntu.md](C:/Users/qwert/Desktop/custom_model/deploy_local_ubuntu.md)
- [production_setup_reference.md](C:/Users/qwert/Desktop/custom_model/production_setup_reference.md)
- [api_spec.md](C:/Users/qwert/Desktop/custom_model/api_spec.md)

## Limitations

- `medium` classification is materially weaker than score prediction
- the model is strongest on paper sketches and wall pieces, not digital or ambiguous images
- this is a domain-specific scoring model, not a general-purpose art model
- the rubric is intentionally practical and repeatable, which means it does not capture every cultural nuance of graffiti evaluation

## Why This Project Matters

This repository demonstrates a complete applied ML workflow:

- problem framing for a niche visual domain
- human-in-the-loop labeling design
- LLM-based weak supervision
- dataset review and split discipline
- multi-head fine-tuning of a smaller vision model
- model benchmarking across multiple backbones
- practical cost-driven deployment migration from cloud GPU serving to self-hosted CPU inference
- production-style API deployment with authentication and stable public routing

The key result is not just a trained model. It is a full system for turning a messy, unlabeled visual dataset into a usable, deployable domain model with explicit tradeoffs and measurable performance.
