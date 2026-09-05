# 用 Docker 跑 Lexoid（Intel Mac）

你的机器是 `macosx-10.15-x86_64`（Intel），而 `cryptography ≥ 49`、`torch ≥ 2.3`、
`paddlepaddle 3.2.2` 都已不再发布 Intel Mac 的预编译 wheel，本地 `make install` 无解。
Docker 里跑的是 linux/amd64，这三个包全都有轮子 —— 已验证 183 个依赖 **零源码编译**。

---

## 1. 一次性准备

```bash
cd ~/Desktop/lexiod/Lexoid
mkdir -p work                 # PDF 放这里，产出也在这里
docker compose build          # 首次约 10~20 分钟，镜像约 4~5 GB
```

`.env` 不进镜像（`.dockerignore` 里排除了），而是由 compose 在**运行时**注入，
所以 API key 不会被烤进镜像层。改完 `.env` 无需重新 build。

## 2. PDF → LaTeX

```bash
cp ~/Downloads/paper.pdf work/

docker compose run --rm lexoid latex -i paper.pdf -o paper.tex
```

产出就在宿主机的 `work/paper.tex`。

指定模型（默认 `gpt-4o-mini`，效果一般，建议换）：

```bash
docker compose run --rm lexoid latex -i paper.pdf -o paper.tex \
  --model gemini-2.5-flash --verbose
```

## 3. 其他子命令

```bash
# PDF → Markdown
docker compose run --rm lexoid parse -i paper.pdf -o paper.md

# 结构化抽取
docker compose run --rm lexoid schema -i invoice.pdf -s schema.json -o out.json

# 看帮助
docker compose run --rm lexoid --help
docker compose run --rm lexoid latex --help
```

## 4. 进容器里调试 / 用 Python API

```bash
docker compose run --rm --entrypoint bash lexoid
# 容器内：
python -c "from lexoid.api import parse_to_latex; print(parse_to_latex('paper.pdf', model='gemini-2.5-flash', max_tokens=8192)[:500])"
```

## 5. 改了 lexoid 源码之后

源码是 `COPY` 进镜像并打成 wheel 安装的，不是挂载的，所以改完要重新 build：

```bash
docker compose build
```

如果想边改边跑，改成挂载源码更方便：在 `docker-compose.yml` 的 `volumes` 下加一行
`- ./lexoid:/opt/venv/lib/python3.10/site-packages/lexoid`。

---

## 对原配置做了什么改动

| 文件 | 改动 | 原因 |
|---|---|---|
| `Dockerfile` | 先从 `download.pytorch.org/whl/cpu` 装 torch | 默认 PyPI 的 linux torch 会连带拉 15 个 `nvidia-cu*` CUDA 包（数 GB）。Docker Desktop on Mac 无 GPU，纯浪费 |
| `Dockerfile` | 移除 `qtbase5-dev` / `python3-pyqt5` 等 12 个 apt 包 | `pyproject` 里 pyqt5 的 marker 是 `platform_system != 'Linux'`，Linux 下压根不装；且 apt 装的 `python3-pyqt5` 在独立 venv 中不可见 |
| `Dockerfile` | `make build` → 直接 `poetry build` | `make build` 是 `poetry update && poetry build`，那个 `update` 会无视 lock 重新解析全部依赖，慢且不可复现 |
| `Dockerfile` | 加 `ENTRYPOINT ["lexoid"]`、`WORKDIR /work` | 让它作为 CLI 直接可用 |
| `docker-compose.yml` | 删掉 `ports: 8000` 和 `restart: unless-stopped` | 镜像里没有任何 server，原配置下容器启动即退出，再被无限重启 |
| `docker-compose.yml` | 加 `volumes: ./work:/work` | 原配置没有挂载，PDF 进不去、`.tex` 出不来 |
| `.dockerignore` | 加 `work`、`docs`、`assets`、`stress_test` | 减小构建上下文 |

原文件都在 git 里，`git checkout Dockerfile docker-compose.yml .dockerignore` 可随时还原。

---

## 已知的上游代码问题（Docker 也躲不掉）

这两处在 `lexoid/api.py` 的 `parse_to_latex()` 里，跟环境无关：

1. **`max_tokens` 默认 1024**（`api.py:601`）—— 一整页论文的 LaTeX 远超这个量，会被截断导致
   `\begin{...}` 不闭合、无法编译。CLI 没暴露这个参数，只能走 Python API 传 `max_tokens=8192`。
2. **中间页 / 末页 prompt 疑似写反**（`api.py:588-593`）——
   `elif i == total_pages - 1: system_prompt = middle_prompt` / `else: system_prompt = last_prompt`。
   按命名意图应当相反，否则多页 PDF 的输出大概率无法直接编译（单页不受影响）。

需要的话我可以把这两处一起改掉并补测试。
