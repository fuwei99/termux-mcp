#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
# 停止 Termux MCP 及 ngrok 隧道
# ============================================================

echo "正在停止 Termux MCP Server..."
pkill -f "termux_mcp.py" >/dev/null 2>&1

echo "正在停止 ngrok..."
pkill -f "ngrok start" >/dev/null 2>&1
pkill -f "ngrok http" >/dev/null 2>&1

echo "✅ 已全部停止。"
