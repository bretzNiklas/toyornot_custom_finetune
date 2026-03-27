# Graffiti Sketch Rubric v1

This rubric is for graffiti sketches on paper or in blackbooks. It is optimized for image-only judging, where the model only sees the artwork and not wall context, artist identity, or event metadata.

## Scope

Use this rubric for:

- letter-based graffiti sketches
- blackbook pages
- marker, pen, pencil, or mixed-media sketches on paper

Do not use wall-specific criteria such as:

- can control at mural scale
- site integration
- community fit
- brief adherence, unless separate metadata exists

## Required metadata

Each image should include:

- `file`
- `image_usable`: `true` or `false`
- `confidence`: `low`, `medium`, or `high`

Optional metadata:

- `piece_type`: `tag`, `throwie`, `straight_letter`, `piece`, `wildstyle`, `mixed`, `other`
- `notes`: one short sentence, max 20 words

If `image_usable` is `false`, still return the record, but set all score fields to `null` and explain why in `notes`.
If the image is character-only and not meaningfully letter-based, mark it as `image_usable = false` because it is out of scope for this dataset.

## Scored categories

Each scored category uses a `1-10` scale.

- `legibility`
- `letter_structure`
- `line_quality`
- `composition`
- `color_harmony`
- `originality`

### Category definitions

#### Legibility

How easily the main word or forms can be read on first pass.

- `1-2`: Mostly unreadable. Confusion comes from poor design, not intentional complexity.
- `3-4`: Some letters can be parsed, but the read is weak or inconsistent.
- `5-6`: Readable with some effort. A few unclear joins, overlaps, or forced shapes.
- `7-8`: Clear read for the intended style. Complexity does not destroy recognition.
- `9-10`: Immediate read with strong style control. Complex forms still resolve cleanly.

#### Letter Structure

How well the letters are built as letters: proportion, consistency, internal logic, and relationship between forms.

- `1-2`: Letterforms are broken, inconsistent, or arbitrary. Parts do not function together.
- `3-4`: Basic letter idea is present, but structure is unstable or awkward.
- `5-6`: Competent structure with some weak proportions, forced add-ons, or uneven rhythm.
- `7-8`: Solid construction. Letters feel related, balanced, and intentionally designed.
- `9-10`: Exceptional structure. Complex moves still feel natural, unified, and precise.

#### Line Quality

How clean, controlled, and confident the drawn lines are.

- `1-2`: Shaky, scratchy, messy, or heavily corrected lines dominate the piece.
- `3-4`: Some control is visible, but wobble, inconsistency, or smudging is frequent.
- `5-6`: Generally competent lines with a few uneven edges or uncertain strokes.
- `7-8`: Clean, steady, confident linework with consistent control.
- `9-10`: Outstanding precision and confidence. Line weight and edge handling feel deliberate throughout.

#### Composition

How well the page is organized: spacing, balance, movement, focal emphasis, and use of negative space.

- `1-2`: Crowded, empty, or poorly arranged. The page feels accidental.
- `3-4`: Some layout intent exists, but balance or spacing problems weaken the piece.
- `5-6`: Functional layout. Main idea reads, but composition is ordinary or uneven.
- `7-8`: Strong organization. Elements support the piece and guide the eye well.
- `9-10`: Excellent page design. Spacing, hierarchy, and movement feel deliberate and refined.

#### Color Harmony

How well color, value, or shading choices support the piece.

If the sketch is black-and-white or color is not meaningfully used, set this to `null`.

- `1-2`: Color or shading clashes, muddies the read, or feels random.
- `3-4`: Some workable choices, but weak contrast, muddy fills, or distracting combinations.
- `5-6`: Competent support for the piece. Nothing broken, but not especially refined.
- `7-8`: Strong palette or shading control. Good contrast, balance, and support for readability.
- `9-10`: Excellent color or value design. Enhances style, depth, and clarity with clear intent.

#### Originality

How distinctive the visual solution feels from the image alone. Score visible uniqueness, not cultural certainty about biting.

- `1-2`: Generic, predictable, or heavily derivative-looking.
- `3-4`: Some style cues exist, but the piece still feels familiar or formulaic.
- `5-6`: Competent personal touches, though the overall solution is not especially surprising.
- `7-8`: Distinctive style choices with clear personality and integration.
- `9-10`: Highly individual and memorable. Strong visual identity without forced gimmicks.

## Weights

Use these weights for score aggregation:

- `legibility`: `15%`
- `letter_structure`: `25%`
- `line_quality`: `20%`
- `composition`: `15%`
- `color_harmony`: `10%`
- `originality`: `15%`

If `color_harmony` is `null`, redistribute its weight proportionally across the remaining scored categories.

## Overall score

Return:

- category scores on `1-10`
- `overall_score` on `1-10`

`overall_score` should reflect the weighted category result, rounded to the nearest whole number, with minor judgment allowed for obvious edge cases.

General meaning:

- `1-2`: very weak
- `3-4`: below average
- `5-6`: competent / average
- `7-8`: strong
- `9-10`: exceptional

## Scoring rules

- Judge only what is visible in the image.
- Do not infer artist reputation, wall context, or theme compliance unless explicit in the image.
- Do not reward unreadable complexity by default.
- Do not punish simple styles if they are executed cleanly and intentionally.
- Use `originality` conservatively; it is the noisiest category.
- Use `confidence = low` when the image is blurry, cropped, ambiguous, or the score depends on uncertain interpretation.

## Output shape

```json
{
  "file": "00127070-65ed-4ece-9069-93acdc73032a.jpg",
  "image_usable": true,
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

If unusable:

```json
{
  "file": "bad_example.jpg",
  "image_usable": false,
  "piece_type": null,
  "legibility": null,
  "letter_structure": null,
  "line_quality": null,
  "composition": null,
  "color_harmony": null,
  "originality": null,
  "overall_score": null,
  "confidence": "low",
  "notes": "Image is too blurry to evaluate."
}
```

## Human review queue

Prioritize manual review for:

- `confidence = low`
- `overall_score <= 3`
- `overall_score >= 9`
- `legibility <= 3` with `overall_score >= 7`
- `letter_structure <= 3` with `overall_score >= 7`
- `originality >= 8`

## Suggested teacher workflow

1. Score `50-100` images manually first to establish taste.
2. Run the teacher model on all images using this rubric.
3. Review the queue above.
4. Correct labels and lock a test split before training the student model.
