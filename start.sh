#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
# Termux MCP 一键启动脚本
# ============================================================

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

mkdir -p logs

PORT="${TERMUX_MCP_PORT:-8996}"
TOKEN="${TERMUX_MCP_TOKEN:-wei123..}"

# 1. 检查并停止旧进程
pkill -f "termux_mcp.py" >/dev/null 2>&1
sleep 0.5

# 2. 启动 MCP Server (后台)
echo "[1/3] 启动 Termux MCP Server (端口 $PORT)..."
python3 termux_mcp.py --port "$PORT" --token "$TOKEN" > logs/mcp.log 2>&1 &
MCP_PID=$!
echo "      MCP PID: $MCP_PID"

sleep 1

# 检查 MCP 是否正常启动
if curl -s "http://127.0.0.1:$PORT/health" | grep -q '"ok":true'; then
    echo "      ✅ MCP Server 本地健康检查通过: http://127.0.0.1:$PORT/health"
else
    echo "      ⚠️ MCP Server 似乎未就绪, 请检查 logs/mcp.log"
fi

# 3. 检查是否有 ngrok 工具与配置
NGROK_BIN=""
if command -v ngrok >/dev/null 2>&1; then
    NGROK_BIN="ngrok"
elif [ -x "$DIR/ngrok" ]; then
    NGROK_BIN="$DIR/ngrok"
fi

if [ -f "ngrok.yml" ] && [ -n "$NGROK_BIN" ]; then
    echo "[2/3] 启动 ngrok 公网隧道 (lovevertex159 账号)..."
    pkill -f "ngrok.*start" >/dev/null 2>&1
    pkill -f "ngrok.*$PORT" >/dev/null 2>&1
    $NGROK_BIN start --config ngrok.yml termux-mcp > logs/ngrok.log 2>&1 &
    sleep 3
    
    # 尝试读取 ngrok 隧道公网地址
    TUNNEL_URL=$(curl -s http://127.0.0.1:4040/api/tunnels | grep -o '"public_url":"https://[^"]*"' | head -n 1 | cut -d '"' -f 4)
    if [ -n "$TUNNEL_URL" ]; then
        echo "[3/3] 🌐 公网访问地址: $TUNNEL_URL/sse"
        echo ""
        echo "============================================================"
        echo " Agent 端 (Rikkahub) 直接添加此项配置即可接入:"
        echo ""
        echo " URL:     $TUNNEL_URL/sse"
        echo " Headers:"
        echo "   Authorization: Bearer $TOKEN"
        echo "   ngrok-skip-browser-warning: true"
        echo "============================================================"
    else
        echo "      ℹ️ ngrok 已后台启动, 如需查看公网地址可执行: curl -s 127.0.0.1:4040/api/tunnels"
    fi
else
    echo "[2/3] ℹ️ 未发现 ngrok 二进制或 ngrok.yml"
    echo "[3/3] 局域网/本机端点: http://127.0.0.1:$PORT/sse"
fi

echo ""
echo "🚀 运行中! 日志查看: tail -f logs/mcp.log"
