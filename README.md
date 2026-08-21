# Termux MCP Agent (远程 Android / Termux 控制套件)

> 纯 Python 3.8+ 标准库实现，零第三方 pip 依赖。自带标准 SSE 与 JSON-RPC 协议实现，通过 MCP (Model Context Protocol) 暴露 Linux / Android 底层控制能力。已内置预配置好的 ngrok 穿透密钥。

---

## ⚡ 新装 Termux 一键搞定（复制整段回车即可）

在备用机新装的 Termux 中，**直接粘贴这一整段命令回车**：

```bash
pkg update -y && pkg install -y git python curl tar ripgrep openssh termux-api && \
git clone https://x-access-token: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@github.com/fuwei99/termux-mcp.git ~/termux-mcp && \
cd ~/termux-mcp && \
chmod +x *.sh termux_mcp.py && \
bash install.sh && \
bash start.sh
```

> **说明**：该命令会自动完成：
> 1. 安装基础依赖；
> 2. 自动检测 CPU 架构并下载安装 `ngrok` 二进制；
> 3. 锁定 WakeLock（后台不休眠）；
> 4. 配置 Termux:Boot 开机自启；
> 5. 启动 MCP Server 并自动通过内置的 `lovevertex159` ngrok 隧道拉起公网，并在终端打印出公网访问地址！

---

## 🛠️ 工具清单 (mcp 9 个 + 母节点 devices = 10)

1. **`shell`**：一次性执行 bash/sh 命令(subprocess, 跑完即销)。适合无状态命令。
2. **`shell_session`**：常驻 pty bash 会话，cd/export/变量在同 session_id 间持久；哨兵协议，超时 output_offset 续读，interrupt 发 Ctrl-C。
3. **`read_file`**：读取任意文本文件（自动兼容各种编码）。
4. **`write_file`**：写入文本文件，自动创建父目录，**覆盖前自动备份**。
5. **`edit_file`**：精确查找替换（支持单次/批量 edits），**改动前自动备份**。
6. **`grep`**：ripgrep 极速代码/文本搜索。
7. **`codex_patch`**：Codex file-style patch，**自动备份**，失败可回滚。
8. **`termux_api`**：Android 底层能力（toast、剪贴板、振动、通知、电池、WiFi 等）。
9. **`backup`**：后悔药——`list` 列出 write/edit/codex_patch 的自动备份，`restore` 用 backup_id 回滚（可只滚指定文件）。
10. **`devices`**（母节点 hub 层）：列出已接入设备及在线状态。

> `ls`/`ps`/`uname`/`free`/`df` 用 `shell`；打开文件/URL 用 `shell` 跑 `termux-open`/`xdg-open`。
> 备份存于 `~/.rikkahub/backups/<backupId>/`（manifest.json + files/），对齐 RikkaHub workspace 格式。

---

## 🔌 Rikkahub / Agent 端接入配置

`start.sh` 执行后会在终端打印出你的公网地址，在 Rikkahub 的 `/rikkahub-data/setting-json/mcp_servers.json` 中添加：

```json
{
  "type": "sse",
  "url": "https://<你的ngrok公网地址>.ngrok-free.dev/sse",
  "headers": {
    "Authorization": "Bearer wei123..",
    "ngrok-skip-browser-warning": "true"
  }
}
```

---

## 🔄 常用维护命令

- **启动**：`cd ~/termux-mcp && bash start.sh`
- **停止**：`cd ~/termux-mcp && bash stop.sh`
- **实时日志**：`tail -f ~/termux-mcp/logs/mcp.log`
