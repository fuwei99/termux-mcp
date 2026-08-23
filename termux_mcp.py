#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Termux MCP Server —— 纯 Python 标准库, 自带 MCP 协议, 零第三方依赖
================================================================
在 Android Termux / Linux 上运行, 把"执行命令 / 读写文件 / 进程 / 系统信息 / Termux API"等能力
以 MCP (Model Context Protocol) 暴露出去, 供远程 agent 调用。

传输: SSE (与 win-pc-agent / workspace-mcp 同一模式, MCP 客户端通用)
   GET  /sse              -> 事件流 (先发 endpoint 事件, 再推消息)
   POST /messages         -> JSON-RPC 请求 (initialize / tools/list / tools/call)
   GET  /health           -> 健康检查 (免鉴权)
   POST /mcp              -> Streamable HTTP 兼容入口

鉴权: Bearer token (header Authorization: Bearer xxx 或 SSE 的 ?auth=xxx)
   token 来源: 环境变量 TERMUX_MCP_TOKEN / MCP_AUTH_TOKEN, 或命令行 --token, 默认 wei123..

用法:
   python3 termux_mcp.py                    # 默认 0.0.0.0:8996
   python3 termux_mcp.py --port 8996 --token wei123..
   export TERMUX_MCP_TOKEN=wei123.. && python3 termux_mcp.py

可选限制: 环境变量 ALLOWED_ROOTS 冒号/分号分隔可访问根目录(默认不限, 本机自用)。
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import pty
import queue
import random
import re
import select
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import traceback
import urllib.parse
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

# ============================================================
# 配置
# ============================================================

VERSION = "1.0.0"
HOST = os.environ.get("TERMUX_MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("TERMUX_MCP_PORT") or os.environ.get("MCP_PORT") or "8996")
# 默认 token 与 workspace-mcp / win-pc-agent 统一, 可用环境变量覆盖
AUTH_TOKEN = os.environ.get("TERMUX_MCP_TOKEN") or os.environ.get("MCP_AUTH_TOKEN") or "wei123.."

_raw_roots = os.environ.get("ALLOWED_ROOTS", "").replace(";", ":")
ALLOWED_ROOTS = [p.strip() for p in _raw_roots.split(":") if p.strip()]
OUTPUT_MAX = 25000            # 单次工具输出最大字符数
PROTOCOL_VERSION = "2024-11-05"

# ---------- 常驻会话配置 ----------
SESSION_IDLE_TIMEOUT = 1800   # 空闲 30 分钟回收会话
SESSION_MAX_LIFETIME = 7200   # 会话最长寿命 2 小时
SESSION_OUTPUT_MAX = 200_000  # 单次命令输出上限(哨兵模式可续读, 不直接截断)
BG_TASK_MAX_OUTPUT = 500_000  # 后台任务日志上限
BG_TASK_IDLE_RECYCLE = 3600   # 后台任务完成后日志保留 1 小时
BG_TASK_MAX_LIFETIME = 86400  # 后台任务最长寿命 24 小时

# ---------- 全权限 proot 沙箱(伪 root + /workspace 虚拟根) ----------
PREFIX = os.environ.get("PREFIX") or "/data/data/com.termux/files/usr"
HOME_DIR = os.environ.get("HOME") or "/data/data/com.termux/files/home"
WORKSPACE_ROOT = Path(os.environ.get("MCP_WORKSPACE_ROOT") or f"{HOME_DIR}/workspace")
try:
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
except OSError:
    pass
_proot_bin = shutil.which("proot") or f"{PREFIX}/bin/proot"
USE_PROOT = (os.environ.get("TERMUX_MCP_PROOT", "1").lower() not in ("0", "false", "no", "off")
             and os.path.exists(_proot_bin))
# 额外挂载: "宿主路径:容器路径,宿主路径:容器路径"
_extra_binds = [b.strip() for b in os.environ.get("TERMUX_MCP_BINDS", "").split(",") if b.strip()]


def _proot_argv(shell_bin: str, command: str, cwd: Optional[str]) -> list[str]:
    """构造 proot 命令行: --root-id 伪 root, / 可写, $HOME/workspace 映射为 /workspace。"""
    argv = [_proot_bin, "--root-id"]
    binds = [
        "/system:/system", "/vendor:/vendor", "/data:/data", "/apex:/apex",
        "/linkerconfig/ld.config.txt:/linkerconfig/ld.config.txt",
        "/storage:/storage", "/dev:/dev", "/proc:/proc",
        f"{PREFIX}:/usr", f"{PREFIX}/bin:/bin", f"{PREFIX}/etc:/etc",
        f"{PREFIX}/lib:/lib", f"{PREFIX}/share:/share",
        f"{PREFIX}/tmp:/tmp", f"{PREFIX}/var:/var",
        f"{WORKSPACE_ROOT}:/workspace",
    ] + _extra_binds
    for b in binds:
        host = b.split(":", 1)[0]
        if os.path.exists(host):
            argv += ["-b", b]
    argv += ["-r", f"{PREFIX}/..", "--cwd=" + (cwd or str(WORKSPACE_ROOT))]
    argv += [shell_bin, "-c", command]
    return argv

SESSIONS: dict[str, queue.Queue[dict]] = {}
SESSIONS_LOCK = threading.Lock()


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def clip(text: str, limit: int = OUTPUT_MAX) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [已截断, 共 {len(text)} 字符]"


def decode_bytes(b: bytes) -> str:
    """逐级尝试解码: utf-8 -> gbk -> latin-1"""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("utf-8", "replace")


# 清除 pty 产生的 ANSI 转义序列 / 回车
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[][()#%][0-9;?]*[a-zA-Z0-9]|\x1b[=>]|\r")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def check_path(raw: str) -> Path:
    p = Path(str(raw)).expanduser()
    # /workspace 及其子路径 -> 宿主真实目录(与 proot 内视角保持一致)
    s = p.as_posix()
    if s == "/workspace":
        p = WORKSPACE_ROOT
    elif s.startswith("/workspace/"):
        p = WORKSPACE_ROOT / s[len("/workspace/"):]
    elif not p.is_absolute():
        p = WORKSPACE_ROOT / p
    if ALLOWED_ROOTS:
        ok = False
        for root in ALLOWED_ROOTS:
            rp = Path(root).resolve()
            try:
                p.resolve().relative_to(rp)
                ok = True
                break
            except ValueError:
                continue
        if not ok:
            raise ValueError(f"路径不在 ALLOWED_ROOTS 内: {raw}")
    return p


# ============================================================
# 命令黑名单 —— 防呆, 不是安全边界
# ============================================================

_BLOCKED_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\s+/(?:\s|$)"), "rm -rf / 禁止"),
    (re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f\s+~(?:/|\s|$)"), "rm -rf ~ 禁止"),
    (re.compile(r"\bmkfs\b"), "mkfs 禁止"),
    (re.compile(r"\bdd\b.*\bof=/dev/"), "dd 写设备 禁止"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"), "fork bomb 禁止"),
    (re.compile(r"\bshutdown\b"), "shutdown 禁止"),
    (re.compile(r"\breboot\b"), "reboot 禁止"),
    (re.compile(r">\s*/dev/sd[a-z]"), "直接写块设备 禁止"),
]


def assert_shell_command_allowed(command: str) -> None:
    """简单静态检查, 拦 rm -rf / 之类的手滑命令。不做沙箱安全保证。"""
    for pat, reason in _BLOCKED_PATTERNS:
        if pat.search(command):
            raise ValueError(f"命令被拒绝: {reason}")


# ============================================================
# 常驻 bash 会话 (pty + 持久环形缓冲 + 游标续读 + nonce 哨兵)
# ============================================================
#
# 设计对齐 rikkahub 的 WorkspaceRepository / WorkspaceInteractiveSession:
#   1. 后台 reader 线程持续把 pty 输出灌进【持久环形缓冲】, 输出不再随调用蒸发。
#   2. cursor 是"绝对字节位置", 续读按 cursor 取增量; 缓冲裁剪时累计 dropped。
#   3. 命令超时【不杀】, 保留 pending_nonce, 之后用 action=read 接着读。
#   4. 只有 action=interrupt 才发 \x03; 发完必须续读哨兵(exit 130)清 pending,
#      否则下一条 exec 会被误判为"仍在运行"。
#   5. 有悬挂命令时拒绝新 exec, 提示改用 read / interrupt。
#   6. 有悬挂命令时【不回收】会话, 免得长任务被 idle/lifetime 判死。

SESSION_BUFFER_MAX = 4_000_000   # 每会话环形缓冲上限(字节)
SENTINEL_CARRY = 128             # 跨次读携带的尾字节数, 防哨兵被切成两半


class ShellSession:
    """一个常驻 bash 进程, 通过 pty 通信。

    命令用 base64 编码后 eval 执行, 避免引号/转义地狱;
    结尾 printf 一个 __RK_<nonce>_<exitcode>__ 哨兵, 读到即本次结束。
    pty 保留 ISIG, 所以 \\x03 是【真 Ctrl-C】(投递给整个前台进程组),
    连 `while true; do sleep 1; done` 这种 bash 自身循环也能停。
    """

    def __init__(self, proot: bool = True, cwd: str = "", session_id: str = "default"):
        self.session_id = session_id
        self.proot = proot
        self.cwd = cwd
        self.master_fd: Optional[int] = None
        self.pid: Optional[int] = None
        self.created = time.time()
        self.last_used = time.time()
        self.lock = threading.Lock()

        # ---- 持久输出缓冲 ----
        self._buf = bytearray()
        self._buf_lock = threading.Lock()
        self.dropped = 0            # 被裁掉的字节数(= buf[0] 的绝对位置)
        self.cursor = 0             # 已消费到的绝对位置
        self._carry = b""           # 上次返回过的尾巴, 仅用于哨兵跨读匹配
        self._reader: Optional[threading.Thread] = None
        self._reader_stop = threading.Event()
        self._eof = False

        # ---- 悬挂命令状态 ----
        self.pending_nonce: Optional[str] = None
        self.pending_command: Optional[str] = None
        self.pending_started: float = 0.0

        self._start()

    # ---------------- 启动 / 停止 ----------------

    def _start(self) -> None:
        shell_bin = os.environ.get("SHELL") or f"{PREFIX}/bin/bash"
        if not os.path.exists(shell_bin):
            shell_bin = shutil.which("bash") or shutil.which("sh") or "/bin/sh"

        if self.proot and os.path.exists(_proot_bin):
            inner = self.cwd or str(WORKSPACE_ROOT)
            try:
                rel = Path(inner).resolve().relative_to(WORKSPACE_ROOT.resolve())
                inner = "/workspace" + ("/" + rel.as_posix() if rel.as_posix() != "." else "")
            except (ValueError, OSError):
                pass
            # _proot_argv 末尾是 [shell, "-c", command]; 去掉后两个, 留 shell 做交互式
            argv = _proot_argv(shell_bin, "", inner)
            argv = argv[:-2]   # 丢 "-c", ""
            # 干净非交互 shell: 不读 bashrc, 不带 prompt
            argv += ["--norc", "--noprofile", "--noediting"]
        else:
            argv = [shell_bin, "--norc", "--noprofile", "--noediting"]

        master, slave = pty.openpty()
        # 在启动 shell 前直接关掉 slave 的 ECHO, 避免首条命令在 stty -echo 生效前被回显
        try:
            attrs = termios.tcgetattr(slave)
            attrs[3] = attrs[3] & ~(termios.ECHO | termios.ECHONL | termios.ECHOCTL
                                    | termios.ECHOE | termios.ECHOK | termios.IXON)
            # 保留 ISIG(让 Ctrl-C 能中断前台进程); 关 OPOST 避免 \n->\r\n 转换
            attrs[1] = attrs[1] & ~(termios.OPOST)
            termios.tcsetattr(slave, termios.TCSANOW, attrs)
        except Exception:
            pass
        # 设置 pty 窗口大小
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ,
                        struct.pack("HHHH", 40, 200, 0, 0))
        except Exception:
            pass

        pid = os.fork()
        if pid == 0:
            os.close(master)
            os.setsid()
            # 把子进程的 controlling terminal 设为 slave pty
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            if slave > 2:
                os.close(slave)
            # 设置 TERM
            env = dict(os.environ)
            env["TERM"] = "dumb"
            env["PS1"] = ""
            env["PROMPT_COMMAND"] = ""
            try:
                os.execvpe(argv[0], argv, env)
            except Exception:
                os._exit(1)
        else:
            os.close(slave)
            self.master_fd = master
            self.pid = pid

        # reader 线程必须先起来, 否则 init 的输出没人接
        self._reader_stop.clear()
        self._eof = False
        self._reader = threading.Thread(target=self._reader_loop, daemon=True,
                                        name=f"sess-reader-{self.session_id}")
        self._reader.start()

        time.sleep(0.2)
        init = (
            "stty -echo -onlcr -ixon 2>/dev/null; "
            "bind 'set enable-bracketed-paste off' 2>/dev/null; "
            "set +m; "
            "printf '__RK_READY__\\n'"
        )
        try:
            os.write(self.master_fd, (init + "\n").encode())
        except Exception:
            pass
        # 消费掉 init 输出, 把 cursor 推到 READY 之后
        ready_re = re.compile(rb"__RK_READY__")
        body, matched, _ = self._poll(ready_re, timeout=8.0, use_carry=False)
        if not matched:
            log(f"[session {self.session_id}] init ready timeout, got {len(body)} bytes")

    def close(self) -> None:
        self._reader_stop.set()
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        if self.pid is not None:
            try:
                os.killpg(os.getpgid(self.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                try:
                    os.kill(self.pid, signal.SIGTERM)
                except OSError:
                    pass
            self.pid = None
        self.pending_nonce = None
        self.pending_command = None

    def _alive(self) -> bool:
        if self.pid is None:
            return False
        try:
            os.kill(self.pid, 0)
            return True
        except OSError:
            return False

    # ---------------- 输出缓冲 ----------------

    def _reader_loop(self) -> None:
        """持续把 pty 输出灌进环形缓冲, 直到 EOF 或被关闭。"""
        while not self._reader_stop.is_set():
            fd = self.master_fd
            if fd is None:
                break
            try:
                r, _, _ = select.select([fd], [], [], 0.5)
            except (OSError, ValueError, select.error):
                break
            if not r:
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                self._eof = True
                break
            with self._buf_lock:
                self._buf += chunk
                if len(self._buf) > SESSION_BUFFER_MAX:
                    cut = len(self._buf) - SESSION_BUFFER_MAX
                    del self._buf[:cut]
                    self.dropped += cut
        self._eof = True

    def _read_since(self, cursor: int) -> tuple[bytes, int, int]:
        """取 cursor 之后的增量。返回 (数据, 新 cursor, 因裁剪跳过的字节数)。"""
        with self._buf_lock:
            base = self.dropped
            skipped = 0
            if cursor < base:
                skipped = base - cursor
                cursor = base
            idx = cursor - base
            data = bytes(self._buf[idx:])
            new_cursor = base + len(self._buf)
        return data, new_cursor, skipped

    def _poll(self, pattern: "re.Pattern[bytes]", timeout: float,
              use_carry: bool = True) -> tuple[bytes, Optional["re.Match[bytes]"], int]:
        """轮询等 pattern 出现。

        返回 (本次应输出的字节, match 或 None, 跳过字节数)。
        命中 -> cursor 推到匹配之后, 清 carry;
        超时 -> cursor 推到已读末尾, carry 留尾巴供下次跨读匹配(不重复输出)。
        """
        carry = self._carry if use_carry else b""
        prefix = len(carry)
        collected = bytearray(carry)
        cursor = self.cursor
        dropped_total = 0
        deadline = time.time() + max(0.0, timeout)

        while True:
            data, cursor, skipped = self._read_since(cursor)
            dropped_total += skipped
            if data:
                collected += data
            m = pattern.search(collected)
            if m:
                self.cursor = cursor - (len(collected) - m.end())
                self._carry = b""
                return bytes(collected[prefix:m.start()]), m, dropped_total
            if self._eof or not self._alive():
                self.cursor = cursor
                self._carry = b""
                return bytes(collected[prefix:]), None, dropped_total
            if time.time() >= deadline:
                self.cursor = cursor
                self._carry = bytes(collected[-SENTINEL_CARRY:]) if use_carry else b""
                return bytes(collected[prefix:]), None, dropped_total
            time.sleep(0.05)

    def _decode(self, raw: bytes) -> str:
        return _strip_ansi(decode_bytes(raw))

    def _result(self, raw: bytes, exit_code: Optional[int], still: bool,
                dropped: int, extra: Optional[dict] = None) -> dict:
        text = self._decode(raw)
        out: dict[str, Any] = {
            "session_id": self.session_id,
            "exit_code": exit_code if exit_code is not None else -1,
            "stdout": clip(text, SESSION_OUTPUT_MAX),
            "stderr": "",
            "timed_out": still,
            "still_running": still,
            "cursor": self.cursor,
        }
        if dropped:
            out["dropped_bytes"] = dropped
            out["warning"] = f"缓冲溢出丢弃 {dropped} 字节(输出太快/太多), 长任务请用 action=bg_start"
        if still:
            out["pending_command"] = self.pending_command
            out["elapsed_sec"] = round(time.time() - self.pending_started, 1)
            out["hint"] = ("命令仍在运行(没被杀)。用 action=read 续读, "
                           "action=interrupt 中断, action=close 强杀。")
        return out

    # ---------------- 动作 ----------------

    def _respawn(self) -> None:
        """会话 shell 死了(用户 exit / 进程崩) -> 原地重开一个干净的。"""
        self.close()
        with self._buf_lock:
            self._buf = bytearray()
            self.dropped = 0
        self.cursor = 0
        self._carry = b""
        self.pending_nonce = None
        self.pending_command = None
        self.created = time.time()
        self._start()

    def exec_command(self, command: str, cwd: str = "", timeout: float = 120.0) -> dict:
        """在会话内执行一条命令, 等哨兵直到 timeout。超时不杀命令。"""
        respawned = False
        if self.master_fd is None or not self._alive() or self._eof:
            self._respawn()
            respawned = True

        with self.lock:
            if self.pending_nonce is not None:
                return {
                    "session_id": self.session_id,
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": (f"会话 {self.session_id} 还有命令在跑"
                               + (f" (`{self.pending_command}`)" if self.pending_command else "")
                               + "。用 action=read 续读, action=interrupt 中断, action=close 强杀。"),
                    "timed_out": False,
                    "still_running": True,
                    "pending_command": self.pending_command,
                    "elapsed_sec": round(time.time() - self.pending_started, 1),
                }

            self.last_used = time.time()
            nonce = f"{int(time.time()*1000):x}{random.randint(0,0xffff):04x}"

            parts = []
            if cwd:
                cwd_p = str(check_path(cwd))
                parts.append(f"cd {shlex.quote(cwd_p)} 2>/dev/null || true")
            b64 = base64.b64encode(command.encode()).decode()
            parts.append(f'eval "$(echo {b64} | base64 -d)"')
            parts.append('__rk=$?')
            parts.append(f'printf "\\n__RK_{nonce}_%d__\\n" "$__rk"')
            wrapped = "; ".join(parts)

            self.pending_nonce = nonce
            self.pending_command = command[:120]
            self.pending_started = time.time()
            self._carry = b""

            try:
                os.write(self.master_fd, (wrapped + "\n").encode())
            except OSError as e:
                self.pending_nonce = None
                self.pending_command = None
                return {"session_id": self.session_id, "exit_code": -1, "stdout": "",
                        "stderr": f"会话写入失败: {e}", "timed_out": False,
                        "still_running": False}

            res = self._await_sentinel(timeout)
            if respawned:
                res["note"] = "会话之前已退出, 已自动重开(cd/变量等状态已丢失)"
            return res

    def _await_sentinel(self, timeout: float) -> dict:
        """等当前悬挂命令的哨兵(调用方持锁)。"""
        nonce = self.pending_nonce
        if nonce is None:
            data, cursor, skipped = self._read_since(self.cursor)
            self.cursor = cursor
            return self._result(data, None, False, skipped)

        end_re = re.compile(rb"__RK_" + re.escape(nonce.encode()) + rb"_(-?\d+)__")
        body, match, dropped = self._poll(end_re, timeout=timeout)
        self.last_used = time.time()

        if match is not None:
            self.pending_nonce = None
            self.pending_command = None
            exit_code = int(match.group(1))
            return self._result(body, exit_code, False, dropped)

        if self._eof or not self._alive():
            self.pending_nonce = None
            self.pending_command = None
            res = self._result(body, None, False, dropped)
            res["stderr"] = ("会话 shell 已退出(命令里有 exit? 或进程被杀)。"
                             "下次 exec 会自动重开新会话, 但 cd/变量状态会丢失。")
            res["session_dead"] = True
            return res

        return self._result(body, None, True, dropped)

    def read_output(self, timeout: float = 30.0) -> dict:
        """续读: 有悬挂命令就等哨兵, 没有就只取增量(读 REPL 回显等)。"""
        with self.lock:
            self.last_used = time.time()
            return self._await_sentinel(timeout)

    def write_stdin(self, data: str) -> dict:
        """裸写 stdin: 喂 REPL、答 y/n、输密码。不加换行, 自己带 \\n。"""
        if self.master_fd is None:
            return {"ok": False, "error": "会话未启动"}
        try:
            os.write(self.master_fd, data.encode())
            self.last_used = time.time()
            return {"ok": True, "session_id": self.session_id,
                    "msg": f"已写入 {len(data)} 字符, 用 action=read 看回显"}
        except OSError as e:
            return {"ok": False, "error": str(e)}

    def interrupt(self) -> dict:
        """发送真 Ctrl-C 中断前台命令, 并续读哨兵清掉 pending。"""
        if self.master_fd is None:
            return {"ok": False, "error": "会话未启动"}
        try:
            os.write(self.master_fd, b"\x03")
        except OSError as e:
            return {"ok": False, "error": str(e)}
        # 给内核投递信号 + bash 收拾现场的时间
        time.sleep(0.25)
        with self.lock:
            self.last_used = time.time()
            if self.pending_nonce is None:
                return {"ok": True, "session_id": self.session_id,
                        "msg": "已发送 Ctrl-C(当时无悬挂命令)"}
            # 命令被 SIGINT 杀死后, 非交互 bash 会【放弃整条命令列表】,
            # 连尾巴上的 printf 哨兵一起丢掉 -> pending 永远清不掉。
            # 对策: 补发一条探针哨兵。bash 只有在前台命令真结束后才会读下一行 stdin,
            # 所以探针能回来 == 命令确实死了; 回不来 == 命令还在跑。
            res = self._await_sentinel(1.5)
            if self.pending_nonce is not None:
                try:
                    os.write(self.master_fd,
                             f'printf "\\n__RK_{self.pending_nonce}_130__\\n"\n'.encode())
                except OSError:
                    pass
                res2 = self._await_sentinel(2.0)
                # 探针回来了: 把两段输出拼上, 别丢中断前的尾巴
                if res.get("stdout") and res2.get("stdout"):
                    res2["stdout"] = res["stdout"] + res2["stdout"]
                elif res.get("stdout"):
                    res2["stdout"] = res["stdout"]
                res = res2
            if self.pending_nonce is None:
                res["msg"] = "interrupted (SIGINT via pty)"
                res["exit_code"] = 130
            else:
                res["msg"] = "已发 SIGINT 但命令仍在跑, 用 action=close 强杀"
            res["ok"] = True
            return res

    def is_idle(self) -> bool:
        # 有命令在跑就不算空闲, 别把长任务给回收了
        if self.pending_nonce is not None:
            return False
        return time.time() - self.last_used > SESSION_IDLE_TIMEOUT

    def is_expired(self) -> bool:
        if self.pending_nonce is not None:
            return False
        return time.time() - self.created > SESSION_MAX_LIFETIME

    def info(self) -> dict:
        return {
            "session_id": self.session_id,
            "alive": self._alive(),
            "pid": self.pid,
            "proot": self.proot,
            "uses_pty": True,
            "command_running": self.pending_nonce is not None,
            "pending_command": self.pending_command,
            "elapsed_sec": (round(time.time() - self.pending_started, 1)
                            if self.pending_nonce else None),
            "cursor": self.cursor,
            "buffered_bytes": len(self._buf),
            "dropped_bytes": self.dropped,
            "idle_sec": round(time.time() - self.last_used, 1),
            "age_sec": round(time.time() - self.created, 1),
        }


# 全局会话池: key = session_id
_SESSIONS: dict[str, ShellSession] = {}
_SESSIONS_LOCK = threading.Lock()
# 后台任务注册表
_BG_TASKS: dict[str, dict] = {}
_BG_TASKS_LOCK = threading.Lock()
BG_LOG_DIR = WORKSPACE_ROOT / ".mcp-bg-logs"
try:
    BG_LOG_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    BG_LOG_DIR = Path("/tmp")


def _get_session(session_id: str = "default", proot: bool = True,
                 cwd: str = "") -> ShellSession:
    with _SESSIONS_LOCK:
        sess = _SESSIONS.get(session_id)
        if sess is None or not sess._alive():
            if sess:
                sess.close()
            sess = ShellSession(proot=proot, cwd=cwd, session_id=session_id)
            _SESSIONS[session_id] = sess
        return sess


def _reap_sessions() -> None:
    """回收空闲/过期会话(调用方持锁)。有悬挂命令的会话不回收。"""
    dead = [k for k, s in _SESSIONS.items()
            if s.is_idle() or s.is_expired() or not s._alive()]
    for k in dead:
        _SESSIONS[k].close()
        del _SESSIONS[k]


def session_cleanup_thread() -> None:
    while True:
        time.sleep(120)
        try:
            with _SESSIONS_LOCK:
                _reap_sessions()
        except Exception:
            pass
# ============================================================
# 后台任务
# ============================================================

def _run_shell(command: str, cwd: str = "", timeout: int = 120,
               proot: Optional[bool] = None) -> dict:
    shell_bin = os.environ.get("SHELL") or f"{PREFIX}/bin/bash"
    if not os.path.exists(shell_bin):
        shell_bin = shutil.which("bash") or shutil.which("sh") or "/bin/sh"

    cwd_p = cwd or None
    if cwd_p:
        cwd_p = str(check_path(cwd_p))

    want_proot = USE_PROOT if proot is None else bool(proot)
    if want_proot and os.path.exists(_proot_bin):
        # proot 内部用容器视角的 cwd: 宿主 workspace 路径反向映射回 /workspace
        inner = cwd_p or str(WORKSPACE_ROOT)
        try:
            rel = Path(inner).resolve().relative_to(WORKSPACE_ROOT.resolve())
            inner = "/workspace" + ("/" + rel.as_posix() if rel.as_posix() != "." else "")
        except (ValueError, OSError):
            pass
        argv = _proot_argv(shell_bin, command, inner)
        popen_cwd = None
    else:
        argv = [shell_bin, "-c", command]
        popen_cwd = cwd_p

    try:
        proc = subprocess.Popen(
            argv,
            cwd=popen_cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except Exception as e:
        return {"exit_code": -1, "stdout": "", "stderr": f"启动失败: {e}", "timed_out": False}
    
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        out, err = proc.communicate()
    
    return {
        "exit_code": proc.returncode,
        "stdout": clip(decode_bytes(out or b"")),
        "stderr": clip(decode_bytes(err or b"")),
        "timed_out": timed_out,
    }


def tool_shell(command: str, cwd: str = "", timeout: int = 120,
               proot: Optional[bool] = None) -> dict:
    """一次性执行 bash/sh 命令(subprocess, 跑完即销)。适合无状态命令。"""
    assert_shell_command_allowed(command)
    return _run_shell(command, cwd, timeout, proot)


def _bg_reap() -> None:
    """清理已结束且超过保留期的后台任务(调用方持锁)。"""
    now = time.time()
    for tid, t in list(_BG_TASKS.items()):
        proc = t.get("proc")
        done = proc is None or proc.poll() is not None
        if done and t.get("ended") and now - t["ended"] > BG_TASK_IDLE_RECYCLE:
            _BG_TASKS.pop(tid, None)
        elif not done and now - t["started"] > BG_TASK_MAX_LIFETIME:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass


def _bg_status(t: dict) -> dict:
    proc = t.get("proc")
    rc = proc.poll() if proc is not None else None
    if rc is not None and not t.get("ended"):
        t["ended"] = time.time()
    log_path = Path(t["log"])
    return {
        "task_id": t["tid"],
        "command": t["command"],
        "running": rc is None,
        "exit_code": rc,
        "pid": proc.pid if proc is not None else None,
        "log": str(log_path),
        "log_bytes": log_path.stat().st_size if log_path.exists() else 0,
        "started_at": datetime.fromtimestamp(t["started"]).strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round((t.get("ended") or time.time()) - t["started"], 1),
    }


def _bg_start(command: str, cwd: str = "", proot: Optional[bool] = None) -> dict:
    """detach 一个后台任务, 输出落盘到日志文件。真正的长任务走这条路。"""
    assert_shell_command_allowed(command)
    with _BG_TASKS_LOCK:
        _bg_reap()
        running = [t for t in _BG_TASKS.values()
                   if t.get("proc") is not None and t["proc"].poll() is None]
        if len(running) >= 8:
            return {"ok": False, "error": f"后台任务已达上限 8, 先 bg_kill 或等结束"}

    shell_bin = os.environ.get("SHELL") or f"{PREFIX}/bin/bash"
    if not os.path.exists(shell_bin):
        shell_bin = shutil.which("bash") or shutil.which("sh") or "/bin/sh"

    cwd_p = str(check_path(cwd)) if cwd else None
    want_proot = USE_PROOT if proot is None else bool(proot)
    if want_proot and os.path.exists(_proot_bin):
        inner = cwd_p or str(WORKSPACE_ROOT)
        try:
            rel = Path(inner).resolve().relative_to(WORKSPACE_ROOT.resolve())
            inner = "/workspace" + ("/" + rel.as_posix() if rel.as_posix() != "." else "")
        except (ValueError, OSError):
            pass
        argv = _proot_argv(shell_bin, command, inner)
        popen_cwd = None
    else:
        argv = [shell_bin, "-c", command]
        popen_cwd = cwd_p

    tid = f"bg-{uuid.uuid4().hex[:8]}"
    log_path = BG_LOG_DIR / f"{tid}.log"
    try:
        fh = open(log_path, "wb")
        proc = subprocess.Popen(argv, cwd=popen_cwd, stdin=subprocess.DEVNULL,
                                stdout=fh, stderr=subprocess.STDOUT,
                                start_new_session=True)
    except Exception as e:
        return {"ok": False, "error": f"启动失败: {e}"}

    t = {"tid": tid, "proc": proc, "command": command[:200],
         "log": str(log_path), "started": time.time(), "ended": None, "fh": fh}
    with _BG_TASKS_LOCK:
        _BG_TASKS[tid] = t
    res = _bg_status(t)
    res["ok"] = True
    res["hint"] = f"用 action=bg_read + task_id={tid} 看输出, action=bg_kill 杀掉"
    return res


def _bg_read(task_id: str, offset: int = 0) -> dict:
    with _BG_TASKS_LOCK:
        t = _BG_TASKS.get(task_id)
    if t is None:
        return {"ok": False, "error": f"无此任务: {task_id}", "tasks": _bg_list()["tasks"]}
    res = _bg_status(t)
    log_path = Path(t["log"])
    text = ""
    if log_path.exists():
        try:
            with open(log_path, "rb") as f:
                f.seek(max(0, offset))
                text = _strip_ansi(decode_bytes(f.read()))
        except OSError as e:
            res["error"] = str(e)
    res["ok"] = True
    res["offset"] = offset
    res["stdout"] = clip(text, SESSION_OUTPUT_MAX)
    res["next_offset"] = res["log_bytes"]
    return res


def _bg_kill(task_id: str) -> dict:
    with _BG_TASKS_LOCK:
        t = _BG_TASKS.get(task_id)
    if t is None:
        return {"ok": False, "error": f"无此任务: {task_id}"}
    proc = t.get("proc")
    if proc is None or proc.poll() is not None:
        return {"ok": True, "msg": "任务已结束", **_bg_status(t)}
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        time.sleep(0.3)
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception as e:
        return {"ok": False, "error": f"杀进程失败: {e}"}
    t["ended"] = time.time()
    return {"ok": True, "msg": "已终止", **_bg_status(t)}


def _bg_list() -> dict:
    with _BG_TASKS_LOCK:
        _bg_reap()
        return {"ok": True, "tasks": [_bg_status(t) for t in _BG_TASKS.values()]}


def tool_shell_session(command: str = "", cwd: str = "", timeout: int = 120,
                       proot: Optional[bool] = None,
                       session_id: str = "default",
                       action: str = "",
                       data: str = "",
                       task_id: str = "",
                       offset: int = 0,
                       output_offset: int = 0,
                       interrupt: bool = False) -> dict:
    """常驻 pty bash 会话 + 后台任务。cd/export/变量在同 session_id 间持久保持。

    命令用 base64+nonce 哨兵协议包裹执行, 读到哨兵即返回。
    **超时不杀命令**: 返回 still_running=true, 之后用 action=read 续读。
    真正的长任务(几分钟以上)用 action=bg_start, 输出落盘, 随时 bg_read。
    """
    want_proot = USE_PROOT if proot is None else bool(proot)
    act = (action or "").strip().lower()

    # 兼容老调用: interrupt=true 等价 action=interrupt
    if interrupt and not act:
        act = "interrupt"
    if not act:
        act = "exec" if command else "read"

    try:
        # ---- 后台任务(不依赖会话) ----
        if act in ("bg_start", "start"):
            return _bg_start(command, cwd=cwd, proot=proot)
        if act in ("bg_read", "task_read"):
            return _bg_read(task_id, offset=offset or output_offset)
        if act in ("bg_kill", "kill"):
            return _bg_kill(task_id)
        if act in ("bg_list", "tasks"):
            return _bg_list()

        # ---- 会话级 ----
        if act == "list":
            with _SESSIONS_LOCK:
                return {"ok": True, "sessions": [s.info() for s in _SESSIONS.values()],
                        "background_tasks": _bg_list()["tasks"]}
        if act == "close":
            with _SESSIONS_LOCK:
                sess = _SESSIONS.pop(session_id, None)
            if sess is None:
                return {"ok": False, "error": f"无此会话: {session_id}"}
            sess.close()
            return {"ok": True, "msg": f"会话 {session_id} 已关闭(命令被强杀)"}

        sess = _get_session(session_id, proot=want_proot, cwd=cwd)

        if act == "info":
            return {"ok": True, **sess.info()}
        if act == "interrupt":
            return sess.interrupt()
        if act == "read":
            return sess.read_output(timeout=min(max(timeout, 1), 600))
        if act == "write":
            if not data:
                return {"ok": False, "error": "action=write 需要 data 参数(记得带 \\n)"}
            return sess.write_stdin(data)
        if act == "exec":
            if not command:
                return {"ok": False, "error": "action=exec 需要 command 参数"}
            assert_shell_command_allowed(command)
            return sess.exec_command(command, cwd=cwd,
                                     timeout=min(max(timeout, 1), 600))

        return {"ok": False, "error": f"未知 action: {action}",
                "valid": ["exec", "read", "write", "interrupt", "close", "info",
                          "list", "bg_start", "bg_read", "bg_kill", "bg_list"]}
    except Exception as e:
        return {"exit_code": -1, "stdout": "", "stderr": f"会话错误: {e}",
                "timed_out": False, "still_running": False,
                "hint": "可改用 shell 工具(一次性)"}


def tool_read_file(path: str = "", offset: int = 0, limit: int = 65536,
                   paths: Optional[list] = None, start_line: int = 0,
                   line_count: int = 0, max_chars: int = 0, **_kw) -> dict:
    """读取文本文件。支持 offset/limit(字节) 或 start_line/line_count(行)，可批量 paths。"""
    targets = [t for t in ([path] if path else []) + list(paths or []) if t]
    if not targets:
        raise ValueError("需要 path 或 paths")
    hard = OUTPUT_MAX * 2
    cap = min(max_chars, hard) if max_chars else hard
    per = max(400, cap // len(targets)) if len(targets) > 1 else cap
    items: list[dict] = []
    for t in targets:
        p = check_path(t)
        item: dict[str, Any] = {"path": str(p)}
        if not p.is_file():
            item["error"] = "文件不存在"
            items.append(item)
            continue
        if start_line or line_count:
            lc = max(1, min(line_count or 400, 2000))
            s = max(1, start_line or 1)
            out: list[str] = []
            with open(p, "rb") as f:
                for n, raw in enumerate(f, 1):
                    if n < s:
                        continue
                    if len(out) >= lc:
                        break
                    out.append(f"{n}\t{decode_bytes(raw).rstrip(chr(10))}")
            item.update(start_line=s, lines=len(out),
                        text=clip("\n".join(out), per))
        else:
            with open(p, "rb") as f:
                f.seek(max(0, offset))
                data = f.read(max(1, min(limit, 4 * 1024 * 1024)))
            item.update(offset=offset, bytes=len(data),
                        text=clip(decode_bytes(data), per))
        items.append(item)
    if len(items) == 1:
        return items[0]
    return {"count": len(items), "files": items}


def tool_write_file(path: str, text: str, overwrite: bool = True) -> dict:
    """写入文本文件(utf-8)。自动创建父目录。覆盖前自动备份原文件。"""
    p = check_path(path)
    if p.exists() and not overwrite:
        raise ValueError(f"文件已存在且 overwrite=False: {path}")
    backup_id = _do_backup([p], "write_file") if p.exists() else None
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    result: dict[str, Any] = {"path": str(p), "bytes": p.stat().st_size}
    if backup_id:
        result["backup_id"] = backup_id
    return result


def tool_edit_file(path: str, old_text: str = "", new_text: str = "",
                   replace_all: bool = False, expected_replacements: int = 0,
                   edits: Optional[list] = None) -> dict:
    """文本精确替换。单次或批量 edits 数组按顺序应用。"""
    p = check_path(path)
    if not p.is_file():
        raise ValueError(f"文件不存在: {path}")
    raw = p.read_bytes()
    content = decode_bytes(raw)
    total = 0

    def apply_one(ot: str, nt: str, ra: bool, exp: int) -> int:
        nonlocal content
        if ra:
            n = content.count(ot)
            if n == 0:
                raise ValueError("old_text 未匹配到任何内容")
            content = content.replace(ot, nt)
        else:
            idx = content.find(ot)
            if idx == -1:
                raise ValueError(f"old_text 未匹配到任何内容: {ot[:40]!r}")
            if content.find(ot, idx + 1) != -1:
                raise ValueError(f"old_text 匹配到多处, 请用 replace_all=True 或更精确的文本: {ot[:40]!r}")
            content = content[:idx] + nt + content[idx + len(ot):]
            n = 1
        if exp and n != exp:
            raise ValueError(f"替换数量不符: 期望 {exp}, 实际 {n}")
        return n

    if edits:
        if not isinstance(edits, list) or not edits:
            raise ValueError("edits 必须是非空数组")
        for i, e in enumerate(edits):
            if not isinstance(e, dict):
                raise ValueError(f"edits[{i}] 必须是对象")
            ot = str(e.get("old_text", ""))
            nt = str(e.get("new_text", ""))
            if not ot:
                raise ValueError(f"edits[{i}].old_text 不能为空")
            total += apply_one(ot, nt, bool(e.get("replace_all", False)),
                               int(e.get("expected_replacements", 0) or 0))
    else:
        if old_text == "":
            raise ValueError("old_text 不能为空")
        total += apply_one(old_text, new_text, replace_all, expected_replacements)

    # 改动前的原文已经在内存里(p.read_bytes()), 直接落盘备份
    backup_id = _do_backup([p], "edit_file")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    result = {"path": str(p), "replacements": total, "bytes": p.stat().st_size}
    if backup_id:
        result["backup_id"] = backup_id
    return result


def tool_termux_api(command: str, args: Optional[list] = None) -> dict:
    """调用 Termux:API 命令 (例如 toast, battery-status, clipboard-get, clipboard-set, notification, vibrate, wifi-connectioninfo, volume 等)。"""
    api_cmd = f"termux-{command}" if not command.startswith("termux-") else command
    cmd_path = shutil.which(api_cmd)
    if not cmd_path:
        raise ValueError(f"Termux:API 工具 {api_cmd} 未安装。请先在 Termux 中运行: pkg install termux-api 并安装 Termux:API App。")
    
    cmd_list = [api_cmd] + [str(a) for a in (args or [])]
    try:
        proc = subprocess.run(cmd_list, capture_output=True, text=True, timeout=30)
        stdout = proc.stdout.strip()
        parsed: Any = stdout
        try:
            parsed = json.loads(stdout)
        except Exception:
            pass
        return {"command": api_cmd, "exit_code": proc.returncode, "result": parsed, "stderr": proc.stderr.strip()}
    except Exception as e:
        return {"command": api_cmd, "error": str(e)}


def tool_open_path(target: str) -> dict:
    """[已废弃] 用 shell 跑 termux-open/xdg-open 即可。保留空实现避免旧调用报错。"""
    return {"deprecated": True, "hint": "改用 shell: termux-open <target> 或 xdg-open <target>"}


# ============================================================
# grep (ripgrep 后端, 对齐 rikkahub workspace_grep)
# ============================================================

GREP_EXCLUDE_DIRS = [
    ".git", ".hg", ".svn", "node_modules", "bower_components", "vendor",
    "build", "dist", "out", "target", "bin", "obj",
    ".gradle", ".idea", ".vscode", ".cxx",
    ".cache", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".venv", "venv", "env", ".tox",
    ".next", ".nuxt", ".svelte-kit", ".terraform",
]

def _rg_exe() -> Optional[str]:
    return shutil.which("rg")


def tool_grep(query: str, path: str = ".", output_mode: str = "files_with_matches",
              glob: str = "", type: str = "", fixed_string: bool = False,
              ignore_case: bool = True, after: int = 0, before: int = 0,
              context: int = 0, head_limit: int = 250, offset: int = 0,
              multiline: bool = False, hidden: bool = False,
              no_ignore: bool = False) -> dict:
    """在 Termux/Linux 上搜索文件内容(ripgrep 后端)。支持 files_with_matches/content/count。"""
    if not query:
        raise ValueError("query 不能为空")
    mode = (output_mode or "files_with_matches").strip().lower()
    if mode not in ("files_with_matches", "content", "count"):
        raise ValueError(f"output_mode 无效: {mode}")
    hl = max(1, min(int(head_limit or 250), 2000))
    off = max(0, int(offset or 0))
    after = max(0, int(after or 0))
    before = max(0, int(before or 0))
    ctx = max(0, int(context or 0))
    if ctx:
        before = max(before, ctx)
        after = max(after, ctx)
    root = check_path(path)
    if not root.exists():
        return {"mode": mode, "backend": "none", "pathMissing": True, "stderr": "", "exitCode": 0}
    
    rg = _rg_exe()
    if not rg:
        # 降级返回提示
        return {"mode": mode, "backend": "fallback", "error": "请在 Termux 运行 pkg install ripgrep 以启用极速 grep"}
    
    args = [rg, "--color=never", "--no-messages"]
    if off > 0 or hl <= 250:
        args.append("--sort=path")
    if mode == "files_with_matches":
        args.append("--files-with-matches")
    elif mode == "count":
        args += ["--count", "--with-filename"]
    else:
        args += ["--no-heading", "--line-number", "--with-filename", "--null"]
        if after > 0:
            args += ["--after-context", str(after)]
        if before > 0:
            args += ["--before-context", str(before)]
    if ignore_case:
        args.append("--ignore-case")
    if fixed_string:
        args.append("--fixed-strings")
    if multiline:
        args += ["--multiline", "--multiline-dotall"]
    if hidden:
        args.append("--hidden")
    if no_ignore:
        args.append("--no-ignore")
    for d in GREP_EXCLUDE_DIRS:
        args += ["--glob", f"!{d}/"]
    if glob:
        args += ["--glob", glob]
    if type:
        args += ["--type", type]
    args += ["--regexp", query, "--", str(root)]
    
    try:
        proc = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        raise ValueError("grep 超时(120s), 请缩小搜索范围")
        
    stdout = proc.stdout or ""
    payload = [ln for ln in stdout.split("\n") if ln.strip()]
    per = 1 if mode != "content" else 1 + before + after
    budget = min(20000, (off + hl + 1) * per)
    hit_budget = len(payload) >= budget
    skip, take = off, hl
    stderr = (proc.stderr or "").strip()
    
    if mode == "files_with_matches":
        all_files = [ln for ln in payload]
        return {"mode": mode, "backend": "rg", "files": all_files[skip:skip + take],
                "totalReturned": len(all_files),
                "truncated": hit_budget or len(all_files) > skip + take,
                "stderr": stderr, "exitCode": proc.returncode}
    if mode == "count":
        counts = []
        for ln in payload:
            idx = ln.rfind(":")
            if idx <= 0 or not ln[idx + 1:].strip().isdigit():
                continue
            counts.append({"path": ln[:idx], "count": int(ln[idx + 1:].strip())})
        counts = [c for c in counts if c["count"] > 0]
        return {"mode": mode, "backend": "rg", "counts": counts[skip:skip + take],
                "totalReturned": len(counts),
                "truncated": hit_budget or len(counts) > skip + take,
                "stderr": stderr, "exitCode": proc.returncode}
    
    matches = []
    for ln in payload:
        nul = ln.find("\x00")
        if nul < 0:
            continue
        rest = ln[nul + 1:]
        sep = -1
        for i, ch in enumerate(rest):
            if ch in (":", "-"):
                sep = i
                break
        if sep <= 0 or not rest[:sep].strip().isdigit():
            continue
        matches.append({"path": ln[:nul], "line": int(rest[:sep].strip()),
                        "text": rest[sep + 1:], "isContext": rest[sep] == "-"})
    return {"mode": mode, "backend": "rg", "matches": matches[skip:skip + take],
            "totalReturned": len(matches),
            "truncated": hit_budget or len(matches) > skip + take,
            "stderr": stderr, "exitCode": proc.returncode}


# ============================================================
# 统一备份系统 (对齐 RikkaHub workspace_backup 格式)
#   <root>/<backupId>/manifest.json + files/<i>.txt
#   write_file / edit_file / codex_patch 改动前自动留底, 可用 backup 后悔。
# ============================================================

BACKUP_ROOT = Path(os.environ.get(
    "TERMUX_MCP_BACKUP_ROOT",
    str(WORKSPACE_ROOT / ".rikkahub" / "backups")))
BACKUP_MAX = 200  # 最多保留备份份数


def _new_backup_id() -> str:
    return f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:4]}"


def _do_backup(paths: "list[Path]", reason: str) -> Optional[str]:
    """把给定文件(已存在的)拷进一个备份目录, 写 manifest, 返回 backup_id。"""
    existing = [p for p in paths if p and p.exists() and p.is_file()]
    if not existing:
        return None
    bid = _new_backup_id()
    bdir = BACKUP_ROOT / bid
    fdir = bdir / "files"
    fdir.mkdir(parents=True, exist_ok=True)
    entries = []
    for i, p in enumerate(existing):
        try:
            rp = p.resolve()
        except OSError:
            rp = p
        dst = fdir / f"{i}.txt"
        try:
            shutil.copy2(str(rp), str(dst))
            size = dst.stat().st_size
        except OSError:
            continue
        entries.append({
            "path": str(rp), "existed": True,
            "backupPath": str(dst), "sizeBytes": size})
    if not entries:
        shutil.rmtree(str(bdir), ignore_errors=True)
        return None
    manifest = {
        "backupId": bid, "createdAt": int(time.time() * 1000),
        "reason": reason, "entries": entries}
    with open(bdir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    _prune_backups()
    return bid


def _prune_backups() -> None:
    """按时间保留最近 BACKUP_MAX 份。"""
    try:
        dirs = [d for d in BACKUP_ROOT.iterdir() if d.is_dir()]
    except OSError:
        return
    if len(dirs) <= BACKUP_MAX:
        return
    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    for old in dirs[BACKUP_MAX:]:
        shutil.rmtree(str(old), ignore_errors=True)


def tool_backup(action: str = "list", backup_id: str = "",
                files: "Optional[list[str]]" = None,
                limit: int = 20) -> dict:
    """备份管理: list(列出最近备份) / restore(回滚指定备份, files 可只回滚部分路径)。"""
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    if action == "restore":
        if not backup_id:
            raise ValueError("restore 需要 backup_id")
        bdir = BACKUP_ROOT / backup_id
        mf = bdir / "manifest.json"
        if not mf.is_file():
            raise ValueError(f"备份不存在: {backup_id}")
        with open(mf, encoding="utf-8") as f:
            manifest = json.load(f)
        restore_set = set(files or [])
        restored = []
        for i, e in enumerate(manifest.get("entries", [])):
            orig = e.get("path", "")
            if restore_set and orig not in restore_set:
                continue
            src = Path(e.get("backupPath") or (bdir / "files" / f"{i}.txt"))
            if not src.is_file():
                continue
            op = Path(orig)
            op.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(op))
            restored.append(orig)
        return {"ok": True, "action": "restore", "backup_id": backup_id,
                "restored": restored, "restored_count": len(restored)}
    # list
    dirs = sorted([d for d in BACKUP_ROOT.iterdir() if d.is_dir()],
                  key=lambda d: d.stat().st_mtime, reverse=True)[:max(1, limit)]
    items = []
    for d in dirs:
        mf = d / "manifest.json"
        info: dict[str, Any] = {"backup_id": d.name}
        if mf.is_file():
            try:
                with open(mf, encoding="utf-8") as f:
                    m = json.load(f)
                info["reason"] = m.get("reason", "")
                info["created_at"] = m.get("createdAt")
                info["files"] = [e.get("path") for e in m.get("entries", [])]
            except Exception:
                pass
        items.append(info)
    return {"count": len(items), "backups": items}


# ============================================================
# codex_patch (OpenAI Codex file-style patch, 纯 Python 解析)
# ============================================================

# 旧的 patch 专用备份已统一到上面的 BACKUP_ROOT


def _find_sequence(lines: list[str], seq: list[str]) -> Optional[int]:
    if not seq:
        return 0
    n, m = len(lines), len(seq)
    for i in range(n - m + 1):
        ok = True
        for j in range(m):
            if lines[i + j].rstrip("\r") != seq[j]:
                ok = False
                break
        if ok:
            return i
    return None


def _parse_codex_patch(patch: str, base: Path) -> list[dict]:
    lines = patch.split("\n")
    while lines and lines[0].strip() == "":
        lines.pop(0)
    while lines and lines[-1].strip() == "":
        lines.pop()
    if not lines or lines[0].strip() != "*** Begin Patch":
        raise ValueError("patch 必须以 *** Begin Patch 开头")
    if not lines or lines[-1].strip() != "*** End Patch":
        raise ValueError("patch 必须以 *** End Patch 结尾")
    ops: list[dict] = []
    cur: Optional[dict] = None
    i = 1
    while i < len(lines) - 1:
        ln = lines[i]
        st = ln.strip()
        if st.startswith("*** Add File: "):
            if cur:
                ops.append(cur)
            cur = {"kind": "add", "path": (base / st[len("*** Add File: "):].strip()).resolve(),
                   "lines": []}
        elif st.startswith("*** Delete File: "):
            if cur:
                ops.append(cur)
            cur = {"kind": "delete", "path": (base / st[len("*** Delete File: "):].strip()).resolve()}
        elif st.startswith("*** Update File: "):
            if cur:
                ops.append(cur)
            cur = {"kind": "update", "path": (base / st[len("*** Update File: "):].strip()).resolve(),
                   "move_to": None, "hunks": []}
        elif st.startswith("*** Move to: ") and cur and cur["kind"] == "update":
            cur["move_to"] = (base / st[len("*** Move to: "):].strip()).resolve()
        elif st.startswith("*** "):
            raise ValueError(f"未知操作头: {st}")
        elif cur is None:
            if st != "":
                raise ValueError(f"缺少操作头: {st}")
        elif cur["kind"] == "add":
            if ln.startswith("+"):
                cur["lines"].append(ln[1:])
            elif st != "":
                raise ValueError(f"Add File 内容行必须以 + 开头: {ln!r}")
        elif cur["kind"] == "update":
            if ln.startswith("@@") or st == "@@":
                cur["hunks"].append({"lines": []})
            elif cur["hunks"]:
                cur["hunks"][-1]["lines"].append(ln)
            elif st != "":
                raise ValueError(f"Update File 在 hunk 前遇到非空行: {ln!r}")
        i += 1
    if cur:
        ops.append(cur)
    return ops


def _patch_backup(ops: list[dict], base: Path) -> Optional[str]:
    """统一备份: 把 ops 涉及的已存在文件留底, 返回 backup_id。"""
    paths = []
    for op in ops:
        p = op.get("path")
        if p and p.exists():
            paths.append(p)
        if op.get("move_to") and op["move_to"].exists():
            paths.append(op["move_to"])
    return _do_backup(paths, "codex_patch")


def _patch_restore(bid: str, base: Path) -> None:
    """回滚整次备份(base 仅为兼容签名, 还原按 manifest 绝对路径)。"""
    if bid:
        tool_backup("restore", backup_id=bid)


def _patch_cleanup(bid: str) -> None:
    """成功后保留备份(供手动后悔), 不再自动删除。"""
    return None


def _apply_codex_op(op: dict, base: Path, dry_run: bool) -> dict:
    p = op["path"]
    if op["kind"] == "add":
        if not dry_run:
            if p.exists():
                raise ValueError(f"文件已存在: {p}")
            p.parent.mkdir(parents=True, exist_ok=True)
            body = "\n".join(op["lines"])
            if op["lines"]:
                body += "\n"
            p.write_text(body, encoding="utf-8", newline="\n")
        return {"action": "add", "path": str(p)}
    if op["kind"] == "delete":
        if not p.exists():
            raise ValueError(f"文件不存在: {p}")
        if not dry_run:
            p.unlink()
        return {"action": "delete", "path": str(p)}
    if not p.exists():
        raise ValueError(f"文件不存在: {p}")
    text = decode_bytes(p.read_bytes())
    lines = text.split("\n")
    for h in op["hunks"]:
        search: list[str] = []
        replace: list[tuple[str, bool]] = []
        for ln in h["lines"]:
            if ln.startswith("@") or ln.startswith("*** End of File"):
                continue
            if ln.startswith("+") and not ln.startswith("+++"):
                replace.append((ln[1:], True))
            elif ln.startswith("-") and not ln.startswith("---"):
                search.append(ln[1:])
            elif ln.startswith(" "):
                search.append(ln[1:])
                replace.append((ln[1:], False))
            elif ln == "":
                search.append("")
                replace.append(("", False))
            else:
                raise ValueError(f"hunk 行必须以 空格/+/- 开头: {ln!r}")
        idx = _find_sequence(lines, search)
        if idx is None:
            raise ValueError(f"hunk 未匹配到文件内容: {p} (期望开头: {search[:2]})")
        new_lines = lines[:idx]
        new_lines += [t for t, _ in replace]
        new_lines += lines[idx + len(search):]
        lines = new_lines
    if not dry_run:
        p.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    if op["move_to"]:
        if not dry_run:
            op["move_to"].parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(p), str(op["move_to"]))
    return {"action": "update", "path": str(p),
            "move_to": str(op["move_to"]) if op["move_to"] else None}


def tool_codex_patch(patch: str, cwd: str = "", dry_run: bool = False,
                     rollback_on_failure: bool = True) -> dict:
    """应用 OpenAI Codex file-style patch (*** Begin Patch / *** End Patch)。"""
    if not patch or not patch.strip():
        raise ValueError("patch 为空")
    base = check_path(cwd).resolve() if cwd else Path(".").resolve()
    if not base.is_dir():
        raise ValueError(f"cwd 不是目录: {base}")
    ops = _parse_codex_patch(patch, base)
    if not ops:
        raise ValueError("patch 里没有文件操作")
    backup_id = None
    if not dry_run:
        backup_id = _patch_backup(ops, base)
    results: list[dict] = []
    try:
        for op in ops:
            results.append(_apply_codex_op(op, base, dry_run))
    except Exception:
        if rollback_on_failure and backup_id:
            _patch_restore(backup_id, base)
        raise
    if backup_id:
        _patch_cleanup(backup_id)
    return {"ok": True, "engine": "codex-patch", "dry_run": dry_run,
            "files": sorted(str(op["path"]) for op in ops),
            "actions": results, "backup_id": backup_id}


# ============================================================
# MCP 协议注册与 JSON-RPC
# ============================================================

TOOLS: list[dict] = [
    {
        "name": "shell",
        "description": "一次性执行 bash 命令(subprocess, 跑完进程即销毁)。适合无状态命令。需要保持 cd/export 状态或跑长任务用 shell_session。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 Shell 命令"},
                "cwd": {"type": "string", "description": "工作目录(可选), 相对路径基于 /workspace"},
                "timeout": {"type": "integer", "description": "超时秒数, 默认 120, 最大 600", "default": 120},
                "proot": {"type": "boolean", "description": "是否用 proot 全权限沙箱, 默认 true"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "shell_session",
        "description": "常驻 pty bash 会话+后台长任务。action: exec/read/write/interrupt/close/info/list/bg_start/bg_read/bg_kill/bg_list; 超时不杀命令, read 续读。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 Shell 命令 (action=exec/bg_start)"},
                "action": {
                    "type": "string",
                    "description": "动作, 默认 exec(给了 command)或 read(没给)",
                    "enum": ["exec", "read", "write", "interrupt", "close", "info",
                             "list", "bg_start", "bg_read", "bg_kill", "bg_list"],
                },
                "cwd": {"type": "string", "description": "工作目录(可选), 相对路径基于 /workspace"},
                "timeout": {"type": "integer", "description": "等哨兵的秒数, 默认 120, 最大 600。超时只是返回部分输出, 命令继续跑", "default": 120},
                "proot": {"type": "boolean", "description": "是否用 proot 全权限沙箱, 默认 true"},
                "session_id": {"type": "string", "description": "会话标识, 默认 'default'。不同 id 互不干扰", "default": "default"},
                "data": {"type": "string", "description": "action=write 时写入 stdin 的原文, 记得自带 \\n"},
                "task_id": {"type": "string", "description": "action=bg_read/bg_kill 的后台任务 id"},
                "offset": {"type": "integer", "description": "action=bg_read 时从日志第 N 字节开始读", "default": 0},
                "interrupt": {"type": "boolean", "description": "[兼容旧参数] 等价于 action=interrupt", "default": False},
            },
            "required": [],
        },
    },
    {
        "name": "read_file",
        "description": "读取文本文件。两种模式: offset/limit(字节偏移) 或 start_line/line_count(行号, 带行号输出)。paths 可一次读多个文件。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "paths": {"type": "array", "items": {"type": "string"}, "description": "批量读取"},
                "offset": {"type": "integer", "default": 0},
                "limit": {"type": "integer", "default": 65536},
                "start_line": {"type": "integer", "description": "起始行(1-based)"},
                "line_count": {"type": "integer", "description": "最多读多少行, 默认 400"},
                "max_chars": {"type": "integer", "description": "输出字符上限"},
            },
        },
    },
    {
        "name": "write_file",
        "description": "写入文本文件(utf-8)。自动创建父目录。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "text": {"type": "string"},
                "overwrite": {"type": "boolean", "default": True},
            },
            "required": ["path", "text"],
        },
    },
    {
        "name": "edit_file",
        "description": "文本精确替换。单次(path+old_text+new_text)或批量(edits 数组)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
                "replace_all": {"type": "boolean", "default": False},
                "expected_replacements": {"type": "integer", "default": 0},
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_text": {"type": "string"},
                            "new_text": {"type": "string"},
                            "replace_all": {"type": "boolean", "default": False},
                            "expected_replacements": {"type": "integer", "default": 0},
                        },
                        "required": ["old_text", "new_text"],
                    },
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "termux_api",
        "description": "调用 Termux:API 设备能力 (如 toast, battery-status, clipboard-get, clipboard-set, notification, vibrate, wifi-connectioninfo, volume 等)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "API 命令名, 如 battery-status, toast, clipboard-get, clipboard-set, vibrate, notification"},
                "args": {"type": "array", "description": "命令参数列表", "items": {"type": "string"}},
            },
            "required": ["command"],
        },
    },
    {
        "name": "grep",
        "description": "搜索文件内容 (ripgrep 后端)。output_mode: files_with_matches/content/count。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索模式(正则或字面量)"},
                "path": {"type": "string", "description": "搜索目录或文件", "default": "."},
                "output_mode": {"type": "string", "enum": ["files_with_matches", "content", "count"], "default": "files_with_matches"},
                "glob": {"type": "string", "description": "文件名过滤 (*.py)"},
                "type": {"type": "string", "description": "文件类型过滤 (py/sh/js/kt)"},
                "fixed_string": {"type": "boolean", "default": False},
                "ignore_case": {"type": "boolean", "default": True},
                "-A": {"type": "integer", "default": 0},
                "-B": {"type": "integer", "default": 0},
                "-C": {"type": "integer", "default": 0},
                "head_limit": {"type": "integer", "default": 250},
                "offset": {"type": "integer", "default": 0},
                "multiline": {"type": "boolean", "default": False},
                "hidden": {"type": "boolean", "default": False},
                "no_ignore": {"type": "boolean", "default": False},
            },
            "required": ["query"],
        },
    },
    {
        "name": "codex_patch",
        "description": "应用 OpenAI Codex file-style patch (*** Begin Patch ... *** End Patch)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "patch": {"type": "string"},
                "cwd": {"type": "string", "default": "."},
                "dry_run": {"type": "boolean", "default": False},
                "rollback_on_failure": {"type": "boolean", "default": True},
            },
            "required": ["patch"],
        },
    },
    {
        "name": "backup",
        "description": "备份/后悔药: list 列出 write_file/edit_file/codex_patch 自动生成的备份; restore 用 backup_id 回滚(可只滚 files 指定路径)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "restore"], "default": "list", "description": "list=列备份; restore=回滚"},
                "backup_id": {"type": "string", "description": "restore 时必填"},
                "files": {"type": "array", "items": {"type": "string"}, "description": "restore 时只回滚这些路径(可选, 默认全部)"},
                "limit": {"type": "integer", "default": 20, "description": "list 返回最近 N 份"},
            },
        },
    },
]

_TOOL_IMPL = {
    "shell": tool_shell,
    "shell_session": tool_shell_session,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "termux_api": tool_termux_api,
    "grep": tool_grep,
    "codex_patch": tool_codex_patch,
    "backup": tool_backup,
}

_GREP_ARG_ALIAS = {"-A": "after", "-B": "before", "-C": "context"}


def call_tool(name: str, args: dict) -> dict:
    fn = _TOOL_IMPL.get(name)
    if fn is None:
        raise ValueError(f"未知工具: {name}")
    if name == "grep":
        args = {_GREP_ARG_ALIAS.get(k, k): v for k, v in (args or {}).items()}
    try:
        result = fn(**args)
        return {"content": [{"type": "text", "text": json.dumps(
            result, ensure_ascii=False, default=str)}], "isError": False}
    except Exception as e:
        return {"content": [{"type": "text",
                             "text": f"[工具错误] {e}\n{traceback.format_exc(limit=3)}"}],
                "isError": True}


def handle_jsonrpc(msg: dict) -> Optional[dict]:
    method = msg.get("method")
    mid = msg.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": mid,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "termux-mcp", "version": VERSION},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params", {})
        try:
            result = call_tool(str(params.get("name", "")), params.get("arguments") or {})
        except Exception as e:
            result = {"content": [{"type": "text", "text": f"调用失败: {e}"}], "isError": True}
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    return {
        "jsonrpc": "2.0", "id": mid,
        "error": {"code": -32601, "message": f"未知方法: {method}"},
    }


# ============================================================
# HTTP 层: SSE + POST
# ============================================================

def auth_ok(self: BaseHTTPRequestHandler) -> bool:
    if not AUTH_TOKEN:
        return True
    hdr = self.headers.get("Authorization", "")
    if hdr == f"Bearer {AUTH_TOKEN}":
        return True
    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    return q.get("auth", [""])[0] == AUTH_TOKEN or q.get("token", [""])[0] == AUTH_TOKEN


class MCPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "termux-mcp/" + VERSION

    def _send(self, code: int, ctype: str, body: bytes, extra: Optional[dict] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, ngrok-skip-browser-warning")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            self._send(200, "application/json",
                       json.dumps({"ok": True, "tools": len(TOOLS),
                                   "auth": bool(AUTH_TOKEN), "server": "termux-mcp"}).encode())
            return
        if path not in ("/sse", "/mcp"):
            self._send(404, "application/json", b'{"error":"not found"}')
            return
        if not auth_ok(self):
            self._send(401, "application/json", b'{"error":"unauthorized"}')
            return
        sid = uuid.uuid4().hex
        q: queue.Queue[dict] = queue.Queue()
        with SESSIONS_LOCK:
            SESSIONS[sid] = q
        log(f"SSE connect: {sid}  ({self.client_address[0]})")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(
                f"event: endpoint\ndata: /messages?session_id={sid}\n\n".encode())
            self.wfile.flush()
            last_beat = time.time()
            while True:
                try:
                    data = q.get(timeout=10)
                    if data is None:
                        break
                    payload = json.dumps(data, ensure_ascii=False)
                    self.wfile.write(f"event: message\ndata: {payload}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    if time.time() - last_beat >= 15:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        last_beat = time.time()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with SESSIONS_LOCK:
                SESSIONS.pop(sid, None)
            log(f"SSE disconnect: {sid}")

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/messages", "/mcp"):
            self._send(404, "application/json", b'{"error":"not found"}')
            return
        if not auth_ok(self):
            self._send(401, "application/json", b'{"error":"unauthorized"}')
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except Exception:
            self._send(400, "application/json", b'{"error":"bad json"}')
            return

        resp = handle_jsonrpc(body)

        # Streamable HTTP (/mcp) 直接同步返回响应
        if path == "/mcp":
            if resp is None:
                self._send(204, "application/json", b"")
            else:
                self._send(200, "application/json", json.dumps(resp, ensure_ascii=False).encode())
            return

        # SSE (/messages) 分发到对应 session 队列
        q_params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        sid = q_params.get("session_id", [""])[0]
        with SESSIONS_LOCK:
            target = SESSIONS.get(sid)

        if resp is not None:
            if target is not None:
                target.put(resp)
                self._send(202, "application/json", b'{"accepted":true}')
            else:
                # 若无 session_id 则降级为直接同步返回
                self._send(200, "application/json", json.dumps(resp, ensure_ascii=False).encode())
        else:
            self._send(204, "application/json", b"")

    def log_message(self, format: str, *args: Any) -> None:
        # 静默常规请求日志, 只在必要时输出
        pass


def main() -> None:
    global HOST, PORT, AUTH_TOKEN
    parser = argparse.ArgumentParser(description="Termux MCP Server")
    parser.add_argument("--host", default=HOST, help="监听地址 (默认 0.0.0.0)")
    parser.add_argument("--port", type=int, default=PORT, help=f"监听端口 (默认 {PORT})")
    parser.add_argument("--token", default=AUTH_TOKEN, help="鉴权 Bearer token (默认 wei123..)")
    args = parser.parse_args()

    HOST = args.host
    PORT = args.port
    AUTH_TOKEN = args.token

    # 启动会话回收线程
    threading.Thread(target=session_cleanup_thread, daemon=True,
                     name="session-cleanup").start()

    server = ThreadingHTTPServer((HOST, PORT), MCPHandler)
    log(f"============================================================")
    log(f" Termux MCP Server v{VERSION} 启动完成")
    log(f" 监听地址: http://{HOST}:{PORT}")
    log(f" SSE 端点: http://127.0.0.1:{PORT}/sse")
    log(f" HTTP 端点: http://127.0.0.1:{PORT}/mcp")
    log(f" 鉴权 Token: {AUTH_TOKEN}")
    log(f" 工具总数: {len(TOOLS)} 个")
    log(f"============================================================")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Server 正在停止...")
        server.shutdown()
    finally:
        # 关闭所有常驻会话
        with _SESSIONS_LOCK:
            for s in _SESSIONS.values():
                s.close()
            _SESSIONS.clear()


if __name__ == "__main__":
    main()
