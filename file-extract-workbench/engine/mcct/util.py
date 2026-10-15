# -*- coding: utf-8 -*-
"""通用工具：路径、系统识别、时间解析、错误类型。"""
import os
import platform
import re
import sys
import time
from datetime import datetime, timedelta

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"

ENGINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL_DIR = os.path.dirname(ENGINE_DIR)
APP_DIR = os.path.join(TOOL_DIR, "app")
PRESET_DIR = os.path.join(TOOL_DIR, "presets")
TEMPLATE_DIR = os.path.join(TOOL_DIR, "templates")
LOG_DIR = os.path.join(TOOL_DIR, "logs")
RUNS_DIR = os.path.join(LOG_DIR, "runs")
SETTINGS_FILE = os.path.join(TOOL_DIR, "settings.json")

HIDDEN_NAMES = {"thumbs.db", "desktop.ini", ".ds_store", "$recycle.bin", "system volume information",
                ".spotlight-v100", ".trashes", ".fseventsd", ".temporaryitems", "icon\r"}


class UserError(Exception):
    """可以直接展示给用户的错误。"""


class Cancelled(Exception):
    pass


# ------------------------------------------------------------------ 路径
def norm_path(p):
    p = (p or "").strip().strip('"').strip("'").strip()
    if not p:
        return ""
    p = os.path.expanduser(p)
    return os.path.normpath(os.path.abspath(p))


def is_inside(child, parent):
    c = os.path.normcase(os.path.normpath(child))
    p = os.path.normcase(os.path.normpath(parent))
    if c == p:
        return True
    return c.startswith(p.rstrip("\\/") + os.sep)


def long_path(p):
    """Windows 下超过 260 字符的路径加 \\\\?\\ 前缀。"""
    if IS_WIN and len(p) > 240 and not p.startswith("\\\\?\\"):
        p = os.path.abspath(p)
        if p.startswith("\\\\"):
            return "\\\\?\\UNC\\" + p[2:]
        return "\\\\?\\" + p
    return p


_RESERVED = {"con", "prn", "aux", "nul"} | {"com%d" % i for i in range(1, 10)} | {"lpt%d" % i for i in range(1, 10)}


def sanitize_segment(s):
    s = re.sub(r'[<>:"|?*\x00-\x1f/\\]', "_", str(s)).strip()
    s = s.rstrip(". ")
    if s.split(".")[0].lower() in _RESERVED:
        s = "_" + s
    return s[:200]


def is_hidden_entry(entry):
    name = entry.name
    if name.startswith(".") or name.lower() in HIDDEN_NAMES or name.startswith("~$"):
        return True
    if IS_WIN:
        try:
            attrs = entry.stat(follow_symlinks=False).st_file_attributes
            return bool(attrs & 0x2 or attrs & 0x4)
        except Exception:
            return False
    return False


def safe_print(*args):
    try:
        print(*args, flush=True)
    except Exception:
        try:
            print(*(str(a).encode("ascii", "replace").decode() for a in args), flush=True)
        except Exception:
            pass


# ------------------------------------------------------------------ 系统
def system_info():
    from . import VERSION
    info = {
        "engine": "Python",
        "engineVersion": platform.python_version(),
        "toolVersion": VERSION,
        "sep": os.sep,
        "home": os.path.expanduser("~"),
        "toolDir": TOOL_DIR,
        "logDir": LOG_DIR,
        "arch": platform.machine(),
        "bundled": os.path.normcase(sys.executable).startswith(os.path.normcase(os.path.join(TOOL_DIR, "runtime"))),
    }
    if IS_WIN:
        build = 0
        try:
            build = sys.getwindowsversion().build
        except Exception:
            pass
        if build >= 22000:
            name = "Windows 11"
        elif build >= 10240:
            name = "Windows 10"
        else:
            name = "Windows " + platform.release()
        info.update(os="windows", osName=name, osLabel="%s（内部版本 %s）" % (name, build), supportsCreated=True)
    elif IS_MAC:
        ver = platform.mac_ver()[0] or ""
        names = {"11": "Big Sur", "12": "Monterey", "13": "Ventura", "14": "Sonoma", "15": "Sequoia",
                 "26": "Tahoe", "10.15": "Catalina", "10.14": "Mojave", "10.13": "High Sierra"}
        parts = ver.split(".")
        key = ".".join(parts[:2]) if parts and parts[0] == "10" else (parts[0] if parts else "")
        info.update(os="macos", osName="macOS", osLabel=("macOS %s %s" % (ver, names.get(key, ""))).strip(),
                    supportsCreated=True)
    else:
        pretty = ""
        try:
            with open("/etc/os-release", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        pretty = line.split("=", 1)[1].strip().strip('"')
        except Exception:
            pass
        info.update(os="linux", osName="Linux", osLabel=pretty or ("Linux " + platform.release()), supportsCreated=False)
    return info


def birth_time(st):
    b = getattr(st, "st_birthtime", None)
    if b:
        return b
    if IS_WIN:
        return st.st_ctime
    return None


# ------------------------------------------------------------------ 时间
def fmt_ts(ts, with_seconds=True):
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def local_ts(dt):
    try:
        return time.mktime(dt.timetuple()) + dt.microsecond / 1e6
    except (OverflowError, ValueError):
        return None


def parse_tod(s, is_end):
    """'11:38' / '11:38:20' → 当天秒数。结束时间只写到分钟时包含整分钟。"""
    m = re.fullmatch(r"\s*(\d{1,2})[:：](\d{1,2})(?:[:：](\d{1,2}))?(?:\.\d+)?\s*", str(s or ""))
    if not m:
        raise UserError("时间格式不正确：“%s”（应为 时:分 或 时:分:秒）" % (s,))
    h, mi = int(m.group(1)), int(m.group(2))
    se = m.group(3)
    if h > 24 or mi > 59 or (se is not None and int(se) > 59) or (h == 24 and (mi or (se and int(se)))):
        raise UserError("时间超出范围：“%s”" % (s,))
    secs = h * 3600 + mi * 60 + (int(se) if se else 0)
    if is_end and secs < 86400:
        secs += 1 if se is not None else 60
    return min(secs, 86400)


def parse_dt(s, is_end):
    """'2026-10-14' / '2026-10-14 11:38' / '2026-10-14T11:38:20' → 时间戳；结束值按精度包含整段。"""
    s = str(s or "").strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[T ](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?(?:\.\d+)?)?", s)
    if not m:
        raise UserError("日期时间格式不正确：“%s”（应为 2026-10-14 11:38:00）" % (s,))
    y, mo, d, h, mi, se = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(se or 0))
    except ValueError:
        raise UserError("日期时间无效：“%s”" % (s,))
    if is_end:
        if h is None:
            dt += timedelta(days=1)
        elif se is None:
            dt += timedelta(seconds=60)
        else:
            dt += timedelta(seconds=1)
    return local_ts(dt)


def day_start(dt):
    return datetime(dt.year, dt.month, dt.day)


def preset_range(name, now=None):
    """返回 (开始, 结束) 时间戳，结束不包含。"""
    now = now or datetime.now()
    today = day_start(now)
    if name == "today":
        a, b = today, today + timedelta(days=1)
    elif name == "yesterday":
        a, b = today - timedelta(days=1), today
    elif name == "dayBefore":
        a, b = today - timedelta(days=2), today - timedelta(days=1)
    elif name in ("thisWeek", "lastWeek"):
        monday = today - timedelta(days=today.weekday())
        a, b = (monday, monday + timedelta(days=7)) if name == "thisWeek" else (monday - timedelta(days=7), monday)
    elif name in ("thisMonth", "lastMonth"):
        first = datetime(today.year, today.month, 1)
        nxt = datetime(first.year + (first.month == 12), first.month % 12 + 1, 1)
        if name == "thisMonth":
            a, b = first, nxt
        else:
            prev = datetime(first.year - (first.month == 1), (first.month - 2) % 12 + 1, 1)
            a, b = prev, first
    elif name in ("thisYear", "lastYear"):
        y = today.year if name == "thisYear" else today.year - 1
        a, b = datetime(y, 1, 1), datetime(y + 1, 1, 1)
    else:
        raise UserError("未知的日期范围：%s" % name)
    return local_ts(a), local_ts(b)


PRESET_LABELS = {"today": "今天", "yesterday": "昨天", "dayBefore": "前天", "thisWeek": "本周", "lastWeek": "上周",
                 "thisMonth": "本月", "lastMonth": "上个月", "thisYear": "今年", "lastYear": "去年"}
UNIT_SECONDS = {"minutes": 60, "hours": 3600, "days": 86400, "weeks": 604800, "months": 2592000, "years": 31536000}
UNIT_LABELS = {"minutes": "分钟", "hours": "小时", "days": "天", "weeks": "周", "months": "个月", "years": "年"}


def fmt_date_pattern(ts, pattern):
    """把时间按 yyyy / yy / MM / dd / HH / mm / ss 格式化。"""
    if ts is None:
        return ""
    try:
        d = datetime.fromtimestamp(ts)
    except (OverflowError, OSError, ValueError):
        return ""
    out = pattern or "yyyy-MM-dd"
    for tok, val in (("yyyy", "%04d" % d.year), ("yy", "%02d" % (d.year % 100)), ("MM", "%02d" % d.month),
                     ("dd", "%02d" % d.day), ("HH", "%02d" % d.hour), ("mm", "%02d" % d.minute),
                     ("ss", "%02d" % d.second)):
        out = out.replace(tok, val)
    return out


def human_size(n):
    n = float(n or 0)
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    if i == 0:
        return "%d B" % n
    return ("%.2f %s" if n < 10 else "%.1f %s") % (n, units[i])
