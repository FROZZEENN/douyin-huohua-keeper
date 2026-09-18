# 给工作台套 HTTPS —— 两套方案，二选一

工作台页面里会出现**抖音登录二维码**（等于账号钥匙），明文 HTTP 传输不合适。
本目录提供两套 nginx 方案，**按手头资源二选一，不要同时启用**：

| 方案 | 配置文件 | 依赖 | 证书 | 特点 |
|---|---|---|---|---|
| **A. 宿主机 nginx + certbot** | `douyin-huohua-keeper.conf` | 域名 + 在宿主机装 nginx | Let's Encrypt 真证书 | 浏览器零告警、功能全（限流/安全头）；**需域名**，大陆服务器用 80 端口做 ACME 校验还可能撞备案 |
| **B. 容器 nginx + 自签证书** | `selfsigned-container.conf` + `docker-compose.https.yml` | 无 | 自签 | **免域名/免备案/免花钱**，与 docker-compose 一体；代价是浏览器告警，点一次「继续」 |

> 注：`douyin-huohua-keeper.conf` 里有个 `location ~ ^/api/accounts/[^/]+/qrcode` 的块，
> 但项目实际路由是 `/api/accounts/qr/start|poll|cancel`，该块**永不匹配**（历史遗留），无害但不生效。

---

## 方案 B（自签，当前采用）

### 架构
```
公网 ──https(443)──▶ [nginx 容器] ──http(内部网络 8787)──▶ [keeper 容器]
                     TLS 终止 + 反代                        只绑宿主回环，不对外
```
对外只开 **443**（不开 80，避开未备案 80 端口拦截）；keeper 的 8787 由 `.env` 的
`HUOHUA_BIND=127.0.0.1` 限定为宿主回环，公网摸不到。

### 步骤（服务器上，项目根目录）

1. 生成自签证书（**私钥只在服务器上生成，不经本机流转**）：
   ```bash
   SERVER_IP=<你的公网IP> bash deploy/nginx/gen-selfsigned.sh
   ```
   → 产出 `deploy/nginx/certs/server.crt` 与 `server.key`（默认 365 天）。

2. 起服务（基础 compose + HTTPS 覆盖层）：
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.https.yml up -d --build
   ```

3. 验证：
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.https.yml ps
   curl -k -H "X-Huohua-Token: <你的令牌>" https://127.0.0.1/api/health   # 应回 200
   ```

4. 手机/电脑打开 `https://<公网IP>/`，告警点「继续」，输入令牌即可。安全组放行 **443**。

### 续期（证书到期）
```bash
SERVER_IP=<公网IP> bash deploy/nginx/gen-selfsigned.sh
docker compose -f docker-compose.yml -f docker-compose.https.yml restart nginx
```

### 回退到裸 HTTP
```bash
# .env 里把 HUOHUA_BIND 改回 0.0.0.0
docker compose -f docker-compose.yml down
docker compose -f docker-compose.yml up -d --build
```

---

## 方案 A（宿主 nginx + certbot，需域名）

见 `douyin-huohua-keeper.conf` 顶部注释。核心：
```bash
sudo cp deploy/nginx/douyin-huohua-keeper.conf /etc/nginx/sites-available/
sudo ln -s /etc/nginx/sites-available/douyin-huohua-keeper.conf /etc/nginx/sites-enabled/
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d 你的域名            # 自动填好证书路径
sudo nginx -t && sudo systemctl reload nginx
```
（`.env` 里同样设 `HUOHUA_BIND=127.0.0.1`，让 keeper 只绑回环，由宿主 nginx 独占 443。
大陆服务器请注意：用 80 端口做 ACME 校验需域名已备案，否则改用 certbot 的 DNS-01 校验。）

---

## 想在 B 之上升级成“真证书”（无浏览器告警）

前提：有域名 + DNS 服务商支持 API（Cloudflare / 阿里云 DNS / DNSPod 均可）。
用 certbot 的 **DNS-01** 校验签发真证书——**不需要开 80 端口**，
把签出的 `fullchain.pem` / `privkey.pem` 放进 `deploy/nginx/certs`
（文件名叫 `server.crt` / `server.key`，或改 `selfsigned-container.conf` 里的两行路径），
`docker compose ... restart nginx` 即可，其余不变。
