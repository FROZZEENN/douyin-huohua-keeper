#!/bin/sh
# =============================================================================
# douyin-huohua-keeper 容器入口
# 职责：等数据目录就绪 -> 做一次轻量自检 -> 交给主进程
# 保持足够简单，任何一步失败都应该显式退出而不是静默继续
# =============================================================================
set -eu

DATA_DIR="${HUOHUA_DATA_DIR:-/app/data}"
LOG_DIR="${HUOHUA_LOG_DIR:-/app/logs}"

log() {
    printf '[entrypoint] %s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

log "starting douyin-huohua-keeper"

# --- 数据目录 ---------------------------------------------------------------
# 挂载卷的时候宿主机目录可能是 root 所有，这里兜一层
for d in "$DATA_DIR" "$DATA_DIR/accounts" "$DATA_DIR/config" "$DATA_DIR/runs" "$LOG_DIR"; do
    if [ ! -d "$d" ]; then
        log "creating $d"
        mkdir -p "$d" || { log "FATAL: cannot create $d"; exit 1; }
    fi
    if [ ! -w "$d" ]; then
        log "FATAL: $d is not writable by uid $(id -u)"
        log "hint: chown -R 10001:10001 <host-dir>"
        exit 1
    fi
done

# --- Chromium ---------------------------------------------------------------
BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright}"
if [ ! -d "$BROWSERS_PATH" ]; then
    log "WARNING: PLAYWRIGHT_BROWSERS_PATH=$BROWSERS_PATH does not exist"
else
    log "chromium path: $BROWSERS_PATH"
fi

# --- 时区 -------------------------------------------------------------------
if [ -n "${TZ:-}" ] && [ -f "/usr/share/zoneinfo/${TZ}" ]; then
    log "timezone: $TZ ($(date '+%Y-%m-%d %H:%M:%S %Z'))"
else
    log "WARNING: TZ='${TZ:-}' not found, falling back to UTC ($(date -u '+%H:%M:%S'))"
    log "         sparks are counted per natural day — a wrong TZ will shift your send window"
fi

# --- 提醒容易被忽略的配置 ---------------------------------------------------
if [ -z "${HUOHUA_TOKEN:-}" ]; then
    log "WARNING: HUOHUA_TOKEN is empty — the workbench will be reachable without auth"
    log "         set it in .env, especially if the port is exposed"
fi

if [ "${HUOHUA_HEADLESS:-true}" != "true" ]; then
    log "WARNING: HUOHUA_HEADLESS=${HUOHUA_HEADLESS} but no display exists in this container"
    log "         the browser will fail to start; set HUOHUA_HEADLESS=true"
fi

log "handing over to: $*"
exec "$@"
