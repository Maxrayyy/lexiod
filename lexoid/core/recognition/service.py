"""Bounded page recognition with durable stages and ordered checkpoints."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import replace
import hashlib
from importlib.metadata import PackageNotFoundError, version
import io
from pathlib import Path
import random
import threading
import time

from PIL import Image

from .cache import RecognitionCache, _atomic_write, build_cache_key
from .models import (
    PageEvidence, PageRecognitionResult, RecognitionConfig, RenderedPage,
    RenderMetadata, VisionPageResult,
)
from .paddle import (
    PaddleLayoutAdapter, PaddleTableAdapter, PaddleTextAdapter,
    PaddleVlFallbackAdapter, table_needs_vl,
)
from .rendering import render_pdf_page
from .vision import (
    PROMPT_VERSION, RecoverableVisionError, VisionLatexAdapter,
    fallback_page_latex, normalize_field_annotation_spacing,
    validate_page_checkpoint, validate_page_latex,
)


class IncompleteDocumentError(RuntimeError):
    def __init__(self, first_missing_page, reason="no valid draft"):
        self.first_missing_page = first_missing_page
        super().__init__(f"Incomplete document at physical page {first_missing_page}: {reason}")


class AdaptiveConcurrency:
    successes_per_recovery = 8

    def __init__(self, initial, minimum=1):
        if not 1 <= minimum <= initial:
            raise ValueError("Invalid concurrency limits")
        self.limit, self.maximum, self.minimum = initial, initial, minimum
        self._active = self._successes = 0
        self._condition = threading.Condition()

    def on_rate_limit(self):
        with self._condition:
            self.limit = max(self.minimum, self.limit // 2)
            self._successes = 0

    def on_success(self):
        with self._condition:
            self._successes += 1
            if self._successes >= self.successes_per_recovery:
                self.limit = min(self.maximum, self.limit + 1)
                self._successes = 0
                self._condition.notify_all()

    @contextmanager
    def slot(self):
        with self._condition:
            self._condition.wait_for(lambda: self._active < self.limit)
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()


def document_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _page_count(path):
    import pypdfium2 as pdfium
    document = pdfium.PdfDocument(str(path))
    try:
        return len(document)
    finally:
        document.close()


class PageRecognizer:
    def __init__(self, config, cache, page_count, vision, text, layout, tables,
                 vl=None, resume=True):
        self.config, self.cache, self.page_count = config, cache, page_count
        self.vision, self.text, self.layout, self.tables, self.vl = vision, text, layout, tables, vl
        self.resume = resume
        self.gate = AdaptiveConcurrency(config.vision_concurrency)

    def cached_page(self, page):
        raw = self.cache.load_json("draft", page) if self.resume else None
        try:
            if not isinstance(raw, dict):
                return None
            evidence = PageEvidence.from_dict(raw["evidence"])
            if evidence.page != page:
                return None
            latex = normalize_field_annotation_spacing(raw["latex"])
            validate_page_checkpoint(latex, page, self.page_count)
            if evidence.fields:
                validate_page_latex(latex, evidence.fields, page, self.page_count)
            return PageRecognitionResult(page, latex, evidence, self.cache.key, True)
        except (KeyError, TypeError, ValueError):
            return None

    def prepare_batch(self, pages):
        if self.config.ocr == "none":
            return {page.page: PageEvidence(
                "recognition/v1", page.page,
                RenderMetadata(page.dpi, page.width, page.height, page.rotation),
            ) for page in pages}
        prepared, missing = {}, []
        for page in pages:
            raw = self.cache.load_json("ocr", page.page) if self.resume else None
            try:
                ev = PageEvidence.from_dict(raw)
                if ev.page != page.page or ev.render != RenderMetadata(page.dpi, page.width, page.height, page.rotation):
                    raise ValueError("Cached render does not match")
                prepared[page.page] = ev
            except (AttributeError, KeyError, TypeError, ValueError):
                missing.append(page)
        if missing:
            blocks, degraded = {}, ()
            for attempt in range(2):
                try:
                    blocks = self.text.predict_batch(missing)
                    break
                except Exception:
                    if attempt == 1:
                        degraded = ("paddle_text",)
            for page in missing:
                ev = PageEvidence("recognition/v1", page.page,
                                  RenderMetadata(page.dpi, page.width, page.height, page.rotation),
                                  ocr_blocks=blocks.get(page.page, ()), degraded_adapters=degraded)
                self.cache.save_json("ocr", page.page, ev.to_dict())
                prepared[page.page] = ev
        for page in pages:
            ev = prepared[page.page]
            raw = self.cache.load_json("tables", page.page) if self.resume else None
            try:
                cached = PageEvidence.from_dict(raw)
                if cached.page != page.page or cached.render != ev.render:
                    raise ValueError("Cached table render does not match")
                prepared[page.page] = replace(ev, tables=cached.tables,
                    degraded_adapters=tuple(dict.fromkeys(ev.degraded_adapters + cached.degraded_adapters)))
                continue
            except (AttributeError, KeyError, TypeError, ValueError):
                pass
            degraded = list(ev.degraded_adapters)
            tables = ()
            try:
                regions = self.layout.predict(page)
            except Exception:
                regions = ()
                degraded.append("paddle_layout")
            try:
                tables = self.tables.predict(page, regions)
            except Exception:
                degraded.append("paddle_tables")
            if self.config.enable_vl_fallback and self.vl is not None:
                resolved = []
                for table in tables:
                    if table_needs_vl(table):
                        try:
                            table = self.vl.predict(page, table)
                        except Exception:
                            degraded.append("paddle_vl")
                    resolved.append(table)
                tables = tuple(resolved)
            ev = replace(ev, tables=tables, degraded_adapters=tuple(dict.fromkeys(degraded)))
            self.cache.save_json("tables", page.page, ev.to_dict())
            prepared[page.page] = ev
        return prepared

    def recognize_page(self, page, evidence=None):
        cached = self.cached_page(page.page)
        if cached is not None:
            return cached
        if evidence is None:
            evidence = self.prepare_batch([page])[page.page]
        fallback = None
        degraded = False
        for attempt in range(2):
            try:
                with self.gate.slot():
                    from lexoid.core.model_telemetry import call_context
                    with call_context(stage="recognize", page=page.page, attempt=attempt + 1):
                        result = self.vision.recognize(page, evidence, self.page_count)
                validate_page_checkpoint(result.latex, page.page, self.page_count)
                self.gate.on_success()
                break
            except Exception as exc:
                from lexoid.core.model_telemetry import emit
                emit({"event": "validation_or_request_error", "stage": "recognize",
                      "page": page.page, "attempt": attempt + 1,
                      "error_type": type(exc).__name__})
                if isinstance(exc, RecoverableVisionError) and exc.fallback is not None:
                    fallback = exc.fallback
                status = getattr(exc, "status_code", None)
                invalid_model_output = isinstance(exc, ValueError)
                retryable = (invalid_model_output
                             or status in (429, 408, 500, 502, 503, 504)
                             or "timeout" in type(exc).__name__.lower())
                if not retryable or attempt == 1:
                    result = fallback or VisionPageResult(
                        page.page,
                        fallback_page_latex(page.page, evidence, self.page_count),
                        (),
                    )
                    degraded = True
                    break
                if status == 429:
                    self.gate.on_rate_limit()
                if not invalid_model_output:
                    time.sleep(min(30, 2 ** attempt) + random.random())
        if degraded:
            evidence = replace(evidence, degraded_adapters=tuple(dict.fromkeys(
                evidence.degraded_adapters + ("vision",)
            )))
        evidence = replace(evidence, fields=result.fields)
        self.cache.save_text("draft", page.page, result.latex)
        self.cache.save_json("draft", page.page, {"latex": result.latex, "evidence": evidence.to_dict()})
        return PageRecognitionResult(page.page, result.latex, evidence, self.cache.key)


class DocumentRecognizer:
    def __init__(self, model, config=None, cache_dir=None, api="openai", resume=True,
                 auto_orient=True, renderer=None, page_counter=None, text_adapter=None,
                 layout_adapter=None, table_adapter=None, vision_adapter=None, vl_adapter=None):
        self.config = config or RecognitionConfig()
        self.model, self.api, self.resume, self.auto_orient = model, api, resume, auto_orient
        self.cache_dir = Path(cache_dir or ".lexoid-cache")
        self.renderer, self.page_counter = renderer or render_pdf_page, page_counter or _page_count
        self.text = self.layout = self.tables = self.vl = None
        if self.config.ocr == "paddleocr":
            self.text = text_adapter or PaddleTextAdapter(self.config.device)
            self.layout = layout_adapter or PaddleLayoutAdapter(self.config.device)
            self.tables = table_adapter or PaddleTableAdapter(self.config.device)
            self.vl = vl_adapter or PaddleVlFallbackAdapter(self.config.device)
        self.vision = vision_adapter or VisionLatexAdapter(model, api, self.config)

    def recognize(self, pdf_path, start_page=1, page_callback=None, expected_total_pages=None):
        pdf_path = Path(pdf_path)
        self.page_count = self.page_counter(pdf_path)
        if not 1 <= start_page <= self.page_count:
            raise ValueError("start_page is outside the document")
        if expected_total_pages is not None and expected_total_pages != self.page_count:
            raise ValueError("PDF page count differs from checkpoint")
        self.document_sha256 = document_sha256(pdf_path)
        versions = {}
        versions["adapter_config"] = "layout-only-v2-arm-static-ir"
        for name in ("paddleocr", "paddlex", "paddlepaddle"):
            try:
                versions[name] = version(name)
            except PackageNotFoundError:
                versions[name] = "unavailable"
        key = build_cache_key(self.document_sha256, self.config, versions,
                              f"{PROMPT_VERSION}:orient={self.auto_orient}", self.model)
        cache = RecognitionCache(self.cache_dir, key)
        owner = PageRecognizer(self.config, cache, self.page_count, self.vision, self.text,
                               self.layout, self.tables, self.vl, self.resume)
        results, errors, pending, todo = {}, {}, {}, []
        next_callback = start_page

        def deliver():
            nonlocal next_callback
            while next_callback in results:
                if page_callback:
                    page_callback(next_callback, self.page_count, results[next_callback].latex)
                next_callback += 1

        def collect(done):
            for future in done:
                number = pending.pop(future)
                try:
                    results[number] = future.result()
                except Exception as exc:
                    errors[number] = exc
            deliver()

        for number in range(1, self.page_count + 1):
            cached = owner.cached_page(number)
            if cached:
                results[number] = cached
            elif number < start_page:
                raise IncompleteDocumentError(number, "resume requires a matching cached draft")
            else:
                todo.append(number)
        deliver()
        # PDFium is not thread-safe. Production renders run in separate processes.
        executor_type = ProcessPoolExecutor if self.renderer is render_pdf_page else ThreadPoolExecutor
        with executor_type(max_workers=self.config.render_workers) as renders, \
                ThreadPoolExecutor(max_workers=self.config.vision_concurrency) as visions:
            batch_size = self.config.paddle_batch_size
            for offset in range(0, len(todo), batch_size):
                batch, render_jobs = [], {}
                for number in todo[offset:offset + batch_size]:
                    path = cache.path("render", number, ".png")
                    try:
                        if not self.resume:
                            raise OSError("Cache disabled")
                        with Image.open(path) as image:
                            image.load()
                            image = image.convert("RGB")
                        meta = cache.load_json("render", number)
                        if not isinstance(meta, dict) or (meta.get("width"), meta.get("height")) != image.size:
                            raise ValueError("Missing render metadata")
                        batch.append(RenderedPage(number, self.config.initial_render_dpi,
                                                  *image.size, image, meta.get("rotation", 0)))
                    except (OSError, ValueError):
                        render_jobs[number] = renders.submit(self.renderer, str(pdf_path), number,
                            self.config.initial_render_dpi, self.auto_orient)
                for number, future in render_jobs.items():
                    page = future.result()
                    buffer = io.BytesIO()
                    page.image.save(buffer, format="PNG")
                    _atomic_write(cache.path("render", number, ".png"), buffer.getvalue())
                    cache.save_json("render", number, RenderMetadata(
                        page.dpi, page.width, page.height, page.rotation).to_dict())
                    batch.append(page)
                prepared = owner.prepare_batch(batch)
                for page in batch:
                    pending[visions.submit(owner.recognize_page, page, prepared[page.page])] = page.page
                while len(pending) >= self.config.vision_concurrency * 2:
                    collect(wait(pending, return_when=FIRST_COMPLETED).done)
                collect({future for future in pending if future.done()})
            if pending:
                collect(wait(pending).done)
        for number in range(1, self.page_count + 1):
            if number not in results:
                raise IncompleteDocumentError(number, str(errors.get(number, "missing page")))
        return [results[number] for number in range(1, self.page_count + 1)]
