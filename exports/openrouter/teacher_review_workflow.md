# Teacher Review Workflow

## Import

1. In Label Studio, create a new project for teacher review.
2. Reuse the existing labeling config from:
   - `C:\Users\qwert\Desktop\custom_model\label_studio\graffiti_rating_config.xml`
3. Add `Source Storage -> Local Files` with:
   - `C:\Users\qwert\Desktop\custom_model\images`
4. Import:
   - `C:\Users\qwert\Desktop\custom_model\label_studio\teacher_review_tasks.json`

The teacher predictions are prefilled as model predictions. Edit them only when they are wrong.

## What To Review First

- teacher-marked unusable images
- `medium` confidence items
- very low scores (`overall_score <= 3`)
- very high scores (`overall_score >= 8`)
- digital / other / unclear images
- `mixed` or `other` piece types

## Export

After reviewing, export the project as JSON and convert it:

```powershell
.\.venv\Scripts\python .\scripts\convert_label_studio_export.py .\path\to\teacher_review_export.json .\exports\openrouter\teacher_review_corrected.jsonl
```

## Merge

Then merge the corrected review labels with the original human labels and the full teacher batch:

```powershell
.\.venv\Scripts\python .\scripts\merge_teacher_review_with_labels.py --reviewed .\exports\openrouter\teacher_review_corrected.jsonl
```

This writes the merged training pool to:

- `C:\Users\qwert\Desktop\custom_model\exports\final\training_pool_v1.jsonl`

Priority order is:

1. original human labels
2. reviewed teacher corrections
3. raw teacher predictions
