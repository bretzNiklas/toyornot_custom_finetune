from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from huggingface_hub import snapshot_download
except ImportError:  # pragma: no cover - exercised indirectly in test import paths
    def snapshot_download(*args, **kwargs):
        raise RuntimeError("huggingface_hub is required to download model artifacts.")


METADATA_FILENAME = ".hf-model-source.json"


@dataclass(frozen=True)
class SyncResult:
    changed: bool
    target_dir: Path
    metadata_path: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download or refresh a pinned Hugging Face model bundle.")
    parser.add_argument("--repo-id", required=True, help="Example: qwertzniki/graffiti-student-dinov2-base-224")
    parser.add_argument("--revision", default="main", help="Pinned Hugging Face revision to deploy.")
    parser.add_argument("--target-dir", type=Path, required=True, help="Directory where the bundle should live.")
    parser.add_argument(
        "--hf-token",
        default=None,
        help="Optional token override. Defaults to HF_TOKEN from the environment when omitted.",
    )
    return parser.parse_args()


def metadata_path_for(target_dir: Path) -> Path:
    return target_dir / METADATA_FILENAME


def read_metadata(target_dir: Path) -> dict[str, Any] | None:
    metadata_path = metadata_path_for(target_dir)
    if not metadata_path.exists():
        return None
    return json.loads(metadata_path.read_text(encoding="utf-8"))


def write_metadata(target_dir: Path, *, repo_id: str, revision: str) -> None:
    metadata_path_for(target_dir).write_text(
        json.dumps(
            {
                "repo_id": repo_id,
                "revision": revision,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def metadata_matches(target_dir: Path, *, repo_id: str, revision: str) -> bool:
    metadata = read_metadata(target_dir)
    if metadata is None:
        return False
    return metadata.get("repo_id") == repo_id and metadata.get("revision") == revision


def sync_model_artifact(
    *,
    repo_id: str,
    revision: str,
    target_dir: Path,
    hf_token: str | None = None,
) -> SyncResult:
    target_dir = target_dir.resolve()
    metadata_path = metadata_path_for(target_dir)

    if target_dir.is_dir() and metadata_matches(target_dir, repo_id=repo_id, revision=revision):
        return SyncResult(changed=False, target_dir=target_dir, metadata_path=metadata_path)

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{target_dir.name}.tmp-", dir=str(target_dir.parent)))

    try:
        snapshot_download(
            repo_id=repo_id,
            repo_type="model",
            local_dir=str(temp_dir),
            revision=revision,
            token=hf_token,
        )
        write_metadata(temp_dir, repo_id=repo_id, revision=revision)

        if target_dir.exists():
            shutil.rmtree(target_dir)
        temp_dir.replace(target_dir)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    return SyncResult(changed=True, target_dir=target_dir, metadata_path=metadata_path)


def main() -> None:
    args = parse_args()
    result = sync_model_artifact(
        repo_id=args.repo_id,
        revision=args.revision,
        target_dir=args.target_dir,
        hf_token=args.hf_token,
    )
    action = "updated" if result.changed else "unchanged"
    print(f"{action}: {result.target_dir}")


if __name__ == "__main__":
    main()
