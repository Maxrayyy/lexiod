"""Download the current hybrid runtime's weights without loading inference models."""

import argparse
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import time


def configured_models():
    from lexoid.core.model_config import resolve_model

    raw = os.getenv("PADDLE_PREFETCH_MODELS", "")
    try:
        models = json.loads(raw)
    except ValueError as exc:
        raise ValueError("Set PADDLE_PREFETCH_MODELS to a JSON array in .env/environment") from exc
    if (not isinstance(models, list) or not models
            or any(not isinstance(name, str) or not name.strip() for name in models)):
        raise ValueError("PADDLE_PREFETCH_MODELS must be a nonempty JSON array of model names")
    return tuple(dict.fromkeys([*models, resolve_model("PADDLE_LAYOUT_MODEL")]))


def inventory(directory):
    if not directory.is_dir():
        raise FileNotFoundError(f"Missing model directory: {directory}")
    files = {}
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        files[str(path.relative_to(directory))] = {
            "bytes": path.stat().st_size, "sha256": digest.hexdigest(),
        }
    if not any(name.endswith((".pdiparams", ".safetensors"))
               and entry["bytes"] > 0 for name, entry in files.items()):
        raise ValueError(f"No nonempty model weights in {directory}")
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path,
                        default=Path("/work/model-cache-manifest.json"))
    parser.add_argument("--verify-only", action="store_true",
                        help="Check saved hashes without downloading or loading models")
    args = parser.parse_args()
    models = configured_models()
    started = time.monotonic()
    packages = {name: version(name) for name in ("paddleocr", "paddlex", "paddlepaddle")}
    if args.verify_only:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        if manifest["packages"] != packages or set(manifest["models"]) != set(models):
            raise ValueError("Runtime or model list changed; prepare a new cache manifest")
        for name, entry in manifest["models"].items():
            if inventory(Path(entry["directory"])) != entry["files"]:
                raise ValueError(f"Model cache checksum mismatch: {name}")
            print(f"Verified {name}", flush=True)
    else:
        from paddlex.inference.utils.official_models import official_models

        manifest = {"packages": packages, "models": {}}
        for name in models:
            formats = ("safetensors",) if name.startswith("PaddleOCR-VL") else ("paddle",)
            directory = Path(official_models.get_model_path(name, model_formats=formats))
            files = inventory(directory)
            manifest["models"][name] = {"directory": str(directory), "files": files}
            print(f"Cached {name}: {sum(f['bytes'] for f in files.values()) / 1024**2:.1f} MiB",
                  flush=True)
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    total = sum(f["bytes"] for m in manifest["models"].values() for f in m["files"].values())
    print(json.dumps({"models": len(models), "total_gib": round(total / 1024**3, 3),
                      "elapsed_seconds": round(time.monotonic() - started, 2),
                      "verified": args.verify_only}), flush=True)


if __name__ == "__main__":
    main()
