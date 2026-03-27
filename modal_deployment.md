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

## 3. Make sure the trained model bundle exists

You need [best_bundle](C:/Users/qwert/Desktop/custom_model/runs/student_v1/stage_b/best_bundle) locally on the machine that runs the deploy command.

## 4. Deploy the app

Run this from the repo root:

```bash
modal deploy deploy/modal_app.py
```

That deploys a web endpoint from [modal_app.py](C:/Users/qwert/Desktop/custom_model/deploy/modal_app.py).

## 5. Get the endpoint URL

Modal prints the URL during deploy. You can also inspect it in the Modal dashboard.

## 6. Send requests

Request body:

```json
{
  "image_b64": "<base64-image>",
  "filename": "test.jpg",
  "include_debug": false
}
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

## Notes

- The app loads the model once per container using `@modal.enter`.
- It defaults to `T4` to keep cost down.
- If `T4` is not available in your Modal account, change `gpu="T4"` in [modal_app.py](C:/Users/qwert/Desktop/custom_model/deploy/modal_app.py) to `L4` or `A10`.
- The deploy machine must have the trained bundle directory present because Modal mounts it into the container with `Image.add_local_dir`.
