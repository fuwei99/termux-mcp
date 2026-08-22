#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 config.jsonc 的字段吐成 shell 可 eval 的赋值语句。

    eval "$(python3 read-config.py)"
    echo "$MODE $CFG_MCP_PORT $CFG_HUB_PORT $CFG_TOKEN $CFG_NGROK_TOKEN"

没有 config 文件就什么都不输出(调用方用自己的默认值)。
被 start.sh / run-mcp.sh / run-hub.sh / run-ngrok.sh 共用, 避免四处重复解析。
"""
from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def strip_jsonc(t: str) -> str:
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


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    cands = [path] if path else [BASE / "config.jsonc", BASE / "config.json"]
    data = None
    for p in cands:
        if p and p.is_file():
            try:
                data = json.loads(strip_jsonc(p.read_text(encoding="utf-8")))
                break
            except Exception as e:
                print(f'echo "[read-config] ⚠️ 解析失败 {p}: {e}" >&2')
                return
    if data is None:
        return

    ports = data.get("ports") or {}

    def emit(key: str, val) -> None:
        if val not in (None, ""):
            print(f"{key}={shlex.quote(str(val))}")

    emit("MODE", str(data.get("mode", "child")).strip().lower())
    emit("CFG_MCP_PORT", ports.get("mcp"))
    emit("CFG_HUB_PORT", ports.get("hub"))
    emit("CFG_TOKEN", data.get("auth_token") or data.get("token"))
    emit("CFG_NGROK_TOKEN", data.get("ngrok-authtoken") or data.get("ngrok_authtoken"))
    emit("CFG_NGROK_WEB_PORT", data.get("ngrok-web-port"))
    # 功能开关 (可选, 默认都开, 兼容老配置):
    #   "ngrok": false       不起公网隧道, 只跑本地 MCP (外部守护器代管隧道时用)
    #   "supervisor": false  不装 supervisor 保活 (Rikkahub scheduled_processes 代管时用)
    emit("CFG_ENABLE_NGROK", data.get("ngrok", True))
    emit("CFG_ENABLE_SUPERVISOR", data.get("supervisor", True))


if __name__ == "__main__":
    main()
