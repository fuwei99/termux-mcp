#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
# Termux MCP 环境一键安装与自启配置脚本
# ============================================================

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "=== [1/6] 更新基础软件包 ==="
pkg update -y
pkg install -y python git curl tar ripgrep openssh termux-api

echo "=== [2/6] 检测并安装 ngrok 隧道工具 ==="
if ! command -v ngrok >/dev/null 2>&1 && [ ! -f "$DIR/ngrok" ]; then
    ARCH=$(uname -m)
    NGROK_URL=""
    if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
        NGROK_URL="https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-arm64.tgz"
    elif [ "$ARCH" = "arm" ] || [ "$ARCH" = "armv7l" ]; then
        NGROK_URL="https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-arm.tgz"
    elif [ "$ARCH" = "x86_64" ]; then
        NGROK_URL="https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz"
    fi

    if [ -n "$NGROK_URL" ]; then
        echo "      正在下载 ngrok ($ARCH)..."
        # 注意: Termux 的 /tmp 可能是只读, 下载到项目目录再解压
        NGROK_TGZ="$DIR/ngrok.tgz"
        curl -sSL "$NGROK_URL" -o "$NGROK_TGZ"
        tar -xzf "$NGROK_TGZ" -C "$DIR"
        rm -f "$NGROK_TGZ"
        chmod +x "$DIR/ngrok"
        
        # 尝试软链接到 PATH
        if [ -n "$PREFIX" ] && [ -d "$PREFIX/bin" ]; then
            ln -sf "$DIR/ngrok" "$PREFIX/bin/ngrok" || true
        fi
        echo "      ✅ ngrok 安装完成"
    else
        echo "      ⚠️ 未知架构: $ARCH, 请手动下载对应版本的 ngrok"
    fi
else
    echo "      ✅ ngrok 已存在，跳过下载"
fi

echo "=== [3/6] 开启后台常驻 (Wake Lock) ==="
termux-wake-lock || true

echo "=== [4/6] 赋予脚本执行权限 ==="
chmod +x start.sh stop.sh termux_mcp.py

echo "=== [5/6] 配置 Termux:Boot 开机自启动 (可选) ==="
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

echo "=== [6/6] 安装完成! ==="
echo ""
echo "常用操作:"
echo "  1. 启动服务(含公网穿透): bash start.sh"
echo "  2. 停止服务:             bash stop.sh"
echo "  3. 查看实时日志:         tail -f logs/mcp.log"
echo ""
