# -*- coding: utf-8 -*-
"""去向规划：路径模板、重命名、重名处理、目标结构预览。"""
import os
import re
import time

from .index import KIND_LABEL
from .meta import META
from .rules import get_value
from .util import UserError, norm_path, is_inside, sanitize_segment, fmt_date_pattern, long_path

VAR_ALIASES = {
    "一级文件夹": "dir1", "二级文件夹": "dir2", "所在文件夹": "folder", "子目录": "subdir", "相对目录": "reldir",
    "来源": "source", "文件名": "name", "主文件名": "stem", "扩展名": "ext", "类型": "kind", "编号": "num",
    "相机": "camera", "作者": "creator", "所有者": "owner", "星级": "rating",
    "拍摄日期": "shot", "修改日期": "mdate", "创建日期": "cdate", "运行日期": "run", "运行时间": "runtime",
}
VAR_LIST = [
    ("{一级文件夹}", "例如机位文件夹名"), ("{二级文件夹}", ""), ("{所在文件夹}", "文件直接所在的文件夹"),
    ("{子目录}", "一级文件夹以下的原有层级"), ("{相对目录}", "保留来源里的完整层级"), ("{来源}", "来源文件夹名"),
    ("{文件名}", ""), ("{主文件名}", "不含扩展名"), ("{扩展名}", ""), ("{类型}", "照片、视频……"), ("{编号}", ""),
    ("{相机}", "相机型号"), ("{作者}", "创建人"), ("{星级}", ""),
    ("{拍摄日期:yyyy-MM-dd}", ""), ("{修改日期:yyyy-MM}", ""), ("{创建日期:yyyy-MM-dd}", ""),
    ("{运行日期:yyyyMMdd}", "执行当天"), ("{第1段}", "文件名正则里第 1 个括号"), ("{名单.列名}", "名单里的某一列"),
]
_VAR_RE = re.compile(r"\{([^{}:]+)(?::([^{}]*))?\}")
PATHY = {"subdir", "reldir"}


def resolve_var(name, fmt, rec, ctx, run_ts):
    name = name.strip()
    key = VAR_ALIASES.get(name, name)
    if key.startswith("名单.") or key.startswith("list."):
        col = key.split(".", 1)[1]
        cond = getattr(ctx, "list_cond", None)
        i = ctx.list_rows.get(rec.id)
        if cond is None or i is None:
            return ""
        cols = cond.list_columns or []
        rows = cond.list_rows or []
        if col in cols and i < len(rows):
            row = rows[i]
            j = cols.index(col)
            return str(row[j]) if j < len(row) else ""
        return ""
    m = re.fullmatch(r"(?:第(\d)段|g(\d))", key)
    if m:
        gi = int(m.group(1) or m.group(2))
        gr = getattr(ctx, "group_regex", None)
        if gr is None:
            return ""
        mm = gr[1].search(rec.name if gr[0] == "name" else rec.stem)
        if mm and gi <= (mm.re.groups or 0):
            return mm.group(gi) or ""
        return ""
    if key == "dir1":
        return rec.parts[0] if rec.parts else ""
    if key == "dir2":
        return rec.parts[1] if len(rec.parts) > 1 else ""
    if key == "folder":
        return rec.parts[-1] if rec.parts else ctx.index.source_names[rec.src]
    if key == "subdir":
        return "/".join(rec.parts[1:])
    if key == "reldir":
        return "/".join(rec.parts)
    if key == "source":
        return ctx.index.source_names[rec.src]
    if key == "name":
        return rec.name
    if key == "stem":
        return rec.stem
    if key == "ext":
        return rec.ext
    if key == "kind":
        return KIND_LABEL.get(rec.kind, rec.kind)
    if key == "num":
        v = get_value(rec, "num", ctx)
        return "" if v is None else str(v)
    if key == "camera":
        return get_value(rec, "camera", ctx) or "未知相机"
    if key == "creator":
        v = get_value(rec, "creator", ctx)
        return v.split(" | ")[0] if v else "未知"
    if key == "owner":
        return get_value(rec, "owner", ctx) or "未知"
    if key == "rating":
        v = get_value(rec, "rating", ctx)
        return "%d星" % v if v is not None else "未评星"
    if key in ("shot", "mdate", "cdate", "run", "runtime"):
        if key == "shot":
            ts = get_value(rec, "exif.time", ctx)
        elif key == "mdate":
            ts = rec.mtime
        elif key == "cdate":
            ts = rec.ctime if rec.ctime is not None else rec.mtime
        else:
            ts = run_ts
        default = "HHmmss" if key == "runtime" else "yyyy-MM-dd"
        return fmt_date_pattern(ts, fmt or default) if ts else "未知日期"
    raise UserError("路径模板里有未知的变量：{%s}" % name)


def render(template, rec, ctx, run_ts):
    def sub(m):
        key = VAR_ALIASES.get(m.group(1).strip(), m.group(1).strip())
        v = resolve_var(m.group(1), m.group(2), rec, ctx, run_ts)
        if key not in PATHY:
            v = str(v).replace("/", "_").replace("\\", "_")
        return v
    return _VAR_RE.sub(sub, template or "")


def dest_rel(dest, rec, ctx, run_ts):
    """返回目标的相对路径片段列表（含文件名）。"""
    tpl = dest.get("template")
    if tpl is None:
        tpl = "{一级文件夹}/{子目录}"
    segs = [sanitize_segment(s) for s in re.split(r"[\\/]+", render(tpl, rec, ctx, run_ts))]
    segs = [s for s in segs if s and s not in (".", "..")]
    fname = rec.name
    rn = (dest.get("rename") or "").strip()
    if rn:
        fname = sanitize_segment(render(rn, rec, ctx, run_ts).replace("/", "_").replace("\\", "_"))
        if rec.ext and not fname.lower().endswith("." + rec.ext):
            fname = fname + "." + rec.ext
        if not fname or fname == "." + rec.ext:
            fname = rec.name
    return segs + [fname]


def check_dest(dest, index, need_root=True):
    root = norm_path(dest.get("root"))
    if not root:
        if need_root:
            raise UserError("请先选择目标文件夹")
        return ""
    for s in index.sources:
        if is_inside(root, s):
            raise UserError("目标文件夹不能放在来源文件夹里面：%s" % root)
    return root


def same_file(rec, path):
    try:
        st = os.stat(long_path(path))
    except OSError:
        return False
    return st.st_size == rec.size and abs(st.st_mtime - rec.mtime) < 2.1


def unique_name(path, taken):
    base, ext = os.path.splitext(path)
    for i in range(1, 100000):
        c = "%s_%d%s" % (base, i, ext)
        if os.path.normcase(c) not in taken and not os.path.exists(long_path(c)):
            return c
    raise UserError("无法生成不重复的文件名：%s" % path)


STATUS_LABEL = {"new": "将新建", "same": "已有相同文件，跳过", "rename": "重名，自动改名", "skip": "重名，跳过",
                "overwrite": "重名，覆盖", "newer": "源较新，覆盖", "older": "目标更新，跳过"}


def plan_items(matched, dest, ctx, run_ts, root, check_fs=True):
    """返回 [(rec, 目标绝对路径, 相对路径, 状态)]。zip / list 动作不检查磁盘。"""
    action = dest.get("action") or "copy"
    conflict = dest.get("conflict") or "rename"
    out = []
    taken = {}
    for rec in matched:
        segs = dest_rel(dest, rec, ctx, run_ts)
        rel = "/".join(segs)
        target = os.path.join(root, *segs) if root else rel
        key = os.path.normcase(target)
        status = "new"
        if action in ("zip", "list"):
            if key in taken:
                target = unique_name(target, taken) if root else target + "_%d" % len(taken)
                status = "rename"
        else:
            exists = check_fs and root and os.path.exists(long_path(target))
            if key in taken:
                if conflict == "skip":
                    status = "skip"
                elif conflict == "overwrite":
                    status = "overwrite"
                else:
                    target = unique_name(target, taken)
                    status = "rename"
            elif exists:
                if conflict == "skip":
                    status = "skip"
                elif conflict == "overwrite":
                    status = "overwrite"
                elif conflict == "newer":
                    try:
                        status = "newer" if rec.mtime > os.stat(long_path(target)).st_mtime + 1 else "older"
                    except OSError:
                        status = "older"
                elif same_file(rec, target):
                    status = "same"
                else:
                    target = unique_name(target, taken)
                    status = "rename"
        taken[os.path.normcase(target)] = True
        rel = os.path.relpath(target, root).replace("\\", "/") if root else target
        out.append((rec, target, rel, status))
    return out


def tree(planned, limit=300):
    dirs = {}
    counts = {}
    for rec, target, rel, status in planned:
        d = rel.rsplit("/", 1)[0] if "/" in rel else ""
        v = dirs.get(d)
        if v is None:
            v = dirs[d] = [0, 0]
        v[0] += 1
        v[1] += rec.size
        counts[status] = counts.get(status, 0) + 1
    items = sorted(dirs.items(), key=lambda x: x[0].lower())
    return {"dirs": [[k, v[0], v[1]] for k, v in items[:limit]], "dirCount": len(items), "status": counts}


def zip_path(dest, root, run_ts):
    name = (dest.get("zipName") or "导出_{运行日期:yyyyMMdd_HHmmss}.zip")
    name = _VAR_RE.sub(lambda m: fmt_date_pattern(run_ts, m.group(2) or "yyyyMMdd_HHmmss")
                       if VAR_ALIASES.get(m.group(1), m.group(1)) in ("run", "runtime") else "", name)
    name = sanitize_segment(name) or "导出.zip"
    if not name.lower().endswith(".zip"):
        name += ".zip"
    return os.path.join(root, name)
