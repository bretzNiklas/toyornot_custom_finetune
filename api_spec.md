# API Spec

This document describes the live Judge API served from the Ubuntu box by [local_api.py](C:/Users/qwert/Desktop/custom_model/deploy/local_api.py), plus the supported upstream handoff pattern used by the Ubuntu Supabase worker.

Base URL:

- `https://api.piecerate.me`

Important:

- Do not expose the bearer token in browser code.
- The supported public contract is async-first: `POST /predictions` plus `GET /predictions/{job_id}`.
- `POST /predict` still exists for internal/manual server use, but it is not the supported upstream integration contract.
- The supported upstream flow is `public.judge_api_jobs` row -> Ubuntu handoff worker -> Judge API `/predictions` -> `public.judge_api_results`.

## Authentication

Every request must include:

```http
Authorization: Bearer <YOUR_SECRET_TOKEN>
```

## Supported Endpoints

- `GET /health`
- `POST /predictions`
- `GET /predictions/{job_id}`

## Health Endpoint

Method:

```http
GET /health
```

Success response:

```json
{
  "status": "ok",
  "model_version": "student-v2-dinov2",
  "app": "graffiti-student-local",
  "queued_jobs": 0,
  "processing_jobs": 0,
  "oldest_queued_age_seconds": null,
  "average_processing_seconds": 5.0,
  "worker_concurrency": 1,
  "fresh_worker_count": 1,
  "worker_heartbeat_age_seconds": 1.2,
  "worker_heartbeat_fresh": true
}
```

If the local queue worker heartbeat is stale, the endpoint returns `503`.

## Create Prediction Job

Method:

```http
POST /predictions
```

Headers:

```http
Authorization: Bearer <YOUR_SECRET_TOKEN>
Content-Type: application/json
```

Request body:

```json
{
  "image_b64": "<base64-encoded-image>",
  "filename": "example.jpg",
  "include_debug": false
}
```

Accepted response:

```json
{
  "job_id": "uuid-here",
  "request_id": "uuid-here",
  "status": "queued",
  "queue_position": 1,
  "estimated_wait_seconds": 5,
  "poll_url": "https://api.piecerate.me/predictions/uuid-here",
  "model_version": "student-v2-dinov2"
}
```

Overload response:

```json
{
  "error": "queue_overloaded",
  "message": "The prediction queue is currently too busy. Retry later.",
  "request_id": "uuid-here",
  "model_version": "student-v2-dinov2",
  "job_id": "uuid-here",
  "queue_position": 18,
  "estimated_wait_seconds": 97
}
```

`Retry-After` is returned on overload responses.

## Poll Prediction Job

Method:

```http
GET /predictions/{job_id}
```

Optional long-poll:

```http
GET /predictions/{job_id}?wait_ms=8000
```

Queued or processing response:

```json
{
  "job_id": "uuid-here",
  "request_id": "uuid-here",
  "status": "queued",
  "model_version": "student-v2-dinov2",
  "queue_position": 2,
  "estimated_wait_seconds": 9
}
```

Completed response:

```json
{
  "job_id": "uuid-here",
  "request_id": "uuid-here",
  "status": "completed",
  "model_version": "student-v2-dinov2",
  "result": {
    "filename": "piece.jpg",
    "image_usable": true,
    "medium": "wall_piece",
    "overall_score": 7,
    "legibility": 6,
    "letter_structure": 7,
    "line_quality": 7,
    "composition": 7,
    "color_harmony": 7,
    "originality": 7,
    "request_id": "uuid-here",
    "model_version": "student-v2-dinov2"
  }
}
```

Failed response:

```json
{
  "job_id": "uuid-here",
  "request_id": "uuid-here",
  "status": "failed",
  "model_version": "student-v2-dinov2",
  "error": "invalid_image",
  "message": "The uploaded content is not a valid image."
}
```

## Request And Result Fields

Request fields:

- `image_b64` required base64 string of the image bytes
- `filename` optional file name echoed back in successful results
- `include_debug` optional boolean for internal/manual scoring only

Result fields:

- `image_usable`
- `medium`
- `overall_score`
- `legibility`
- `letter_structure`
- `line_quality`
- `composition`
- `color_harmony`
- `originality`
- `request_id`
- `model_version`

`medium` values:

- `paper_sketch`
- `wall_piece`
- `digital`
- `other_or_unclear`

Scores are integers from `1` to `10` when applicable and may be `null` when the image is unusable or outside the core scoring domain.

## Structured Error Payload

Errors return JSON like:

```json
{
  "error": "invalid_image",
  "message": "The uploaded content is not a valid image.",
  "request_id": "uuid-here",
  "model_version": "student-v2-dinov2"
}
```

Common codes:

- `invalid_base64`
- `invalid_image`
- `image_too_large`
- `image_too_small`
- `image_too_large_dimensions`
- `queue_overloaded`
- `job_not_found`
- `internal_error`

## Upstream Worker Pattern

The supported end-to-end contract is:

1. Vercel `/api/rate` uploads `judgeImage` to Supabase Storage.
2. Vercel inserts a `pending` row into `public.judge_api_jobs`.
3. `deploy/judge_api_handoff_worker.py` claims the row, downloads the storage object, and calls `POST /predictions`.
4. The worker polls `GET /predictions/{job_id}?wait_ms=8000` until the Judge API reaches `completed` or `failed`.
5. The worker archives the judged image on local disk, upserts `public.judge_api_results` with that archive reference, marks the job row terminal, and deletes the transient Supabase storage object.

The worker is the only process that writes `public.judge_api_results`.
