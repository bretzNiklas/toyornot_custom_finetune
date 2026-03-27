# V1 Dataset Artifacts

## Snapshot

- labeled rows: 210
- usable rows: 200
- core usable rows (paper + wall): 179
- core strong rows (paper + wall, medium/high confidence): 169
- side-domain usable rows (digital + unclear): 21
- low-confidence core review queue: 10

## Splits

- train seed: 129
- validation locked: 16
- test locked: 24
- teacher prompt anchors: 6

## Train Seed Mix

- medium: paper_sketch=86, wall_piece=43
- piece_type: piece=27, straight_letter=44, tag=10, throwie=31, wildstyle=17

## Validation Mix

- medium: paper_sketch=11, wall_piece=5
- score_bucket: high=3, low=6, mid=7

## Test Mix

- medium: paper_sketch=16, wall_piece=8
- score_bucket: high=4, low=8, mid=12

## How To Use

- Keep validation and test locked. Do not use them as prompt examples or relabel them casually.
- Use the teacher prompt anchors only for in-context calibration.
- Use the train seed for a first human-only baseline or for teacher-vs-human comparisons.
- Review the low-confidence queue before folding those labels into training.
- Treat the side-domain file as optional for v1. It is better for later expansion than for the first core model.
