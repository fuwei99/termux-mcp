#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RikkaHub Workspace MCP - 工具核心实现(零第三方依赖)
==================================================
把 RikkaHub 内置 proot workspace 的 8 个工具通过 MCP 暴露给外部 agent。

监听: 0.0.0.0:8997 (SSE)   —— 8998/8999 已被 chat/sqlite MCP 占用
工具: shell / shell_session / read_file / write_file / edit_file
      apply_patch / grep / backup

设计要点
--------
* 沙箱: 只允许操作 ALLOWED_ROOTS 内的路径, 默认 /workspace + 三个挂载点。
* 相对路径: 一律相对 /workspace 解析(与 RikkaHub 原生工具行为一致)。
* 备份: 所有写操作前自动备份到 .rikkahub/backups-mcp/<id>/, 可 restore。
* 会话: 优先 pty(可传 Ctrl-C), 不可用时降级为管道。
* 后台任务: start/kill/list, 输出落盘到 .rikkahub/mcp-tasks/<id>.log。
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ============================================================
# 配置
# ============================================================

WORKSPACE_ROOT = Path(os.environ.get("MCP_WORKSPACE_ROOT", "/workspace"))
ALLOWED_ROOTS = [
    Path(p) for p in os.environ.get(
        "MCP_ALLOWED_ROOTS",
        "/workspace:/mnt/obsidian:/mnt/BaiduNetdisk:/rikkahub-data:/tmp:/skills:/upload",
    ).split(":") if p
]
READ_ONLY_ROOTS = [Path(p) for p in os.environ.get("MCP_READONLY_ROOTS", "/upload:/skills").split(":") if p]

BACKUP_DIR = WORKSPACE_ROOT / ".rikkahub" / "backups-mcp"
TASK_DIR = WORKSPACE_ROOT / ".rikkahub" / "mcp-tasks"

# shell 限制(对齐 workspace_config.jsonc)
SHELL_DEFAULT_TIMEOUT = 30
SHELL_MAX_TIMEOUT = 600
OUTPUT_MAX_CHARS = 128 * 1024
READ_DEFAULT_LINES = 400
READ_MAX_LINES = 2000
READ_DEFAULT_CHARS = 20_000
READ_HARD_MAX_CHARS = 60_000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_SESSIONS = 4
SESSION_IDLE_TIMEOUT = 3600
MAX_BACKGROUND = 5


# ============================================================
# 路径安全
# ============================================================


class ToolError(Exception):
    pass


def resolve_path(raw: str, *, write: bool = False) -> Path:
    """把用户给的路径解析成绝对路径, 并做沙箱校验。"""
    if raw is None or str(raw).strip() == "":
        raise ToolError("path 不能为空")
    p = Path(str(raw).strip()).expanduser()
    if not p.is_absolute():
        p = WORKSPACE_ROOT / p
    # 逐级 resolve, 允许目标不存在
    try:
        p = Path(os.path.normpath(str(p)))
        real = Path(os.path.realpath(str(p)))
    except OSError:
        real = p

    def inside(target: Path, roots: list[Path]) -> bool:
        for r in roots:
            try:
                rr = Path(os.path.realpath(str(r)))
            except OSError:
                rr = r
            if target == rr or str(target).startswith(str(rr).rstrip("/") + "/"):
                return True
            if target == r or str(target).startswith(str(r).rstrip("/") + "/"):
                return True
        return False

    if not inside(real, ALLOWED_ROOTS) and not inside(p, ALLOWED_ROOTS):
        raise ToolError(f"路径越界(沙箱外): {p}\n允许的根: {[str(x) for x in ALLOWED_ROOTS]}")
    if write and (inside(real, READ_ONLY_ROOTS) or inside(p, READ_ONLY_ROOTS)):
        raise ToolError(f"该路径为只读挂载, 禁止写入: {p}")
    return p


def clip(text: str, limit: int = OUTPUT_MAX_CHARS) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2:]
    return f"{head}\n\n... [截断 {len(text) - limit} 字符] ...\n\n{tail}"


# ============================================================
# 备份
# ============================================================


def make_backup(paths: list[Path], note: str = "") -> Optional[str]:
    existing = [p for p in paths if p.exists()]
    if not existing:
        return None
    bid = f"{int(time.time() * 1000)}-{os.getpid() % 10000}"
    dest = BACKUP_DIR / bid
    dest.mkdir(parents=True, exist_ok=True)
    manifest = {"id": bid, "note": note, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "files": []}
    for i, p in enumerate(existing):
        try:
            store = dest / f"{i:03d}_{p.name}"
            if p.is_dir():
                shutil.copytree(p, store, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(p, store)
            manifest["files"].append({"original": str(p), "stored": store.name})
        except Exception as e:  # noqa: BLE001
            manifest["files"].append({"original": str(p), "error": str(e)})
    (dest / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
    _prune_backups()
    return bid


def _prune_backups(keep: int = 100):
    try:
        items = sorted([d for d in BACKUP_DIR.iterdir() if d.is_dir()], key=lambda d: d.name)
        for d in items[:-keep]:
            shutil.rmtree(d, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


# ============================================================
# 1. shell
# ============================================================


def shell(
    command: str,
    cwd: str = "",
    timeout: int = SHELL_DEFAULT_TIMEOUT,
    session_id: str = "",
) -> dict:
    """在 RikkaHub workspace 的 proot rootfs 里执行 bash 命令。

    Args:
        command: 要执行的 bash 命令。
        cwd: 工作目录, 相对路径基于 /workspace。session 模式下忽略(用 cd)。
        timeout: 超时秒数, 默认 30, 最大 600。
        session_id: 传入 shell_session 返回的 id 则在该持久会话中执行。
    """
    if session_id:
        return session_exec(session_id, command, timeout)
    timeout = max(1, min(int(timeout or SHELL_DEFAULT_TIMEOUT), SHELL_MAX_TIMEOUT))
    workdir = resolve_path(cwd) if cwd else WORKSPACE_ROOT
    if not workdir.is_dir():
        raise ToolError(f"cwd 不是目录: {workdir}")
    try:
        proc = subprocess.run(
            ["bash", "-lc", command],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
        return {
            "exitCode": proc.returncode,
            "stdout": clip(proc.stdout),
            "stderr": clip(proc.stderr),
            "timedOut": False,
            "cwd": str(workdir),
        }
    except subprocess.TimeoutExpired as e:
        return {
            "exitCode": -1,
            "stdout": clip(e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")),
            "stderr": clip(e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")),
            "timedOut": True,
            "cwd": str(workdir),
        }


# ============================================================
# 2. shell_session (持久会话 + 后台任务)
# ============================================================


@dataclass
class Session:
    sid: str
    proc: subprocess.Popen
    master_fd: Optional[int]
    buffer: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_used: float = field(default_factory=time.time)
    pty: bool = False


@dataclass
class BackgroundTask:
    tid: str
    proc: subprocess.Popen
    command: str
    log: Path
    started: float = field(default_factory=time.time)


SESSIONS: dict[str, Session] = {}
TASKS: dict[str, BackgroundTask] = {}
_SENTINEL = "__MCP_DONE_{}__"


def _reap():
    now = time.time()
    for sid, s in list(SESSIONS.items()):
        if s.proc.poll() is not None or now - s.last_used > SESSION_IDLE_TIMEOUT:
            _close_session(sid)
    for tid, t in list(TASKS.items()):
        if t.proc.poll() is not None and now - t.started > 600:
            TASKS.pop(tid, None)


def _close_session(sid: str):
    s = SESSIONS.pop(sid, None)
    if not s:
        return
    try:
        if s.master_fd is not None:
            os.close(s.master_fd)
    except OSError:
        pass
    try:
        s.proc.terminate()
        s.proc.wait(timeout=3)
    except Exception:  # noqa: BLE001
        try:
            s.proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _open_session(cwd: str) -> Session:
    workdir = resolve_path(cwd) if cwd else WORKSPACE_ROOT
    sid = f"s-{uuid.uuid4().hex[:8]}"
    env = dict(os.environ, TERM="dumb", PS1="", PS2="")
    try:
        import pty as _pty

        master, slave = _pty.openpty()
        proc = subprocess.Popen(
            ["bash", "--noprofile", "--norc", "-i"],
            stdin=slave, stdout=slave, stderr=slave,
            cwd=str(workdir), env=env, preexec_fn=os.setsid, close_fds=True,
        )
        os.close(slave)
        os.set_blocking(master, False)
        s = Session(sid=sid, proc=proc, master_fd=master, pty=True)
    except Exception:  # noqa: BLE001  pty 不可用 -> 管道降级
        proc = subprocess.Popen(
            ["bash", "--noprofile", "--norc"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=str(workdir), env=env, text=True, bufsize=0,
        )
        os.set_blocking(proc.stdout.fileno(), False)
        s = Session(sid=sid, proc=proc, master_fd=None, pty=False)
    SESSIONS[sid] = s
    # 吞掉启动噪音
    _drain(s, 0.6)
    s.buffer = ""
    return s


def _write_session(s: Session, data: str):
    if s.master_fd is not None:
        os.write(s.master_fd, data.encode("utf-8", "replace"))
    else:
        s.proc.stdin.write(data)
        s.proc.stdin.flush()


def _drain(s: Session, wait: float) -> str:
    out = []
    deadline = time.time() + wait
    fd = s.master_fd if s.master_fd is not None else s.proc.stdout.fileno()
    while time.time() < deadline:
        try:
            chunk = os.read(fd, 65536)
            if chunk:
                out.append(chunk.decode("utf-8", "replace"))
                continue
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            break
        time.sleep(0.05)
    text = "".join(out)
    s.buffer += text
    return text


def session_exec(sid: str, command: str, timeout: int) -> dict:
    s = SESSIONS.get(sid)
    if not s:
        raise ToolError(f"会话不存在或已过期: {sid}")
    if s.proc.poll() is not None:
        _close_session(sid)
        raise ToolError(f"会话已退出: {sid}")
    timeout = max(1, min(int(timeout or SHELL_DEFAULT_TIMEOUT), SHELL_MAX_TIMEOUT))
    s.last_used = time.time()
    with s.lock:
        token = _SENTINEL.format(uuid.uuid4().hex[:6])
        s.buffer = ""
        _write_session(s, f"{command}\nprintf '\\n{token}%d\\n' $?\n")
        collected = ""
        deadline = time.time() + timeout
        rc = None
        while time.time() < deadline:
            collected += _drain(s, 0.2)
            m = re.search(re.escape(token) + r"(\d+)", collected)
            if m:
                rc = int(m.group(1))
                collected = collected[: m.start()]
                break
        still = rc is None
        cleaned = _clean(collected, command, token)
        return {
            "session_id": sid,
            "exitCode": rc if rc is not None else -1,
            "output": clip(cleaned),
            "still_running": still,
            "pty": s.pty,
        }


def _clean(text: str, command: str, token: str) -> str:
    text = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "")
    lines = text.split("\n")
    # 去掉 pty 回显的命令本身和哨兵行
    first = command.strip().split("\n")[0].strip()
    out = []
    for i, ln in enumerate(lines):
        st = ln.strip()
        if token in st:
            continue
        if i < 3 and st == first:
            continue
        if i < 3 and st.startswith("printf ") and token in ln:
            continue
        out.append(ln)
    return "\n".join(out).strip("\n")


def shell_session(
    action: str,
    session_id: str = "",
    command: str = "",
    cwd: str = "",
    data: str = "",
    process_id: str = "",
    wait_seconds: int = 5,
) -> dict:
    """管理持久 shell 会话(cwd/env/函数跨调用保留)与分离式后台任务。

    Args:
        action: open / close / read / write / interrupt / start / kill / list
        session_id: close/read/write/interrupt 必填。
        command: action=start 时的后台命令。
        cwd: action=open/start 的工作目录。
        data: action=write 写入 stdin 的原始文本(记得带换行)。
        process_id: action=kill 的任务 id(也接受 session_id)。
        wait_seconds: action=read 等待秒数。
    """
    _reap()
    action = (action or "").lower().strip()

    if action == "open":
        if len(SESSIONS) >= MAX_SESSIONS:
            raise ToolError(f"会话数已达上限 {MAX_SESSIONS}, 请先 close")
        s = _open_session(cwd)
        return {"session_id": s.sid, "pty": s.pty, "cwd": str(resolve_path(cwd) if cwd else WORKSPACE_ROOT)}

    if action == "close":
        _close_session(session_id)
        return {"closed": session_id}

    if action == "read":
        s = SESSIONS.get(session_id)
        if not s:
            raise ToolError(f"会话不存在: {session_id}")
        s.last_used = time.time()
        txt = _drain(s, max(0.2, min(float(wait_seconds or 5), 60)))
        return {"session_id": session_id, "output": clip(re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", txt)),
                "alive": s.proc.poll() is None}

    if action == "write":
        s = SESSIONS.get(session_id)
        if not s:
            raise ToolError(f"会话不存在: {session_id}")
        s.last_used = time.time()
        _write_session(s, data)
        time.sleep(0.3)
        return {"session_id": session_id, "written": len(data), "output": clip(_drain(s, 1.0))}

    if action == "interrupt":
        s = SESSIONS.get(session_id)
        if not s:
            raise ToolError(f"会话不存在: {session_id}")
        if s.pty and s.master_fd is not None:
            os.write(s.master_fd, b"\x03")
        else:
            try:
                os.killpg(os.getpgid(s.proc.pid), signal.SIGINT)
            except Exception:  # noqa: BLE001
                s.proc.send_signal(signal.SIGINT)
        time.sleep(0.4)
        return {"session_id": session_id, "interrupted": True, "output": clip(_drain(s, 1.0))}

    if action == "start":
        if not command:
            raise ToolError("action=start 需要 command")
        if len([t for t in TASKS.values() if t.proc.poll() is None]) >= MAX_BACKGROUND:
            raise ToolError(f"后台任务已达上限 {MAX_BACKGROUND}")
        TASK_DIR.mkdir(parents=True, exist_ok=True)
        tid = f"t-{uuid.uuid4().hex[:8]}"
        log = TASK_DIR / f"{tid}.log"
        workdir = resolve_path(cwd) if cwd else WORKSPACE_ROOT
        fh = open(log, "wb")
        proc = subprocess.Popen(
            ["bash", "-lc", command], cwd=str(workdir),
            stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )
        TASKS[tid] = BackgroundTask(tid=tid, proc=proc, command=command, log=log)
        time.sleep(min(float(wait_seconds or 2), 10))
        head = log.read_text("utf-8", "replace")[-4000:] if log.exists() else ""
        return {"process_id": tid, "pid": proc.pid, "log": str(log),
                "alive": proc.poll() is None, "output": head}

    if action == "kill":
        pid = process_id or session_id
        if pid in SESSIONS:
            _close_session(pid)
            return {"killed": pid, "kind": "session"}
        t = TASKS.get(pid)
        if not t:
            raise ToolError(f"任务不存在: {pid}")
        try:
            os.killpg(os.getpgid(t.proc.pid), signal.SIGTERM)
        except Exception:  # noqa: BLE001
            t.proc.terminate()
        TASKS.pop(pid, None)
        return {"killed": pid, "kind": "task"}

    if action == "list":
        return {
            "sessions": [
                {"session_id": s.sid, "alive": s.proc.poll() is None, "pty": s.pty,
                 "idle_seconds": round(time.time() - s.last_used, 1)}
                for s in SESSIONS.values()
            ],
            "tasks": [
                {"process_id": t.tid, "command": t.command, "pid": t.proc.pid,
                 "alive": t.proc.poll() is None, "log": str(t.log),
                 "uptime_seconds": round(time.time() - t.started, 1)}
                for t in TASKS.values()
            ],
        }

    raise ToolError(f"未知 action: {action}")


# ============================================================
# 3. read_file
# ============================================================


def read_file(
    path: str = "",
    paths: Optional[list[str]] = None,
    start_line: int = 1,
    line_count: int = READ_DEFAULT_LINES,
    max_chars: int = READ_DEFAULT_CHARS,
) -> dict:
    """读取文本文件内容(带行号)。支持单文件或一次读多个文件(最多 8 个)。

    Args:
        path: 单个文件路径, 相对路径基于 /workspace。
        paths: 一次读多个文件(最多 8 个), 可与 path 合并。
        start_line: 起始行(1-based)。
        line_count: 最多返回行数, 默认 400, 上限 2000。
        max_chars: 最多返回字符数, 默认 20000, 上限 60000。
    """
    targets: list[str] = []
    if path:
        targets.append(path)
    for p in paths or []:
        if p:
            targets.append(p)
    if not targets:
        raise ToolError("必须提供 path 或 paths")
    if len(targets) > 8:
        raise ToolError("一次最多读 8 个文件")

    start_line = max(1, int(start_line or 1))
    line_count = max(1, min(int(line_count or READ_DEFAULT_LINES), READ_MAX_LINES))
    max_chars = max(200, min(int(max_chars or READ_DEFAULT_CHARS), READ_HARD_MAX_CHARS))
    budget = max_chars
    results = []
    for t in targets:
        f = resolve_path(t)
        item: dict[str, Any] = {"path": str(f)}
        if not f.exists():
            item["error"] = "文件不存在"
        elif f.is_dir():
            try:
                entries = sorted(os.listdir(f))[:200]
                item["is_dir"] = True
                item["entries"] = entries
            except OSError as e:
                item["error"] = str(e)
        elif f.stat().st_size > MAX_FILE_BYTES:
            item["error"] = f"文件过大 ({f.stat().st_size} bytes > {MAX_FILE_BYTES})"
        else:
            try:
                raw = f.read_text("utf-8", "replace")
            except OSError as e:
                item["error"] = str(e)
                results.append(item)
                continue
            lines = raw.split("\n")
            total = len(lines)
            chunk = lines[start_line - 1: start_line - 1 + line_count]
            width = len(str(start_line + len(chunk)))
            body = "\n".join(f"{start_line + i:>{width}}\u2502{ln}" for i, ln in enumerate(chunk))
            if len(body) > budget:
                body = body[:budget] + f"\n... [超出 max_chars 预算, 已截断]"
            budget = max(0, budget - len(body))
            item.update({
                "total_lines": total,
                "start_line": start_line,
                "returned_lines": len(chunk),
                "truncated": start_line - 1 + len(chunk) < total,
                "content": body,
            })
        results.append(item)
    return {"files": results} if len(results) > 1 else results[0]


# ============================================================
# 4. write_file
# ============================================================


def write_file(path: str, text: str, overwrite: bool = True) -> dict:
    """创建或覆盖 UTF-8 文本文件, 写前自动备份。

    Args:
        path: 文件路径, 相对路径基于 /workspace。
        text: 文件内容。
        overwrite: 文件已存在时是否覆盖, 默认 True。
    """
    f = resolve_path(path, write=True)
    if f.exists() and not overwrite:
        raise ToolError(f"文件已存在且 overwrite=False: {f}")
    if f.is_dir():
        raise ToolError(f"目标是目录: {f}")
    bid = make_backup([f], note=f"write_file {f}")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, "utf-8")
    return {"path": str(f), "bytes": len(text.encode("utf-8")),
            "lines": text.count("\n") + 1, "backup_id": bid}


# ============================================================
# 5. edit_file
# ============================================================


def _flex_replace(content: str, old: str, new: str, replace_all: bool) -> tuple[str, int, str]:
    """精确匹配优先, 失败则空白容忍匹配。返回 (新内容, 替换次数, 匹配方式)。"""
    if old in content:
        n = content.count(old)
        if n > 1 and not replace_all:
            raise ToolError(f"old_text 匹配到 {n} 处, 请加 replace_all=True 或提供更长上下文")
        return (content.replace(old, new) if replace_all else content.replace(old, new, 1)), \
            (n if replace_all else 1), "exact"
    # 空白容忍: 把 old 里的空白序列变成 \s+
    pat = re.compile(r"\s+".join(re.escape(tok) for tok in old.split()), re.MULTILINE)
    matches = list(pat.finditer(content))
    if not matches:
        raise ToolError("未找到 old_text(精确与空白容忍匹配均失败)")
    if len(matches) > 1 and not replace_all:
        raise ToolError(f"空白容忍匹配到 {len(matches)} 处, 请加 replace_all=True")
    if replace_all:
        return pat.sub(lambda _: new, content), len(matches), "whitespace-tolerant"
    m = matches[0]
    return content[: m.start()] + new + content[m.end():], 1, "whitespace-tolerant"


def edit_file(
    path: str,
    old_text: str = "",
    new_text: str = "",
    edits: Optional[list[dict]] = None,
    replace_all: bool = False,
    dry_run: bool = False,
) -> dict:
    """编辑文本文件。传 old_text+new_text 做单次替换, 或传 edits 数组做多次顺序替换。

    Args:
        path: 文件路径。
        old_text: 要被替换的文本(需唯一, 除非 replace_all)。
        new_text: 替换成的文本。
        edits: [{old_text, new_text, replace_all?}, ...] 按顺序应用; 与 old_text 同时给时 edits 优先。
        replace_all: 替换全部匹配。
        dry_run: 只预览不落盘。
    """
    f = resolve_path(path, write=True)
    if not f.is_file():
        raise ToolError(f"文件不存在: {f}")
    content = f.read_text("utf-8", "replace")
    original = content
    plan = edits if edits else [{"old_text": old_text, "new_text": new_text, "replace_all": replace_all}]
    if not plan or not plan[0].get("old_text"):
        raise ToolError("必须提供 old_text 或 edits")
    if len(plan) > 20:
        raise ToolError("单次最多 20 个 edit")
    detail = []
    for i, e in enumerate(plan):
        content, n, how = _flex_replace(
            content, e.get("old_text", ""), e.get("new_text", ""),
            bool(e.get("replace_all", replace_all)),
        )
        detail.append({"index": i, "replacements": n, "match": how})
    bid = None
    if not dry_run:
        bid = make_backup([f], note=f"edit_file {f}")
        f.write_text(content, "utf-8")
    return {
        "path": str(f), "edits": detail, "dry_run": dry_run, "backup_id": bid,
        "size_before": len(original), "size_after": len(content),
        "diff_preview": clip(_mini_diff(original, content), 4000),
    }


def _mini_diff(a: str, b: str) -> str:
    import difflib
    return "".join(difflib.unified_diff(
        a.splitlines(keepends=True), b.splitlines(keepends=True),
        fromfile="before", tofile="after", n=2,
    ))


# ============================================================
# 6. apply_patch
# ============================================================


def apply_patch(patch: str, cwd: str = "", dry_run: bool = False, rollback_on_failure: bool = True) -> dict:
    """应用 git 风格 unified diff, 可新增/修改/删除/重命名文件。

    Args:
        patch: unified diff 文本(`--- a/x` / `+++ b/x` 头, 相对路径基于 cwd)。
        cwd: 补丁基准目录, 默认 /workspace。
        dry_run: 只做 --check 不落盘。
        rollback_on_failure: 失败时自动回滚已改动的文件。
    """
    if not patch or not patch.strip():
        raise ToolError("patch 为空")
    base = resolve_path(cwd, write=True) if cwd else WORKSPACE_ROOT
    if not base.is_dir():
        raise ToolError(f"cwd 不是目录: {base}")
    # 提取受影响文件用于备份
    files = set()
    for m in re.finditer(r"^(?:---|\+\+\+) (?:[ab]/)?(\S+)", patch, re.MULTILINE):
        if m.group(1) != "/dev/null":
            files.add(base / m.group(1))
    for f in files:
        resolve_path(str(f), write=True)  # 沙箱校验

    tmp = TASK_DIR / f"patch-{uuid.uuid4().hex[:8]}.diff"
    TASK_DIR.mkdir(parents=True, exist_ok=True)
    body = patch if patch.endswith("\n") else patch + "\n"
    tmp.write_text(body, "utf-8")
    try:
        check = subprocess.run(
            ["git", "apply", "--check", "-p1", "--unidiff-zero", str(tmp)],
            cwd=str(base), capture_output=True, text=True, timeout=60,
        )
        if dry_run:
            return {"dry_run": True, "ok": check.returncode == 0,
                    "files": sorted(str(f) for f in files),
                    "stderr": clip(check.stderr, 4000)}
        bid = make_backup(sorted(files), note="apply_patch") if files else None
        res = subprocess.run(
            ["git", "apply", "-p1", "--unidiff-zero", "--reject" if not rollback_on_failure else "--verbose", str(tmp)],
            cwd=str(base), capture_output=True, text=True, timeout=120,
        )
        if res.returncode != 0:
            # 退一步试 patch(1)
            res2 = subprocess.run(
                ["patch", "-p1", "--forward", "-i", str(tmp)],
                cwd=str(base), capture_output=True, text=True, timeout=120,
            )
            if res2.returncode != 0:
                if rollback_on_failure and bid:
                    backup(action="restore", backup_id=bid)
                raise ToolError(
                    f"补丁应用失败\ngit apply: {clip(res.stderr, 2000)}\npatch(1): {clip(res2.stdout + res2.stderr, 2000)}"
                )
            return {"ok": True, "engine": "patch(1)", "backup_id": bid,
                    "files": sorted(str(f) for f in files), "stdout": clip(res2.stdout, 4000)}
        return {"ok": True, "engine": "git apply", "backup_id": bid,
                "files": sorted(str(f) for f in files), "stderr": clip(res.stderr, 4000)}
    finally:
        tmp.unlink(missing_ok=True)


# ============================================================
# 7. grep
# ============================================================


def grep(
    query: str,
    path: str = "",
    regex: bool = False,
    ignore_case: bool = True,
    include_glob: str = "",
    max_results: int = 100,
) -> dict:
    """在 workspace / 挂载目录里搜索文件内容。跳过 .git、node_modules、build。

    Args:
        query: 搜索内容。
        path: 搜索目录, 默认 /workspace。
        regex: True 表示正则或 'a|b' 或搜索; False 为字面量。
        ignore_case: 忽略大小写, 默认 True。
        include_glob: 文件名过滤, 如 *.kt。
        max_results: 最多返回匹配数 1-500, 默认 100。
    """
    if not query:
        raise ToolError("query 不能为空")
    root = resolve_path(path) if path else WORKSPACE_ROOT
    if not root.exists():
        raise ToolError(f"路径不存在: {root}")
    n = max(1, min(int(max_results or 100), 500))
    cmd = ["grep", "-rn", "--binary-files=without-match"]
    if ignore_case:
        cmd.append("-i")
    cmd.append("-E" if regex else "-F")
    for ex in (".git", "node_modules", "build", ".gradle", "__pycache__", ".venv"):
        cmd.append(f"--exclude-dir={ex}")
    if include_glob:
        g = include_glob if include_glob.startswith("*") or "/" in include_glob else f"*{include_glob}"
        cmd.append(f"--include={g}")
    cmd += ["-m", "20", "--", query, str(root)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        raise ToolError("grep 超时(120s), 请缩小搜索范围")
    lines = [ln for ln in proc.stdout.split("\n") if ln.strip()]
    truncated = len(lines) > n
    hits = []
    for ln in lines[:n]:
        parts = ln.split(":", 2)
        if len(parts) == 3:
            hits.append({"file": parts[0], "line": int(parts[1]) if parts[1].isdigit() else 0,
                         "text": parts[2][:500]})
        else:
            hits.append({"raw": ln[:500]})
    return {"query": query, "root": str(root), "count": len(hits),
            "truncated": truncated, "matches": hits}


# ============================================================
# 8. backup
# ============================================================


def backup(action: str = "list", backup_id: str = "", files: Optional[list[str]] = None, limit: int = 20) -> dict:
    """查看或恢复本 MCP 写操作自动创建的备份。

    Args:
        action: list(列出, 最新在前) 或 restore(回滚)。
        backup_id: restore 必填。
        files: 只恢复指定路径, 省略则恢复该备份全部条目。
        limit: list 返回条数, 默认 20。
    """
    action = (action or "list").lower().strip()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if action == "list":
        items = sorted([d for d in BACKUP_DIR.iterdir() if d.is_dir()], key=lambda d: d.name, reverse=True)
        out = []
        for d in items[: max(1, int(limit or 20))]:
            mf = d / "manifest.json"
            if mf.exists():
                try:
                    m = json.loads(mf.read_text("utf-8"))
                    out.append({"id": m.get("id", d.name), "created": m.get("created"),
                                "note": m.get("note"), "files": [x.get("original") for x in m.get("files", [])]})
                    continue
                except Exception:  # noqa: BLE001
                    pass
            out.append({"id": d.name})
        return {"backups": out, "backup_dir": str(BACKUP_DIR)}

    if action == "restore":
        if not backup_id:
            raise ToolError("restore 需要 backup_id")
        d = BACKUP_DIR / backup_id
        mf = d / "manifest.json"
        if not mf.exists():
            raise ToolError(f"备份不存在: {backup_id}")
        m = json.loads(mf.read_text("utf-8"))
        want = set(files or [])
        restored, skipped = [], []
        # 恢复前先给当前状态兜一份底
        targets = [Path(x["original"]) for x in m.get("files", []) if "stored" in x
                   and (not want or x["original"] in want)]
        pre = make_backup(targets, note=f"pre-restore of {backup_id}")
        for entry in m.get("files", []):
            orig = entry.get("original")
            stored = entry.get("stored")
            if not stored or (want and orig not in want):
                skipped.append(orig)
                continue
            src, dst = d / stored, Path(orig)
            try:
                resolve_path(str(dst), write=True)
                dst.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    shutil.rmtree(dst, ignore_errors=True)
                    shutil.copytree(src, dst, symlinks=True)
                else:
                    shutil.copy2(src, dst)
                restored.append(orig)
            except Exception as e:  # noqa: BLE001
                skipped.append(f"{orig}: {e}")
        return {"backup_id": backup_id, "restored": restored, "skipped": skipped,
                "pre_restore_backup_id": pre}

    raise ToolError(f"未知 action: {action}(支持 list/restore)")


# ============================================================
# 启动
# ============================================================

