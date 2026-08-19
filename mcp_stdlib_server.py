#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
零依赖 MCP Server (Python 标准库 only)
=====================================
为 Termux/Android 而写 —— fastmcp 依赖 Rust 扩展(watchfiles/pydantic-core),
Android ABI 无预编译 wheel, 装不上。这里手写 JSON-RPC + SSE 协议层。

只用: http.server, json, threading, socketserver —— 全部标准库。

工具实现来自 workspace_mcp_core.py (同样零依赖)。

用法:
    MCP_PORT=8996 MCP_WORKSPACE_ROOT=$HOME python mcp_stdlib_server.py

传输:
    POST /mcp    - Streamable HTTP (MCP 2025-03-26, 推荐)
    GET  /sse    - SSE 握手, 返回 endpoint 事件
    POST /messages?session_id=xxx - SSE 模式下的消息通道
    GET  /health - 健康检查
"""

from __future__ import annotations

import inspect
import json
import os
import queue
import sys
import threading
import traceback
import typing
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import workspace_mcp_core as core  # noqa: E402

PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = os.environ.get("MCP_SERVER_NAME", "Workspace MCP (stdlib)")
SERVER_VERSION = "1.0.0"
AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()

# ============================================================
# 工具注册: 从 core 里挑出 8 个工具, 自动生成 JSON Schema
# ============================================================

TOOL_FUNCS = [
    core.shell, core.shell_session, core.read_file, core.write_file,
    core.edit_file, core.apply_patch, core.grep, core.backup,
]


def _py_to_json_type(ann) -> dict:
    """把 Python 类型注解转成 JSON Schema 片段。"""
    origin = typing.get_origin(ann)
    if origin is typing.Union or (origin is not None and str(origin) == "typing.Union"):
        args = [a for a in typing.get_args(ann) if a is not type(None)]
        if args:
            return _py_to_json_type(args[0])
        return {"type": "string"}
    if origin in (list, typing.List):
        args = typing.get_args(ann)
        inner = _py_to_json_type(args[0]) if args else {}
        if args and args[0] is dict:
            inner = {"type": "object", "additionalProperties": True}
        return {"type": "array", "items": inner}
    if ann is int:
        return {"type": "integer"}
    if ann is float:
        return {"type": "number"}
    if ann is bool:
        return {"type": "boolean"}
    if ann is dict:
        return {"type": "object", "additionalProperties": True}
    if ann is list:
        return {"type": "array", "items": {}}
    return {"type": "string"}


def _parse_docstring(doc: str) -> tuple[str, dict[str, str]]:
    """拆出摘要和 Args 段的参数说明。"""
    if not doc:
        return "", {}
    lines = doc.strip().split("\n")
    summary_lines, args_desc, in_args = [], {}, False
    cur_key = None
    for ln in lines:
        st = ln.strip()
        if st.lower().startswith("args:"):
            in_args = True
            continue
        if in_args:
            if not st:
                continue
            if ":" in st and not st.startswith(" ") and len(st.split(":")[0].split()) == 1:
                k, v = st.split(":", 1)
                cur_key = k.strip()
                args_desc[cur_key] = v.strip()
            elif cur_key:
                args_desc[cur_key] += " " + st
        else:
            summary_lines.append(st)
    return " ".join(x for x in summary_lines if x).strip(), args_desc


def build_tool_specs() -> list[dict]:
    specs = []
    for fn in TOOL_FUNCS:
        sig = inspect.signature(fn)
        hints = typing.get_type_hints(fn)
        summary, args_desc = _parse_docstring(fn.__doc__ or "")
        props, required = {}, []
        for name, p in sig.parameters.items():
            schema = _py_to_json_type(hints.get(name, str))
            if name in args_desc:
                schema["description"] = args_desc[name]
            if p.default is inspect.Parameter.empty:
                required.append(name)
            else:
                if p.default is not None and not isinstance(p.default, (list, dict)):
                    schema["default"] = p.default
            props[name] = schema
        specs.append({
            "name": fn.__name__,
            "description": summary or fn.__name__,
            "inputSchema": {
                "type": "object",
                "properties": props,
                **({"required": required} if required else {}),
            },
        })
    return specs


TOOL_SPECS = build_tool_specs()
TOOL_MAP = {fn.__name__: fn for fn in TOOL_FUNCS}


# ============================================================
# JSON-RPC 分发
# ============================================================


def handle_rpc(msg: dict) -> dict | None:
    """处理一条 JSON-RPC 消息。返回 None 表示是通知(无需响应)。"""
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, message, data=None):
        e = {"code": code, "message": message}
        if data:
            e["data"] = data
        return {"jsonrpc": "2.0", "id": mid, "error": e}

    if method == "initialize":
        return ok({
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return ok({})

    if method == "tools/list":
        return ok({"tools": TOOL_SPECS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = TOOL_MAP.get(name)
        if not fn:
            return err(-32602, f"Unknown tool: {name}")
        try:
            sig = inspect.signature(fn)
            clean = {k: v for k, v in args.items() if k in sig.parameters}
            result = fn(**clean)
            text = json.dumps(result, ensure_ascii=False, indent=2) if not isinstance(result, str) else result
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except core.ToolError as e:
            return ok({"content": [{"type": "text", "text": f"错误: {e}"}], "isError": True})
        except Exception as e:  # noqa: BLE001
            tb = traceback.format_exc(limit=3)
            return ok({"content": [{"type": "text", "text": f"内部错误: {e}\n{tb}"}], "isError": True})

    if method in ("resources/list", "prompts/list"):
        key = "resources" if "resources" in method else "prompts"
        return ok({key: []})

    return err(-32601, f"Method not found: {method}")


# ============================================================
# HTTP 层
# ============================================================

SSE_QUEUES: dict[str, queue.Queue] = {}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "McpStdlib/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("MCP_VERBOSE"):
            sys.stderr.write("[http] " + fmt % args + "\n")

    def _authed(self) -> bool:
        if not AUTH_TOKEN:
            return True
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Bearer ") and hdr[7:].strip() == AUTH_TOKEN:
            return True
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        return q.get("access_token", [""])[0] == AUTH_TOKEN

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, status=200, ctype="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Mcp-Session-Id")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            self._send_json({"status": "ok", "server": SERVER_NAME,
                             "tools": [t["name"] for t in TOOL_SPECS],
                             "root": str(core.WORKSPACE_ROOT),
                             "allowed": [str(p) for p in core.ALLOWED_ROOTS]})
            return
        if not self._authed():
            self._send_json({"error": "unauthorized"}, 401)
            return
        if path in ("/sse", "/mcp"):
            self._serve_sse()
            return
        self._send_json({"error": "not found", "hint": "POST /mcp or GET /sse"}, 404)

    def _serve_sse(self):
        sid = uuid.uuid4().hex
        q: queue.Queue = queue.Queue()
        SSE_QUEUES[sid] = q
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            ep = f"/messages?session_id={sid}"
            self.wfile.write(f"event: endpoint\ndata: {ep}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    item = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                if item is None:
                    break
                self.wfile.write(f"event: message\ndata: {json.dumps(item, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            SSE_QUEUES.pop(sid, None)

    def do_POST(self):
        path = self.path.split("?")[0]
        if not self._authed():
            self._send_json({"error": "unauthorized"}, 401)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError as e:
            self._send_json({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": f"Parse error: {e}"}}, 400)
            return

        batch = payload if isinstance(payload, list) else [payload]

        if path == "/messages":
            from urllib.parse import urlparse, parse_qs
            sid = parse_qs(urlparse(self.path).query).get("session_id", [""])[0]
            q = SSE_QUEUES.get(sid)
            if q is None:
                self._send_json({"error": "unknown session"}, 404)
                return
            self._send_text("Accepted", 202)
            for m in batch:
                r = handle_rpc(m)
                if r is not None:
                    q.put(r)
            return

        # /mcp - Streamable HTTP: 直接在响应里回
        results = [r for r in (handle_rpc(m) for m in batch) if r is not None]
        if not results:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return
        self._send_json(results if isinstance(payload, list) else results[0])


def main():
    port = int(os.environ.get("MCP_PORT", "8996"))
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    core.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    core.TASK_DIR.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    print(f"[mcp-stdlib] {SERVER_NAME} on http://{host}:{port}", file=sys.stderr)
    print(f"[mcp-stdlib] root={core.WORKSPACE_ROOT}", file=sys.stderr)
    print(f"[mcp-stdlib] allowed={[str(p) for p in core.ALLOWED_ROOTS]}", file=sys.stderr)
    print(f"[mcp-stdlib] tools={[t['name'] for t in TOOL_SPECS]}", file=sys.stderr)
    print(f"[mcp-stdlib] auth={'ON' if AUTH_TOKEN else 'OFF'}", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[mcp-stdlib] bye", file=sys.stderr)


if __name__ == "__main__":
    main()
