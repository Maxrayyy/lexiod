# 注意：这里刻意不写 `# syntax=docker/dockerfile:1`。
# 那一行会让 BuildKit 先去 Docker Hub 拉 docker/dockerfile 前端镜像，
# 在国内网络下多一个必然失败的点，而本文件并未用到任何需要它的新语法。

# 基础镜像可通过 build arg 换成国内镜像源，例如：
#   docker compose build --build-arg BASE_IMAGE=docker.1ms.run/library/python:3.10-slim
ARG BASE_IMAGE=python:3.10-slim
FROM ${BASE_IMAGE}

# pip 源同样可替换，例如清华：
#   --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DEFAULT_TIMEOUT=120 \
    POETRY_VIRTUALENVS_CREATE=false

RUN if [ -n "$PIP_TRUSTED_HOST" ]; then \
        printf '[global]\ntrusted-host = %s\n' "$PIP_TRUSTED_HOST" > /etc/pip.conf; \
    fi

# 仅保留真正需要的系统依赖：
# - build-essential/git/curl: 少数包的构建与下载
# - libgl1 / libglib2.0-0: opencv-python 运行时
# Playwright 的浏览器系统库交给 `playwright install --with-deps` 自己装。
# 原 Dockerfile 里的 qtbase5-dev / python3-pyqt5 系列已移除：
# pyproject 中 pyqt5 的 marker 是 platform_system != 'Linux'，Linux 下根本不装；
# 且 apt 装的 python3-pyqt5 在独立 venv 中不可见，属于纯粹的体积浪费。
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        curl \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" VIRTUAL_ENV=/opt/venv

RUN pip install --upgrade pip setuptools wheel

# 关键优化：先从 PyTorch 官方 CPU 源装 torch。
# 默认 PyPI 的 linux torch 会连带拉 15 个 nvidia-cu* CUDA 包（数 GB），
# 而 Docker Desktop on Mac 没有 GPU，这些完全用不上。
# 先装好 CPU 版，后续 resolver 看到 torch 已满足就不会再拉 CUDA 轮子。
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN pip install --index-url ${TORCH_INDEX_URL} torch

WORKDIR /app

# 依赖层只由锁文件决定。以后修改 Python 源码或提示词时，这个耗时层可以直接复用。
#
# poetry.lock 锁定的 PyPI Linux torch 会强制安装 Triton 和整套 CUDA
# 依赖，并把上面已安装的 +cpu 轮子替换掉。因此从锁文件导出
# requirements，只过滤 GPU 依赖，其余版本仍严格使用 poetry.lock。
COPY pyproject.toml poetry.lock /app/
# Poetry 读取 pyproject 时会校验 readme 路径；先放空占位文件，
# 真实 README 在代码层再复制，避免只改文档就重建依赖层。
RUN touch /app/README.md \
    && pip install poetry poetry-plugin-export \
    && poetry export --only main --without-hashes --format requirements.txt \
        --output /tmp/requirements.txt \
    && sed -i '/^\(cuda-bindings\|cuda-toolkit\|nvidia-[a-z0-9-]*\|torch\|triton\)==/d' \
        /tmp/requirements.txt \
    && pip install --requirement /tmp/requirements.txt \
    && pip check

# Chromium 同样放在源码 COPY 之前，避免每次代码变化都重新下载浏览器。
RUN playwright install --with-deps --only-shell chromium

# 最后只复制构建 Lexoid wheel 所需的文件。测试、文档、输出物等
# 无关变化不会再使代码层失效，更不会触发依赖重装。
COPY README.md /app/README.md
COPY lexoid /app/lexoid

# 不走 `make build`：它会执行 poetry update，无视 lock 重新解析依赖。
RUN poetry build \
    && pip install --no-deps dist/*.whl \
    && pip uninstall -y poetry

# 工作目录挂载点：宿主机的 ./work 映射到这里
WORKDIR /work

ENTRYPOINT ["lexoid"]
CMD ["--help"]
