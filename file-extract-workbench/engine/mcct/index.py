# -*- coding: utf-8 -*-
"""扫描来源文件夹，建立内存索引。"""
import os
import threading
import time

from .util import UserError, Cancelled, norm_path, is_inside, is_hidden_entry, birth_time, long_path

KINDS = [
    ("photo", "照片", ["jpg", "jpeg", "jpe", "jfif", "png", "heic", "heif", "webp", "bmp", "gif", "tif", "tiff", "avif"]),
    ("raw", "RAW 原片", ["cr2", "cr3", "crw", "nef", "nrw", "arw", "srf", "sr2", "dng", "raf", "orf", "rw2", "pef",
                       "srw", "3fr", "iiq", "x3f", "erf", "kdc", "mos", "mef", "rwl", "dcr", "fff"]),
    ("video", "视频", ["mp4", "mov", "m4v", "avi", "mts", "m2ts", "mxf", "mkv", "wmv", "3gp", "webm", "flv", "mpg",
                     "mpeg", "braw", "r3d", "insv", "lrv"]),
    ("audio", "音频", ["mp3", "wav", "m4a", "aac", "flac", "ogg", "wma", "aif", "aiff", "amr"]),
    ("word", "文字文档", ["doc", "docx", "docm", "dotx", "rtf", "odt", "wps", "pages"]),
    ("sheet", "表格", ["xls", "xlsx", "xlsm", "xltx", "csv", "tsv", "ods", "et", "numbers"]),
    ("slides", "演示文稿", ["ppt", "pptx", "pptm", "potx", "odp", "dps", "key"]),
    ("pdf", "PDF", ["pdf"]),
    ("text", "文本", ["txt", "md", "log", "json", "xml", "yaml", "yml", "ini", "srt", "html", "htm"]),
    ("archive", "压缩包", ["zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "iso"]),
    ("design", "设计文件", ["psd", "psb", "ai", "eps", "indd", "sketch", "fig", "xd", "cdr", "svg", "afdesign"]),
    ("sidecar", "附属文件", ["xmp", "thm", "aae", "xml"]),
]
KIND_LABEL = {k: label for k, label, _ in KINDS}
KIND_LABEL["other"] = "其他"
EXT_KIND = {}
for _k, _l, _exts in KINDS:
    for _e in _exts:
        EXT_KIND.setdefault(_e, _k)


class Rec(object):
    __slots__ = ("id", "src", "rel", "name", "stem", "ext", "size", "mtime", "ctime", "parts", "path", "kind")

    def __init__(self, rid, src, rel, name, size, mtime, ctime, parts, path):
        self.id = rid
        self.src = src
        self.rel = rel
        self.name = name
        stem, ext = os.path.splitext(name)
        self.stem = stem
        self.ext = ext[1:].lower()
        self.size = size
        self.mtime = mtime
        self.ctime = ctime
        self.parts = parts
        self.path = path
        self.kind = EXT_KIND.get(self.ext, "other")


class Index(object):
    def __init__(self, gen, sources, opts):
        self.gen = gen
        self.sources = sources
        self.source_names = [os.path.basename(s.rstrip("\\/")) or s for s in sources]
        self.opts = opts
        self.recs = []
        self.errors = []
        self.built = time.time()
        self.scan_seconds = 0.0

    def summary(self):
        dirs = {}
        exts = {}
        kinds = {}
        size = 0
        for r in self.recs:
            key = (r.src, r.parts[0] if r.parts else "")
            d = dirs.get(key)
            if d is None:
                d = dirs[key] = [0, 0]
            d[0] += 1
            d[1] += r.size
            exts[r.ext] = exts.get(r.ext, 0) + 1
            kinds[r.kind] = kinds.get(r.kind, 0) + 1
            size += r.size
        dir_list = [{"src": k[0], "name": k[1], "count": v[0], "bytes": v[1]} for k, v in dirs.items()]
        dir_list.sort(key=lambda d: (d["src"], d["name"] == "", d["name"].lower()))
        return {
            "gen": self.gen,
            "sources": [{"path": p, "name": n} for p, n in zip(self.sources, self.source_names)],
            "total": len(self.recs),
            "bytes": size,
            "dirs": dir_list,
            "exts": sorted(([k, v] for k, v in exts.items()), key=lambda x: -x[1]),
            "kinds": sorted(([k, KIND_LABEL.get(k, k), v] for k, v in kinds.items()), key=lambda x: -x[2]),
            "errors": self.errors[:50],
            "seconds": round(self.scan_seconds, 2),
        }


def scan(sources, opts, exclude=(), progress=None, cancel=None, gen=0):
    """sources: 文件夹列表；opts: recursive, skipHidden, maxDepth。"""
    t0 = time.time()
    clean = []
    errors = []
    for s in sources or []:
        p = norm_path(s)
        if not p:
            continue
        if not os.path.isdir(long_path(p)):
            errors.append("来源文件夹不存在或无法访问：%s" % p)
            continue
        if any(is_inside(p, c) or is_inside(c, p) for c in clean):
            errors.append("来源文件夹与其他来源重叠，已忽略：%s" % p)
            continue
        clean.append(p)
    idx = Index(gen, clean, opts)
    idx.errors = errors
    skip_hidden = opts.get("skipHidden", True)
    max_depth = int(opts.get("maxDepth") or 0)
    excl = [norm_path(e) for e in exclude if e]
    recs = idx.recs
    last = time.time()
    for si, root in enumerate(clean):
        stack = [(root, ())]
        while stack:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            d, parts = stack.pop()
            try:
                with os.scandir(long_path(d)) as it:
                    entries = list(it)
            except OSError as ex:
                if len(idx.errors) < 200:
                    idx.errors.append("无法读取文件夹：%s（%s）" % (d, getattr(ex, "strerror", ex) or ex))
                continue
            entries.sort(key=lambda e: e.name.lower())
            subdirs = []
            for e in entries:
                try:
                    is_dir = e.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if skip_hidden and is_hidden_entry(e):
                    continue
                if is_dir:
                    full = os.path.join(d, e.name)
                    if any(is_inside(full, x) for x in excl):
                        continue
                    depth = len(parts) + 1
                    if not max_depth or depth <= max_depth:
                        subdirs.append((full, parts + (e.name,)))
                    continue
                try:
                    if not e.is_file():
                        continue
                    st = e.stat()
                except OSError:
                    continue
                if e.name.endswith(".mcpart"):
                    continue
                rel = os.sep.join(parts + (e.name,))
                recs.append(Rec(len(recs), si, rel, e.name, st.st_size, st.st_mtime, birth_time(st), parts,
                                os.path.join(d, e.name)))
            for sd in reversed(subdirs):
                stack.append(sd)
            if progress is not None and time.time() - last > 0.25:
                last = time.time()
                progress(len(recs), d)
    idx.scan_seconds = time.time() - t0
    if progress is not None:
        progress(len(recs), "")
    return idx


class IndexHolder(object):
    """当前索引 + 后台扫描状态。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.index = None
        self.gen = 0
        self.state = "idle"
        self.count = 0
        self.current = ""
        self.error = ""
        self.cancel = None
        self.thread = None

    def start(self, sources, opts, exclude=()):
        with self.lock:
            if self.cancel is not None:
                self.cancel.set()
            self.gen += 1
            gen = self.gen
            cancel = threading.Event()
            self.cancel = cancel
            self.state = "scanning"
            self.count = 0
            self.current = ""
            self.error = ""

        def work():
            def prog(n, cur):
                if self.gen == gen:
                    self.count = n
                    self.current = cur
            try:
                idx = scan(sources, opts, exclude, prog, cancel, gen)
                with self.lock:
                    if self.gen == gen:
                        self.index = idx
                        self.state = "ready"
            except Cancelled:
                pass
            except Exception as ex:  # noqa
                with self.lock:
                    if self.gen == gen:
                        self.state = "error"
                        self.error = str(ex)
        t = threading.Thread(target=work, daemon=True)
        self.thread = t
        t.start()
        return gen

    def status(self):
        out = {"state": self.state, "gen": self.gen, "count": self.count, "current": self.current, "error": self.error}
        if self.state == "ready" and self.index is not None and self.index.gen == self.gen:
            out["index"] = self.index.summary()
        return out

    def require(self, gen=None):
        idx = self.index
        if idx is None or self.state != "ready":
            raise UserError("文件索引还没准备好，请先添加来源文件夹并等待扫描完成")
        if gen is not None and int(gen) != idx.gen:
            raise UserError("索引已更新，请刷新结果")
        return idx


HOLDER = IndexHolder()
