# -*- coding: utf-8 -*-
"""以例选文：根据用户勾选的几个样例文件，推断它们的共同点，给出可选的条件。"""
import os
import re
import time
from datetime import datetime

from .index import KIND_LABEL, KINDS
from .rules import Ctx, get_value, compile_cond, scoped
from .util import UserError, fmt_ts

_TOK = re.compile(r"\d+|[A-Za-z]+|[^\dA-Za-z]+")


def _tokens(s):
    return _TOK.findall(s)


def _kind_of(tok):
    if tok.isdigit():
        return "d"
    if re.fullmatch(r"[A-Za-z]+", tok):
        return "a"
    return "o"


def name_pattern(stems):
    toks = [_tokens(s) for s in stems]
    shapes = [[_kind_of(t) for t in ts] for ts in toks]
    if len(set(map(tuple, shapes))) == 1 and toks and toks[0]:
        parts = []
        wild = []
        literal_len = 0
        vary_digit = None
        for i in range(len(toks[0])):
            col = [ts[i] for ts in toks]
            k = shapes[0][i]
            if len(set(c.lower() for c in col)) == 1:
                parts.append(re.escape(col[0]))
                wild.append(col[0])
                literal_len += len(col[0])
            elif k == "d":
                lens = set(len(c) for c in col)
                parts.append(r"\d{%d}" % lens.pop() if len(lens) == 1 else r"\d+")
                wild.append("?" * len(col[0]) if len(set(len(c) for c in col)) == 1 else "*")
                vary_digit = i
            elif k == "a":
                parts.append("[A-Za-z]+")
                wild.append("*")
            else:
                parts.append(".+?")
                wild.append("*")
        if literal_len >= 2:
            return {"regex": "^" + "".join(parts) + "$", "wildcard": "".join(wild), "varyDigit": vary_digit}
    # 结构不同：找共同前缀 / 后缀
    low = [s.lower() for s in stems]
    prefix = os.path.commonprefix(low)
    suffix = os.path.commonprefix([s[::-1] for s in low])[::-1]
    out = {}
    if len(prefix) >= 2:
        out["prefix"] = stems[0][:len(prefix)]
    if len(suffix) >= 2:
        out["suffix"] = stems[0][len(stems[0]) - len(suffix):]
    return out


def _count(index, plan, cond, ctx):
    try:
        c = compile_cond(cond, datetime.now())
    except UserError:
        return None
    return sum(1 for r in scoped(index, plan) if c.test(r, ctx))


def infer(index, plan, ids):
    ids = [int(i) for i in ids or []]
    if not ids:
        raise UserError("请先在结果里勾选样例文件")
    recs = [index.recs[i] for i in ids if 0 <= i < len(index.recs)]
    if not recs:
        raise UserError("样例文件已不在当前索引中，请重新勾选")
    ctx = Ctx(index, plan.get("timeFallback") or "mtime")
    sug = []

    def add(key, label, cond, selected, note=""):
        sug.append({"key": key, "label": label, "cond": cond, "selected": selected, "note": note})

    # 类型 / 扩展名
    exts = sorted(set(r.ext for r in recs))
    kinds = sorted(set(r.kind for r in recs))
    add("ext", "扩展名是 " + "、".join(exts), {"field": "ext", "op": "in", "value": exts}, True)
    if len(kinds) == 1 and kinds[0] != "other":
        add("kind", "文件类型是 " + KIND_LABEL.get(kinds[0], kinds[0]), {"field": "kind", "op": "in", "value": kinds}, False,
            "比扩展名更宽：同类型的其他格式也会选中")
    # 文件名
    stems = [r.stem for r in recs]
    pat = name_pattern(stems) if len(recs) >= 2 else {"regex": "^" + re.escape(stems[0]) + "$"}
    if pat.get("regex") and len(recs) >= 2:
        add("pattern", "文件名格式像 " + pat["wildcard"], {"field": "stem", "op": "regex", "value": pat["regex"]}, True,
            "正则：" + pat["regex"])
    if pat.get("prefix"):
        add("prefix", "主文件名开头是“%s”" % pat["prefix"], {"field": "stem", "op": "startsWith", "value": pat["prefix"]}, True)
    if pat.get("suffix"):
        add("suffix", "主文件名结尾是“%s”" % pat["suffix"], {"field": "stem", "op": "endsWith", "value": pat["suffix"]}, False)
    nums = [get_value(r, "num", ctx) for r in recs]
    if len(recs) >= 2 and all(n is not None for n in nums) and len(set(nums)) > 1:
        add("num", "编号在 %d–%d 之间" % (min(nums), max(nums)), {"field": "num", "op": "between", "value": [min(nums), max(nums)]},
            False, "只选这个编号区间")
    # 时间
    photo_like = all(r.kind in ("photo", "raw", "video") for r in recs)
    tf = "exif.time" if photo_like else "mtime"
    times = [get_value(r, tf, ctx) for r in recs]
    try:
        ok_times = all(t is not None for t in times) and bool(datetime.fromtimestamp(min(times))) and bool(datetime.fromtimestamp(max(times)))
    except (OSError, OverflowError, ValueError):
        ok_times = False
    if ok_times:
        lo, hi = min(times), max(times)
        dlo, dhi = datetime.fromtimestamp(lo), datetime.fromtimestamp(hi)
        flabel = "拍摄时间" if tf == "exif.time" else "修改时间"
        tod_a = dlo.strftime("%H:%M")
        tod_b = dhi.strftime("%H:%M")
        if dlo.date() == dhi.date():
            add("tod", "%s每天 %s–%s" % (flabel, tod_a, tod_b), {"field": tf, "op": "timeOfDay", "value": [tod_a, tod_b]},
                hi - lo <= 4 * 3600)
            add("date", "%s在 %s" % (flabel, dlo.strftime("%Y-%m-%d")), {"field": tf, "op": "onDate", "value": [dlo.strftime("%Y-%m-%d")]},
                False)
        else:
            lts = [datetime.fromtimestamp(t) for t in times]
            tods = [x.hour * 3600 + x.minute * 60 for x in lts]
            if max(tods) - min(tods) <= 3 * 3600:
                a = min(lts, key=lambda x: x.hour * 3600 + x.minute * 60).strftime("%H:%M")
                b = max(lts, key=lambda x: x.hour * 3600 + x.minute * 60).strftime("%H:%M")
                add("tod", "%s每天 %s–%s" % (flabel, a, b), {"field": tf, "op": "timeOfDay", "value": [a, b]}, True,
                    "样例来自不同日期，但时刻接近")
            add("range", "%s在 %s 到 %s 之间" % (flabel, dlo.strftime("%Y-%m-%d %H:%M"), dhi.strftime("%Y-%m-%d %H:%M")),
                {"field": tf, "op": "between", "value": [dlo.strftime("%Y-%m-%d %H:%M"), dhi.strftime("%Y-%m-%d %H:%M")]},
                False)
    # 位置
    d1 = set(r.parts[0] if r.parts else "" for r in recs)
    folders = set(r.parts[-1] if r.parts else "" for r in recs)
    if len(d1) == 1 and list(d1)[0]:
        add("dir1", "一级文件夹是“%s”" % list(d1)[0], {"field": "dir1", "op": "eq", "value": list(d1)[0]}, False,
            "只在这一个文件夹里找")
    elif len(folders) == 1 and list(folders)[0] and len(d1) > 1:
        add("folder", "所在文件夹叫“%s”" % list(folders)[0], {"field": "folder", "op": "eq", "value": list(folders)[0]}, True,
            "样例分布在 %d 个一级文件夹里，但都在同名文件夹中" % len(d1))
    # 相机
    if photo_like:
        cams = set(get_value(r, "camera", ctx) for r in recs)
        if len(cams) == 1 and list(cams)[0]:
            add("camera", "相机是 %s" % list(cams)[0], {"field": "camera", "op": "eq", "value": list(cams)[0]}, False)
    # 大小
    sizes = [r.size for r in recs]
    if len(recs) >= 2:
        mb_lo = max(0, round(min(sizes) / 1048576 * 0.8, 2))
        mb_hi = round(max(sizes) / 1048576 * 1.25 + 0.01, 2)
        add("size", "大小在 %g–%g MB 之间" % (mb_lo, mb_hi), {"field": "size", "op": "between", "value": [mb_lo, mb_hi], "unit": "MB"},
            False)
    for s in sug:
        s["count"] = _count(index, plan, s["cond"], ctx)
    # 默认组合的数量
    sel = [s["cond"] for s in sug if s["selected"]]
    combined = None
    if sel:
        conds = [compile_cond(c, datetime.now()) for c in sel]
        combined = sum(1 for r in scoped(index, plan) if all(c.test(r, ctx) for c in conds))
    return {"examples": [{"id": r.id, "name": r.name, "rel": r.rel} for r in recs], "suggestions": sug,
            "combined": combined, "dirCount": len(d1)}
