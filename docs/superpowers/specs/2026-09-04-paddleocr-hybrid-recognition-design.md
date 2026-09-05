# PaddleOCR Hybrid PDF Recognition Design

## Status

Approved in conversation on 2026-09-04. The temporary RTX 5090 direction was withdrawn
on 2026-09-05; the production target remains the current CPU Docker host.

## Context

Lexoid currently converts a PDF to LaTeX by rendering each physical page as an
image and sending pages sequentially to a vision model. The current implementation
has three quality and throughput constraints:

- PDF pages are rendered with `pypdfium2` at `scale=1`, approximately 72 DPI.
- Vision-model output defaults to 1024 tokens per page.
- `parse_to_latex()` waits for each page request before starting the next one.

This is insufficient for dense Chinese GMP batch records containing small printed
text, merged table cells, handwriting, checkboxes, rotated scans, and long
documents. The system already depends on PaddleOCR 3.7.0 and has access to
`PaddleOCR`, `PaddleOCRVL`, `PPStructureV3`, and
`TableRecognitionPipelineV2`, but the PDF-to-LaTeX path does not currently use
their combined evidence.

The selected design is accuracy-first and runs in Docker on the current macOS host
without GPU access.

## Goals

- Improve printed Chinese text, numbers, batch IDs, dates, handwriting,
  checkboxes, and table structure recognition.
- Preserve the existing page-specific `VALUE_ID` scheme and page checkpoints.
- Let the vision model make the final recognition decision using the source image
  and Paddle evidence.
- Preserve a best guess for uncertain handwriting and mark it for human review.
- Recheck only conflicting or uncertain fields instead of recognizing every page
  twice.
- Allow evidence-backed field corrections before structural TeX optimization.
- Reduce first-run wall-clock time by at least 60 percent relative to the current
  serial pipeline, subject to upstream API rate limits.
- Make retries and re-optimization reuse durable page-level artifacts.

## Non-goals

- Fully offline PDF-to-LaTeX conversion.
- Removing the vision model from the recognition path.
- Treating Paddle-generated HTML or Markdown as the final LaTeX document.
- Automatically accepting uncertain GMP field values without subsequent human
  review.
- Running PaddleOCR-VL on every page by default on the CPU-only host.
- Changing the existing business meaning or ordering of fields.

## Architecture

### Pipeline stages

The pipeline has three explicit stages:

```text
recognize   PDF -> raw.tex + recognition.json
reconcile   review exceptional fields -> reconciled.tex + fields.json
optimise    table/syntax/SyncTeX work -> optimized.tex + report.json
```

The user-facing workflow may group `reconcile` and `optimise` under "AI
optimization", but the implementations remain separate. Recognition correction
requires source-image evidence; structural LaTeX repair must not invent or alter
field values without that evidence.

### Recognition module

Add a deep `PageRecognition` module with one external interface:

```python
recognize_page(page: RenderedPage) -> PageRecognitionResult
```

`PageRecognition` owns model initialization, batching, preprocessing, evidence
normalization, and caching. Callers do not construct Paddle pipelines or interpret
their provider-specific results. `PageRecognitionResult` contains the normalized
`PageEvidence`, the generated page LaTeX, and the page checkpoint metadata.

Internal adapters provide the behavior that varies:

- `PaddleTextAdapter`: printed text, bounding boxes, and recognition scores.
- `PaddleLayoutAdapter`: page regions and table locations through
  `PPStructureV3`.
- `PaddleTableAdapter`: cell topology through
  `TableRecognitionPipelineV2`.
- `PaddleVlFallbackAdapter`: complex table fallback only.
- `VisionLatexAdapter`: page image plus compact evidence to LaTeX.
- `FieldReconcileAdapter`: high-resolution crop plus evidence to a final value.

The seam is justified because multiple adapters perform genuinely different
recognition strategies and must be independently benchmarked and replaced.

### End-to-end data flow

```text
PDF
  -> render page at initial DPI
  -> classify orientation and normalize page
  -> detect layout and table regions
  -> recognize printed text and coordinates
  -> recognize table cell topology where tables exist
  -> send page image and compact evidence to the vision model
  -> save page LaTeX and recognition evidence atomically
  -> select exceptional fields
  -> crop exceptional fields at high DPI and reconcile them
  -> assemble pages in physical-page order
  -> optimize table and LaTeX structure
  -> compile twice with XeLaTeX
  -> publish for human review
```

## Image Processing

- Initial full-page rendering defaults to 240 DPI and is configurable.
- Field and table retry crops default to 480 DPI and are configurable up to 600
  DPI.
- Orientation classification runs before recognition.
- Deskew, document unwarping, and text-line orientation are enabled when their
  measured benefit exceeds their CPU cost on the acceptance corpus.
- A page image is rendered once for a given cache key and reused by all adapters.
- Blank-page detection may skip model calls only when a deterministic detector
  finds no visible content; blank pages still receive a physical page checkpoint.

## Table Recognition

`PPStructureV3` first locates tables. Only detected table regions are passed to
`TableRecognitionPipelineV2`. Traditional PaddleOCR supplies text and coordinates
for cells. `PaddleOCRVL` is reserved for tables whose topology is inconsistent or
cannot be resolved by the table-specific pipeline.

Normalized table evidence has this shape:

```json
{
  "page": 12,
  "bbox": [42, 180, 1130, 1540],
  "rows": 8,
  "columns": 6,
  "cells": [
    {
      "row": 2,
      "column": 1,
      "rowspan": 1,
      "colspan": 2,
      "text": "操作人",
      "bbox": [45, 320, 280, 390],
      "score": 0.98
    }
  ]
}
```

The vision model receives the table skeleton together with the original table
image. It remains responsible for final LaTeX generation, handwriting, checkbox
semantics, and resolving ambiguous structure. The deterministic optimizer verifies
declared column counts, row spans, and `\multicolumn` use before publication.

## Handwriting And Field Policy

Initial recognition follows the existing best-guess policy:

- Recognizable handwriting is emitted as the recognized value.
- Uncertain handwriting is emitted as the model's best guess.
- An uncertain value receives `% #TODO #HANDWRITTEN: <guess>; <reason>`.
- The visible value remains `\fieldvalue{\handwritten{<guess>}}`.
- A checkbox remains one of `checked`, `unchecked`, or `unclear`.

Example:

```latex
% #VALUE_ID: LEX-P0021-V0003
% #FIELD_VALUE: 操作人
% #TODO #HANDWRITTEN: 张伟; handwriting uncertain, model best guess
\fieldvalue{\handwritten{张伟}}
```

The reconciliation stage runs only when at least one of these conditions holds:

- Paddle text and the vision-model value disagree after normalization.
- The initial model emitted a handwritten TODO marker.
- Paddle recognition scores are below the configured threshold.
- A critical value fails its format rule, such as a date, batch ID, quantity, or
  unit.
- A checkbox state is unclear.
- A detected OCR value has no corresponding TeX field.

Reconciliation uses a high-resolution crop, nearby printed labels, Paddle text,
the initial model guess, and the original page image. If it confirms a value, it
may update the field and replace the TODO marker with a normal `#HANDWRITTEN`
marker. If uncertainty remains, it keeps the best guess and TODO marker.

Structural syntax repair runs after reconciliation. It may escape field payloads
without changing rendered meaning, but it may not make unsupported semantic field
changes.

## Evidence Contract

Recognition evidence is stored separately from TeX. It is not rendered in the
final PDF.

```json
{
  "document_sha256": "...",
  "pipeline_version": "...",
  "pages": [
    {
      "page": 21,
      "render": {"dpi": 240, "width": 1983, "height": 2804},
      "ocr_blocks": [],
      "tables": [],
      "fields": [
        {
          "field_id": "LEX-P0021-V0003",
          "label": "操作人",
          "bbox": [620, 1180, 910, 1260],
          "value": "张伟",
          "paddle_text": "张仿",
          "model_guess": "张伟",
          "confidence": 0.54,
          "needs_review": true,
          "history": []
        }
      ]
    }
  ]
}
```

`confidence` is an audit-only reconciliation decision score, while raw Paddle
block scores remain on their OCR blocks. It is not treated as a calibrated
probability and cannot by itself approve a field or suppress human review.
`bbox` is the field's rendered-page pixel box, derived from a table cell or matched
OCR block and validated against the render dimensions. It is used for high-resolution
reconciliation crops and is not rendered in TeX.
`needs_review` is derived from the presence of the handwritten TODO marker. A
field history records initial and reconciled values so human review can trace
changes without adding metadata to the visible document.

Final artifacts are:

```text
<stem>.optimized.tex
<stem>.fields.json
<stem>.recognition.json
<stem>.report.json
<stem>.compile.log
```

## Interfaces

Extend raw conversion with optional recognition controls:

```bash
lexoid latex \
  --input input.pdf \
  --output raw.tex \
  --ocr paddleocr \
  --render-dpi 240 \
  --evidence-output raw.recognition.json
```

Expose reconciliation as a separate command:

```bash
texopt reconcile raw.tex \
  --source-pdf input.pdf \
  --recognition-evidence raw.recognition.json \
  -o reconciled.tex \
  --fields reconciled.fields.json
```

Structural optimization consumes the reconciled document:

```bash
texopt optimise reconciled.tex \
  --registry final.fields.json \
  --report final.report.json \
  --compile-check \
  -o final.optimized.tex
```

Existing `lexoid latex` and `texopt optimise` invocations remain valid. The batch
pipeline enables hybrid recognition and reconciliation through configuration.

## Concurrency And Throughput

The CPU-only default configuration is:

```text
render_workers=2
paddle_batch_size=4
vision_concurrency=4
reconcile_concurrency=2
initial_render_dpi=240
retry_crop_dpi=480
ocr_device=cpu
```

- A single long-lived Paddle worker owns model instances to avoid repeated model
  loading and excessive memory use.
- Rendering, Paddle inference, remote vision calls, and reconciliation operate as
  bounded pipeline stages.
- Vision calls are asynchronous and limited globally across documents.
- HTTP 429 or repeated upstream timeouts reduce concurrency automatically, with a
  slow recovery after successful calls.
- Pages are written independently and assembled by page number, so request
  completion order cannot reorder the document.
- Dynamic output budgets range from 2048 to 8192 tokens based on OCR text volume,
  table density, and observed page complexity.
- Syntax repair is invoked only when deterministic validation finds a defect.
- Semantic field naming uses printed labels directly and batches ambiguous tables
  into fewer model calls.

Existing logs show typical serial page latency of 30 to 100 seconds, with some
pages taking longer than two minutes. At concurrency four, the five selected U2
documents, totaling 375 pages, are expected to take approximately 1.5 to 3 hours
instead of 6 to 10 hours, subject to model-provider rate limits.

## Cache And Resume

Durable artifacts are stored per physical page:

```text
render/<cache-key>/<page>.png
ocr/<cache-key>/<page>.json
tables/<cache-key>/<page>.json
draft/<cache-key>/<page>.tex
reconciled/<cache-key>/<page>.tex
```

The cache key contains:

```text
PDF SHA-256
+ rendering configuration
+ Paddle model and pipeline versions
+ prompt version
+ vision-model identifier
```

Each artifact is written to a temporary sibling and atomically renamed after
validation. Resume scans the expected page set and processes only missing or
invalid stages. Changing optimizer code does not invalidate page recognition.
Changing image, Paddle, prompt, or vision-model configuration invalidates only the
affected recognition keys.

The Paddle model cache remains in a persistent Docker volume so restarting a
worker does not download models again.

## Error Handling

- Paddle text failure: retry the page once, then use vision-only recognition and
  record the degraded adapter result in `recognition.json`.
- Layout or table failure: preserve OCR text and coordinates, then let the vision
  adapter infer structure from the page image.
- Vision API rate limiting: exponential backoff with jitter and adaptive global
  concurrency.
- Vision page failure: continue independent pages and retry only the failed page.
- Reconciliation failure: preserve the initial best guess and TODO marker.
- Invalid or missing page output: stop assembly at the first gap, keep the job
  incomplete, and do not publish a final document.
- Structural TeX failure: retain raw, reconciled, probe, and compile artifacts; do
  not publish a final optimized TeX.
- XeLaTeX failure: fail the job after the configured repair attempts and retain
  the complete log.
- Cache corruption: reject the artifact by schema and hash validation, then
  recompute that stage.

No fallback may silently remove a field, table cell, page, or checkbox.

## Testing Strategy

### Module tests

- Normalize Paddle text, coordinates, scores, table cells, row spans, and column
  spans into `PageEvidence`.
- Verify cache keys and atomic cache writes.
- Verify page assembly remains ordered under out-of-order completion.
- Verify resume processes only missing pages.
- Verify adaptive concurrency decreases on rate limiting and later recovers.
- Verify dynamic token budgets stay within configured limits.
- Verify exception selection includes every conflict, TODO marker, invalid critical
  value, unclear checkbox, and unmatched OCR value.
- Verify reconciliation updates TeX and JSON consistently.

### Adapter contract tests

Use fixed provider fixtures for each adapter. Tests exercise the same
`recognize_page()` interface used by production callers and assert both evidence
and generated page LaTeX. No unit test depends on live model downloads or external
model APIs.

### Golden-page acceptance corpus

Select representative pages from the five U2 documents:

- dense printed Chinese text;
- handwritten names, dates, and numeric values;
- checked, unchecked, and unclear checkboxes;
- simple and merged-cell tables;
- rotated, skewed, or low-contrast scans;
- multi-page table continuations.

Human-reviewed ground truth records visible text, critical fields, checkbox
states, table topology, and page ordering.

### Acceptance criteria

- All five documents produce complete optimized TeX outputs.
- All 375 physical page markers are present exactly once and in order.
- Critical batch ID, date, and numeric error rate improves by at least 30 percent
  from the current baseline.
- Table row, column, and merged-cell topology accuracy is at least 95 percent.
- Checkbox-state accuracy is at least 98 percent.
- Every final document passes two XeLaTeX compilation passes.
- No field, table cell, checkbox, or page is silently dropped.
- Four-way vision concurrency reduces wall-clock time by at least 60 percent from
  the serial baseline under the same provider limits.
- A cached re-optimization performs no PDF recognition calls.

## Rollout

1. Add fixtures and measure the current vision-only baseline.
2. Add rendering controls and the `PageEvidence` contract.
3. Integrate Paddle text and orientation with durable cache support.
4. Add layout and table adapters, benchmark `TableRecognitionPipelineV2` against
   `PaddleOCRVL`, and keep the stronger adapter as the complex-table fallback.
5. Add evidence-aware vision prompting and dynamic token budgets.
6. Add page-level concurrency, ordered assembly, rate limiting, and resume.
7. Add selective field reconciliation and field history.
8. Integrate the stages into the Docker batch worker.
9. Run the five-document acceptance corpus before enabling the new path for U1 or
   other bulk workloads.

The rollout is controlled by a hybrid-recognition configuration flag until the
acceptance criteria pass. Existing vision-only conversion remains available as a
fallback during rollout.

## Expected Code Areas

Lexoid:

- `lexoid/api.py`: call the recognition module and preserve existing callbacks.
- `lexoid/cli.py`: expose OCR, rendering, evidence, concurrency, and resume options.
- `lexoid/core/conversion_utils.py`: configurable rendering and page preprocessing.
- `lexoid/core/prompt_templates.py`: consume compact OCR and table evidence.
- `lexoid/core/recognition/`: new deep module and internal adapters.

Texopt pipeline:

- `files/cli.py`: add the `reconcile` command while preserving structural
  optimization behavior.
- `files/pipeline_daemon.py`: orchestrate recognize, reconcile, and optimize stages.
- `files/pipeline_state.py`: persist per-page and per-stage state.
- `files/fields.py`: merge reconciliation provenance into field records.
- Docker configuration: persist Paddle and page caches without mounting source
  code into runtime containers.
