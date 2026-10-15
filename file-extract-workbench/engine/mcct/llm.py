# -*- coding: utf-8 -*-
"""可选：用大模型把一句话翻译成规则。只发送这句话、可用字段清单、文件夹名和少量样本文件名，不发送文件内容。
支持 Anthropic Claude API，以及兼容 OpenAI 格式的接口（DeepSeek、通义千问、Kimi、本地 Ollama 等）。"""
import json
import re
import ssl
import urllib.error
import urllib.request
from datetime import datetime

from .rules import FIELDS, OPS, OP_LABEL, validate_items, describe_plan
from .index import KINDS
from .util import UserError

DEFAULTS = {
    "anthropic": {"baseUrl": "https://api.anthropic.com", "model": "claude-haiku-5-5"},
    "openai": {"baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
}

VALUE_HELP = """各条件的 value 写法：
- contains / notContains / eq / ne / startsWith / endsWith / regex / notRegex / wildcard：字符串（regex 用 Python 正则）
- in / notIn：字符串数组；扩展名小写不带点，例如 ["jpg","cr3"]；文件类型用类型代码
- gt / gte / lt / lte / eq / ne（数字字段）：数字；between：[最小, 最大]
- 大小字段 size：数字 + 另加 "unit": "KB"|"MB"|"GB"
- timeOfDay：["11:38","11:41"]（每天的时段，结束分钟整分钟包含）
- onDate：["2026-10-14"]
- datePreset："today"|"yesterday"|"dayBefore"|"thisWeek"|"lastWeek"|"thisMonth"|"lastMonth"|"thisYear"|"lastYear"
- recent：{"amount": 3, "unit": "minutes"|"hours"|"days"|"weeks"|"months"|"years"}
- between（时间字段）：["2026-10-01 08:00", "2026-10-05"]，只写日期时结束日包含整天
- after / before：时间字符串
- empty / notEmpty：不需要 value"""

OUTPUT_HELP = """只输出一个 JSON 对象，不要任何解释文字，格式：
{
  "items": [条件或条件组],
  "limit": null 或 {"enabled": true, "per": "none"|"dir1"|"dir2"|"folder", "count": 10, "order": "newest"|"oldest"|"largest"|"smallest"|"nameAsc", "by": 时间字段},
  "dest": {"template": "目标路径模板，可省略", "action": "copy"|"move"|"link"|"zip"|"list"（可省略）, "root": "用户明确给出的目标路径，可省略"},
  "dedupe": true|false|null,
  "understood": [{"text": "原句中的片段", "meaning": "你的理解"}],
  "unknown": ["无法对应到任何字段的词"],
  "notes": ["需要提醒用户的地方"]
}
条件：{"field": 字段, "op": 运算, "value": 值}；条件组：{"group": true, "mode": "all"|"any"|"none", "items": [...]}。
多个条件默认全部满足。用户说“或者”时用 any 组；说“不要/除了/排除”时用否定运算或 none 组。
路径模板变量：{一级文件夹} {二级文件夹} {所在文件夹} {子目录} {相对目录} {来源} {文件名} {主文件名} {扩展名} {类型} {编号} {相机} {作者}
{拍摄日期:yyyy-MM-dd} {修改日期:yyyy-MM} {创建日期:yyyy-MM-dd} {运行日期:yyyyMMdd}。用 / 分隔多级。
“每个机位/每个文件夹分别存放”= template "{一级文件夹}/{子目录}"。
用户没提到的条件不要编造；只能用下面列出的字段。数据里不存在的信息（例如某个人名在样本中找不到对应字段）请放进 unknown 并在 notes 说明。"""


def build_system(context):
    lines = ["你负责把用户的中文需求翻译成文件筛选规则（JSON）。今天是 %s。" % datetime.now().strftime("%Y-%m-%d %A"), "", "可用字段："]
    for key, label, typ, _, _, hint in FIELDS:
        lines.append("- %s：%s（类型 %s，运算 %s）%s" % (key, label, typ, "/".join(OPS[typ]), ("。" + hint) if hint else ""))
    lines.append("")
    lines.append("文件类型代码：" + "，".join("%s=%s" % (k, l) for k, l, _ in KINDS))
    lines.append("运算含义：" + "，".join("%s=%s" % (k, v) for k, v in OP_LABEL.items()))
    lines.append("")
    lines.append(VALUE_HELP)
    lines.append("")
    lines.append(OUTPUT_HELP)
    return "\n".join(lines)


def build_user(text, context):
    ctx = {
        "一级文件夹（部分）": (context.get("dirs") or [])[:60],
        "常见扩展名": (context.get("exts") or [])[:30],
        "样本文件名": (context.get("samples") or [])[:40],
        "照片是否带拍摄时间": context.get("hasExif"),
    }
    return "需求：%s\n\n这批文件的情况（仅供参考）：\n%s" % (text, json.dumps(ctx, ensure_ascii=False))


def _post(url, headers, body, timeout=60):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=dict(headers, **{"Content-Type": "application/json"}))
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        detail = ""
        try:
            detail = ex.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa
            pass
        if ex.code in (401, 403):
            raise UserError("大模型接口拒绝访问（%d）：请检查 API Key 是否正确。%s" % (ex.code, detail))
        if ex.code == 404:
            raise UserError("大模型接口地址或模型名称不正确（404）。%s" % detail)
        if ex.code == 429:
            raise UserError("大模型接口调用太频繁或额度不足（429），请稍后再试。")
        raise UserError("大模型接口返回错误 %d：%s" % (ex.code, detail))
    except urllib.error.URLError as ex:
        reason = getattr(ex, "reason", ex)
        if isinstance(reason, ssl.SSLError):
            raise UserError("连接大模型接口时证书校验失败：%s。公司网络若有代理或防火墙，请联系网络管理员。" % reason)
        raise UserError("无法连接大模型接口（%s）。请确认这台电脑能上网，或改用本地解析。" % reason)
    except (TimeoutError, OSError) as ex:
        raise UserError("连接大模型接口超时或失败：%s" % ex)


def call(settings, system, user):
    provider = settings.get("provider") or "anthropic"
    key = (settings.get("apiKey") or "").strip()
    if not key and provider == "anthropic":
        raise UserError("还没有填写大模型 API Key，请在“设置”里填写")
    base = (settings.get("baseUrl") or DEFAULTS.get(provider, {}).get("baseUrl") or "").rstrip("/")
    model = settings.get("model") or DEFAULTS.get(provider, {}).get("model")
    if not base or not model:
        raise UserError("请在“设置”里填写接口地址和模型名称")
    if provider == "anthropic":
        url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
        res = _post(url, {"x-api-key": key, "anthropic-version": "2023-06-01"},
                    {"model": model, "max_tokens": 2000, "system": system,
                     "messages": [{"role": "user", "content": user}]})
        parts = [c.get("text", "") for c in res.get("content") or [] if c.get("type") == "text"]
        return "".join(parts)
    url = base + ("/chat/completions" if not base.endswith("/chat/completions") else "")
    headers = {"Authorization": "Bearer " + key} if key else {}
    res = _post(url, headers, {"model": model, "temperature": 0,
                               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
    try:
        return res["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise UserError("大模型接口返回的格式无法识别")


def extract_json(text):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < a:
        raise UserError("大模型没有返回有效的规则，请换个说法再试")
    try:
        return json.loads(text[a:b + 1])
    except ValueError:
        raise UserError("大模型返回的规则格式有误，请再试一次")


def _assign_ids(items, prefix="l"):
    n = [0]

    def walk(lst):
        for it in lst:
            n[0] += 1
            it["id"] = "%s%d" % (prefix, n[0])
            if it.get("group") or "items" in it:
                it["group"] = True
                walk(it.get("items") or [])
    walk(items)
    return items


def translate(text, context, settings):
    if not (text or "").strip():
        raise UserError("请先输入一句话")
    raw = call(settings, build_system(context), build_user(text, context))
    data = extract_json(raw)
    items = _assign_ids([it for it in (data.get("items") or []) if isinstance(it, dict)])
    ok, errs = validate_items(items)
    limit = data.get("limit") if isinstance(data.get("limit"), dict) else None
    if limit:
        limit = {"enabled": True, "per": limit.get("per") if limit.get("per") in ("none", "dir1", "dir2", "folder", "source") else "none",
                 "count": int(limit.get("count") or 10),
                 "order": limit.get("order") if limit.get("order") in ("newest", "oldest", "largest", "smallest", "nameAsc", "nameDesc") else "newest",
                 "by": limit.get("by") or "mtime"}
    dest = data.get("dest") if isinstance(data.get("dest"), dict) else {}
    dest = {k: v for k, v in dest.items() if k in ("template", "action", "root") and isinstance(v, str)}
    if dest.get("action") not in (None, "copy", "move", "link", "zip", "list"):
        dest.pop("action", None)
    rule = {"mode": "all", "items": ok}
    return {
        "items": ok,
        "limit": limit,
        "dest": dest,
        "dedupe": data.get("dedupe") if isinstance(data.get("dedupe"), bool) else None,
        "understood": [u for u in (data.get("understood") or []) if isinstance(u, dict)][:30],
        "unknown": [str(u) for u in (data.get("unknown") or [])][:30],
        "notes": [str(n) for n in (data.get("notes") or [])][:10] + ["已忽略一条无效条件：" + e for e in errs],
        "summary": describe_plan(rule, limit),
        "engine": "llm",
    }


def test(settings):
    raw = call(settings, "只回复 JSON：{\"ok\": true}", "测试连接")
    return {"ok": True, "reply": raw[:200]}
