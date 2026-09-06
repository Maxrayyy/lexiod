"""Platform options shared by Paddle document and recognition models."""

import platform


def paddle_runtime_options(device="cpu"):
    options = {"device": device}
    if (device == "cpu" and platform.system() == "Linux"
            and platform.machine().lower() in {"arm64", "aarch64"}):
        # Paddle 3.2.2's PIR CPU predictor segfaults on the M5 Docker host.
        # Scope the workaround to static models so VL can use its dynamic engine.
        options["engine_config"] = {"paddle_static": {
            "run_mode": "paddle", "cpu_threads": 2, "enable_new_ir": False,
        }}
    return options
