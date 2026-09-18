#!/usr/bin/env python
"""自检脚本 —— 在真正跑起服务之前，把「环境是不是有问题」一次性问清楚。

它的价值在于：把那些通常要等运行时才暴露的坑（Chromium 没装、数据目录
不可写、时区不对、通知密钥填错格式）提前列出来，而不是让你在某个凌晨发现
火花断了然后回头翻日志。

    python scripts/healthcheck.py
    python scripts/healthcheck.py --json     # 输出机器可读结果

退出码：0 = 全部通过（可能有警告），1 = 存在必须修复的错误。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import shutil
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

Status = Literal["ok", "warn", "error"]

RESET = "\033[0m"
COLORS: dict[Status, str] = {
    "ok": "\033[32m",
    "warn": "\033[33m",
    "error": "\033[31m",
}
ICONS: dict[Status, str] = {"ok": "OK  ", "warn": "WARN", "error": "FAIL"}


@dataclass
class Check:
    """单项检查结果。"""

    name: str
    status: Status
    detail: str
    hint: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: Status, detail: str, hint: str = "") -> None:
        self.checks.append(Check(name=name, status=status, detail=detail, hint=hint))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == "error"]

    @property
    def warned(self) -> list[Check]:
        return [c for c in self.checks if c.status == "warn"]


# ---------------------------------------------------------------------------
# 各项检查
# ---------------------------------------------------------------------------


def check_python(report: Report) -> None:
    major, minor = sys.version_info[:2]
    version = f"{major}.{minor}.{sys.version_info[2]}"
    if (major, minor) >= (3, 11):
        report.add("Python 版本", "ok", version)
    else:
        report.add(
            "Python 版本",
            "error",
            f"{version}（需要 >= 3.11）",
            hint="3.10 及以下缺少本项目用到的若干类型语法",
        )


def check_platform(report: Report) -> None:
    report.add("运行平台", "ok", f"{platform.system()} {platform.release()} ({platform.machine()})")


def check_dependencies(report: Report) -> None:
    required = {
        "playwright": "playwright",
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "apscheduler": "apscheduler",
        "dotenv": "python-dotenv",
    }
    missing: list[str] = []
    for module, package in required.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(package)

    if missing:
        report.add(
            "Python 依赖",
            "error",
            f"缺少：{', '.join(missing)}",
            hint="pip install -r requirements.txt",
        )
    else:
        report.add("Python 依赖", "ok", f"{len(required)} 项全部就绪")


def check_chromium(report: Report) -> None:
    """确认 Playwright 真的能开出浏览器 —— 这是最容易漏掉的一步。"""
    browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or ""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        report.add("Chromium", "warn", "playwright 未安装，跳过检查", hint="pip install playwright")
        return

    try:
        with sync_playwright() as pw:
            exe = pw.chromium.executable_path
    except Exception as exc:  # noqa: BLE001
        report.add(
            "Chromium",
            "error",
            f"无法定位浏览器：{type(exc).__name__}: {exc}",
            hint="playwright install chromium chromium-headless-shell",
        )
        return

    if exe and Path(exe).exists():
        where = f"{exe}"
        if browsers_path:
            where += f"（PLAYWRIGHT_BROWSERS_PATH={browsers_path}）"
        report.add("Chromium", "ok", where)
    else:
        report.add(
            "Chromium",
            "error",
            f"路径不存在：{exe}",
            hint="playwright install chromium chromium-headless-shell",
        )


def check_data_dir(report: Report) -> None:
    data_dir = Path(os.environ.get("HUOHUA_DATA_DIR") or "data")
    subdirs = ["accounts", "config", "runs"]

    if not data_dir.exists():
        report.add(
            "数据目录",
            "warn",
            f"{data_dir} 不存在（首次启动会自动创建）",
        )
        return

    problems: list[str] = []
    for sub in subdirs:
        d = data_dir / sub
        if not d.exists():
            problems.append(f"{sub}/ 缺失")
        elif not os.access(d, os.W_OK):
            problems.append(f"{sub}/ 不可写")

    if problems:
        report.add(
            "数据目录",
            "error",
            f"{data_dir}：{'; '.join(problems)}",
            hint="检查目录权限，Docker 下通常是 chown -R 10001:10001 <host-dir>",
        )
    else:
        report.add("数据目录", "ok", f"{data_dir.resolve()}")


def check_atomic_write(report: Report) -> None:
    """真的写一个小文件再重命名 —— 比检查权限位更能反映现实。"""
    data_dir = Path(os.environ.get("HUOHUA_DATA_DIR") or "data")
    if not data_dir.exists():
        report.add("原子写能力", "warn", "数据目录不存在，跳过")
        return

    probe = data_dir / "runs" / ".healthcheck.tmp"
    target = data_dir / "runs" / ".healthcheck"
    try:
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text('{"ok": true}', encoding="utf-8")
        os.replace(probe, target)
        target.read_text(encoding="utf-8")
        target.unlink()
        report.add("原子写能力", "ok", "写临时文件 + 重命名 正常")
    except OSError as exc:
        report.add("原子写能力", "error", f"{type(exc).__name__}: {exc}")
        for leftover in (probe, target):
            with contextlib.suppress(OSError):
                leftover.unlink()


def check_timezone(report: Report) -> None:
    """火花按自然日算，时区错了发送窗口就错了。"""
    tz = os.environ.get("TZ")
    now = datetime.now()

    if sys.platform == "win32":
        # Windows 上 TZ 环境变量基本无效，看系统本地时间即可
        report.add("时区", "ok", f"系统本地时间 {now:%Y-%m-%d %H:%M:%S}（Windows，TZ 变量不生效）")
        return

    if not tz:
        report.add(
            "时区",
            "warn",
            f"未设置 TZ，当前按 {now:%H:%M:%S} 运行",
            hint="容器里建议设 TZ=Asia/Shanghai（docker-compose.yml 已默认）",
        )
    else:
        report.add("时区", "ok", f"TZ={tz}，当前 {now:%Y-%m-%d %H:%M:%S} ({now:%Z})")


def check_display(report: Report) -> None:
    """无头环境里却要求有头浏览器，会直接启动失败 —— 提前说清楚。"""
    headless_raw = os.environ.get("HUOHUA_HEADLESS", "true").strip().lower()
    headless = headless_raw in {"1", "true", "yes", "on"}
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    is_windows = sys.platform == "win32"

    if headless:
        report.add("浏览器模式", "ok", "HUOHUA_HEADLESS=true（无头）")
    elif is_windows or has_display:
        report.add("浏览器模式", "ok", "有头模式，检测到可用显示环境")
    else:
        report.add(
            "浏览器模式",
            "error",
            "HUOHUA_HEADLESS=false 但当前环境没有 DISPLAY",
            hint="容器/服务器上请设为 true，或安装 Xvfb",
        )


def check_token(report: Report) -> None:
    token = (os.environ.get("HUOHUA_TOKEN") or "").strip()
    if not token:
        report.add(
            "工作台令牌",
            "warn",
            "HUOHUA_TOKEN 为空，工作台将无鉴权开放",
            hint="设置一个足够长的随机串；若端口暴露在公网则必须设置",
        )
    elif len(token) < 12:
        report.add(
            "工作台令牌",
            "warn",
            f"令牌过短（{len(token)} 字符）",
            hint='建议 24 字符以上随机串：python -c "import secrets;print(secrets.token_urlsafe(32))"',
        )
    else:
        report.add("工作台令牌", "ok", f"已设置（{len(token)} 字符）")


def check_allowed_ips(report: Report) -> None:
    raw = (os.environ.get("HUOHUA_ALLOWED_IPS") or "").strip()
    if not raw:
        report.add(
            "IP 白名单",
            "warn",
            "未限制来源 IP",
            hint="个人自用建议只放行自己的出口 IP，双保险",
        )
        return

    import ipaddress

    bad: list[str] = []
    for item in (p.strip() for p in raw.split(",") if p.strip()):
        try:
            if "/" in item:
                ipaddress.ip_network(item, strict=False)
            else:
                ipaddress.ip_address(item)
        except ValueError:
            bad.append(item)

    if bad:
        report.add("IP 白名单", "error", f"格式错误：{', '.join(bad)}", hint="用逗号分隔的 IP 或 CIDR")
    else:
        report.add("IP 白名单", "ok", raw)


def check_notify_channels(report: Report) -> None:
    raw = (os.environ.get("HUOHUA_NOTIFY_CHANNELS") or "").strip()
    if not raw:
        report.add(
            "通知通道",
            "warn",
            "未配置任何通知通道（失败时你不会收到消息）",
            hint="HUOHUA_NOTIFY_CHANNELS=bark,serverchan 之类，见 .env.example",
        )
        return

    channel_env = {
        "bark": ["HUOHUA_BARK_URL"],
        "serverchan": ["HUOHUA_SERVERCHAN_KEY"],
        "dingtalk": ["HUOHUA_DINGTALK_WEBHOOK"],
        "feishu": ["HUOHUA_FEISHU_WEBHOOK"],
        "telegram": ["HUOHUA_TELEGRAM_BOT_TOKEN", "HUOHUA_TELEGRAM_CHAT_ID"],
        "webhook": ["HUOHUA_GENERIC_WEBHOOK"],
    }

    enabled = [c.strip().lower() for c in raw.split(",") if c.strip()]
    unknown = [c for c in enabled if c not in channel_env]
    incomplete: list[str] = []

    for name in enabled:
        if name not in channel_env:
            continue
        missing = [var for var in channel_env[name] if not (os.environ.get(var) or "").strip()]
        if missing:
            incomplete.append(f"{name}(缺 {', '.join(missing)})")

    if unknown:
        report.add(
            "通知通道",
            "error",
            f"未知通道：{', '.join(unknown)}",
            hint=f"可用：{', '.join(channel_env)}",
        )
    elif incomplete:
        report.add("通知通道", "error", f"配置不完整：{'; '.join(incomplete)}", hint="见 .env.example")
    else:
        report.add("通知通道", "ok", f"已启用：{', '.join(enabled)}")


def check_thresholds(report: Report) -> None:
    def as_int(var: str, default: int) -> int | None:
        raw = (os.environ.get(var) or "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            return None

    warn = as_int("HUOHUA_WARN_THRESHOLD", 2)
    crit = as_int("HUOHUA_CRITICAL_THRESHOLD", 3)

    if warn is None or crit is None:
        report.add(
            "告警阈值", "error", "取值不是整数", hint="HUOHUA_WARN_THRESHOLD / HUOHUA_CRITICAL_THRESHOLD"
        )
    elif warn >= crit:
        report.add(
            "告警阈值",
            "error",
            f"警告阈值({warn}) 必须小于 紧急阈值({crit})",
            hint="否则永远不会升级到 CRITICAL",
        )
    elif warn < 1:
        report.add("告警阈值", "warn", f"警告阈值={warn}，第一次失败就会告警")
    else:
        report.add("告警阈值", "ok", f"连续失败 {warn} 天 WARNING，{crit} 天 CRITICAL")


def check_disk(report: Report) -> None:
    data_dir = Path(os.environ.get("HUOHUA_DATA_DIR") or "data")
    probe = data_dir if data_dir.exists() else Path(".")
    try:
        usage = shutil.disk_usage(probe)
    except OSError as exc:
        report.add("磁盘空间", "warn", f"无法读取：{exc}")
        return

    free_gb = usage.free / (1024**3)
    if free_gb < 0.5:
        report.add(
            "磁盘空间",
            "error",
            f"仅剩 {free_gb:.2f} GB",
            hint="失败截图会占空间，清一下 data/runs/ 里的旧记录",
        )
    elif free_gb < 2:
        report.add("磁盘空间", "warn", f"剩余 {free_gb:.2f} GB")
    else:
        report.add("磁盘空间", "ok", f"剩余 {free_gb:.1f} GB")


def check_existing_state(report: Report) -> None:
    """有登录态就说一句，顺便提示大致新鲜度。"""
    data_dir = Path(os.environ.get("HUOHUA_DATA_DIR") or "data")
    accounts = data_dir / "accounts"
    if not accounts.is_dir():
        report.add("登录态", "warn", "尚无登录态，需要在工作台扫码绑定")
        return

    states = sorted(accounts.glob("*.state.json"))
    if not states:
        report.add("登录态", "warn", "尚无登录态，需要在工作台扫码绑定")
        return

    lines: list[str] = []
    for path in states:
        try:
            age_days = (datetime.now().timestamp() - path.stat().st_mtime) / 86400
            if age_days > 14:
                lines.append(f"{path.stem}（{age_days:.0f} 天未刷新，可能已失效）")
            else:
                lines.append(f"{path.stem}（{age_days:.1f} 天前刷新）")
        except OSError:
            lines.append(path.stem)

    stale = any("可能已失效" in line for line in lines)
    report.add(
        "登录态",
        "warn" if stale else "ok",
        "; ".join(lines),
        hint="扫码重登即可，工作台点「重新扫码」" if stale else "",
    )


CHECKS = [
    check_python,
    check_platform,
    check_dependencies,
    check_chromium,
    check_data_dir,
    check_atomic_write,
    check_disk,
    check_timezone,
    check_display,
    check_token,
    check_allowed_ips,
    check_notify_channels,
    check_thresholds,
    check_existing_state,
]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def load_dotenv_if_present() -> bool:
    env_file = Path(".env")
    if not env_file.is_file():
        return False
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    load_dotenv(env_file, override=False)
    return True


def render(report: Report, env_loaded: bool) -> None:
    print("douyin-huohua-keeper 自检")
    print("=" * 68)
    if env_loaded:
        print("已加载 .env")
    else:
        print("未找到 .env（只检查当前环境变量）")
    print()

    for check in report.checks:
        color = COLORS[check.status]
        print(f"  {color}{ICONS[check.status]}{RESET}  {check.name:<14} {check.detail}")
        if check.hint:
            print(f"        {check.name:<14} \033[2m-> {check.hint}{RESET}")

    print()
    print("=" * 68)
    total = len(report.checks)
    if report.failed:
        print(f"{COLORS['error']}{len(report.failed)} 项必须修复 / {total} 项检查{RESET}")
    elif report.warned:
        print(f"{COLORS['warn']}全部通过，{len(report.warned)} 项提醒 / {total} 项检查{RESET}")
    else:
        print(f"{COLORS['ok']}全部通过 / {total} 项检查{RESET}")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="douyin-huohua-keeper 环境自检")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是彩色文本")
    parser.add_argument("--quiet", action="store_true", help="只输出结论")
    args = parser.parse_args(list(argv) if argv is not None else None)

    env_loaded = load_dotenv_if_present()

    report = Report()
    for check in CHECKS:
        try:
            check(report)
        except Exception as exc:  # noqa: BLE001
            report.add(check.__name__, "error", f"检查过程异常：{type(exc).__name__}: {exc}")

    if args.json:
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "env_loaded": env_loaded,
            "failed": len(report.failed),
            "warned": len(report.warned),
            "checks": [asdict(c) for c in report.checks],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.quiet:
        if report.failed:
            print(f"{len(report.failed)} 项必须修复")
        elif report.warned:
            print(f"通过（{len(report.warned)} 项提醒）")
        else:
            print("通过")
    else:
        render(report, env_loaded)

    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
