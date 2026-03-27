# Modal API Spec

This document describes the deployed Modal inference API served by [modal_app.py](C:/Users/qwert/Desktop/custom_model/deploy/modal_app.py).

Important:

- Do not call this API directly from a public browser client with the bearer token.
- Store the token on your backend, server action, edge function, or API route.
- Let the frontend call your own backend, and let your backend call Modal.

## Base URLs

- Health: `https://bretzniklas--graffiti-student-v1-graffitistudentservice-health.modal.run`
- Predict: `https://bretzniklas--graffiti-student-v1-graffitistudentservice-predict.modal.run`

## Authentication

Every request must include:

```http
Authorization: Bearer <YOUR_SECRET_TOKEN>
```

## Health Endpoint

Method:

```http
GET /
```

Example:

```http
GET https://bretzniklas--graffiti-student-v1-graffitistudentservice-health.modal.run
Authorization: Bearer <YOUR_SECRET_TOKEN>
```

Success response:

```json
{
  "status": "ok",
  "model_version": "student-v1",
  "app": "graffiti-student-v1"
}
```

## Prediction Endpoint

Method:

```http
POST /
```

Headers:

```http
Authorization: Bearer <YOUR_SECRET_TOKEN>
Content-Type: application/json
```

Request body:

```json
{
  "image_b64": "<base64-encoded-image>",
  "filename": "example.jpg",
  "include_debug": false
}
```

### Request Fields

- `image_b64`
  Required. Base64 string of the image bytes.
- `filename`
  Optional. Echoed back in the response.
- `include_debug`
  Optional boolean. Default `false`.

## Success Response

Example:

```json
{
  "filename": "example.jpg",
  "image_usable": true,
  "medium": "wall_piece",
  "overall_score": 7,
  "legibility": 6,
  "letter_structure": 7,
  "line_quality": 7,
  "composition": 7,
  "color_harmony": 7,
  "originality": 7,
  "request_id": "uuid-here",
  "model_version": "student-v1"
}
```

### Medium Values

- `paper_sketch`
- `wall_piece`
- `digital`
- `other_or_unclear`

### Score Fields

Score fields are integers from `1` to `10` when applicable:

- `overall_score`
- `legibility`
- `letter_structure`
- `line_quality`
- `composition`
- `color_harmony`
- `originality`

Fields may be `null` when not applicable.

## Response Rules

1. If `image_usable = false`, all score fields are `null`.
2. If `image_usable = true` and `medium` is `digital` or `other_or_unclear`, all score fields are `null`.
3. If `image_usable = true` and `medium` is `paper_sketch` or `wall_piece`, score fields are returned.
4. `color_harmony` may be `null` even for usable scored images if color is not applicable.

## Error Response

Errors return structured JSON:

```json
{
  "error": "invalid_image",
  "message": "The uploaded content is not a valid image.",
  "request_id": "uuid-here",
  "model_version": "student-v1"
}
```

### Common Error Codes

- `invalid_base64`
- `invalid_image`
- `image_too_large`
- `image_too_small`
- `image_too_large_dimensions`
- `internal_error`

## Backend Integration Pattern

Recommended flow:

1. Frontend uploads an image to your backend.
2. Your backend converts the file to base64 if needed.
3. Your backend calls the Modal predict endpoint with the bearer token.
4. Your backend returns a sanitized response to the frontend.

## Recommended TypeScript Types

```ts
type PredictRequest = {
  image_b64: string;
  filename?: string;
  include_debug?: boolean;
};

type PredictResponse = {
  filename?: string;
  image_usable: boolean;
  medium: "paper_sketch" | "wall_piece" | "digital" | "other_or_unclear";
  overall_score: number | null;
  legibility: number | null;
  letter_structure: number | null;
  line_quality: number | null;
  composition: number | null;
  color_harmony: number | null;
  originality: number | null;
  request_id: string;
  model_version: string;
  debug?: {
    usable_probability: number;
    usable_threshold: number;
    color_applicable_probability: number;
    color_threshold: number;
  };
};

type ApiError = {
  error: string;
  message: string;
  request_id: string;
  model_version: string;
};
```

## Product Guidance

For `v1`, trust these outputs most:

- `image_usable`
- `overall_score`

Treat `medium` as supporting metadata rather than a highly reliable classification output.
