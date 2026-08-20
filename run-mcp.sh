#!/data/data/com.termux/files/usr/bin/bash
# 只跑 termux_mcp.py (本机能力), 端口/token 从 config.jsonc 读。
# 给 supervisor.py 用: 单一职责, 不 pkill 任何东西, 不管隧道。
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1
mkdir -p logs

eval "$(python3 read-config.py 2>/dev/null)"
PORT="${TERMUX_MCP_PORT:-${CFG_MCP_PORT:-8996}}"
TOKEN="${TERMUX_MCP_TOKEN:-${CFG_TOKEN:-wei123..}}"

# 端口已经有人听着就不重复起(守护器判活有延迟, 防双开)
if curl -s -m 3 "http://127.0.0.1:$PORT/health" | grep -q '"ok":true'; then
  echo "[run-mcp] :$PORT 已在运行, 退出"
  exit 0
fi

echo "[run-mcp] 启动 termux_mcp.py :$PORT"
exec python3 termux_mcp.py --port "$PORT" --token "$TOKEN" >> logs/mcp.log 2>&1
