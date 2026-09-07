# GDrive Context Cart 

A lightweight visual context staging bridge connecting cloud storage and AI coding agents (Hermes, Claude Code, Cursor, OpenClaw, Codex, etc.).

---

## English

### Overview

Most agent integrations for cloud drives (such as headless CLI tools or MCP servers) rely exclusively on programmatic API calls or keyword searches. When a workspace contains thousands of documents, an agent searching blind wastes significant context tokens and frequently retrieves outdated or incorrect revisions.

`gdrive-cart` implements a **Context Staging Basket** architecture:
1. **Human Curation**: Rapid visual navigation, search, and selection of relevant files and folders in a clean local web interface.
2. **Machine Processing**: Queued batch downloading, automatic format conversion (Docs to Markdown, Sheets to CSV), recursive directory resolution, and compilation of a structured `_context_manifest.md`.
3. **Seamless Hand-off**: Automatic generation and copying of the local path hand-off text to the system clipboard, allowing users to paste (`Ctrl + V`) directly into their agent interface without disrupting prompt composition.

### Upstream Dependency & Companion Architecture

`gdrive-cart` is designed as a **companion visual tool** rather than a standalone authentication stack. It builds directly upon the official **`google-workspace`** skill (by Nous Research):

- **Authentication Delegation**: It intentionally does not re-implement the Google Cloud OAuth PKCE verification flow. Instead, it delegates credential creation and refresh to the upstream `google-workspace` setup, consuming the resulting `google_token.json`.
- **Separation of Concerns**: The upstream `google-workspace` skill provides headless function-calling tools; `gdrive-cart` provides the human-facing visual staging layer, format conversion, and context distillation.
- **Standalone Usage**: In non-Hermes environments (such as Claude Code or Cursor), users simply place an authorized `google_token.json` in any standard discoverable location (e.g., `~/.config/gdrive/` or `~/.hermes/`).

### Key Features

- **Format Downgrading**: Automatically converts Google Docs into clean GitHub-flavored Markdown (`.md`) and Google Sheets into standard CSV (`.csv`), significantly reducing token consumption and improving LLM parsing accuracy.
- **Context Manifest**: Generates an authoritative `_context_manifest.md` and `_manifest.json` in the target folder, cataloging original filenames, remote modification timestamps, web links, and conversion metadata.
- **Harness Agnostic**: Works with any agent harness (Hermes, Claude Code, Cursor, Windsurf, OpenClaw). Communication is handled via standard file system paths and system clipboard.
- **Local Directory Browser**: Includes an in-browser Windows-style directory explorer and support for native OS folder picker dialogs.
- **Zero Heavy Dependencies**: Built entirely with Python standard library (`http.server`) and the official Google API client (`google-api-python-client`). No Node.js or database runtime required.
- **Bilingual UI**: Full bilingual support (English and Chinese) with persistent language preferences.

### Architecture & Token Discovery

The server resolves Google OAuth credentials by checking the following locations in order:
1. `$GDRIVE_TOKEN_PATH` (custom environment variable)
2. `$HERMES_HOME/google_token.json` (Hermes profile directory)
3. `~/.hermes/google_token.json`
4. `~/.config/gdrive/google_token.json`
5. `~/.gdrive/google_token.json`
6. Local `<script_dir>/google_token.json` or parent directory

### Getting Started

#### Prerequisites
- Python 3.9+
- `google-api-python-client` and `google-auth-oauthlib` installed.
- A valid `google_token.json` authorized with Google Drive API scopes (generated via `google-workspace` setup or standard OAuth desktop flow).

#### Setup Flow
1. If using Hermes, ensure the upstream skill is authenticated:
   ```bash
   python <skills_path>/productivity/google-workspace/scripts/setup.py --check
   ```
2. Start the context cart server:
   ```bash
   python scripts/server.py
   ```
   Or on Windows, double-click `scripts/run.bat`.
3. Open `http://127.0.0.1:8765` in your browser.
4. Browse or search files and folders by name, click `[+ Add]` to place items in the staging cart.
5. Select or create your local destination folder.
6. Click **Stage Context & Copy Path**.
7. Switch to your agent chat or terminal, press `Ctrl + V`, and append your task instructions.

### Security & Threat Model

- **Local Trust Boundary**: `gdrive-cart` binds exclusively to loopback (`127.0.0.1:8765`) and assumes a fully trusted single-user host. Local file system traversal and staging operations operate under the privileges of the running process.
- **Do Not Bind to Public Interfaces**: Never bind the host to `0.0.0.0` or expose the port to external networks via port-forwarding or reverse tunnels without an upstream authentication reverse proxy.
- **Multi-Tenant Machines**: Avoid running this tool unattended on shared multi-user workstations where untrusted local users have shell access to the loopback interface.

---

## 中文说明

### 概述

现有的云盘 Agent 工具（如无界面的 CLI 或 MCP 工具）大多完全依赖模型进行盲搜。当云盘积累了大量历史文件时，纯靠关键词检索既消耗大量上下文 Token，又极易检索到错误或过期的版本。

`gdrive-cart` 采用了**上下文素材暂存篮（Context Staging Basket）**的人机协同模式：
1. **人工视觉初筛**：利用极简的本地 Web 界面，由人类在数秒内快速点选当前任务所需的关键文件与文件夹。
2. **机器流水线转译**：在后台自动排队下载、格式清洗降级（Docs 转 Markdown，Sheets 转 CSV）、子目录递归展开，并在目标目录生成结构化的 `_context_manifest.md` 清单。
3. **无缝引流交接**：下载完成后自动将本地路径提示词写入系统剪贴板，用户切回 Agent 输入框直接按 `Ctrl + V`，即可无缝续写后续任务指令。

### 上游依赖与定位关系

`gdrive-cart` 在架构上被设计为官方 **`google-workspace`** 技能（Nous Research）的**可视化伴生工具（Companion Skill）**，而非重复造轮子的独立认证栈：

- **认证委托设计**：本项目不重复实现复杂的 Google Cloud OAuth PKCE 引导逻辑，而是直接借用并复用 `google-workspace` 已经建立的认证结果（`google_token.json`）。
- **职责边界清晰**：上游 `google-workspace` 负责 Headless CLI 操作与底层 Token 自动续期；`gdrive-cart` 负责外置的人工视觉素材篮、格式自动降级与任务清单编译。
- **独立环境兼容**：在未安装 `google-workspace` 的其他 Harness 环境（如 Claude Code、Cursor）中，仅需将已授权的 `google_token.json` 放置在任一标准探测路径（如 `~/.config/gdrive/`）即可直接运行。

### 核心特性

- **面向 Agent 的格式原生降级**：下载时自动将 Google Docs 导出为纯 Markdown (`.md`)、Google Sheets 导出为标准 CSV (`.csv`)，消除了二进制与私有排版开销，大幅节省 Token 并提升模型解析稳定性。
- **任务上下文清单 (Manifest)**：在落盘目录中自动生成 `_context_manifest.md` 与 `_manifest.json`，完整记录原始云端文件名、修改时间、原链接与转译信息，使 Agent 打开目录第一眼即可掌握全局结构。
- **跨 Harness 通用兼容**：不绑定特定 Agent 框架，通过标准操作系统文件路径和系统剪贴板交接，无缝支持 Hermes、Claude Code、Cursor、Windsurf、OpenClaw 等任意终端或客户端。
- **双通道本地目录选择器**：网页内置仿 Windows 资源管理器的直观文件浏览器（支持点选层级与即时新建文件夹），同时支持一键呼出 Windows 系统原生文件选择窗口。
- **零冗余单文件架构**：基于 Python 标准库 (`http.server`) 与官方 `google-api-python-client`，不引入 Node.js、npm 或任何重型数据库。
- **双语界面**：提供完整的中英文双语界面切换，并通过 `localStorage` 自动记忆用户偏好。

### 凭据自动发现机制

服务会按以下优先级自动探测并复用本地的 `google_token.json`：
1. 环境变量 `$GDRIVE_TOKEN_PATH`
2. `$HERMES_HOME/google_token.json`
3. `~/.hermes/google_token.json`
4. `~/.config/gdrive/google_token.json`
5. `~/.gdrive/google_token.json`
6. 脚本同级目录或上级目录中的 `google_token.json`

### 快速开始

#### 环境要求
- Python 3.9+
- 安装依赖库：`pip install google-api-python-client google-auth-oauthlib`
- 机器上已具备通过桌面应用模式授权的 `google_token.json`。

#### 使用步骤
1. 若在 Hermes 环境下，先确认上游技能认证有效：
   ```bash
   python <skills_path>/productivity/google-workspace/scripts/setup.py --check
   ```
2. 启动本地服务：
   ```bash
   python scripts/server.py
   ```
   Windows 环境下亦可直接双击 `scripts/run.bat`。
3. 浏览器访问 `http://127.0.0.1:8765`。
4. 浏览或按名称搜索文件与文件夹，点击 `[+ 加入]` 放入待选素材篮。
5. 确认或通过浏览弹窗选择/新建本地目标文件夹。
6. 点击 **一键拉取素材并复制路径**。
7. 切回 Agent 聊天框或终端，按 `Ctrl + V` 粘贴路径并敲入任务需求。

### 安全与威胁模型说明

- **本地信任假设**：`gdrive-cart` 默认仅监听本地环回地址 (`127.0.0.1:8765`)，设计前提为**完全信任当前操作系统与同机单用户环境**。本地文件目录浏览与建目录等操作均继承运行该服务的系统进程权限。
- **严禁暴露公网**：切勿将监听地址绑定至 `0.0.0.0`，亦不要在缺少鉴权反向代理的前提下通过端口映射或内网穿透（如 ngrok/frp）暴露至外部网络。
- **多租户与共享环境**：请勿在有不受信本地用户的共享开发机或实验室机器上长期常驻本服务。

---

## License

MIT License (c) 2026 wyuebei-cloud
