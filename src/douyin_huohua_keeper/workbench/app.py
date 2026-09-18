"""FastAPI 应用装配。

三件事：

1. **鉴权** —— 令牌（必需）+ IP 白名单（可选）。这是保护二维码和配置的底线。
2. **共享状态** —— 引擎实例、调度器、仓库，放在 ``app.state`` 里。
3. **静态资源** —— 原生前端，直接挂载目录。

关于鉴权的实现：用中间件而不是 FastAPI 的依赖注入，因为：
- ``/api/health`` 和静态资源需要豁免（Docker healthcheck 不带令牌）
- 依赖注入要写进每个路由签名，容易漏掉一个
中间件默认全拦，显式豁免，漏掉的风险低得多。
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import mimetypes
import secrets
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import Settings, load_settings
from ..store.repo import Repository

LOGGER = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# 修正静态资源的 MIME 类型。
#
# 为什么需要这个：Windows 的注册表里 .js 经常被写成 text/plain，
# 而 Python 的 mimetypes 会读注册表。结果是浏览器收到 text/plain 的 JS ——
# 严格模式（X-Content-Type-Options: nosniff）下会直接拒绝执行，
# 表现为「页面加载了但一片空白，控制台报 MIME type 错误」。
# 显式钉死这几个类型，避免依赖运行环境的注册表状态。
_MIME_OVERRIDES = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".html": "text/html",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".webmanifest": "application/manifest+json",
}

for _ext, _type in _MIME_OVERRIDES.items():
    mimetypes.add_type(_type, _ext)

# 不需要令牌的路径。加东西到这里要非常谨慎。
#
# ``/`` 必须在里面 —— 它就是前端页面本身。如果不放行，用户打开站点会看到
# 一段 401 JSON 而不是界面，而界面才是输入令牌的地方（先有鸡还是先有蛋）。
# 放行它是安全的：页面本身不含任何数据，数据都要带令牌才能拿到。
PUBLIC_PATHS: frozenset[str] = frozenset({"/", "/index.html", "/api/health"})

# 静态资源的扩展名，命中就放行（前端页面本身不含敏感数据）
STATIC_SUFFIXES = (".html", ".css", ".js", ".ico", ".png", ".svg", ".woff2", ".webmanifest")

# 前端资源后缀 —— 这些请求要显式禁缓存
FRONTEND_SUFFIXES = (".html", ".js", ".css", ".webmanifest")


class AppState:
    """应用的共享状态。

    用显式的类而不是往 ``app.state`` 上随手挂属性，好处是类型清楚、
    初始化时机明确，而且测试里可以整体替换。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.repository = Repository(settings.data_dir)

        # 浏览器引擎不是线程安全的，而且启动慢，
        # 所以懒加载 + 用锁保护，不要在这里启动
        self._engine: Any = None
        self._engine_lock = threading.Lock()
        # 共享浏览器最近一次被使用的时间（monotonic）。空闲回收靠它判断。
        self._engine_last_used: float = 0.0
        self._reaper: threading.Thread | None = None
        self._reaper_stop = threading.Event()

        self.scheduler: Any = None

        # 扫码流程的状态。同一时刻只允许一个扫码会话。
        self.qr_lock = threading.Lock()
        self.qr_session: Any = None

    # --- 引擎 ---------------------------------------------------------------

    def get_engine(self) -> Any:
        """懒加载引擎。第一次调用时会启动浏览器（几秒钟）。"""
        from ..engine import Engine

        with self._engine_lock:
            if self._engine is None:
                engine = Engine(self.settings.browser, self.settings.send)
                engine.start()
                self._engine = engine
            self._engine_last_used = time.monotonic()
            return self._engine

    def peek_engine(self) -> Any:
        """看一眼引擎，不触发启动。用于状态查询。"""
        return self._engine

    def shutdown_engine(self) -> None:
        with self._engine_lock:
            self._engine_last_used = 0.0
            if self._engine is not None:
                try:
                    self._engine.stop()
                except Exception as exc:  # noqa: BLE001
                    LOGGER.debug("关闭引擎失败：%s", exc)
                finally:
                    self._engine = None

    # --- 空闲回收 -----------------------------------------------------------

    def maybe_reap_idle_engine(self, *, now: float | None = None) -> bool:
        """共享浏览器空闲够久就关掉，把内存还给系统。关掉了返回 True。

        **为什么需要它**：工作台的共享浏览器一旦被用过（同步收件人 / 好友列表），
        就会一直留在进程里；而定时任务又会另起一个 —— 实测「两个 Chromium 同时活着」
        约 1.3 GB，在 2GB 的小机器上贴着上限跑（而且云主机通常没有 swap）。
        空闲回收让常驻内存回到几十 MB。

        判定用 ``monotonic``，不受系统时间调整影响。超时设 0 表示不回收。
        """
        timeout = self.settings.browser.engine_idle_timeout_sec
        if timeout <= 0 or self._engine is None:
            return False

        moment = time.monotonic() if now is None else now

        # 有调用正在进行中：把它当作「刚用过」，绝不能在这里 stop ——
        # 那会把正在跑的那次调用打断（同步好友列表可能要几分钟）。
        session = getattr(self._engine, "session", None)
        if session is not None and getattr(session, "busy", False):
            self._engine_last_used = moment
            return False

        if moment - self._engine_last_used < timeout:
            return False

        LOGGER.info("共享浏览器已空闲 %d 秒，关闭它以回收内存", timeout)
        self.shutdown_engine()
        return True

    def start_engine_reaper(self, *, interval_sec: float = 30.0) -> None:
        """起一个后台线程定期回收空闲的共享浏览器。"""
        if self.settings.browser.engine_idle_timeout_sec <= 0 or self._reaper is not None:
            return

        def loop() -> None:
            while not self._reaper_stop.wait(interval_sec):
                with contextlib.suppress(Exception):
                    self.maybe_reap_idle_engine()

        self._reaper = threading.Thread(target=loop, name="huohua-engine-reaper", daemon=True)
        self._reaper.start()

    def stop_engine_reaper(self) -> None:
        self._reaper_stop.set()
        self._reaper = None


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造 FastAPI 应用。"""
    settings = settings or load_settings()
    settings.ensure_dirs()

    state = AppState(settings)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # --- 启动 ---
        # 清理上次崩溃留下的临时文件和过期锁
        removed = state.repository.cleanup()
        if removed:
            LOGGER.info("清理了 %d 个残留临时文件", removed)

        # 空闲回收共享浏览器（否则它会一直占着 ~600MB）
        state.start_engine_reaper()
        idle = state.settings.browser.engine_idle_timeout_sec
        if idle > 0:
            LOGGER.info("共享浏览器空闲回收：%d 秒无操作后自动关闭", idle)

        # 启动调度器
        if state.settings.scheduler.enabled and not state.settings.scheduler.run_once:
            from ..scheduler import Scheduler, set_singleton

            scheduler = Scheduler(state.settings)
            scheduler.start()
            set_singleton(scheduler)
            state.scheduler = scheduler
            LOGGER.info("调度状态：%s", scheduler.describe_next())

        yield

        # --- 关闭 ---
        state.stop_engine_reaper()
        if state.scheduler is not None:
            state.scheduler.shutdown()
        state.shutdown_engine()
        LOGGER.info("工作台已停止")

    app = FastAPI(
        title="douyin-huohua-keeper",
        description="抖音火花自动续期工作台",
        version=__version__,
        docs_url=None,  # 关掉交互式文档 —— 它是个额外的攻击面，且自用不需要
        redoc_url=None,
        openapi_url=None,
        # 用 lifespan 而不是已废弃的 on_event —— 后者会让
        # error::DeprecationWarning 过滤直接把测试炸掉（实测）
        lifespan=lifespan,
    )
    app.state.keeper = state

    _install_auth_middleware(app, settings)
    _install_frontend_no_cache(app)
    _install_routes(app)
    _install_static(app)

    @app.exception_handler(Exception)
    async def _unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        """兜住未处理的异常。

        没有这一层，路由里抛出的异常会变成「500 但日志里一片空白」——
        用户只看到 internal server error，排障无从下手。
        这里显式记一条带堆栈的 error，并回一个能看懂的错误体。
        """
        LOGGER.exception("未处理的异常：%s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                # ⚠️ 只回一句笼统提示，**不把异常原文回给客户端** ——
                # 里面常带本机绝对路径、库内部实现细节。细节只留在服务端日志里。
                "detail": "服务内部错误",
                "hint": "这是程序自身的问题（不是你的配置）。服务端日志里已记录堆栈，可据此排查。",
            },
        )

    # 启动时的配置问题提示
    problems = settings.validate()
    for problem in problems:
        LOGGER.warning("配置问题：%s", problem)

    return app


# =============================================================================
# 鉴权
# =============================================================================

# 令牌校验失败的退避（评审 P0-3）
#
# 为什么需要：原来每次失败都立刻返回 401，没有任何代价 —— 等于允许**无限次、
# 无限速**地猜令牌。令牌是 43 位随机串，暴力猜中不现实，但这属于「默认就该有
# 的下限防护」，不该省。这里是**计数 + 限时挡**，不 sleep —— 线程不会被占住。
#
# 这三个常量写成模块级，是为了能在测试里调小（否则要真等 60 秒）。
AUTH_FAIL_WINDOW_SEC = 300.0  # 统计窗口（秒）
# 最多记录多少个客户端。**必须有上限** —— 客户端 IP 不受我们控制，
# 扫描器换着 IP 来一次就走，那些记录永远不会被再次访问（也就永远不会过期清理）。
AUTH_FAIL_MAX_TRACKED_CLIENTS = 5000
AUTH_FAIL_LIMIT = 5  # 窗口内失败几次开始挡
AUTH_LOCKOUT_SEC = 60.0  # 挡多久（秒）
_AUTH_FAILS: dict[str, list[float]] = {}


def _auth_failures(client: str) -> list[float]:
    """取该客户端窗口内的失败时刻（顺带清掉过期记录，避免字典无限变大）。"""
    now = time.monotonic()
    times = [t for t in _AUTH_FAILS.get(client, []) if now - t < AUTH_FAIL_WINDOW_SEC]
    if times:
        _AUTH_FAILS[client] = times
    else:
        _AUTH_FAILS.pop(client, None)
    return times


def _record_auth_failure(client: str) -> None:
    times = _auth_failures(client)
    times.append(time.monotonic())
    _AUTH_FAILS[client] = times
    _prune_auth_failures()


def _prune_auth_failures() -> None:
    """控制 ``_AUTH_FAILS`` 的规模。

    ``_auth_failures`` 只在**访问某个客户端时**清理该客户端的过期记录 —— 也就是说
    「来过一次再也不来」的 IP 会永远留在字典里。来源 IP 不受我们控制，所以这里再兜一层：
    条目数超上限就先清掉所有过期记录；还超就直接清空（宁可丢一点计数，也不能让内存无界）。
    """
    if len(_AUTH_FAILS) <= AUTH_FAIL_MAX_TRACKED_CLIENTS:
        return
    now = time.monotonic()
    for key, stamps in list(_AUTH_FAILS.items()):
        if not stamps or now - stamps[-1] >= AUTH_FAIL_WINDOW_SEC:
            _AUTH_FAILS.pop(key, None)
    if len(_AUTH_FAILS) > AUTH_FAIL_MAX_TRACKED_CLIENTS:
        _AUTH_FAILS.clear()


def _auth_locked(client: str) -> bool:
    """是否处于「失败过多」的冷却中。"""
    if AUTH_FAIL_LIMIT <= 0:
        # 关掉限流时不能让下面的下标变成 times[-1]（空列表会 IndexError）
        return False
    times = _auth_failures(client)
    if len(times) < AUTH_FAIL_LIMIT:
        return False
    # 从第 AUTH_FAIL_LIMIT 次失败那一刻起，挡 AUTH_LOCKOUT_SEC；过期后自动放行
    return time.monotonic() - times[AUTH_FAIL_LIMIT - 1] < AUTH_LOCKOUT_SEC


def _clear_auth_failures(client: str) -> None:
    """认证成功后清零 —— 别让偶发的手误积累成误伤。"""
    _AUTH_FAILS.pop(client, None)


def _install_auth_middleware(app: FastAPI, settings: Settings) -> None:
    token = (settings.workbench.token or "").strip()
    allowed = _parse_allowed_networks(settings.workbench.allowed_ips)

    if not token:
        # 未配令牌时中间件**不拦任何请求** —— 这是刻意的默认（本地自用开箱即用），
        # 但代价是「谁都能访问」。所以这里必须吵到无法忽视：
        # 多行、带框、带具体的修复命令。只在日志里轻轻提一句是不够的。
        LOGGER.warning(
            "\n"
            "  ============================================================\n"
            "   ⚠  未设置 HUOHUA_TOKEN —— 工作台当前无任何鉴权\n"
            "      任何能访问 %s:%d 的人都可以看到你的二维码、\n"
            "      收件人列表，并触发发送。\n"
            "\n"
            "      仅在本机使用时可以忽略；\n"
            "      一旦端口对局域网/公网开放，请立刻设置令牌：\n"
            "\n"
            "        HUOHUA_TOKEN=<至少24位随机串>\n"
            "\n"
            "      生成一个：\n"
            '        python -c "import secrets;print(secrets.token_urlsafe(32))"\n'
            "  ============================================================",
            settings.workbench.host,
            settings.workbench.port,
        )

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path

        # 静态资源与健康检查放行。
        #
        # 用 rstrip("/") 再做一次判断是为了兜住 `//` 这类写法 ——
        # 如果只精确匹配 "/"，访问 `//` 就会掉进鉴权分支，看到一段 401 JSON
        # 而不是页面。这类边界不该靠运气。
        if path in PUBLIC_PATHS or path.rstrip("/") == "" or path.endswith(STATIC_SUFFIXES):
            return await call_next(request)

        # IP 白名单
        if allowed is not None:
            client_ip = _client_ip(request)
            if not _ip_allowed(client_ip, allowed):
                LOGGER.warning("拒绝来自 %s 的访问（不在白名单内）", client_ip)
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={
                        "error": "ip_not_allowed",
                        "message": (
                            f"你的 IP（{client_ip}）不在允许列表内。\n"
                            "如果出口 IP 变了，更新 .env 里的 HUOHUA_ALLOWED_IPS。"
                        ),
                    },
                )

        # 令牌
        if token:
            client = _client_ip(request)

            # 失败太多 → 先挡一会儿（说明见 AUTH_FAIL_LIMIT 处）
            if _auth_locked(client):
                LOGGER.warning("客户端 %s 令牌校验失败过多，暂时拒绝", client)
                return JSONResponse(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    content={
                        "error": "too_many_auth_failures",
                        "message": (
                            f"令牌连续输错 {AUTH_FAIL_LIMIT} 次，已暂停 "
                            f"{int(AUTH_LOCKOUT_SEC)} 秒。\n"
                            "请确认 .env 里的 HUOHUA_TOKEN，稍后再试。"
                        ),
                    },
                    headers={"Retry-After": str(int(AUTH_LOCKOUT_SEC))},
                )

            provided = _extract_token(request)
            # compare_digest 防时序攻击。虽然这个场景下收益有限，但没成本
            if not provided or not secrets.compare_digest(provided, token):
                _record_auth_failure(client)
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={
                        "error": "unauthorized",
                        "message": "令牌不正确。请检查 .env 里的 HUOHUA_TOKEN，或重新登录工作台。",
                    },
                    headers={"WWW-Authenticate": "Bearer"},
                )
            # 成功就清零，别让偶发手误积累成误伤
            _clear_auth_failures(client)

        return await call_next(request)


def _install_frontend_no_cache(app: FastAPI) -> None:
    """给前端资源（html / js / css）加禁缓存头。

    为什么需要：前端是零构建的原生 JS，改动靠刷新生效。但浏览器会缓存
    ``app.js`` / ``style.css``，于是出现「代码明明改了，页面还是旧的」——
    用户看不到修复，很容易以为没修好。这里显式禁缓存，保证刷新一定拿到最新前端。
    """

    @app.middleware("http")
    async def _no_cache(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.rstrip("/") == "" or path.endswith(FRONTEND_SUFFIXES):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response


def _extract_token(request: Request) -> str:
    """从请求里取令牌。

    支持三种传递方式，方便不同场景：
    - ``X-Huohua-Token`` 头（前端用这个）
    - ``Authorization: Bearer xxx``（curl 和脚本用这个）
    - ``?token=xxx`` 查询参数（方便手机浏览器直接访问链接）
    """
    header = request.headers.get("X-Huohua-Token")
    if header:
        return header.strip()

    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()

    return (request.query_params.get("token") or "").strip()


def _client_ip(request: Request) -> str:
    """取客户端 IP。

    注意：**不信任** ``X-Forwarded-For``，除非明确配置了在反代之后 ——
    否则任何人都能伪造这个头绕过 IP 白名单。
    如果确实在 Nginx 后面，应该在 Nginx 层做 IP 限制（deploy/nginx/ 里有示例）。
    """
    return request.client.host if request.client else "unknown"


def _parse_allowed_networks(raw: tuple[str, ...]) -> tuple[Any, ...] | None:
    """把配置里的 IP / CIDR 解析成网络对象。

    返回 None 表示「不限制」。解析失败的项会被跳过并记日志 ——
    不能因为一条写错的 IP 就把所有访问都拒掉（那是把自己关在门外）。
    """
    if not raw:
        return None

    networks = []
    for item in raw:
        try:
            if "/" in item:
                networks.append(ipaddress.ip_network(item, strict=False))
            else:
                networks.append(ipaddress.ip_network(f"{item}/32" if ":" not in item else f"{item}/128"))
        except ValueError:
            LOGGER.error("HUOHUA_ALLOWED_IPS 里的 %r 不是合法的 IP 或 CIDR，已跳过", item)

    return tuple(networks) if networks else None


def _ip_allowed(client_ip: str, networks: tuple[Any, ...]) -> bool:
    try:
        address = ipaddress.ip_address(client_ip)
    except ValueError:
        return False

    # 本机永远放行 —— 否则 Docker healthcheck 和自检脚本会被自己拦掉
    if address.is_loopback:
        return True

    return any(address in network for network in networks)


# =============================================================================
# 路由与静态资源
# =============================================================================


def _install_routes(app: FastAPI) -> None:
    from .routes import register_routes

    register_routes(app)


# 参与「构建戳」计算的前端文件。戳 = 这些文件里最新的 mtime，
# 只在前端真的改过时才变化 —— 用来把浏览器/预览面板的旧缓存顶掉。
_BUILD_STAMP_FILES = (
    "index.html",
    "common.js",
    "app.js",
    "style.css",
    "scan.html",
)


def frontend_build_stamp() -> str:
    """前端构建戳。用于给 app.js / style.css 加 ``?v=`` 破缓存。"""
    latest = 0.0
    for name in _BUILD_STAMP_FILES:
        with contextlib.suppress(OSError):
            latest = max(latest, (STATIC_DIR / name).stat().st_mtime)
    return str(int(latest))


def _install_static(app: FastAPI) -> None:
    if not STATIC_DIR.is_dir():
        LOGGER.error("静态资源目录不存在：%s —— 前端将无法加载", STATIC_DIR)
        return

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    async def _index() -> HTMLResponse:
        """带构建戳返回首页。

        为什么不直接交给 StaticFiles 发：首页里的 ``app.js`` / ``style.css``
        需要带上 ``?v=<构建戳>``。否则浏览器（尤其是内置预览面板）会一直吃旧缓存，
        表现为「代码明明改了、页面还是老版本」——实测踩过。
        """
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__BUILD__", frontend_build_stamp()))

    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


# =============================================================================
# 启动入口
# =============================================================================


def serve(*, host: str = "0.0.0.0", port: int = 8787, settings: Settings | None = None) -> int:
    """启动 uvicorn。CLI 的默认行为。"""
    import uvicorn

    app = create_app(settings)

    LOGGER.info("工作台启动在 http://%s:%d", host, port)

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=(settings.log_level.lower() if settings else "info"),
        access_log=False,  # 自己的请求日志够了，uvicorn 的每请求一行太吵
    )
    return 0


__all__ = ["PUBLIC_PATHS", "AppState", "create_app", "serve"]
