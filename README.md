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

- `ViT-base` image encoder
- LoRA-based fine-tuning
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

The current deployed `v1` student met the main scoring targets on the locked human test set.

Test metrics:

- `image_usable` accuracy: `0.977`
- `image_usable` precision: `0.976`
- `image_usable` recall: `1.000`
- `image_usable` F1: `0.988`
- `overall_score` MAE: `1.016`
- `overall_band_accuracy`: `0.597`
- `paper_sketch` overall-score MAE: `0.892`
- `wall_piece` overall-score MAE: `1.200`

Rubric MAE:

- `legibility`: `1.532`
- `letter_structure`: `1.129`
- `line_quality`: `1.710`
- `composition`: `1.323`
- `color_harmony`: `1.184`
- `originality`: `1.194`

The weakest head is `medium`, with accuracy around `0.52`, so in the current product shape it should be treated as supporting metadata rather than the primary product output.

The strongest outputs are:

- `image_usable`
- `overall_score`

## Deployment

Training was designed for a rented cloud GPU box rather than a local desktop GPU.

Serving was implemented with two options:

- Hugging Face Inference Endpoints
- Modal

The cheaper deployed path is Modal, using:

- a packaged trained model bundle
- a custom Python predictor
- a protected POST API

Current endpoint hardening includes:

- bearer-token authentication
- request size limits
- image validation
- structured error responses
- health endpoint
- request ids
- model version tagging

Measured live end-to-end inference latency on the deployed Modal endpoint was about `0.9s` per request.

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
- [deploy/modal_app.py](C:/Users/qwert/Desktop/custom_model/deploy/modal_app.py)  
  Modal deployment entrypoint
- [modal_api_spec.md](C:/Users/qwert/Desktop/custom_model/modal_api_spec.md)  
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

- [student_cloud_workflow.md](C:/Users/qwert/Desktop/custom_model/student_cloud_workflow.md)
- [modal_deployment.md](C:/Users/qwert/Desktop/custom_model/modal_deployment.md)
- [modal_api_spec.md](C:/Users/qwert/Desktop/custom_model/modal_api_spec.md)

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
- cloud deployment with a production-oriented API contract

The key result is not just a trained model. It is a full system for turning a messy, unlabeled visual dataset into a usable, deployable domain model with explicit tradeoffs and measurable performance.
