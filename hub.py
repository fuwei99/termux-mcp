#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Termux MCP Hub (母节点) —— 纯 Python 标准库, 零第三方依赖
================================================================
一个 MCP 端点管理所有 Termux 设备。母节点跑在其中一台机器上(如 K60),
Rikkahub 只连它, 由它按 `device` 参数转发到各子节点的 termux_mcp.py。

    Rikkahub ──🌐ngrok──> [母节点 hub.py] ─┬─> 127.0.0.1:8996  本机 termux_mcp (零网络跳)
                                           └─🌐ngrok──> 子节点 termux_mcp (跨设备)

为什么不用 fastmcp: 它依赖 Rust 扩展(pydantic-core/watchfiles), Android ABI 无预编译
wheel, Termux 装不上。这里手写 JSON-RPC + SSE, 只用 http.server/http.client/json/queue。

传输 (与 termux_mcp.py 完全一致, 客户端通用):
    GET  /sse   /termux/sse    -> SSE 事件流 (先发 endpoint 事件)
    POST /messages?session_id= -> SSE 模式的消息通道
    POST /mcp   /termux/mcp    -> Streamable HTTP 同步入口
    GET  /health               -> 健康检查 (免鉴权, 附各节点状态)

工具表: 启动时/按需从任一在线节点拉 tools/list, 自动给每个工具注入 `device` 参数,
        并加前缀 termux_ 暴露。子节点加工具, 母节点零改动自动跟随。

性能要点 (2026-08-21 血泪):
    1. 转发必须复用 TCP+TLS 连接。实测跨 ngrok 新建连接 ~0.9s, 复用后 ~0.3s。
    2. 宿主 proot 曾因 /etc/resolv.conf 首行 nameserver 1.1.1.1 (国内不可达)
       导致 glibc 每次 DNS 死等 5s, 单次调用 6.1s。母节点搬到 Termux 原生环境后
       天然免疫; 但 start.sh 仍会校正 DNS, 防 Go/ngrok 的 [::1]:53 问题。

配置: 同目录 config.jsonc (见 config.jsonc.example), 支持 // 与 /* */ 注释。
用法: python3 hub.py                     # 端口取 config.ports.hub
      python3 hub.py --port 8994 --config /path/to/config.jsonc
"""

from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"
BASE = Path(__file__).resolve().parent

CONNECT_TIMEOUT = 3.0         # 建连超时: 3s 快速失败, 探活/离线不拖累其他设备
DEFAULT_TIMEOUT = 180.0       # 工具调用读超时
PROBE_TIMEOUT = 3.0           # termux_devices / health 探活超时
CONN_IDLE_MAX = 240.0         # 空闲连接最长复用寿命(秒)
TOOLS_CACHE_TTL = 300.0       # 工具表缓存

SESSIONS: dict[str, "queue.Queue[Optional[dict]]"] = {}
SESSIONS_LOCK = threading.Lock()


def log(msg: str) -> None:
    print(f"[hub {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ============================================================
# config.jsonc 解析 (支持注释 + 尾逗号)
# ============================================================

def strip_jsonc(text: str) -> str:
    """去掉 // 行注释、/* */ 块注释、尾逗号。字符串内的 // 不误伤。"""
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


class Config:
    """config.jsonc 的内存视图。字段缺失一律给安全默认值。"""

    def __init__(self, raw: dict, path: Optional[Path] = None):
        self.raw = raw or {}
        self.path = path
        self.mode = str(self.raw.get("mode", "child")).strip().lower()
        ports = self.raw.get("ports") or {}
        self.hub_port = int(ports.get("hub") or 8994)
        self.mcp_port = int(ports.get("mcp") or 8996)
        self.ngrok_authtoken = str(self.raw.get("ngrok-authtoken")
                                   or self.raw.get("ngrok_authtoken") or "").strip()
        self.auth_token = str(self.raw.get("auth_token") or self.raw.get("token")
                              or os.environ.get("TERMUX_MCP_TOKEN")
                              or os.environ.get("MCP_AUTH_TOKEN") or "wei123..").strip()
        self.nodes: dict[str, dict] = {}
        for name, cfg in (self.raw.get("nodes") or {}).items():
            if not isinstance(cfg, dict):
                continue
            if cfg.get("enabled") is False:
                continue
            url = str(cfg.get("url") or "").strip()
            if not url:
                continue
            lan_url = str(cfg.get("lan_url") or "").strip()
            self.nodes[str(name)] = {
                "url": url,
                "rpc_url": self._to_rpc(url),
                "lan_url": lan_url,
                "lan_rpc_url": self._to_rpc(lan_url) if lan_url else "",
                "headers": self._headers(cfg),
                "note": str(cfg.get("note") or ""),
            }

    @staticmethod
    def _to_rpc(url: str) -> str:
        """config 里写 /sse (与 Rikkahub 配置同形), 转发实际打无状态的 /mcp。"""
        u = url.rstrip("/")
        for suffix in ("/sse", "/messages", "/mcp"):
            if u.endswith(suffix):
                return u[: -len(suffix)] + "/mcp"
        return u + "/mcp"

    @staticmethod
    def _headers(cfg: dict) -> dict[str, str]:
        """除 url/note/enabled 外的键都当 HTTP header 透传(Authorization 等)。"""
        skip = {"url", "lan_url", "note", "enabled", "local"}
        h = {"ngrok-skip-browser-warning": "true"}
        for k, v in cfg.items():
            if k in skip or v is None:
                continue
            if isinstance(v, bool):
                v = "true" if v else "false"
            h[str(k)] = str(v)
        return h

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        cand = [Path(path)] if path else [BASE / "config.jsonc", BASE / "config.json"]
        for p in cand:
            if p.is_file():
                try:
                    data = json.loads(strip_jsonc(p.read_text(encoding="utf-8")))
                    log(f"配置已加载: {p}")
                    return cls(data, p)
                except Exception as e:
                    log(f"⚠️ 配置解析失败 {p}: {e} —— 用默认值继续")
                    return cls({}, p)
        log("⚠️ 未找到 config.jsonc, 用默认值 (mode=child, 无节点)")
        return cls({})


CFG = Config.load(os.environ.get("TERMUX_HUB_CONFIG"))


# ============================================================
# 转发层: 常驻连接池 (keep-alive 复用, 这是 0.3s 的关键)
# ============================================================

_IDLE: dict[tuple, list] = {}
_IDLE_LOCK = threading.Lock()


def _create_connection_ipv4_first(address: tuple[str, int], timeout: float = CONNECT_TIMEOUT,
                                  source_address: Optional[tuple] = None) -> socket.socket:
    """建连: 强制 IPv4 优先 (解决 Android/Termux 局域网无公网路由 IPv6 黑洞吞 SYN 死等超时问题)。"""
    host, port = address
    err = None
    try:
        addrs = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except Exception as e:
        raise e
    addrs.sort(key=lambda x: 0 if x[0] == socket.AF_INET else 1)
    deadline = time.time() + (timeout or 2.5)
    # 最多尝试前 2 个地址 (优先 IPv4), 绝不傻等 5 个 IP 轮询
    for af, socktype, proto, canonname, sa in addrs[:2]:
        remain = deadline - time.time()
        if remain <= 0:
            break
        sock = None
        try:
            sock = socket.socket(af, socktype, proto)
            sock.settimeout(min(remain, 1.5))
            if source_address:
                sock.bind(source_address)
            sock.connect(sa)
            return sock
        except Exception as e:
            err = e
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
    if err is not None:
        raise err
    raise TimeoutError(f"建连超时 (>{timeout:.1f}s)")


class _IPv4FirstHTTPConnection(http.client.HTTPConnection):
    def _create_connection(self, address, timeout=CONNECT_TIMEOUT, source_address=None):
        return _create_connection_ipv4_first(address, timeout, source_address)


class _IPv4FirstHTTPSConnection(http.client.HTTPSConnection):
    def _create_connection(self, address, timeout=CONNECT_TIMEOUT, source_address=None):
        return _create_connection_ipv4_first(address, timeout, source_address)


def _new_conn(scheme: str, netloc: str, connect_timeout: float = CONNECT_TIMEOUT):
    host, _, port = netloc.partition(":")
    port_i = int(port) if port else (443 if scheme == "https" else 80)
    if scheme == "https":
        conn = _IPv4FirstHTTPSConnection(host, port_i, timeout=connect_timeout)
    else:
        conn = _IPv4FirstHTTPConnection(host, port_i, timeout=connect_timeout)
    conn.connect()                       # 显式建连, 用较短的 CONNECT_TIMEOUT
    return conn


def _take_conn(key: tuple):
    with _IDLE_LOCK:
        pool = _IDLE.get(key) or []
        while pool:
            conn, ts = pool.pop()
            if time.time() - ts < CONN_IDLE_MAX:
                return conn
            try:
                conn.close()
            except Exception:
                pass
    return None


def _keep_conn(key: tuple, conn) -> None:
    with _IDLE_LOCK:
        _IDLE.setdefault(key, []).append((conn, time.time()))


def post_json(url: str, payload: dict, headers: dict, timeout: float) -> tuple[int, bytes]:
    """POST JSON, 复用长连接; 连接失效自动重试一次(用新连接)。"""
    p = urllib.parse.urlsplit(url)
    key = (p.scheme, p.netloc)
    path = p.path or "/"
    if p.query:
        path += "?" + p.query
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "Accept": "application/json",
            "Content-Length": str(len(body)), "Connection": "keep-alive", **headers}
    last_err: Optional[Exception] = None
    for attempt in (0, 1):
        reused = _take_conn(key)
        conn = reused or _new_conn(p.scheme, p.netloc)
        try:
            if conn.sock is not None:
                conn.sock.settimeout(timeout)     # 读超时可以很长, 建连超时很短
            conn.request("POST", path, body=body, headers=hdrs)
            resp = conn.getresponse()
            data = resp.read()
            status = resp.status
            if resp.will_close:
                try:
                    conn.close()
                except Exception:
                    pass
            else:
                _keep_conn(key, conn)
            return status, data
        except Exception as e:
            last_err = e
            try:
                conn.close()
            except Exception:
                pass
            if reused and attempt == 0:
                continue          # 复用的连接被对端掐了, 换新连接再来一次
            break
    raise last_err if last_err else RuntimeError("post_json 未知失败")


def node_rpc(node: str, method: str, params: Optional[dict] = None,
             timeout: float = DEFAULT_TIMEOUT) -> dict:
    """给某节点发一条 JSON-RPC, 返回 {"result":...} 或 {"error":...}。"""
    cfg = CFG.nodes.get(node)
    if not cfg:
        return {"error": f"未知设备: {node}。可用: {' / '.join(CFG.nodes) or '(无)'}"}
    payload = {"jsonrpc": "2.0", "id": uuid.uuid4().int % 100000, "method": method}
    if params is not None:
        payload["params"] = params
    try:
        status, raw = post_json(cfg["rpc_url"], payload, cfg["headers"], timeout)
    except (socket.timeout, TimeoutError):
        return {"error": f"[设备 {node} 超时] 建连/响应超时 (>{timeout:.0f}s)。"
                         f"检查该机 Termux 是否在跑 (bash start.sh) 及 ngrok 是否被系统杀掉。"}
    except Exception as e:
        return {"error": f"[设备 {node} 未连通] {type(e).__name__}: {e}。"
                         f"端点 {cfg['rpc_url']}"}
    if status == 401:
        return {"error": f"[设备 {node} 鉴权失败 401] Authorization 不匹配"}
    if status >= 400:
        return {"error": f"[设备 {node} HTTP {status}] {raw[:200].decode('utf-8', 'replace')}"}
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        head = raw[:200].decode("utf-8", "replace")
        if "ngrok" in head.lower():
            return {"error": f"[设备 {node} 隧道离线] ngrok 返回了错误页, "
                             f"该机 ngrok agent 大概被系统杀了。原文: {head}"}
        return {"error": f"[设备 {node} 响应非 JSON] HTTP {status}: {head}"}
    if "error" in data and data.get("result") is None:
        return {"error": f"[设备 {node} 协议错误] "
                         f"{json.dumps(data['error'], ensure_ascii=False)[:300]}"}
    return {"result": data.get("result")}


# ============================================================
# 工具表: 从在线节点拉 tools/list, 注入 device 参数后前缀暴露
# ============================================================

_FALLBACK_TOOLS = [
    ("shell", "一次性执行 bash/sh 命令(subprocess, 跑完即销)", {
        "command": {"type": "string", "description": "要执行的命令"},
        "cwd": {"type": "string", "description": "工作目录(可选)"},
        "timeout": {"type": "integer", "description": "超时秒数, 默认 120"},
        "proot": {"type": "boolean", "description": "是否用 proot 全权限沙箱, 默认 true"}},
        ["command"]),
    ("shell_session", "常驻 pty bash 会话: cd/export 状态持久, 哨兵协议, output_offset 续读", {
        "command": {"type": "string", "description": "要执行的命令"},
        "cwd": {"type": "string"},
        "timeout": {"type": "integer"},
        "proot": {"type": "boolean"},
        "session_id": {"type": "string", "description": "会话标识, 默认 default"},
        "output_offset": {"type": "integer", "description": "从第 N 字节续读"},
        "interrupt": {"type": "boolean", "description": "发送 Ctrl-C"}},
        ["command"]),
    ("read_file", "读取指定设备上的文本文件", {
        "path": {"type": "string"}, "offset": {"type": "integer"},
        "limit": {"type": "integer"}}, ["path"]),
    ("write_file", "向指定设备写入文本文件(utf-8), 自动建父目录", {
        "path": {"type": "string"}, "text": {"type": "string"},
        "overwrite": {"type": "boolean"}}, ["path", "text"]),
    ("edit_file", "文本精确替换(单次或批量)", {
        "path": {"type": "string"}, "old_text": {"type": "string"},
        "new_text": {"type": "string"}}, ["path"]),
    ("grep", "搜索文件内容(ripgrep)", {
        "query": {"type": "string"}, "path": {"type": "string"}}, ["query"]),
    ("codex_patch", "应用 Codex file-style patch", {
        "patch": {"type": "string"}}, ["patch"]),
    ("termux_api", "调用 Termux:API 设备能力", {
        "command": {"type": "string"}}, ["command"]),
    ("backup", "备份/后悔药: list 列出自动备份, restore 回滚", {
        "action": {"type": "string", "description": "list / restore"},
        "backup_id": {"type": "string"},
        "files": {"type": "array", "items": {"type": "string"}}}, []),
]

_tools_cache: dict[str, Any] = {"ts": 0.0, "tools": [], "src": ""}
_tools_lock = threading.Lock()


def _device_prop() -> dict:
    """device 参数的 schema。

    只给一行枚举 + 一句提示, 不逐台展开 note —— 否则每台设备的网络链路说明
    会在 9+ 个工具里各复制一遍, 一轮对话白烧上千 token。
    设备详情(note/url/在线状态)由 devices 工具负责, 别在这念经。
    """
    names = list(CFG.nodes)
    if names:
        return {"type": "string",
                "description": "目标设备: " + " / ".join(names) + " (详情见 devices 工具)",
                "enum": names}
    return {"type": "string", "description": "目标设备: (未配置节点)"}


# 工具名直接透传子节点原名, 不加前缀。
# 理由: Rikkahub 侧 MCP 本身已命名为 termux, 再加前缀会变成
# mcp__termux__termux_write_file 这种叠字, 自己调都嫌啰唢。
# 保持和子节点直连时一模一样的名字(write_file / read_file / shell ...),
# 从直连改成过母节点时, 除了多一个 device 参数, 其余使用习惯不变。
_RESERVED = {"devices"}          # 母节点自己的工具, 不转发


def _expose_name(remote: str) -> str:
    return remote


def _remote_name(exposed: str) -> str:
    # 兼容旧的 termux_ 前缀写法(客户端缓存了旧工具表时不致于直接报错)
    if exposed.startswith("termux_") and exposed != "termux_api":
        return exposed[len("termux_"):]
    return exposed


def _wrap(spec: dict) -> dict:
    """给子节点工具的 schema 注入 device, 名字加 termux_ 前缀。"""
    name = str(spec.get("name") or "")
    schema = json.loads(json.dumps(spec.get("inputSchema") or {"type": "object"}))
    schema.setdefault("type", "object")
    props = dict(schema.get("properties") or {})
    schema["properties"] = {"device": _device_prop(), **props}
    req = list(schema.get("required") or [])
    schema["required"] = ["device"] + [r for r in req if r != "device"]
    return {"name": _expose_name(name),
            "description": (spec.get("description") or "").strip(),
            "inputSchema": schema}


def _fallback_specs() -> list[dict]:
    out = []
    for name, desc, props, req in _FALLBACK_TOOLS:
        out.append(_wrap({"name": name, "description": desc + " (兜底表: 节点离线, "
                                                              "无法拉取完整工具清单)",
                          "inputSchema": {"type": "object", "properties": props,
                                          "required": req}}))
    return out


def gateway_tools(force: bool = False) -> list[dict]:
    with _tools_lock:
        fresh = time.time() - float(_tools_cache["ts"]) < TOOLS_CACHE_TTL
        if not force and fresh and _tools_cache["tools"]:
            return list(_tools_cache["tools"])
        upstream, src = [], ""
        for node in CFG.nodes:
            r = node_rpc(node, "tools/list", timeout=PROBE_TIMEOUT)
            tools = (r.get("result") or {}).get("tools") if r.get("result") else None
            if tools:
                upstream, src = tools, node
                break
        wrapped = [_wrap(t) for t in upstream] if upstream else _fallback_specs()
        wrapped.insert(0, {
            "name": "devices",
            "description": "列出母节点已接入的所有 Termux 设备及在线状态、端点、工具数。"
                           "不确定设备名或怀疑某台掉线时先调它。",
            "inputSchema": {"type": "object", "properties": {
                "probe": {"type": "boolean",
                          "description": "是否逐台探活(默认 true, 每台最多等 6s)"}},
                "required": []},
        })
        _tools_cache.update({"ts": time.time(), "tools": wrapped, "src": src})
        log(f"工具表刷新: {len(wrapped)} 个 (上游来源: {src or '兜底表'})")
        return list(wrapped)


def _text_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _probe_single_node(name: str, cfg: dict) -> str:
    tag = f"  {name:6s} {cfg['url']}"
    if cfg.get("lan_url"):
        tag += f"  (LAN: {cfg['lan_url']})"
    if cfg.get("note"):
        tag += f"  ({cfg['note']})"
    t0 = time.time()
    r = node_rpc(name, "tools/list", timeout=PROBE_TIMEOUT)
    dt = time.time() - t0
    ch = r.get("_channel")
    channel_desc = f" [{ch}直连]" if ch == "LAN" else (f" [{ch}隧道]" if ch == "WAN" else "")
    if r.get("result"):
        n = len((r["result"] or {}).get("tools") or [])
        tag += f"\n         ✅ 在线{channel_desc} {dt:.2f}s  工具 {n} 个"
    else:
        tag += f"\n         ❌ {r.get('error')}"
    return tag


def tool_devices(probe: bool = True) -> dict:
    lines = [f"母节点 mode={CFG.mode}  hub :{CFG.hub_port}  本机 mcp :{CFG.mcp_port}",
             f"节点数 {len(CFG.nodes)}"]
    if not probe:
        for name, cfg in CFG.nodes.items():
            tag = f"  {name:6s} {cfg['url']}"
            if cfg.get("note"):
                tag += f"  ({cfg['note']})"
            lines.append(tag)
        return _text_result("\n".join(lines))

    # 并发探活: 所有子节点线程池并发, 耗时取决于 max(节点耗时) 而非 sum(节点耗时), 彻底解决卡死
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(len(CFG.nodes), 8))) as pool:
        futures = {name: pool.submit(_probe_single_node, name, cfg)
                   for name, cfg in CFG.nodes.items()}
        for name in CFG.nodes:
            try:
                lines.append(futures[name].result(timeout=PROBE_TIMEOUT + 3.0))
            except Exception as e:
                err_msg = type(e).__name__ if not str(e) else str(e)
                lines.append(f"  {name:6s} {CFG.nodes[name]['url']}\n         ❌ 探活超时/异常: {err_msg}")
    return _text_result("\n".join(lines))


def dispatch_tool(name: str, args: dict) -> dict:
    args = dict(args or {})
    if name in ("devices", "termux_devices"):
        return tool_devices(bool(args.get("probe", True)))
    remote = _remote_name(name)
    device = str(args.pop("device", "") or "").strip()
    if not device:
        if len(CFG.nodes) == 1:
            device = next(iter(CFG.nodes))      # 只有一台就不用啰嗦
        else:
            return _text_result(
                f"缺少 device 参数。可用设备: {' / '.join(CFG.nodes) or '(无)'}", True)
    timeout = DEFAULT_TIMEOUT
    try:
        if args.get("timeout"):
            timeout = min(max(float(args["timeout"]) + 20.0, 30.0), 900.0)
    except Exception:
        pass
    r = node_rpc(device, "tools/call", {"name": remote, "arguments": args}, timeout)
    if r.get("error"):
        return _text_result(str(r["error"]), True)
    res = r.get("result") or {}
    if isinstance(res, dict) and "content" in res:
        return res                              # 原样透传子节点结果
    return _text_result(json.dumps(res, ensure_ascii=False)[:20000])


# ============================================================
# JSON-RPC
# ============================================================

def handle_jsonrpc(msg: dict) -> Optional[dict]:
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "termux-hub", "version": VERSION}}}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": gateway_tools()}}
    if method == "tools/call":
        params = msg.get("params") or {}
        try:
            result = dispatch_tool(str(params.get("name") or ""),
                                   params.get("arguments") or {})
        except Exception as e:
            result = _text_result(f"[母节点异常] {e}\n{traceback.format_exc(limit=3)}", True)
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"未知方法: {method}"}}


# ============================================================
# HTTP 层
# ============================================================

def auth_ok(h: BaseHTTPRequestHandler) -> bool:
    if not CFG.auth_token:
        return True
    if h.headers.get("Authorization", "") == f"Bearer {CFG.auth_token}":
        return True
    q = urllib.parse.parse_qs(urllib.parse.urlparse(h.path).query)
    return (q.get("auth", [""])[0] == CFG.auth_token
            or q.get("token", [""])[0] == CFG.auth_token)


SSE_PATHS = ("/sse", "/termux/sse")
RPC_PATHS = ("/mcp", "/termux/mcp")
MSG_PATHS = ("/messages", "/termux/messages")


class HubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "termux-hub/" + VERSION

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization, Content-Type, ngrok-skip-browser-warning")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path == "/health":
            self._send(200, "application/json", json.dumps({
                "ok": True, "role": "hub", "mode": CFG.mode,
                "hub_port": CFG.hub_port, "mcp_port": CFG.mcp_port,
                "nodes": list(CFG.nodes), "tools": len(gateway_tools()),
                "tools_src": _tools_cache.get("src") or "fallback",
            }, ensure_ascii=False).encode())
            return
        if path == "/":
            self._send(200, "application/json", json.dumps({
                "service": "Termux MCP Hub", "version": VERSION, "mode": CFG.mode,
                "sse": f"http://127.0.0.1:{CFG.hub_port}/sse",
                "nodes": {n: c["url"] for n, c in CFG.nodes.items()},
            }, ensure_ascii=False).encode())
            return
        if path not in SSE_PATHS:
            self._send(404, "application/json", b'{"error":"not found"}')
            return
        if not auth_ok(self):
            self._send(401, "application/json", b'{"error":"unauthorized"}')
            return
        prefix = "/termux" if path.startswith("/termux") else ""
        sid = uuid.uuid4().hex
        q: "queue.Queue[Optional[dict]]" = queue.Queue()
        with SESSIONS_LOCK:
            SESSIONS[sid] = q
        log(f"SSE connect {sid[:8]} ({self.client_address[0]})")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(
                f"event: endpoint\ndata: {prefix}/messages?session_id={sid}\n\n".encode())
            self.wfile.flush()
            last = time.time()
            while True:
                try:
                    data = q.get(timeout=5)
                    if data is None:
                        break
                    self.wfile.write(("event: message\ndata: "
                                      + json.dumps(data, ensure_ascii=False)
                                      + "\n\n").encode())
                    self.wfile.flush()
                except queue.Empty:
                    if time.time() - last >= 15:
                        self.wfile.write(b": ping\n\n")   # 心跳防 ngrok 掐空闲连接
                        self.wfile.flush()
                        last = time.time()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with SESSIONS_LOCK:
                SESSIONS.pop(sid, None)
            log(f"SSE disconnect {sid[:8]}")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path not in RPC_PATHS + MSG_PATHS:
            self._send(404, "application/json", b'{"error":"not found"}')
            return
        if not auth_ok(self):
            self._send(401, "application/json", b'{"error":"unauthorized"}')
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception as e:
            self._send(400, "application/json",
                       json.dumps({"error": f"bad json: {e}"}).encode())
            return
        resp = handle_jsonrpc(body)
        if path in RPC_PATHS:                      # Streamable HTTP: 同步返回
            if resp is None:
                self._send(202, "application/json", b"{}")
            else:
                self._send(200, "application/json",
                           json.dumps(resp, ensure_ascii=False).encode())
            return
        sid = urllib.parse.parse_qs(parsed.query).get("session_id", [""])[0]
        with SESSIONS_LOCK:
            q = SESSIONS.get(sid)
        if resp is None:
            self._send(202, "application/json", b"{}")
            return
        if q is None:                              # 没有会话就退化成同步返回
            self._send(200, "application/json",
                       json.dumps(resp, ensure_ascii=False).encode())
            return
        q.put(resp)
        self._send(202, "application/json", b"{}")

    def log_message(self, fmt: str, *args: Any) -> None:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Termux MCP Hub (母节点)")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--host", default=os.environ.get("TERMUX_HUB_HOST", "0.0.0.0"))
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    global CFG
    if args.config:
        CFG = Config.load(args.config)
    port = args.port or int(os.environ.get("TERMUX_HUB_PORT") or CFG.hub_port)

    log(f"Termux MCP Hub v{VERSION}  mode={CFG.mode}")
    log(f" 监听 {args.host}:{port}")
    log(f" SSE  http://127.0.0.1:{port}/sse   (亦兼容 /termux/sse)")
    log(f" HTTP http://127.0.0.1:{port}/mcp")
    log(f" 鉴权 Bearer {'已开启' if CFG.auth_token else '关闭'}")
    for name, cfg in CFG.nodes.items():
        log(f"   ├─ {name:6s} -> {cfg['rpc_url']}  {cfg['note']}")
    if not CFG.nodes:
        log("   ⚠️ nodes 为空, 请在 config.jsonc 里配置节点")
    threading.Thread(target=gateway_tools, kwargs={"force": True}, daemon=True).start()
    srv = ThreadingHTTPServer((args.host, port), HubHandler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("收到 Ctrl-C, 退出")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
