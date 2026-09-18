#!/usr/bin/env bash
# =============================================================================
# 把本机项目传到 ECS（含 data/ 登录态 + .env.server）。
# 用 tar 通过 ssh 管道传输，自动排除 logs/.git/venv/node_modules 等无关内容。
#
# 用法（在本机 Git Bash 里，cd 到项目根后执行）：
#   SERVER=user@1.2.3.4 KEY=~/.ssh/my-key bash deploy/sync-to-server.sh
#   （SERVER=服务器地址，KEY=你的私钥路径；不传 KEY 则用默认 ssh 配置）
#   ⚠ 用 `bash deploy/sync-to-server.sh` 调用，别依赖文件可执行位（Windows 下传输易丢）
# =============================================================================
set -euo pipefail

SERVER="${SERVER:?请设置 SERVER，例如 user@1.2.3.4}"
KEY="${KEY:-}"
REMOTE_DIR="${REMOTE_DIR:-~/douyin-huohua-keeper}"

# 传整个项目；到服务器后把 .env.server 复制成 .env。
# 注意：本机 .env 不传（否则会覆盖服务器配置）；证书目录不传（在服务器上现生成）。
EXCLUDES=(
  --exclude='./.git'
  --exclude='./logs'
  --exclude='./venv'
  --exclude='./node_modules'
  --exclude='./__pycache__'
  --exclude='./*.pyc'
  --exclude='./.env'                   # 本机配置不传
  --exclude='./deploy/nginx/certs'     # 证书在服务器上现生成，私钥不经本机流转
)

SSH_CMD=(ssh)
if [ -n "$KEY" ]; then SSH_CMD+=(-i "$KEY"); fi

echo "==> 打包并传到 $SERVER:$REMOTE_DIR"
tar czf - "${EXCLUDES[@]}" . \
  | "${SSH_CMD[@]}" "$SERVER" "mkdir -p $REMOTE_DIR && tar xzf - -C $REMOTE_DIR && cp -f $REMOTE_DIR/.env.server $REMOTE_DIR/.env"

echo "==> 完成。登录服务器后（项目根目录）按顺序执行："
echo "    cd $REMOTE_DIR"
echo "    SERVER_IP=<公网IP> bash deploy/nginx/gen-selfsigned.sh   # 生成自签证书"
echo "    docker compose -f docker-compose.yml -f docker-compose.https.yml up -d --build"
echo "    docker compose -f docker-compose.yml -f docker-compose.https.yml logs -f"
echo "    浏览器打开 https://<公网IP>/  （自签告警点“继续前往”），输入令牌登录"
