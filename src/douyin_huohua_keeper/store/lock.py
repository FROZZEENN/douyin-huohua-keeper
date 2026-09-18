"""文件锁 —— 保证同一时刻只有一个任务在跑。

为什么需要锁：假设定时器在 10:30 触发了一次发送，你 10:31 又好奇地在网页上
点了「立即执行」。两个任务同时操作同一个浏览器和同一份登录态文件，结果是
两次发送互相干扰，或者登录态被写坏。

实现用 ``O_CREAT | O_EXCL``：这个组合在 POSIX 和 Windows 上都是原子的
「不存在才创建」，天然就是一个锁原语，不需要引入 ``filelock`` 之类的依赖。

锁文件里写进程 ID 和启动时间，这样卡死时能看出来是谁占着。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from pathlib import Path
from types import TracebackType

LOGGER = logging.getLogger(__name__)

# 超过这个时间的锁被认为是残留（进程崩了没来得及删）。
#
# 定成 15 分钟而不是 1 小时：一次发送任务最多几分钟（浏览器启动 + 几个
# 收件人的错峰间隔），不存在正常跑满 15 分钟的情况。阈值太长意味着
# 一次崩溃会让后面 15 分钟内的触发全部被挡掉 —— 对一个「每天必须成功
# 一次」的任务，这个代价太高。真正的第一道防线是 pid 存活检查，
# 时间只是 pid 判断不适用时的兜底。
DEFAULT_STALE_SECONDS = 900.0


class LockBusyError(RuntimeError):
    """锁被占用，且不是残留锁。"""

    def __init__(self, path: Path, holder: dict | None) -> None:
        self.path = Path(path)
        self.holder = holder or {}

        who = self.holder.get("pid")
        since = self.holder.get("acquired_at")
        detail = ""
        if who:
            detail = f"当前持有者：pid={who}"
            if since:
                detail += f"，自 {since} 起"

        super().__init__(
            f"另一个任务正在运行，无法获取锁 {self.path.name}。{detail}\n"
            "如果确认没有任务在跑（比如上次异常退出），删掉这个锁文件即可。"
        )


class FileLock:
    """基于锁文件的互斥。

    用法::

        with FileLock(path) as lock:
            do_the_thing()

    也可以手动 ``acquire()`` / ``release()``。
    """

    def __init__(self, path: Path, *, stale_seconds: float = DEFAULT_STALE_SECONDS) -> None:
        self.path = Path(path)
        self.stale_seconds = stale_seconds
        self._fd: int | None = None
        self._acquired_at: float | None = None

    # --- 上下文管理 ---------------------------------------------------------

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    # --- 核心操作 -----------------------------------------------------------

    def acquire(self) -> None:
        """获取锁。已被占用时抛 :class:`LockBusyError`。

        会自动清理过期的残留锁 —— 否则一次崩溃就会导致之后再也跑不起来，
        而用户完全不知道该删哪个文件。
        """
        if self._fd is not None:
            raise RuntimeError("锁已被本对象持有，不要重复 acquire")

        self.path.parent.mkdir(parents=True, exist_ok=True)

        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if self._is_stale():
                self._force_release()
                try:
                    fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError as exc:
                    raise LockBusyError(self.path, self._read_holder()) from exc
            else:
                raise LockBusyError(self.path, self._read_holder()) from None

        payload = {
            "pid": os.getpid(),
            "acquired_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "acquired_ts": time.time(),
        }
        try:
            os.write(fd, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            os.fsync(fd)
        except OSError:
            os.close(fd)
            self.path.unlink(missing_ok=True)
            raise

        self._fd = fd
        self._acquired_at = payload["acquired_ts"]

    def release(self) -> None:
        """释放锁。重复调用是安全的。

        ⚠️ 只在「锁文件仍然是自己写的那份」时才删除它。
        否则会出现 ABA：A 的锁被判残留并被 B 夺走，A 结束时却把 B 刚建的锁
        删掉 —— 于是第三方又能拿到锁，两个任务并跑。
        """
        if self._fd is None:
            return

        with contextlib.suppress(OSError):
            os.close(self._fd)
        self._fd = None

        holder = self._read_holder()
        still_mine = holder is None or (
            self._acquired_at is not None and holder.get("acquired_ts") == self._acquired_at
        )
        if still_mine:
            with contextlib.suppress(OSError):
                self.path.unlink(missing_ok=True)
        else:
            LOGGER.debug("锁文件已易主，跳过删除：%s", self.path)

        self._acquired_at = None

    # --- 状态查询 -----------------------------------------------------------

    @property
    def held(self) -> bool:
        return self._fd is not None

    def age_seconds(self) -> float | None:
        """本对象持锁多久了。未持有时返回 None。"""
        if self._acquired_at is None:
            return None
        return time.time() - self._acquired_at

    # --- 内部 ---------------------------------------------------------------

    def _read_holder(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _is_stale(self) -> bool:
        """判断现有锁是不是残留。

        **判断顺序很关键：pid 优先，时间兜底。**

        ⚠️ 回归：以前是先判「年龄超过 stale_seconds 就直接算残留」，
        于是**一个正常但跑得久的任务（>15 分钟）会被后来者夺锁** ——
        两个发送任务并跑，正是这个锁要防的事。而模块开头写的也是
        「真正的第一道防线是 pid 存活检查，时间只是兜底」，实现和说法反了。
        """
        holder = self._read_holder()

        if holder is None:
            # 文件在但内容读不出来 —— 可能是刚创建还没写完，也可能坏了。
            # 没有 pid 可查，只能看 mtime。
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                return False
            return age > self.stale_seconds

        pid = holder.get("pid")
        if isinstance(pid, int):
            # 持有者还活着 → 不是残留，绝不夺锁（哪怕它跑很久）
            return not _pid_alive(pid)

        # 锁文件里没有 pid 信息（老版本写的）→ 退回时间判断
        ts = holder.get("acquired_ts")
        if isinstance(ts, (int, float)):
            return time.time() - ts > self.stale_seconds
        return False

    def _force_release(self) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink()


def _pid_alive(pid: int) -> bool:
    """检查进程是否还活着。

    为什么要认真实现：如果这里总是返回 True（保守），那么一次崩溃留下的
    锁会**卡住之后整整一小时**的自动发送 —— 而用户看到的只是「今天没发出去」。
    对一个「每天必须成功一次」的项目来说，这是不能接受的失败模式。

    - POSIX：``os.kill(pid, 0)`` —— 不真发信号，只做存在性检查
    - Windows：``OpenProcess`` + ``GetExitCodeProcess``。
      不能用 ``os.kill(pid, 0)`` —— Windows 上它会去调 TerminateProcess
      之类的语义，非常危险。
    """
    if pid <= 0:
        return False

    if os.name == "nt":
        return _pid_alive_windows(pid)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在但不属于当前用户 —— 它确实还活着
        return True
    except OSError:
        return False
    return True


def _pid_alive_windows(pid: int) -> bool:
    """Windows 下的进程存活检查。

    用 ctypes 直接调 kernel32：``OpenProcess`` 拿句柄，``GetExitCodeProcess``
    看是否还是 ``STILL_ACTIVE``。

    这里是**保守但正确**的方向：只有在能明确断言「进程已结束」时才返回 False。
    任何 API 调用失败（权限不足、句柄拿不到）都当作「还活着」——
    宁可等超时，也不要误删一个正在写入的进程的锁。
    """
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return True  # 拿不到 kernel32 是极异常的情况，保守处理

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        # 87 = ERROR_INVALID_PARAMETER：pid 不存在 → 进程已结束
        # 5  = ERROR_ACCESS_DENIED：进程存在但不属于我们 → 还活着
        return error != 87

    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True  # 查不出来就别乱判
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def read_lock_holder(path: Path) -> dict | None:
    """读取锁文件内容，用于展示「谁占着锁」。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
