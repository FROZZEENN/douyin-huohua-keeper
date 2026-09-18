#!/usr/bin/env bash
# =============================================================================
# 生成自签 TLS 证书（免域名、免备案、免花钱）。
#
# 在【服务器上】执行（私钥不必经由本机流转）：
#   SERVER_IP=<服务器公网IP> bash deploy/nginx/gen-selfsigned.sh
#
# 可选：
#   DAYS=825 ...   有效期天数（默认 365；>398 天个别 iOS/Safari 会更挑剔）
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERT_DIR="$HERE/certs"
IP="${SERVER_IP:?用法: SERVER_IP=<服务器公网IP> bash deploy/nginx/gen-selfsigned.sh}"
DAYS="${DAYS:-365}"

command -v openssl >/dev/null 2>&1 || { echo "缺 openssl，请先 apt-get install -y openssl"; exit 1; }

mkdir -p "$CERT_DIR"

# CN 用 IP 是为了好认；现代浏览器只认 SAN，所以必须带 subjectAltName
openssl req -x509 -nodes -newkey rsa:2048 -days "$DAYS" \
  -keyout "$CERT_DIR/server.key" \
  -out    "$CERT_DIR/server.crt" \
  -subj   "/C=CN/O=douyin-huohua-keeper/CN=$IP" \
  -addext "subjectAltName=IP:$IP,DNS:localhost" \
  >/dev/null 2>&1

chmod 600 "$CERT_DIR/server.key"
chmod 644 "$CERT_DIR/server.crt"

echo "✅ 已生成自签证书："
echo "   $CERT_DIR/server.crt  (SAN=IP:$IP,DNS:localhost  有效期 ${DAYS} 天)"
echo "   $CERT_DIR/server.key  (私钥，勿外传)"
echo
echo "下一步："
echo "   docker compose -f docker-compose.yml -f docker-compose.https.yml up -d --build"
