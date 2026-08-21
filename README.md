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

## 🛠️ 工具清单 (8 个能力)

1. **`shell`**：执行 bash/sh 命令行指令，带超时、工作目录切换与自动截断。
2. **`read_file`**：读取任意文本文件（自动兼容各种编码）。
3. **`write_file`**：写入文本文件，自动创建父级目录。
4. **`edit_file`**：精确查找并替换文件内容（支持单次与批量 edits）。
5. **`grep`**：ripgrep 极速代码/文本搜索（支持正则、文件类型过滤、上下文行）。
6. **`codex_patch`**：OpenAI Codex file-style patch 解析与应用，带自动备份与失败自动回滚。
7. **`termux_api`**：直接调用 Android 底层能力（toast 弹窗、剪贴板获取/设置、振动、通知、电池状态、WiFi 信息等）。
8. **`open_path`**：调用 `termux-open` 打开 URL 或本地文件。

> `ls` / `ps` / `uname` / `free` / `df` 等系统信息查询全部用 `shell` 工具直接跑命令，不再单独封装。

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
