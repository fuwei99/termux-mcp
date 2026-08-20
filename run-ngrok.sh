#!/data/data/com.termux/files/usr/bin/bash
# 只跑 ngrok 隧道。暴露哪个端口由 config.jsonc 的 mode 决定:
#   root  -> ports.hub  (母节点, Rikkahub 连它一个端点管所有设备)
#   child -> ports.mcp  (子节点, 只暴露本机能力)
# 给 supervisor.py 用: ngrok 是最容易被 HyperOS 杀的, 守护它是重点。
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1
mkdir -p logs

eval "$(python3 read-config.py 2>/dev/null)"
MODE="${MODE:-child}"
PORT="${TERMUX_MCP_PORT:-${CFG_MCP_PORT:-8996}}"
HUB_PORT="${TERMUX_HUB_PORT:-${CFG_HUB_PORT:-8994}}"

if [ "$MODE" = "root" ]; then
  TUNNEL_PORT="$HUB_PORT"
else
  TUNNEL_PORT="$PORT"
fi

# ngrok agent 活着就会监听 4040, 已在跑就别重复起(免费号同 token 只允许一条在线)
if curl -s -m 3 http://127.0.0.1:4040/api/tunnels >/dev/null 2>&1; then
  echo "[run-ngrok] 4040 已在监听, 隧道在跑, 退出"
  exit 0
fi

# DNS: Go 的解析器碰上不可达的首个 nameserver 会死等, 顺手校正
if [ -n "$PREFIX" ] && [ -d "$PREFIX/etc" ]; then
  if [ ! -s "$PREFIX/etc/resolv.conf" ] || grep -qE "::1|^nameserver 1\.1\.1\.1" "$PREFIX/etc/resolv.conf"; then
    printf 'nameserver 223.5.5.5\nnameserver 119.29.29.29\nnameserver 8.8.8.8\noptions timeout:1 attempts:2\n' > "$PREFIX/etc/resolv.conf"
  fi
fi

NGROK_BIN=""
if command -v ngrok >/dev/null 2>&1; then
  NGROK_BIN="ngrok"
elif [ -x "$DIR/ngrok" ]; then
  NGROK_BIN="$DIR/ngrok"
else
  echo "[run-ngrok] ❌ 找不到 ngrok 二进制"
  exit 1
fi

# token: config.jsonc 优先, 否则沿用仓库 ngrok.yml 里的
NG_TOKEN="$CFG_NGROK_TOKEN"
NG_REGION="ap"
if [ -f "$DIR/ngrok.yml" ]; then
  [ -z "$NG_TOKEN" ] && NG_TOKEN="$(grep -E '^authtoken:' "$DIR/ngrok.yml" | head -1 | sed 's/^authtoken:[[:space:]]*//')"
  R="$(grep -E '^region:' "$DIR/ngrok.yml" | head -1 | sed 's/^region:[[:space:]]*//')"
  [ -n "$R" ] && NG_REGION="$R"
fi
if [ -z "$NG_TOKEN" ]; then
  echo "[run-ngrok] ❌ 没有 authtoken (config.jsonc 的 ngrok-authtoken 或 ngrok.yml)"
  exit 1
fi

NG_CFG="$DIR/ngrok.runtime.yml"
cat > "$NG_CFG" <<EOF
# 由 run-ngrok.sh 按 config.jsonc 的 mode 自动生成, 改它没用, 改 config.jsonc
version: "2"
authtoken: $NG_TOKEN
region: $NG_REGION
web_addr: 127.0.0.1:4040
log: stdout
log_level: info

tunnels:
  termux-mcp:
    proto: http
    addr: $TUNNEL_PORT
EOF

echo "[run-ngrok] mode=$MODE 隧道 -> :$TUNNEL_PORT"
if command -v termux-chroot >/dev/null 2>&1; then
  exec termux-chroot "$NGROK_BIN" start --config "$NG_CFG" termux-mcp >> logs/ngrok.log 2>&1
else
  exec "$NGROK_BIN" start --config "$NG_CFG" termux-mcp >> logs/ngrok.log 2>&1
fi
