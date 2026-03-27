from __future__ import annotations

import argparse
import os
from pathlib import Path

from huggingface_hub import HfApi


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Push a packaged student bundle to a private Hugging Face model repo.")
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--repo-id", required=True, help="Example: username/graffiti-student-v1")
    parser.add_argument("--private", action="store_true", default=False)
    parser.add_argument("--token-env", default="HF_TOKEN", help="Environment variable containing the HF token.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    token = os.getenv(args.token_env)
    if not token:
        raise SystemExit(f"Missing Hugging Face token in {args.token_env}.")

    api = HfApi(token=token)
    api.create_repo(repo_id=args.repo_id, repo_type="model", private=args.private, exist_ok=True)
    api.upload_folder(
        folder_path=str(args.bundle_dir.resolve()),
        repo_id=args.repo_id,
        repo_type="model",
    )
    print(f"Uploaded {args.bundle_dir.resolve()} to hf://{args.repo_id}")


if __name__ == "__main__":
    main()
