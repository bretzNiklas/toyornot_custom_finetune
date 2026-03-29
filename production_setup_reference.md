# Production Setup Reference

This document records the current production setup for the graffiti scoring API.

It is intended as the single reference for:

- where the model runs
- how the API is served
- how the public hostname is exposed
- how backend callers should use it
- what to check when something breaks

## Current Architecture

The live deployment uses:

- model host: local Ubuntu server
- hardware: HP T640 Terminal, AMD Ryzen R1505G, `8 GB RAM`
- model: `student-v2-dinov2`
- backbone: `facebook/dinov2-base`
- local app server: FastAPI + uvicorn
- local reverse proxy: nginx
- public exposure: Cloudflare named tunnel
- public API hostname: `https://api.piecerate.me`
- caller: backend/server code only
- deployment flow: GitHub push -> self-hosted GitHub Actions runner on `niklasserver` -> `deploy_remote.sh`

The supported runtime path is a Supabase-to-Piecerate handoff through `POST /predictions`.

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
- [deploy/judge_api_handoff_worker.py](C:/Users/qwert/Desktop/custom_model/deploy/judge_api_handoff_worker.py)  
  Supabase-backed worker that claims `judge_api_jobs` and writes `judge_api_results`
- [deploy/ubuntu/graffiti-student.service](C:/Users/qwert/Desktop/custom_model/deploy/ubuntu/graffiti-student.service)  
  systemd unit for the API
- [deploy/ubuntu/deploy_remote.sh](C:/Users/qwert/Desktop/custom_model/deploy/ubuntu/deploy_remote.sh)  
  server-side deploy script for exact-commit pull deploys
- [deploy/ubuntu/sync_model_artifact.py](C:/Users/qwert/Desktop/custom_model/deploy/ubuntu/sync_model_artifact.py)  
  helper that refreshes the pinned Hugging Face model bundle only when metadata changes
- [deploy/ubuntu/nginx-graffiti-student.conf](C:/Users/qwert/Desktop/custom_model/deploy/ubuntu/nginx-graffiti-student.conf)  
  nginx reverse proxy config
- [.github/workflows/deploy-production.yml](C:/Users/qwert/Desktop/custom_model/.github/workflows/deploy-production.yml)  
  CI job that tests and deploys pushes to `main`
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
MODEL_REPO_ID=qwertzniki/graffiti-student-dinov2-base-224
MODEL_REVISION=main
MODEL_VERSION=student-v2-dinov2
MODEL_DIR=/home/niklas/toyornot_custom_finetune/models/dinov2_base_224
```

Optional:

```text
HF_TOKEN=<token with access to the private model repo>
```

## Deployment Automation

Production deploys are now pull-based.

GitHub Actions triggers on pushes to `main`, runs the local API tests on a hosted runner, then schedules the deploy job onto a self-hosted runner installed on the Ubuntu host. That runner invokes:

```bash
bash <detected-app-dir>/deploy/ubuntu/deploy_remote.sh <commit-sha>
```

The workflow currently supports both of these server layouts:

- `/home/niklas/toyornot_custom_finetune`
- `/srv/graffiti-student/app`

The local deploy script on the server:

1. fetches `origin`
2. checks out the exact pushed commit SHA
3. creates or reuses `/home/niklas/toyornot_custom_finetune/.venv`
4. installs `requirements-serve.txt`
5. refreshes the pinned Hugging Face bundle only when `MODEL_REPO_ID` or `MODEL_REVISION` changed
6. restarts `graffiti-student`
7. runs an authenticated local `/health` smoke test

The deploy runner must expose the labels:

- `self-hosted`
- `linux`
- `x64`
- `graffiti-deploy`

## Local API

Internal local paths:

- health: `http://127.0.0.1:8000/health`
- create prediction: `http://127.0.0.1:8000/predictions`

nginx proxies public local HTTP:

- `http://127.0.0.1/health`
- `http://127.0.0.1/predictions`

The API requires:

```http
Authorization: Bearer <AUTH_TOKEN>
```

## Services

### Graffiti API

Service name:

```text
graffiti-student
```

Useful commands:

```bash
systemctl status graffiti-student --no-pager
journalctl -u graffiti-student -n 100 -l --no-pager
```

### nginx

Useful commands:

```bash
sudo nginx -t
sudo systemctl status nginx --no-pager
sudo systemctl restart nginx
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
- `POST /predictions`
- `GET /predictions/{job_id}`

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

## Backend Integration

These environment variables should exist in the Ubuntu handoff worker environment:

```env
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service-role-key>
JUDGE_API_TOKEN=<AUTH_TOKEN>
```

Important:

- the browser should not call `api.piecerate.me` directly with the secret
- Vercel `/api/rate` should upload to Supabase Storage and insert `public.judge_api_jobs`
- only the Ubuntu handoff worker should call the Judge API and write `public.judge_api_results`

## End-To-End Request Flow

1. User uploads an image to the app.
2. Frontend sends the image to backend/server code.
3. Vercel `/api/rate` uploads the image to Supabase Storage and inserts a `pending` job row.
4. `graffiti-judge-handoff-worker` claims the row, calls `POST https://api.piecerate.me/predictions`, and polls the job.
5. Cloudflare routes traffic through the named tunnel.
6. nginx forwards the request to the local FastAPI service.
7. FastAPI enqueues the prediction locally, the local worker scores it, and the Judge API returns the terminal payload.
8. The handoff worker archives the judged image on local disk, writes `public.judge_api_results` with that archive reference, and finalizes the job row.

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

### Deploy workflow issue

Check the GitHub Actions run first, then rerun the server-side script manually if needed:

```bash
bash /home/niklas/toyornot_custom_finetune/deploy/ubuntu/deploy_remote.sh "$(git -C /home/niklas/toyornot_custom_finetune rev-parse HEAD)"
```

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
sudo systemctl restart graffiti-student-worker
sudo systemctl restart graffiti-judge-handoff-worker
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
- The supported integration path is `judge_api_jobs` -> Ubuntu handoff worker -> Judge API `/predictions`.
- The strongest production outputs are `image_usable` and `overall_score`.
