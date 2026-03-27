from __future__ import annotations

from pathlib import Path

import modal
from pydantic import BaseModel


APP_NAME = "graffiti-student-v1"
MODEL_DIR = Path("runs/student_v1/stage_b/best_bundle")
REMOTE_MODEL_DIR = "/root/model_bundle"


image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "torch>=2.4.0",
        "torchvision>=0.19.0",
        "transformers>=4.55.0",
        "peft>=0.13.0",
        "accelerate>=0.34.0",
        "safetensors>=0.4.5",
        "Pillow>=10.4.0",
        "pydantic>=2.8.0",
        "fastapi[standard]>=0.115.0",
    )
    .add_local_python_source("student")
    .add_local_dir(MODEL_DIR, remote_path=REMOTE_MODEL_DIR)
)

app = modal.App(APP_NAME, image=image)


class PredictionRequest(BaseModel):
    image_b64: str
    filename: str | None = None
    include_debug: bool = False


@app.cls(gpu="T4", scaledown_window=300, timeout=300)
class GraffitiStudentService:
    @modal.enter()
    def load(self) -> None:
        from student.predictor import StudentPredictor

        self.predictor = StudentPredictor(Path(REMOTE_MODEL_DIR))

    @modal.fastapi_endpoint(method="POST", docs=True)
    def predict(self, payload: PredictionRequest) -> dict:
        return self.predictor.predict_base64(
            payload.image_b64,
            filename=payload.filename,
            include_debug=payload.include_debug,
        )
