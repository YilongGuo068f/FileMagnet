# -*- coding: utf-8 -*-
"""解析名单：粘贴的文本、CSV、TXT、Excel（.xlsx）。"""
import base64
import csv
import html
import io
import re
import zipfile

from .util import UserError

MAX_ROWS = 50000
HEADER_WORDS = re.compile(r"编号|序号|文件名|名称|名字|照片|图片|客户|姓名|分组|备注|name|file|id|no\.?$|number|code", re.I)


def _decode(b):
    for enc in ("utf-8-sig", "utf-16"):
        try:
            if enc == "utf-16" and not b.startswith((b"\xff\xfe", b"\xfe\xff")):
                continue
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("gb18030", "replace")


def _col_index(ref):
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def parse_xlsx(data):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise UserError("无法读取这个 Excel 文件（只支持 .xlsx）")
    names = z.namelist()
    shared = []
    if "xl/sharedStrings.xml" in names:
        xml = z.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
        for si in re.findall(r"<si>(.*?)</si>", xml, re.S):
            shared.append(html.unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S))))
    sheet = "xl/worksheets/sheet1.xml"
    try:
        wb = z.read("xl/workbook.xml").decode("utf-8", "ignore")
        rels = z.read("xl/_rels/workbook.xml.rels").decode("utf-8", "ignore")
        m = re.search(r'<sheet\b[^>]*r:id="([^"]+)"', wb)
        if m:
            t = re.search(r'<Relationship\b[^>]*Id="%s"[^>]*Target="([^"]+)"' % re.escape(m.group(1)), rels) or \
                re.search(r'<Relationship\b[^>]*Target="([^"]+)"[^>]*Id="%s"' % re.escape(m.group(1)), rels)
            if t:
                target = t.group(1).lstrip("/")
                sheet = target if target.startswith("xl/") else "xl/" + target
    except KeyError:
        pass
    if sheet not in names:
        cands = sorted(n for n in names if n.startswith("xl/worksheets/sheet"))
        if not cands:
            raise UserError("Excel 文件里没有工作表")
        sheet = cands[0]
    xml = z.read(sheet).decode("utf-8", "ignore")
    rows = []
    for rm in re.finditer(r"<row\b[^>]*>(.*?)</row>", xml, re.S):
        cells = {}
        for cm in re.finditer(r"<c\b([^>]*?)(?:/>|>(.*?)</c>)", rm.group(1), re.S):
            attrs, body = cm.group(1), cm.group(2) or ""
            r = re.search(r'r="([A-Z]+)\d+"', attrs)
            t = re.search(r't="(\w+)"', attrs)
            typ = t.group(1) if t else ""
            if typ == "inlineStr":
                val = html.unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", body, re.S)))
            else:
                v = re.search(r"<v>(.*?)</v>", body, re.S)
                val = html.unescape(v.group(1)) if v else ""
                if typ == "s" and val.isdigit() and int(val) < len(shared):
                    val = shared[int(val)]
                elif re.fullmatch(r"-?\d+\.0", val):
                    val = val[:-2]
            col = _col_index(r.group(1)) if r else len(cells)
            cells[col] = val
        if cells:
            width = max(cells) + 1
            rows.append([cells.get(i, "") for i in range(width)])
        if len(rows) >= MAX_ROWS:
            break
    return rows


def parse_text(text):
    lines = [l for l in re.split(r"\r?\n", text) if l.strip()]
    if not lines:
        return []
    sample = "\n".join(lines[:20])
    if "\t" in sample:
        return [l.split("\t") for l in lines[:MAX_ROWS]]
    commas = sum(1 for l in lines[:20] if "," in l)
    if commas >= max(1, len(lines[:20]) // 2) and len(lines) > 1:
        return list(csv.reader(lines[:MAX_ROWS]))
    # 单列：每行一个，或一行里用逗号 / 顿号 / 空格隔开
    vals = []
    for l in lines:
        parts = [p for p in re.split(r"[\s,，、;；]+", l.strip()) if p]
        vals.extend(parts)
        if len(vals) >= MAX_ROWS:
            break
    return [[v] for v in vals[:MAX_ROWS]]


def parse(text=None, b64=None, filename="", header=None):
    fn = (filename or "").lower()
    if b64:
        try:
            data = base64.b64decode(b64)
        except (ValueError, TypeError):
            raise UserError("文件读取失败")
        if fn.endswith(".xlsx") or fn.endswith(".xlsm") or data[:2] == b"PK":
            rows = parse_xlsx(data)
        elif fn.endswith(".xls"):
            raise UserError("暂不支持旧版 .xls，请在 Excel 里另存为 .xlsx 或 .csv")
        else:
            rows = parse_text(_decode(data))
    else:
        rows = parse_text(text or "")
    rows = [[str(c).strip() for c in r] for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        raise UserError("名单是空的")
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    has_header = False
    if len(rows) > 1:
        first = rows[0]
        if any(HEADER_WORDS.search(c or "") for c in first):
            has_header = True
        elif width > 1 and all(not re.search(r"\d", c) for c in first if c) and any(re.search(r"\d", c) for c in rows[1]):
            has_header = True
    if header is not None and len(rows) > 1:
        has_header = bool(header)
    if has_header:
        columns = [c or ("第%d列" % (i + 1)) for i, c in enumerate(rows[0])]
        body = rows[1:]
    else:
        columns = ["第%d列" % (i + 1) for i in range(width)]
        body = rows
    key_col = 0
    for i, c in enumerate(columns):
        if re.search(r"编号|文件名|名称|照片|图片|name|file|no|code", c, re.I):
            key_col = i
            break
    keys = [r[key_col] for r in body]
    mode = "number" if keys and sum(1 for k in keys if re.fullmatch(r"[A-Za-z_\-]*\d+", k or "")) >= len(keys) * 0.8 and \
        sum(1 for k in keys if re.fullmatch(r"\d+", k or "")) >= len(keys) * 0.5 else \
        ("stem" if keys and sum(1 for k in keys if re.search(r"[A-Za-z]", k or "") and re.search(r"\d", k or "")) >= len(keys) * 0.5 else "contains")
    return {"columns": columns, "rows": body, "hasHeader": has_header, "keyCol": key_col, "mode": mode,
            "count": len(body)}
