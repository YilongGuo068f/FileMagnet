# -*- coding: utf-8 -*-
"""执行任务：拷贝 / 移动 / 链接 / 打包 / 导出清单，重复文件识别，持续监视，撤销。"""
import csv
import json
import os
import shutil
import threading
import time
import traceback
import zipfile
from datetime import datetime

from . import index as index_mod
from .meta import META, file_hash
from .plan import plan_items, check_dest, zip_path, STATUS_LABEL
from .rules import evaluate, get_value, describe_plan
from .util import (UserError, Cancelled, IS_WIN, LOG_DIR, RUNS_DIR, long_path, fmt_ts, birth_time, human_size,
                   norm_path)

RESULT_LABEL = {"copied": "已拷贝", "moved": "已移动", "linked": "已链接", "zipped": "已打包", "listed": "已列入清单",
                "skipped": "已跳过", "failed": "失败", "duplicate": "重复，跳过"}
NO_COMPRESS = {"photo", "raw", "video", "audio", "archive"}


class Job(object):
    def __init__(self, plan, kind="run", echo=False):
        self.plan = plan
        self.kind = kind  # run / watch
        self.echo = echo
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.state = "running"
        self.phase = "准备"
        self.message = ""
        self.started = datetime.now()
        self.finished = None
        self.total = 0
        self.processed = 0
        self.done = 0
        self.skipped = 0
        self.failed = 0
        self.bytes_total = 0
        self.bytes_done = 0
        self.logs = []
        self.log_seq = 0
        self.run_id = self.started.strftime("%Y%m%d_%H%M%S_%f")[:-3]
        self.log_file = ""
        self.root = ""
        self.output = ""
        self.watch_rounds = 0

    def log(self, level, msg):
        with self.lock:
            self.log_seq += 1
            self.logs.append({"seq": self.log_seq, "t": datetime.now().strftime("%H:%M:%S"), "level": level,
                              "msg": msg})
            if len(self.logs) > 3000:
                del self.logs[:1000]
        if self.echo:
            from .util import safe_print
            safe_print("[%s] %s" % ({"error": "错误", "warn": "注意", "ok": "完成"}.get(level, "信息"), msg))

    def snapshot(self, since=0):
        with self.lock:
            logs = [l for l in self.logs if l["seq"] > since]
        end = self.finished or datetime.now()
        return {"state": self.state, "kind": self.kind, "phase": self.phase, "message": self.message,
                "runId": self.run_id, "elapsed": round((end - self.started).total_seconds(), 1),
                "total": self.total, "processed": self.processed, "done": self.done, "skipped": self.skipped,
                "failed": self.failed, "bytesTotal": self.bytes_total, "bytesDone": self.bytes_done,
                "logs": logs, "logSeq": self.log_seq, "logFile": self.log_file, "root": self.root,
                "output": self.output, "watchRounds": self.watch_rounds,
                "action": (self.plan.get("dest") or {}).get("action") or "copy"}


# ------------------------------------------------------------------ 文件操作
def _set_creation_time_win(path, ts):
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        h = k32.CreateFileW(path, 0x100, 0x7, None, 3, 0x80, None)
        if h in (None, ctypes.c_void_p(-1).value):
            return
        ft = int(ts * 10000000) + 116444736000000000
        ctime = wintypes.FILETIME(ft & 0xFFFFFFFF, ft >> 32)
        k32.SetFileTime(h, ctypes.byref(ctime), None, None)
        k32.CloseHandle(h)
    except Exception:  # noqa
        pass


def _makedirs(path, created):
    missing = []
    p = path
    while p and not os.path.isdir(long_path(p)):
        missing.append(p)
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    os.makedirs(long_path(path), exist_ok=True)
    for m in reversed(missing):
        created.append(m)


def copy_file(src, dst, job, dest_opts, size):
    tmp = dst + ".mcpart"
    try:
        with open(long_path(src), "rb") as fi, open(long_path(tmp), "wb") as fo:
            while True:
                if job.cancel.is_set():
                    raise Cancelled()
                chunk = fi.read(1 << 20)
                if not chunk:
                    break
                fo.write(chunk)
                job.bytes_done += len(chunk)
        if dest_opts.get("preserveTimes", True):
            try:
                shutil.copystat(long_path(src), long_path(tmp))
            except OSError:
                pass
            if IS_WIN:
                try:
                    b = birth_time(os.stat(long_path(src)))
                    if b:
                        _set_creation_time_win(long_path(tmp), b)
                except OSError:
                    pass
        if dest_opts.get("verify", True):
            got = os.path.getsize(long_path(tmp))
            if got != size:
                raise OSError("校验失败：大小不一致（源 %d，目标 %d）" % (size, got))
        os.replace(long_path(tmp), long_path(dst))
    except BaseException:
        try:
            if os.path.exists(long_path(tmp)):
                os.remove(long_path(tmp))
        except OSError:
            pass
        raise


def _backup(target, root, run_id, entry):
    rel = os.path.relpath(target, root)
    bk = os.path.join(root, ".mcct_backup", run_id, rel)
    os.makedirs(long_path(os.path.dirname(bk)), exist_ok=True)
    os.replace(long_path(target), long_path(bk))
    entry["backup"] = bk


def do_transfer(rec, target, status, job, dest, root, created, manifest):
    """执行单个文件。返回 (结果, 说明)。"""
    action = dest.get("action") or "copy"
    entry = {"src": rec.path, "dest": target, "size": rec.size, "mtime": rec.mtime, "action": action}
    if status in ("skip", "same", "older"):
        job.bytes_done += rec.size
        return "skipped", STATUS_LABEL[status]
    if status == "rename" and os.path.exists(long_path(target)):
        from .plan import unique_name
        target = unique_name(target, {})
        entry["dest"] = target
    _makedirs(os.path.dirname(target), created)
    if status in ("overwrite", "newer") and os.path.exists(long_path(target)):
        _backup(target, root, job.run_id, entry)
    if action == "move":
        try:
            os.replace(long_path(rec.path), long_path(target))
            job.bytes_done += rec.size
            entry["result"] = "moved"
            manifest.append(entry)
            return "moved", ""
        except OSError:
            copy_file(rec.path, target, job, dest, rec.size)
            os.remove(long_path(rec.path))
            entry["result"] = "moved"
            manifest.append(entry)
            return "moved", "跨磁盘：已拷贝后删除源文件"
    if action == "link":
        try:
            os.link(long_path(rec.path), long_path(target))
            job.bytes_done += rec.size
            entry["result"] = "linked"
            manifest.append(entry)
            return "linked", ""
        except (OSError, AttributeError) as ex:
            copy_file(rec.path, target, job, dest, rec.size)
            entry["result"] = "copied"
            manifest.append(entry)
            return "copied", "无法建立链接（%s），已改为拷贝" % (getattr(ex, "strerror", None) or "不同磁盘")
    copy_file(rec.path, target, job, dest, rec.size)
    entry["result"] = "copied"
    manifest.append(entry)
    return "copied", ""


def find_duplicates(planned, job):
    """同大小的文件计算哈希，返回重复的 rec.id 集合（保留第一个）。"""
    by_size = {}
    for item in planned:
        by_size.setdefault(item[0].size, []).append(item[0])
    dup = set()
    groups = [g for g in by_size.values() if len(g) > 1]
    for g in groups:
        seen = {}
        for rec in g:
            if job.cancel.is_set():
                raise Cancelled()
            try:
                h = META.get(rec, "hash")
            except OSError:
                continue
            if h in seen:
                dup.add(rec.id)
            else:
                seen[h] = rec.id
    return dup


def _write_manifest(job, manifest, created, extra):
    os.makedirs(RUNS_DIR, exist_ok=True)
    data = {"runId": job.run_id, "kind": job.kind, "startedAt": fmt_ts(job.started.timestamp()),
            "finishedAt": fmt_ts(time.time()), "root": job.root, "entries": manifest, "createdDirs": created,
            "summary": {"done": job.done, "skipped": job.skipped, "failed": job.failed}, "undone": False}
    data.update(extra)
    with open(os.path.join(RUNS_DIR, job.run_id + ".json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


# ------------------------------------------------------------------ 执行
def _prepare(job, cancel_check):
    plan = job.plan
    dest = plan.get("dest") or {}
    job.phase = "扫描来源"
    sources = plan.get("sources") or []
    if not sources:
        raise UserError("请先添加来源文件夹")
    idx = index_mod.scan(sources, plan.get("scan") or {}, exclude=[dest.get("root")], cancel=job.cancel)
    for e in idx.errors[:20]:
        job.log("warn", e)
    if not idx.recs:
        raise UserError("来源文件夹里没有找到文件")
    job.phase = "筛选文件"
    matched, stats, ctx = evaluate(idx, plan, {"hist": False}, cancel_check)
    root = check_dest(dest, idx, need_root=True)
    job.root = root
    run_ts = job.started.timestamp()
    planned = plan_items(matched, dest, ctx, run_ts, root, check_fs=(dest.get("action") not in ("zip", "list")))
    return idx, matched, stats, ctx, root, planned


def run_job(job):
    plan = job.plan
    dest = plan.get("dest") or {}
    action = dest.get("action") or "copy"
    csv_file = None
    manifest = []
    created = []
    extra = {"action": action, "description": describe_plan(plan.get("rule"), plan.get("limit"))}

    def cancel_check():
        if job.cancel.is_set():
            raise Cancelled()
    try:
        idx, matched, stats, ctx, root, planned = _prepare(job, cancel_check)
        job.total = len(planned)
        job.bytes_total = sum(p[0].size for p in planned)
        job.log("info", "筛选完成：%d 个文件中匹配 %d 个（%s）" % (stats["scoped"], len(planned), human_size(job.bytes_total)))
        if not planned:
            job.state = "done"
            job.message = "没有符合条件的文件"
            job.log("warn", job.message)
            return
        dups = set()
        if dest.get("dedupe"):
            job.phase = "识别重复文件"
            dups = find_duplicates(planned, job)
            if dups:
                job.log("info", "发现 %d 个内容重复的文件，将跳过" % len(dups))
        os.makedirs(LOG_DIR, exist_ok=True)
        job.log_file = os.path.join(LOG_DIR, "run_%s.csv" % job.run_id)
        csv_file = open(job.log_file, "w", newline="", encoding="utf-8-sig")
        writer = csv.writer(csv_file)
        writer.writerow(["时间", "源文件", "目标", "结果", "说明", "大小(字节)"])
        _makedirs(root, created)

        if action == "list":
            job.phase = "导出清单"
            out = os.path.join(root, "清单_%s.csv" % job.started.strftime("%Y%m%d_%H%M%S"))
            with open(long_path(out), "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["序号", "文件名", "来源路径", "大小(字节)", "修改时间", "拍摄时间", "规划的相对路径"])
                for i, (rec, target, rel, status) in enumerate(planned, 1):
                    cancel_check()
                    shot = ""
                    try:
                        if rec.kind in ("photo", "raw", "video"):
                            shot = fmt_ts(get_value(rec, "exif.time", ctx))
                    except Exception:  # noqa
                        pass
                    w.writerow([i, rec.name, rec.path, rec.size, fmt_ts(rec.mtime), shot, rel])
                    job.processed = job.done = i
                    job.bytes_done += rec.size
            job.output = out
            extra["output"] = out
            job.state = "done"
            job.message = "已导出清单：%d 个文件" % len(planned)
            job.log("ok", job.message + " → " + out)
            return

        if action == "zip":
            job.phase = "打包"
            out = zip_path(dest, root, job.started.timestamp())
            if os.path.exists(long_path(out)):
                from .plan import unique_name
                out = unique_name(out, {})
            job.output = out
            extra["output"] = out
            tmp = out + ".mcpart"
            try:
                with zipfile.ZipFile(long_path(tmp), "w", allowZip64=True) as z:
                    for rec, target, rel, status in planned:
                        cancel_check()
                        if rec.id in dups:
                            job.skipped += 1
                            job.processed += 1
                            writer.writerow([fmt_ts(time.time()), rec.path, rel, RESULT_LABEL["duplicate"], "", rec.size])
                            continue
                        ct = zipfile.ZIP_STORED if rec.kind in NO_COMPRESS else zipfile.ZIP_DEFLATED
                        try:
                            zi = zipfile.ZipInfo.from_file(long_path(rec.path), arcname=rel)
                            zi.compress_type = ct
                            with open(long_path(rec.path), "rb") as fi, z.open(zi, "w", force_zip64=rec.size > 2 ** 31) as fo:
                                while True:
                                    cancel_check()
                                    b = fi.read(1 << 20)
                                    if not b:
                                        break
                                    fo.write(b)
                                    job.bytes_done += len(b)
                            job.done += 1
                            res, note = "zipped", ""
                        except (OSError, ValueError) as ex:
                            job.failed += 1
                            res, note = "failed", str(ex)
                        job.processed += 1
                        writer.writerow([fmt_ts(time.time()), rec.path, rel, RESULT_LABEL[res], note, rec.size])
                        if res == "failed":
                            job.log("error", "%s → 失败（%s）" % (rec.rel, note))
                os.replace(long_path(tmp), long_path(out))
            except BaseException:
                try:
                    os.remove(long_path(tmp))
                except OSError:
                    pass
                raise
            job.state = "done"
            job.message = "已打包 %d 个文件 → %s" % (job.done, os.path.basename(out))
            job.log("ok", job.message)
            return

        job.phase = {"move": "移动文件", "link": "建立链接"}.get(action, "拷贝文件")
        for rec, target, rel, status in planned:
            cancel_check()
            if rec.id in dups:
                res, note = "duplicate", ""
                job.skipped += 1
                job.bytes_done += rec.size
            else:
                try:
                    res, note = do_transfer(rec, target, status, job, dest, root, created, manifest)
                    if res == "skipped":
                        job.skipped += 1
                    else:
                        job.done += 1
                except Cancelled:
                    raise
                except Exception as ex:  # noqa
                    res, note = "failed", str(getattr(ex, "strerror", None) or ex)
                    job.failed += 1
            job.processed += 1
            writer.writerow([fmt_ts(time.time()), rec.path, target, RESULT_LABEL.get(res, res), note, rec.size])
            level = "error" if res == "failed" else ("warn" if res in ("skipped", "duplicate") else "file")
            job.log(level, "%s → %s%s" % (rec.rel, RESULT_LABEL.get(res, res), ("（%s）" % note) if note else ""))
        verb = {"move": "移动", "link": "链接"}.get(action, "拷贝")
        job.state = "done"
        job.message = "完成：%s %d 个，跳过 %d 个，失败 %d 个" % (verb, job.done, job.skipped, job.failed)
        job.log("ok" if not job.failed else "warn", job.message)
    except Cancelled:
        job.state = "cancelled"
        job.message = "已停止（已处理 %d 个文件）" % job.processed
        job.log("warn", job.message)
    except UserError as ex:
        job.state = "error"
        job.message = str(ex)
        job.log("error", job.message)
    except Exception as ex:  # noqa
        job.state = "error"
        job.message = "运行出错：%s" % ex
        job.log("error", job.message)
        traceback.print_exc()
    finally:
        job.finished = datetime.now()
        if csv_file:
            try:
                csv_file.close()
            except Exception:  # noqa
                pass
        if manifest or job.output or created:
            try:
                _write_manifest(job, manifest, created, extra)
            except Exception:  # noqa
                traceback.print_exc()


def watch_job(job):
    """持续监视：每隔一段时间重新扫描，新出现且已写完的文件按规则处理。"""
    plan = job.plan
    dest = plan.get("dest") or {}
    if (dest.get("action") or "copy") in ("zip", "list"):
        job.state = "error"
        job.message = "持续监视只支持拷贝、移动、链接"
        job.finished = datetime.now()
        return
    interval = max(3, int(dest.get("watchInterval") or 10))
    processed = set()
    pending = {}
    manifest = []
    created = []
    first = True
    os.makedirs(LOG_DIR, exist_ok=True)
    job.log_file = os.path.join(LOG_DIR, "watch_%s.csv" % job.run_id)
    csv_file = open(job.log_file, "w", newline="", encoding="utf-8-sig")
    writer = csv.writer(csv_file)
    writer.writerow(["时间", "源文件", "目标", "结果", "说明", "大小(字节)"])

    def cancel_check():
        if job.cancel.is_set():
            raise Cancelled()
    try:
        include_existing = bool(dest.get("watchIncludeExisting", True))
        while not job.cancel.is_set():
            job.watch_rounds += 1
            job.phase = "检查新文件"
            try:
                idx, matched, stats, ctx, root, planned = _prepare(job, cancel_check)
            except UserError as ex:
                if "没有找到文件" in str(ex):
                    planned = []
                    root = job.root
                else:
                    raise
            now = time.time()
            todo = []
            for item in planned:
                rec = item[0]
                key = (rec.path, rec.size, rec.mtime)
                if key in processed:
                    continue
                if first and not include_existing:
                    processed.add(key)
                    continue
                prev = pending.get(rec.path)
                cur = (rec.size, rec.mtime)
                # 写入完成的判断：修改时间已过去 2 秒，且两次检查之间大小和时间都没变
                if now - rec.mtime >= 2 and (prev == cur or first):
                    todo.append(item)
                else:
                    pending[rec.path] = cur
            first = False
            job.total += len(todo)
            job.bytes_total += sum(i[0].size for i in todo)
            if todo:
                job.phase = "处理新文件"
            for rec, target, rel, status in todo:
                cancel_check()
                try:
                    res, note = do_transfer(rec, target, status, job, dest, root, created, manifest)
                    if res == "skipped":
                        job.skipped += 1
                    else:
                        job.done += 1
                except Cancelled:
                    raise
                except Exception as ex:  # noqa
                    res, note = "failed", str(getattr(ex, "strerror", None) or ex)
                    job.failed += 1
                processed.add((rec.path, rec.size, rec.mtime))
                pending.pop(rec.path, None)
                job.processed += 1
                writer.writerow([fmt_ts(time.time()), rec.path, target, RESULT_LABEL.get(res, res), note, rec.size])
                csv_file.flush()
                job.log("error" if res == "failed" else "file",
                        "%s → %s%s" % (rec.rel, RESULT_LABEL.get(res, res), ("（%s）" % note) if note else ""))
            job.phase = "等待新文件"
            job.message = "监视中：已处理 %d 个，跳过 %d 个，失败 %d 个" % (job.done, job.skipped, job.failed)
            if job.cancel.wait(interval):
                break
        raise Cancelled()
    except Cancelled:
        job.state = "cancelled"
        job.message = "已停止监视：共处理 %d 个，跳过 %d 个，失败 %d 个" % (job.done, job.skipped, job.failed)
        job.log("ok", job.message)
    except UserError as ex:
        job.state = "error"
        job.message = str(ex)
        job.log("error", job.message)
    except Exception as ex:  # noqa
        job.state = "error"
        job.message = "运行出错：%s" % ex
        job.log("error", job.message)
        traceback.print_exc()
    finally:
        job.finished = datetime.now()
        try:
            csv_file.close()
        except Exception:  # noqa
            pass
        if manifest or created:
            try:
                _write_manifest(job, manifest, created, {"action": dest.get("action") or "copy",
                                                         "description": "持续监视：" + describe_plan(plan.get("rule"), plan.get("limit"))})
            except Exception:  # noqa
                traceback.print_exc()


# ------------------------------------------------------------------ 历史与撤销
def history(limit=40):
    out = []
    if not os.path.isdir(RUNS_DIR):
        return out
    names = sorted((n for n in os.listdir(RUNS_DIR) if n.endswith(".json")), reverse=True)[:limit]
    for n in names:
        try:
            with open(os.path.join(RUNS_DIR, n), encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        out.append({"runId": d.get("runId"), "kind": d.get("kind"), "startedAt": d.get("startedAt"),
                    "action": d.get("action"), "root": d.get("root"), "output": d.get("output", ""),
                    "count": len(d.get("entries") or []), "summary": d.get("summary"), "undone": d.get("undone"),
                    "description": d.get("description", "")})
    return out


def undo(run_id):
    if not run_id or "/" in run_id or "\\" in run_id or ".." in run_id:
        raise UserError("记录编号无效")
    p = os.path.join(RUNS_DIR, run_id + ".json")
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        raise UserError("找不到这次执行的记录")
    if d.get("undone"):
        raise UserError("这次执行已经撤销过了")
    restored = removed = kept = 0
    notes = []
    out = d.get("output")
    if out and os.path.exists(long_path(out)):
        os.remove(long_path(out))
        removed += 1
    for e in reversed(d.get("entries") or []):
        dest, src = e.get("dest"), e.get("src")
        res = e.get("result")
        try:
            if res == "moved":
                if os.path.exists(long_path(dest)) and not os.path.exists(long_path(src)):
                    os.makedirs(long_path(os.path.dirname(src)), exist_ok=True)
                    try:
                        os.replace(long_path(dest), long_path(src))
                    except OSError:
                        shutil.move(long_path(dest), long_path(src))
                    restored += 1
                else:
                    kept += 1
                    notes.append("未能移回：%s" % src)
            else:
                if os.path.exists(long_path(dest)):
                    st = os.stat(long_path(dest))
                    if st.st_size == e.get("size") and abs(st.st_mtime - (e.get("mtime") or 0)) < 2.1:
                        os.remove(long_path(dest))
                        removed += 1
                    else:
                        kept += 1
                        notes.append("目标已被改动，保留：%s" % dest)
            bk = e.get("backup")
            if bk and os.path.exists(long_path(bk)) and not os.path.exists(long_path(dest)):
                os.replace(long_path(bk), long_path(dest))
                restored += 1
        except OSError as ex:
            kept += 1
            notes.append("%s：%s" % (dest, getattr(ex, "strerror", ex)))
    for dpath in sorted(d.get("createdDirs") or [], key=len, reverse=True):
        try:
            os.rmdir(long_path(dpath))
        except OSError:
            pass
    root = d.get("root") or ""
    bkroot = os.path.join(root, ".mcct_backup", run_id) if root else ""
    if bkroot and os.path.isdir(long_path(bkroot)):
        for dp, dns, fns in os.walk(long_path(bkroot), topdown=False):
            try:
                os.rmdir(dp)
            except OSError:
                pass
        try:
            os.rmdir(long_path(os.path.join(root, ".mcct_backup")))
        except OSError:
            pass
    d["undone"] = True
    d["undoneAt"] = fmt_ts(time.time())
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    msg = "已撤销：删除 %d 个，恢复 %d 个" % (removed, restored)
    if kept:
        msg += "，%d 个未处理" % kept
    return {"message": msg, "notes": notes[:50]}
