#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件提取工作台 —— 本地服务（只用 Python 标准库，Python 3.8+）
支持 Windows 10 / 11、macOS、Linux。

用法:
  python server.py                       启动工作台并自动打开浏览器
  python server.py --port 8800           指定端口
  python server.py --no-browser          不自动打开浏览器
  python server.py --run 方案.json        不打开界面，按方案直接执行
  python server.py --run 方案.json --preview   只预览匹配结果，不执行
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse  # noqa: E402
import base64  # noqa: E402
import json  # noqa: E402
import mimetypes  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
import webbrowser  # noqa: E402
from collections import OrderedDict  # noqa: E402
from datetime import datetime  # noqa: E402
from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402
from socketserver import ThreadingMixIn  # noqa: E402
from urllib.parse import urlparse, parse_qs  # noqa: E402

from mcct import VERSION  # noqa: E402
from mcct import util  # noqa: E402
from mcct.util import (UserError, Cancelled, IS_WIN, IS_MAC, APP_DIR, PRESET_DIR, TEMPLATE_DIR, SETTINGS_FILE,  # noqa: E402
                       norm_path, long_path, safe_print, sanitize_segment, is_inside, human_size, fmt_ts)
from mcct.index import HOLDER, KIND_LABEL  # noqa: E402
from mcct import rules, plan as planmod, jobs, nl, infer, listfile, llm  # noqa: E402
from mcct.meta import META, preview_image, BROWSER_IMAGES, VIDEO_TYPES, AUDIO_TYPES, JPEG_EXTS  # noqa: E402

TOKEN = secrets.token_urlsafe(18)
STATE = {"job": None, "httpd": None, "query_seq": 0}
THUMBS = OrderedDict()
THUMB_LOCK = threading.Lock()


# ================================================================== 设置
def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8-sig") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(d):
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_FILE)


def public_settings():
    s = load_settings()
    l = dict(s.get("llm") or {})
    key = l.pop("apiKey", "") or ""
    l["hasKey"] = bool(key)
    l["keyHint"] = ("…" + key[-4:]) if len(key) >= 8 else ("已填写" if key else "")
    l.setdefault("provider", "anthropic")
    l.setdefault("enabled", False)
    return {"llm": l, "ui": s.get("ui") or {}}


# ================================================================== 接口
def api_info(d):
    info = util.system_info()
    info["registry"] = rules.registry()
    info["vars"] = planmod.VAR_LIST
    info["settings"] = public_settings()
    info["llmDefaults"] = llm.DEFAULTS
    return info


def api_index(d):
    p = d.get("plan") or {}
    sources = [s for s in (p.get("sources") or []) if str(s).strip()]
    if not sources:
        raise UserError("请先添加来源文件夹")
    root = norm_path((p.get("dest") or {}).get("root"))
    gen = HOLDER.start(sources, p.get("scan") or {}, exclude=[root] if root else [])
    return {"gen": gen}


def api_index_status(d):
    return HOLDER.status()


def api_query(d):
    STATE["query_seq"] += 1
    seq = STATE["query_seq"]

    def cancel_check():
        if STATE["query_seq"] != seq:
            raise Cancelled()
    idx = HOLDER.require(d.get("gen"))
    p = d.get("plan") or {}
    o = d.get("opts") or {}
    t0 = time.time()
    try:
        matched, stats, ctx = rules.evaluate(idx, p, {"histField": o.get("histField"), "hist": True}, cancel_check)
    except Cancelled:
        return {"stale": True}
    hist_field = stats["histField"]
    srt = rules.sort_recs(matched, o.get("sort") or "path", ctx, hist_field)
    offset = max(0, int(o.get("offset") or 0))
    count = min(1000, max(1, int(o.get("limit") or 200)))
    page = srt[offset:offset + count]
    items = [rules.item_json(r, ctx, hist_field) for r in page]
    scope = rules.scoped(idx, p)
    out = {
        "gen": idx.gen,
        "total": len(idx.recs),
        "scoped": stats["scoped"],
        "matched": len(matched),
        "beforeLimit": stats["beforeLimit"],
        "matchedBytes": sum(r.size for r in matched),
        "condStats": stats["items"],
        "mode": stats["mode"],
        "histField": hist_field,
        "hist": rules.histogram(stats["histValues"], o.get("histMode") or "timeline"),
        "facets": rules.facets(scope, matched),
        "items": items,
        "offset": offset,
        "summary": rules.describe_plan(p.get("rule"), p.get("limit")),
        "list": stats.get("list"),
    }
    # 去向预览
    dest = p.get("dest") or {}
    action = dest.get("action") or "copy"
    try:
        root = planmod.check_dest(dest, idx, need_root=False)
        if matched:
            check_fs = action not in ("zip", "list") and bool(root) and len(matched) <= 30000
            planned = planmod.plan_items(srt, dest, ctx, time.time(), root, check_fs=check_fs)
            pm = dict((pi[0].id, pi) for pi in planned)
            for it in items:
                pi = pm.get(it["id"])
                if pi:
                    it["dest"] = pi[2]
                    it["status"] = pi[3]
            tr = planmod.tree(planned)
            out["destTree"] = tr
            out["destRoot"] = root
            if action == "zip" and root:
                out["zipPath"] = planmod.zip_path(dest, root, time.time())
    except UserError as ex:
        out["destError"] = str(ex)
    out["ms"] = int((time.time() - t0) * 1000)
    return out


def _rec(d):
    idx = HOLDER.require(d.get("gen"))
    try:
        i = int(d.get("id"))
        return idx, idx.recs[i]
    except (TypeError, ValueError, IndexError):
        raise UserError("文件不在当前索引中，请刷新")


def api_file_info(d):
    idx, rec = _rec(d)
    return rules.file_info(rec, idx)


def nl_context():
    idx = HOLDER.index
    if idx is None:
        return {}
    dirs, exts, samples = [], {}, []
    seen = set()
    for r in idx.recs:
        d1 = r.parts[0] if r.parts else ""
        if d1 and d1 not in seen:
            seen.add(d1)
            dirs.append(d1)
        exts[r.ext] = exts.get(r.ext, 0) + 1
    step = max(1, len(idx.recs) // 40)
    samples = [idx.recs[i].rel for i in range(0, len(idx.recs), step)][:40]
    has_exif = None
    for r in idx.recs[:400]:
        if r.kind in ("photo", "raw"):
            has_exif = bool(META.get(r, "exif").get("time"))
            break
    return {"dirs": dirs[:200], "exts": [k for k, _ in sorted(exts.items(), key=lambda x: -x[1])][:30],
            "samples": samples, "hasExif": has_exif}


def api_nl(d):
    text = d.get("text") or ""
    if d.get("engine") == "llm":
        s = load_settings().get("llm") or {}
        return llm.translate(text, nl_context(), s)
    return nl.parse(text, nl_context())


def api_infer(d):
    idx = HOLDER.require(d.get("gen"))
    return infer.infer(idx, d.get("plan") or {}, d.get("ids") or [])


def api_list_parse(d):
    return listfile.parse(text=d.get("text"), b64=d.get("b64"), filename=d.get("filename") or "", header=d.get("header"))


def api_run(d):
    j = STATE["job"]
    if j and j.state == "running":
        raise UserError("已有任务正在运行，请等它结束或先停止")
    p = d.get("plan") or {}
    if not (p.get("dest") or {}).get("root"):
        raise UserError("请先选择目标文件夹")
    rules.compile_rule(p.get("rule"))
    kind = "watch" if d.get("mode") == "watch" else "run"
    job = jobs.Job(p, kind)
    STATE["job"] = job
    target = jobs.watch_job if kind == "watch" else jobs.run_job
    threading.Thread(target=target, args=(job,), daemon=True).start()
    return {"ok": True, "runId": job.run_id}


def api_job_status(d):
    j = STATE["job"]
    if not j:
        return {"state": "idle", "logs": [], "logSeq": 0}
    return j.snapshot(int(d.get("since") or 0))


def api_job_cancel(d):
    j = STATE["job"]
    if j and j.state == "running":
        j.cancel.set()
    return {"ok": True}


def api_history(d):
    return {"runs": jobs.history()}


def api_undo(d):
    j = STATE["job"]
    if j and j.state == "running":
        raise UserError("请等当前任务结束后再撤销")
    return jobs.undo(d.get("runId"))


def api_settings_get(d):
    return public_settings()


def api_settings_save(d):
    s = load_settings()
    new = d.get("llm")
    if isinstance(new, dict):
        cur = dict(s.get("llm") or {})
        for k in ("provider", "baseUrl", "model", "enabled"):
            if k in new:
                cur[k] = new[k]
        if new.get("apiKey"):
            cur["apiKey"] = new["apiKey"].strip()
        if new.get("clearKey"):
            cur.pop("apiKey", None)
        s["llm"] = cur
    if isinstance(d.get("ui"), dict):
        s["ui"] = d["ui"]
    save_settings(s)
    return public_settings()


def api_llm_test(d):
    return llm.test(load_settings().get("llm") or {})


# ---------------------------------------------------------------- 方案与模板
def _read_json(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def api_presets_list(d):
    out = {"presets": [], "templates": []}
    os.makedirs(PRESET_DIR, exist_ok=True)
    for folder, key in ((PRESET_DIR, "presets"), (TEMPLATE_DIR, "templates")):
        if not os.path.isdir(folder):
            continue
        for fn in sorted(os.listdir(folder)):
            if not fn.lower().endswith(".json"):
                continue
            try:
                j = _read_json(os.path.join(folder, fn))
                out[key].append({"file": fn, "name": j.get("name") or fn[:-5], "description": j.get("description", ""),
                                 "savedAt": j.get("savedAt", ""), "order": j.get("order", 99)})
            except (OSError, ValueError):
                out[key].append({"file": fn, "name": fn[:-5] + "（文件损坏）", "description": "", "savedAt": "", "order": 999})
    out["templates"].sort(key=lambda x: (x["order"], x["name"]))
    return out


def _preset_path(d, folder=PRESET_DIR):
    fn = d.get("file") or ""
    if not fn or "/" in fn or "\\" in fn or ".." in fn:
        raise UserError("文件名无效")
    p = os.path.join(folder, fn)
    if not os.path.isfile(p):
        raise UserError("找不到这个方案")
    return p


def api_presets_load(d):
    folder = TEMPLATE_DIR if d.get("template") else PRESET_DIR
    j = _read_json(_preset_path(d, folder))
    return {"name": j.get("name", ""), "description": j.get("description", ""), "plan": j.get("plan") or j.get("config") or {},
            "template": bool(d.get("template"))}


def api_presets_save(d):
    name = (d.get("name") or "").strip()
    if not name:
        raise UserError("方案名称不能为空")
    fn = sanitize_segment(name.replace("/", "_").replace("\\", "_"))[:80] + ".json"
    os.makedirs(PRESET_DIR, exist_ok=True)
    obj = {"name": name, "savedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "tool": "file-extract-workbench",
           "version": 2, "plan": d.get("plan") or {}}
    with open(os.path.join(PRESET_DIR, fn), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    return {"file": fn}


def api_presets_delete(d):
    os.remove(_preset_path(d))
    return {"ok": True}


# ---------------------------------------------------------------- 文件夹浏览
def list_roots():
    roots = []
    home = os.path.expanduser("~")
    if IS_WIN:
        try:
            import ctypes
            mask = ctypes.windll.kernel32.GetLogicalDrives()
            for i in range(26):
                if mask & (1 << i):
                    roots.append({"name": "%s: 盘" % chr(65 + i), "path": "%s:\\" % chr(65 + i)})
        except Exception:  # noqa
            for c in "CDEFGHIJ":
                if os.path.exists(c + ":\\"):
                    roots.append({"name": c + ": 盘", "path": c + ":\\"})
    else:
        roots.append({"name": "根目录", "path": "/"})
        for base in (["/Volumes"] if IS_MAC else ["/media", "/mnt", "/run/media"]):
            try:
                for e in sorted(os.listdir(base)):
                    p = os.path.join(base, e)
                    if os.path.isdir(p):
                        roots.append({"name": e, "path": p})
            except OSError:
                pass
    for label, sub in (("主目录", ""), ("桌面", "Desktop"), ("图片", "Pictures"), ("下载", "Downloads"), ("文档", "Documents")):
        p = os.path.join(home, sub) if sub else home
        if os.path.isdir(p):
            roots.append({"name": label, "path": p})
    return roots


def api_list_dir(d):
    raw = (d.get("path") or "").strip()
    roots = list_roots()
    if not raw:
        return {"path": "", "parent": None, "dirs": [], "roots": roots}
    p = norm_path(raw)
    if not os.path.isdir(long_path(p)):
        raise UserError("文件夹不存在或无法访问：%s" % p)
    dirs = []
    try:
        with os.scandir(long_path(p)) as it:
            for e in it:
                try:
                    if e.is_dir() and not util.is_hidden_entry(e):
                        dirs.append({"name": e.name, "path": os.path.join(p, e.name)})
                except OSError:
                    pass
    except OSError as ex:
        raise UserError("无法读取文件夹：%s" % ex)
    dirs.sort(key=lambda x: x["name"].lower())
    parent = os.path.dirname(p.rstrip("\\/")) if p.rstrip("\\/") else ""
    if parent == p or (IS_WIN and re.fullmatch(r"[A-Za-z]:\\?", p)):
        parent = ""
    if parent and IS_WIN and re.fullmatch(r"[A-Za-z]:", parent):
        parent += "\\"
    return {"path": p, "parent": parent, "dirs": dirs, "roots": roots}


def ps_quote(s):
    return "'" + (s or "").replace("'", "''") + "'"


def api_pick_dir(d):
    initial = norm_path(d.get("initial") or "")
    title = d.get("title") or "选择文件夹"
    try:
        if IS_WIN:
            ps = ("Add-Type -AssemblyName System.Windows.Forms;[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                  "$d=New-Object System.Windows.Forms.FolderBrowserDialog;$d.Description=%s;$d.ShowNewFolderButton=$true;" % ps_quote(title)
                  + ("$d.SelectedPath=%s;" % ps_quote(initial) if initial else "")
                  + "$o=New-Object System.Windows.Forms.Form -Property @{TopMost=$true;ShowInTaskbar=$false};"
                    "if($d.ShowDialog($o) -eq 'OK'){Write-Output $d.SelectedPath}")
            enc = base64.b64encode(ps.encode("utf-16-le")).decode()
            r = subprocess.run(["powershell", "-NoProfile", "-STA", "-EncodedCommand", enc], capture_output=True,
                               timeout=900, creationflags=0x08000000)
            out = r.stdout.decode("utf-8", "ignore").strip()
        elif IS_MAC:
            script = 'POSIX path of (choose folder with prompt "%s"%s)' % (
                title.replace('"', ""),
                (' default location (POSIX file "%s")' % initial.replace('"', "")) if initial and os.path.isdir(initial) else "")
            r = subprocess.run(["osascript", "-e", "activate", "-e", script], capture_output=True, timeout=900)
            out = r.stdout.decode("utf-8", "ignore").strip()
        else:
            if not shutil.which("zenity"):
                return {"unsupported": True}
            r = subprocess.run(["zenity", "--file-selection", "--directory", "--title", title], capture_output=True, timeout=900)
            out = r.stdout.decode("utf-8", "ignore").strip()
    except Exception as ex:  # noqa
        return {"unsupported": True, "error": str(ex)}
    if not out:
        return {"cancelled": True}
    return {"path": norm_path(out)}


def api_open_path(d):
    p = norm_path(d.get("path"))
    if not p or not os.path.exists(long_path(p)):
        raise UserError("路径不存在：%s" % p)
    reveal = bool(d.get("reveal")) and os.path.isfile(long_path(p))
    if IS_WIN:
        if reveal:
            subprocess.Popen(["explorer", "/select,", p])
        else:
            os.startfile(p)
    elif IS_MAC:
        subprocess.Popen(["open", "-R", p] if reveal else ["open", p])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(p) if reveal else p])
    return {"ok": True}


def api_shutdown(d):
    j = STATE["job"]
    if j and j.state == "running":
        j.cancel.set()
    threading.Timer(0.3, lambda: STATE["httpd"].shutdown()).start()
    return {"ok": True}


API = {
    "info": api_info, "index": api_index, "index_status": api_index_status, "query": api_query,
    "file_info": api_file_info, "nl": api_nl, "infer": api_infer, "list_parse": api_list_parse, "run": api_run,
    "job_status": api_job_status, "job_cancel": api_job_cancel, "history": api_history, "undo": api_undo,
    "settings_get": api_settings_get, "settings_save": api_settings_save, "llm_test": api_llm_test,
    "presets_list": api_presets_list, "presets_load": api_presets_load, "presets_save": api_presets_save,
    "presets_delete": api_presets_delete, "list_dir": api_list_dir, "pick_dir": api_pick_dir,
    "open_path": api_open_path, "shutdown": api_shutdown,
}


# ================================================================== 预览图
def get_preview(rec, want):
    key = (rec.path, rec.size, rec.mtime, want)
    with THUMB_LOCK:
        if key in THUMBS:
            THUMBS.move_to_end(key)
            return THUMBS[key]
    exif = None
    if rec.kind in ("photo", "raw") or rec.ext in JPEG_EXTS:
        exif = META.get(rec, "exif")
    res = preview_image(rec.path, rec.ext, rec.size, exif, want)
    if want == "thumb" and res and len(res[0]) <= 2 << 20:
        with THUMB_LOCK:
            THUMBS[key] = res
            while len(THUMBS) > 800:
                THUMBS.popitem(last=False)
    return res


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = False


STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                ".ico": "image/x-icon", ".woff2": "font/woff2"}


class Handler(BaseHTTPRequestHandler):
    server_version = "FileExtractWorkbench"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype, extra=None):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        if path in ("/", "/index.html"):
            path = "/index.html"
        if path in ("/thumb", "/view", "/raw"):
            return self._media(path, parse_qs(u.query))
        name = path.lstrip("/")
        if re.fullmatch(r"[A-Za-z0-9_\-]+\.(html|css|js|svg|png|ico|woff2)", name):
            fp = os.path.join(APP_DIR, name)
            if os.path.isfile(fp):
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), STATIC_TYPES.get(os.path.splitext(name)[1], "application/octet-stream"))
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        self._send(404, b"Not Found", "text/plain")

    def _media(self, kind, q):
        if (q.get("t") or [""])[0] != TOKEN:
            return self._send(403, b"Forbidden", "text/plain")
        try:
            idx = HOLDER.require((q.get("gen") or [None])[0])
            rec = idx.recs[int((q.get("id") or ["-1"])[0])]
        except (UserError, ValueError, IndexError):
            return self._send(404, b"Not Found", "text/plain")
        try:
            if kind in ("/thumb", "/view"):
                res = get_preview(rec, "thumb" if kind == "/thumb" else "view")
                if not res:
                    return self._send(404, b"No preview", "text/plain")
                data, mime, orient = res
                return self._send(200, data, mime, {"X-Orient": str(orient or 1)})
            return self._stream(rec)
        except OSError:
            return self._send(404, b"Unreadable", "text/plain")

    def _stream(self, rec):
        mime = VIDEO_TYPES.get(rec.ext) or AUDIO_TYPES.get(rec.ext) or BROWSER_IMAGES.get(rec.ext) or \
            ("application/pdf" if rec.ext == "pdf" else None) or ("image/jpeg" if rec.ext in JPEG_EXTS else None)
        if mime is None:
            return self._send(415, b"Unsupported", "text/plain")
        size = rec.size
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        partial = False
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
                partial = True
        if start > end:
            return self._send(416, b"", "text/plain", {"Content-Range": "bytes */%d" % size})
        length = end - start + 1
        try:
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", mime)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if partial:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.end_headers()
            with open(long_path(rec.path), "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    b = f.read(min(left, 1 << 20))
                    if not b:
                        break
                    self.wfile.write(b)
                    left -= len(b)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_POST(self):
        path = urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if not path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        if self.headers.get("X-Token") != TOKEN:
            return self._json(403, {"error": "访问令牌无效，请关闭本页并重新双击启动脚本"})
        try:
            data = json.loads(raw.decode("utf-8") or "{}") if raw else {}
        except ValueError:
            return self._json(400, {"error": "请求格式错误"})
        fn = API.get(path[5:])
        if not fn:
            return self._json(404, {"error": "未知接口"})
        try:
            self._json(200, fn(data))
        except UserError as ex:
            self._json(400, {"error": str(ex)})
        except Exception as ex:  # noqa
            traceback.print_exc()
            self._json(500, {"error": "内部错误：%s" % ex})


def find_port(start):
    for p in range(start, start + 50):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    raise SystemExit("找不到可用端口")


def serve(port, open_browser):
    port = find_port(port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    STATE["httpd"] = httpd
    url = "http://127.0.0.1:%d/?t=%s" % (port, TOKEN)
    info = util.system_info()
    safe_print("=" * 64)
    safe_print(" 文件提取工作台 v%s（Python %s%s）" % (VERSION, info["engineVersion"], "，随包内置" if info.get("bundled") else ""))
    safe_print(" 系统：%s" % info["osLabel"])
    safe_print(" 工作台地址：%s" % url)
    safe_print(" 使用期间请不要关闭本窗口；关闭窗口即退出程序。")
    safe_print("=" * 64)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    safe_print("程序已退出。")


# ================================================================== 命令行
def run_cli(path, preview):
    with open(path, encoding="utf-8-sig") as f:
        d = json.load(f)
    p = d.get("plan") or d.get("config") or d
    p["rule"], skipped = rules.prune_invalid(p.get("rule"))
    for n in skipped:
        safe_print("[注意] 已忽略未填写完整的条件：" + n)
    if preview:
        from mcct import index as index_mod
        idx = index_mod.scan(p.get("sources") or [], p.get("scan") or {}, exclude=[(p.get("dest") or {}).get("root")])
        for e in idx.errors:
            safe_print("[注意] " + e)
        matched, stats, ctx = rules.evaluate(idx, p, {"hist": False})
        safe_print("规则：" + rules.describe_plan(p.get("rule"), p.get("limit")))
        safe_print("共 %d 个文件，匹配 %d 个（%s）" % (stats["scoped"], len(matched), human_size(sum(r.size for r in matched))))
        dest = p.get("dest") or {}
        root = planmod.check_dest(dest, idx, need_root=False)
        planned = planmod.plan_items(matched, dest, ctx, time.time(), root)
        for rec, target, rel, status in planned[:50]:
            safe_print("  %s  →  %s  [%s]" % (rec.rel, rel, planmod.STATUS_LABEL.get(status, status)))
        if len(planned) > 50:
            safe_print("  ……其余 %d 个省略" % (len(planned) - 50))
        return 0
    job = jobs.Job(p, "run", echo=True)
    jobs.run_job(job)
    if job.log_file:
        safe_print("明细：%s" % job.log_file)
    if job.state != "done":
        return 1
    return 2 if job.failed else 0


def main():
    ap = argparse.ArgumentParser(description="文件提取工作台")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--run", metavar="方案.json", help="不打开界面，按方案文件直接执行")
    ap.add_argument("--preview", action="store_true", help="与 --run 一起使用：只预览不执行")
    a = ap.parse_args()
    if a.run:
        sys.exit(run_cli(a.run, a.preview))
    serve(a.port, not a.no_browser)


if __name__ == "__main__":
    main()
