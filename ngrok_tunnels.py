#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ngrok 多隧道守护器 (termux-mcp 版)
==================================
一条隧道 = 一个 token = 一个独立 ngrok 进程 = 一个独立 web 面板端口。
与 MCP 解耦: termux_mcp.py 只管本机 MCP, 隧道全部由本守护器统一管理。
以后要接其他隧道(comfyui / 备用口子 / 其他设备), 往 tunnels.jsonc 加条目即可。

配置: <项目>/tunnels.jsonc        (支持 // 注释, 复制 tunnels.jsonc.example)
日志: <项目>/logs/ngrok-<id>.log
面板: http://127.0.0.1:<web_port>  (看 public_url + 流量 inspect)
状态: <项目>/logs/tunnels-status.json

用法:
    python3 ngrok_tunnels.py            # 守护(由 supervisor 的 processes.jsonc 注册保活)
    python3 ngrok_tunnels.py --status   # 看公网地址
环境变量覆盖:
    NGROK_DEVICE  本机身份 (默认读 tunnels.jsonc 顶层 device)
    NGROK_BIN     ngrok 二进制路径 (默认 which ngrok, 再回落 <项目>/ngrok)
    TUNNELS_CONFIG 配置文件路径 (默认 <项目>/tunnels.jsonc)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

BASE = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get("TUNNELS_CONFIG", BASE / "tunnels.jsonc"))
YML_DIR = BASE / "logs" / "tunnels"
LOG_DIR = BASE / "logs"
STATUS = LOG_DIR / "tunnels-status.json"
NGROK = os.environ.get("NGROK_BIN") or shutil.which("ngrok") or str(BASE / "ngrok")

_STOP = False
_LOG_DEVICE_SET = False


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def mask(t: str) -> str:
    return f"{t[:6]}...{t[-4:]}" if len(t) > 12 else "***"


def strip_jsonc(t: str) -> str:
    """剥掉 // 与 /* */ 注释, 去掉尾逗号, 返回合法 JSON。"""
    out, i, n = [], 0, len(t)
    while i < n:
        c = t[i]
        if c == '"':
            j = i + 1
            while j < n:
                if t[j] == "\\":
                    j += 2
                    continue
                if t[j] == '"':
                    break
                j += 1
            out.append(t[i:j + 1])
            i = j + 1
            continue
        if t.startswith("//", i):
            j = t.find("\n", i)
            i = n if j < 0 else j
            continue
        if t.startswith("/*", i):
            j = t.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


class Tunnel:
    def __init__(self, cfg: dict[str, Any], web_port: int, st: dict[str, Any]):
        self.id: str = str(cfg["id"])
        self.port = str(cfg.get("port", ""))
        self.device: str = str(cfg.get("device", "")).strip()

        self.proto: str = cfg.get("proto", "http")
        assert self.proto in ("http", "tcp", "tls"), "proto must be http/tcp/tls"

        self.region: str = cfg.get("region", "ap")
        self.token: str = str(cfg.get("token", "")).strip()
        self.email: str = str(cfg.get("email", ""))
        self.domain: str = str(cfg.get("domain") or cfg.get("static_domain", "")).strip()
        self.basic_auth = cfg.get("basic_auth", "")
        self.web_port: int = int(cfg.get("web_port") or web_port)

        self.yml = YML_DIR / f"tunnel-{self.id}.yml"
        self.logfile = LOG_DIR / f"ngrok-{self.id}.log"
        self.proc: Optional[subprocess.Popen] = None
        self.fh = None
        self.fails = 0
        self.next_try = 0.0
        self.dead = False
        self.delay = int(st.get("restart_delay", 10))
        self.max_fails = int(st.get("max_failures", 5))

    def write_yml(self) -> None:
        YML_DIR.mkdir(parents=True, exist_ok=True)
        self.yml.write_text(
            f'version: "2"\n'
            f"authtoken: {self.token}\n"
            f"web_addr: 127.0.0.1:{self.web_port}\n"
            f"log: stdout\n"
            f"log_level: info\n"
        )
        self.yml.chmod(0o600)

    def cmd(self) -> list[str]:
        c = [NGROK, self.proto, self.port, "--config", str(self.yml)]
        # region 和 basic-auth 在 3.39 标为 deprecated 但仍可用
        if self.region:
            c += ["--region", self.region]
        if self.domain:
            c += ["--url", self.domain]
        if self.basic_auth:
            items = [self.basic_auth] if isinstance(self.basic_auth, str) else self.basic_auth
            for ba in items:
                c += ["--basic-auth", ba]
        return c

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.write_yml()
        try:
            self.fh = open(self.logfile, "ab", buffering=0)
            self.fh.write(f"\n== start {time.strftime('%F %T')} "
                          f"{self.proto}:{self.port} region={self.region} "
                          f"web=:{self.web_port} ==\n".encode())
            self.proc = subprocess.Popen(
                self.cmd(), stdout=self.fh, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True)
            self.started = time.time()
            log(f"  ✓ {self.id}: pid={self.proc.pid} {self.proto}:{self.port} "
                f"region={self.region} web=:{self.web_port} "
                f"token={mask(self.token)} "
                f"{'email='+self.email if self.email else ''}")
        except OSError as e:
            log(f"  ✗ {self.id}: 启动失败 {e}")
            self.fails += 1
            self.next_try = time.time() + self.delay

    def stop(self) -> None:
        if self.alive():
            assert self.proc
            try:
                self.proc.terminate()
                self.proc.wait(timeout=6)
            except Exception:
                try:
                    self.proc.kill()
                except OSError:
                    pass
        if self.fh:
            try:
                self.fh.close()
            except OSError:
                pass
            self.fh = None
        self.proc = None
        try:
            self.yml.unlink()
        except OSError:
            pass

    def public_urls(self) -> list[str]:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.web_port}/api/tunnels", timeout=3) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            return [t.get("public_url", "") for t in data.get("tunnels", []) or []]
        except Exception:
            return []

    def tail(self, n: int = 6) -> str:
        try:
            txt = self.logfile.read_bytes()[-4000:].decode("utf-8", "replace")
            lines = [l for l in txt.strip().splitlines() if l.strip()]
            return "\n".join(f"      {l}" for l in lines[-n:])
        except OSError:
            return ""


def load() -> tuple[dict[str, Any], list[Tunnel], str]:
    if not CONFIG.is_file():
        log(f"✗ 找不到配置文件: {CONFIG} (复制 tunnels.jsonc.example 为 tunnels.jsonc)")
        return {}, [], ""
    try:
        cfg = json.loads(strip_jsonc(CONFIG.read_text(encoding="utf-8")))
    except Exception as e:
        log(f"✗ 配置解析失败 {CONFIG}: {e}")
        return {}, [], ""

    st = cfg.get("settings") or {}
    device = os.environ.get("NGROK_DEVICE") or str(cfg.get("device", "")).strip()

    global _LOG_DEVICE_SET
    if not _LOG_DEVICE_SET:
        log(f"本机 device = {device or '(未设置, 不拉起任何隧道)'}  (只拉起 device 匹配的隧道)")
        _LOG_DEVICE_SET = True

    base = int(st.get("web_port_base", 4040))
    tunnels, i = [], 0
    for t in cfg.get("tunnels", []):
        if not t.get("enabled", True):
            continue
        dev = str(t.get("device", "")).strip()
        if not dev:
            log(f"  ! 跳过 {t.get('id')}: 未注册设备(device 为空)")
            continue
        if dev != device:
            log(f"  ! 跳过 {t.get('id')}: 属于 {dev}, 非本机({device or '?'})")
            continue
        if not t.get("token"):
            log(f"  ! 跳过 {t.get('id')}: 没填 token")
            continue
        tunnels.append(Tunnel(t, base + i, st))
        i += 1
    return st, tunnels, device


def write_status(tunnels: list[Tunnel], device: str) -> None:
    data = {
        "updated_at": time.strftime("%F %T"),
        "device": device,
        "tunnels": [{
            "id": t.id,
            "email": t.email,
            "device": t.device,
            "alive": t.alive(),
            "pid": t.proc.pid if t.alive() else None,
            "local": f"{t.proto}:{t.port}",
            "region": t.region,
            "web": f"http://127.0.0.1:{t.web_port}",
            "public_url": t.public_urls(),
            "fails": t.fails,
            "gave_up": t.dead,
        } for t in tunnels],
    }
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATUS)


def show_status() -> int:
    if not STATUS.is_file():
        print("守护器没在跑(无状态文件), 先启动: python3 ngrok_tunnels.py")
        return 1
    d = json.loads(STATUS.read_text(encoding="utf-8"))
    print(f"更新于 {d['updated_at']}   本机 device={d.get('device', '?')}\n")
    hd = f"{'隧道':<24}{'状态':<8}{'本地':<16}{'面板':<26}公网"
    print(hd)
    print("-" * len(hd))
    for t in d["tunnels"]:
        flag = "运行" if t["alive"] else ("放弃" if t["gave_up"] else "停止")
        url = ", ".join(u for u in t["public_url"] if u) or "(未就绪)"
        name = f"{t['id']}"
        if t.get("device"):
            name += f" @{t['device']}"
        elif t.get("email"):
            name += f" ({t['email']})"
        print(f"{name:<24}{flag:<8}{t['local']:<16}{t['web']:<26}{url}")
    return 0


def on_sig(sig, _f):
    global _STOP
    _STOP = True
    log(f"收到信号 {sig}, 退出中...")


def already_running() -> bool:
    """防双开: 检测是否已有本守护器实例在跑(排除自己)。"""
    try:
        me = os.getpid()
        out = subprocess.run(
            ["pgrep", "-f", "ngrok_tunnels.py"], capture_output=True, text=True,
            timeout=5).stdout
        for pid in out.split():
            if pid.strip() and int(pid) != me:
                return True
    except Exception:
        pass
    return False


def main() -> int:
    if "--status" in sys.argv[1:]:
        return show_status()

    log("=" * 55)
    log(f"ngrok 多隧道守护器启动  bin={NGROK}")
    if not Path(NGROK).is_file():
        log(f"✗ 找不到 ngrok: {NGROK} (先跑 install.sh 装 ngrok 或手动放一个到项目目录)")
        return 2
    if already_running():
        log("✗ 已有实例在跑, 退出 (防双开)")
        return 0

    try:
        _st, tunnels, device = load()
    except Exception as e:
        log(f"✗ 配置错误 {CONFIG}: {e}")
        return 2
    if not tunnels:
        log(f"没有属于本机({device or '?'})的启用隧道, 退出。")
        return 0

    signal.signal(signal.SIGTERM, on_sig)
    signal.signal(signal.SIGINT, on_sig)

    log(f"启用 {len(tunnels)} 条隧道:")
    for t in tunnels:
        t.start()

    last = 0.0
    try:
        while not _STOP:
            now = time.time()
            for t in tunnels:
                if t.dead or t.alive():
                    continue
                if t.proc is not None:                     # 刚退出
                    rc, ran = t.proc.returncode, int(now - t.started)
                    t.stop()
                    t.fails = 0 if (rc == 0 and ran > 60) else t.fails + 1
                    log(f"  ! {t.id}: 退出 rc={rc} 存活{ran}s 失败{t.fails}/{t.max_fails}")
                    tl = t.tail()
                    if tl:
                        log("    日志尾部:\n" + tl)
                    if t.fails >= t.max_fails:
                        t.dead = True
                        log(f"  ✗ {t.id}: 连续失败过多, 放弃。修好后重启守护器。")
                        continue
                    t.next_try = now + t.delay
                    log(f"    {t.delay}s 后重启")
                elif now >= t.next_try:
                    t.start()

            if all(t.dead for t in tunnels):
                log("✗ 全部隧道已放弃, 退出。")
                write_status(tunnels, device)
                return 1
            if now - last >= 15:
                write_status(tunnels, device)
                last = now
            time.sleep(5)
    finally:
        log("清理子进程...")
        for t in tunnels:
            t.stop()
        write_status(tunnels, device)
        log("已退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
