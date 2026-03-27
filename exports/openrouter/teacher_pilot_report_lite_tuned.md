# Teacher Pilot Evaluation

## Teacher Run

- predictions written: 50
- total cost usd: 0.0746
- average cost per image usd: 0.0015
- prompt tokens: 474210
- completion tokens: 8514
- predicted medium mix: digital=1, other_or_unclear=2, paper_sketch=32, wall_piece=15

## All Pilot Rows

- human rows: 50
- matched predictions: 50
- coverage: 100.0%
- image usable accuracy: 100.0%
- medium accuracy: 94.0%
- piece type accuracy: 62.0%
- overall bucket accuracy: 46.0%

### Numeric MAE

- legibility: 1.640
- letter_structure: 1.360
- line_quality: 1.780
- composition: 1.380
- color_harmony: 1.175
- originality: 1.220
- overall_score: 1.120

### Largest Overall Disagreements

- 2e2da899-d697-43bd-a61d-448237cf68b3.jpg: delta=3 medium=paper_sketch human=8 pred=5 human_type=wildstyle pred_type=wildstyle
- 43ab1bd8-4c47-4d70-a446-e240cb952313.jpg: delta=3 medium=paper_sketch human=6 pred=3 human_type=straight_letter pred_type=tag
- 15aa5eeb-6ce6-460e-a091-b85e3c2ca2e0.jpg: delta=2 medium=paper_sketch human=8 pred=6 human_type=straight_letter pred_type=piece
- 19876a6b-e2ec-4492-96ea-a2569eafabc7.jpg: delta=2 medium=wall_piece human=8 pred=6 human_type=piece pred_type=straight_letter
- 1de6cfe5-9efb-44f2-835d-310cdc2808e4.jpg: delta=2 medium=wall_piece human=4 pred=6 human_type=straight_letter pred_type=straight_letter
- 2743d88b-7dab-44db-8b92-5aec317ef921.jpg: delta=2 medium=wall_piece human=8 pred=6 human_type=throwie pred_type=throwie
- 2ddb2607-4c9d-48b0-b38c-34e198c02f3d.jpg: delta=2 medium=paper_sketch human=4 pred=6 human_type=straight_letter pred_type=straight_letter
- 30ddd2f7-0215-47ee-a740-134cc766aa54.jpg: delta=2 medium=paper_sketch human=4 pred=6 human_type=straight_letter pred_type=straight_letter
- 39bcc432-5d4a-4ec6-931e-2c55f42ea609.jpg: delta=2 medium=paper_sketch human=3 pred=5 human_type=wildstyle pred_type=wildstyle
- 3ef01a05-da82-41b5-b790-567d54094488.jpg: delta=2 medium=paper_sketch human=3 pred=5 human_type=throwie pred_type=throwie

## Locked Eval Rows

- human rows: 40
- matched predictions: 40
- coverage: 100.0%
- image usable accuracy: 100.0%
- medium accuracy: 92.5%
- piece type accuracy: 62.5%
- overall bucket accuracy: 47.5%

### Numeric MAE

- legibility: 1.625
- letter_structure: 1.425
- line_quality: 1.675
- composition: 1.400
- color_harmony: 1.125
- originality: 1.150
- overall_score: 1.100

### Largest Overall Disagreements

- 2e2da899-d697-43bd-a61d-448237cf68b3.jpg: delta=3 medium=paper_sketch human=8 pred=5 human_type=wildstyle pred_type=wildstyle
- 43ab1bd8-4c47-4d70-a446-e240cb952313.jpg: delta=3 medium=paper_sketch human=6 pred=3 human_type=straight_letter pred_type=tag
- 15aa5eeb-6ce6-460e-a091-b85e3c2ca2e0.jpg: delta=2 medium=paper_sketch human=8 pred=6 human_type=straight_letter pred_type=piece
- 19876a6b-e2ec-4492-96ea-a2569eafabc7.jpg: delta=2 medium=wall_piece human=8 pred=6 human_type=piece pred_type=straight_letter
- 2743d88b-7dab-44db-8b92-5aec317ef921.jpg: delta=2 medium=wall_piece human=8 pred=6 human_type=throwie pred_type=throwie
- 2ddb2607-4c9d-48b0-b38c-34e198c02f3d.jpg: delta=2 medium=paper_sketch human=4 pred=6 human_type=straight_letter pred_type=straight_letter
- 39bcc432-5d4a-4ec6-931e-2c55f42ea609.jpg: delta=2 medium=paper_sketch human=3 pred=5 human_type=wildstyle pred_type=wildstyle
- 3ef01a05-da82-41b5-b790-567d54094488.jpg: delta=2 medium=paper_sketch human=3 pred=5 human_type=throwie pred_type=throwie
- 466b9fac-e7ca-4572-a717-290e058a3556.jpg: delta=2 medium=paper_sketch human=3 pred=5 human_type=throwie pred_type=straight_letter
- 4b85506c-5be8-41ef-a547-9596012b0173.jpg: delta=2 medium=wall_piece human=9 pred=7 human_type=piece pred_type=piece

## Pilot-Only Rows

- human rows: 10
- matched predictions: 10
- coverage: 100.0%
- image usable accuracy: 100.0%
- medium accuracy: 100.0%
- piece type accuracy: 60.0%
- overall bucket accuracy: 40.0%

### Numeric MAE

- legibility: 1.700
- letter_structure: 1.100
- line_quality: 2.200
- composition: 1.300
- color_harmony: 1.375
- originality: 1.500
- overall_score: 1.200

### Largest Overall Disagreements

- 1de6cfe5-9efb-44f2-835d-310cdc2808e4.jpg: delta=2 medium=wall_piece human=4 pred=6 human_type=straight_letter pred_type=straight_letter
- 30ddd2f7-0215-47ee-a740-134cc766aa54.jpg: delta=2 medium=paper_sketch human=4 pred=6 human_type=straight_letter pred_type=straight_letter
- 46dcc6c2-6eec-4efd-afef-9facbef2cde2.jpg: delta=2 medium=wall_piece human=7 pred=5 human_type=throwie pred_type=throwie
- 0076e231-e722-43e4-8416-1ba2d182ceeb.jpg: delta=1 medium=paper_sketch human=5 pred=6 human_type=wildstyle pred_type=piece
- 0ea57cd7-b3e1-4bb0-a555-1666c1063620.jpg: delta=1 medium=paper_sketch human=5 pred=4 human_type=straight_letter pred_type=straight_letter
- 16330982-8768-46fd-b268-a4c7f96cd024.jpg: delta=1 medium=paper_sketch human=5 pred=6 human_type=straight_letter pred_type=piece
- 2394b44a-efda-4afd-9fde-2f7e99712631.jpg: delta=1 medium=paper_sketch human=4 pred=5 human_type=straight_letter pred_type=throwie
- 40301a56-1496-474f-8ba9-d7d84099e081.jpg: delta=1 medium=paper_sketch human=8 pred=7 human_type=piece pred_type=piece
- 5194e5b0-fc3c-42d5-b340-1ce7323f8721.jpg: delta=1 medium=wall_piece human=8 pred=7 human_type=straight_letter pred_type=piece
- 0cc59285-bf1a-4021-9eb4-ef772c6f33f6.jpg: delta=0 medium=paper_sketch human=3 pred=3 human_type=throwie pred_type=throwie
