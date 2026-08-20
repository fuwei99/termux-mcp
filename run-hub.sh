#!/data/data/com.termux/files/usr/bin/bash
# 只跑 hub.py (母节点转发器), 端口从 config.jsonc 读。
# 子节点不需要它 —— processes.jsonc 里把 termux-hub 的 enabled 改 false。
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1
mkdir -p logs

eval "$(python3 read-config.py 2>/dev/null)"
MODE="${MODE:-child}"
HUB_PORT="${TERMUX_HUB_PORT:-${CFG_HUB_PORT:-8994}}"

if [ "$MODE" != "root" ]; then
  echo "[run-hub] mode=$MODE 不是 root, 无需 hub, 退出"
  exit 0
fi
if [ ! -f "$DIR/hub.py" ]; then
  echo "[run-hub] ❌ 找不到 hub.py, 先 git pull"
  exit 1
fi
if curl -s -m 3 "http://127.0.0.1:$HUB_PORT/health" | grep -q '"ok":true'; then
  echo "[run-hub] :$HUB_PORT 已在运行, 退出"
  exit 0
fi

echo "[run-hub] 启动 hub.py :$HUB_PORT"
exec python3 hub.py --port "$HUB_PORT" >> logs/hub.log 2>&1
