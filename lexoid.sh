#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────
# lexoid.sh — Lexoid Docker CLI wrapper
#
# 用法:
#   ./lexoid.sh build                                    # 构建/重建镜像
#   ./lexoid.sh parse  -i input.pdf -o output.md         # 解析为 Markdown
#   ./lexoid.sh latex  -i input.pdf -o output.tex [...]  # 转换为 LaTeX
#   ./lexoid.sh schema -i input.pdf -o <output.json> [...]# 结构化提取
#
# 特点:
#   - 首次运行自动构建镜像
#   - 本机文件路径自动挂载到容器，直接传绝对路径即可
#   - 所有 lexoid CLI 参数原样透传
#   - 兼容 bash 3.2+（macOS 原生可用）
# ──────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
IMAGE_NAME="lexoid:local"

# ── 自动加载 .env ──
# 按优先级查找: 脚本同目录 → 上层目录 → 用户指定
for env_dir in "$SCRIPT_DIR" "$SCRIPT_DIR/.." "$SCRIPT_DIR/../lexiod-pipeline"; do
    if [[ -f "${env_dir}/.env" ]]; then
        set -a
        source "${env_dir}/.env"
        set +a
        break
    fi
done

# ── 颜色 ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { printf "${GREEN}[lexoid]${NC} %s\n" "$*" >&2; }
warn() { printf "${YELLOW}[lexoid]${NC} %s\n" "$*" >&2; }

# ── 构建镜像 ──
build_image() {
    log "构建镜像 ${IMAGE_NAME} ..."
    cd "$SCRIPT_DIR"
    docker compose build
    log "镜像构建完成 ✅"
}

ensure_image() {
    if ! docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
        warn "镜像 ${IMAGE_NAME} 不存在，自动构建..."
        build_image
    fi
}

# ── 特殊命令 ──
case "${1:-}" in
    build)  build_image; exit 0 ;;
    --help|-h|"")
        echo "Lexoid Docker CLI Wrapper"
        echo ""
        echo "用法:"
        echo "  ./lexoid.sh build                                 # 构建/重建镜像"
        echo "  ./lexoid.sh parse  -i <input> -o <output> [...]   # 解析文档"
        echo "  ./lexoid.sh latex  -i <input> -o <output> [...]   # 转换 LaTeX"
        echo "  ./lexoid.sh schema -i <input> -o <output> [...]   # 结构化提取"
        echo ""
        echo "文件路径用本机绝对路径，脚本自动挂载。"
        echo "示例:"
        echo "  ./lexoid.sh latex -i ~/Downloads/input.pdf -o ~/Downloads/output.tex --model gpt-5.6-luna"
        exit 0
        ;;
esac

ensure_image

# ── 路径解析 ──
resolve_path() {
    local p="$1"
    # 展开 ~
    case "$p" in ~*) p="${HOME}${p#\~}" ;; esac
    # 相对路径 → 绝对路径
    [[ "$p" != /* ]] && p="$(pwd)/$p"
    # 规范化
    if [[ -e "$p" ]]; then
        echo "$(cd "$(dirname "$p")" && pwd)/$(basename "$p")"
    elif [[ -d "$(dirname "$p")" ]]; then
        echo "$(cd "$(dirname "$p")" && pwd)/$(basename "$p")"
    else
        echo "$p"
    fi
}

# ── 收集挂载目录 & 重写参数 ──
# 用两个平行数组代替关联数组（兼容 bash 3）
HOST_DIRS=()       # 本机目录列表
CONTAINER_DIRS=()  # 对应的容器内目录
MOUNT_FLAGS=()     # -v 参数列表
CONTAINER_ARGS=()  # 重写后的 CLI 参数
DIR_INDEX=0

# 查找已注册的目录，返回索引；未找到返回 -1
find_dir() {
    local target="$1"
    local i
    if [[ $DIR_INDEX -eq 0 ]]; then
        echo "-1"
        return
    fi
    for i in $(seq 0 $((DIR_INDEX - 1))); do
        [[ "${HOST_DIRS[$i]}" == "$target" ]] && echo "$i" && return
    done
    echo "-1"
}

# 注册一个目录，设置全局 LAST_CONTAINER_PATH
LAST_CONTAINER_PATH=""
register_dir() {
    local abs="$1"
    local parent="$(dirname "$abs")"
    local filename="$(basename "$abs")"
    local idx
    idx=$(find_dir "$parent")
    if [[ "$idx" == "-1" ]]; then
        idx=$DIR_INDEX
        HOST_DIRS+=("$parent")
        CONTAINER_DIRS+=("/data/${idx}")
        MOUNT_FLAGS+=("-v" "${parent}:/data/${idx}")
        DIR_INDEX=$((DIR_INDEX + 1))
    fi
    LAST_CONTAINER_PATH="/data/${idx}/${filename}"
}

# 需要路径重写的参数（-i 和 -o 的值）
PATH_FLAGS="-i --input -o --output"

args=("$@")
nargs=${#args[@]}
i=0

# 第一个参数是子命令（latex/parse/schema），直接透传
if [[ $nargs -gt 0 ]] && [[ "${args[0]}" != -* ]]; then
    CONTAINER_ARGS+=("${args[0]}")
    i=1
fi

while [[ $i -lt $nargs ]]; do
    arg="${args[$i]}"
    case "$arg" in
        -i|--input|-o|--output)
            # 这是路径 flag，透传 flag 本身，下一个参数做路径重写
            CONTAINER_ARGS+=("$arg")
            i=$((i + 1))
            if [[ $i -lt $nargs ]]; then
                val="${args[$i]}"
                abs="$(resolve_path "$val")"
                register_dir "$abs"
                CONTAINER_ARGS+=("$LAST_CONTAINER_PATH")
            fi
            ;;
        --*=*)
            # --key=value 形式，检查 value 是否是路径
            CONTAINER_ARGS+=("$arg")
            ;;
        *)
            # 其他参数直接透传（--model, gpt-5.6-luna, --start-page, etc.）
            CONTAINER_ARGS+=("$arg")
            ;;
    esac
    i=$((i + 1))
done

# ── 执行 ──
log "执行: lexoid ${CONTAINER_ARGS[*]}"

# 收集需要传递的环境变量
ENV_FLAGS=()
for var in OPENAI_API_KEY GOOGLE_API_KEY ANTHROPIC_API_KEY MISTRAL_API_KEY \
    TOGETHER_API_KEY HUGGINGFACEHUB_API_TOKEN OPENROUTER_API_KEY \
    FIREWORKS_API_KEY DEEPSEEK_API_KEY DEEPSEEK_BASE_URL MINIMAX_API_KEY \
    MINIMAX_BASE_URL DEFAULT_LLM LEXOID_MODEL TEXOPT_MODEL; do
    if [[ -n "${!var:-}" ]]; then
        ENV_FLAGS+=("-e" "${var}=${!var}")
    fi
done

docker run --rm \
    ${MOUNT_FLAGS[@]+"${MOUNT_FLAGS[@]}"} \
    ${ENV_FLAGS[@]+"${ENV_FLAGS[@]}"} \
    "$IMAGE_NAME" \
    "${CONTAINER_ARGS[@]}"
