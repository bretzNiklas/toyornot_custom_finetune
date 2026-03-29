# Judge API Quick Spec

The public Judge API is now async-first and queue-backed.

Base URL:

- `https://api.piecerate.me`

All endpoints require:

```http
Authorization: Bearer <YOUR_SECRET_TOKEN>
```

Do not expose this token in browser code.

## Public Endpoints

### `POST /predictions`

Create a queued prediction job.

Request body:

```json
{
  "image_b64": "<base64 image bytes>",
  "filename": "piece.jpg",
  "include_debug": false
}
```

Success response (`202 Accepted`):

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

Overload response (`429 Too Many Requests`):

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

`Retry-After` is returned in the response headers.

### `GET /predictions/{job_id}`

Read job status or final result.

Queued or processing example:

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

Completed example:

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

Failed example:

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

Optional long-poll:

- `GET /predictions/{job_id}?wait_ms=8000`

Use this to wait up to `8000 ms` for a state change before polling again.

### `GET /health`

Returns API and queue health.

Important fields:

- `queued_jobs`
- `processing_jobs`
- `oldest_queued_age_seconds`
- `worker_concurrency`
- `fresh_worker_count`
- `worker_heartbeat_age_seconds`
- `worker_heartbeat_fresh`

If the worker heartbeat is stale, the endpoint returns `503`.

## Internal-Only Endpoint

### `POST /predict`

This still exists for local/manual use on the server, but it is no longer the supported public contract.

External callers should use:

1. `POST /predictions`
2. `GET /predictions/{job_id}`

## Frontend Pattern

Recommended flow:

1. Upload the image to your backend.
2. Base64-encode the raw file bytes.
3. Call `POST /predictions`.
4. Wait your initial UI delay.
5. Poll `GET /predictions/{job_id}?wait_ms=8000` until the job is `completed` or `failed`.
