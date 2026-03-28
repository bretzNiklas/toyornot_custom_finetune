# Production Setup Reference

This document records the current production setup for the graffiti scoring API.

It is intended as the single reference for:

- where the model runs
- how the API is served
- how the public hostname is exposed
- how Vercel should call it
- what to check when something breaks

## Current Architecture

The live deployment uses:

- model host: local Ubuntu server
- hardware: HP T640 Terminal, AMD Ryzen R1505G, `8 GB RAM`
- model: `student-v2-dinov2`
- backbone: `facebook/dinov2-base`
- local app server: FastAPI + uvicorn
- local queue worker: Python + systemd + `pgmq` + Postgres `LISTEN/NOTIFY`
- local reverse proxy: nginx
- public exposure: Cloudflare named tunnel
- public API hostname: `https://api.piecerate.me`
- caller: Vercel backend only

This replaced the earlier Modal deployment path because the local CPU host was fast enough and much cheaper.

## Performance

Measured on the production host:

- first hit: about `805 ms`
- warm average: about `807 ms`
- p95 warm: about `810 ms`

Locked human test metrics for the deployed model:

- `image_usable` accuracy: `0.988`
- `image_usable` precision: `0.988`
- `image_usable` recall: `1.000`
- `image_usable` F1: `0.994`
- `medium` accuracy: `0.815`
- `overall_score` MAE: `0.710`
- `overall_band_accuracy`: `0.710`
- `paper_sketch` MAE: `0.622`
- `wall_piece` MAE: `0.840`

## Repository Files That Matter

- [deploy/local_api.py](C:/Users/qwert/Desktop/custom_model/deploy/local_api.py)  
  Public API application
- [deploy/ubuntu/graffiti-student.service](C:/Users/qwert/Desktop/custom_model/deploy/ubuntu/graffiti-student.service)  
  systemd unit for the API
- [deploy/ubuntu/nginx-graffiti-student.conf](C:/Users/qwert/Desktop/custom_model/deploy/ubuntu/nginx-graffiti-student.conf)  
  nginx reverse proxy config
- [api_spec.md](C:/Users/qwert/Desktop/custom_model/api_spec.md)  
  API contract
- [deploy_local_ubuntu.md](C:/Users/qwert/Desktop/custom_model/deploy_local_ubuntu.md)  
  Ubuntu deployment guide
- [student/predictor.py](C:/Users/qwert/Desktop/custom_model/student/predictor.py)  
  Inference logic

## Model Location On Server

The deployed model bundle is stored at:

```text
/home/niklas/toyornot_custom_finetune/models/dinov2_base_224
```

It was downloaded from the private Hugging Face repo:

```text
qwertzniki/graffiti-student-dinov2-base-224
```

## App Runtime

The API app runs from:

```text
/home/niklas/toyornot_custom_finetune
```

The Python environment is:

```text
/home/niklas/toyornot_custom_finetune/.venv
```

The runtime environment file is:

```text
/home/niklas/toyornot_custom_finetune/.env.local
```

Expected contents:

```env
AUTH_TOKEN=replace_with_real_secret
MODEL_VERSION=student-v2-dinov2
MODEL_DIR=/home/niklas/toyornot_custom_finetune/models/dinov2_base_224
SUPABASE_DB_URL=postgresql://postgres.your-project:password@db.your-project.supabase.co:5432/postgres
# Or use a Supavisor session pooler URL for the worker:
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
```

Use a direct Postgres connection or a Supavisor session pooler connection. Do not use the transaction pooler for the worker because `LISTEN/NOTIFY` needs a persistent session.

## Local API

Internal local paths:

- health: `http://127.0.0.1:8000/health`
- predict: `http://127.0.0.1:8000/predict`

nginx proxies public local HTTP:

- `http://127.0.0.1/health`
- `http://127.0.0.1/predict`

The API requires:

```http
Authorization: Bearer <AUTH_TOKEN>
```

## systemd Services

### Graffiti API

Service name:

```text
graffiti-student
```

Useful commands:

```bash
sudo systemctl status graffiti-student --no-pager
sudo systemctl restart graffiti-student
journalctl -u graffiti-student -n 100 -l --no-pager
```

### Rating Queue Worker

Service name:

```text
toyornot-rating-queue
```

Useful commands:

```bash
sudo systemctl status toyornot-rating-queue --no-pager
sudo systemctl restart toyornot-rating-queue
journalctl -u toyornot-rating-queue -n 100 -l --no-pager
```

### Cloudflare Tunnel

Service name:

```text
cloudflared
```

Useful commands:

```bash
sudo systemctl status cloudflared --no-pager
sudo systemctl restart cloudflared
journalctl -u cloudflared -n 100 -l --no-pager
```

## Cloudflare Named Tunnel

Named tunnel:

```text
graffiti-student
```

Tunnel id:

```text
48c94455-a16e-4f64-87cb-41ffc2968224
```

Public hostname:

```text
api.piecerate.me
```

User config file:

```text
/home/niklas/.cloudflared/config.yml
```

System config file:

```text
/etc/cloudflared/config.yml
```

Expected config:

```yaml
tunnel: 48c94455-a16e-4f64-87cb-41ffc2968224
credentials-file: /home/niklas/.cloudflared/48c94455-a16e-4f64-87cb-41ffc2968224.json

ingress:
  - hostname: api.piecerate.me
    service: http://127.0.0.1:80
  - service: http_status:404
```

Useful commands:

```bash
cloudflared tunnel info graffiti-student
cloudflared tunnel route dns graffiti-student api.piecerate.me
```

## Public API

Base URL:

```text
https://api.piecerate.me
```

Endpoints:

- `GET /health`
- `POST /predict`

Example health test:

```bash
curl -H "Authorization: Bearer <AUTH_TOKEN>" https://api.piecerate.me/health
```

Example predict payload:

```json
{
  "image_b64": "<base64-image>",
  "filename": "example.jpg",
  "include_debug": false
}
```

## Vercel Integration

These environment variables should exist in Vercel backend/server environments:

```env
GRAFFITI_API_URL=https://api.piecerate.me
GRAFFITI_API_TOKEN=<AUTH_TOKEN>
```

Important:

- the browser should not call `api.piecerate.me` directly with the secret
- only backend/server code should call the API
- Vercel keeps `/api/rate` and `/api/rate-status`; Ubuntu owns the always-on queue worker and queued score-log persistence

## End-To-End Request Flow

1. User uploads image to the app.
2. Frontend sends image to Vercel backend.
3. Vercel backend converts it to base64 if needed.
4. Vercel backend calls `POST https://api.piecerate.me/predict`.
5. Cloudflare routes traffic through the named tunnel.
6. nginx forwards the request to the local FastAPI service.
7. FastAPI loads the DINOv2 model and returns the structured result.
8. Vercel backend returns a sanitized response to the frontend.

Async queue flow:

1. ToyOrNot Vercel `/api/rate` calls `public.enqueue_rating_job(...)`.
2. The enqueue RPC inserts one `public.rating_jobs` row, sends one `pgmq` message to `rating_dispatch`, and issues `pg_notify('rating_queue_wakeup', jobId)`.
3. Ubuntu `toyornot-rating-queue` wakes on `LISTEN rating_queue_wakeup` or on the idle reconcile timeout, then drains `pgmq.read('rating_dispatch', ...)` in batches.
4. For each message, the worker claims the referenced row through `public.claim_rating_job(...)`, calls `POST http://127.0.0.1:8000/predict`, and completes the row through `public.complete_rating_job(...)`, which also inserts exactly one `rating_scores` score-log row linked by `source_job_id`.
5. ToyOrNot Vercel `/api/rate-status` still polls `public.rating_jobs` by owner and returns the final state to the frontend.

## Troubleshooting

### Public API is down

Check:

```bash
curl -H "Authorization: Bearer <AUTH_TOKEN>" http://127.0.0.1:8000/health
curl -H "Authorization: Bearer <AUTH_TOKEN>" http://127.0.0.1/health
curl -H "Authorization: Bearer <AUTH_TOKEN>" https://api.piecerate.me/health
```

Interpretation:

- `:8000` fails -> FastAPI or systemd issue
- `127.0.0.1` on port `80` fails -> nginx issue
- public HTTPS fails but local works -> Cloudflare tunnel or DNS issue

### API service issue

```bash
sudo systemctl status graffiti-student --no-pager
journalctl -u graffiti-student -n 100 -l --no-pager
```

### Queue worker issue

```bash
sudo systemctl status toyornot-rating-queue --no-pager
journalctl -u toyornot-rating-queue -n 100 -l --no-pager
```

If queue rows stay in `queued`, check that:

- `SUPABASE_DB_URL` or `SUPABASE_SESSION_POOLER_URL` is set
- `pgmq` is enabled
- `public.rating_jobs`, `public.enqueue_rating_job(...)`, `public.claim_rating_job(...)`, and `public.complete_rating_job(...)` exist
- the worker can hold a persistent session and is not using a transaction pooler
- `graffiti-student` is healthy on `127.0.0.1:8000`

If the worker is healthy but slow to pick up work, check:

- `LISTEN rating_queue_wakeup` is active
- new rows are inserting messages into `pgmq.q_rating_dispatch`
- idle logs do not show the old 1 Hz `PATCH/POST` claim loop

### Cloudflare tunnel issue

```bash
sudo systemctl status cloudflared --no-pager
journalctl -u cloudflared -n 100 -l --no-pager
cloudflared tunnel info graffiti-student
```

### nginx issue

```bash
sudo nginx -t
sudo systemctl status nginx --no-pager
```

### DNS issue

```bash
dig api.piecerate.me
dig api.piecerate.me @1.1.1.1
dig api.piecerate.me @8.8.8.8
```

## Restart Sequence

If the server is in a bad state, restart in this order:

```bash
sudo systemctl restart graffiti-student
sudo systemctl restart toyornot-rating-queue
sudo systemctl restart nginx
sudo systemctl restart cloudflared
```

Then test:

```bash
curl -H "Authorization: Bearer <AUTH_TOKEN>" https://api.piecerate.me/health
```

## Notes

- This setup intentionally avoids recurring GPU hosting cost.
- The current local CPU host is good enough for production at the current scale.
- The API should be treated as a backend-only service.
- The Ubuntu host now owns the always-on queue worker; ToyOrNot should not run its own `worker:rating-queue` daemon.
- The strongest production outputs are `image_usable` and `overall_score`.
