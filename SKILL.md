---
name: gdrive-cart
description: "Use when visually staging files from GDrive into a local task context cart with format conversion."
version: 1.1.0
category: productivity
platforms: [windows, linux, macos]
required_credential_files:
  - path: google_token.json
    description: OAuth token from google-workspace / any Discoverable location
metadata:
  hermes:
    tags: [gdrive, context-cart, staging, web-ui, file-manager, markdown-downgrade]
    related_skills: [google-workspace]
---

# GDrive Context Cart (Context Bridge)

A portable local visual staging bridge between GDrive and any agent harness (Hermes, Claude Code, Cursor, OpenClaw, ...). Turns cloud storage into a visual staging basket: users visually select files and folders in an intuitive web UI (Scheme A: compact folder cards + file table), batch-download them into an agent-friendly local directory with automatic format downgrading (Docs to Markdown, Sheets to CSV), generate a structured `_context_manifest.md`, and copy a seamless prompt hand-off to the clipboard.

Reuses OAuth tokens discovered from standard locations — no third-party services, extra accounts, or duplicate authentication.

## Scripts & Tools

- `scripts/server.py` — Local HTTP server (Python stdlib + `google-api-python-client`) bound to `127.0.0.1:8765`. Provides GDrive explorer, search, cart staging, batch download, format conversion, and manifest generation.
- `scripts/run.bat` — Windows double-click launcher.

## Token Discovery (resolve_token_path)

The server searches for `google_token.json` in this order:

1. `$GDRIVE_TOKEN_PATH` env var (if set and file exists)
2. `$HERMES_HOME/google_token.json` (Hermes profile dir)
3. `~/.hermes/google_token.json`
4. `~/.config/gdrive/google_token.json`
5. `~/.gdrive/google_token.json`
6. `<script_dir>/google_token.json`
7. `<script_dir>/../google_token.json` (skill root)

First match wins. Any machine that completed a standard Google OAuth flow in any of these locations works out of the box.

## Workflow

### 1. Check / Ensure Authentication
Verify that a token is discoverable (auth step is delegated — run once per machine):
```bash
python "C:/Users/ywang/AppData/Local/hermes/skills/productivity/google-workspace/scripts/setup.py" --check
```
If not authenticated, complete the OAuth flow via `google-workspace` on that machine.

### 2. Launch Cart Server
When the user asks to open/launch GDrive Cart / Context Bridge:
1. Check if port `8765` is already listening.
2. If not running, start the server in the background:
   ```bash
   python "C:/Users/ywang/AppData/Local/hermes/skills/productivity/gdrive-cart/scripts/server.py"
   ```
3. Direct the user to the web UI at `http://127.0.0.1:8765`.

### 3. User Selection & Context Hand-Off
1. User browses/searches files or folders in the web UI.
2. User clicks `[+ 加入]` / `[+ Add]` to stage files or whole folders into the right-hand **Context Cart**.
3. Target directory defaults to `~/Documents/Hermes/context_YYYYMMDD_HHMMSS`. User can also use the built-in Windows-style folder explorer or native system picker to select/create a folder.
4. User clicks **"🚀 一键拉取素材并复制路径"** / **"🚀 Stage Context & Copy Path"**.
5. Server downloads and converts all staged items:
   - Google Docs → clean Markdown (`.md`)
   - Google Sheets → CSV (`.csv`)
   - Google Slides → PDF (`.pdf`)
   - Other files → Raw binary
   - Folders → Recursively traversed and mirrored in subdirectories
6. Server compiles `_context_manifest.md` and `_manifest.json` in the target directory.
7. Web UI automatically copies the prompt prefix to the user's clipboard:
   - Chinese: `此次任务的上下文路径在: <target_dir>，`
   - English: `The context path for this task is: <target_dir>, `
8. User pastes (`Ctrl + V`) into any harness input and seamlessly appends their task prompt.

## Multi-Host Deployment
To replicate this workflow on another computer:
1. Complete OAuth once on that machine (e.g. via `google-workspace`).
2. Copy the `gdrive-cart` skill folder (or run `run.bat`).
3. The server auto-discovers the token by the fallback chain above.