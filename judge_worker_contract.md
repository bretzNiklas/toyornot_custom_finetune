# Judge Worker Contract

This repository now supports the full Ubuntu-side worker handoff for Judge API jobs.

## Flow

1. Vercel `/api/rate` uploads the `judgeImage` input to Supabase Storage.
2. Vercel inserts a `pending` row into `public.judge_api_jobs`.
3. `deploy/judge_api_handoff_worker.py` claims the job from Supabase.
4. The worker downloads the image, calls `POST /predictions`, and polls `GET /predictions/{job_id}?wait_ms=8000`.
5. The worker archives the judged image on local disk, upserts `public.judge_api_results` with that local image reference, updates the job row to `completed` or `failed`, and deletes the transient Supabase storage object.

## Worker Environment

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `JUDGE_API_TOKEN`
- optional `JUDGE_API_BASE_URL` default `https://api.piecerate.me`
- optional `JUDGE_API_TIMEOUT_MS` default `30000`
- optional `SUPABASE_JUDGE_API_JOBS_TABLE` default `judge_api_jobs`
- optional `SUPABASE_JUDGE_API_RESULTS_TABLE` default `judge_api_results`
- optional `SUPABASE_JUDGE_API_INPUT_BUCKET` default `judge-api-inputs`
- optional `JUDGED_IMAGE_ARCHIVE_DIR` default `/srv/graffiti-student/runtime/judged-images`
- optional `WORKER_ID` default current hostname
- optional `JUDGE_JOB_LOCK_TIMEOUT_SECONDS` default `600`
- optional `JUDGE_JOB_POLL_WAIT_MS` default `8000`
- optional `JUDGE_JOB_IDLE_SLEEP_SECONDS` default `1`
- optional `JUDGE_JOB_MAX_ATTEMPTS` default `5`
- optional `JUDGE_JOB_BACKOFF_SCHEDULE_SECONDS` default `30,120,600,600`

## Job Semantics

- The worker claims one ready row at a time through the Supabase RPC `claim_next_judge_api_job`.
- Stale `claimed` or `processing` rows are reclaimable once `locked_at` is older than the configured lock timeout.
- If a reclaimed `processing` row already has a `piecerate_job_id`, the worker resumes polling that remote Piecerate job instead of uploading the image again.
- `request_id` is the idempotency key for `public.judge_api_results`.
- Cache hits must be handled upstream and must not enqueue a new job.

## Result Semantics

Each result row persists:

- normalized Judge API result fields
- `judge_api_job_id`
- `judge_api_request_id`
- `judge_api_model_version`
- `judge_api_http_status`
- local source image metadata including archive path and filename under `response_payload.source_image` or `error_payload.source_image`
- `response_payload` on success or `error_payload` on failure

Raw image bytes and `image_b64` are never written to Supabase tables.
