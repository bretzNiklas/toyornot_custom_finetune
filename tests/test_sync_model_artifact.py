from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.ubuntu import sync_model_artifact


class SyncModelArtifactTests(unittest.TestCase):
    def test_skips_download_when_repo_and_revision_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target_dir = Path(tmpdir) / "model"
            target_dir.mkdir()
            sync_model_artifact.write_metadata(
                target_dir,
                repo_id="org/private-model",
                revision="abc123",
            )

            with patch.object(sync_model_artifact, "snapshot_download") as mocked_download:
                result = sync_model_artifact.sync_model_artifact(
                    repo_id="org/private-model",
                    revision="abc123",
                    target_dir=target_dir,
                )

        self.assertFalse(result.changed)
        mocked_download.assert_not_called()

    def test_downloads_new_snapshot_when_revision_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target_dir = Path(tmpdir) / "model"
            target_dir.mkdir()
            (target_dir / "stale.txt").write_text("stale", encoding="utf-8")
            sync_model_artifact.write_metadata(
                target_dir,
                repo_id="org/private-model",
                revision="old-revision",
            )

            def fake_snapshot_download(*, repo_id: str, repo_type: str, local_dir: str, revision: str, token=None):
                self.assertEqual(repo_id, "org/private-model")
                self.assertEqual(repo_type, "model")
                self.assertEqual(revision, "new-revision")
                self.assertIsNone(token)
                temp_path = Path(local_dir)
                (temp_path / "weights.safetensors").write_text("fresh", encoding="utf-8")
                return local_dir

            with patch.object(
                sync_model_artifact,
                "snapshot_download",
                side_effect=fake_snapshot_download,
            ) as mocked_download:
                result = sync_model_artifact.sync_model_artifact(
                    repo_id="org/private-model",
                    revision="new-revision",
                    target_dir=target_dir,
                )

            metadata = sync_model_artifact.read_metadata(target_dir)
            self.assertTrue(result.changed)
            mocked_download.assert_called_once()
            self.assertFalse((target_dir / "stale.txt").exists())
            self.assertTrue((target_dir / "weights.safetensors").exists())
            self.assertIsNotNone(metadata)
            self.assertEqual(metadata["repo_id"], "org/private-model")
            self.assertEqual(metadata["revision"], "new-revision")
