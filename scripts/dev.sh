#!/usr/bin/env bash
# =============================================================================
# 开发环境辅助脚本
#
#   ./scripts/dev.sh setup     建虚拟环境、装依赖、下载 Chromium
#   ./scripts/dev.sh run       本地启动（会弹出真实浏览器窗口）
#   ./scripts/dev.sh run-head  本地启动（无头，贴近生产）
#
# 上面两个 run 默认**不启用定时任务**（只起工作台）。同一个抖音账号不该
# 有两个实例各跑各的定时 —— 既是风控风险，也会互相顶号。确实要本地定时：
#
#   ./scripts/dev.sh run --with-scheduler
#
#   ./scripts/dev.sh test      跑测试
#   ./scripts/dev.sh cov       跑测试 + 覆盖率报告
#   ./scripts/dev.sh lint      静态检查
#   ./scripts/dev.sh fmt       格式化
#   ./scripts/dev.sh check     自检：环境、依赖、存储、通知通道
#   ./scripts/dev.sh clean     清掉缓存和临时产物
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

VENV_DIR="${VENV_DIR:-.venv}"
PY="${VENV_DIR}/bin/python"
if [ ! -x "${PY}" ]; then
    # Windows / Git Bash 下的路径
    PY="${VENV_DIR}/Scripts/python.exe"
fi

log() { printf '\033[36m[dev]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[dev] %s\033[0m\n' "$*" >&2; exit 1; }

ensure_venv() {
    if [ ! -x "${PY}" ]; then
        die "虚拟环境不存在，先跑：./scripts/dev.sh setup"
    fi
}

cmd_setup() {
    # 先查 Python 版本 —— 不然会一路装到底、最后才报一句看不懂的错
    local pybin="${PYTHON:-python3}"
    command -v "${pybin}" >/dev/null 2>&1 || pybin=python
    command -v "${pybin}" >/dev/null 2>&1 || die "找不到 python（本项目需要 3.11+）"
    "${pybin}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
        || die "Python 版本过低（需要 3.11+），当前：$("${pybin}" -V 2>&1)"

    log "创建虚拟环境 ${VENV_DIR}"
    "${pybin}" -m venv "${VENV_DIR}"
    ensure_venv

    log "升级 pip"
    "${PY}" -m pip install --upgrade pip setuptools wheel

    log "安装运行依赖"
    "${PY}" -m pip install -r requirements.txt

    log "安装开发依赖"
    "${PY}" -m pip install -r requirements-dev.txt

    log "以可编辑模式安装本项目"
    "${PY}" -m pip install -e .

    # ⚠️ 两个都得装：有头模式用 chromium，**无头模式用的是
    # chromium-headless-shell**。只装 chromium 的话，无头跑起来会报
    # "Executable doesn't exist" —— 而生产/服务器正是无头模式。
    log "下载 Chromium + headless shell（约 200MB，只做一次）"
    "${PY}" -m playwright install chromium chromium-headless-shell

    if [ ! -f .env ]; then
        log "从模板生成 .env"
        cp .env.example .env
        log "本机自用可以不填 HUOHUA_TOKEN（那是给公网访问用的门禁）"
    fi

    log "完成。下一步：./scripts/dev.sh run"
}

# 本地启动的公共默认值。
#
# ⚠️ 默认**不启用定时任务**：默认值必须安全 —— 谁都不想「只是想本机看一眼」，
#    结果它到点真的给 15 个好友发了消息，而服务器那边也在发（同一账号两个实例）。
apply_local_defaults() {
    export HUOHUA_HOST="${HUOHUA_HOST:-127.0.0.1}"
    if [ "${1:-}" = "--with-scheduler" ]; then
        log "⚠️  已显式启用定时任务 —— 它会真的按时发送消息"
    else
        export HUOHUA_ENABLE_SCHEDULER=false
        log "定时任务已关闭（只起工作台，不会自动发送）"
        log "确实要本机定时：./scripts/dev.sh ${2:-run} --with-scheduler"
    fi
}

cmd_run() {
    ensure_venv
    log "本地启动（有头模式，方便观察浏览器动作）"
    log "工作台地址 http://127.0.0.1:${HUOHUA_PORT:-8787}"
    export HUOHUA_HEADLESS="${HUOHUA_HEADLESS:-false}"
    apply_local_defaults "${1:-}" "run"
    "${PY}" -m douyin_huohua_keeper
}

cmd_run_headless() {
    ensure_venv
    log "本地启动（无头模式，贴近生产行为）"
    export HUOHUA_HEADLESS=true
    apply_local_defaults "${1:-}" "run-head"
    "${PY}" -m douyin_huohua_keeper
}

cmd_test() {
    ensure_venv
    log "跑测试"
    "${PY}" -m pytest "$@"
}

cmd_cov() {
    ensure_venv
    log "跑测试 + 覆盖率"
    "${PY}" -m pytest --cov --cov-report=term-missing --cov-report=html
    log "HTML 报告：htmlcov/index.html"
}

cmd_lint() {
    ensure_venv
    log "ruff check"
    "${PY}" -m ruff check src tests scripts
    log "ruff format --check"
    "${PY}" -m ruff format --check src tests scripts
}

cmd_fmt() {
    ensure_venv
    log "ruff format"
    "${PY}" -m ruff format src tests scripts
    log "ruff check --fix"
    "${PY}" -m ruff check --fix src tests scripts
}

cmd_check() {
    ensure_venv
    log "运行自检"
    "${PY}" scripts/healthcheck.py
}

cmd_clean() {
    log "清理缓存与临时产物"
    find . -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
    find . -type d -name '.pytest_cache' -prune -exec rm -rf {} + 2>/dev/null || true
    find . -type d -name '.ruff_cache' -prune -exec rm -rf {} + 2>/dev/null || true
    find . -type d -name 'htmlcov' -prune -exec rm -rf {} + 2>/dev/null || true
    find . -type f -name '.coverage*' -delete 2>/dev/null || true
    find . -type f -name '*.pyc' -delete 2>/dev/null || true
    rm -rf dist build *.egg-info src/*.egg-info 2>/dev/null || true
    log "注意：没有动 data/ —— 那里面是你的登录态和配置，要删请手动来"
}

case "${1:-}" in
    setup)       cmd_setup ;;
    run)         shift; cmd_run "$@" ;;
    run-head)    shift; cmd_run_headless "$@" ;;
    test)        shift; cmd_test "$@" ;;
    cov)         shift; cmd_cov "$@" ;;
    lint)        cmd_lint ;;
    fmt)         cmd_fmt ;;
    check)       cmd_check ;;
    clean)       cmd_clean ;;
    *)
        sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
        exit 1
        ;;
esac
