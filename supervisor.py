#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Termux 进程守护器 (supervisor) —— 纯标准库, 零第三方依赖
================================================================
把宿主 workspace 那套 scheduled_processes.json 的玩法移植到 Termux 上:
配置里声明要常驻的进程, 守护器每隔几秒巡一遍, 死了就拉起来。

用途: 母节点 hub / termux_mcp / ngrok 被 HyperOS 杀掉后自动复活。
      从此"重启服务"= 直接 pkill, 等几秒它自己回来 —— 不用再手动上机救。
      (注意: 守护器自己不能被它守护的东西杀掉, 所以它跟 ngrok 是分开的进程)

配置: 同目录 processes.jsonc (见 processes.jsonc.example), 支持 // 注释。
    每条进程:
      id            唯一标识 (日志名/pidfile 名)
      name          人类可读名字
      enabled       false = 不管它
      command       要跑的命令 (bash -c 执行)
      cwd           工作目录 (相对 supervisor 所在目录)
      check         存活判定, 三种任选:
                      {"http": "http://127.0.0.1:8994/health"}  健康检查
                      {"pgrep": "hub.py"}                       进程名匹配
                      {"port": 8996}                            端口在听
      grace         启动后等几秒才开始判活 (默认 15, 给它初始化时间)
      restartDelay  发现死了之后等几秒再拉 (默认 3)
      daysOfWeek    1=周一..7=周日, 默认全周
      startMinutes  当天从第几分钟开始守护 (默认 0)
      endMinutes    到第几分钟结束 (默认 1440)
      maxConsecutiveStartFailures  连续拉起失败几次后放弃 (默认 5, 0=永不放弃)

用法:
    python3 supervisor.py               # 前台跑 (Termux:Boot 里用 nohup 起)
    python3 supervisor.py --once        # 只巡一遍就退出 (调试/cron 用)
    python3 supervisor.py --status      # 打印各进程当前状态后退出
    python3 supervisor.py --interval 10 # 巡检间隔秒数, 默认 15
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

BASE = Path(__file__).resolve().parent
LOG_DIR = BASE / "logs"
STATE_FILE = LOG_DIR / "supervisor-state.json"
SELF_PIDFILE = LOG_DIR / "supervisor.pid"

DEFAULT_INTERVAL = 15.0
_stop = False


def log(msg: str) -> None:
    line = f"[sup {datetime.now():%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)


# ============================================================
# jsonc
# ============================================================

def strip_jsonc(text: str) -> str:
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_config(path: Optional[str] = None) -> list[dict]:
    cands = [Path(path)] if path else [BASE / "processes.jsonc", BASE / "processes.json"]
    for p in cands:
        if p.is_file():
            try:
                data = json.loads(strip_jsonc(p.read_text(encoding="utf-8")))
                procs = [x for x in (data.get("processes") or []) if isinstance(x, dict)]
                log(f"配置已加载: {p}  ({len(procs)} 条)")
                return procs
            except Exception as e:
                log(f"⚠️ 配置解析失败 {p}: {e}")
                return []
    log("⚠️ 未找到 processes.jsonc (可 cp processes.jsonc.example processes.jsonc)")
    return []


# ============================================================
# 存活判定
# ============================================================

def _http_ok(url: str, timeout: float = 5.0) -> bool:
    try:
        req = urllib.request.Request(url, headers={"ngrok-skip-browser-warning": "true"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 400
    except Exception:
        return False


def _port_ok(port: int, host: str = "127.0.0.1", timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def _pgrep_ok(pattern: str) -> bool:
    exe = shutil.which("pgrep")
    if exe:
        try:
            r = subprocess.run([exe, "-f", pattern], capture_output=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                return True
            if r.returncode == 1:
                return False
        except Exception:
            pass
    # pgrep 不可用时退回 ps 扫描
    try:
        r = subprocess.run(["ps", "-ef"], capture_output=True, timeout=10)
        me = str(os.getpid())
        for line in r.stdout.decode("utf-8", "replace").splitlines():
            if pattern in line and "supervisor.py" not in line and me not in line.split()[:2]:
                return True
    except Exception:
        pass
    return False


def is_alive(proc: dict) -> tuple[bool, str]:
    """返回 (是否存活, 判定依据描述)。多个 check 全部满足才算活。"""
    check = proc.get("check") or {}
    if not isinstance(check, dict) or not check:
        pat = proc.get("command", "")[:40]
        return _pgrep_ok(pat), f"pgrep({pat})"
    reasons = []
    for kind, val in check.items():
        if kind == "http":
            ok = _http_ok(str(val))
            reasons.append(f"http({val})={'OK' if ok else 'DOWN'}")
        elif kind == "port":
            ok = _port_ok(int(val))
            reasons.append(f"port({val})={'OK' if ok else 'DOWN'}")
        elif kind == "pgrep":
            ok = _pgrep_ok(str(val))
            reasons.append(f"pgrep({val})={'OK' if ok else 'DOWN'}")
        else:
            continue
        if not ok:
            return False, " ".join(reasons)
    return True, " ".join(reasons) or "no-check"


# ============================================================
# 时间窗
# ============================================================

def in_window(proc: dict, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    days = proc.get("daysOfWeek") or [1, 2, 3, 4, 5, 6, 7]
    if now.isoweekday() not in days:
        return False
    start = int(proc.get("startMinutes", 0))
    end = int(proc.get("endMinutes", 1440))
    cur = now.hour * 60 + now.minute
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end          # 跨午夜


# ============================================================
# 状态
# ============================================================

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    except Exception as e:
        log(f"⚠️ 状态写入失败: {e}")


def spawn(proc: dict) -> Optional[int]:
    """起一个进程, 完全脱离守护器 (setsid), 免得守护器重启把它带走。"""
    pid_ = proc.get("id") or "proc"
    cmd = proc.get("command") or ""
    cwd = proc.get("cwd") or str(BASE)
    cwd_p = Path(cwd)
    if not cwd_p.is_absolute():
        cwd_p = BASE / cwd
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = open(LOG_DIR / f"{pid_}.out.log", "ab", buffering=0)
    try:
        out.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} supervisor 拉起 =====\n"
                  .encode())
    except Exception:
        pass
    try:
        p = subprocess.Popen(["bash", "-lc", cmd], cwd=str(cwd_p),
                             stdout=out, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        return p.pid
    except Exception as e:
        log(f"  ❌ {pid_} 启动异常: {e}")
        return None
    finally:
        try:
            out.close()
        except Exception:
            pass


def tick(procs: list[dict], state: dict) -> None:
    now = datetime.now()
    for proc in procs:
        pid_ = str(proc.get("id") or "")
        if not pid_ or proc.get("enabled") is False:
            continue
        st = state.setdefault(pid_, {"fails": 0, "startedAt": 0, "restarts": 0})
        if not in_window(proc, now):
            st["note"] = "时间窗外"
            continue
        grace = float(proc.get("grace", 15))
        if st.get("startedAt") and time.time() - float(st["startedAt"]) < grace:
            continue                                     # 刚拉起, 给它时间
        alive, why = is_alive(proc)
        if alive:
            if st.get("fails"):
                log(f"  ✅ {pid_} 恢复正常 ({why})")
            st["fails"] = 0
            st["note"] = f"运行中 {why}"
            st["lastSeen"] = int(time.time())
            continue
        limit = int(proc.get("maxConsecutiveStartFailures", 5))
        if limit and int(st.get("fails", 0)) >= limit:
            st["note"] = f"已放弃 (连续失败 {st['fails']} 次), 修好后删 {STATE_FILE.name} 或重启守护器"
            continue
        delay = float(proc.get("restartDelay", 3))
        log(f"  ⚠️ {pid_} 不在了 ({why}) → {delay:.0f}s 后拉起 "
            f"[第 {int(st.get('restarts', 0)) + 1} 次]")
        time.sleep(delay)
        new_pid = spawn(proc)
        st["startedAt"] = time.time()
        st["restarts"] = int(st.get("restarts", 0)) + 1
        st["lastRestart"] = datetime.now().strftime("%m-%d %H:%M:%S")
        if new_pid:
            st["fails"] = int(st.get("fails", 0)) + 1     # 下轮判活成功会清零
            st["note"] = f"已拉起 pid={new_pid}, 等待判活"
            log(f"  ▶ {pid_} 已拉起 pid={new_pid} ({proc.get('name') or ''})")
        else:
            st["fails"] = int(st.get("fails", 0)) + 1
            st["note"] = "拉起失败"


def print_status(procs: list[dict], state: dict) -> None:
    print(f"{'ID':22s} {'状态':6s} 说明")
    print("-" * 78)
    for proc in procs:
        pid_ = str(proc.get("id") or "")
        if proc.get("enabled") is False:
            print(f"{pid_:22s} {'停用':6s} enabled=false")
            continue
        alive, why = is_alive(proc)
        st = state.get(pid_, {})
        flag = "✅活" if alive else "❌死"
        extra = f"重启 {st.get('restarts', 0)} 次"
        if st.get("lastRestart"):
            extra += f", 最近 {st['lastRestart']}"
        if not in_window(proc):
            flag = "⏸窗外"
        print(f"{pid_:22s} {flag:6s} {why}  |  {extra}")


def _sig(signum, frame):
    global _stop
    _stop = True
    log(f"收到信号 {signum}, 准备退出")


def main() -> None:
    ap = argparse.ArgumentParser(description="Termux 进程守护器")
    ap.add_argument("--config", default=None)
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    ap.add_argument("--once", action="store_true", help="只巡一遍就退出")
    ap.add_argument("--status", action="store_true", help="打印状态后退出")
    args = ap.parse_args()

    procs = load_config(args.config)
    state = load_state()

    if args.status:
        print_status(procs, state)
        return

    if not procs:
        log("没有要守护的进程, 退出")
        return

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        SELF_PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass

    log(f"守护器启动 pid={os.getpid()}  巡检间隔 {args.interval:.0f}s")
    for p in procs:
        if p.get("enabled") is not False:
            log(f"  ├─ {p.get('id'):20s} {p.get('name') or ''}")

    if args.once:
        tick(procs, state)
        save_state(state)
        return

    while not _stop:
        try:
            tick(procs, state)
            save_state(state)
        except Exception as e:
            log(f"⚠️ 巡检异常: {e}")
        for _ in range(int(max(args.interval, 1))):
            if _stop:
                break
            time.sleep(1)
    log("守护器退出")


if __name__ == "__main__":
    main()
