#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
# 已废弃: 隧道统一由 ngrok_tunnels.py 守护器管理 (与 MCP 解耦)。
#   - 配置: tunnels.jsonc (cp tunnels.jsonc.example tunnels.jsonc)
#   - 启动: python3 ngrok_tunnels.py
#   - 保活: supervisor 的 processes.jsonc 里 "tunnels" 条目已接管
# 本文件仅保留兼容旧 processes.jsonc, 不会再被新配置调用。
# ============================================================
echo "[run-ngrok] ⚠️ 已废弃, 隧道由 ngrok_tunnels.py 统一管理 (配置 tunnels.jsonc)"
exit 0
