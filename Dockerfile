# syntax=docker/dockerfile:1

# =============================================================================
# douyin-huohua-keeper
# 多阶段构建：builder 装依赖，runtime 只保留运行所需内容
# =============================================================================

ARG PYTHON_VERSION=3.12


# -----------------------------------------------------------------------------
# Stage 1: builder —— 收集 Python 依赖到独立目录
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

# pip 源。默认走阿里云镜像（国内快，境外也能访问）。
# 想用官方源：docker compose build --build-arg PIP_INDEX_URL=https://pypi.org/simple/
#
# ⚠️ 别写成 `ARG PIP_INDEX_URL=默认值` —— compose 传空字符串时会**覆盖**掉那个默认值，
#    结果 pip 静默退回官方源（国内就慢了）。必须用 `:-` 兜住。
ARG PIP_INDEX_URL
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/} \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# 只复制依赖清单，让这一层可以被缓存
COPY requirements.txt ./

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install -r requirements.txt


# -----------------------------------------------------------------------------
# Stage 2: runtime
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime


LABEL org.opencontainers.image.title="douyin-huohua-keeper" \
      org.opencontainers.image.description="Keep your Douyin streak alive, automatically and observably." \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/<your-name>/douyin-huohua-keeper"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:${PATH}" \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    # 容器内没有显示器，必须无头
    HUOHUA_HEADLESS=true \
    HUOHUA_DATA_DIR=/app/data \
    HUOHUA_HOST=0.0.0.0 \
    HUOHUA_PORT=8787 \
    TZ=Asia/Shanghai

# ---------------------------------------------------------------------------
# Chromium 运行依赖
# 只装真正需要的库，不用 --with-deps（它会拖进一大堆 X11 全家桶）
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 把 Debian apt 源换成国内镜像（bookworm 用 deb822 的 debian.sources）。
# 不换的话 deb.debian.org 在国内能慢到十几分钟（实测 477s+，2026-09-17）。
# ---------------------------------------------------------------------------
# apt 源。默认换成阿里云镜像 —— 国内不换的话 deb.debian.org 慢到十几分钟（实测 477s+）。
#
# ⚠️ 但**境外构建（比如 GitHub Actions 的 runner）不该走阿里云**：
#    从美国数据中心拉阿里云的包同样会慢/失败。境外请指定 APT_MIRROR=official
#    跳过替换（本仓库的 CI 就是这么做的）。
ARG APT_MIRROR=mirrors.aliyun.com
RUN set -eux; \
    if [ -n "$APT_MIRROR" ] && [ "$APT_MIRROR" != "official" ]; then \
        sed -i "s|deb.debian.org|$APT_MIRROR|g" \
            /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true; \
    fi; \
    apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-noto-color-emoji \
        fonts-wqy-zenhei \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libatspi2.0-0 \
        libcairo2 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libgbm1 \
        libglib2.0-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libx11-6 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        tini \
        tzdata \
    && rm -rf /var/lib/apt/lists/*


# Python 虚拟环境（从 builder 拿）
COPY --from=builder /opt/venv /opt/venv

# Playwright 的浏览器包（chromium + headless shell，约 190MB）。
#
# ⚠️ 默认从 cdn.playwright.dev 下载 —— **国内实测可能慢到 1.5 小时以上**（甚至超时失败）。
# 国内构建请指定镜像源（本地安装同理，见 README「开始之前」）：
#
#   docker compose build --build-arg \
#     PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
#
# 也可以写进 .env（compose 会自动透传）：
#   PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
ARG PLAYWRIGHT_DOWNLOAD_HOST=
ENV PLAYWRIGHT_DOWNLOAD_HOST=${PLAYWRIGHT_DOWNLOAD_HOST}
RUN playwright install chromium chromium-headless-shell

WORKDIR /app

# 应用不 pip 安装，以 /app/src 为导入根；缺这行会报 No module named douyin_huohua_keeper
ENV PYTHONPATH=/app/src

# 先复制元数据，利用层缓存
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY scripts ./scripts

# 以非 root 运行
RUN groupadd --gid 10001 keeper \
    && useradd --uid 10001 --gid keeper --shell /usr/sbin/nologin --create-home keeper \
    && mkdir -p /app/data/accounts /app/data/config /app/data/runs /app/logs \
    && chown -R keeper:keeper /app

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER keeper

VOLUME ["/app/data"]

EXPOSE 8787

# tini 负责回收僵尸进程；Chromium 会 fork 一堆子进程
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "douyin_huohua_keeper"]
