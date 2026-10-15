# -*- coding: utf-8 -*-
"""字段、条件、规则编译与查询（数量、每条件卡掉数、分布、排序、分页）。"""
import fnmatch
import os
import re
import time
from datetime import datetime

from .index import KIND_LABEL, KINDS
from .meta import META
from .util import (UserError, Cancelled, parse_tod, parse_dt, preset_range, PRESET_LABELS, UNIT_SECONDS,
                   UNIT_LABELS, fmt_ts, human_size)

# ------------------------------------------------------------------ 字段
# key, 标签, 类型, 代价(0 快 / 2 要读文件头 / 3 要读全文), 分组, 说明
FIELDS = [
    ("name", "文件名", "str", 0, "文件名", "含扩展名，例如 IMG_0123.CR3"),
    ("stem", "主文件名", "str", 0, "文件名", "不含扩展名，例如 IMG_0123"),
    ("ext", "扩展名", "ext", 0, "文件名", "例如 jpg、cr3"),
    ("kind", "文件类型", "kind", 0, "文件名", "照片、RAW、视频、文档等大类"),
    ("num", "文件名中的编号", "num", 0, "文件名", "文件名里最后一串数字，IMG_0123 → 123"),
    ("namedate", "文件名中的日期", "time", 0, "文件名", "文件名里形如 20261014 的日期"),
    ("dir1", "一级文件夹", "str", 0, "位置", "来源文件夹下的第一层，例如机位文件夹"),
    ("dir2", "二级文件夹", "str", 0, "位置", "第二层文件夹"),
    ("folder", "所在文件夹", "str", 0, "位置", "文件直接所在的文件夹"),
    ("folders", "路径中任一文件夹", "str", 0, "位置", "从来源到文件之间的任意一层"),
    ("path", "相对路径", "str", 0, "位置", "相对来源文件夹的完整路径"),
    ("depth", "所在层级", "num", 0, "位置", "0 表示直接在来源文件夹里"),
    ("source", "来源文件夹", "str", 0, "位置", "来源文件夹的名称"),
    ("size", "大小", "size", 0, "时间与大小", ""),
    ("mtime", "修改时间", "time", 0, "时间与大小", "文件最后写入的时刻"),
    ("ctime", "创建时间", "time", 0, "时间与大小", "从存储卡拷到电脑后通常变成拷贝时刻"),
    ("exif.time", "拍摄时间", "time", 2, "照片与视频", "照片 EXIF / 视频文件内记录的拍摄时刻"),
    ("camera", "相机", "str", 2, "照片与视频", "品牌 + 型号，例如 Canon EOS R5"),
    ("exif.serial", "机身序列号", "str", 2, "照片与视频", ""),
    ("exif.lens", "镜头", "str", 2, "照片与视频", ""),
    ("exif.artist", "照片作者", "str", 2, "照片与视频", "EXIF / XMP 中的作者"),
    ("rating", "星级", "num", 2, "照片与视频", "0–5，读取文件内或同名 .xmp 中的评星"),
    ("creator", "创建人（任意来源）", "str", 3, "人员", "依次查文档作者、最后修改者、照片作者、文件所有者"),
    ("doc.author", "文档作者", "str", 2, "人员", "Word / Excel / PPT / PDF 内记录的作者"),
    ("doc.modifiedBy", "最后修改者", "str", 2, "人员", "Office 文档内记录"),
    ("owner", "文件所有者", "str", 2, "人员", "操作系统记录的账户"),
    ("doc.title", "文档标题", "str", 2, "内容", ""),
    ("content", "文件内容", "text", 3, "内容", "支持 txt、csv、Word、Excel、PPT；PDF 尽力而为"),
    ("list", "名单", "list", 1, "名单", "按客户给的编号或文件名清单查找"),
]
FIELD = {f[0]: {"key": f[0], "label": f[1], "type": f[2], "cost": f[3], "group": f[4], "hint": f[5]} for f in FIELDS}
TIME_FIELDS = [f[0] for f in FIELDS if f[2] == "time"]

OPS = {
    "str": ["contains", "notContains", "eq", "ne", "startsWith", "endsWith", "in", "notIn", "wildcard", "regex",
            "notRegex", "empty", "notEmpty"],
    "text": ["contains", "notContains", "regex"],
    "ext": ["in", "notIn"],
    "kind": ["in", "notIn"],
    "num": ["eq", "ne", "gt", "gte", "lt", "lte", "between", "empty", "notEmpty"],
    "size": ["gt", "gte", "lt", "lte", "between"],
    "time": ["timeOfDay", "onDate", "datePreset", "recent", "between", "after", "before", "empty", "notEmpty"],
    "list": ["match"],
}
OP_LABEL = {
    "contains": "包含", "notContains": "不包含", "eq": "等于", "ne": "不等于", "startsWith": "开头是",
    "endsWith": "结尾是", "in": "是其中之一", "notIn": "不是其中任何一个", "wildcard": "符合通配符",
    "regex": "符合正则", "notRegex": "不符合正则", "empty": "为空", "notEmpty": "不为空", "gt": "大于", "gte": "至少",
    "lt": "小于", "lte": "至多", "between": "介于", "timeOfDay": "每天的时段", "onDate": "在某一天",
    "datePreset": "在", "recent": "最近", "after": "晚于", "before": "早于", "match": "匹配",
}
SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}
LIST_MODES = {"stem": "主文件名相同", "name": "文件名相同", "contains": "文件名包含", "number": "编号相同"}


def registry():
    return {
        "fields": [FIELD[f[0]] for f in FIELDS],
        "ops": OPS,
        "opLabels": OP_LABEL,
        "kinds": [{"key": k, "label": l, "exts": e} for k, l, e in KINDS],
        "presets": PRESET_LABELS,
        "units": UNIT_LABELS,
        "listModes": LIST_MODES,
    }


def safe_localtime(v):
    try:
        return time.localtime(v)
    except (OSError, OverflowError, ValueError):
        return None


# ------------------------------------------------------------------ 取值
_NUM_RE = re.compile(r"\d+")
_DATE_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.]?(\d{2})[-_.]?(\d{2})(?!\d)")


class Ctx(object):
    def __init__(self, index, fallback="mtime", now=None, cancel=None):
        self.index = index
        self.fallback = fallback
        self.now = now or datetime.now()
        self.cancel = cancel
        self.list_rows = {}
        self.groups = {}


def _exif(rec):
    if rec.kind in ("photo", "raw", "video", "design") or rec.ext in ("heic", "heif"):
        return META.get(rec, "exif")
    return {}


def _doc(rec):
    if rec.kind in ("word", "sheet", "slides", "pdf"):
        return META.get(rec, "doc")
    return {}


def get_value(rec, field, ctx):
    if field == "name":
        return rec.name
    if field == "stem":
        return rec.stem
    if field == "ext":
        return rec.ext
    if field == "kind":
        return rec.kind
    if field == "path":
        return rec.rel
    if field == "dir1":
        return rec.parts[0] if rec.parts else ""
    if field == "dir2":
        return rec.parts[1] if len(rec.parts) > 1 else ""
    if field == "folder":
        return rec.parts[-1] if rec.parts else ctx.index.source_names[rec.src]
    if field == "folders":
        return rec.parts
    if field == "depth":
        return len(rec.parts)
    if field == "source":
        return ctx.index.source_names[rec.src]
    if field == "size":
        return rec.size
    if field == "mtime":
        return rec.mtime
    if field == "ctime":
        return rec.ctime if rec.ctime is not None else (rec.mtime if ctx.fallback != "none" else None)
    if field == "num":
        nums = _NUM_RE.findall(rec.stem)
        return int(nums[-1]) if nums else None
    if field == "namedate":
        m = _DATE_RE.search(rec.stem)
        if m:
            try:
                return time.mktime(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).timetuple())
            except (ValueError, OverflowError):
                return None
        return None
    if field == "exif.time":
        t = _exif(rec).get("time")
        if t is None:
            if ctx.fallback == "mtime":
                return rec.mtime
            if ctx.fallback == "ctime":
                return rec.ctime if rec.ctime is not None else rec.mtime
        return t
    if field == "camera":
        e = _exif(rec)
        make, model = e.get("make", ""), e.get("model", "")
        if make and model.lower().startswith(make.split()[0].lower()):
            return model
        return (make + " " + model).strip()
    if field == "exif.serial":
        return _exif(rec).get("serial", "")
    if field == "exif.lens":
        return _exif(rec).get("lens", "")
    if field == "exif.artist":
        return _exif(rec).get("artist", "")
    if field == "rating":
        return _exif(rec).get("rating")
    if field == "doc.author":
        return _doc(rec).get("author", "")
    if field == "doc.modifiedBy":
        return _doc(rec).get("modifiedBy", "")
    if field == "doc.title":
        return _doc(rec).get("title", "")
    if field == "owner":
        return META.get(rec, "owner")
    if field == "creator":
        vals = []
        d = _doc(rec)
        vals += [d.get("author", ""), d.get("modifiedBy", "")]
        vals.append(_exif(rec).get("artist", "") if rec.kind in ("photo", "raw") else "")
        vals.append(META.get(rec, "owner"))
        return " | ".join(v for v in vals if v)
    if field == "content":
        from .meta import CONTENT_EXTS
        if rec.ext in CONTENT_EXTS:
            return META.get(rec, "content")
        return ""
    raise UserError("未知字段：%s" % field)


# ------------------------------------------------------------------ 编译
def _as_list(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        items = v
    else:
        items = re.split(r"[\n\r,，;；、]+", str(v))
    return [str(x).strip() for x in items if str(x).strip()]


def _num(v, label):
    try:
        if isinstance(v, str):
            v = v.strip()
        return float(v)
    except (TypeError, ValueError):
        raise UserError("“%s”需要填写数字" % label)


class Node(object):
    cost = 0
    field = None
    is_group = False


class Cond(Node):
    def __init__(self, cid, field, op, test, cost, label, raw):
        self.id = cid
        self.field = field
        self.op = op
        self.test_value = test
        self.cost = cost
        self.label = label
        self.raw = raw

    def test(self, rec, ctx):
        return self.test_value(rec, ctx)


class Group(Node):
    is_group = True

    def __init__(self, gid, mode, children, raw):
        self.id = gid
        self.mode = mode
        self.children = sorted(children, key=lambda c: c.cost)
        self.cost = max([c.cost for c in children] + [0])
        self.raw = raw
        fields = set(c.field for c in children)
        self.field = fields.pop() if len(fields) == 1 else None

    def test(self, rec, ctx):
        if self.mode == "any":
            return any(c.test(rec, ctx) for c in self.children) if self.children else True
        if self.mode == "none":
            return not any(c.test(rec, ctx) for c in self.children)
        return all(c.test(rec, ctx) for c in self.children)


def compile_cond(c, now):
    field = c.get("field")
    op = c.get("op")
    f = FIELD.get(field)
    if not f:
        raise UserError("未知字段：%s" % field)
    ftype = f["type"]
    if op not in OPS[ftype]:
        raise UserError("“%s”不支持“%s”这个条件" % (f["label"], OP_LABEL.get(op, op)))
    label = f["label"]
    val = c.get("value")
    cost = f["cost"]

    def getter(rec, ctx):
        return get_value(rec, field, ctx)

    # ---- 字符串
    if ftype in ("str", "text"):
        multi = field == "folders"
        if op in ("empty", "notEmpty"):
            want_empty = op == "empty"

            def t(rec, ctx):
                v = getter(rec, ctx)
                e = (not v) if not multi else (len(v) == 0)
                return e == want_empty
            return Cond(c.get("id"), field, op, t, cost, label, c)
        if op in ("in", "notIn"):
            items = set(x.lower() for x in _as_list(val))
            if not items:
                raise UserError("“%s”的候选值不能为空" % label)
            neg = op == "notIn"

            def pred(s):
                return s.lower() in items
        elif op in ("regex", "notRegex"):
            pat = str(val or "")
            if not pat:
                raise UserError("“%s”的正则表达式不能为空" % label)
            if len(pat) > 1000:
                raise UserError("正则表达式太长")
            try:
                rx = re.compile(pat, re.IGNORECASE)
            except re.error as ex:
                raise UserError("正则表达式有误：%s（%s）" % (pat, ex))
            neg = op == "notRegex"

            def pred(s):
                return rx.search(s) is not None
        elif op == "wildcard":
            pats = _as_list(val)
            if not pats:
                raise UserError("“%s”的通配符不能为空" % label)
            rxs = [re.compile(fnmatch.translate(p), re.IGNORECASE) for p in pats]
            neg = False

            def pred(s):
                return any(r.match(s) for r in rxs)
        else:
            s0 = str(val if val is not None else "").strip()
            if s0 == "":
                raise UserError("“%s %s”后面需要填写内容" % (label, OP_LABEL[op]))
            needle = s0.lower()
            neg = op in ("notContains", "ne")
            if op in ("contains", "notContains"):
                def pred(s):
                    return needle in s.lower()
            elif op in ("eq", "ne"):
                def pred(s):
                    return s.lower() == needle
            elif op == "startsWith":
                def pred(s):
                    return s.lower().startswith(needle)
            else:
                def pred(s):
                    return s.lower().endswith(needle)

        if multi:
            def t(rec, ctx):
                hit = any(pred(p) for p in getter(rec, ctx))
                return hit != neg
        else:
            def t(rec, ctx):
                v = getter(rec, ctx)
                return pred(v or "") != neg
        return Cond(c.get("id"), field, op, t, cost, label, c)

    # ---- 扩展名 / 类型
    if ftype in ("ext", "kind"):
        items = set(x.lower().lstrip(".").lstrip("*.") for x in _as_list(val))
        if not items:
            raise UserError("请至少选择一种%s" % ("扩展名" if ftype == "ext" else "文件类型"))
        neg = op == "notIn"
        attr = "ext" if ftype == "ext" else "kind"

        def t(rec, ctx):
            return (getattr(rec, attr) in items) != neg
        return Cond(c.get("id"), field, op, t, cost, label, c)

    # ---- 数字 / 大小
    if ftype in ("num", "size"):
        mult = SIZE_UNITS.get(str(c.get("unit") or "MB").upper(), 1024 ** 2) if ftype == "size" else 1
        if op in ("empty", "notEmpty"):
            want = op == "empty"

            def t(rec, ctx):
                return (getter(rec, ctx) is None) == want
            return Cond(c.get("id"), field, op, t, cost, label, c)
        if op == "between":
            vals = val if isinstance(val, (list, tuple)) else _as_list(val)
            if len(vals) != 2:
                raise UserError("“%s 介于”需要填写两个数" % label)
            lo = _num(vals[0], label) * mult if str(vals[0]).strip() != "" else None
            hi = _num(vals[1], label) * mult if str(vals[1]).strip() != "" else None
            if lo is not None and hi is not None and lo > hi:
                lo, hi = hi, lo

            def t(rec, ctx):
                v = getter(rec, ctx)
                return v is not None and (lo is None or v >= lo) and (hi is None or v <= hi)
            return Cond(c.get("id"), field, op, t, cost, label, c)
        x = _num(val, label) * mult
        cmp = {"eq": lambda v: v == x, "ne": lambda v: v != x, "gt": lambda v: v > x, "gte": lambda v: v >= x,
               "lt": lambda v: v < x, "lte": lambda v: v <= x}[op]

        def t(rec, ctx):
            v = getter(rec, ctx)
            return v is not None and cmp(v)
        return Cond(c.get("id"), field, op, t, cost, label, c)

    # ---- 时间
    if ftype == "time":
        if op in ("empty", "notEmpty"):
            want = op == "empty"

            def t(rec, ctx):
                return (getter(rec, ctx) is None) == want
            return Cond(c.get("id"), field, op, t, cost, label, c)
        if op == "timeOfDay":
            vals = val if isinstance(val, (list, tuple)) else _as_list(val)
            if len(vals) != 2 or not str(vals[0]).strip() or not str(vals[1]).strip():
                raise UserError("“%s 每天的时段”需要开始和结束时间" % label)
            s = parse_tod(vals[0], False)
            e = parse_tod(vals[1], True)
            wrap = s >= e

            def t(rec, ctx):
                v = getter(rec, ctx)
                if v is None:
                    return False
                lt = safe_localtime(v)
                if lt is None:
                    return False
                tod = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec + (v % 1)
                return (tod >= s or tod < e) if wrap else (s <= tod < e)
            return Cond(c.get("id"), field, op, t, cost, label, c)
        if op == "onDate":
            dates = _as_list(val)
            if not dates:
                raise UserError("请选择日期")
            ranges = [(parse_dt(d, False), parse_dt(d, True)) for d in dates]
        elif op == "datePreset":
            ranges = [preset_range(str(val or "today"), now)]
        elif op == "recent":
            v = val if isinstance(val, dict) else {"amount": val, "unit": c.get("unit") or "days"}
            amount = _num(v.get("amount"), label)
            if amount <= 0:
                raise UserError("“最近”的时长必须大于 0")
            unit = v.get("unit") or "days"
            since = time.mktime(now.timetuple()) - amount * UNIT_SECONDS.get(unit, 86400)
            ranges = [(since, None)]
        elif op == "between":
            vals = val if isinstance(val, (list, tuple)) else _as_list(val)
            if len(vals) != 2:
                raise UserError("“%s 介于”需要开始和结束" % label)
            a, b = parse_dt(vals[0], False), parse_dt(vals[1], True)
            if a is None and b is None:
                raise UserError("“%s 介于”至少填写一端" % label)
            if a is not None and b is not None and b <= a:
                raise UserError("结束时间必须晚于开始时间")
            ranges = [(a, b)]
        elif op == "after":
            ranges = [(parse_dt(val, False), None)]
            if ranges[0][0] is None:
                raise UserError("请填写时间")
        elif op == "before":
            b = parse_dt(val, False)
            if b is None:
                raise UserError("请填写时间")
            ranges = [(None, b)]
        else:
            raise UserError("未知条件：%s" % op)

        def t(rec, ctx):
            v = getter(rec, ctx)
            if v is None:
                return False
            for a, b in ranges:
                if (a is None or v >= a) and (b is None or v < b):
                    return True
            return False
        return Cond(c.get("id"), field, op, t, cost, label, c)

    # ---- 名单
    if ftype == "list":
        v = val if isinstance(val, dict) else {}
        keys = [str(k).strip() for k in (v.get("keys") or []) if str(k).strip()]
        if not keys:
            raise UserError("名单是空的")
        mode = v.get("mode") or "stem"
        rows = v.get("rows") or []
        key_rows = {}
        for i, k in enumerate(keys):
            key_rows.setdefault(_list_key(k, mode), i)
        if mode == "contains":
            ordered = sorted(set(k.lower() for k in keys), key=len, reverse=True)
            rx = re.compile("|".join(re.escape(k) for k in ordered), re.IGNORECASE)
            lowmap = {}
            for i, k in enumerate(keys):
                lowmap.setdefault(k.lower(), i)

            def find(rec):
                m = rx.search(rec.name)
                return lowmap.get(m.group(0).lower()) if m else None
        elif mode == "number":
            def find(rec):
                for n in reversed(_NUM_RE.findall(rec.stem)):
                    i = key_rows.get(_list_key(n, "number"))
                    if i is not None:
                        return i
                return None
        else:
            def find(rec):
                return key_rows.get((rec.stem if mode == "stem" else rec.name).lower())

        def t(rec, ctx):
            i = find(rec)
            if i is None:
                return False
            ctx.list_rows[rec.id] = i
            return True
        cond = Cond(c.get("id"), field, op, t, cost, label, c)
        cond.list_keys = keys
        cond.list_rows = rows
        cond.list_columns = v.get("columns") or []
        cond.list_find = find
        return cond
    raise UserError("未知字段类型")


def _list_key(k, mode):
    k = str(k).strip()
    if mode == "number":
        nums = _NUM_RE.findall(k)
        return str(int(nums[-1])) if nums else k.lower()
    if mode == "stem":
        return os.path.splitext(k)[0].lower() if "." in k and len(k.rsplit(".", 1)[1]) <= 5 else k.lower()
    return k.lower()


def compile_node(node, now, path="root"):
    if node.get("group") or "items" in node:
        mode = node.get("mode") or "all"
        if mode not in ("all", "any", "none"):
            raise UserError("未知的组合方式：%s" % mode)
        children = []
        for i, ch in enumerate(node.get("items") or []):
            if ch.get("off"):
                continue
            children.append(compile_node(ch, now, "%s.%d" % (path, i)))
        return Group(node.get("id") or path, mode, children, node)
    return compile_cond(node, now)


def compile_rule(rule, now=None):
    now = now or datetime.now()
    rule = rule or {"mode": "all", "items": []}
    return compile_node(dict(rule, group=True), now)


def validate_items(items, now=None):
    """检查外部（自然语言 / 大模型）生成的条件，返回 (可用的, 错误说明列表)。"""
    ok, errs = [], []
    for it in items or []:
        try:
            compile_node(it, now or datetime.now())
            ok.append(it)
        except UserError as ex:
            errs.append(str(ex))
        except Exception as ex:  # noqa
            errs.append("无法识别的条件：%s" % ex)
    return ok, errs


def prune_invalid(rule, now=None):
    """去掉停用和填写不完整的条件（命令行执行时使用），返回 (规则, 被忽略的说明)。"""
    notes = []

    def walk(node):
        out = dict(node)
        items = []
        for it in node.get("items") or []:
            if it.get("off"):
                continue
            if it.get("group") or "items" in it:
                g = walk(it)
                if g["items"]:
                    items.append(g)
                continue
            try:
                compile_cond(it, now or datetime.now())
                items.append(it)
            except UserError as ex:
                notes.append("%s：%s" % (describe_cond(it), ex))
        out["items"] = items
        return out
    return walk(rule or {"mode": "all", "items": []}), notes


# ------------------------------------------------------------------ 描述
def _fmt_val_time(c):
    op, v = c.get("op"), c.get("value")
    if op == "timeOfDay":
        a, b = (v or ["", ""])[:2]
        return "每天 %s–%s" % (a, b)
    if op == "onDate":
        return "在 " + "、".join(_as_list(v))
    if op == "datePreset":
        return "在" + PRESET_LABELS.get(v, str(v))
    if op == "recent":
        v = v if isinstance(v, dict) else {"amount": v, "unit": c.get("unit") or "days"}
        amt = v.get("amount")
        try:
            amt = ("%g" % float(amt))
        except (TypeError, ValueError):
            pass
        return "在最近 %s %s内" % (amt, UNIT_LABELS.get(v.get("unit"), ""))
    if op == "between":
        a, b = (list(v or []) + ["", ""])[:2]
        if a and b:
            return "在 %s 到 %s 之间" % (a, b)
        return ("晚于 %s" % a) if a else ("早于 %s" % b)
    if op == "after":
        return "晚于 %s" % v
    if op == "before":
        return "早于 %s" % v
    return OP_LABEL.get(op, op)


def describe_cond(c):
    f = FIELD.get(c.get("field"), {"label": c.get("field"), "type": "str"})
    op, v = c.get("op"), c.get("value")
    label = f["label"]
    t = f["type"]
    if op in ("empty", "notEmpty"):
        return "%s%s" % (label, OP_LABEL[op])
    if t == "time":
        return "%s%s" % (label, _fmt_val_time(c))
    if t == "kind":
        names = [KIND_LABEL.get(x, x) for x in _as_list(v)]
        return "%s%s %s" % (label, "是" if op == "in" else "不是", "、".join(names))
    if t == "ext":
        return "扩展名%s %s" % ("是" if op == "in" else "不是", "、".join(_as_list(v)))
    if t == "size":
        unit = c.get("unit") or "MB"
        if op == "between":
            a, b = (list(v or []) + ["", ""])[:2]
            return "大小在 %s–%s %s 之间" % (a, b, unit)
        return "大小%s %s %s" % (OP_LABEL[op], v, unit)
    if t == "num":
        if op == "between":
            a, b = (list(v or []) + ["", ""])[:2]
            return "%s在 %s–%s 之间" % (label, a, b)
        return "%s%s %s" % (label, OP_LABEL[op], v)
    if t == "list":
        v = v or {}
        return "名单匹配 %d 项（%s）" % (len(v.get("keys") or []), LIST_MODES.get(v.get("mode"), ""))
    if op in ("in", "notIn"):
        return "%s%s %s" % (label, "是" if op == "in" else "不是", "、".join(_as_list(v)))
    return "%s%s“%s”" % (label, OP_LABEL.get(op, op), v)


def describe_node(n, top=False):
    if n.get("group") or "items" in n:
        items = [x for x in (n.get("items") or []) if not x.get("off")]
        parts = [describe_node(x) for x in items]
        if not parts:
            return "全部文件" if top else ""
        mode = n.get("mode") or "all"
        if mode == "any":
            s = "满足其一：" + "；".join(parts) if not top else "；或者 ".join(parts)
        elif mode == "none":
            s = "排除：" + "；".join(parts)
        else:
            s = "，并且".join(parts) if top else "全部满足：" + "；".join(parts)
        return s if top else "（%s）" % s
    return describe_cond(n)


LIMIT_PER = {"none": "", "dir1": "每个一级文件夹", "dir2": "每个二级文件夹", "folder": "每个所在文件夹", "source": "每个来源"}
LIMIT_ORDER = {"newest": "最新的", "oldest": "最早的", "largest": "最大的", "smallest": "最小的", "nameAsc": "按名称排在最前的",
               "nameDesc": "按名称排在最后的"}


def describe_plan(rule, limit):
    s = describe_node(dict(rule or {"items": []}, group=True), top=True)
    if limit and limit.get("enabled"):
        per = LIMIT_PER.get(limit.get("per") or "none", "")
        by = FIELD.get(limit.get("by") or "mtime", {}).get("label", "")
        order = limit.get("order") or "newest"
        s += "；%s只取%s %s 个%s" % (per or "总共", LIMIT_ORDER.get(order, ""), limit.get("count"),
                                 ("（按%s）" % by) if order in ("newest", "oldest") else "")
    return s


# ------------------------------------------------------------------ 查询
def scoped(index, plan):
    excl = set((plan.get("scan") or {}).get("excludeDirs") or [])
    if not excl:
        return index.recs
    out = []
    for r in index.recs:
        key = "%d|%s" % (r.src, r.parts[0] if r.parts else "")
        if key not in excl:
            out.append(r)
    return out


def limit_key(rec, per):
    if per == "dir1":
        return (rec.src, rec.parts[0] if rec.parts else "")
    if per == "dir2":
        return (rec.src,) + rec.parts[:2]
    if per == "folder":
        return (rec.src,) + rec.parts
    if per == "source":
        return rec.src
    return 0


def apply_limit(recs, limit, ctx):
    if not limit or not limit.get("enabled"):
        return recs
    try:
        n = max(0, int(limit.get("count") or 0))
    except (TypeError, ValueError):
        raise UserError("数量上限需要填写整数")
    per = limit.get("per") or "none"
    order = limit.get("order") or "newest"
    by = limit.get("by") or "mtime"
    if by not in TIME_FIELDS:
        by = "mtime"
    groups = {}
    for r in recs:
        groups.setdefault(limit_key(r, per), []).append(r)
    keep = set()
    for lst in groups.values():
        if order in ("newest", "oldest"):
            withv = [(get_value(r, by, ctx), r) for r in lst]
            has = [x for x in withv if x[0] is not None]
            has.sort(key=lambda x: (x[0], x[1].name.lower()), reverse=(order == "newest"))
            ordered = [r for _, r in has] + [r for v, r in withv if v is None]
        elif order in ("largest", "smallest"):
            ordered = sorted(lst, key=lambda r: r.size, reverse=(order == "largest"))
        else:
            ordered = sorted(lst, key=lambda r: r.name.lower(), reverse=(order == "nameDesc"))
        keep.update(r.id for r in ordered[:n])
    return [r for r in recs if r.id in keep]


HIST_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800,
              604800, 1209600, 2592000, 7776000, 15552000, 31536000]
TOD_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600]


def histogram(values, mode):
    """values: [(时间戳, 是否匹配)]。"""
    if not values:
        return None
    values = [(v, m) for v, m in values if -62135596800 < v < 253402300799]
    if not values:
        return None
    if mode == "tod":
        pts = []
        for v, m in values:
            lt = safe_localtime(v)
            if lt is None:
                continue
            pts.append((lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec, m))
        steps, maxbins = TOD_STEPS, 96
    else:
        pts = [(v, m) for v, m in values]
        steps, maxbins = HIST_STEPS, 90
    lo = min(p[0] for p in pts)
    hi = max(p[0] for p in pts)
    span = max(hi - lo, 1)
    size = steps[-1]
    for s in steps:
        if span / s <= maxbins:
            size = s
            break
    if mode == "tod":
        start = int(lo // size * size)
    else:
        lt0 = safe_localtime(lo)
        off = (lt0.tm_gmtoff or 0) if (lt0 is not None and size >= 3600) else 0
        start = int((lo + off) // size * size - off)
    nbins = int((hi - start) // size) + 1
    total = [0] * nbins
    matched = [0] * nbins
    for v, m in pts:
        i = min(int((v - start) // size), nbins - 1)
        total[i] += 1
        if m:
            matched[i] += 1
    return {"mode": mode, "start": start, "size": size, "total": total, "matched": matched, "min": lo, "max": hi,
            "count": len(pts)}


SORTS = {"path", "name", "time", "size", "shot"}


def evaluate(index, plan, opts=None, cancel_check=None):
    """返回 (匹配记录列表, 统计信息, ctx)。"""
    opts = opts or {}
    now = datetime.now()
    ctx = Ctx(index, (plan.get("timeFallback") or "mtime"), now)
    root = compile_rule(plan.get("rule"), now)
    recs = scoped(index, plan)
    items = root.children if root.children else []
    n = len(items)
    hist_field = opts.get("histField") or first_time_field(plan.get("rule")) or "mtime"
    if hist_field not in TIME_FIELDS:
        hist_field = "mtime"
    want_hist = bool(opts.get("hist", True))
    hset = set(i for i, it in enumerate(items) if it.field == hist_field)
    unique_block = [0] * n
    hits = [0] * n
    matched = []
    hist_vals = []
    mode = root.mode
    order = sorted(range(n), key=lambda i: items[i].cost)
    non_h = [i for i in order if i not in hset]
    only_h = [i for i in order if i in hset]
    for k, rec in enumerate(recs):
        if cancel_check is not None and k % 256 == 0:
            cancel_check()
        if n == 0:
            matched.append(rec)
            if want_hist:
                hv = get_value(rec, hist_field, ctx)
                if hv is not None:
                    hist_vals.append((hv, True, rec.id))
            continue
        if mode == "all":
            fails = []
            for i in non_h:
                if not items[i].test(rec, ctx):
                    fails.append(i)
                    if len(fails) >= 2:
                        break
            if len(fails) >= 2:
                continue
            hfail = []
            for i in only_h:
                if not items[i].test(rec, ctx):
                    hfail.append(i)
                    if len(hfail) + len(fails) >= 2:
                        break
            allf = fails + hfail
            if not allf:
                matched.append(rec)
            elif len(allf) == 1:
                unique_block[allf[0]] += 1
            if want_hist and not fails:
                hv = get_value(rec, hist_field, ctx)
                if hv is not None:
                    hist_vals.append((hv, not allf, rec.id))
        else:
            res = [items[i].test(rec, ctx) for i in range(n)]
            for i, r in enumerate(res):
                if r:
                    hits[i] += 1
            ok = any(res) if mode == "any" else (not any(res))
            if ok:
                matched.append(rec)
            if want_hist:
                hv = get_value(rec, hist_field, ctx)
                if hv is not None:
                    hist_vals.append((hv, ok, rec.id))
    before_limit = len(matched)
    matched = apply_limit(matched, plan.get("limit"), ctx)
    if len(matched) != before_limit and want_hist:
        keep = set(r.id for r in matched)
        hist_vals = [(v, m and rid in keep, rid) for v, m, rid in hist_vals]
    stats = {
        "scoped": len(recs),
        "beforeLimit": before_limit,
        "items": [{"id": items[i].id, "uniqueBlock": unique_block[i], "hits": hits[i]} for i in range(n)],
        "mode": mode,
        "histField": hist_field,
        "histValues": [(v, m) for v, m, _ in hist_vals],
    }
    # 名单报告
    list_cond = None
    for it in items:
        if not it.is_group and it.field == "list":
            list_cond = it
            break
    if list_cond is not None:
        counts = [0] * len(list_cond.list_keys)
        for r in matched:
            i = ctx.list_rows.get(r.id)
            if i is None:
                i = list_cond.list_find(r)
                if i is not None:
                    ctx.list_rows[r.id] = i
            if i is not None:
                counts[i] += 1
        missing = [k for k, cnt in zip(list_cond.list_keys, counts) if cnt == 0]
        stats["list"] = {"total": len(counts), "found": sum(1 for x in counts if x == 1),
                         "multiple": sum(1 for x in counts if x > 1), "missing": len(missing),
                         "missingKeys": missing[:2000],
                         "multipleKeys": [k for k, cnt in zip(list_cond.list_keys, counts) if cnt > 1][:500]}
        ctx.list_cond = list_cond
    # 第 N 段：文件名 / 主文件名上的第一个正则条件
    for it in items:
        if not it.is_group and it.field in ("name", "stem") and it.op == "regex":
            ctx.group_regex = (it.field, re.compile(str(it.raw.get("value")), re.IGNORECASE))
            break
    return matched, stats, ctx


def first_time_field(rule):
    for it in (rule or {}).get("items") or []:
        if it.get("off"):
            continue
        if it.get("items") is not None:
            f = first_time_field(it)
            if f:
                return f
        elif FIELD.get(it.get("field"), {}).get("type") == "time":
            return it.get("field")
    return None


def sort_recs(recs, sort, ctx, field):
    sort = sort if sort in SORTS else "path"
    if sort == "name":
        return sorted(recs, key=lambda r: r.name.lower())
    if sort == "size":
        return sorted(recs, key=lambda r: -r.size)
    if sort in ("time", "shot"):
        f = field if sort == "time" else "exif.time"
        return sorted(recs, key=lambda r: (get_value(r, f, ctx) or 0, r.name.lower()))
    return sorted(recs, key=lambda r: (r.src, [p.lower() for p in r.parts], r.name.lower()))


def facets(recs, matched):
    mids = set(r.id for r in matched)
    kinds, exts, dirs = {}, {}, {}
    for r in recs:
        m = r.id in mids
        for d, key in ((kinds, r.kind), (exts, r.ext), (dirs, (r.src, r.parts[0] if r.parts else ""))):
            v = d.get(key)
            if v is None:
                v = d[key] = [0, 0]
            v[0] += 1
            if m:
                v[1] += 1
    return {
        "kinds": sorted(([k, KIND_LABEL.get(k, k), v[0], v[1]] for k, v in kinds.items()), key=lambda x: -x[2]),
        "exts": sorted(([k, v[0], v[1]] for k, v in exts.items()), key=lambda x: -x[1])[:40],
        "dirs": sorted(([k[0], k[1], v[0], v[1]] for k, v in dirs.items()), key=lambda x: (x[0], x[1].lower())),
    }


def item_json(rec, ctx, field):
    out = {"id": rec.id, "name": rec.name, "rel": rec.rel, "src": rec.src,
           "dir1": rec.parts[0] if rec.parts else "", "size": rec.size, "mtime": rec.mtime, "kind": rec.kind,
           "ext": rec.ext}
    try:
        out["time"] = get_value(rec, field, ctx) if field != "mtime" else rec.mtime
    except Exception:  # noqa
        out["time"] = None
    cached = META.store.get((rec.path, rec.size, rec.mtime), {}).get("exif")
    if cached:
        out["orient"] = cached.get("orientation") or 1
        if cached.get("rating") is not None:
            out["rating"] = cached.get("rating")
    return out


def file_info(rec, index):
    """单个文件的全部信息（会读取文件头）。"""
    ctx = Ctx(index, "none")
    rows = []
    for f in FIELDS:
        key = f[0]
        if key in ("list", "folders", "content"):
            continue
        try:
            v = get_value(rec, key, ctx)
        except Exception:  # noqa
            v = None
        if v in (None, "", ()):
            continue
        if f[2] == "time":
            v = fmt_ts(v)
        elif f[2] == "size":
            v = "%s（%d 字节）" % (human_size(v), v)
        elif f[2] == "kind":
            v = KIND_LABEL.get(v, v)
        rows.append([f[1], str(v)])
    return {"id": rec.id, "name": rec.name, "path": rec.path, "rows": rows}
