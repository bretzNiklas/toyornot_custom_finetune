# Local Ubuntu Deployment

This deploys the winning `student-v2-dinov2` model on a local Ubuntu box using:

- FastAPI
- uvicorn
- systemd
- nginx

## 1. System packages

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip nginx
```

## 2. Clone and install

```bash
git clone https://github.com/bretzNiklas/toyornot_custom_finetune.git
cd toyornot_custom_finetune
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements-serve.txt
```

## 3. Download the model bundle

Export a Hugging Face token with access to the private model repo:

```bash
export HF_TOKEN=your_hf_token_here
```

Then download the model:

```bash
python - <<'PY'
import os
from huggingface_hub import snapshot_download

snapshot_download(
    repo_id="qwertzniki/graffiti-student-dinov2-base-224",
    repo_type="model",
    local_dir="models/dinov2_base_224",
    token=os.environ["HF_TOKEN"],
)
print("Downloaded model to models/dinov2_base_224")
PY
```

## 4. Local env file

Create `.env.local` in the repo root:

```bash
cat > .env.local <<'EOF'
AUTH_TOKEN=replace_with_your_secret_token
MODEL_VERSION=student-v2-dinov2
MODEL_DIR=/home/niklas/toyornot_custom_finetune/models/dinov2_base_224
SUPABASE_DB_URL=postgresql://postgres.your-project:password@db.your-project.supabase.co:5432/postgres
# Optional: use a Supavisor session pooler URL instead of the direct DB URL for the worker.
# SUPABASE_SESSION_POOLER_URL=postgresql://postgres.your-project:password@aws-0-region.pooler.supabase.com:5432/postgres
RATING_QUEUE_NAME=rating_dispatch
RATING_QUEUE_NOTIFY_CHANNEL=rating_queue_wakeup
RATING_QUEUE_BATCH_SIZE=25
RATING_QUEUE_VISIBILITY_TIMEOUT_SECONDS=300
RATING_QUEUE_STALE_AFTER_SECONDS=300
RATING_QUEUE_IDLE_RECONCILE_SECONDS=300
RATING_QUEUE_MAX_RETRIES=3
GRAFFITI_API_URL=http://127.0.0.1:8000
GRAFFITI_API_TOKEN=replace_with_same_value_as_AUTH_TOKEN
EOF
```

If `GRAFFITI_API_TOKEN` is omitted, the worker falls back to `AUTH_TOKEN`.
Use a direct Postgres URL or a Supavisor session pooler URL. Do not use the transaction pooler for the always-on worker because `LISTEN/NOTIFY` requires a persistent session.

## 4b. Supabase queue bootstrap

The Ubuntu worker now uses:

- `public.rating_jobs` as the durable status table for Vercel `/api/rate-status`
- `public.complete_rating_job(...)` to atomically mark queued jobs completed and persist one `rating_scores` score-log row
- `pgmq` queue `rating_dispatch` for delivery, retries, and visibility timeouts
- `LISTEN/NOTIFY` on `rating_queue_wakeup` so the worker is event-driven when new jobs arrive

Apply [scripts/supabase-rating-jobs.sql](C:/Users/qwert/Desktop/custom_model/scripts/supabase-rating-jobs.sql)
to create or update:

- `public.rating_jobs`
- `public.rating_jobs.score_log_persisted_at`
- `public.rating_scores.source_job_id`
- `public.enqueue_rating_job(...)`
- `public.complete_rating_job(...)`
- `public.claim_rating_job(...)`
- `public.claim_next_rating_job()` as a temporary rollback helper
- `pgmq` queue `rating_dispatch`

If you are migrating from the old poller, apply the SQL during a quiet window or after the old worker is stopped so no new rows are left without a queue message.
Apply the base `rating_scores` table migration before this queue script because `public.complete_rating_job(...)` inserts queued score-log rows there.

## 5. Smoke test without systemd

Run:

```bash
source .venv/bin/activate
set -a
source .env.local
set +a
uvicorn deploy.local_api:app --host 127.0.0.1 --port 8000
```

In another shell:

```bash
curl -H "Authorization: Bearer replace_with_your_secret_token" http://127.0.0.1:8000/health
```

## 6. systemd service

Copy the provided unit:

```bash
sudo cp deploy/ubuntu/graffiti-student.service /etc/systemd/system/graffiti-student.service
sudo systemctl daemon-reload
sudo systemctl enable --now graffiti-student
sudo systemctl status graffiti-student
```

If your Linux username is not `niklas`, edit the unit file first.

## 6b. Queue worker systemd service

Copy the worker unit and start it:

```bash
sudo cp deploy/ubuntu/toyornot-rating-queue.service /etc/systemd/system/toyornot-rating-queue.service
sudo systemctl daemon-reload
sudo systemctl enable --now toyornot-rating-queue
sudo systemctl status toyornot-rating-queue
```

Useful logs:

```bash
journalctl -u toyornot-rating-queue -n 100 -l --no-pager
```

The worker opens two persistent Postgres sessions:

- one dedicated `LISTEN rating_queue_wakeup`
- one for `pgmq.read()`, job claims, and atomic completion writes

An idle worker should not emit a `PATCH/POST` loop every second anymore. It will sleep until a `NOTIFY`, then drain the queue in batches.

## 7. nginx reverse proxy

```bash
sudo cp deploy/ubuntu/nginx-graffiti-student.conf /etc/nginx/sites-available/graffiti-student
sudo ln -sf /etc/nginx/sites-available/graffiti-student /etc/nginx/sites-enabled/graffiti-student
sudo nginx -t
sudo systemctl reload nginx
```

Now the API is reachable on port `80` on the machine.

## 8. API

Endpoints:

- `GET /health`
- `POST /predict`

Both require:

```http
Authorization: Bearer <AUTH_TOKEN>
```

Prediction body:

```json
{
  "image_b64": "<base64>",
  "filename": "example.jpg",
  "include_debug": false
}
```

## 9. Queue verification

Local health:

```bash
curl -H "Authorization: Bearer <AUTH_TOKEN>" http://127.0.0.1:8000/health
```

Then verify the async flow end to end:

1. Submit one rating through the existing ToyOrNot Vercel `/api/rate` route.
2. Confirm a new row appears in `public.rating_jobs`.
3. Confirm the row was created by `public.enqueue_rating_job(...)` and a message exists in `pgmq.q_rating_dispatch`.
4. Watch the row move `queued -> processing -> completed` or `failed`.
5. Confirm `public.rating_jobs.score_log_persisted_at` is set and one `public.rating_scores` row appears with `source_job_id = public.rating_jobs.id`.
6. Confirm ToyOrNot `/api/rate-status` returns the final payload.
7. Leave the worker idle and confirm the old 1 Hz Supabase polling loop is gone from the journal.

The Ubuntu worker does not add any new public endpoint. ToyOrNot keeps enqueue and status polling; the Ubuntu box owns the always-on worker loop and queued `rating_scores` persistence.

## 10. Migration note

ToyOrNot should not run any separate `worker:rating-queue` daemon from the Vercel-side codebase.

Keep the Vercel `/api/rate` and `/api/rate-status` handlers unchanged, but run the always-on queue worker only through the Ubuntu systemd service:

```bash
sudo systemctl status toyornot-rating-queue
```
