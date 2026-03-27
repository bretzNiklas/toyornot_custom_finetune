# Modal Deployment

Use this instead of Hugging Face Inference Endpoints if you want cheaper hosted inference.

## 1. Install Modal on the machine you will deploy from

```bash
pip install modal
```

## 2. Authenticate Modal

```bash
modal setup
```

## 3. Create the API secret

Create a Modal secret named `graffiti-student-web-auth` with an `AUTH_TOKEN` entry:

```bash
modal secret create graffiti-student-web-auth AUTH_TOKEN=your_long_random_token
```

Optional model version tag:

```bash
modal secret create graffiti-student-web-auth \
  AUTH_TOKEN=your_long_random_token \
  MODEL_VERSION=student-v1
```

Do not embed this token in a public browser app. Use it from your backend, server action, or edge function.

## 4. Make sure the trained model bundle exists

You need [best_bundle](C:/Users/qwert/Desktop/custom_model/runs/student_v1/stage_b/best_bundle) locally on the machine that runs the deploy command.

## 5. Deploy the app

Run this from the repo root:

```bash
modal deploy deploy/modal_app.py
```

That deploys a web endpoint from [modal_app.py](C:/Users/qwert/Desktop/custom_model/deploy/modal_app.py).

## 6. Get the endpoint URL

Modal prints the URL during deploy. You can also inspect it in the Modal dashboard.

## 7. Send requests

Request body:

```json
{
  "image_b64": "<base64-image>",
  "filename": "test.jpg",
  "include_debug": false
}
```

Required header:

```text
Authorization: Bearer your_long_random_token
```

Response body:

```json
{
  "filename": "test.jpg",
  "image_usable": true,
  "medium": "paper_sketch",
  "overall_score": 6,
  "legibility": 6,
  "letter_structure": 6,
  "line_quality": 7,
  "composition": 5,
  "color_harmony": null,
  "originality": 5
}
```

Health check:

```bash
curl -H "Authorization: Bearer your_long_random_token" \
  https://your-endpoint.modal.run/health
```

## Notes

- The app loads the model once per container using `@modal.enter`.
- It defaults to `T4` to keep cost down.
- If `T4` is not available in your Modal account, change `gpu="T4"` in [modal_app.py](C:/Users/qwert/Desktop/custom_model/deploy/modal_app.py) to `L4` or `A10`.
- The deploy machine must have the trained bundle directory present because Modal mounts it into the container with `Image.add_local_dir`.
- Requests larger than `8 MB` are rejected.
- Invalid base64 or invalid image data returns structured `400` JSON instead of a traceback.
