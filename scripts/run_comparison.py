"""Run isolated, serial comparisons against the same three physical PDF pages."""

import argparse
from dataclasses import replace
import json
from pathlib import Path
import resource
import subprocess
import sys
import threading
import time
import traceback

VARIANTS = ("vision", "hybrid-full", "hybrid-1920", "hybrid-1280", "hybrid-1280-no-vl")


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class Measurement:
    def __init__(self, directory, batch):
        self.path = directory / f"timing-{batch}.json"
        self.started = time.monotonic()
        self.data = {"stages": [], "status": "running", "cgroup_peak_mib": 0}
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.sample, daemon=True)
        self.thread.start()

    def sample(self):
        while not self.stop.wait(1):
            try:
                current = int(Path("/sys/fs/cgroup/memory.current").read_text()) / 1024**2
                self.data["cgroup_peak_mib"] = max(self.data["cgroup_peak_mib"], round(current, 2))
            except OSError:
                pass

    def run(self, stage, operation):
        start = time.monotonic()
        print(f"START {stage}", flush=True)
        record = {"stage": stage}
        try:
            result = operation()
            record["status"] = "ok"
            return result
        except Exception as exc:
            record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            record.update(seconds=round(time.monotonic() - start, 3),
                          peak_rss_mib=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2))
            self.data["stages"].append(record)
            self.flush()
            print(json.dumps(record, ensure_ascii=False), flush=True)

    def flush(self):
        self.data["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        save(self.path, self.data)

    def finish(self, status):
        self.stop.set()
        self.thread.join()
        self.data["status"] = status
        self.flush()


def recognize(args):
    from lexoid.core.model_config import resolve_model
    from lexoid.core.paddle_runtime import paddle_runtime_options
    from lexoid.core.recognition.models import PageEvidence, RecognitionConfig, RenderMetadata
    from lexoid.core.recognition.paddle import (
        PaddleTextAdapter, PaddleLayoutAdapter, PaddleTableAdapter,
        PaddleVlFallbackAdapter, table_needs_vl,
    )
    from lexoid.core.recognition.rendering import render_pdf_page
    from lexoid.core.recognition.service import document_sha256
    from lexoid.core.recognition.vision import VisionLatexAdapter

    vision_model = resolve_model("LEXOID_MODEL")
    directory = args.root / args.variant
    meter = Measurement(directory, "-".join(map(str, args.pages)))
    config = RecognitionConfig(min_output_tokens=8192, max_output_tokens=8192,
                               paddle_batch_size=1, vision_concurrency=1)
    vision = VisionLatexAdapter(vision_model, config=config)
    text, layout, tables, vl = PaddleTextAdapter(), PaddleLayoutAdapter(), PaddleTableAdapter(), PaddleVlFallbackAdapter()
    if args.variant.startswith(("hybrid-1920", "hybrid-1280")):
        text.options = {**text.options, "text_det_limit_side_len": int(args.variant.split("-")[1]),
                        "text_det_limit_type": "max"}
    try:
        for number in args.pages:
            page = meter.run(f"p{number}.render_orientation", lambda: render_pdf_page(str(args.source), number, 240))
            evidence = PageEvidence("recognition/v1", number,
                                    RenderMetadata(page.dpi, page.width, page.height, page.rotation))
            if args.variant != "vision":
                if text._pipeline is None:
                    meter.run(f"p{number}.text_init", lambda: text.pipeline)
                blocks = meter.run(f"p{number}.text_infer", lambda: text.predict_batch([page])[number])
                if layout._pipeline is None:
                    meter.run(f"p{number}.layout_init", lambda: layout.pipeline)
                regions = meter.run(f"p{number}.layout_infer", lambda: layout.predict(page))
                if any(r.kind == "table" for r in regions):
                    if tables._pipeline is None:
                        meter.run(f"p{number}.table_init", lambda: tables.pipeline)
                    found = meter.run(f"p{number}.table_infer", lambda: tables.predict(page, regions))
                else:
                    found = ()
                resolved, degraded = [], []
                for index, table in enumerate(found):
                    if table_needs_vl(table) and not args.variant.endswith("-no-vl"):
                        try:
                            if vl._pipeline is None:
                                meter.run(f"p{number}.vl_init", lambda: vl.pipeline)
                            table = meter.run(f"p{number}.vl_infer_{index}", lambda: vl.predict(page, table))
                        except Exception:
                            degraded.append("paddle_vl")
                    resolved.append(table)
                evidence = replace(evidence, ocr_blocks=blocks, tables=tuple(resolved),
                                   degraded_adapters=tuple(degraded))
            save(directory / "tex" / f"page-{number}.ocr.json", evidence.to_dict())
            # Capture raw API replies, including rejected attempts, for audit.
            from lexoid.core.parse_type.llm_parser import create_response
            attempt = 0
            def response(**kwargs):
                nonlocal attempt
                attempt += 1
                result = create_response(**kwargs)
                save(directory / "responses" / f"page-{number}-attempt-{attempt}.json", result)
                return result
            vision._respond = response
            for retry in range(3):
                try:
                    result = meter.run(f"p{number}.vision_attempt_{retry + 1}",
                                       lambda: vision.recognize(page, evidence, 3))
                    break
                except Exception:
                    if retry == 2:
                        raise
                    time.sleep(2)
            evidence = replace(evidence, fields=result.fields)
            save(directory / "tex" / f"page-{number}.recognition.json", evidence.to_dict())
            (directory / "tex" / f"page-{number}.tex").write_text(result.latex, encoding="utf-8")
        paths = [directory / "tex" / f"page-{n}.tex" for n in (1, 2, 3)]
        if all(p.exists() for p in paths):
            (directory / "tex" / "sample.tex").write_text("\n".join(p.read_text() for p in paths), encoding="utf-8")
            save(directory / "tex" / "sample.recognition.json", {
                "schema": "recognition/v1", "document_sha256": document_sha256(args.source),
                "model": vision_model, "variant": args.variant,
                "pages": [json.loads(p.with_suffix(".recognition.json").read_text()) for p in paths],
            })
        meter.finish("ok")
    except BaseException:
        meter.finish("failed")
        raise


def postprocess(args):
    from lexoid.core.model_config import resolve_model
    directory = args.root / args.variant
    raw, optimized = directory / "tex", directory / "optimized"
    optimized.mkdir(parents=True, exist_ok=True)
    from texopt.reconcile import reconcile_document
    meter = Measurement(directory, "postprocess")
    try:
        report = meter.run("reconcile", lambda: reconcile_document(
            raw / "sample.tex", args.source, raw / "sample.recognition.json",
            raw / "sample.reconciled.tex", raw / "sample.fields.json", concurrency=2))
        save(directory / "reconcile-summary.json", {"selected": report.selected,
            "confirmed": report.confirmed, "failed": report.failed, "errors": report.errors})
        command = ["texopt", "optimise", str(raw / "sample.reconciled.tex"),
            "-o", str(optimized / "sample.optimized.tex"),
            "--source-registry", str(raw / "sample.fields.json"),
            "--registry", str(optimized / "sample.fields.json"),
            "--report", str(optimized / "report.json"),
            "--log-file", str(optimized / "optimise.log"),
            "--llm-model", resolve_model("TEXOPT_MODEL"),
            "--repair-model", resolve_model("TEXOPT_REPAIR_MODEL"),
            "--llm-repair-on-failure", "--repair-on-failure-attempts", "2",
            "--name-cache", str(directory / "name-cache.json"),
            "--syntax-repair-cache", str(directory / "repair-cache.json"),
            "--probe-dir", str(optimized / "probe"),
            "--compile-check", "--compile-log", str(optimized / "compile.log")]
        meter.run("optimize_and_compile", lambda: subprocess.run(command, check=True, timeout=1800))
        meter.finish("ok")
    except BaseException:
        meter.finish("failed")
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/data/benchmarks/2026-09-05"))
    parser.add_argument("--source", type=Path, default=Path("/data/benchmarks/2026-09-05/reference/sample.pdf"))
    parser.add_argument("--variant", choices=VARIANTS)
    parser.add_argument("--variants", choices=VARIANTS, nargs="+", default=VARIANTS)
    parser.add_argument("--pages", nargs="+", type=int, default=[1, 2, 3])
    parser.add_argument("--postprocess", action="store_true")
    parser.add_argument("--continue-after-pilot", action="store_true")
    args = parser.parse_args()
    from lexoid.core.model_config import resolve_model
    if args.variant:
        return postprocess(args) if args.postprocess else recognize(args)
    runs = (json.loads((args.root / "runs.json").read_text())
            if args.continue_after_pilot else [])
    save(args.root / "protocol.json", {"variants": VARIANTS, "source": str(args.source),
        "physical_source_pages": [1, 21, 50], "render_dpi": 240, "retry_dpi": 480,
        "vision_model": resolve_model("LEXOID_MODEL"),
        "optimizer_model": resolve_model("TEXOPT_MODEL"),
        "reconcile_model": resolve_model("RECONCILE_MODEL"),
        "repair_model": resolve_model("TEXOPT_REPAIR_MODEL"), "output_tokens": 8192,
        "schedule": "Serial variants; page 1 pilot, then pages 2 and 3; fresh process per batch",
        "paddle_batch_size": 1, "vision_concurrency": 1, "reconcile_concurrency": 2,
        "weight_cache": "preloaded", "recognition_cache": "disabled", "max_vision_attempts": 3,
        "notes": "Single-run latency comparison, not tuned production throughput. No human acceptance.",
        "prompt_fix": "Pilot used v1; subsequent pages use v2 to correct standalone handwriting wrapper instructions for every variant."})
    for pages in (([2, 3],) if args.continue_after_pilot else ([1], [2, 3])):
        for variant in args.variants:
            directory = args.root / variant
            directory.mkdir(parents=True, exist_ok=True)
            command = [sys.executable, __file__, "--root", str(args.root), "--source", str(args.source),
                       "--variant", variant, "--pages", *map(str, pages)]
            started = time.monotonic()
            with (directory / f"recognize-{'-'.join(map(str, pages))}.log").open("w") as log:
                try:
                    code = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=2400).returncode
                except subprocess.TimeoutExpired:
                    code = 124
            runs.append({"variant": variant, "pages": pages, "returncode": code,
                         "seconds": round(time.monotonic() - started, 2)})
            save(args.root / "runs.json", runs)
            print(json.dumps(runs[-1]), flush=True)
    for variant in args.variants:
        directory = args.root / variant
        if not (directory / "tex" / "sample.tex").exists():
            continue
        started = time.monotonic()
        with (directory / "postprocess.log").open("w") as log:
            try:
                code = subprocess.run([sys.executable, __file__, "--root", str(args.root),
                    "--source", str(args.source), "--variant", variant, "--postprocess"],
                    stdout=log, stderr=subprocess.STDOUT, timeout=2400).returncode
            except subprocess.TimeoutExpired:
                code = 124
        runs.append({"variant": variant, "stage": "postprocess", "returncode": code,
                     "seconds": round(time.monotonic() - started, 2)})
        save(args.root / "runs.json", runs)
        print(json.dumps(runs[-1]), flush=True)


if __name__ == "__main__":
    main()
