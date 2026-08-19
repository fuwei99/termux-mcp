#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
# Termux MCP 一键启动脚本
# ============================================================

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

mkdir -p logs

PORT="${TERMUX_MCP_PORT:-8996}"
TOKEN="${TERMUX_MCP_TOKEN:-wei123..}"

# 0. 修复 Termux DNS 配置 (防止 Go/ngrok 报 [::1]:53 connection refused)
if [ -n "$PREFIX" ] && [ -d "$PREFIX/etc" ]; then
    mkdir -p "$PREFIX/etc"
    if [ ! -s "$PREFIX/etc/resolv.conf" ] || grep -q "::1" "$PREFIX/etc/resolv.conf"; then
        echo "nameserver 223.5.5.5" > "$PREFIX/etc/resolv.conf"
        echo "nameserver 119.29.29.29" >> "$PREFIX/etc/resolv.conf"
        echo "nameserver 8.8.8.8" >> "$PREFIX/etc/resolv.conf"
    fi
fi

# 1. 检查并彻底停止旧进程
pkill -9 -f "termux_mcp.py" >/dev/null 2>&1
pkill -9 -f "ngrok" >/dev/null 2>&1
sleep 1

# 2. 启动 MCP Server (后台)
echo "[1/3] 启动 Termux MCP Server (端口 $PORT)..."
python3 termux_mcp.py --port "$PORT" --token "$TOKEN" > logs/mcp.log 2>&1 &
MCP_PID=$!
echo "      MCP PID: $MCP_PID"

sleep 1

# 检查 MCP 是否正常启动 (轮询 3 次)
MCP_OK=0
for i in {1..3}; do
    sleep 1
    if curl -s "http://127.0.0.1:$PORT/health" | grep -q '"ok":true'; then
        echo "      ✅ MCP Server 本地健康检查通过: http://127.0.0.1:$PORT/health"
        MCP_OK=1
        break
    fi
done
if [ $MCP_OK -eq 0 ]; then
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
    echo "[2/3] 启动 ngrok 公网隧道 (dashmuse123 账号)..."
    pkill -9 -f "ngrok" >/dev/null 2>&1
    sleep 0.5
    
    # 若有 termux-chroot, 用它包裹以让 /etc/resolv.conf 生效, 避免 Go DNS 报 [::1]:53 错误
    if command -v termux-chroot >/dev/null 2>&1; then
        termux-chroot $NGROK_BIN start --config "$DIR/ngrok.yml" termux-mcp > "$DIR/logs/ngrok.log" 2>&1 &
    else
        $NGROK_BIN start --config "$DIR/ngrok.yml" termux-mcp > "$DIR/logs/ngrok.log" 2>&1 &
    fi
    
    # 轮询等待 ngrok 隧道建立 (最多等 8 秒)
    TUNNEL_URL=""
    for i in {1..8}; do
        sleep 1
        TUNNEL_URL=$(curl -s http://127.0.0.1:4040/api/tunnels 2>/dev/null | grep -o '"public_url":"https://[^"]*"' | head -n 1 | cut -d '"' -f 4)
        if [ -n "$TUNNEL_URL" ]; then
            break
        fi
    done

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
        echo "      ⚠️ ngrok 未能在 8 秒内建立隧道, 请查看 logs/ngrok.log:"
        tail -n 10 logs/ngrok.log 2>/dev/null
    fi
else
    echo "[2/3] ℹ️ 未发现 ngrok 二进制或 ngrok.yml"
    echo "[3/3] 局域网/本机端点: http://127.0.0.1:$PORT/sse"
fi

echo ""
echo "🚀 运行中! 日志查看: tail -f logs/mcp.log"
