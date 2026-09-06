# PaddleOCR Hybrid PDF Recognition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:executing-plans` to implement this plan inline, task-by-task, with
> review checkpoints. Do not dispatch subagents unless the user explicitly asks.

**Goal:** Build a resumable, evidence-backed PaddleOCR and vision-model PDF-to-TeX
pipeline, selectively reconcile uncertain fields, and use it to regenerate the five
specified U2 documents.

**Architecture:** Lexoid owns page rendering, Paddle adapters, evidence normalization,
vision-based TeX generation, page caching, and ordered assembly. Texopt consumes the
versioned JSON evidence through a separate `reconcile` command, then performs its
existing structural optimization and compilation checks. The two packages exchange
files only; neither imports the other's internal Python modules.

**Tech Stack:** Python 3.10+, PaddleOCR 3.7.0, PaddlePaddle CPU, pypdfium2, Pillow,
OpenAI-compatible vision APIs, pytest, argparse/click, Docker Compose, XeLaTeX.

**Spec:** `docs/superpowers/specs/2026-09-04-paddleocr-hybrid-recognition-design.md`

## Global Constraints

- User update (2026-09-05): recognition quality and conversion performance take
  priority. The host has 16 GB RAM and Docker has about 9.48 GB available. Keep
  initial/retry DPI and avoid duplicate model work before reducing concurrency.
  The layout adapter now calls PPStructureV3's `PP-DocLayout_plus-L` through the
  public `LayoutDetection` API, avoiding PPStructureV3's second complete OCR
  pipeline. Worker memory is capped at 8 GB; global Docker allocation is unchanged.
- User update (2026-09-05): implement in `paddleocr-hybrid-refactor/`. Mirror every
  `Downloads/U1|U2|.../<relative>/<name>.pdf` to
  `data/U1|U2|.../<relative>/tex/<name>.tex` (recognition) and
  `data/U1|U2|.../<relative>/optimized/<name>.optimized.tex` (optimization). Retain both TeX
  files and associated evidence/reconciliation artifacts. Do not delete raw TeX
  on publication. Existing output files require input/config fingerprint checks
  before reuse. Preserve every intermediate category directory, including
  `U1/批次数据/<编号>` and `U2/批次/<编号>`. Group raw and optimized TeX in separate
  directories inside each numbered source directory.
- User update (2026-09-05): use `gpt-6-astra` for vision, field reconciliation,
  and difficult targeted repairs; use `gpt-5.6-sol` for semantic naming and
  ordinary optimization. Rebuild containers from refactor after implementation.

- Do not stage or commit any file. The user has permanently disabled Git commits for
  this work.
- Preserve unrelated working-tree changes made by the user or another Codex window.
- Run inline in the current workspace; do not create a worktree or dispatch subagents.
- The production default is CPU-only on the current Apple M5 macOS Docker host.
- Keep `lexoid latex` and `texopt optimise` backward compatible when new flags are
  omitted.
- Use 240 DPI for initial pages and 480 DPI for retry crops, configurable up to 600 DPI.
- Default limits are 2 render workers, Paddle batch size 4, vision concurrency 4, and
  reconciliation concurrency 2.
- PaddleOCR-VL runs only for unresolved complex tables, never for every page.
- The vision model remains the final page-to-TeX generator.
- Uncertain handwriting always retains a visible best guess and the literal
  `% #TODO #HANDWRITTEN` review marker.
- Evidence JSON is audit data and must never be rendered into the output PDF.
- Do not publish a document with a missing page, field, table cell, checkbox, failed
  structural validation, or failed two-pass XeLaTeX check.

---

### Task 1: Versioned Recognition Contracts

**Files:**
- Create: `lexoid/core/recognition/__init__.py`
- Create: `lexoid/core/recognition/models.py`
- Create: `tests/test_recognition_models.py`

**Interfaces:**
- Produces: `RecognitionConfig`, `RenderMetadata`, `RenderedPage`, `OcrBlock`,
  `LayoutRegion`, `TableCell`, `TableEvidence`, `FieldHistory`, `FieldEvidence`,
  `PageEvidence`, `VisionPageResult`, and `PageRecognitionResult`.
- Produces: `PageEvidence.to_dict() -> dict` and
  `PageEvidence.from_dict(payload: Mapping[str, object]) -> PageEvidence`.
- Contract version: `recognition/v1`.

- [x] **Step 1: Write serialization and validation tests**

```python
def test_page_evidence_round_trips_without_provider_objects():
    evidence = PageEvidence(
        schema="recognition/v1",
        page=21,
        render={"dpi": 240, "width": 1983, "height": 2804},
        ocr_blocks=(OcrBlock("operator", (10, 20, 80, 44), 0.91),),
        tables=(),
        fields=(FieldEvidence(
            field_id="LEX-P0021-V0003", label="operator", value="Zhang",
            bbox=(620, 1180, 910, 1260),
            paddle_text="Zhang", model_guess="Zhang", confidence=0.54,
            needs_review=True, history=(),
        ),),
        degraded_adapters=(),
    )
    assert PageEvidence.from_dict(evidence.to_dict()) == evidence


def test_page_evidence_rejects_unknown_schema():
    with pytest.raises(ValueError, match="schema"):
        PageEvidence.from_dict({"schema": "recognition/v9", "page": 1})
```

- [x] **Step 2: Run the tests and verify the contract is absent**

Run: `pytest -q tests/test_recognition_models.py`

Expected: collection fails because `lexoid.core.recognition.models` does not exist.

- [x] **Step 3: Add immutable dataclasses and strict JSON conversion**

```python
@dataclass(frozen=True)
class RecognitionConfig:
    ocr: str = "paddleocr"
    device: str = "cpu"
    initial_render_dpi: int = 240
    retry_crop_dpi: int = 480
    render_workers: int = 2
    paddle_batch_size: int = 4
    vision_concurrency: int = 4
    min_output_tokens: int = 2048
    max_output_tokens: int = 8192
    paddle_low_score: float = 0.70
    enable_vl_fallback: bool = True

    def __post_init__(self) -> None:
        if self.ocr not in {"none", "paddleocr"}:
            raise ValueError(f"Unsupported OCR mode: {self.ocr}")
        if self.device != "cpu" and not re.fullmatch(r"gpu(?::\d+)?", self.device):
            raise ValueError(f"Unsupported OCR device: {self.device}")
        if not 72 <= self.initial_render_dpi <= 600:
            raise ValueError("initial_render_dpi must be between 72 and 600")
        if not self.initial_render_dpi <= self.retry_crop_dpi <= 600:
            raise ValueError("retry_crop_dpi must be between initial DPI and 600")


@dataclass(frozen=True)
class PageRecognitionResult:
    page: int
    latex: str
    evidence: PageEvidence
    cache_key: str
    from_cache: bool = False
```

Keep bounding boxes in rendered-image pixel coordinates as four-integer tuples. Store
only JSON-native values in `to_dict`; reject missing page/render keys, malformed boxes,
and schema versions other than `recognition/v1` in `from_dict`.

- [x] **Step 4: Run the contract tests**

Run: `pytest -q tests/test_recognition_models.py`

Expected: all tests pass.

---

### Task 2: Configurable Rendering and Atomic Page Cache

**Files:**
- Create: `lexoid/core/recognition/rendering.py`
- Create: `lexoid/core/recognition/cache.py`
- Create: `tests/test_recognition_rendering.py`
- Create: `tests/test_recognition_cache.py`

**Interfaces:**
- Consumes: `RenderedPage`, `RecognitionConfig`, and `PageEvidence` from Task 1.
- Produces: `render_pdf_page(path: str, page: int, dpi: int,
  auto_orient: bool = True) -> RenderedPage`.
- Produces: `RecognitionCache(root: Path, key: str)` with `load_json`, `save_json`,
  `load_text`, `save_text`, and `invalidate` methods.
- Produces: `build_cache_key(pdf_sha256: str, config: RecognitionConfig,
  paddle_versions: Mapping[str, str], prompt_version: str,
  vision_model: str) -> str`.

- [x] **Step 1: Extend the render tests to assert DPI and single-page behavior**

```python
def test_render_uses_dpi_scale(monkeypatch):
    page = _Page(_fixture_image())
    document = _Document(_fixture_image())
    document._page = page
    render_pdf_page_from_document(document, page=0, dpi=240, auto_orient=False)
    assert page.render_scale == pytest.approx(240 / 72)


def test_rendered_page_keeps_physical_page_number_and_dimensions():
    rendered = render_pdf_page_from_document(
        _Document(_fixture_image()), page=0, dpi=240, auto_orient=False
    )
    assert (rendered.page, rendered.dpi) == (1, 240)
    assert (rendered.width, rendered.height) == rendered.image.size
```

Update the local `_Page.render()` fake to record `scale` rather than requiring 1.

- [x] **Step 2: Add cache-key, atomic-write, and corruption tests**

```python
def test_optimizer_version_does_not_change_recognition_cache_key():
    one = build_cache_key(PDF_SHA, CONFIG, {"paddleocr": "3.7.0"}, "p1", "vision")
    two = build_cache_key(PDF_SHA, CONFIG, {"paddleocr": "3.7.0"}, "p1", "vision")
    assert one == two


def test_cache_rejects_truncated_json(tmp_path):
    cache = RecognitionCache(tmp_path, "abc")
    cache.path("ocr", 1, ".json").parent.mkdir(parents=True)
    cache.path("ocr", 1, ".json").write_text("{", encoding="utf-8")
    assert cache.load_json("ocr", 1) is None
```

- [x] **Step 3: Run the focused tests and confirm failures**

Run: `pytest -q tests/test_conversion_utils.py tests/test_recognition_cache.py`

Expected: failures identify the missing DPI render and cache APIs.

- [x] **Step 4: Implement 240-DPI rendering without document-wide image retention**

Use `scale=dpi / 72.0`, normalize orientation once, save RGB PNG bytes once, and
return `RenderedPage(page=page + 1, dpi=dpi, width=..., height=..., image=...)`.
Keep `convert_pdf_page_to_base64()` unchanged as the legacy path; the hybrid path calls
the focused `recognition.rendering.render_pdf_page()` API directly. Task 6 owns routing
between the two paths.

- [x] **Step 5: Implement deterministic cache keys and sibling-temp atomic writes**

```python
def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
```

Hash canonical JSON containing the PDF SHA-256, render settings, Paddle versions,
prompt version, and vision model. Cache paths must exactly follow
`render|ocr|tables|draft|reconciled/<cache-key>/<page>.<ext>`.

- [x] **Step 6: Run the focused tests**

Run: `pytest -q tests/test_conversion_utils.py tests/test_recognition_cache.py`

Expected: all tests pass.

---

### Task 3: Paddle Text, Layout, Table, and VL Adapters

Implementation update: use `LayoutDetection` with `PP-DocLayout_plus-L` instead
of creating the whole `PPStructureV3` pipeline. The latter exceeded 8 GB during
real CPU inference because it also ran full-resolution server OCR. The text,
table, and on-demand VL stages remain separate. ARM64 static predictors disable
the crashing new IR path; VL uses the provider's JSON conversion interface.

**Files:**
- Create: `lexoid/core/recognition/paddle.py`
- Create: `tests/fixtures/recognition/paddle_text.json`
- Create: `tests/fixtures/recognition/ppstructure_table.json`
- Create: `tests/fixtures/recognition/table_pipeline.json`
- Create: `tests/test_paddle_adapters.py`

**Interfaces:**
- Consumes: normalized model types from Task 1.
- Produces: `PaddleTextAdapter.predict_batch(pages: Sequence[RenderedPage]) ->
  dict[int, tuple[OcrBlock, ...]]`.
- Produces: `PaddleLayoutAdapter.predict(page: RenderedPage) ->
  tuple[LayoutRegion, ...]`.
- Produces: `PaddleTableAdapter.predict(page: RenderedPage,
  regions: Sequence[LayoutRegion]) -> tuple[TableEvidence, ...]`.
- Produces: `PaddleVlFallbackAdapter.predict(page: RenderedPage,
  table: TableEvidence) -> TableEvidence`.
- Produces: `table_needs_vl(table: TableEvidence) -> bool`.

- [ ] **Step 1: Add provider-fixture normalization tests**

```python
def test_text_adapter_preserves_text_bbox_and_score(text_fixture, rendered_page):
    adapter = PaddleTextAdapter(pipeline=FakePipeline(text_fixture))
    blocks = adapter.predict_batch([rendered_page])[1]
    assert blocks[0] == OcrBlock("Batch ID", (42, 81, 233, 126), 0.98)


def test_table_adapter_preserves_row_and_column_spans(table_fixture, rendered_page):
    table = PaddleTableAdapter(pipeline=FakePipeline(table_fixture)).predict(
        rendered_page, [LayoutRegion("table", (40, 180, 1130, 1540), 0.99)]
    )[0]
    assert (table.rows, table.columns) == (8, 6)
    assert (table.cells[0].rowspan, table.cells[0].colspan) == (1, 2)


def test_vl_is_selected_only_for_inconsistent_topology():
    assert table_needs_vl(table(rows=2, columns=2, cells=[])) is True
    assert table_needs_vl(complete_two_by_two_table()) is False
```

- [ ] **Step 2: Run tests and verify the adapters are missing**

Run: `pytest -q tests/test_paddle_adapters.py`

Expected: import failure for `lexoid.core.recognition.paddle`.

- [ ] **Step 3: Implement lazy, long-lived Paddle model ownership**

Construct `PaddleOCR`, `PPStructureV3`, and `TableRecognitionPipelineV2` only on the
first call and retain each instance on its adapter. Configure PaddleOCR with
`use_doc_orientation_classify=False` because rendering already normalized orientation;
enable `use_doc_unwarping` and `use_textline_orientation` only through explicit config.
Do not import Paddle at module import time so fixture tests need no model downloads.

- [ ] **Step 4: Normalize provider objects through dictionary access only**

Map Paddle text arrays to integer pixel boxes and float scores. Crop only detected
table regions before table inference. Treat negative indices, spans outside declared
dimensions, overlapping non-spanning cells, empty dimensions, or missing cells as
inconsistent topology. Call VL only when `table_needs_vl()` is true and VL fallback is
enabled; retain both the table-pipeline result and fallback reason in evidence.

- [ ] **Step 5: Run adapter contract tests without live models**

Run: `pytest -q tests/test_paddle_adapters.py`

Expected: all fixture-backed tests pass and no network access occurs.

---

### Task 4: Evidence-Aware Vision TeX Generation

**Files:**
- Create: `lexoid/core/recognition/vision.py`
- Modify: `lexoid/core/prompt_templates.py`
- Create: `tests/test_recognition_vision.py`
- Modify: `tests/test_latex_value_ids.py`

**Interfaces:**
- Consumes: `RenderedPage`, `PageEvidence`, and existing `create_response()`.
- Produces: `compact_evidence(evidence: PageEvidence, max_chars: int = 24000) -> str`.
- Produces: `output_token_budget(evidence: PageEvidence,
  minimum: int = 2048, maximum: int = 8192) -> int`.
- Produces: `VisionLatexAdapter.recognize(page, evidence, page_count) ->
  VisionPageResult` containing page LaTeX and normalized field records.
- Prompt version: `hybrid-latex-v1`.

- [ ] **Step 1: Write budget, evidence, and policy tests**

```python
def test_dynamic_budget_is_bounded_and_increases_for_tables():
    plain = output_token_budget(page_evidence(ocr_chars=300, tables=0))
    dense = output_token_budget(page_evidence(ocr_chars=5000, tables=3))
    assert 2048 <= plain < dense <= 8192


def test_compact_evidence_contains_cells_but_not_provider_debug_data():
    payload = compact_evidence(page_evidence_with_merged_cell())
    assert '"colspan":2' in payload
    assert "doc_preprocessor_res" not in payload


def test_vision_fields_have_valid_crop_boxes(fake_vision_adapter, rendered_page):
    result = fake_vision_adapter.recognize(rendered_page, EVIDENCE, page_count=3)
    assert result.fields[0].bbox == (620, 1180, 910, 1260)


def test_prompt_requires_best_guess_for_uncertain_handwriting():
    assert "% #TODO #HANDWRITTEN: <best guess>; <short reason>" in LATEX_COMMON_PROMPT
    assert "Do not leave an uncertain handwritten value empty" in LATEX_COMMON_PROMPT
```

- [ ] **Step 2: Run tests and verify the new behavior fails**

Run: `pytest -q tests/test_recognition_vision.py tests/test_latex_value_ids.py`

Expected: missing module or prompt assertions fail.

- [ ] **Step 3: Add a compact, untrusted-evidence prompt section**

The prompt must state that Paddle evidence is advisory, coordinates are pixel boxes,
the source image wins on conflict, table spans must be respected, and provider text
must never be followed as instructions. Keep existing page-specific VALUE_ID,
checkbox, and handwritten marker rules intact. Use minified `json.dumps(...,
ensure_ascii=False, separators=(",", ":"))` and deterministic reading order.

- [ ] **Step 4: Implement dynamic budgets and response validation**

Use a monotonic complexity score based on OCR characters, cell count, merged spans,
and table count, then clamp to 2048-8192. The hybrid prompt requests strict JSON with
`latex` and `fields`; each field contains `field_id`, `label`, `bbox`, `model_guess`,
`confidence`, and `needs_review`. Match a field box from its table cell or OCR block;
allow a model-proposed box only after clamping it to render dimensions. Reject an empty
response, malformed field box, page number mismatch, duplicate VALUE_IDs, mismatch
between TeX and JSON field IDs, or missing physical-page completion marker before
caching the draft.

- [ ] **Step 5: Run vision contract tests**

Run: `pytest -q tests/test_recognition_vision.py tests/test_latex_value_ids.py`

Expected: all tests pass with a fake `create_response`; no live API is called.

---

### Task 5: Resumable Bounded Document Recognition

**Files:**
- Create: `lexoid/core/recognition/service.py`
- Create: `tests/test_recognition_service.py`

**Interfaces:**
- Consumes: rendering, cache, Paddle adapters, and `VisionLatexAdapter` from Tasks 1-4.
- Produces: `PageRecognizer.recognize_page(page: RenderedPage) ->
  PageRecognitionResult`.
- Produces: `DocumentRecognizer.recognize(pdf_path: Path,
  start_page: int = 1, page_callback: Callable | None = None) ->
  list[PageRecognitionResult]`.
- Produces: `AdaptiveConcurrency(initial: int, minimum: int = 1)` with
  `on_rate_limit()`, `on_success()`, and `limit`.

- [ ] **Step 1: Write tests for ordering, resume, degradation, and rate limiting**

```python
def test_out_of_order_calls_return_results_in_physical_page_order():
    recognizer = document_recognizer(completion_order=[3, 1, 2])
    results = recognizer.recognize(PDF)
    assert [result.page for result in results] == [1, 2, 3]


def test_resume_skips_valid_cached_stages():
    recognizer, calls = cached_document_recognizer(cached_pages={1, 2})
    recognizer.recognize(PDF, start_page=1)
    assert calls.vision_pages == [3]
    assert calls.paddle_pages == [3]


def test_paddle_failure_records_degraded_result_and_still_calls_vision():
    result = page_recognizer(paddle_errors=[RuntimeError("bad"), RuntimeError("bad")])
    assert result.evidence.degraded_adapters == ("paddle_text",)
    assert result.latex


def test_rate_limit_halves_concurrency_then_recovers_slowly():
    gate = AdaptiveConcurrency(initial=4)
    gate.on_rate_limit()
    assert gate.limit == 2
    for _ in range(gate.successes_per_recovery):
        gate.on_success()
    assert gate.limit == 3
```

- [ ] **Step 2: Run the service tests and verify failure**

Run: `pytest -q tests/test_recognition_service.py`

Expected: import failure for the missing service.

- [ ] **Step 3: Implement the bounded stage pipeline**

Use a two-worker render executor, one long-lived Paddle owner accepting batches of at
most four pages, and a four-worker vision executor. Bound queues to twice their worker
count to prevent 240-DPI images from filling 16 GB RAM. Retry Paddle text once; on a
second failure record degradation and continue vision-only. Retry 429/timeouts with
exponential backoff and jitter while `AdaptiveConcurrency` reduces the global vision
limit. Recover by one slot only after a configured streak of successes.

- [ ] **Step 4: Make cache validation stage-specific and assembly gap-strict**

Load render, OCR, table, and draft artifacts independently. Recompute only missing or
invalid stages. Deliver callbacks in physical-page order even when requests complete
out of order. Raise `IncompleteDocumentError(first_missing_page=N)` before assembly if
any expected page lacks a validated draft; never concatenate around a gap.

- [ ] **Step 5: Run service tests repeatedly to catch ordering races**

Run: `pytest -q tests/test_recognition_service.py --count=10` when `pytest-repeat` is
available; otherwise run `for i in 1 2 3 4 5; do pytest -q tests/test_recognition_service.py || exit 1; done`.

Expected: every run passes with identical page order.

---

### Task 6: Backward-Compatible Lexoid API and CLI

**Files:**
- Modify: `lexoid/api.py`
- Modify: `lexoid/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_latex_page_writer.py`
- Create: `tests/test_hybrid_latex_api.py`
- Modify: `docs/api.rst`
- Modify: `docs/cli.rst`

**Interfaces:**
- Extends: `parse_to_latex(..., ocr: str = "none", render_dpi: int = 240,
  evidence_output: str | None = None, cache_dir: str | None = None,
  vision_concurrency: int = 4, resume: bool = True, **kwargs) -> str`.
- Extends: `lexoid latex` with `--ocr [none|paddleocr]`, `--render-dpi`,
  `--evidence-output`, `--cache-dir`, `--vision-concurrency`, and
  `--resume/--no-resume`.

- [ ] **Step 1: Add CLI forwarding and compatibility tests**

```python
def test_latex_hybrid_flags_are_forwarded(runner, monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr("lexoid.cli.api_parse_to_latex",
                        lambda *args, **kwargs: seen.update(kwargs) or "tex")
    result = runner.invoke(app, ["latex", "-i", str(PDF), "-o", str(tmp_path / "x.tex"),
                                 "--ocr", "paddleocr", "--render-dpi", "240",
                                 "--evidence-output", str(tmp_path / "x.json")])
    assert result.exit_code == 0
    assert (seen["ocr"], seen["render_dpi"]) == ("paddleocr", 240)


def test_latex_default_still_uses_legacy_vision_path(monkeypatch):
    assert parse_to_latex(PDF, ocr="none", response_factory=fake_response)
```

- [ ] **Step 2: Run the API and CLI tests and verify failure**

Run: `pytest -q tests/test_cli.py tests/test_latex_page_writer.py tests/test_hybrid_latex_api.py`

Expected: new option and signature assertions fail.

- [ ] **Step 3: Route only `ocr=paddleocr` through `DocumentRecognizer`**

Keep the current serial behavior untouched for `ocr=none`. For hybrid mode, assemble
ordered page results, write each page with the existing checkpoint callback, and write
one top-level evidence file atomically:

```json
{"schema":"recognition/v1","document_sha256":"...","pipeline_version":"hybrid-latex-v1","pages":[]}
```

If `--evidence-output` is omitted, default to `<output-stem>.recognition.json` only in
hybrid mode. Require `--output` when evidence output or resume is explicitly requested.

- [ ] **Step 4: Document the exact commands and compatibility defaults**

Document:

```bash
lexoid latex --input input.pdf --output raw.tex --ocr paddleocr \
  --render-dpi 240 --evidence-output raw.recognition.json
```

State that the current macOS Docker host uses CPU and that `--ocr none` preserves the
legacy path.

- [ ] **Step 5: Run the full Lexoid unit suite**

Run: `pytest -q`

Expected: all tests pass without model downloads or external API calls.

---

### Task 7: Selective Field Reconciliation

**Files:**
- Create: `../../lexiod-pipeline/files/reconcile.py`
- Create: `../../lexiod-pipeline/files/test_reconcile.py`
- Modify: `../../lexiod-pipeline/files/cli.py`
- Modify: `../../lexiod-pipeline/files/pyproject.toml`

**Interfaces:**
- Consumes: `recognition/v1` JSON and raw TeX; no Lexoid Python import.
- Produces: `select_exceptional_fields(tex: str, evidence: dict,
  low_score: float = 0.70) -> list[FieldCandidate]`.
- Produces: `reconcile_document(tex_path: Path, source_pdf: Path,
  evidence_path: Path, output_path: Path, fields_path: Path,
  adapter: FieldReconcileAdapter, concurrency: int = 2,
  retry_dpi: int = 480) -> ReconcileReport`.
- Adds: `texopt reconcile INPUT --source-pdf PDF --recognition-evidence JSON
  -o OUTPUT --fields FIELDS_JSON`.

- [ ] **Step 1: Write exception-selection and synchronized-update tests**

```python
def test_selector_includes_every_exception_reason():
    selected = select_exceptional_fields(TEX_WITH_EXCEPTIONAL_FIELDS, EVIDENCE)
    assert {reason for item in selected for reason in item.reasons} == {
        "paddle_model_conflict", "handwritten_review_marker", "low_paddle_score",
        "critical_format_invalid", "unclear_checkbox", "unmatched_ocr_value",
    }


def test_confirmed_guess_updates_tex_and_history(tmp_path):
    report = reconcile_document_fixture(
        tmp_path, model_reply={"value": "Zhang", "confidence": 0.91,
                               "needs_review": False, "reason": "crop confirms strokes"}
    )
    assert "% #HANDWRITTEN: Zhang" in report.tex
    assert "% #TODO #HANDWRITTEN" not in report.tex
    assert report.fields[0]["history"][-1]["from"] == "Chang"
    assert report.fields[0]["value"] == "Zhang"


def test_failed_reconciliation_keeps_guess_and_review_marker():
    report = reconcile_document_fixture(adapter=FailingAdapter())
    assert r"\fieldvalue{\handwritten{Chang}}" in report.tex
    assert "% #TODO #HANDWRITTEN: Chang" in report.tex
```

- [ ] **Step 2: Run the reconciliation tests and verify failure**

Run: `cd ../../lexiod-pipeline && python -m pytest -q files/test_reconcile.py`

Expected: import failure for `files.reconcile`.

- [ ] **Step 3: Implement deterministic candidate selection**

Parse adjacent `#VALUE_ID`, `#FIELD_VALUE`, `#HANDWRITTEN`, `fieldvalue`, and
`checkboxfield` structures with brace-aware helpers already used in `fields.py`.
Select on normalized Paddle/model disagreement, marker presence, low raw OCR score,
configured regex failure for dates/batch IDs/quantities/units, unclear checkbox, or
unmatched nearby OCR. Deduplicate by `field_id` and store every cause in
`FieldCandidate.reasons: tuple[str, ...]`.

- [ ] **Step 4: Implement 480-DPI crop reconciliation with strict JSON replies**

Crop the field box plus nearby label context from the original physical page. Send the
crop, initial guess, Paddle text, label, and page context to the configured vision
model. Accept only JSON containing `value`, numeric `confidence`, boolean
`needs_review`, and `reason`. A confirmed reply may change the TeX payload and replace
the review marker with ordinary `#HANDWRITTEN`; an uncertain or failed reply keeps the
best guess and review marker. Escape TeX metacharacters without changing rendered text.
Declare `pypdfium2` and `Pillow` as texopt runtime dependencies so the standalone
`reconcile` command can render crops outside the combined Lexoid image.

- [ ] **Step 5: Write `fields.json` with auditable history**

Each record contains exactly `field_id`, `label`, `value`, `paddle_text`,
`model_guess`, `confidence`, `needs_review`, and `history`. Append timestamp-free,
deterministic history entries containing `stage`, `from`, `to`, and `reason`, so cached
reruns remain byte-stable. `needs_review` must equal the presence of the review marker
in reconciled TeX.

- [ ] **Step 6: Add and test the argparse command**

Run: `cd ../../lexiod-pipeline && python -m pytest -q files/test_reconcile.py`

Expected: all tests pass using fake crop and model adapters.

---

### Task 8: Optimizer and Batch Pipeline Stage Integration

**Files:**
- Modify: `../../lexiod-pipeline/files/fields.py`
- Modify: `../../lexiod-pipeline/files/cli.py`
- Modify: `../../lexiod-pipeline/files/pipeline_daemon.py`
- Modify: `../../lexiod-pipeline/files/pipeline_state.py`
- Modify: `../../lexiod-pipeline/files/pdf_batch.py`
- Modify: `../../lexiod-pipeline/files/test_field_annotation.py`
- Modify: `../../lexiod-pipeline/files/test_pipeline_state.py`
- Modify: `../../lexiod-pipeline/tests/test_pdf_batch.py`

**Interfaces:**
- Consumes: reconciled TeX and `fields.json` from Task 7.
- Produces stages: `recognize -> reconcile -> optimise` with distinct fingerprints,
  outputs, logs, retry state, and completion markers.
- Extends: `texopt optimise --registry INPUT_FIELDS_JSON` to preserve reconciliation
  provenance while updating `tex_line`, `semantic_alias`, and structural metadata.

- [ ] **Step 1: Add tests for stage commands and artifact paths**

```python
def test_batch_builds_three_distinct_commands(tmp_path):
    commands = build_stage_commands(SOURCE, WORK_ROOT, OUTPUT_ROOT, CONFIG)
    assert [command.stage for command in commands] == ["recognize", "reconcile", "optimise"]
    assert "--recognition-evidence" in commands[1].argv
    assert commands[2].argv[2].endswith(".reconciled.tex")
    assert "--llm-syntax-repair" not in commands[2].argv
    assert "--llm-repair-on-failure" in commands[2].argv


def test_reoptimization_never_calls_recognition(tmp_path):
    state = completed_recognition_and_reconciliation(tmp_path)
    calls = run_pipeline_fixture(state, optimizer_version="changed")
    assert calls.recognize == []
    assert calls.reconcile == []
    assert len(calls.optimise) == 1


def test_optimizer_preserves_reconciliation_history():
    records = annotate_fields(TEX, registry=RECONCILED_FIELDS)
    assert records[0].history == RECONCILED_FIELDS["fields"][0]["history"]
```

- [ ] **Step 2: Run pipeline tests and verify failure**

Run: `cd ../../lexiod-pipeline && python -m pytest -q files/test_pipeline_state.py files/test_field_annotation.py tests/test_pdf_batch.py`

Expected: failures show the pipeline still has only conversion and optimization.

- [ ] **Step 3: Extend state fingerprints without invalidating recognition**

Recognition fingerprint includes source SHA plus recognition configuration. Reconcile
fingerprint includes recognition fingerprint, reconciliation prompt/model, and retry
DPI. Optimize fingerprint includes reconciled TeX SHA plus optimizer version and args.
Store stage metadata as JSON in the existing SQLite job/event records. A changed
optimizer fingerprint must enqueue only optimization.

- [ ] **Step 4: Split commands and publication into three explicit stages**

Use artifact names:

```text
<stem>.tex
<stem>.recognition.json
<stem>.reconciled.tex
<stem>.fields.json
<stem>.optimized.tex
<stem>.report.json
<stem>.compile.log
```

Require validated page completeness before reconciliation and validated field/TeX
consistency before optimization. Publish final artifacts only after `optimise
--compile-check` succeeds twice. Retain raw, reconciled, probe, report, and logs on any
failure. Do not pass `--llm-syntax-repair` for an unconditional whole-document model
pass; keep `--llm-repair-on-failure` so structural model repair starts only after the
deterministic validator or compile probe identifies a defect.

- [ ] **Step 5: Merge registry provenance instead of replacing it**

Index incoming field records by original `field_id`. Preserve `value`, Paddle/model
values, confidence, review status, and history; add or refresh optimizer-owned
`tex_line`, `semantic_alias`, table context, and fingerprint fields. Reject duplicate
IDs and a mismatch between TeX value and registry value.

- [ ] **Step 6: Run all texopt tests**

Run: `cd ../../lexiod-pipeline && python -m pytest -q files/test_*.py tests`

Expected: all tests pass.

---

### Task 9: CPU Docker Runtime and Persistent Caches

**Files:**
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `.env_example`
- Modify: `../../lexiod-pipeline/files/Dockerfile`
- Modify: `../../lexiod-pipeline/files/docker-compose.yml`
- Modify: `../../lexiod-pipeline/files/.env.example`
- Modify: `../../lexiod-pipeline/files/README.md`
- Create: `../../lexiod-pipeline/files/test_runtime_config.py`

**Interfaces:**
- Consumes: hybrid Lexoid flags and three-stage pipeline configuration.
- Produces persistent mounts for `/data/cache/pages` and `/root/.paddlex`.
- Produces exact CPU defaults through environment variables.

- [ ] **Step 1: Write configuration tests**

```python
def test_cpu_defaults_match_approved_limits(monkeypatch):
    clear_pipeline_env(monkeypatch)
    config = Config.from_env()
    assert config.ocr_device == "cpu"
    assert (config.render_workers, config.paddle_batch_size) == (2, 4)
    assert (config.vision_concurrency, config.reconcile_concurrency) == (4, 2)
    assert (config.initial_render_dpi, config.retry_crop_dpi) == (240, 480)
```

- [ ] **Step 2: Run the configuration test and verify failure**

Run: `cd ../../lexiod-pipeline && python -m pytest -q files/test_runtime_config.py`

Expected: `Config` lacks the hybrid-recognition fields.

- [ ] **Step 3: Add explicit CPU and cache settings**

Add:

```dotenv
LEXOID_OCR=paddleocr
OCR_DEVICE=cpu
INITIAL_RENDER_DPI=240
RETRY_CROP_DPI=480
RENDER_WORKERS=2
PADDLE_BATCH_SIZE=4
VISION_CONCURRENCY=4
RECONCILE_CONCURRENCY=2
PAGE_CACHE_DIR=/data/cache/pages
PADDLE_HOME=/root/.paddlex
ENABLE_PADDLE_VL_FALLBACK=true
```

Mount named volumes or host directories for both cache paths. Do not mount source code
into runtime containers. Do not add CUDA packages to the Apple-host image. Preserve a
future `OCR_DEVICE` switch for a separately built Linux NVIDIA image.

- [ ] **Step 4: Build and inspect the combined image**

Run: `cd ../../lexiod-pipeline/files && docker compose build pipeline-cron`

Expected: the image builds with PaddleOCR, PPStructureV3,
TableRecognitionPipelineV2, PaddleOCR-VL fallback support, XeLaTeX, and Poppler.

- [ ] **Step 5: Run offline unit tests inside the image**

Run: `docker run --rm --entrypoint python lexoid-texopt-pipeline:local -m pytest -q`

Expected: all bundled unit tests pass without downloading models or calling APIs.

- [ ] **Step 6: Warm each Paddle model once and verify cache reuse**

Run one fixture page through text, layout, table, and VL initialization with
`PADDLE_HOME` mounted. Restart the container and repeat with network disabled.

Expected: the second run initializes from the persistent model cache and performs no
download.

---

### Task 10: Five-Document U2 Acceptance and Production Run

**Files:**
- Create: `../../lexiod-pipeline/files/acceptance.py`
- Create: `../../lexiod-pipeline/files/test_acceptance.py`
- Create at runtime: `/Users/dongdong/code/lexiod/Downloads/U2/_hybrid-baseline.json`
- Create at runtime: `/Users/dongdong/code/lexiod/Downloads/U2/_hybrid-result.json`

**Interfaces:**
- Consumes exactly these PDFs: `S22C-726080515020.pdf`,
  `S22C-726080515560.pdf`, `S22C-726080516000.pdf`,
  `S22C-726080516040.pdf`, and `S22C-726080516090.pdf`.
- Produces a deterministic acceptance report covering all 375 physical pages.

- [ ] **Step 1: Write acceptance metric tests**

```python
def test_acceptance_rejects_missing_or_duplicate_pages():
    result = evaluate(document_pages=[1, 2, 2, 4], expected_pages=4)
    assert result.passed is False
    assert result.missing_pages == [3]
    assert result.duplicate_pages == [2]


def test_acceptance_thresholds_are_exact():
    result = evaluate_metrics(critical_improvement=.30, table_accuracy=.95,
                              checkbox_accuracy=.98, speed_improvement=.60,
                              compiled_documents=5)
    assert result.passed is True
```

- [ ] **Step 2: Run acceptance unit tests**

Run: `cd ../../lexiod-pipeline && python -m pytest -q files/test_acceptance.py`

Expected: all metric and page-accounting tests pass.

- [ ] **Step 3: Capture the current vision-only baseline on the reviewed page sample**

Record exact source hashes, selected page numbers, critical-field errors, checkbox
errors, table topology errors, total wall time, and model/API identifiers. Store only
metrics and identifiers in `_hybrid-baseline.json`; do not copy PDFs or field contents
into source control.

- [ ] **Step 4: Run hybrid recognition and reconciliation on the five PDFs**

Use the U2 input directory and a separate work directory so source PDFs remain
read-only. Require a preflight total of exactly 375 pages before starting. Run with
CPU defaults, `gpt-5.6-luna` for page recognition, `gpt-5.6-terra` for reconciliation
and optimization, persistent caches, and stage logs. Resume rather than restart after
any API or container interruption.

- [ ] **Step 5: Optimize and compile every reconciled document**

Run structural optimization with two-pass XeLaTeX validation. Verify each output has
exactly one ordered physical-page marker per source page, no missing field/table/
checkbox evidence, and all five final `.optimized.tex` artifacts plus JSON reports and
compile logs.

- [ ] **Step 6: Evaluate acceptance gates**

Require critical batch ID/date/number error improvement of at least 30%, table topology
accuracy at least 95%, checkbox accuracy at least 98%, wall-clock improvement at least
60% against the same-provider serial baseline, successful two-pass compilation for all
five documents, and zero recognition calls during cached re-optimization.

- [ ] **Step 7: Stop rollout if any gate fails**

If a gate fails, keep the legacy vision-only path enabled, retain all stage artifacts,
and report the exact pages/fields/tables responsible. Correct the bounded failure and
rerun only invalid cache stages. Enable hybrid recognition for further U1/U2 work only
after every gate passes.

---

## Final Verification

- [ ] Run Lexoid tests: `cd /Users/dongdong/code/lexiod/lexiod/Lexoid && pytest -q`.
- [ ] Run texopt tests: `cd /Users/dongdong/code/lexiod/lexiod-pipeline && python -m pytest -q files/test_*.py tests`.
- [ ] Confirm no live-network test runs in either unit suite.
- [ ] Confirm the five U2 outputs cover exactly 375 ordered physical pages.
- [ ] Confirm all five outputs compile twice with XeLaTeX.
- [ ] Confirm cached optimization produces zero PDF, Paddle, or vision recognition calls.
- [ ] Inspect `git status --short` only for review; do not stage or commit any file.
