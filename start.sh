#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
# Termux MCP 一键启动脚本 (支持母节点 / 子节点两种模式)
#
#   config.jsonc 的 mode 字段决定行为 (没有 config.jsonc = child, 与老版本完全一致):
#     child : 只跑 termux_mcp.py, ngrok 暴露 ports.mcp (默认 8996)
#     root  : 跑 termux_mcp.py + hub.py, ngrok 暴露 ports.hub (默认 8994)
#             Rikkahub 只连母节点一个端点, 由它按 device 参数转发到所有子节点
#
#   环境变量可覆盖: TERMUX_MCP_PORT / TERMUX_HUB_PORT / TERMUX_MCP_TOKEN
# ============================================================

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

mkdir -p logs

# ── 读 config.jsonc (mode / ports / auth_token) ─────────────
# 用 python 解析, 顺带剥掉 jsonc 注释; 没有配置文件就走默认 child
CFG_FILE=""
[ -f "$DIR/config.jsonc" ] && CFG_FILE="$DIR/config.jsonc"
[ -z "$CFG_FILE" ] && [ -f "$DIR/config.json" ] && CFG_FILE="$DIR/config.json"

MODE="child"
CFG_MCP_PORT=""
CFG_HUB_PORT=""
CFG_TOKEN=""
CFG_NGROK_TOKEN=""
CFG_NGROK_WEB_PORT=""
if [ -n "$CFG_FILE" ]; then
    eval "$(python3 - "$CFG_FILE" <<'PYEOF'
import json, re, sys, shlex
def strip_jsonc(t):
    out, i, n = [], 0, len(t)
    while i < n:
        c = t[i]
        if c == '"':
            j = i + 1
            while j < n:
                if t[j] == '\\': j += 2; continue
                if t[j] == '"': break
                j += 1
            out.append(t[i:j+1]); i = j + 1; continue
        if t.startswith('//', i):
            j = t.find('\n', i); i = n if j < 0 else j; continue
        if t.startswith('/*', i):
            j = t.find('*/', i+2); i = n if j < 0 else j+2; continue
        out.append(c); i += 1
    return re.sub(r',(\s*[}\]])', r'\1', ''.join(out))
try:
    d = json.loads(strip_jsonc(open(sys.argv[1], encoding='utf-8').read()))
except Exception as e:
    print(f'echo "[start.sh] ⚠️ config 解析失败: {e}" >&2'); sys.exit(0)
p = d.get('ports') or {}
def emit(k, v):
    if v not in (None, ''): print(f'{k}={shlex.quote(str(v))}')
emit('MODE', str(d.get('mode', 'child')).strip().lower())
emit('CFG_MCP_PORT', p.get('mcp'))
emit('CFG_HUB_PORT', p.get('hub'))
emit('CFG_TOKEN', d.get('auth_token') or d.get('token'))
emit('CFG_NGROK_TOKEN', d.get('ngrok-authtoken') or d.get('ngrok_authtoken'))
emit('CFG_NGROK_WEB_PORT', d.get('ngrok-web-port'))
PYEOF
)"
    echo "[start.sh] 配置: $CFG_FILE  mode=$MODE"
else
    echo "[start.sh] 未找到 config.jsonc, 按子节点模式启动 (可 cp config.jsonc.example config.jsonc)"
fi

PORT="${TERMUX_MCP_PORT:-${CFG_MCP_PORT:-8996}}"
HUB_PORT="${TERMUX_HUB_PORT:-${CFG_HUB_PORT:-8994}}"
TOKEN="${TERMUX_MCP_TOKEN:-${CFG_TOKEN:-wei123..}}"
# ngrok 本地 web 面板端口: 默认 4045, 避开宿主 proot 守护器的 4040 (同机共享端口!)
NG_WEB_PORT="${TERMUX_NGROK_WEB_PORT:-${CFG_NGROK_WEB_PORT:-4045}}"

# 隧道暴露哪个端口, 由 mode 决定
if [ "$MODE" = "root" ]; then
    TUNNEL_PORT="$HUB_PORT"
    TUNNEL_HINT="母节点 Hub (转发所有设备)"
else
    TUNNEL_PORT="$PORT"
    TUNNEL_HINT="子节点 Termux MCP (本机能力)"
fi

# 0. 修复 Termux DNS 配置 (防止 Go/ngrok 报 [::1]:53 connection refused)
#    顺带把国内不可达的 1.1.1.1 挪走: glibc/Go 对首个 DNS 会死等 ~5s 才轮到下一个,
#    宿主 proot 上就因为这个让每次请求白交 5.1s 过路费 (6.1s → 0.8s 的元凶)。
if [ -n "$PREFIX" ] && [ -d "$PREFIX/etc" ]; then
    mkdir -p "$PREFIX/etc"
    if [ ! -s "$PREFIX/etc/resolv.conf" ] || grep -qE "::1|^nameserver 1\.1\.1\.1" "$PREFIX/etc/resolv.conf"; then
        printf 'nameserver 223.5.5.5\nnameserver 119.29.29.29\nnameserver 8.8.8.8\noptions timeout:1 attempts:2\n' > "$PREFIX/etc/resolv.conf"
    fi
fi

# 1. 检查并彻底停止旧进程
pkill -9 -f "termux_mcp.py" >/dev/null 2>&1
pkill -9 -f "hub.py" >/dev/null 2>&1
# 只杀自己管理的 ngrok (匹配 runtime 配置), 不碰宿主 proot 守护器的 ngrok
pkill -9 -f "ngrok.runtime.yml" >/dev/null 2>&1
sleep 1

# 2. 启动 MCP Server (后台) —— 两种模式都要, 母节点自己也是一台设备
echo "[1/4] 启动 Termux MCP Server (端口 $PORT)..."
python3 termux_mcp.py --port "$PORT" --token "$TOKEN" > logs/mcp.log 2>&1 &
echo "      MCP PID: $!"

sleep 1

MCP_OK=0
for i in {1..3}; do
    sleep 1
    if curl -s "http://127.0.0.1:$PORT/health" | grep -q '"ok":true'; then
        echo "      ✅ MCP Server 健康检查通过: http://127.0.0.1:$PORT/health"
        MCP_OK=1
        break
    fi
done
[ $MCP_OK -eq 0 ] && echo "      ⚠️ MCP Server 似乎未就绪, 请检查 logs/mcp.log"

# 3. 母节点模式: 再拉起 hub.py
if [ "$MODE" = "root" ]; then
    echo "[2/4] 启动 Termux MCP Hub 母节点 (端口 $HUB_PORT)..."
    if [ ! -f "$DIR/hub.py" ]; then
        echo "      ❌ 找不到 hub.py, 请先 git pull"
    else
        python3 hub.py --port "$HUB_PORT" > logs/hub.log 2>&1 &
        echo "      HUB PID: $!"
        HUB_OK=0
        for i in {1..5}; do
            sleep 1
            if curl -s "http://127.0.0.1:$HUB_PORT/health" | grep -q '"ok":true'; then
                echo "      ✅ Hub 健康检查通过: http://127.0.0.1:$HUB_PORT/health"
                HUB_OK=1
                break
            fi
        done
        [ $HUB_OK -eq 0 ] && echo "      ⚠️ Hub 未就绪, 请检查 logs/hub.log"
    fi
else
    echo "[2/4] 子节点模式, 跳过 Hub"
fi

# 4. ngrok 隧道 —— 暴露 $TUNNEL_PORT
NGROK_BIN=""
if command -v ngrok >/dev/null 2>&1; then
    NGROK_BIN="ngrok"
elif [ -x "$DIR/ngrok" ]; then
    NGROK_BIN="$DIR/ngrok"
fi

if [ -n "$NGROK_BIN" ]; then
    echo "[3/4] 启动 ngrok 隧道 -> :$TUNNEL_PORT  ($TUNNEL_HINT)"
    pkill -9 -f "ngrok.runtime.yml" >/dev/null 2>&1
    sleep 0.5

    # 隧道配置: 有 config 的 ngrok-authtoken 就现生成一份 ngrok.runtime.yml,
    # 否则沿用仓库自带的 ngrok.yml (但 addr 需按 mode 改, 所以同样重写一份)
    NG_CFG="$DIR/ngrok.runtime.yml"
    NG_TOKEN="$CFG_NGROK_TOKEN"
    if [ -z "$NG_TOKEN" ] && [ -f "$DIR/ngrok.yml" ]; then
        NG_TOKEN="$(grep -E '^authtoken:' "$DIR/ngrok.yml" | head -1 | sed 's/^authtoken:[[:space:]]*//')"
    fi
    NG_REGION="ap"
    [ -f "$DIR/ngrok.yml" ] && NG_REGION="$(grep -E '^region:' "$DIR/ngrok.yml" | head -1 | sed 's/^region:[[:space:]]*//')"
    [ -z "$NG_REGION" ] && NG_REGION="ap"

    if [ -n "$NG_TOKEN" ]; then
        cat > "$NG_CFG" <<EOF
# 本文件由 start.sh 按 config.jsonc 的 mode 自动生成, 改它没用, 改 config.jsonc
version: "2"
authtoken: $NG_TOKEN
region: $NG_REGION
web_addr: 127.0.0.1:$NG_WEB_PORT
log: stdout
log_level: info

tunnels:
  termux-mcp:
    proto: http
    addr: $TUNNEL_PORT
EOF
        if command -v termux-chroot >/dev/null 2>&1; then
            termux-chroot $NGROK_BIN start --config "$NG_CFG" termux-mcp > "$DIR/logs/ngrok.log" 2>&1 &
        else
            $NGROK_BIN start --config "$NG_CFG" termux-mcp > "$DIR/logs/ngrok.log" 2>&1 &
        fi

        TUNNEL_URL=""
        for i in {1..10}; do
            sleep 1
            TUNNEL_URL=$(curl -s "http://127.0.0.1:$NG_WEB_PORT/api/tunnels" 2>/dev/null | grep -o '"public_url":"https://[^"]*"' | head -n 1 | cut -d '"' -f 4)
            [ -n "$TUNNEL_URL" ] && break
        done

        if [ -n "$TUNNEL_URL" ]; then
            echo "[4/4] 🌐 公网地址: $TUNNEL_URL/sse"
            echo ""
            echo "============================================================"
            echo " Rikkahub 添加此 MCP 即可接入 ($TUNNEL_HINT):"
            echo ""
            echo " URL:     $TUNNEL_URL/sse"
            echo " Headers:"
            echo "   Authorization: Bearer $TOKEN"
            echo "   ngrok-skip-browser-warning: true"
            if [ "$MODE" = "root" ]; then
                echo ""
                echo " ⚠️ 母节点模式: 所有设备都通过这一个端点操作(工具带 device 参数),"
                echo "    Rikkahub 里各子节点的独立 MCP 可以停用了。"
            fi
            echo "============================================================"
        else
            echo "      ⚠️ ngrok 未能在 10 秒内建立隧道, 查看 logs/ngrok.log:"
            tail -n 10 logs/ngrok.log 2>/dev/null
        fi
    else
        echo "      ⚠️ 未找到 ngrok authtoken (config.jsonc 的 ngrok-authtoken 或 ngrok.yml)"
    fi
else
    echo "[3/4] ℹ️ 未发现 ngrok 二进制"
    echo "[4/4] 本机端点: http://127.0.0.1:$TUNNEL_PORT/sse"
fi

echo ""
if [ "$MODE" = "root" ]; then
    echo "🚀 母节点运行中! 日志: tail -f logs/hub.log  /  logs/mcp.log"
else
    echo "🚀 子节点运行中! 日志: tail -f logs/mcp.log"
fi
