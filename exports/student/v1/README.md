# Student V1 Artifacts

## Snapshot

- total merged rows: 1496
- human-quality rows: 573
- teacher rows: 923
- stage A rows: 1324
- teacher core score rows: 923

## Human Locked Split

- train: 401
- val: 86
- test: 86

## Human Train Mix

- usable: {'False': 26, 'True': 375}
- medium: {'digital': 73, 'other_or_unclear': 16, 'paper_sketch': 171, 'wall_piece': 115}
- score bucket: {'high': 78, 'low': 82, 'mid': 126}

## Locked Eval Mix

- val medium: {'digital': 16, 'other_or_unclear': 4, 'paper_sketch': 37, 'wall_piece': 24}
- test medium: {'digital': 16, 'other_or_unclear': 3, 'paper_sketch': 37, 'wall_piece': 25}

## Notes

- Validation and test are human-only and must stay locked.
- Stage A includes all human-train rows plus all teacher rows.
- Score losses should be masked to usable paper/wall rows only.
- Digital and other_or_unclear remain for medium learning, not score learning.

