#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
# Termux MCP 环境一键安装与自启配置脚本
# ============================================================

set -e

echo "=== [1/5] 更新基础软件包 ==="
pkg update -y
pkg install -y python git curl ripgrep openssh termux-api

echo "=== [2/5] 开启后台常驻 (Wake Lock) ==="
termux-wake-lock || true

echo "=== [3/5] 给脚本赋予执行权限 ==="
chmod +x start.sh stop.sh termux_mcp.py

echo "=== [4/5] 配置 Termux:Boot 开机自启动 (可选) ==="
BOOT_DIR="$HOME/.termux/boot"
mkdir -p "$BOOT_DIR"
cat << 'EOF' > "$BOOT_DIR/start-termux-mcp.sh"
#!/data/data/com.termux/files/usr/bin/bash
# 开机保持后台唤醒
termux-wake-lock
# 进入项目目录并启动 MCP
DIR="$HOME/termux-mcp"
if [ -d "$DIR" ]; then
    cd "$DIR"
    bash start.sh
fi
EOF
chmod +x "$BOOT_DIR/start-termux-mcp.sh"
echo "      ✅ Termux:Boot 自启脚本已写入 ~/.termux/boot/start-termux-mcp.sh"

echo "=== [5/5] 安装完成! ==="
echo ""
echo "常用操作:"
echo "  1. 启动服务: bash start.sh"
echo "  2. 停止服务: bash stop.sh"
echo "  3. 查看日志: tail -f logs/mcp.log"
echo "  4. 配置公网: 复制 ngrok.yml.example 为 ngrok.yml 并填入 authtoken"
echo ""
