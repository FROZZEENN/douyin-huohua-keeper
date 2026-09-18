"""命令行入口。

绝大多数情况下你会用网页工作台，但 CLI 在三种场景下不可替代：

1. **cron 触发** —— 不需要常驻进程，跑完就走
2. **容器健康检查** —— 轻量、快速、退出码明确
3. **排障** —— 不用开浏览器就能看到配置到底被解析成了什么

    python -m douyin_huohua_keeper                  # 启动服务（默认）
    python -m douyin_huohua_keeper --check          # 自检，不启动服务
    python -m douyin_huohua_keeper --check-today    # 今天是否已成功发送
    python -m douyin_huohua_keeper --run-once       # 立即跑一次然后退出
    python -m douyin_huohua_keeper --show-config    # 打印解析后的配置

退出码：
    0  成功 / 今天已成功
    1  失败
    2  参数错误
    3  今天尚未成功（配合 --check-today 用于 cron 补发）
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_NOT_DONE_TODAY = 3

BANNER = rf"""
   _                     _                       _          _   _
  | |__  _   _  ___  _  | |_ _   _  ___  _   _  | |__ _   _| |_| |_
  | '_ \| | | |/ _ \| | | __| | | |/ _ \| | | | | '_ \ | | | __| __|
  | | | | |_| | (_) | | | |_| |_| | (_) | |_| | | | | | |_| | |_| |_
  |_| |_|\__,_|\___/|_|  \__|\__, |\___/ \__,_| |_| |_|\__,_|\__|\__|
                             |___/
                                       抖音火花自动续期 · {__version__}
"""


def _load_env() -> None:
    """尽早加载 .env —— 后面所有路径判断都依赖它。"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    env_file = Path(".env")
    if env_file.is_file():
        load_dotenv(env_file, override=False)


def _configure_logging() -> None:
    """把日志接到 ``log_dir`` 下的滚动文件。

    必须在 ``apply_overrides`` 之后调用 —— 否则 ``--log-level`` 的覆盖
    不会体现在文件日志的级别上。失败不影响运行（最多是日志只打控制台）。
    """
    try:
        from .config import load_settings
        from .logging_setup import configure_logging

        path = configure_logging(load_settings(refresh=True))
        if path is not None:
            logging.getLogger(__name__).info("日志已写入 %s", path)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("初始化文件日志失败：%s", exc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="douyin-huohua-keeper",
        description="让抖音火花不再因无人值守而中断",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  %(prog)s                          启动服务（含网页工作台）\n"
            "  %(prog)s --check                  只做环境自检\n"
            "  %(prog)s --run-once               立刻发一次然后退出\n"
            "  %(prog)s --check-today            今天成功过就返回 0，否则返回 3\n"
            "  %(prog)s --show-config            打印解析后的配置\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    mode = parser.add_argument_group("运行模式")
    mode.add_argument("--check", action="store_true", help="运行环境自检后退出")
    mode.add_argument("--run-once", action="store_true", help="立即执行一次发送任务后退出")
    mode.add_argument("--check-today", action="store_true", help="检查今天是否已成功发送（供 cron 补发用）")
    mode.add_argument("--show-config", action="store_true", help="打印解析后的配置后退出")
    mode.add_argument("--no-banner", action="store_true", help="不打印启动横幅（日志里更干净）")

    runtime = parser.add_argument_group("运行时覆盖")
    runtime.add_argument("--host", help="工作台监听地址（覆盖 HUOHUA_HOST）")
    runtime.add_argument("--port", type=int, help="工作台监听端口（覆盖 HUOHUA_PORT）")
    runtime.add_argument("--headless", action="store_true", help="强制无头模式")
    runtime.add_argument("--headed", action="store_true", help="强制有头模式（本地观察用）")
    runtime.add_argument("--log-level", help="DEBUG / INFO / WARNING / ERROR")
    runtime.add_argument("--dry-run", action="store_true", help="只走流程不真正发送，用于验证链路")

    return parser


def apply_overrides(args: argparse.Namespace) -> None:
    """把命令行参数写回环境变量，让下游配置加载逻辑统一读取。"""
    if args.host:
        os.environ["HUOHUA_HOST"] = args.host
    if args.port is not None:
        os.environ["HUOHUA_PORT"] = str(args.port)
    if args.headless and args.headed:
        raise SystemExit("--headless 与 --headed 不能同时使用")
    if args.headless:
        os.environ["HUOHUA_HEADLESS"] = "true"
    if args.headed:
        os.environ["HUOHUA_HEADLESS"] = "false"
    if args.log_level:
        os.environ["HUOHUA_LOG_LEVEL"] = args.log_level
    if args.dry_run:
        os.environ["HUOHUA_DRY_RUN"] = "true"


def cmd_check(args: argparse.Namespace) -> int:
    """复用 healthcheck 脚本，避免两套自检逻辑各自漂移。"""
    script = Path(__file__).resolve().parents[2] / "scripts" / "healthcheck.py"
    if not script.is_file():
        print(
            "未找到 scripts/healthcheck.py。\n"
            "从源码运行时请确保工作目录是项目根目录；"
            "通过 pip 安装时请改用 `huohua-keeper --check`。",
            file=sys.stderr,
        )
        return EXIT_FAILURE

    import runpy

    # ⚠️ 必须临时替换 ``sys.argv``。
    #
    # healthcheck 内部用 argparse 解析参数，而它默认读的是**当前进程的 sys.argv**。
    # 我们是带 ``--check`` 进来的，于是它会把 ``--check`` 当成未知参数 →
    # 报错并以退出码 2 结束 —— **自检从来没真正跑过**（实测踩过）。
    #
    # 用 runpy（而不是手工 importlib 加载）：runpy 会把脚本按 __main__ 注册进
    # ``sys.modules``，而 healthcheck 里有 ``from __future__ import annotations``
    # 的 @dataclass —— dataclasses 需要从 sys.modules 反查模块命名空间，
    # 手工 module_from_spec 不注册的话会直接炸在 @dataclass 上（实测踩过）。
    argv_backup = sys.argv
    sys.argv = [str(script)]
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or EXIT_OK)
    finally:
        sys.argv = argv_backup
    return EXIT_OK


def cmd_show_config() -> int:
    try:
        from .config import load_settings
    except ImportError as exc:
        print(f"配置模块尚未就绪：{exc}", file=sys.stderr)
        return EXIT_FAILURE

    settings = load_settings()
    print(settings.describe())
    return EXIT_OK


def cmd_check_today() -> int:
    """今天已有成功记录 → 0；否则 → 3（让 cron 的 || 分支能接住）。"""
    try:
        from .store import today_succeeded
    except ImportError as exc:
        print(f"存储模块尚未就绪：{exc}", file=sys.stderr)
        return EXIT_FAILURE

    try:
        done = today_succeeded()
    except Exception as exc:  # noqa: BLE001
        print(f"读取运行记录失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    if done:
        print("今天已成功发送")
        return EXIT_OK
    print("今天尚未成功发送")
    return EXIT_NOT_DONE_TODAY


def cmd_run_once() -> int:
    try:
        from .scheduler import run_once
    except ImportError as exc:
        print(f"调度模块尚未就绪：{exc}", file=sys.stderr)
        return EXIT_FAILURE

    report = run_once()
    if report is None:
        print("没有启用的任务，跳过", file=sys.stderr)
        return EXIT_OK

    print(report.summary())
    return EXIT_OK if report.all_succeeded else EXIT_FAILURE


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        from .workbench import serve
    except ImportError as exc:
        print(f"工作台模块尚未就绪：{exc}", file=sys.stderr)
        return EXIT_FAILURE

    host = os.environ.get("HUOHUA_HOST", "0.0.0.0")
    port = int(os.environ.get("HUOHUA_PORT", "8787"))

    if not args.no_banner:
        print(BANNER, file=sys.stderr)
        print(f"  工作台地址  http://{host}:{port}", file=sys.stderr)
        if not os.environ.get("HUOHUA_TOKEN"):
            print("  ⚠ 未设置 HUOHUA_TOKEN，工作台无鉴权开放", file=sys.stderr)
        print(file=sys.stderr)

    return serve(host=host, port=port)


def main(argv: Sequence[str] | None = None) -> int:
    _load_env()

    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        apply_overrides(args)
    except SystemExit as exc:
        print(exc.code, file=sys.stderr)
        return EXIT_USAGE

    _configure_logging()

    if args.check:
        return cmd_check(args)
    if args.show_config:
        return cmd_show_config()
    if args.check_today:
        return cmd_check_today()
    if args.run_once:
        return cmd_run_once()
    return cmd_serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
