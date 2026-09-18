---
name: Bug 报告
about: 跑不起来 / 发不出去 / 行为不符合预期
title: "[Bug] "
labels: bug
---

<!--
请尽量把下面几项填完。这个项目的问题（选择器失效、登录态、网络）几乎都能靠这几条定位 ——
信息少了，我只能反复问，来回好几轮。
-->

## 一句话描述

<!-- 例如：演练能过，但真发时卡在「找不到输入框」 -->

## 环境

| 项 | 值 |
|---|---|
| 操作系统 | <!-- Windows 11 / macOS 14 / Ubuntu 22.04 / 树莓派 … --> |
| Python 版本 | <!-- `python -V` 的输出 --> |
| 部署方式 | <!-- 本地 venv / Docker / systemd / cron --> |
| 项目版本 | <!-- `python -m douyin_huohua_keeper --version`，或 git commit --> |

## 自检输出（**必填**）

```bash
python scripts/healthcheck.py
```

<details>
<summary>把输出粘在这里</summary>

```

```
</details>

## 复现步骤

1.
2.
3.

## 相关日志

```bash
tail -50 logs/huohua.log
```

<details>
<summary>粘在这里</summary>

```

```
</details>

<!-- ⚠️ 粘贴前请先打码：好友昵称、HUOHUA_TOKEN、Server酱 Key、服务器 IP -->

## 已经试过什么

- [ ] 先跑了「演练」模式（比真发更容易复现，且对好友无影响）
- [ ] `playwright install chromium chromium-headless-shell`（**两个**都装了）
- [ ] 确认能正常打开 douyin.com
- [ ] 看了 [docs/05-故障排查.md](../docs/05-故障排查.md) 里对应的症状

## 补充说明

<!-- 其它你觉得有用的信息。截图很有帮助，但记得脱敏。 -->
