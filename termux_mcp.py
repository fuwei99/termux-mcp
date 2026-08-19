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
import json
import os
import platform
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
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


def check_path(raw: str) -> Path:
    p = Path(raw).expanduser()
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
# 工具实现
# ============================================================

def _run_shell(command: str, cwd: str = "", timeout: int = 120) -> dict:
    shell_bin = os.environ.get("SHELL") or "/data/data/com.termux/files/usr/bin/bash"
    if not os.path.exists(shell_bin):
        shell_bin = shutil.which("bash") or shutil.which("sh") or "/bin/sh"
    
    cwd_p = cwd or None
    if cwd_p:
        cwd_p = str(check_path(cwd_p))
    
    try:
        proc = subprocess.Popen(
            [shell_bin, "-c", command],
            cwd=cwd_p,
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


def tool_shell(command: str, cwd: str = "", timeout: int = 120) -> dict:
    """在 Termux/Linux 上执行 bash/sh 命令。"""
    return _run_shell(command, cwd, timeout)


def tool_read_file(path: str, offset: int = 0, limit: int = 65536) -> dict:
    """读取文本文件, offset/limit 为字节偏移, 默认 64KB。"""
    p = check_path(path)
    if not p.is_file():
        raise ValueError(f"文件不存在: {path}")
    with open(p, "rb") as f:
        f.seek(max(0, offset))
        data = f.read(max(1, min(limit, 4 * 1024 * 1024)))
    text = decode_bytes(data)
    return {"path": str(p), "offset": offset, "text": clip(text, OUTPUT_MAX * 2), "bytes": len(data)}


def tool_write_file(path: str, text: str, overwrite: bool = True) -> dict:
    """写入文本文件(utf-8)。自动创建父目录。"""
    p = check_path(path)
    if p.exists() and not overwrite:
        raise ValueError(f"文件已存在且 overwrite=False: {path}")
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return {"path": str(p), "bytes": p.stat().st_size}


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

    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    return {"path": str(p), "replacements": total, "bytes": p.stat().st_size}


def tool_list_dir(path: str = ".", depth: int = 1) -> dict:
    """列出目录树(深度限制, 默认 1)。"""
    p = check_path(path)
    if not p.is_dir():
        raise ValueError(f"目录不存在: {path}")
    items: list[dict] = []

    def walk(d: Path, cur: int):
        try:
            entries = sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except OSError as e:
            items.append({"path": str(d), "type": "error", "note": str(e)})
            return
        for e in entries:
            try:
                st = e.stat()
                it: dict[str, Any] = {"path": str(e), "name": e.name}
                if e.is_dir():
                    it["type"] = "dir"
                    it["size"] = None
                    items.append(it)
                    if cur < depth:
                        walk(e, cur + 1)
                else:
                    it["type"] = "file"
                    it["size"] = st.st_size
                    items.append(it)
            except OSError:
                items.append({"path": str(e), "name": e.name, "type": "?", "size": None})
    walk(p, 1)
    return {"path": str(p), "count": len(items), "items": items}


def tool_list_processes(top: int = 30) -> dict:
    """列出系统进程(基于 ps aux 提取 top N)。"""
    try:
        r = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=10)
        lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    except Exception as e:
        return {"error": f"ps aux 失败: {e}"}
    
    if not lines:
        return {"count": 0, "processes": []}
    
    header = lines[0]
    procs: list[dict] = []
    for ln in lines[1:]:
        parts = ln.split(None, 10)
        if len(parts) >= 11:
            procs.append({
                "user": parts[0],
                "pid": parts[1],
                "cpu": parts[2],
                "mem": parts[3],
                "vsz": parts[4],
                "rss": parts[5],
                "stat": parts[7],
                "command": parts[10],
            })
        elif len(parts) >= 4:
            procs.append({"pid": parts[1], "info": ln})
            
    return {"count": len(procs), "processes": procs[:max(1, top)]}


def tool_system_info() -> dict:
    """获取 Termux / Linux 系统信息(架构/内存/存储/电池等)。"""
    info: dict[str, Any] = {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "node": platform.node(),
        "user": os.environ.get("USER") or "termux",
        "home": os.environ.get("HOME", ""),
        "prefix": os.environ.get("PREFIX", "/data/data/com.termux/files/usr"),
        "python": sys.version.split()[0],
        "cwd": os.getcwd(),
        "uptime_ts": time.time(),
    }
    
    # 内存与存储
    try:
        mem = subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=5)
        info["free_m"] = mem.stdout.strip()
    except Exception:
        pass
        
    try:
        disk = subprocess.run(["df", "-h", "."], capture_output=True, text=True, timeout=5)
        info["disk_h"] = disk.stdout.strip()
    except Exception:
        pass
        
    # Termux API 电池检测(若有)
    if shutil.which("termux-battery-status"):
        try:
            bat = subprocess.run(["termux-battery-status"], capture_output=True, text=True, timeout=5)
            info["battery"] = json.loads(bat.stdout)
        except Exception:
            pass

    return info


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
    """用 termux-open / xdg-open 打开文件或 URL。"""
    opener = shutil.which("termux-open") or shutil.which("xdg-open")
    if not opener:
        raise ValueError("系统未找到 termux-open 或 xdg-open")
    
    subprocess.Popen([opener, target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"opened": target, "via": opener}


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
# codex_patch (OpenAI Codex file-style patch, 纯 Python 解析)
# ============================================================

PATCH_BACKUP_ROOT = Path(__file__).resolve().parent / "logs" / "patch-backups"


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


def _patch_backup(ops: list[dict], base: Path) -> str:
    bid = f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
    d = PATCH_BACKUP_ROOT / bid
    for op in ops:
        p = op["path"]
        if p.exists():
            try:
                rel = p.relative_to(base)
            except ValueError:
                rel = Path(p.name)
            dest = d / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(p), str(dest))
    return bid


def _patch_restore(bid: str, base: Path) -> None:
    d = PATCH_BACKUP_ROOT / bid
    if not d.is_dir():
        return
    for f in d.rglob("*"):
        if f.is_file():
            orig = base / f.relative_to(d)
            orig.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(f), str(orig))


def _patch_cleanup(bid: str) -> None:
    d = PATCH_BACKUP_ROOT / bid
    if d.is_dir():
        shutil.rmtree(str(d), ignore_errors=True)


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
        "description": "在 Termux / Linux 上执行 bash/sh 命令, 返回 stdout/stderr/exit_code。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 Shell 命令"},
                "cwd": {"type": "string", "description": "工作目录(可选)"},
                "timeout": {"type": "integer", "description": "超时秒数, 默认 120", "default": 120},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "读取文本文件。offset/limit 为字节偏移, 默认 64KB。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "default": 0},
                "limit": {"type": "integer", "default": 65536},
            },
            "required": ["path"],
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
        "name": "list_dir",
        "description": "列出目录内容与文件大小。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "depth": {"type": "integer", "default": 1},
            },
        },
    },
    {
        "name": "list_processes",
        "description": "列出系统进程列表(top N)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "top": {"type": "integer", "default": 30},
            },
        },
    },
    {
        "name": "system_info",
        "description": "获取系统信息(Termux环境/CPU/内存/磁盘/电池等)。",
        "inputSchema": {"type": "object", "properties": {}},
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
        "name": "open_path",
        "description": "调用 termux-open 打开文件/URL。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
            },
            "required": ["target"],
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
]

_TOOL_IMPL = {
    "shell": tool_shell,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "list_dir": tool_list_dir,
    "list_processes": tool_list_processes,
    "system_info": tool_system_info,
    "termux_api": tool_termux_api,
    "open_path": tool_open_path,
    "grep": tool_grep,
    "codex_patch": tool_codex_patch,
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
    parser = argparse.ArgumentParser(description="Termux MCP Server")
    parser.add_argument("--host", default=HOST, help="监听地址 (默认 0.0.0.0)")
    parser.add_argument("--port", type=int, default=PORT, help=f"监听端口 (默认 {PORT})")
    parser.add_argument("--token", default=AUTH_TOKEN, help="鉴权 Bearer token (默认 wei123..)")
    args = parser.parse_args()

    global HOST, PORT, AUTH_TOKEN
    HOST = args.host
    PORT = args.port
    AUTH_TOKEN = args.token

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


if __name__ == "__main__":
    main()
