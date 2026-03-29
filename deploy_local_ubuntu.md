# Local Ubuntu Deployment

This deploys the winning `student-v2-dinov2` model on a local Ubuntu box using:

- FastAPI
- uvicorn
- systemd
- nginx
- a GitHub Actions push-to-deploy workflow backed by a self-hosted runner on the Ubuntu server

The supported runtime flow is now a Supabase-to-Piecerate handoff:

1. Vercel `/api/rate` uploads `judgeImage` to Supabase Storage and inserts `public.judge_api_jobs`
2. `graffiti-judge-handoff-worker` claims the job and calls `POST /predictions`
3. the worker polls `GET /predictions/{job_id}?wait_ms=8000`, archives the judged image locally, writes `public.judge_api_results`, and finalizes the job row

## One-Line Clean Bootstrap

If you already have a working but messy install and want the clean `/srv/graffiti-student` layout in one step, run this from your Windows machine:

```powershell
.\scripts\bootstrap_clean_server.ps1
```

What it does:

- fetches a fresh GitHub Actions runner registration token with `gh`
- SSHes into `niklas@192.168.178.96`
- runs the root bootstrap script at [deploy/ubuntu/bootstrap_clean_server.sh](C:/Users/qwert/Desktop/custom_model/deploy/ubuntu/bootstrap_clean_server.sh)
- creates the `/srv/graffiti-student` layout
- migrates the existing model bundle if found
- writes `/etc/graffiti-student.env`
- installs the `graffiti-student` systemd unit and nginx config
- installs the self-hosted runner as a system service

You still need a sudo-capable account on the Ubuntu box because the clean layout writes into `/srv`, `/etc`, and `/etc/systemd/system`.

## 1. System packages

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip nginx
```

## 2. Create the dedicated server layout

Create a dedicated user that owns the runtime files and runs the self-hosted GitHub Actions deploy runner:

```bash
sudo useradd --create-home --home-dir /srv/graffiti-student --shell /bin/bash graffiti
sudo install -d -o graffiti -g graffiti /srv/graffiti-student/app
sudo install -d -o graffiti -g graffiti /srv/graffiti-student/models
```

Clone the repo into the fixed app path:

```bash
sudo -u graffiti git clone https://github.com/bretzNiklas/toyornot_custom_finetune.git /srv/graffiti-student/app
```

The standardized production layout is:

```text
/srv/graffiti-student/app
/srv/graffiti-student/venv
/srv/graffiti-student/models/<model_version>
/etc/graffiti-student.env
```

## 3. Create the runtime env file

Copy the example and edit it:

```bash
sudo cp /srv/graffiti-student/app/deploy/ubuntu/graffiti-student.env.example /etc/graffiti-student.env
sudo chown root:graffiti /etc/graffiti-student.env
sudo chmod 640 /etc/graffiti-student.env
sudoedit /etc/graffiti-student.env
```

Expected fields:

```env
AUTH_TOKEN=replace_with_your_secret_token
HF_TOKEN=hf_token_with_access_to_the_private_model_repo
MODEL_REPO_ID=qwertzniki/graffiti-student-dinov2-base-224
MODEL_REVISION=main
MODEL_VERSION=student-v2-dinov2
MODEL_ROOT=/srv/graffiti-student/models
MODEL_DIR=/srv/graffiti-student/models/student-v2-dinov2
```

`AUTH_TOKEN` is used by the API and the post-deploy smoke test.  
`HF_TOKEN` is used by the server-side deploy script to pull the model bundle only when the pinned repo or revision changes.

## 4. Install the systemd services

```bash
sudo cp /srv/graffiti-student/app/deploy/ubuntu/graffiti-student.service /etc/systemd/system/graffiti-student.service
sudo cp /srv/graffiti-student/app/deploy/ubuntu/graffiti-student-worker.service /etc/systemd/system/graffiti-student-worker.service
sudo cp /srv/graffiti-student/app/deploy/ubuntu/graffiti-judge-handoff-worker.service /etc/systemd/system/graffiti-judge-handoff-worker.service
sudo systemctl daemon-reload
sudo systemctl enable graffiti-student graffiti-student-worker graffiti-judge-handoff-worker
```

The units expect:

- app checkout: `/srv/graffiti-student/app`
- venv: `/srv/graffiti-student/venv`
- env file: `/etc/graffiti-student.env`
- service user: `graffiti`

## 5. Install nginx

```bash
sudo cp /srv/graffiti-student/app/deploy/ubuntu/nginx-graffiti-student.conf /etc/nginx/sites-available/graffiti-student
sudo ln -sf /etc/nginx/sites-available/graffiti-student /etc/nginx/sites-enabled/graffiti-student
sudo nginx -t
sudo systemctl reload nginx
```

Now the API is reachable on local port `80` on the machine.

## 6. Verify one manual deploy on the server

Run the repo-tracked deploy script once before enabling GitHub Actions:

```bash
sudo -u graffiti bash /srv/graffiti-student/app/deploy/ubuntu/deploy_remote.sh "$(git -C /srv/graffiti-student/app rev-parse HEAD)"
```

This will:

- fetch the repo
- checkout the exact commit
- create `/srv/graffiti-student/venv` if missing
- install `requirements-serve.txt`
- pull the pinned Hugging Face model into `MODEL_DIR` if needed
- restart `graffiti-student`, `graffiti-student-worker`, and `graffiti-judge-handoff-worker`
- run an authenticated `http://127.0.0.1:8000/health` smoke test

Useful logs:

```bash
journalctl -u graffiti-student -n 100 -l --no-pager
journalctl -u graffiti-student-worker -n 100 -l --no-pager
journalctl -u graffiti-judge-handoff-worker -n 100 -l --no-pager
```

## 7. Install the self-hosted GitHub Actions runner

If your production server is only reachable on a private LAN address, the deploy job must run on a self-hosted GitHub runner installed on that same Ubuntu box.

High-level setup:

1. Download the latest Linux x64 runner from the official `actions/runner` release page.
2. Configure it against `https://github.com/bretzNiklas/toyornot_custom_finetune`.
3. Give it the label `graffiti-deploy`.
4. Keep it running in the background and add an `@reboot` crontab entry or equivalent startup hook.

The workflow in [.github/workflows/deploy-production.yml](C:/Users/qwert/Desktop/custom_model/.github/workflows/deploy-production.yml) now targets:

- `self-hosted`
- `linux`
- `x64`
- `graffiti-deploy`

## 8. GitHub Actions deploy flow

Pushes to `main` now do the following:

1. Run `python -m unittest tests.test_local_api tests.test_local_queue tests.test_local_worker tests.test_judge_api_handoff_worker tests.test_sync_model_artifact`
2. Queue the deploy job on the server's self-hosted runner
3. Invoke `bash /srv/graffiti-student/app/deploy/ubuntu/deploy_remote.sh <commit-sha>` locally on that machine
4. Fail the workflow if the service restart or health check fails

This replaces manual `scp` deployments with a pull-based deploy that always checks out the exact pushed commit.

## 9. API contract

Endpoints:

- `GET /health`
- `POST /predictions`
- `GET /predictions/{job_id}`

Both require:

```http
Authorization: Bearer <AUTH_TOKEN>
```

Prediction submission body:

```json
{
  "image_b64": "<base64>",
  "filename": "example.jpg",
  "include_debug": false
}
```

`POST /predictions` returns a queued job payload with `job_id`, `request_id`, `queue_position`, and `poll_url`.

`GET /predictions/{job_id}` returns queued state or terminal result/error.

## 10. Backend integration

Recommended application flow:

1. Vercel `/api/rate` uploads the source image to Supabase Storage.
2. Vercel inserts a `pending` row into `public.judge_api_jobs`.
3. `graffiti-judge-handoff-worker` calls `POST /predictions` on this Ubuntu-hosted API.
4. The worker polls until terminal, writes `public.judge_api_results` with the archived local image reference, and then deletes the transient Supabase upload.

If your backend is deployed separately, give it its own caller-side environment variables such as:

```env
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service-role-key>
JUDGE_API_TOKEN=<AUTH_TOKEN>
```

Keep the bearer token and Supabase service role key on the backend or worker only.
