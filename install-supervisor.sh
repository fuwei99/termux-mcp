#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
# 把守护器装成"开机自启 + 常驻"
#   1. 生成 processes.jsonc (若不存在, 从 example 复制, 按 mode 自动开关 hub 那条)
#   2. 改写 Termux:Boot 脚本 -> 开机只拉守护器一个进程, 其余由它负责
#   3. 立刻把守护器拉起来
#
# 装完之后: 重启服务 = pkill -f hub.py, 等 15 秒它自己回来。
# 用法: bash install-supervisor.sh
# ============================================================
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1
mkdir -p logs

chmod +x *.sh 2>/dev/null
eval "$(python3 read-config.py 2>/dev/null)"
MODE="${MODE:-child}"
SUP_ON="$(echo "${CFG_ENABLE_SUPERVISOR:-true}" | tr 'A-Z' 'a-z')"
if [ "$SUP_ON" != "true" ]; then
    echo "=== [0/4] ⚙️ config.jsonc 的 \"supervisor\": false, 跳过保活安装 ==="
    echo "      (该节点由外部守护器如 Rikkahub scheduled_processes 代管, 不需要 supervisor)"
    exit 0
fi
echo "=== [1/4] 当前 mode=$MODE ==="

# ── 生成 processes.jsonc ────────────────────────────────────
if [ ! -f "$DIR/processes.jsonc" ]; then
    cp "$DIR/processes.jsonc.example" "$DIR/processes.jsonc"
    echo "      已从 example 生成 processes.jsonc"
    if [ "$MODE" != "root" ]; then
        # 子节点不需要 hub: 把 termux-hub 那条关掉
        python3 - "$DIR/processes.jsonc" <<'PY'
import re, sys
p = sys.argv[1]
t = open(p, encoding='utf-8').read()
t = re.sub(r'("id":\s*"termux-hub",.*?)"enabled":\s*true',
           r'\1"enabled": false', t, flags=re.S)
open(p, 'w', encoding='utf-8').write(t)
print("      子节点模式: 已关闭 termux-hub 那条")
PY
    fi
else
    echo "      processes.jsonc 已存在, 保留不动"
    # 迁移: 老版本 wake-lock 用 "sleep 3600" 作判活串, 太常见容易被别的进程撞上
    # 造成"死了也判活"。换成独占的 run-wakelock.sh。
    if grep -q '"pgrep": *"sleep 3600"' "$DIR/processes.jsonc" 2>/dev/null; then
        python3 - "$DIR/processes.jsonc" <<'PY'
import sys
p = sys.argv[1]
t = open(p, encoding='utf-8').read()
t = t.replace('"command": "termux-wake-lock && sleep 3600"',
              '"command": "bash run-wakelock.sh"')
t = t.replace('"pgrep": "sleep 3600"', '"pgrep": "run-wakelock.sh"')
open(p, 'w', encoding='utf-8').write(t)
print("      已迁移 wake-lock 判活串 -> run-wakelock.sh (原 sleep 3600 易误判)")
PY
        pkill -f "sleep 3600" 2>/dev/null   # 杀掉老式 wake-lock, 让守护器用新方式重拉
    fi
    # 迁移: ngrok 判活从 port(4040) 换成 tunnel —— agent 可能进程活着、4040 在听,
    # 但隧道早已掉线(公网 ERR_NGROK_3200), 只看端口不会重启它。
    if grep -q '"port": *4040' "$DIR/processes.jsonc" 2>/dev/null; then
        python3 - "$DIR/processes.jsonc" <<'PY'
import re, sys
p = sys.argv[1]
t = open(p, encoding='utf-8').read()
t = re.sub(r'"check":\s*\{\s*"port":\s*4040\s*\}', '"check": { "tunnel": true }', t)
open(p, 'w', encoding='utf-8').write(t)
print("      已迁移 ngrok 判活 port(4040) -> tunnel (端口在听但隧道掉线时也能发现)")
PY
    fi
fi

# ── 按 ngrok 开关关掉 processes.jsonc 里的 ngrok 条目 ──────
# config.jsonc "ngrok": false = 隧道由外部守护器管, supervisor 不碰它
NGROK_ON="$(echo "${CFG_ENABLE_NGROK:-true}" | tr 'A-Z' 'a-z')"
if [ "$NGROK_ON" != "true" ] && [ -f "$DIR/processes.jsonc" ] && grep -q '"id": *"ngrok"' "$DIR/processes.jsonc"; then
    python3 - "$DIR/processes.jsonc" <<'PY'
import re, sys
p = sys.argv[1]
t = open(p, encoding='utf-8').read()
t2 = re.sub(r'("id":\s*"ngrok",.*?)"enabled":\s*true', r'\1"enabled": false', t, flags=re.S)
if t2 != t:
    open(p, 'w', encoding='utf-8').write(t2)
    print("      ⚙️ config \"ngrok\": false -> processes.jsonc 里 ngrok 条目已关闭 (交给外部守护器)")
else:
    print("      ⚙️ config \"ngrok\": false (ngrok 条目已是关闭状态)")
PY
fi

# ── 按 config 端口修正 check URL ────────────────────────────
# processes.jsonc.example 里 termux-mcp 判活硬编码 8996 / hub 硬编码 8994,
# 但 config.jsonc 的 ports.mcp / ports.hub 可能不同(如 8998)。不修会误报死。
MCP_PORT="${CFG_MCP_PORT:-8996}"
HUB_PORT="${CFG_HUB_PORT:-8994}"
python3 - "$DIR/processes.jsonc" "$MCP_PORT" "$HUB_PORT" <<'PY'
import sys
p, mp, hp = sys.argv[1], sys.argv[2], sys.argv[3]
t = open(p, encoding='utf-8').read()
t = t.replace('http://127.0.0.1:8996/health', f'http://127.0.0.1:{mp}/health')
t = t.replace('http://127.0.0.1:8994/health', f'http://127.0.0.1:{hp}/health')
open(p, 'w', encoding='utf-8').write(t)
print(f"      ✅ check URL 端口已按 config 修正 (mcp={mp}, hub={hp})")
PY

# ── Termux:Boot ─────────────────────────────────────────────
echo "=== [2/4] 配置开机自启 (Termux:Boot) ==="
BOOT_DIR="$HOME/.termux/boot"
mkdir -p "$BOOT_DIR"
# 老的 start-termux-mcp.sh 会整体重启(含 pkill ngrok), 和守护器打架, 换掉
rm -f "$BOOT_DIR/start-termux-mcp.sh"
cat > "$BOOT_DIR/00-supervisor.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
# 开机只拉守护器一个进程, 服务由它负责拉起并持续守护
termux-wake-lock 2>/dev/null
cd "$DIR" || exit 1
mkdir -p logs
# 已在跑就别重复起
if pgrep -f "supervisor.py" >/dev/null 2>&1; then exit 0; fi
nohup setsid python3 supervisor.py >> logs/supervisor.log 2>&1 < /dev/null &
EOF
chmod +x "$BOOT_DIR/00-supervisor.sh"
echo "      已写入 $BOOT_DIR/00-supervisor.sh"
echo "      (旧的 start-termux-mcp.sh 已移除, 它会 pkill ngrok 和守护器打架)"

# ── 拉起守护器 ──────────────────────────────────────────────
echo "=== [3/4] 启动守护器 ==="
if pgrep -f "supervisor.py" >/dev/null 2>&1; then
    echo "      已在运行, 先停掉旧的"
    pkill -f "supervisor.py"
    sleep 1
fi
termux-wake-lock 2>/dev/null
nohup setsid python3 supervisor.py >> logs/supervisor.log 2>&1 < /dev/null &
sleep 3
if pgrep -f "supervisor.py" >/dev/null 2>&1; then
    echo "      ✅ 守护器已启动 pid=$(pgrep -f supervisor.py | head -1)"
else
    echo "      ❌ 守护器没起来, 看 logs/supervisor.log"
    tail -n 15 logs/supervisor.log 2>/dev/null
fi

echo "=== [4/4] 等 20 秒让它把服务拉起来, 然后看状态 ==="
sleep 20
python3 supervisor.py --status

echo ""
echo "============================================================"
echo " 装好了。以后要重启服务, 直接杀就行, 15 秒内自动复活:"
echo "     pkill -f hub.py        # 母节点转发器"
echo "     pkill -f termux_mcp.py # 本机 MCP"
echo "     pkill -f ngrok         # 隧道"
echo ""
echo " 看状态:  cd $DIR && python3 supervisor.py --status"
echo " 看日志:  tail -f $DIR/logs/supervisor.log"
echo " 停守护:  pkill -f supervisor.py   (之后服务就不会自动复活了)"
echo "============================================================"
