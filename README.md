# Termux MCP Agent (远程 Android / Termux 控制套件)

> 纯 Python 3.8+ 标准库实现，零第三方 pip 依赖。自带标准 SSE 与 JSON-RPC 协议实现，通过 MCP (Model Context Protocol) 暴露 Linux / Android 各种底层控制能力。

---

## ⚡ 新装 Termux 一键搞定（复制这一整段直接回车）

在手机/平板新装的 Termux 中，直接粘贴运行这一段命令：

```bash
pkg update -y && pkg install -y git python curl ripgrep openssh termux-api && \
git clone https://x-access-token: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@github.com/fuwei99/termux-mcp.git ~/termux-mcp && \
cd ~/termux-mcp && \
chmod +x *.sh termux_mcp.py && \
bash install.sh && \
bash start.sh
```

---

## 🛠️ 工具清单 (11 个能力)

1. **`shell`**：执行 bash/sh 各种命令行指令，带超时与自动截断。
2. **`read_file`**：读取任意文本文件（自动兼容各种编码）。
3. **`write_file`**：写入文本文件，自动创建父级目录。
4. **`edit_file`**：精确查找并替换文件内容（支持单次与批量 edits）。
5. **`list_dir`**：列出目录结构与文件大小。
6. **`list_processes`**：列出系统当前正在运行的进程 (top N)。
7. **`system_info`**：查看系统架构、Linux 内核版本、内存占用 `free`、磁盘占用 `df`、电池电量等。
8. **`grep`**：ripgrep 极速代码/文本搜索（支持正则、文件类型过滤、上下文行）。
9. **`codex_patch`**：OpenAI Codex file-style patch 解析与应用，带自动备份与失败自动回滚。
10. **`termux_api`**：直接调用 Android 底层能力（toast 弹窗、剪贴板获取/设置、振动、通知、电池状态、WiFi 信息等）。
11. **`open_path`**：调用 `termux-open` 打开 URL 或本地文件。

---

## 🌐 配合 ngrok 内网穿透 (公网远程控制)

如果要在外网通过 Rikkahub 或其他 Agent 远程控制这台备用机：

### 1. 配置 ngrok
```bash
cd ~/termux-mcp
cp ngrok.yml.example ngrok.yml
# 编辑 ngrok.yml 填入你的 authtoken
nano ngrok.yml
```

### 2. 启动服务与隧道
```bash
bash start.sh
```
`start.sh` 会自动检测 `ngrok.yml`，启动隧道并输出你的公网 `https://xxxx.ngrok-free.dev/sse` 地址。

---

## 🔌 Rikkahub / Agent 端配置接入

在 Rikkahub 的 `/rikkahub-data/setting-json/mcp_servers.json` 或 Agent 的 MCP 配置中添加：

```json
{
  "mcpServers": {
    "termux-remote": {
      "type": "sse",
      "url": "https://<你的ngrok公网地址>.ngrok-free.dev/sse",
      "headers": {
        "Authorization": "Bearer wei123..",
        "ngrok-skip-browser-warning": "true"
      }
    }
  }
}
```

*注：如果是同一局域网内直连，url 直接填 `http://<备用机内网IP>:8996/sse` 即可。*

---

## 🔄 常用维护命令

- **启动**：`cd ~/termux-mcp && bash start.sh`
- **停止**：`cd ~/termux-mcp && bash stop.sh`
- **实时日志**：`tail -f ~/termux-mcp/logs/mcp.log`
- **开机自启**：配合 **Termux:Boot** App，`install.sh` 已自动写入 `~/.termux/boot/start-termux-mcp.sh`，手机重启即可自动后台唤醒拉起。
