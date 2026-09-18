"""手动扫码登录助手。

## 为什么需要这个脚本

抖音对「自动化浏览器 + 陌生环境」会触发风控，要求**二级身份验证**
（短信验证码 / 刷脸 / 登录密码）。这一步**必须由人来完成** ——
程序没法替你输验证码，也不该去规避风控。

所以首次登录的流程是：

1. 用这个脚本弹出一个**有头（可见）**的浏览器窗口
2. 你在窗口里扫码，并按提示完成身份验证
3. 脚本检测到真正登录成功后，把登录态原子写入 ``data/accounts/<id>.state.json``
4. 之后日常运行**复用这份登录态，不再需要验证**

## 用法

```bash
python scripts/login.py                # 默认账号 main
python scripts/login.py --account main # 指定账号 id
python scripts/login.py --timeout 600  # 自定义等待秒数
```

登录态文件等同密码，请勿提交到版本库（``.gitignore`` 已排除 ``data/``）。

## 关于无头模式

**不要用无头模式做首次登录。** 二级验证弹窗在无头浏览器里用户看不见、
点不到，会表现为「扫了码但永远登不上」。登录完成后再切回无头跑日常任务。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# 允许直接 `python scripts/login.py` 运行（不必先 pip install -e .）
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from douyin_huohua_keeper.config import load_settings
from douyin_huohua_keeper.engine import qrlogin
from douyin_huohua_keeper.store.repo import Repository

# 每轮检查的间隔。1.5 秒足够灵敏，也不会把 CPU 打满。
POLL_INTERVAL_SECONDS = 1.5


def _print_header(account_id: str, target: Path) -> None:
    print()
    print("=" * 68)
    print("  抖音火花守护 · 手动扫码登录")
    print("=" * 68)
    print(f"  账号 id   : {account_id}")
    print(f"  登录态将存: {target}")
    print()
    print("  接下来会弹出一个浏览器窗口。请在那里面：")
    print("    1. 用手机抖音「扫一扫」扫描页面上的二维码")
    print("    2. 如果出现「身份验证」，按提示完成")
    print("       （短信验证码 / 刷脸 / 登录密码，任选一种）")
    print()
    print("  ⚠ 步骤 2 是必须的：抖音对陌生环境会要求二次验证，")
    print("    跳过它就会一直停在「需在手机上进行确认」。")
    print()
    print("  完成后本脚本会自动保存登录态并退出。")
    print("=" * 68)
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="弹出可见浏览器窗口，手动扫码登录并保存登录态",
    )
    parser.add_argument("--account", default=None, help="账号 id（默认用配置里的第一个）")
    parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="等待登录完成的秒数（默认 600，即 10 分钟）",
    )
    args = parser.parse_args()

    settings = load_settings()
    repo = Repository(settings.data_dir)
    repo.ensure_layout()

    account_id = args.account or "main"
    target = repo.account_state_path(account_id)

    _print_header(account_id, target)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("❌ 未安装 Playwright。请先执行：", file=sys.stderr)
        print("     pip install -r requirements.txt", file=sys.stderr)
        print("     playwright install chromium", file=sys.stderr)
        return 2

    with sync_playwright() as pw:
        try:
            # headless=False 是这个脚本的**核心**：
            # 二级验证必须有人在窗口前操作。
            browser = pw.chromium.launch(
                headless=False,
                args=["--start-maximized"],
            )
        except Exception as exc:  # noqa: BLE001
            print(f"❌ 浏览器启动失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            print("   没装 Chromium 的话执行：playwright install chromium", file=sys.stderr)
            return 2

        context = browser.new_context(
            locale="zh-CN",
            timezone_id=settings.scheduler.schedule.timezone or "Asia/Shanghai",
            viewport={"width": 1280, "height": 860},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
        )
        page = context.new_page()

        print("正在打开抖音登录页…")
        try:
            qr = qrlogin.fetch_qrcode(page, navigate=True, timeout_ms=30_000)
            print(f"✅ 二维码已就绪（{len(qr.image_base64)} 字节的 base64）")
            print("   → 请切到浏览器窗口扫码")
        except Exception as exc:  # noqa: BLE001
            print(f"⚠ 没能在页面上定位到二维码：{exc}")
            print("  窗口仍然开着，你可以直接在里面操作。")

        print()
        deadline = time.monotonic() + args.timeout
        last_state: str | None = None
        saved = False

        while time.monotonic() < deadline:
            poll = qrlogin.poll_login(page, timeout_ms=500)
            state = poll.state.value

            if state != last_state:
                stamp = time.strftime("%H:%M:%S")
                print(f"[{stamp}] {state:<14} {poll.detail}")
                last_state = state

            if poll.state is qrlogin.LoginState.SUCCESS:
                print()
                print("检测到登录成功，正在保存登录态…")
                try:
                    qrlogin.save_storage_state(page, target)
                except Exception as exc:  # noqa: BLE001
                    print(f"❌ 保存失败：{type(exc).__name__}: {exc}", file=sys.stderr)
                    print("   登录态没存下来的话，这次登录就等于白做了。", file=sys.stderr)
                    browser.close()
                    return 1

                size = target.stat().st_size
                print(f"✅ 登录态已保存：{target}（{size} 字节）")
                print()
                print("接下来可以：")
                print("  1. 启动工作台： python -m douyin_huohua_keeper")
                print("  2. 或立刻发一次： python -m douyin_huohua_keeper --run-once")
                saved = True
                break

            if poll.state is qrlogin.LoginState.EXPIRED:
                print("   （二维码失效了，可以用脚本重开，或在窗口里点刷新）")

            time.sleep(POLL_INTERVAL_SECONDS)

        if not saved:
            print()
            print(f"⏱ 等待超时（{args.timeout:.0f} 秒），没有检测到登录成功。")
            print("  常见原因：没扫码 / 没在手机上点「确认登录」/ 身份验证没做完。")
            print(f"  退出时状态：{last_state}")

        browser.close()

    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
