# 备份、迁移与灾难恢复

这个项目最怕的不是「服务挂了」（挂了重启就行），而是**登录态丢了**或者
**配置丢了**。前者要重新扫码，后者要重新配一遍收件人和时间。

## 你需要备份什么

| 路径 | 内容 | 丢了会怎样 | 优先级 |
| --- | --- | --- | --- |
| `data/accounts/*.state.json` | 登录态 | 要重新扫码（30 秒的事，但得本人操作） | 中 |
| `data/config/` | 收件人、任务、时间 | 要重新配一遍 | **高** |
| `data/runs/` | 运行历史 | 看上个月的记录就没了 | 低 |
| `.env` | 令牌、通知密钥 | 要重新填一遍 | **高** |

一句话：**`data/` 加 `.env`，就是全部。**

⚠️ 但请注意两件事：

1. `data/accounts/*.state.json` **等同于该账号的密码**。备份文件不要丢到网盘、
   不要发到群里、不要提交进 git。要备份就用加密归档，或者干脆不备份 ——
   毕竟重新扫码的成本只有 30 秒，而泄露的后果不可控。
2. `.env` 里是通知通道的密钥（Bark Key、钉钉 Webhook 等）。泄露了别人可以
   给你的手机发消息。同样按密钥对待。

## 完整备份

```bash
cd /opt/douyin-huohua-keeper

# 打一个带日期的归档（加密，密码自己记牢）
tar czf - data .env \
  | openssl enc -aes-256-cbc -pbkdf2 -salt -out huohua-backup-$(date +%F).tar.gz.enc

# 验证归档能解开（很重要，别等到要恢复时才发现打不开）
openssl enc -d -aes-256-cbc -pbkdf2 -in huohua-backup-$(date +%F).tar.gz.enc | tar tzf - | head
```

不带加密的版本（仅在确定存放位置安全时使用）：

```bash
tar czf huohua-backup-$(date +%F).tar.gz data .env
```

## 恢复 / 迁移到新服务器

```bash
# 1. 新机器上准备项目
git clone https://github.com/FROZZEENN/douyin-huohua-keeper.git /opt/douyin-huohua-keeper
cd /opt/douyin-huohua-keeper
python -m venv .venv && .venv/bin/pip install -r requirements.txt
# 两个都要：无头模式用的是 chromium-headless-shell（只装 chromium 会报
# "Executable doesn't exist"）。--with-deps 会顺带 apt 装缺的系统库。
.venv/bin/playwright install --with-deps chromium chromium-headless-shell

# 2. 解开备份，覆盖到项目目录
openssl enc -d -aes-256-cbc -pbkdf2 -in huohua-backup-2026-09-14.tar.gz.enc | tar xzf -

# 3. 确认目录权限（Docker 下 uid 是 10001）
sudo chown -R keeper:keeper data logs
chmod 600 data/accounts/*.state.json .env

# 4. 自检
.venv/bin/python scripts/healthcheck.py      # 或 ./scripts/dev.sh check

# 5. 起服务
sudo systemctl start douyin-huohua-keeper    # 或 docker compose up -d
```

## 定期自动备份（可选）

```bash
crontab -e
```

```cron
# 每周一凌晨 3 点备份一次，保留最近 8 份
0 3 * * 1 cd /opt/douyin-huohua-keeper && tar czf /var/backups/huohua-$(date +\%F).tar.gz data .env && ls -t /var/backups/huohua-*.tar.gz | tail -n +9 | xargs -r rm --
```

## 只丢了登录态怎么办

最轻的情况。不用恢复任何东西，打开工作台：

1. 账号状态会显示「登录态失效」或类似字样
2. 点「重新扫码」
3. 手机抖音扫一下，确认
4. 完成

当天如果还没发送，扫码成功后可以直接点「立即发送一次」补上。

## 只丢了配置怎么办

如果 `data/config/` 没了但账号还在：

1. 打开工作台，进「好友」页
2. 点「同步会话列表」—— 会重新拉取你最近的聊天对象
3. 勾选要保火花的那些人
4. 回「任务」页重设时间

大约两分钟。所以配置其实没那么怕丢 —— 真正不可再生的是时间（连续天数）。

## 灾难恢复清单

| 症状 | 最可能的原因 | 处理 |
| --- | --- | --- |
| 工作台打不开 | 容器/服务没起 | `docker compose ps` / `systemctl status` |
| 工作台 401 | 令牌不对 | 核对 `.env` 里的 `HUOHUA_TOKEN` |
| 工作台 403 | 被 IP 白名单拦了 | 你的出口 IP 变了，更新 `HUOHUA_ALLOWED_IPS` |
| 账号状态显示失效 | Cookie 过期（正常现象，7~30 天一次） | 重新扫码 |
| 连续失败且报风控 | 行为被判定为异常 | **先停一天**，换文案和时段，别硬刚 |
| 浏览器启动即崩 | 内存不足 / `/dev/shm` 太小 | 加内存，或 compose 里设 `shm_size: 1gb` |
| 日志里一堆 timeout | 网络不稳 | 看报告里的失败分类，TRANSIENT 会自动重试 |
