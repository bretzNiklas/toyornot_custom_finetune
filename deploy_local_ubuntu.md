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
EOF
```

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
