# 贡献指南

感谢你有兴趣参与。这个项目很小，所以流程也简单。

## 开始之前

```bash
git clone https://github.com/FROZZEENN/douyin-huohua-keeper.git
cd douyin-huohua-keeper
./scripts/dev.sh setup      # 建环境、装依赖、下 Chromium
./scripts/dev.sh run        # 本地跑起来看看
```

Windows 用户：`scripts/dev.sh` 需要 Git Bash 或 WSL。也可以手动执行里面的命令，
每一步都是普通的 `python -m venv` / `pip install`。

## 提交前必须做的两件事

```bash
./scripts/dev.sh lint     # ruff check + format --check
./scripts/dev.sh test     # pytest
```

两个都要干净。CI 会跑同样的检查，本地先跑一遍能省一轮来回。

### 改前端时，再跑一次 JS 静态检查

前端是**零构建的原生 JS**（`workbench/static/`），没有类型检查兜底，
所以单独配了一道 ESLint（开发期工具，**运行时不需要 Node**）：

```bash
npm install        # 只需一次；只装开发依赖，node_modules 已被 .gitignore 排除
npm run lint:js
```

它主要盯 `no-undef` —— 也就是「用了没声明的变量 / 漏传的参数」。
这类错误在浏览器里只有点到那个按钮才会炸，实测踩过两次：
`contactRow` 加参数时漏改签名，函数体里的 `reload` 直接 ReferenceError
（注意可选链 `reload?.()` 挡不住「根本没声明」）。

## 代码约定

### 分层别绕过

```
workbench (HTTP)  →  scheduler (决策)  →  engine (操作)  →  store (持久化)
                       ↓
                    notify (出声)
```

- `engine` 不做重试决策、不发通知、不写磁盘。它只回答「这次成功还是失败，失败属于哪一类」。
- `workbench` 的路由保持薄。业务逻辑下沉，路由只做参数校验和序列化。
- `store` 对外只暴露原子操作。不要在别处直接 `open()` 数据文件。

### 错误处理

**禁止静默 `except`。** 这是这个项目最在意的一条。

```python
# 不行 —— 出错了没人知道
try:
    do_thing()
except Exception:
    logger.exception("failed")

# 可以 —— 分类处理，要么重试要么上报
try:
    do_thing()
except TransientError as exc:
    return FailureKind.TRANSIENT, str(exc)
except AuthError as exc:
    raise AuthExpired(str(exc)) from exc
```

如果确实需要吞掉异常（比如「清理临时文件失败无所谓」），就在 `except` 里写一行
注释说明为什么不重要，让读代码的人知道你是有意为之。

### 类型

- 全部函数都要有类型标注，包括返回值。
- 数据模型用 `frozen=True, slots=True` 的 dataclass。
- 不用 `Any`，除非在序列化边界上并且加了注释。

### 注释

中文注释，写「为什么」而不是「做什么」。

```python
# 不行 —— 代码本身就说明了
# 遍历所有联系人
for contact in contacts:

# 可以 —— 说明了一个不明显的约束
# 必须按名称排序：抖音的会话列表顺序会随最近聊天时间变化，
# 不排序的话「轮流发送」的顺序每天都不一样
for contact in sorted(contacts, key=lambda c: c.name):
```

### 测试

- 单元测试不打网络、不开浏览器。外部依赖用替身。
- 涉及真实浏览器或文件系统的放 `tests/integration/`，标 `@pytest.mark.integration`。
- 修 bug 时先写一个能复现的失败测试，再修。
- 目标覆盖率 80%。不用为覆盖率而覆盖率 —— 那些「只有真跑起来才知道对不对」
  的代码，用集成测试覆盖更诚实。

## 特别欢迎的贡献

### 新的通知通道

在 `src/douyin_huohua_keeper/notify/channels.py` 里加一个类，实现两个方法：
`send(level, title, body)` 和 `validate_config()`。然后在 `.env.example`
和 `scripts/healthcheck.py` 的 `channel_env` 里登记一下。

### 更稳的会话定位策略

抖音前端改版很勤，「找到某个好友的会话」这件事经常需要调整。
如果你发现定位失败了，欢迎把新的选择器提上来 —— 最好附上当时的页面结构片段。

### 文档与教程

用得上的教程比代码更稀缺。「我是怎么在一台 1 核 1G 的轻量服务器上把它跑起来的」
这类内容对新手价值极高。投稿到 `docs/` 下即可。

## 请不要做的

- **不要添加规避风控的功能。** 包括但不限于：伪造设备指纹、绕过验证码、
  代理 IP 池轮换。这个项目的定位是「帮个忙」，不是「对抗平台」。
  这类 PR 会被直接关闭。
- **不要让项目支持批量群发。** 设计目标是维护少数几个真实关系，
  不是变成营销工具。
- **不要引入前端构建链。** 前端保持零构建是有意为之，不是偷懒。

## Commit 信息

用中文或英文都行，说清楚改了什么、为什么。不需要严格的 Conventional Commits 格式，
但请避免「fix bug」「update」这种没信息量的消息。

```
好的例子：
  修复会话列表未加载完就点击导致的定位失败
  通知通道增加失败重试，避免告警本身静默丢失

不好的例子：
  update
  fix
  ...
```

## 提 PR

1. Fork，开分支，改完跑 lint 和 test
2. PR 描述里说清楚：解决了什么问题、怎么验证的
3. 如果改动涉及界面，附一张截图
4. 如果改动涉及行为变化，更新 README 或相关文档

## 关于许可证

提交的代码默认按 MIT 授权。别提交你从别处抄来的、许可证不兼容的代码。

## 有问题？

开 Issue 问就行。这个项目没有「太基础的问题」这回事 —— 如果你没看懂文档，
那说明文档该改。
