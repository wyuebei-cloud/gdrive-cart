#!/usr/bin/env python3
"""GDrive Context Cart — Context Bridge.

A portable local web frontend and staging service that bridges Google Drive and
any agent harness (Hermes, Claude Code, Cursor, OpenClaw, ...). Users can
browse/search their Google Drive, add files/folders to a staging cart, and
batch-download them into an agent-friendly local directory with Markdown/CSV
format downgrade, an automatic _context_manifest.md, and one-click clipboard
text for seamless prompting.

Also includes an in-browser Windows-like Local Folder Explorer and native
Windows Folder Picker dialog for point-and-click selection and new folder
creation.

Reuses OAuth credentials discovered in a few standard locations (see
resolve_token_path). Standard library HTTP server + googleapiclient.
"""

from __future__ import annotations

import datetime
import json
import mimetypes
import os
import re
import string
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

def resolve_token_path() -> Path:
    env_custom = os.environ.get("GDRIVE_TOKEN_PATH")
    if env_custom and Path(env_custom).exists():
        return Path(env_custom).resolve()

    candidates = [
        Path(os.environ.get("HERMES_HOME", Path.home() / "AppData" / "Local" / "hermes")) / "google_token.json",
        Path.home() / ".hermes" / "google_token.json",
        Path.home() / ".config" / "gdrive" / "google_token.json",
        Path.home() / ".gdrive" / "google_token.json",
        Path(__file__).resolve().parent / "google_token.json",
        Path(__file__).resolve().parent.parent / "google_token.json",
    ]
    for c in candidates:
        if c.exists():
            return c.resolve()
    return candidates[0]


TOKEN_PATH = resolve_token_path()
HOST, PORT = "127.0.0.1", 8765

FOLDER_MIME = "application/vnd.google-apps.folder"
_FID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

EXPORT_MAP = {
    "application/vnd.google-apps.document": ("text/markdown", "md"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", "csv"),
    "application/vnd.google-apps.slides": ("application/pdf", "pdf"),
    "application/vnd.google-apps.drawing": ("image/png", "png"),
    "application/vnd.google-apps.script": ("application/json", "json"),
}
NODOWNLOAD = {
    FOLDER_MIME,
    "application/vnd.google-apps.form",
    "application/vnd.google-apps.shortcut",
    "application/vnd.google-apps.map",
    "application/vnd.google-apps.jam",
}

_service_lock = threading.Lock()
_cached_service = None


def get_default_context_dir() -> Path:
    home = Path(os.environ.get("USERPROFILE", Path.home()))
    now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return home / "Documents" / "Hermes" / f"context_{now_str}"


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
    return cleaned or "unnamed_file"


def format_size(bytes_num: int | None) -> str:
    if bytes_num is None:
        return "—"
    if bytes_num < 1024:
        return f"{bytes_num} B"
    if bytes_num < 1024 * 1024:
        return f"{bytes_num / 1024:.1f} KB"
    if bytes_num < 1024 * 1024 * 1024:
        return f"{bytes_num / (1024 * 1024):.1f} MB"
    return f"{bytes_num / (1024 * 1024 * 1024):.2f} GB"


def _retry(fn, attempts=3, base_delay=0.6):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            if i < attempts - 1:
                time.sleep(base_delay * (2 ** i))
    raise last


def get_service():
    """Build a cached Drive service, refreshing the token if expired."""
    global _cached_service
    with _service_lock:
        if _cached_service is not None:
            return _cached_service
        if not TOKEN_PATH.exists():
            raise RuntimeError(f"Google token not found at {TOKEN_PATH}. Run google-workspace skill setup.")
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            payload = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
            payload.update({k: v for k, v in json.loads(creds.to_json()).items() if v is not None})
            TOKEN_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        if not creds.valid:
            raise RuntimeError("Drive auth is invalid. Please re-authenticate via google-workspace skill.")
        _cached_service = build("drive", "v3", credentials=creds)
        return _cached_service


def _list_all(svc, query, page_size=1000):
    out, token = [], None
    while True:
        page = _retry(
            lambda: svc.files()
            .list(
                q=query,
                pageSize=page_size,
                fields="nextPageToken, files(id, name, mimeType, size, modifiedTime, webViewLink)",
                orderBy="folder,name_natural",
                pageToken=token,
            )
            .execute()
        )
        out.extend(page.get("files", []))
        token = page.get("nextPageToken")
        if not token:
            break
    return sorted(out, key=lambda f: (f.get("mimeType") != FOLDER_MIME, f.get("name", "").lower()))


def drive_list(folder_id):
    if folder_id != "root" and not _FID_RE.match(folder_id):
        raise RuntimeError("Bad folder id")
    return _list_all(get_service(), f"'{folder_id}' in parents and trashed = false")


def drive_search(term):
    safe = term.replace("\\", "\\\\").replace("'", "\\'")
    return _list_all(get_service(), f"name contains '{safe}' and trashed = false")


def folder_info(folder_id):
    if folder_id == "root":
        return {"id": "root", "name": "My Drive"}
    if not _FID_RE.match(folder_id):
        raise RuntimeError("Bad folder id")
    f = get_service().files().get(fileId=folder_id, fields="id, name, mimeType, parents").execute()
    return {"id": f["id"], "name": f["name"]}


def download_single_file(svc, file_id: str, dest_path: Path, mime_type: str) -> tuple[str, int]:
    """Downloads or exports a single Drive file to local path. Returns (format_desc, bytes_written)."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if mime_type in NODOWNLOAD:
        raise RuntimeError(f"Cannot download item of type {mime_type}")

    if mime_type.startswith("application/vnd.google-apps."):
        if mime_type not in EXPORT_MAP:
            raise RuntimeError(f"Unsupported Google document format: {mime_type}")
        export_mime, ext = EXPORT_MAP[mime_type]
        data = _retry(lambda: svc.files().export_media(fileId=file_id, mimeType=export_mime).execute())
        desc = f"Google {ext.upper()} (exported as .{ext})"
    else:
        data = _retry(lambda: svc.files().get_media(fileId=file_id).execute())
        desc = f"Original binary ({mime_type})"

    data_bytes = data if isinstance(data, (bytes, bytearray)) else (data.encode("utf-8") if isinstance(data, str) else b"")
    dest_path.write_bytes(data_bytes)
    return desc, len(data_bytes)


def resolve_and_download_item(svc, item: dict, target_root: Path, rel_dir: Path = Path("")) -> list[dict]:
    """Recursively processes a file or folder and downloads to target directory."""
    item_mime = item.get("mimeType", "")
    item_name = item.get("name", "unnamed")
    item_id = item["id"]

    results = []
    if item_mime == FOLDER_MIME:
        folder_clean_name = sanitize_filename(item_name)
        new_rel_dir = rel_dir / folder_clean_name
        (target_root / new_rel_dir).mkdir(parents=True, exist_ok=True)
        children = _list_all(svc, f"'{item_id}' in parents and trashed = false")
        for child in children:
            results.extend(resolve_and_download_item(svc, child, target_root, new_rel_dir))
    else:
        clean_name = sanitize_filename(item_name)
        if item_mime in EXPORT_MAP:
            _, ext = EXPORT_MAP[item_mime]
            if not clean_name.lower().endswith(f".{ext}"):
                clean_name = f"{clean_name}.{ext}"

        dest_file = target_root / rel_dir / clean_name
        desc, bytes_written = download_single_file(svc, item_id, dest_file, item_mime)
        rel_file_path = (rel_dir / clean_name).as_posix()
        results.append({
            "id": item_id,
            "original_name": item_name,
            "local_name": clean_name,
            "relative_path": rel_file_path,
            "size_bytes": bytes_written,
            "size_formatted": format_size(bytes_written),
            "format_desc": desc,
            "mime_type": item_mime,
            "modified_time": item.get("modifiedTime", ""),
            "web_link": item.get("webViewLink", f"https://drive.google.com/open?id={item_id}"),
        })
    return results


def build_manifest(target_dir: Path, downloaded_files: list[dict], account_email: str = "") -> Path:
    """Generates an agent-friendly _context_manifest.md and _manifest.json."""
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_bytes = sum(f["size_bytes"] for f in downloaded_files)

    md_lines = [
        "# 任务上下文清单 (Context Manifest)",
        "",
        f"- **生成时间**: {now_str}",
        f"- **来源平台**: Google Drive" + (f" ({account_email})" if account_email else ""),
        f"- **本地根路径**: `{target_dir}`",
        f"- **文件总数**: {len(downloaded_files)} 项 (总大小: {format_size(total_bytes)})",
        "",
        "> 提示 (Note for Agent):",
        "> 本清单汇总了由人类挑选并拉取到本地的任务上下文文件。",
        "> Google Docs 已自动转换为纯 Markdown (.md)，Google Sheets 已转为 CSV (.csv)，格式对 Agent 极度友好，可直接检索与解析。",
        "",
        "---",
        "",
        "## 文件条目明细",
        "",
    ]

    for idx, f in enumerate(downloaded_files, start=1):
        md_lines.extend([
            f"### {idx}. {f['local_name']}",
            f"- **本地相对路径**: `{f['relative_path']}`",
            f"- **文件大小**: {f['size_formatted']}",
            f"- **格式转换**: {f['format_desc']}",
            f"- **原始云端名称**: {f['original_name']}",
            f"- **云端最后修改**: {f['modified_time']}",
            f"- **云端链接**: [在 Google Drive 查看]({f['web_link']})",
            "",
        ])

    manifest_md_path = target_dir / "_context_manifest.md"
    manifest_md_path.write_text("\n".join(md_lines), encoding="utf-8")

    manifest_json_path = target_dir / "_manifest.json"
    manifest_json_path.write_text(
        json.dumps({
            "generated_at": now_str,
            "target_dir": str(target_dir),
            "account": account_email,
            "total_files": len(downloaded_files),
            "total_bytes": total_bytes,
            "files": downloaded_files,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest_md_path


# Local File Browser Utilities (Windows-friendly)

def get_windows_drives() -> list[dict]:
    drives = []
    for letter in string.ascii_uppercase:
        drive_str = f"{letter}:\\"
        p = Path(f"{letter}:/")
        try:
            if p.exists():
                drives.append({
                    "name": f"本地磁盘 ({letter}:)",
                    "path": drive_str,
                    "icon": "💾"
                })
        except Exception:
            pass
    return drives


def browse_local_dir(target_str: str) -> dict:
    home = Path(os.environ.get("USERPROFILE", Path.home()))
    default_base = home / "Documents" / "Hermes"

    if not target_str:
        p = default_base
    else:
        p = Path(target_str)

    if not p.exists():
        if p.parent.exists():
            p = p.parent
        else:
            p = default_base
    elif p.is_file():
        p = p.parent

    p = p.resolve()

    # Breadcrumbs
    parts = list(p.parts)
    breadcrumbs = []
    if parts:
        cur_acc = Path(parts[0])
        breadcrumbs.append({"name": parts[0], "path": str(cur_acc)})
        for part in parts[1:]:
            cur_acc = cur_acc / part
            breadcrumbs.append({"name": part, "path": str(cur_acc)})

    # Subfolders
    folders = []
    try:
        for entry in p.iterdir():
            try:
                if entry.is_dir() and not entry.name.startswith("."):
                    mtime = datetime.datetime.fromtimestamp(entry.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                    folders.append({
                        "name": entry.name,
                        "path": str(entry.resolve()),
                        "mtime": mtime
                    })
            except (PermissionError, OSError):
                continue
    except (PermissionError, OSError):
        pass

    folders.sort(key=lambda x: x["name"].lower())
    parent_path = str(p.parent.resolve()) if p.parent != p else ""

    quick_access = [
        {"name": "Context 工作区", "path": str(default_base), "icon": "⚡"},
        {"name": "我的文档", "path": str(home / "Documents"), "icon": "📑"},
        {"name": "桌面", "path": str(home / "Desktop"), "icon": "🖥️"},
        {"name": "下载", "path": str(home / "Downloads"), "icon": "📥"},
    ]
    quick_access.extend(get_windows_drives())

    return {
        "current": str(p),
        "parent": parent_path,
        "breadcrumbs": breadcrumbs,
        "quick_access": quick_access,
        "folders": folders,
    }


def pick_native_folder(initial_dir: str = "") -> str:
    """Invokes the native Windows folder picker dialog."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", 1)
        folder = filedialog.askdirectory(
            initialdir=initial_dir or str(get_default_context_dir().parent),
            title="选择保存目标文件夹"
        )
        root.destroy()
        return str(Path(folder).resolve()) if folder else ""
    except Exception as e:
        print("Native picker error:", e)
        return ""


def make_local_dir(parent_str: str, folder_name: str) -> str:
    """Creates a new subdirectory under parent_str."""
    parent = Path(parent_str).resolve()
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
    clean_name = sanitize_filename(folder_name)
    target = parent / clean_name
    target.mkdir(parents=True, exist_ok=True)
    return str(target.resolve())


PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>GDrive Context Cart — Context Bridge</title>
<style>
  :root {
    --fg: #1f2328;
    --bg: #f8fafc;
    --card: #ffffff;
    --muted: #64748b;
    --border: #e2e8f0;
    --accent: #2563eb;
    --accent-hover: #1d4ed8;
    --row-hover: #f1f5f9;
    --danger: #ef4444;
    --success: #10b981;
    --success-bg: #ecfdf5;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: var(--fg); background: var(--bg); height: 100vh; display: flex; flex-direction: column; overflow: hidden; }

  /* Top Navigation */
  header { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 10px 20px; border-bottom: 1px solid var(--border); background: var(--card); z-index: 10; }
  .brand { display: flex; align-items: center; gap: 8px; font-weight: 600; font-size: 15px; color: var(--fg); flex-shrink: 0; }
  .brand span.tag { background: #eff6ff; color: var(--accent); font-size: 11px; padding: 2px 6px; border-radius: 4px; font-weight: 500; }
  
  form.search-form { display: flex; align-items: center; gap: 8px; flex: 1; max-width: 520px; }
  form.search-form input[type=text] { flex: 1; min-width: 160px; padding: 7px 12px; border: 1px solid var(--border); border-radius: 6px; font: inherit; outline: none; background: #fff; }
  form.search-form input[type=text]:focus { border-color: var(--accent); box-shadow: 0 0 0 2px rgba(37,99,235,0.15); }
  
  button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; padding: 7px 14px; border: 1px solid var(--border); border-radius: 6px; background: #fff; cursor: pointer; font: inherit; font-size: 13px; font-weight: 500; color: var(--fg); white-space: nowrap; flex-shrink: 0; transition: all 0.15s; }
  button:hover { background: #f8fafc; border-color: #cbd5e1; }
  button.primary { background: var(--accent); color: #fff; border-color: transparent; }
  button.primary:hover { background: var(--accent-hover); }
  button.danger-text { color: var(--danger); border-color: transparent; background: transparent; padding: 4px 8px; }
  button.danger-text:hover { background: #fee2e2; }
  button.add-btn { padding: 5px 12px; font-size: 12px; border-radius: 6px; border: 1px solid #cbd5e1; white-space: nowrap; min-width: 68px; height: 28px; display: inline-flex; align-items: center; justify-content: center; font-weight: 500; }
  button.add-btn.added { background: #ecfdf5; color: #059669; border-color: #a7f3d0; cursor: default; }

  /* Breadcrumb */
  nav.crumbs { display: flex; align-items: center; gap: 4px; padding: 8px 20px; border-bottom: 1px solid var(--border); font-size: 13px; background: #fff; }
  nav.crumbs a { color: var(--accent); text-decoration: none; display: flex; align-items: center; gap: 4px; }
  nav.crumbs a:hover { text-decoration: underline; }
  .sep { margin: 0 6px; color: #cbd5e1; }

  /* Workspace Layout (Two Columns) */
  .workspace { display: flex; flex: 1; overflow: hidden; }
  
  /* Left Explorer */
  .explorer { flex: 1; display: flex; flex-direction: column; overflow: hidden; background: #fff; }
  .table-scroll { flex: 1; overflow-y: auto; }
  
  /* Section Headings */
  .section-header { display: flex; align-items: center; gap: 8px; padding: 14px 20px 8px; font-size: 13px; font-weight: 600; color: var(--muted); }
  .section-badge { background: #e2e8f0; color: #475569; font-size: 11px; padding: 1px 6px; border-radius: 10px; font-weight: 600; }

  /* Folders Grid (Scheme A) */
  .folders-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 10px; padding: 4px 20px 16px; }
  .folder-card { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 8px 12px; background: #ffffff; border: 1px solid var(--border); border-radius: 8px; cursor: pointer; transition: all 0.15s ease; user-select: none; }
  .folder-card:hover { background: #f8fafc; border-color: #cbd5e1; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
  .folder-card-main { display: flex; align-items: center; gap: 8px; overflow: hidden; flex: 1; min-width: 0; }
  .folder-card-icon { font-size: 18px; flex-shrink: 0; }
  .folder-card-name { font-size: 13px; font-weight: 500; color: var(--fg); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .folder-card-add { padding: 3px 8px; font-size: 11px; border-radius: 5px; border: 1px solid #cbd5e1; background: #fff; white-space: nowrap; flex-shrink: 0; cursor: pointer; color: var(--muted); font-weight: 500; transition: all 0.15s; }
  .folder-card-add:hover { background: #eff6ff; color: var(--accent); border-color: var(--accent); }
  .folder-card-add.added { background: #ecfdf5; color: #059669; border-color: #a7f3d0; cursor: default; }

  /* Files Table Container (Scheme A) */
  .files-table-container { padding: 4px 20px 20px; }
  .files-table-wrap { background: #fff; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
  table { width: 100%; border-collapse: collapse; text-align: left; table-layout: fixed; }
  th, td { padding: 10px 14px; border-bottom: 1px solid var(--border); font-size: 13px; vertical-align: middle; }
  th { position: sticky; top: 0; background: #f8fafc; color: var(--muted); font-size: 12px; font-weight: 600; border-bottom: 1px solid var(--border); z-index: 2; }
  th:first-child, td:first-child { padding-left: 16px; }
  th:last-child, td:last-child { padding-right: 16px; }
  tr:last-child td { border-bottom: none; }

  .th-name, td.col-name { width: 44%; }
  .th-type, td.col-type { width: 22%; }
  .th-size, td.col-size { width: 10%; text-align: right; }
  .th-date, td.col-date { width: 14%; }
  .th-action, td.col-action { width: 10%; text-align: center; }

  tr[data-kind=file]:hover td { background: var(--row-hover); }
  .col-name { font-weight: 500; }
  .col-name-box { display: flex; align-items: center; gap: 8px; min-width: 0; }
  .col-name-box span.icon { font-size: 16px; width: 20px; text-align: center; flex-shrink: 0; }
  .file-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; min-width: 0; }
  .col-type { color: var(--muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .col-size { color: var(--muted); font-size: 12px; }
  .col-date { color: var(--muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  /* Right Cart Panel */
  .cart-panel { width: 380px; border-left: 1px solid var(--border); background: #fafafa; display: flex; flex-direction: column; overflow: hidden; }
  .cart-header { padding: 12px 16px; border-bottom: 1px solid var(--border); background: #fff; display: flex; align-items: center; justify-content: space-between; }
  .cart-title { font-weight: 600; font-size: 14px; display: flex; align-items: center; gap: 8px; }
  .badge { background: #eff6ff; color: var(--accent); font-size: 11px; padding: 2px 7px; border-radius: 12px; font-weight: 600; }

  .cart-items { flex: 1; overflow-y: auto; padding: 12px 16px; display: flex; flex-direction: column; gap: 8px; }
  .cart-item { display: flex; align-items: center; justify-content: space-between; background: #fff; border: 1px solid var(--border); padding: 8px 10px; border-radius: 6px; font-size: 13px; }
  .cart-item-info { display: flex; align-items: center; gap: 8px; overflow: hidden; }
  .cart-item-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 500; max-width: 230px; }
  .cart-item-meta { font-size: 11px; color: var(--muted); }

  .cart-footer { padding: 16px; border-top: 1px solid var(--border); background: #fff; display: flex; flex-direction: column; gap: 12px; }
  .field-label-row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
  .field-label { font-size: 12px; font-weight: 600; color: var(--muted); }
  .path-input-group { display: flex; gap: 6px; }
  .target-path-input { font-family: monospace; font-size: 12px; flex: 1; }
  .btn-sm { padding: 6px 10px; font-size: 12px; }

  /* Status / Empty States */
  .empty-state { padding: 40px 20px; text-align: center; color: var(--muted); font-size: 13px; }
  .error-state { padding: 30px; text-align: center; color: var(--danger); }

  /* Modals */
  .modal-overlay { position: fixed; inset: 0; background: rgba(15, 23, 42, 0.5); display: flex; align-items: center; justify-content: center; z-index: 100; backdrop-filter: blur(2px); }
  .modal-card { background: #fff; border-radius: 12px; width: 90%; max-width: 540px; padding: 24px; box-shadow: 0 20px 25px -5px rgba(0,0,0,0.1); display: flex; flex-direction: column; gap: 16px; }
  .modal-title { font-size: 16px; font-weight: 600; color: #065f46; display: flex; align-items: center; gap: 8px; }
  .prompt-box { width: 100%; height: 72px; padding: 10px; font-family: monospace; font-size: 13px; border: 1px solid #a7f3d0; border-radius: 6px; background: #f0fdf4; resize: none; color: #1e293b; outline: none; }
  .modal-actions { display: flex; justify-content: flex-end; gap: 8px; }

  /* Windows-like File Explorer Modal */
  .explorer-dialog { background: #fff; border-radius: 10px; width: 90%; max-width: 760px; height: 520px; display: flex; flex-direction: column; box-shadow: 0 25px 35px -5px rgba(0,0,0,0.25); border: 1px solid var(--border); overflow: hidden; }
  .exp-header { display: flex; align-items: center; justify-content: space-between; padding: 10px 16px; border-bottom: 1px solid var(--border); background: #f8fafc; }
  .exp-title { font-weight: 600; font-size: 14px; display: flex; align-items: center; gap: 8px; }
  
  .exp-toolbar { display: flex; align-items: center; gap: 8px; padding: 8px 16px; border-bottom: 1px solid var(--border); background: #fff; }
  .exp-addr-bar { flex: 1; display: flex; align-items: center; gap: 4px; border: 1px solid var(--border); border-radius: 6px; padding: 4px 10px; font-size: 12px; background: #f8fafc; overflow-x: auto; white-space: nowrap; }
  .exp-addr-bar a { color: var(--accent); text-decoration: none; cursor: pointer; }
  .exp-addr-bar a:hover { text-decoration: underline; }

  .exp-body { display: flex; flex: 1; overflow: hidden; }
  .exp-sidebar { width: 190px; border-right: 1px solid var(--border); background: #f8fafc; overflow-y: auto; padding: 10px 8px; display: flex; flex-direction: column; gap: 4px; }
  .exp-side-section { font-size: 11px; font-weight: 600; color: var(--muted); padding: 6px 8px 2px; }
  .exp-side-item { display: flex; align-items: center; gap: 8px; padding: 6px 10px; border-radius: 6px; cursor: pointer; font-size: 12px; text-decoration: none; color: var(--fg); }
  .exp-side-item:hover { background: #e2e8f0; }

  .exp-files { flex: 1; overflow-y: auto; padding: 12px 16px; display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); grid-auto-rows: max-content; gap: 8px; align-content: start; background: #fff; }
  .folder-tile { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 12px 8px; border: 1px solid transparent; border-radius: 6px; cursor: pointer; text-align: center; user-select: none; transition: all 0.1s; }
  .folder-tile:hover { background: #f1f5f9; border-color: #cbd5e1; }
  .folder-tile.selected { background: #eff6ff; border-color: #93c5fd; }
  .folder-tile-icon { font-size: 32px; line-height: 1; margin-bottom: 6px; }
  .folder-tile-name { font-size: 12px; font-weight: 500; word-break: break-all; max-height: 32px; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }

  .exp-footer { padding: 10px 16px; border-top: 1px solid var(--border); background: #f8fafc; display: flex; align-items: center; justify-content: space-between; font-size: 12px; }
  .exp-path-preview { font-family: monospace; color: var(--muted); max-width: 440px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  /* New Folder Inline Form */
  .new-folder-tile { display: flex; flex-direction: column; align-items: center; padding: 8px; border: 1px dashed var(--accent); border-radius: 6px; background: #eff6ff; }
  .new-folder-tile input { width: 100%; font-size: 11px; padding: 3px 6px; margin: 4px 0; border: 1px solid var(--border); border-radius: 4px; text-align: center; }

  /* Progress Overlay */
  .download-progress { display: flex; align-items: center; gap: 10px; color: var(--accent); font-weight: 500; font-size: 13px; }

  /* Language Toggle */
  .lang-toggle { display: inline-flex; align-items: center; gap: 2px; padding: 5px 10px; border: 1px solid var(--border); border-radius: 16px; background: #fff; cursor: pointer; font-size: 12px; font-weight: 600; color: var(--muted); transition: all 0.15s; white-space: nowrap; }
  .lang-toggle:hover { border-color: var(--accent); color: var(--accent); background: #eff6ff; }
  .lang-toggle .lang-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--accent); }
</style>
</head>
<body>

<header>
  <div class="brand">
    <span>☁️ GDrive Context Cart</span>
    <span class="tag">Context Bridge</span>
  </div>
  <form class="search-form" id="search-frm">
    <input type="text" id="search-input" placeholder="在整个 Google Drive 搜索文件..." autocomplete="off" data-i18n-placeholder="searchPlaceholder">
    <button type="submit" class="primary" data-i18n="searchBtn">搜索</button>
    <button type="button" id="btn-home" title="返回根目录" data-i18n="homeBtn">根目录</button>
  </form>
  <button type="button" id="lang-toggle" class="lang-toggle" title="Switch language / 切换语言">
    <span class="lang-dot"></span>
    <span id="lang-toggle-label">EN</span>
  </button>
</header>

<nav class="crumbs" id="crumbs"></nav>

<div class="workspace">
  <!-- Left Explorer (Google Drive) -->
  <div class="explorer">
    <div class="table-scroll">
      <div id="status-view" class="empty-state">正在加载云盘文件...</div>
      <div id="explorer-content" style="display:none;">
        <!-- Folders Section (Grid) -->
        <div id="section-folders" style="display:none;">
          <div class="section-header">
            <span data-i18n="foldersHeader">📂 文件夹</span>
            <span class="section-badge" id="folders-count">0</span>
          </div>
          <div class="folders-grid" id="folders-grid"></div>
        </div>

        <!-- Files Section (Table) -->
        <div id="section-files" style="display:none;">
          <div class="section-header">
            <span data-i18n="filesHeader">📄 文件</span>
            <span class="section-badge" id="files-count">0</span>
          </div>
          <div class="files-table-container">
            <div class="files-table-wrap">
              <table id="file-table">
                <thead>
                  <tr>
                    <th class="th-name" data-i18n="thName">名称</th>
                    <th class="th-type" data-i18n="thType">类型 / 降级策略</th>
                    <th class="th-size" data-i18n="thSize">大小</th>
                    <th class="th-date" data-i18n="thDate">修改时间</th>
                    <th class="th-action" data-i18n="thAction">操作</th>
                  </tr>
                </thead>
                <tbody id="rows"></tbody>
              </table>
            </div>
          </div>
        </div>

        <!-- Empty State View -->
        <div id="empty-state-view" class="empty-state" style="display:none;" data-i18n="emptyDir"></div>
      </div>
    </div>
  </div>

  <!-- Right Cart Panel -->
  <div class="cart-panel">
    <div class="cart-header">
      <div class="cart-title">
        <span data-i18n="cartTitle">待选素材篮</span>
        <span class="badge" id="cart-count">0 项</span>
      </div>
      <button class="danger-text" id="btn-clear-cart" data-i18n="clearCart">清空</button>
    </div>

    <div class="cart-items" id="cart-list">
      <div class="empty-state" style="padding-top: 60px;" data-i18n="cartEmpty"></div>
    </div>

    <div class="cart-footer">
      <div>
        <div class="field-label-row">
          <span class="field-label" data-i18n="targetLabel">保存目标本地文件夹 (Local Path)</span>
          <button type="button" class="btn-sm" id="btn-native-picker" data-i18n="nativePicker">🖥️ 唤起系统窗口</button>
        </div>
        <div class="path-input-group">
          <input type="text" id="target-dir-input" class="target-path-input" placeholder="加载中..." data-i18n-placeholder="loading">
          <button type="button" id="btn-browse-local" class="btn-sm" data-i18n="browseLocal">📂 浏览...</button>
        </div>
      </div>
      <button class="primary" id="btn-start-download" style="padding: 10px; font-size: 14px;">
        <span data-i18n="startDownload">🚀 一键拉取素材并复制路径</span>
      </button>
      <div id="download-spinner" style="display: none;" class="download-progress">
        <span data-i18n="downloadProgress">⏳ 正在排队下载与格式转换中...</span>
      </div>
    </div>
  </div>
</div>

<!-- Local Windows-like File Explorer Modal -->
<div id="local-explorer-modal" class="modal-overlay" style="display:none">
  <div class="explorer-dialog">
    <div class="exp-header">
      <div class="exp-title">
        <span data-i18n="expTitle">📁 选择本地目标文件夹</span>
      </div>
      <div style="display:flex; gap:6px;">
        <button type="button" class="btn-sm" id="btn-modal-native" data-i18n="systemDialog">🖥️ 系统原生窗口</button>
        <button type="button" class="btn-sm" id="btn-close-explorer">✕</button>
      </div>
    </div>

    <div class="exp-toolbar">
      <button type="button" class="btn-sm" id="btn-exp-up" data-i18n="upLevel">⬆ 上一级</button>
      <div class="exp-addr-bar" id="exp-breadcrumbs"></div>
      <button type="button" class="btn-sm" id="btn-exp-newfolder" data-i18n="newFolder">➕ 新建文件夹</button>
      <button type="button" class="btn-sm" id="btn-exp-refresh" data-i18n="refresh">🔄 刷新</button>
    </div>

    <div class="exp-body">
      <!-- Quick Access Sidebar -->
      <div class="exp-sidebar" id="exp-quick-access"></div>

      <!-- Folders Grid -->
      <div class="exp-files" id="exp-folders-grid"></div>
    </div>

    <div class="exp-footer">
      <div class="exp-path-preview" id="exp-selected-preview" data-i18n-prefix="selectedPrefix"></div>
      <div style="display:flex; gap:8px;">
        <button type="button" id="btn-cancel-exp" data-i18n="cancel">取消</button>
        <button type="button" class="primary" id="btn-confirm-exp" data-i18n="confirmDir">✓ 确定选择此目录</button>
      </div>
    </div>
  </div>
</div>

<!-- Complete Modal -->
<div id="success-modal" class="modal-overlay" style="display:none">
  <div class="modal-card">
    <div class="modal-title">
      <span data-i18n="successTitle">✅ 上下文素材已就绪！</span>
    </div>
    <div style="font-size: 13px; color: var(--muted);" data-i18n="successHint"></div>
    <textarea class="prompt-box" id="clipboard-preview" readonly></textarea>
    <div style="display:flex; justify-content:space-between; align-items:center;">
      <span id="copy-status" style="font-size:12px; color:#059669; font-weight:500;" data-i18n="copyStatus"></span>
      <div class="modal-actions">
        <button id="btn-copy-again" data-i18n="copyAgain">📋 重新复制</button>
        <button id="btn-open-dir" data-i18n="openDir">📂 打开本地目录</button>
        <button class="primary" id="btn-close-modal" data-i18n="done">完成</button>
      </div>
    </div>
  </div>
</div>

<script>
const state = {
  mode: "list",
  stack: [{ id: "root", name: "My Drive" }],
  searchQuery: "",
  cart: [],
  latestTargetDir: "",
  // Local explorer state
  localCurDir: "",
  localParent: "",
  localSelectedDir: "",
  isCreatingFolder: false,
  lang: localStorage.getItem("gdrive_cart_lang") || "zh"
};

// -------------- i18n --------------
const I18N = {
  zh: {
    searchPlaceholder: "在整个 Google Drive 搜索文件...",
    searchBtn: "搜索",
    homeBtn: "根目录",
    foldersHeader: "📂 文件夹",
    filesHeader: "📄 文件",
    thName: "名称",
    thType: "类型 / 降级策略",
    thSize: "大小",
    thDate: "修改时间",
    thAction: "操作",
    emptyDir: "当前目录下无内容",
    cartTitle: "待选素材篮",
    clearCart: "清空",
    cartEmpty: "从左侧点选文件/文件夹<br>放入素材篮",
    targetLabel: "保存目标本地文件夹 (Local Path)",
    nativePicker: "🖥️ 唤起系统窗口",
    browseLocal: "📂 浏览...",
    loading: "加载中...",
    startDownload: "🚀 一键拉取素材并复制路径",
    downloadProgress: "⏳ 正在排队下载与格式转换中...",
    expTitle: "📁 选择本地目标文件夹",
    systemDialog: "🖥️ 系统原生窗口",
    upLevel: "⬆ 上一级",
    newFolder: "➕ 新建文件夹",
    refresh: "🔄 刷新",
    selectedPrefix: "选定: ",
    cancel: "取消",
    confirmDir: "✓ 确定选择此目录",
    successTitle: "✅ 上下文素材已就绪！",
    successHint: "已自动将路径提示复制到剪贴板。回到对话输入框，直接 <code>Ctrl + V</code> 粘贴即可接上你的后续指令：",
    copyStatus: "✓ 已自动写入剪贴板",
    copyAgain: "📋 重新复制",
    openDir: "📂 打开本地目录",
    done: "完成",
    crumbUp: "⬆ 返回上级",
    crumbHome: "⬆ 返回根目录",
    searchResultsPrefix: "搜索结果: ",
    loadingDrive: "正在读取云端目录...",
    loadingSearch: "正在云端搜索...",
    readFailed: "读取失败: ",
    searchFailed: "搜索失败: ",
    folderEmpty: "当前文件夹为空",
    noMatch: "没有找到匹配的文件或文件夹",
    addBtn: "+ 加入",
    addedBtn: "✓ 已加",
    folderMeta: "文件夹",
    nativeCloud: "云端原生",
    folderAddTitle: "打包加入素材篮",
    alreadyInCartTitle: "已在素材篮",
    quickAccess: "快捷位置",
    newFolderDefault: "新建文件夹",
    localEmpty: "此文件夹为空",
    cartItemRemove: "移除",
    cartCountSuffix: "项",
    folderCountSuffix: "个",
    alertCartEmpty: "请先从左侧选择至少一个文件或文件夹加入素材篮！",
    alertNoTarget: "请指定保存目标路径！",
    alertDownloadFail: "下载或转换失败: ",
    alertLocalPathFail: "无法读取本地路径: ",
    alertMkdirFail: "创建文件夹失败: ",
    alertNativePickerFail: "呼出系统选择器失败: ",
    copyStatusFallback: "⚠ 复制受阻，请点击下方手动复制",
    copiedOk: "✓ 已复制到剪贴板！",
    copiedFail: "复制失败",
    typeDoc: "Google Doc → <b>Markdown</b>",
    typeSheet: "Google Sheet → <b>CSV</b>",
    typeSlides: "Google Slides → <b>PDF</b>",
    typeImage: "图片 (原件)",
    typePdf: "PDF (原件)",
    typeFolder: "文件夹",
    typeOther: "文件",
    typeRaw: "原件",
    nameLabel: "Context 工作区",
    docsLabel: "我的文档",
    desktopLabel: "桌面",
    downloadsLabel: "下载",
    driveLabel: "本地磁盘",
  },
  en: {
    searchPlaceholder: "Search files across Google Drive...",
    searchBtn: "Search",
    homeBtn: "Home",
    foldersHeader: "📂 Folders",
    filesHeader: "📄 Files",
    thName: "Name",
    thType: "Type / Convert",
    thSize: "Size",
    thDate: "Modified",
    thAction: "Action",
    emptyDir: "No content in this folder",
    cartTitle: "Context Cart",
    clearCart: "Clear",
    cartEmpty: "Pick files/folders on the left <br>and add them to the cart",
    targetLabel: "Destination Local Folder",
    nativePicker: "🖥️ System Dialog",
    browseLocal: "📂 Browse...",
    loading: "Loading...",
    startDownload: "🚀 Pull Files & Copy Path",
    downloadProgress: "⏳ Downloading & converting...",
    expTitle: "📁 Choose a Local Folder",
    systemDialog: "🖥️ Native Dialog",
    upLevel: "⬆ Up",
    newFolder: "➕ New Folder",
    refresh: "🔄 Refresh",
    selectedPrefix: "Selected: ",
    cancel: "Cancel",
    confirmDir: "✓ Use This Folder",
    successTitle: "✅ Context Ready!",
    successHint: "The path hint was copied to your clipboard. Go back to your prompt input and press <code>Ctrl + V</code> to continue with your instructions:",
    copyStatus: "✓ Copied to clipboard",
    copyAgain: "📋 Copy Again",
    openDir: "📂 Open Folder",
    done: "Done",
    crumbUp: "⬆ Up",
    crumbHome: "⬆ Home",
    searchResultsPrefix: "Results for: ",
    loadingDrive: "Loading Drive folder...",
    loadingSearch: "Searching Drive...",
    readFailed: "Failed to load: ",
    searchFailed: "Search failed: ",
    folderEmpty: "This folder is empty",
    noMatch: "No matching files or folders",
    addBtn: "+ Add",
    addedBtn: "✓ Added",
    folderMeta: "Folder",
    nativeCloud: "Native cloud file",
    folderAddTitle: "Add entire folder",
    alreadyInCartTitle: "Already in cart",
    quickAccess: "Quick Access",
    newFolderDefault: "New Folder",
    localEmpty: "This folder is empty",
    cartItemRemove: "Remove",
    cartCountSuffix: " item(s)",
    folderCountSuffix: "",
    alertCartEmpty: "Please add at least one file or folder to the cart first!",
    alertNoTarget: "Please specify a destination path!",
    alertDownloadFail: "Download or conversion failed: ",
    alertLocalPathFail: "Cannot read local path: ",
    alertMkdirFail: "Failed to create folder: ",
    alertNativePickerFail: "Failed to open system picker: ",
    copyStatusFallback: "⚠ Clipboard blocked — copy manually below",
    copiedOk: "✓ Copied to clipboard!",
    copiedFail: "Copy failed",
    typeDoc: "Google Doc → <b>Markdown</b>",
    typeSheet: "Google Sheet → <b>CSV</b>",
    typeSlides: "Google Slides → <b>PDF</b>",
    typeImage: "Image (original)",
    typePdf: "PDF (original)",
    typeFolder: "Folder",
    typeOther: "file",
    typeRaw: "original",
    nameLabel: "Context Workspace",
    docsLabel: "Documents",
    desktopLabel: "Desktop",
    downloadsLabel: "Downloads",
    driveLabel: "Local Drive",
  }
};

function tt(key) {
  return (I18N[state.lang] && I18N[state.lang][key]) || (I18N.zh[key] || key);
}

function applyLang() {
  // Update language toggle label
  $("#lang-toggle-label").textContent = state.lang === "zh" ? "EN" : "中文";
  // Update all data-i18n elements
  document.querySelectorAll("[data-i18n]").forEach(el => {
    const key = el.dataset.i18n;
    if (key) el.innerHTML = tt(key);
  });
  // Update placeholders
  document.querySelectorAll("[data-i18n-placeholder]").forEach(el => {
    el.placeholder = tt(el.dataset.i18nPlaceholder);
  });
  // Update prefix-style elements
  document.querySelectorAll("[data-i18n-prefix]").forEach(el => {
    const prefix = tt(el.dataset.i18nPrefix);
    if (el.id === "exp-selected-preview") {
      el.textContent = prefix + (state.localSelectedDir || "...");
    } else {
      el.textContent = prefix;
    }
  });
  // Title attributes
  const homeEl = document.querySelector("#btn-home");
  if (homeEl) homeEl.title = tt("homeBtn");
  const browseEl = document.querySelector("#btn-browse-local");
  if (browseEl) browseEl.title = tt("browseLocal");
  const nativeEl = document.querySelector("#btn-native-picker");
  if (nativeEl) nativeEl.title = tt("nativePicker");
  const modalNativeEl = document.querySelector("#btn-modal-native");
  if (modalNativeEl) modalNativeEl.title = tt("systemDialog");
  // Re-render dynamic content
  renderCrumbs();
  if (state.lastItems) renderExplorer(state.lastItems);
  renderCart();
  updateExpSelectedPreview();
}

document.addEventListener("click", (e) => {
  const langBtn = e.target.closest("#lang-toggle");
  if (langBtn) {
    state.lang = state.lang === "zh" ? "en" : "zh";
    localStorage.setItem("gdrive_cart_lang", state.lang);
    applyLang();
  }
});

const $ = s => document.querySelector(s);
const escapeHtml = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmtSize = (b, mime) => {
  if (mime && mime.startsWith("application/vnd.google-apps.")) {
    if (mime === "application/vnd.google-apps.folder") return "—";
    return '<span style="color:#94a3b8; font-size:11px;">' + tt("nativeCloud") + '</span>';
  }
  if (b == null) return '<span style="color:#94a3b8; font-size:11px;">' + tt("nativeCloud") + '</span>';
  if (b < 1024) return b + " B";
  if (b < 1048576) return (b / 1024).toFixed(1) + " KB";
  if (b < 1073741824) return (b / 1048576).toFixed(1) + " MB";
  return (b / 1073741824).toFixed(2) + " GB";
};
const fmtDate = d => d ? new Date(d).toLocaleDateString(state.lang === "en" ? "en-US" : "zh-CN") + " " + new Date(d).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) : "—";

function iconOf(m) {
  if (m === "application/vnd.google-apps.folder") return "📁";
  if (m === "application/vnd.google-apps.document") return "📝";
  if (m === "application/vnd.google-apps.spreadsheet") return "📊";
  if (m === "application/vnd.google-apps.presentation" || m === "application/vnd.google-apps.slides") return "📽";
  if (m.startsWith("image/")) return "🖼️";
  if (m.startsWith("video/")) return "🎞️";
  if (m.startsWith("audio/")) return "🎧";
  if (m === "application/pdf") return "📄";
  if (m.includes("zip") || m.includes("compressed") || m.includes("rar")) return "🗜️";
  if (m === "text/csv" || m.includes("spreadsheet")) return "📊";
  if (m.includes("word") || m.includes("officedocument")) return "📝";
  return "📎";
}

function typeBadge(m) {
  if (m === "application/vnd.google-apps.folder") return tt("typeFolder");
  if (m === "application/vnd.google-apps.document") return tt("typeDoc");
  if (m === "application/vnd.google-apps.spreadsheet") return tt("typeSheet");
  if (m === "application/vnd.google-apps.slides") return tt("typeSlides");
  if (m.startsWith("image/")) return tt("typeImage");
  if (m === "application/pdf") return tt("typePdf");
  return (m.split("/").pop().toUpperCase() || tt("typeOther")) + ` (${tt("typeRaw")})`;
}

function renderCrumbs() {
  const parts = state.mode === "list"
    ? state.stack.map((s, i) => i === state.stack.length - 1
        ? `<span>${escapeHtml(s.name)}</span>`
        : `<a href="#" data-crumb="${i}">${escapeHtml(s.name)}</a>`).join('<span class="sep">/</span>')
    : `<span>${tt("searchResultsPrefix")}"${escapeHtml(state.searchQuery)}"</span>`;
  const lead = state.mode === "list"
    ? `<a href="#" id="crumb-up" title="${tt("crumbUp")}">${tt("crumbUp")}</a>`
    : `<a href="#" id="crumb-home">${tt("crumbHome")}</a>`;
  $("#crumbs").innerHTML = lead + `<span class="sep">|</span>` + parts;
}

async function api(url, opts) {
  const r = await fetch(url, opts);
  const j = await r.json();
  if (j.error) throw new Error(j.error);
  return j;
}

function setStatus(msg, isErr) {
  const el = $("#status-view");
  el.className = isErr ? "error-state" : "empty-state";
  el.innerHTML = msg;
  $("#explorer-content").style.display = "none";
  el.style.display = "block";
}

function isInCart(id) {
  return state.cart.some(it => it.id === id);
}

function updateButtonState(id, inCart) {
  document.querySelectorAll(`[data-id="${id}"]`).forEach(btn => {
    if (inCart) {
      btn.classList.add("added");
      btn.textContent = tt("addedBtn");
    } else {
      btn.classList.remove("added");
      btn.textContent = tt("addBtn");
    }
  });
}

function renderExplorer(items) {
  state.lastItems = items;
  $("#status-view").style.display = "none";
  const contentEl = $("#explorer-content");
  contentEl.style.display = "block";

  const folders = items.filter(it => it.mimeType === "application/vnd.google-apps.folder");
  const files = items.filter(it => it.mimeType !== "application/vnd.google-apps.folder");

  // 1. Render Folders Grid (Scheme A)
  const secFolders = $("#section-folders");
  const gridFolders = $("#folders-grid");
  if (folders.length > 0) {
    $("#folders-count").textContent = `${folders.length}${tt("folderCountSuffix")}`;
    gridFolders.innerHTML = "";
    for (const f of folders) {
      const card = document.createElement("div");
      card.className = "folder-card";
      const inCart = isInCart(f.id);
      card.innerHTML = `
        <div class="folder-card-main" title="${escapeHtml(f.name)}">
          <span class="folder-card-icon">📁</span>
          <span class="folder-card-name">${escapeHtml(f.name)}</span>
        </div>
        <button type="button" class="folder-card-add ${inCart ? 'added' : ''}" data-id="${f.id}" title="${inCart ? tt('alreadyInCartTitle') : tt('folderAddTitle')}">
          ${inCart ? tt('addedBtn') : tt('addBtn')}
        </button>
      `;
      card.querySelector(".folder-card-main").addEventListener("click", () => {
        if (state.mode === "search") state.stack = [{ id: "root", name: "My Drive" }];
        state.stack.push({ id: f.id, name: f.name });
        loadFolder(f.id);
      });
      card.querySelector(".folder-card-add").addEventListener("click", (e) => {
        e.stopPropagation();
        if (!isInCart(f.id)) {
          addToCart(f);
        }
      });
      gridFolders.appendChild(card);
    }
    secFolders.style.display = "block";
  } else {
    secFolders.style.display = "none";
  }

  // 2. Render Files Table (Scheme A)
  const secFiles = $("#section-files");
  const tbody = $("#rows");
  if (files.length > 0) {
    $("#files-count").textContent = `${files.length}${tt("folderCountSuffix")}`;
    tbody.innerHTML = "";
    for (const it of files) {
      const tr = document.createElement("tr");
      tr.dataset.kind = "file";
      const inCart = isInCart(it.id);
      tr.innerHTML = `
        <td class="col-name">
          <div class="col-name-box">
            <span class="icon">${iconOf(it.mimeType)}</span>
            <span class="file-title" title="${escapeHtml(it.name)}">${escapeHtml(it.name)}</span>
          </div>
        </td>
        <td class="col-type">${typeBadge(it.mimeType)}</td>
        <td class="col-size" style="text-align:right;">${fmtSize(it.size, it.mimeType)}</td>
        <td class="col-date">${fmtDate(it.modifiedTime)}</td>
        <td class="col-action">
          <button class="add-btn ${inCart ? 'added' : ''}" data-id="${it.id}">
            ${inCart ? tt('addedBtn') : tt('addBtn')}
          </button>
        </td>
      `;
      tr.querySelector(".add-btn").addEventListener("click", (e) => {
        e.stopPropagation();
        if (!isInCart(it.id)) {
          addToCart(it);
        }
      });
      tbody.appendChild(tr);
    }
    secFiles.style.display = "block";
  } else {
    secFiles.style.display = "none";
  }

  // Overall empty view
  const emptyView = $("#empty-state-view");
  if (folders.length === 0 && files.length === 0) {
    emptyView.textContent = state.mode === "list" ? tt("folderEmpty") : tt("noMatch");
    emptyView.style.display = "block";
  } else {
    emptyView.style.display = "none";
  }
}

function addToCart(item) {
  if (!isInCart(item.id)) {
    state.cart.push(item);
    renderCart();
    updateButtonState(item.id, true);
  }
}

function removeFromCart(id) {
  state.cart = state.cart.filter(it => it.id !== id);
  renderCart();
  updateButtonState(id, false);
}

function renderCart() {
  $("#cart-count").textContent = `${state.cart.length} ${tt("cartCountSuffix")}`;
  const container = $("#cart-list");
  if (!state.cart.length) {
    container.innerHTML = `
      <div class="empty-state" style="padding-top: 60px;">
        ${tt("cartEmpty")}
      </div>
    `;
    return;
  }

  container.innerHTML = "";
  for (const it of state.cart) {
    const el = document.createElement("div");
    el.className = "cart-item";
    el.innerHTML = `
      <div class="cart-item-info">
        <span>${iconOf(it.mimeType)}</span>
        <div style="overflow:hidden;">
          <div class="cart-item-name" title="${escapeHtml(it.name)}">${escapeHtml(it.name)}</div>
          <div class="cart-item-meta">${it.mimeType === 'application/vnd.google-apps.folder' ? tt('folderMeta') : fmtSize(it.size)}</div>
        </div>
      </div>
      <button class="danger-text" title="${tt('cartItemRemove')}" data-rm="${it.id}">✕</button>
    `;
    el.querySelector("[data-rm]").addEventListener("click", () => removeFromCart(it.id));
    container.appendChild(el);
  }
}

async function loadFolder(id) {
  state.mode = "list";
  renderCrumbs();
  setStatus(tt("loadingDrive"));
  try {
    const res = await api(`/api/list?folder=${encodeURIComponent(id)}`);
    renderExplorer(res.items);
  } catch (err) {
    setStatus(tt("readFailed") + err.message, true);
  }
}

async function doSearch(query) {
  state.mode = "search";
  state.searchQuery = query;
  renderCrumbs();
  setStatus(tt("loadingSearch"));
  try {
    const res = await api(`/api/search?q=${encodeURIComponent(query)}`);
    renderExplorer(res.items);
  } catch (err) {
    setStatus(tt("searchFailed") + err.message, true);
  }
}

async function initDefaultTargetDir() {
  try {
    const res = await api("/api/default-target");
    if (res.default_dir) {
      $("#target-dir-input").value = res.default_dir;
      state.localSelectedDir = res.default_dir;
    }
  } catch(e) {}
}

async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  }
}

// Local Explorer UI Implementation

async function openLocalExplorer(initialPath) {
  state.isCreatingFolder = false;
  $("#local-explorer-modal").style.display = "flex";
  await loadLocalDir(initialPath || $("#target-dir-input").value.trim());
}

async function loadLocalDir(pathStr) {
  try {
    const res = await api(`/api/local/browse?path=${encodeURIComponent(pathStr || '')}`);
    state.localCurDir = res.current;
    state.localParent = res.parent;
    state.localSelectedDir = res.current;
    updateExpSelectedPreview();

    // Breadcrumbs
    const bc = $("#exp-breadcrumbs");
    bc.innerHTML = res.breadcrumbs.map((b, i) => 
      i === res.breadcrumbs.length - 1
        ? `<b>${escapeHtml(b.name)}</b>`
        : `<a data-localpath="${escapeHtml(b.path)}">${escapeHtml(b.name)}</a>`
    ).join(' <span style="color:#cbd5e1;">&gt;</span> ');

    // Sidebar
    const sb = $("#exp-quick-access");
    sb.innerHTML = `<div class="exp-side-section">${tt("quickAccess")}</div>` +
      res.quick_access.map(q => `
        <div class="exp-side-item" data-localpath="${escapeHtml(q.path)}">
          <span>${q.icon}</span>
          <span>${escapeHtml(q.name)}</span>
        </div>
      `).join('');

    // Grid
    renderLocalFolders(res.folders);
  } catch (err) {
    alert(tt("alertLocalPathFail") + err.message);
  }
}

function updateExpSelectedPreview() {
  const el = $("#exp-selected-preview");
  if (el) el.textContent = `${tt("selectedPrefix")}${state.localSelectedDir || "..."}`;
}

function renderLocalFolders(folders) {
  const grid = $("#exp-folders-grid");
  grid.innerHTML = "";

  if (state.isCreatingFolder) {
    const nf = document.createElement("div");
    nf.className = "new-folder-tile";
    nf.innerHTML = `
      <span style="font-size:24px;">📁</span>
      <input type="text" id="new-folder-input" value="${tt("newFolderDefault")}" autofocus>
      <div style="display:flex; gap:4px; margin-top:2px;">
        <button type="button" class="btn-sm primary" id="btn-save-newfolder" style="padding:2px 6px; font-size:10px;">✓</button>
        <button type="button" class="btn-sm" id="btn-cancel-newfolder" style="padding:2px 6px; font-size:10px;">✕</button>
      </div>
    `;
    grid.appendChild(nf);

    const inp = nf.querySelector("#new-folder-input");
    inp.focus();
    inp.select();

    nf.querySelector("#btn-save-newfolder").addEventListener("click", () => submitNewFolder(inp.value.trim()));
    nf.querySelector("#btn-cancel-newfolder").addEventListener("click", () => {
      state.isCreatingFolder = false;
      renderLocalFolders(folders);
    });
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") submitNewFolder(inp.value.trim());
      if (e.key === "Escape") {
        state.isCreatingFolder = false;
        renderLocalFolders(folders);
      }
    });
  }

  if (!folders.length && !state.isCreatingFolder) {
    grid.innerHTML = `<div style="grid-column: 1/-1; padding: 40px; text-align: center; color: var(--muted);">${tt("localEmpty")}</div>`;
    return;
  }

  for (const f of folders) {
    const el = document.createElement("div");
    el.className = "folder-tile" + (f.path === state.localSelectedDir ? " selected" : "");
    el.innerHTML = `
      <div class="folder-tile-icon">📁</div>
      <div class="folder-tile-name" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</div>
    `;

    // Single click selects
    el.addEventListener("click", () => {
      document.querySelectorAll(".folder-tile").forEach(t => t.classList.remove("selected"));
      el.classList.add("selected");
      state.localSelectedDir = f.path;
      updateExpSelectedPreview();
    });

    // Double click enters
    el.addEventListener("dblclick", () => {
      loadLocalDir(f.path);
    });

    grid.appendChild(el);
  }
}

async function submitNewFolder(name) {
  if (!name) return;
  try {
    const res = await api("/api/local/mkdir", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parent: state.localCurDir, name: name })
    });
    state.isCreatingFolder = false;
    await loadLocalDir(state.localCurDir);
    state.localSelectedDir = res.path;
    updateExpSelectedPreview();
  } catch (err) {
    alert(tt("alertMkdirFail") + err.message);
  }
}

async function triggerNativePicker() {
  try {
    const cur = $("#target-dir-input").value.trim();
    const res = await api("/api/local/pick-native", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ initial_dir: cur })
    });
    if (res.path) {
      $("#target-dir-input").value = res.path;
      state.localSelectedDir = res.path;
      if ($("#local-explorer-modal").style.display !== "none") {
        await loadLocalDir(res.path);
      }
    }
  } catch (err) {
    alert(tt("alertNativePickerFail") + err.message);
  }
}

// Event Listeners

$("#btn-browse-local").addEventListener("click", () => {
  openLocalExplorer($("#target-dir-input").value.trim());
});

$("#btn-native-picker").addEventListener("click", () => {
  triggerNativePicker();
});

$("#btn-modal-native").addEventListener("click", () => {
  triggerNativePicker();
});

$("#btn-close-explorer").addEventListener("click", () => {
  $("#local-explorer-modal").style.display = "none";
});

$("#btn-cancel-exp").addEventListener("click", () => {
  $("#local-explorer-modal").style.display = "none";
});

$("#btn-confirm-exp").addEventListener("click", () => {
  if (state.localSelectedDir) {
    $("#target-dir-input").value = state.localSelectedDir;
  }
  $("#local-explorer-modal").style.display = "none";
});

$("#btn-exp-up").addEventListener("click", () => {
  if (state.localParent) {
    loadLocalDir(state.localParent);
  }
});

$("#btn-exp-refresh").addEventListener("click", () => {
  loadLocalDir(state.localCurDir);
});

$("#btn-exp-newfolder").addEventListener("click", () => {
  state.isCreatingFolder = true;
  loadLocalDir(state.localCurDir);
});

document.addEventListener("click", (e) => {
  const item = e.target.closest("[data-localpath]");
  if (item) {
    loadLocalDir(item.dataset.localpath);
  }
});

// Search and Navigation for Drive
$("#search-frm").addEventListener("submit", e => {
  e.preventDefault();
  const q = $("#search-input").value.trim();
  if (q) doSearch(q);
});

$("#btn-home").addEventListener("click", () => {
  $("#search-input").value = "";
  state.stack = [{ id: "root", name: "My Drive" }];
  loadFolder("root");
});

document.addEventListener("click", e => {
  const up = e.target.closest("#crumb-up");
  if (up) {
    if (state.stack.length > 1) {
      state.stack.pop();
      loadFolder(state.stack[state.stack.length - 1].id);
    }
    return;
  }
  const home = e.target.closest("#crumb-home");
  if (home) {
    state.stack = [{ id: "root", name: "My Drive" }];
    loadFolder("root");
    return;
  }
  const crumb = e.target.closest("[data-crumb]");
  if (crumb) {
    const idx = +crumb.dataset.crumb;
    state.stack = state.stack.slice(0, idx + 1);
    loadFolder(state.stack[idx].id);
    return;
  }
});

$("#btn-clear-cart").addEventListener("click", () => {
  state.cart = [];
  renderCart();
  document.querySelectorAll("[data-id].added").forEach(b => {
    b.classList.remove("added");
    b.textContent = tt("addBtn");
  });
});

// Download Action
$("#btn-start-download").addEventListener("click", async () => {
  if (!state.cart.length) {
    alert(tt("alertCartEmpty"));
    return;
  }
  const targetDir = $("#target-dir-input").value.trim();
  if (!targetDir) {
    alert(tt("alertNoTarget"));
    return;
  }

  const btn = $("#btn-start-download");
  const spinner = $("#download-spinner");
  btn.disabled = true;
  btn.style.display = "none";
  spinner.style.display = "flex";

  try {
    const res = await api("/api/batch-download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        target_dir: targetDir,
        items: state.cart
      })
    });

    state.latestTargetDir = res.target_dir;
    const promptText = state.lang === "en"
      ? (res.clipboard_text_en || `The context path for this task is: ${res.target_dir}, `)
      : (res.clipboard_text || `此次任务的上下文路径在: ${res.target_dir}，`);
    $("#clipboard-preview").value = promptText;
    
    const ok = await copyToClipboard(promptText);
    $("#copy-status").textContent = ok ? tt("copyStatus") : tt("copyStatusFallback");
    $("#copy-status").style.display = "";
    $("#success-modal").style.display = "flex";
  } catch (err) {
    alert(tt("alertDownloadFail") + err.message);
  } finally {
    btn.disabled = false;
    btn.style.display = "inline-flex";
    spinner.style.display = "none";
  }
});

$("#btn-copy-again").addEventListener("click", async () => {
  const txt = $("#clipboard-preview").value;
  const ok = await copyToClipboard(txt);
  $("#copy-status").textContent = ok ? tt("copiedOk") : tt("copiedFail");
});

$("#btn-open-dir").addEventListener("click", async () => {
  if (state.latestTargetDir) {
    await api("/api/open-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: state.latestTargetDir })
    });
  }
});

$("#btn-close-modal").addEventListener("click", () => {
  $("#success-modal").style.display = "none";
  initDefaultTargetDir();
});

// Boot
initDefaultTargetDir();
applyLang();
loadFolder("root");
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status: int, body: bytes, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, status: int = 200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(status, data, "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

        try:
            if path in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/list":
                folder = q.get("folder", "root")
                self._json({"folder": folder_info(folder), "items": drive_list(folder)})
            elif path == "/api/search":
                term = q.get("q", "").strip()
                self._json({"query": term, "items": drive_search(term) if term else []})
            elif path == "/api/default-target":
                self._json({"default_dir": str(get_default_context_dir())})
            elif path == "/api/local/browse":
                target_path = q.get("path", "").strip()
                self._json(browse_local_dir(target_path))
            else:
                self._json({"error": f"Endpoint not found: {path}"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(content_length)
            body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}

            if path == "/api/batch-download":
                target_dir_str = body.get("target_dir", "").strip()
                if not target_dir_str:
                    raise RuntimeError("Missing target_dir")
                target_dir = Path(target_dir_str).resolve()
                target_dir.mkdir(parents=True, exist_ok=True)

                items = body.get("items", [])
                if not items:
                    raise RuntimeError("No items selected in cart")

                svc = get_service()

                user_email = ""
                try:
                    about = svc.about().get(fields="user(emailAddress)").execute()
                    user_email = about.get("user", {}).get("emailAddress", "")
                except Exception:
                    pass

                downloaded = []
                for item in items:
                    downloaded.extend(resolve_and_download_item(svc, item, target_dir))

                manifest_path = build_manifest(target_dir, downloaded, user_email)
                clipboard_text = f"此次任务的上下文路径在: {target_dir}，"
                clipboard_text_en = f"The context path for this task is: {target_dir}, "

                self._json({
                    "success": True,
                    "target_dir": str(target_dir),
                    "count": len(downloaded),
                    "manifest_path": str(manifest_path),
                    "clipboard_text": clipboard_text,
                    "clipboard_text_en": clipboard_text_en,
                    "files": downloaded,
                })

            elif path == "/api/local/mkdir":
                parent_dir = body.get("parent", "").strip()
                folder_name = body.get("name", "").strip()
                if not parent_dir or not folder_name:
                    raise RuntimeError("Parent path and folder name are required")
                new_path = make_local_dir(parent_dir, folder_name)
                self._json({"success": True, "path": new_path, "name": folder_name})

            elif path == "/api/local/pick-native":
                initial_dir = body.get("initial_dir", "").strip()
                picked = pick_native_folder(initial_dir)
                self._json({"path": picked})

            elif path == "/api/open-folder":
                folder_path = body.get("path", "").strip()
                if folder_path and Path(folder_path).exists():
                    if sys.platform == "win32":
                        os.startfile(folder_path)
                    elif sys.platform == "darwin":
                        subprocess.run(["open", folder_path], check=False)
                    else:
                        subprocess.run(["xdg-open", folder_path], check=False)
                    self._json({"success": True})
                else:
                    self._json({"error": "Folder does not exist"}, 400)
            else:
                self._json({"error": f"Endpoint not found: {path}"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)


def main():
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        print(f"ERROR: Cannot bind {HOST}:{PORT} — {e}")
        raise SystemExit(1)

    srv.daemon_threads = True
    url = f"http://{HOST}:{PORT}"
    print(f"GDrive Context Cart running at: {url}")
    print(f"Token location: {TOKEN_PATH}")

    try:
        webbrowser.open(url)
    except Exception:
        pass

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
