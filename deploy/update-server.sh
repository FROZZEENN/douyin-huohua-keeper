#!/usr/bin/env bash
# =============================================================================
# douyin-huohua-keeper —— 把本机【代码】更新到 ECS，并在服务器上重建（做法 B）
#
# 与 sync-to-server.sh 的区别（很重要）：
#   sync-to-server.sh  = 首次部署用，会把 data/（登录态）和 .env.server 一起推上去。
#   update-server.sh   = 日常更新用，★绝不上传 data/ .env 证书★。
#
#   为什么？服务器上的 data/ 是挂载卷，登录态、联系人、定时配置都【活在服务器上】，
#   本地那份是旧的。整包上传会把服务器上较新的状态倒灌回旧版。
#   同理 .env 里 HUOHUA_ALLOWED_IPS 如果写的是本机内网地址（192.168.x.x），
#   传上去会把自己锁在门外。
#
# 用法（本机，cd 到项目根后执行）：
#   SERVER=root@<你的服务器IP> KEY=<你的私钥路径> bash deploy/update-server.sh
#   （只推代码不重建：加 --no-build）
# =============================================================================
set -euo pipefail

SERVER="${SERVER:?请设置 SERVER，例如 root@<你的服务器IP>}"
KEY="${KEY:-}"
REMOTE_DIR="${REMOTE_DIR:-/root/douyin-huohua-keeper}"
DO_BUILD=1
[ "${1:-}" = "--no-build" ] && DO_BUILD=0

# 切到项目根（本脚本在 deploy/ 下）
cd "$(dirname "${BASH_SOURCE[0]}")/.."

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
[ -n "$KEY" ] && SSH_OPTS+=(-i "$KEY")

# ★ 排除清单：凡是「服务器上才是权威」的东西，一律不传 ★
EXCLUDES=(
  --exclude='./.git'
  --exclude='./logs'
  --exclude='./venv'
  --exclude='./.venv'
  --exclude='./node_modules'
  --exclude='./__pycache__'
  --exclude='./.pytest_cache'
  --exclude='./.ruff_cache'
  --exclude='./*.pyc'
  --exclude='./.env'                   # ★ 服务器配置，不覆盖
  --exclude='./.env.server'            # ★ 同上
  --exclude='./data'                   # ★ 登录态/联系人/定时配置，绝不覆盖
  --exclude='./deploy/nginx/certs'     # ★ 私钥只留在服务器上，不经本机流转
  --exclude='./*.tar.gz'
)

echo "==> [1/2] 推代码到 $SERVER:$REMOTE_DIR （不含 data/ .env certs）"
# --no-same-owner：Windows 上打的包，属主是 Windows uid（如 197609）。
# 不加这个，root 解压时会照搬归档属主，把服务器上的目录/文件 chown 成 197609，
# 甚至可能连家目录一起改掉，导致 sshd StrictModes 拒绝登录（2026-09-17 踩过）。
tar czf - "${EXCLUDES[@]}" . \
  | ssh "${SSH_OPTS[@]}" "$SERVER" "mkdir -p $REMOTE_DIR && tar --no-same-owner -xzf - -C $REMOTE_DIR && echo '代码已就位'"

if [ "$DO_BUILD" = "1" ]; then
  echo
  echo "==> [2/2] 服务器上重建镜像并重启（只改代码时很快，依赖层走缓存）"
  ssh "${SSH_OPTS[@]}" "$SERVER" \
    "cd $REMOTE_DIR && docker compose -f docker-compose.yml -f docker-compose.https.yml up -d --build"
  echo
  echo "✅ 完成。打开 https://<公网IP>/ 确认（自签告警点“继续前往”）。"
else
  echo
  echo "✅ 代码已推送（--no-build，未重建）。记得在服务器上跑："
  echo "   cd $REMOTE_DIR && docker compose -f docker-compose.yml -f docker-compose.https.yml up -d --build"
fi
