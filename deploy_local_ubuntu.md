# Local Ubuntu Deployment

This deploys the winning `student-v2-dinov2` model on a local Ubuntu box using:

- FastAPI
- uvicorn
- systemd
- nginx
- a GitHub Actions push-to-deploy workflow

The supported runtime flow remains direct synchronous inference through `POST /predict`.

## 1. System packages

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip nginx
```

## 2. Create the dedicated server layout

Create a dedicated user that owns the runtime files and receives GitHub Actions SSH deploys:

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

## 4. Install the systemd service

```bash
sudo cp /srv/graffiti-student/app/deploy/ubuntu/graffiti-student.service /etc/systemd/system/graffiti-student.service
sudo systemctl daemon-reload
sudo systemctl enable graffiti-student
```

The unit expects:

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
- restart `graffiti-student`
- run an authenticated `http://127.0.0.1:8000/health` smoke test

Useful logs:

```bash
journalctl -u graffiti-student -n 100 -l --no-pager
```

## 7. Wire GitHub Actions deploy access

Generate a deploy key pair for the `graffiti` user and add the public key to `~graffiti/.ssh/authorized_keys`.

GitHub Actions secrets required by [.github/workflows/deploy-production.yml](C:/Users/qwert/Desktop/custom_model/.github/workflows/deploy-production.yml):

- `DEPLOY_HOST`
- `DEPLOY_USER`
- `DEPLOY_PORT`
- `DEPLOY_SSH_PRIVATE_KEY`
- `DEPLOY_KNOWN_HOSTS`

The SSH user must be able to:

- read `/etc/graffiti-student.env`
- write under `/srv/graffiti-student`
- run `sudo systemctl daemon-reload`
- run `sudo systemctl restart graffiti-student`
- run `sudo systemctl status graffiti-student`
- run `sudo journalctl -u graffiti-student`

If you use the same `graffiti` account for SSH and the service, give it passwordless sudo for those commands only.

## 8. GitHub Actions deploy flow

Pushes to `main` now do the following:

1. Run `python -m unittest tests.test_local_api tests.test_sync_model_artifact`
2. SSH into the Ubuntu box
3. Invoke `bash /srv/graffiti-student/app/deploy/ubuntu/deploy_remote.sh <commit-sha>`
4. Fail the workflow if the service restart or health check fails

This replaces manual `scp` deployments with a pull-based deploy that always checks out the exact pushed commit.

## 9. API contract

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

The response returns the rating JSON immediately, including:

- `image_usable`
- `medium`
- `overall_score`
- rubric subscores
- `request_id`
- `model_version`

## 10. Backend integration

Recommended application flow:

1. Frontend uploads the image to your backend.
2. Your backend converts the file to base64.
3. Your backend calls `POST /predict` on this Ubuntu-hosted API.
4. Your backend returns a sanitized response to the frontend.

If your backend is deployed separately, give it its own caller-side environment variables such as:

```env
GRAFFITI_API_URL=https://api.piecerate.me
GRAFFITI_API_TOKEN=<AUTH_TOKEN>
```

Keep the bearer token on the backend only.
