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
fi

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
