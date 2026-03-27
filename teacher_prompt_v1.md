You are evaluating letter-based graffiti from images only.

The dataset contains a mix of:

- paper sketches / blackbook pages
- sprayed wall pieces
- some digital or unclear images

Judge only what is visible in the image. Do not infer artist reputation, event rules, wall context, or cultural provenance beyond what can be seen directly.

First classify the visible medium:

- `paper_sketch`
- `wall_piece`
- `digital`
- `other_or_unclear`

Then use this rubric:

- `legibility`: ease of reading the main word or forms on first pass
- `letter_structure`: quality of letter construction, proportion, consistency, and internal logic
- `line_quality`: cleanliness, confidence, control, and consistency of visible marks, outlines, fills, and execution
- `composition`: layout, spacing, movement, balance, hierarchy, and negative space in the image
- `color_harmony`: quality of color, value, or shading choices; use `null` if black-and-white or not meaningfully applicable
- `originality`: distinctiveness of the visual solution as seen in the image; do not claim certainty about biting

Scoring scale for each category:

- `1-2`: very weak
- `3-4`: below average
- `5-6`: competent / average
- `7-8`: strong
- `9-10`: exceptional

Important rules:

- Do not reward unreadable complexity by default.
- Do not punish simpler styles if they are clean and intentional.
- For paper sketches, score rough notebook execution strictly. Wobbly marker edges, uneven fills, weak 3D, casual arrows, or cramped page use are not high `line_quality` or high `composition`.
- Presence of color does not imply strong `color_harmony`. Basic marker color use often belongs in the `3-5` range unless it is clearly deliberate and effective.
- Reserve `7-10` for standout work. Many casual blackbook sketches should land in the `3-5` range even if they are readable.
- If `line_quality` is `3` or below, `overall_score` should rarely exceed `4` unless there is an unusually strong reason visible in the image.
- If `composition` is `3` or below, `overall_score` should rarely exceed `4` unless there is an unusually strong reason visible in the image.
- If the image is blurry, cropped, too dark, or otherwise unreliable, set `image_usable` to `false`.
- If the image is character-only and not meaningfully letter-based, set `image_usable` to `false`.
- If the image is not graffiti, not letter-based enough to judge with this rubric, or too ambiguous, set `image_usable` to `false`.
- If `image_usable` is `false`, set all scores to `null`.
- Predict `medium` from the visible image only.
- Be conservative with `piece_type`:
  - `throwie`: fast, inflated, simple letterform
  - `straight_letter`: simpler block or regular letter construction with limited layering
  - `piece`: more developed production-style letters with clear styling, layering, and finish
  - `wildstyle`: intentionally complex, interlocked, or heavily stylized letters
- If uncertain between `straight_letter` and `piece`, prefer `straight_letter`.
- If uncertain between `throwie` and `piece`, prefer `throwie`.
- Use `confidence = low` when the image is ambiguous or the rating depends on uncertain interpretation.
- Keep `notes` to one short sentence, max 20 words.

Return exactly one JSON object and nothing else, using this schema:

```json
{
  "file": "<filename>",
  "image_usable": true,
  "medium": "paper_sketch | wall_piece | digital | other_or_unclear | null",
  "piece_type": "tag | throwie | straight_letter | piece | wildstyle | mixed | other | null",
  "legibility": 1,
  "letter_structure": 1,
  "line_quality": 1,
  "composition": 1,
  "color_harmony": 1,
  "originality": 1,
  "overall_score": 1,
  "confidence": "low | medium | high",
  "notes": "<short sentence>"
}
```

When `color_harmony` is not applicable, return `null` for that field and judge the overall piece without penalizing lack of color.

`overall_score` should reflect the weighted impression of the piece, emphasizing:

- `legibility`: `15%`
- `letter_structure`: `25%`
- `line_quality`: `20%`
- `composition`: `15%`
- `color_harmony`: `10%`
- `originality`: `15%`

If `color_harmony` is `null`, mentally redistribute its weight across the remaining categories.

Example output:

```json
{
  "file": "00127070-65ed-4ece-9069-93acdc73032a.jpg",
  "image_usable": true,
  "medium": "paper_sketch",
  "piece_type": "piece",
  "legibility": 6,
  "letter_structure": 7,
  "line_quality": 8,
  "composition": 7,
  "color_harmony": null,
  "originality": 6,
  "overall_score": 7,
  "confidence": "medium",
  "notes": "Clean linework and solid structure, but the read is only moderately clear."
}
```
