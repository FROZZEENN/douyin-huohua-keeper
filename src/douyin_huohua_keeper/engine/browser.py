"""浏览器生命周期管理。

Playwright 的 API 是「同步但要求同一线程内使用」，而 FastAPI 是异步的。
这个矛盾很容易踩坑：在工作台里直接开同步浏览器，请求会阻塞整个事件循环，
表现为「点了扫码登录之后整个页面卡住」。

处理办法是把浏览器操作全部放到一个**专用线程**里，用队列传递命令。
这样：
- 浏览器始终在同一个线程里创建和使用（满足 Playwright 的要求）
- 事件循环不被阻塞（工作台还能响应其他请求）
- 同一时刻只有一个操作在跑（天然串行化，不需要额外加锁）

这个模块只负责「有一个能用的浏览器」。发消息的逻辑在 navigator / composer 里。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, TypeVar

from ..config import BrowserSettings

LOGGER = logging.getLogger(__name__)

T = TypeVar("T")

# 抖音的登录页与会话页


class BrowserError(RuntimeError):
    """浏览器相关操作失败。"""


class BrowserNotStartedError(BrowserError):
    """还没有启动浏览器就要求执行操作。"""


@dataclass(frozen=True, slots=True)
class BrowserContext:
    """一次浏览器会话的运行环境。

    把 Playwright 的四个对象打包在一起，调用方不需要关心它们的嵌套关系。
    """

    playwright: Any
    browser: Any
    context: Any
    page: Any

    def close(self) -> None:
        """按创建顺序的逆序关闭。任何一步失败都不该阻断后面的清理。"""
        for name, closer in (
            ("page", lambda: self.page.close()),
            ("context", lambda: self.context.close()),
            ("browser", lambda: self.browser.close()),
            ("playwright", lambda: self.playwright.stop()),
        ):
            try:
                closer()
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("关闭 %s 失败（忽略）：%s", name, exc)


class BrowserSession:
    """把浏览器约束在一个专用线程里。

    典型用法::

        session = BrowserSession(settings.browser)
        session.start()
        try:
            page = session.call(lambda ctx: ctx.page.title())
        finally:
            session.stop()

    或者用上下文管理器::

        with BrowserSession(settings.browser) as session:
            session.call(...)
    """

    def __init__(self, settings: BrowserSettings) -> None:
        self.settings = settings
        self._thread: threading.Thread | None = None
        self._context: BrowserContext | None = None
        self._error: BaseException | None = None
        self._started = threading.Event()
        self._stop_requested = threading.Event()
        # 正在排队/执行的调用数。外部（比如工作台的空闲回收）靠它判断
        # 「现在能不能安全地 stop」—— 操作进行中 stop 会打断那次调用。
        self._pending = 0

    @property
    def busy(self) -> bool:
        """是否有浏览器调用正在排队或执行中。"""
        return self._pending > 0

    # --- 生命周期 -----------------------------------------------------------

    def start(self) -> BrowserContext:
        """在新线程里启动浏览器，并等待它就绪。

        启动失败会把异常原样抛出来 —— 不要吞掉，因为最常见的失败原因是
        「Chromium 没装」，而那个错误信息（"Executable doesn't exist"）
        对用户来说是唯一有用的线索。
        """
        if self._thread is not None and self._thread.is_alive():
            if self._context is None:
                raise BrowserError("浏览器正在启动中")
            return self._context

        self._started.clear()
        self._stop_requested.clear()
        self._error = None

        self._thread = threading.Thread(
            target=self._run,
            name="huohua-browser",
            daemon=True,  # 主进程退出时不该被浏览器拖住
        )
        self._thread.start()

        # 等待启动完成。超时给足，因为首次启动 Chromium 可能要几秒
        if not self._started.wait(timeout=self.settings.browser_timeout_ms / 1000 + 10):
            raise BrowserError(
                f"浏览器启动超时（{self.settings.browser_timeout_ms / 1000:.0f}s）。"
                "内存不足或 Chromium 未正确安装都可能导致这个问题。"
            )

        if self._error is not None:
            raise self._error

        if self._context is None:
            raise BrowserError("浏览器线程结束了但没有提供上下文")

        return self._context

    def stop(self, timeout: float = 15.0) -> None:
        """请求停止并等待线程收尾。"""
        self._stop_requested.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        self._context = None

    def __enter__(self) -> BrowserSession:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # --- 调用 ---------------------------------------------------------------

    def call(self, fn: Callable[[BrowserContext], T], *, timeout: float | None = None) -> T:
        """在浏览器线程里执行 ``fn``，同步等待结果。

        ``fn`` 里可以做任何 Playwright 操作，但不该做耗时过长的网络等待 ——
        那会让工作台的其他操作排队。长操作请拆成多步。
        """
        context = self._context
        if context is None or self._thread is None or not self._thread.is_alive():
            raise BrowserNotStartedError("浏览器未启动，请先调用 start()")

        future: Future[T] = Future()

        def task() -> None:
            try:
                future.set_result(fn(context))
            except BaseException as exc:  # noqa: BLE001
                future.set_exception(exc)

        self._pending += 1
        try:
            self._queue.put(task)  # type: ignore[attr-defined]

            wait_seconds = (
                timeout if timeout is not None else self.settings.browser_timeout_ms / 1000 + 30
            )
            try:
                return future.result(timeout=wait_seconds)
            except TimeoutError as exc:
                raise BrowserError(f"浏览器操作超时（{wait_seconds:.0f}s）") from exc
        finally:
            self._pending -= 1

    # --- 内部 ---------------------------------------------------------------

    def _run(self) -> None:
        """浏览器线程的主体。

        用一个简单的任务队列串行执行请求 —— 比每次都新建线程简单，
        而且天然保证不会有并发操作打架。
        """
        import queue

        self._queue: queue.Queue[Callable[[], None] | None] = queue.Queue()

        try:
            context = self._launch()
            self._context = context
        except BaseException as exc:  # noqa: BLE001
            self._error = exc
            self._started.set()
            return

        self._started.set()

        try:
            while not self._stop_requested.is_set():
                try:
                    task = self._queue.get(timeout=0.25)
                except queue.Empty:
                    continue

                if task is None:
                    break

                try:
                    task()
                except Exception as exc:
                    LOGGER.exception("浏览器任务执行失败：%s", exc)
        finally:
            try:
                context.close()
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("关闭浏览器失败：%s", exc)

    def _launch(self) -> BrowserContext:
        from playwright.sync_api import sync_playwright

        settings = self.settings

        LOGGER.info(
            "启动浏览器：headless=%s slow_mo=%dms",
            settings.headless,
            settings.slow_mo_ms,
        )

        playwright = sync_playwright().start()

        launch_kwargs: dict[str, Any] = {
            "headless": settings.headless,
            "timeout": settings.browser_timeout_ms,
            "args": _chromium_args(),
        }
        if settings.slow_mo_ms:
            launch_kwargs["slow_mo"] = settings.slow_mo_ms
        if settings.browser_path:
            launch_kwargs["executable_path"] = str(settings.browser_path)

        try:
            browser = playwright.chromium.launch(**launch_kwargs)
        except Exception as exc:
            playwright.stop()
            raise BrowserError(_explain_launch_failure(exc)) from exc

        try:
            context = browser.new_context(**_context_kwargs())
        except Exception:
            browser.close()
            playwright.stop()
            raise

        context.set_default_timeout(settings.action_timeout_ms)
        context.set_default_navigation_timeout(settings.nav_timeout_ms)

        page = context.new_page()
        return BrowserContext(playwright=playwright, browser=browser, context=context, page=page)

    @property
    def context(self) -> BrowserContext | None:
        return self._context

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._context is not None


def _chromium_args() -> list[str]:
    """Chromium 启动参数。

    容器里跑 Chromium 有一串经典坑，这些参数都是针对它们的：
    - ``--no-sandbox``：容器内通常没有 user namespace 权限
    - ``--disable-dev-shm-usage``：``/dev/shm`` 默认只有 64MB，页面一大就崩
    - ``--disable-gpu``：没有 GPU 时各种图形后端探测会拖慢启动

    注意：这些是**兼容性**参数，不是用于隐藏自动化特征的。项目明确不做规避风控。
    """
    return [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,BackForwardCache",
    ]


def _context_kwargs() -> dict[str, Any]:
    """浏览器上下文的创建参数。

    中文用户代理和语言是刻意的：抖音页面会根据语言环境给出不同的 DOM，
    用默认的 en-US 会让选择器失效。
    """
    return {
        "viewport": {"width": 1440, "height": 900},
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "extra_http_headers": {"Accept-Language": "zh-CN,zh;q=0.9"},
    }


def _explain_launch_failure(exc: Exception) -> str:
    """把 Playwright 的原始报错翻译成能照着做的提示。

    「Executable doesn't exist」这类信息对不熟悉 Playwright 的用户没有任何指导意义。
    """
    text = str(exc)

    if "Executable doesn't exist" in text or "playwright install" in text:
        return (
            "Chromium 未安装或路径不对。\n"
            "请在项目目录执行：playwright install chromium\n"
            "如果是 Docker 部署，请确认镜像里的 PLAYWRIGHT_BROWSERS_PATH 指向正确位置。"
        )
    if "Target page, context or browser has been closed" in text:
        return "浏览器已关闭。通常是上一次操作异常退出导致的，重启服务即可。"
    if "Cannot allocate memory" in text or "Out of memory" in text:
        return (
            "内存不足，Chromium 无法启动。\n"
            "建议至少 1GB 可用内存；容器里还要确认 /dev/shm 不小于 512MB。"
        )
    if "Host system is missing dependencies" in text:
        return (
            "系统缺少 Chromium 所需的动态库。\n"
            "Debian/Ubuntu 上执行：playwright install --with-deps chromium"
        )
    if "spawn" in text.lower() and "ENOENT" in text:
        return "找不到浏览器可执行文件。检查 HUOHUA_BROWSER_PATH 是否指向了真实存在的文件。"

    return f"浏览器启动失败：{type(exc).__name__}: {text}"




