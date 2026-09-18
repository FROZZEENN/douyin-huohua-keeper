# douyin-huohua-keeper — 部署辅助

这个目录放的是「把项目跑到你自己机器上」的辅助材料。Docker 是推荐路径，
其余是给特定场景的补充。

## 文件一览

| 文件 | 用途 |
| --- | --- |
| `systemd/douyin-huohua-keeper.service` | 不用 Docker，直接在 Linux 上跑成系统服务 |
| `nginx/douyin-huohua-keeper.conf` | 套一层 HTTPS 反代（强烈建议公网部署时使用） |
| `crontab.example` | 只用 cron 触发、不用内置调度器的极简玩法 |
| `BACKUP.md` | 备份与迁移：换服务器时怎么把登录态搬过去 |

## 三种部署姿势怎么选

### 姿势一：Docker Compose（推荐）

适合绝大多数人。容器里自带 Chromium 和它的系统依赖，你不用在宿主机上装
一坨 `.so`。升级就是 `docker compose pull && docker compose up -d`。

```bash
cp .env.example .env && vim .env
docker compose up -d && docker compose logs -f
```

### 姿势二：裸机 + systemd

适合树莓派、老笔记本、已经跑着别的东西不想再上 Docker 的机器。
见 `systemd/` 下的单元文件，里面有完整注释。

### 姿势三：只借用它的发送能力

适合已经有自己的调度体系的人。用 cron 每天调一次命令行接口即可，
内置调度器可以不启用（`HUOHUA_ENABLE_SCHEDULER=false`）。
见 `crontab.example`。

## 公网部署必做三件事

1. **改令牌**。`.env` 里的 `HUOHUA_TOKEN` 换成 32 位以上随机串：
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
2. **限来源 IP**。设 `HUOHUA_ALLOWED_IPS=你的出口IP`，或者干脆把 compose 里的
   端口映射改成 `127.0.0.1:8787:8787`，只允许本机访问，需要时再 SSH 隧道。
3. **上 HTTPS**。工作台里会出现抖音登录二维码，明文 HTTP 传输不合适。
   `nginx/` 下的配置配合 certbot 十分钟搞定。

## 资源占用参考

| 场景 | 内存 | 磁盘 |
| --- | --- | --- |
| 常驻（空闲，浏览器未启动） | 约 120 MB | 约 600 MB（镜像 + 依赖） |
| 执行中（Chromium 已拉起） | 约 500–800 MB | 每次运行截图 200 KB–2 MB |
| 建议规格 | 1 核 1 GB 起 | 10 GB 足够 |

轻量应用服务器最低配就够用。**不要**买那种 512 MB 内存的机器 —— Chromium 会
在页面加载时直接 OOM，而且报错信息很难看懂。
