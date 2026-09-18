#!/usr/bin/env bash
# =============================================================================
# 一键部署（在【服务器】上跑，只用这一条命令）
#
# 用法（服务器上，先进项目目录）：
#   cd ~/douyin-huohua-keeper
#   bash deploy/一键部署.sh <你的服务器IP>
#
# 它会依次：装 Docker → 生成自签证书 → 启动服务 → 自检。
# 可重复执行（已装/已生成的会跳过）。
# =============================================================================
set -euo pipefail

IP="${1:-}"
if [ -z "$IP" ]; then
  echo "用法: bash deploy/一键部署.sh <服务器公网IP>"
  echo "例  : bash deploy/一键部署.sh <你的服务器IP>"
  exit 1
fi

# 切到项目根（本脚本在 deploy/ 下）
cd "$(dirname "${BASH_SOURCE[0]}")/.."
echo "项目目录: $(pwd)"

# 如果还没 .env，把 .env.server 复制成 .env（小白少打一步）
if [ ! -f .env ] && [ -f .env.server ]; then
  echo "正在把 .env.server 复制为 .env ..."
  cp -f .env.server .env
fi

if [ "$(id -u)" != "0" ] && ! command -v sudo >/dev/null 2>&1; then
  echo "⚠ 需要 root 权限（安装 Docker 要写系统目录）。请用 root 登录，或命令前加 sudo。"
fi
SUDO=""
[ "$(id -u)" = "0" ] || SUDO="sudo"

echo
echo "==== [1/3] 安装 Docker ===="
if command -v docker >/dev/null 2>&1; then
  echo "已安装：$(docker --version)"
else
  $SUDO apt-get update
  $SUDO apt-get install -y ca-certificates curl
  $SUDO install -m 0755 -d /etc/apt/keyrings
  # Docker 官方源 download.docker.com 在国内常被重置（Connection reset by peer），
  # 默认改走阿里云镜像；如需官方源：DOCKER_CE_MIRROR=https://download.docker.com
  DOCKER_CE_BASE="${DOCKER_CE_MIRROR:-https://mirrors.aliyun.com/docker-ce}/linux/ubuntu"
  if ! $SUDO curl -fsSL "$DOCKER_CE_BASE/gpg" -o /etc/apt/keyrings/docker.asc 2>/dev/null; then
    echo "⚠ 镜像源不可用，回退到 Docker 官方源..."
    DOCKER_CE_BASE="https://download.docker.com/linux/ubuntu"
    $SUDO curl -fsSL "$DOCKER_CE_BASE/gpg" -o /etc/apt/keyrings/docker.asc
  fi
  $SUDO chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] $DOCKER_CE_BASE $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null
  $SUDO apt-get update
  $SUDO apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  $SUDO systemctl enable --now docker
  echo "已安装：$(docker --version)"
fi

echo
echo "==== [2/3] 生成自签证书 ===="
if [ -f deploy/nginx/certs/server.crt ]; then
  echo "证书已存在，跳过（要重签先删 deploy/nginx/certs/）"
else
  SERVER_IP="$IP" bash deploy/nginx/gen-selfsigned.sh
fi

echo
echo "==== [3/3] 启动服务（首次会 build，拉 ~600MB，耐心等 1~3 分钟）===="

# 数据目录必须能被容器内的 keeper 用户（uid 10001）写入，
# 否则入口脚本会 "FATAL: /app/data is not writable" 直接退出，
# 容器起不来，nginx 反代就是 502。（2026-09-17 踩过）
mkdir -p data logs
$SUDO chown -R 10001:10001 data logs 2>/dev/null \
  || echo "⚠ chown 失败，请手动执行： $SUDO chown -R 10001:10001 data logs"

$SUDO docker compose -f docker-compose.yml -f docker-compose.https.yml up -d --build

echo
echo "==== 完成，做一次自检（首次启动较慢，最多等 90 秒）===="
TOKEN="$(grep -E '^HUOHUA_TOKEN=' .env | cut -d= -f2- || true)"
code="000"
for _ in $(seq 1 30); do
  code="$(curl -k -s -o /dev/null -w '%{http_code}' -H "X-Huohua-Token: $TOKEN" https://127.0.0.1/api/health || echo 000)"
  [ "$code" = "200" ] && break
  sleep 3
done
echo "https://127.0.0.1/api/health  ->  $code  (期望 200)"

if [ "$code" = "200" ]; then
  echo
  echo "✅ 成功！用手机/电脑打开： https://$IP/"
  echo "   浏览器会提示证书不安全 —— 点「高级」→「继续前往」即可，然后输入工作台令牌。"
else
  echo
  echo "⚠ 自检没通过。看日志排错： $SUDO docker compose -f docker-compose.yml -f docker-compose.https.yml logs --tail=100"
fi
