# Labeling Cheat Sheet v1

Use this during labeling sessions. This is the fast reference. For full examples and exact anchor scores, use [anchor_pack_v1.md](C:/Users/qwert/Desktop/custom_model/exports/anchor_pack_v1.md).

## Session rules

- Judge only what is visible.
- Keep `medium` strict.
- Mark character-only as `Unusable -> Character-only / out of scope`.
- Use `Color Harmony = Not applicable` when color or shading is not really part of the piece.
- Use `confidence = low` when you feel like you are guessing.

## Score shorthand

- `2` = weak / broken
- `4` = below average
- `6` = competent
- `8` = strong
- `10` = exceptional

Use even numbers by default. Use odd numbers only when the piece clearly sits between bands.

## Category shortcuts

- `Legibility`: can you read it without fighting it?
- `Letter Structure`: do the letters feel built correctly, or random and forced?
- `Line Quality`: are the marks clean and controlled?
- `Composition`: does the whole page or wall feel balanced and intentional?
- `Color Harmony`: do the color or shading choices help the piece?
- `Originality`: does it feel generic, or does it have a distinct look?

## Medium reminders

### Paper sketches

- Do not over-penalize notebook paper, rough photo quality, or unfinished fill.
- Do penalize weak construction, shaky outlines, and lazy layout.

### Wall pieces

- Do not give automatic high scores just because the paint looks polished.
- Keep `letter_structure` separate from spray finish.

### Digital

- Score it normally, but keep confidence honest if it feels overly artificial or hard to judge.

## Paper anchors

### Low

`P2` overall `3`

![Paper low](../images/1866d8af-4e5d-4886-a3ff-2a1b585b917d.jpg)

- weak structure
- weak execution
- readable enough, but not strong

### Mid

`P4` overall `4`

![Paper mid](../images/071690cb-68a4-48ff-836f-863675a7c88f.jpg)

- fairly readable
- still weak in build and finish
- low-mid, not truly strong

### High

`P6` overall `8`

![Paper high](../images/1976b092-8fe6-4f07-8d2d-8cabe505c6fd.jpg)

- strong structure
- strong control
- color and composition support the style

## Wall anchors

### Low

`W1` overall `4`

![Wall low](../images/4acbf5fe-7b90-4761-9f50-8d3b9e9a029a.jpg)

- basic throwie
- limited originality
- weak finish

### Mid

`W3` overall `7`

![Wall mid](../images/13f276a1-ca0d-4f5b-823c-b0d1e8718ab2.jpg)

- solid piece
- real style effort
- good, but not top-tier

### High

`W6` overall `10`

![Wall high](../images/0e95a5f5-63d1-4e82-8757-a8aad973d615.jpg)

- exceptional across almost every category
- this is near the top of the current dataset

## Quick workflow

1. Check `usable / unusable`.
2. Set `medium`.
3. Set `piece_type`.
4. Score categories.
5. Compare mentally to the nearest anchor.
6. Set `confidence`.

## Drift checks

If you catch yourself doing any of these, pause and recalibrate:

- giving wall pieces free points for polish
- punishing paper pieces for being on notebook paper
- using `originality` too generously
- giving too many `7-8` scores in a row
- scoring character-only pieces as valid graffiti letters
