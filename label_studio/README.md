# Label Studio Workflow

This setup uses Label Studio as the human editor layer for the graffiti scoring pipeline.

## What is included

- `label_studio/graffiti_rating_config.xml`
  - Labeling interface for your six score categories
  - Extra metadata fields for `medium`, `piece_type`, `image_usable`, and `confidence`
  - Character-only pieces are out of scope and should be marked unusable
- `label_studio/tasks.json`
  - Generated task file that points Label Studio at local images
- `scripts/generate_label_studio_tasks.py`
  - Creates task JSON from the `images/` folder
- `scripts/convert_label_studio_export.py`
  - Flattens Label Studio JSON export into training-friendly JSONL
- `scripts/export_current_label_studio_annotations.py`
  - Reads the local Label Studio SQLite DB and exports the latest annotations directly to JSONL
- `scripts/audit_current_labels.py`
  - Summarizes label distribution and flags likely rubric drift
- `scripts/start_label_studio.ps1`
  - Starts Label Studio with the right local file settings

## Generate tasks

From the workspace root:

```powershell
.\.venv\Scripts\python .\scripts\generate_label_studio_tasks.py --shuffle
```

This writes `label_studio/tasks.json`.

## Start Label Studio

```powershell
.\scripts\start_label_studio.ps1
```

The script:

- keeps Label Studio data inside this workspace
- enables local file serving
- exposes your workspace as the local file document root

## Create the project

1. Open `http://localhost:8080`
2. Create a local account if needed
3. Create a new project
4. Paste in the contents of `label_studio/graffiti_rating_config.xml`
5. Open project `Settings -> Cloud Storage`
6. Add a `Source Storage` of type `Local Files`
7. Set `Absolute local path` to `C:\Users\qwert\Desktop\custom_model\images`
8. Save the storage, but do not sync it if you plan to import `tasks.json`
9. Import `label_studio/tasks.json`

The local storage entry is required even when you import tasks manually. Without it, image URLs like `/data/local-files/?d=images/...` return `404`.

If you already imported tasks and see `404` errors in the server log:

1. Add the `Local Files` source storage above
2. Refresh the project page
3. If thumbnails are still broken, delete the imported tasks and import `label_studio/tasks.json` again

## Export labels

After labeling, export JSON from Label Studio and convert it:

```powershell
.\.venv\Scripts\python .\scripts\convert_label_studio_export.py .\exports\label_studio_raw.json .\exports\labels.jsonl
```

Or, if you are using the local SQLite database created by `start_label_studio.ps1`, export the latest saved annotations directly:

```powershell
.\.venv\Scripts\python .\scripts\export_current_label_studio_annotations.py
```

To audit the current batch before labeling more:

```powershell
.\.venv\Scripts\python .\scripts\audit_current_labels.py
```

## Notes on examples

Do not put a large exemplar pack into every task. It slows labeling and nudges every score toward the same few references.

Better process:

1. Label `30-50` images yourself
2. Pick `9-12` anchor examples from those labels
3. Use that anchor set later for teacher-model prompting and calibration

Because this dataset is mixed, the `medium` field is important. If you want a paper-sketch-only student model later, filter to `medium = paper_sketch`.
Character-only pieces should not be included in the first model; mark them `Unusable -> Character-only / out of scope`.
