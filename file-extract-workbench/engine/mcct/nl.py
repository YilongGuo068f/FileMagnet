# -*- coding: utf-8 -*-
"""本地中文自然语言解析：把一句话翻译成规则积木（离线、即时，不调用任何外部服务）。

思路：按“目标路径 → 动作 → 数量 → 分组 → 时间 → 名单/编号 → 文件名 → 文件夹 → 大小 → 相机 → 人员 → 内容 → 星级 → 类型”
的顺序逐一识别，识别过的文字会被遮住，避免重复解释；最后剩下的词作为“没听懂的部分”返回。"""
import re
from datetime import datetime

from .index import KINDS, KIND_LABEL
from .rules import validate_items, describe_plan

CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CLASSIFIERS = "分钟|分|小时|个小时|钟头|天|日|号|周|星期|礼拜|个月|月|年|张|个|份|条|段|部|只|星|点|秒|层"

BRANDS = [("佳能", "Canon"), ("尼康", "Nikon"), ("索尼", "Sony"), ("富士", "FUJIFILM"), ("大疆", "DJI"),
          ("松下", "Panasonic"), ("徕卡", "Leica"), ("莱卡", "Leica"), ("哈苏", "Hasselblad"), ("奥林巴斯", "Olympus"),
          ("理光", "Ricoh"), ("宾得", "Pentax"), ("适马", "Sigma"), ("苹果", "Apple"), ("华为", "HUAWEI"),
          ("小米", "Xiaomi"), ("Canon", "Canon"), ("Nikon", "Nikon"), ("Sony", "Sony"), ("Fujifilm", "FUJIFILM"),
          ("Fuji", "FUJIFILM"), ("DJI", "DJI"), ("Panasonic", "Panasonic"), ("Leica", "Leica"), ("iPhone", "iPhone"),
          ("GoPro", "GoPro"), ("Insta360", "Insta360"), ("OPPO", "OPPO"), ("vivo", "vivo"), ("Hasselblad", "Hasselblad")]

KIND_WORDS = [
    (r"RAW|raw|Raw|生图|原始文件|原始格式|无损原图", ["raw"]),
    (r"原片|原图", ["photo", "raw"]),
    (r"照片|图片|相片|图像|相机图片", ["photo"]),
    (r"视频|录像|影片|短片|片段|vlog|Vlog", ["video"]),
    (r"音频|录音|音乐|声音|配乐", ["audio"]),
    (r"Word文档|Word|word|文档|文稿|稿子", ["word"]),
    (r"Excel表格|Excel|excel|表格|报表|电子表格", ["sheet"]),
    (r"PPT|ppt|幻灯片|演示文稿|课件", ["slides"]),
    (r"PDF|pdf", ["pdf"]),
    (r"压缩包|压缩文件", ["archive"]),
    (r"文本文件|文本", ["text"]),
    (r"设计稿|设计文件|源文件", ["design"]),
]
ALL_EXTS = sorted(set(e for _, _, exts in KINDS for e in exts) | {"heic", "mp3", "wav"}, key=len, reverse=True)
EXT_RE = re.compile(r"(?<![A-Za-z0-9])\.?(%s)(?![A-Za-z0-9])" % "|".join(re.escape(e) for e in ALL_EXTS if len(e) >= 2), re.I)

NEG = r"(?:不要|不含|不包括|不包含|除了|除去|排除|去掉|去除|别要|别|非|不是)"
NAME_STOP = {"我", "你", "他", "她", "我们", "你们", "他们", "自己", "大家", "这个", "那个", "这些", "那些", "所有", "全部",
             "今天", "昨天", "前天", "最近", "刚才", "刚刚", "之前", "以前", "之后", "以后", "本周", "上周", "本月", "上月",
             "相机", "手机", "机位", "无人机", "文件", "照片", "视频", "上午", "下午", "晚上", "早上", "中午", "凌晨", "客户",
             "现场", "他们拍", "别人"}
TITLE_SUFFIX = re.compile(r"(经理|老师|总监|主管|主任|同学|先生|女士|小姐|师傅|总|工|哥|姐)$")
STOPWORDS = ["帮我", "请", "麻烦", "把", "将", "给我", "我要", "我想要", "我想", "想要", "需要", "要", "只要", "只", "仅",
             "找出", "找到", "找", "挑出", "挑", "选出", "选", "筛出", "筛选", "过滤", "锁定", "拷贝", "复制", "拷", "出来",
             "里面", "里头", "里", "中", "内", "下", "的", "所有", "全部", "全都", "都", "和", "与", "及", "以及", "或",
             "或者", "并且", "而且", "且", "还有", "同时", "另外", "文件", "那些", "这些", "一下", "吧", "啊", "呢", "了",
             "之间", "之内", "以内", "在", "是", "有", "个", "些", "张", "份", "拍", "摄", "到", "至", "从", "然后",
             "就", "去", "一起", "其他机位", "其余机位", "其他", "其余", "剩下的", "别的", "机位", "全", "东西", "素材", "资料", "相关", "对应", "指定", "目录", "一个", "目标"]


def cn2num(s):
    if s.isdigit():
        return int(s)
    total, cur = 0, 0
    for ch in s:
        if ch in CN_DIGITS:
            cur = CN_DIGITS[ch]
        elif ch == "十":
            total += (cur or 1) * 10
            cur = 0
        elif ch == "百":
            total += (cur or 1) * 100
            cur = 0
    return total + cur


def normalize(text):
    out = []
    for ch in text:
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E:
            ch = chr(o - 0xFEE0)
        elif ch == "\u3000":
            ch = " "
        out.append(ch)
    t = "".join(out)
    t = t.replace("：", ":").replace("～", "~")
    t = re.sub(r"半个?小时", "30分钟", t)
    t = re.sub(r"一刻钟", "15分钟", t)
    t = re.sub(r"半天", "12小时", t)
    t = re.sub(r"([零〇一二两三四五六七八九十百]+)(?=\s*(?:%s))" % CLASSIFIERS, lambda m: str(cn2num(m.group(1))), t)
    return t


class Parser(object):
    def __init__(self, text, context):
        self.raw = text
        self.t = normalize(text)
        self.mask = list(self.t)
        self.context = context or {}
        self.items = []
        self.understood = []
        self.notes = []
        self.limit = None
        self.dest = {}
        self.dedupe = None
        self.quotes = {}
        self.time_field = None
        self.n = 0

    # ---------------------------------------------------------- 工具
    @property
    def m(self):
        return "".join(self.mask)

    def consume(self, a, b, meaning=None):
        text = self.t[a:b]
        for i in range(a, b):
            self.mask[i] = "\x00"
        if meaning:
            self.understood.append({"text": self._restore(text).strip(), "meaning": meaning})

    def _restore(self, s):
        return re.sub(r"\x01(\d+)\x02", lambda m: "“%s”" % self.quotes.get(int(m.group(1)), ""), s)

    def finditer(self, pattern, flags=0):
        rx = re.compile(pattern, flags)
        found = []
        pos = 0
        while True:
            m = rx.search(self.m, pos)
            if not m:
                break
            if m.end() == m.start():
                pos = m.end() + 1
                continue
            found.append(m)
            pos = m.end()
        return found

    def add(self, field, op, value, **extra):
        self.n += 1
        c = {"id": "n%d" % self.n, "field": field, "op": op, "value": value}
        c.update(extra)
        self.items.append(c)
        return c

    def qval(self, s):
        """把占位符还原为引号里的原文。"""
        m = re.fullmatch(r"\x01(\d+)\x02", s.strip())
        if m:
            return self.quotes.get(int(m.group(1)), "")
        return s.strip()

    # ---------------------------------------------------------- 步骤
    def protect_quotes(self):
        def rep(m):
            k = len(self.quotes) + 1
            self.quotes[k] = m.group(1)
            return "\x01%d\x02" % k
        self.t = re.sub(r"[“\"「『‘']([^”\"」』’']+)[”\"」』’']", rep, self.t)
        self.mask = list(self.t)

    VAL = r"(\x01\d+\x02|[A-Za-z0-9_\-.#@]+|[\u4e00-\u9fff]{1,12}?)"
    END = r"(?=的|，|,|。|；|;|并且|而且|且|和|、|或|\s|$|\x00|里|中|下)"

    def step_dest(self):
        for m in self.finditer(r"(拷贝|拷|复制|放|存|移动|移|剪切|挪|导出|输出|保存|打包|压缩)\s*(?:到|至|进|去|在)\s*"
                               r"([A-Za-z]:[\\/][^\s，。,；;\x00]*|\\\\[^\s，。,；;\x00]+|/[^\s，。,；;\x00]+|~[/\\]?[^\s，。,；;\x00]*)"):
            self.dest["root"] = m.group(2).rstrip("的")
            verb = m.group(1)
            if verb in ("移动", "移", "剪切", "挪"):
                self.dest["action"] = "move"
            elif verb in ("打包", "压缩"):
                self.dest["action"] = "zip"
            self.consume(m.start(), m.end(), "目标文件夹：%s" % self.dest["root"])

    def step_action(self):
        for m in self.finditer(r"移动|剪切|挪走|挪到"):
            self.dest["action"] = "move"
            self.consume(m.start(), m.end(), "动作：移动")
        for m in self.finditer(r"打包|压缩成\s*zip|压缩成压缩包|压缩"):
            self.dest["action"] = "zip"
            self.consume(m.start(), m.end(), "动作：打包成 zip")
        for m in self.finditer(r"(?<![按用照依据])(?:列|导出|生成|做)(?:一个|一份)?(?:文件)?(?:清单|列表)|只(?:要)?清单"):
            self.dest["action"] = "list"
            self.consume(m.start(), m.end(), "动作：只导出清单")
        for m in self.finditer(r"硬链接|建(?:立)?链接"):
            self.dest["action"] = "link"
            self.consume(m.start(), m.end(), "动作：建立链接")
        for m in self.finditer(r"去重|去掉重复(?:的)?|不要重复(?:的)?|重复的(?:不要|跳过|去掉)|跳过重复(?:的)?"):
            self.dedupe = True
            self.consume(m.start(), m.end(), "跳过内容重复的文件")

    def step_limit(self):
        orders = {"最新": "newest", "最近": "newest", "最后": "newest", "最晚": "newest", "最早": "oldest", "最先": "oldest",
                  "最大": "largest", "最小": "smallest", "前": "nameAsc"}
        pat = (r"(每个?|各个?|每一个)?\s*(机位|文件夹|子文件夹|目录|组|相机|人)?\s*(?:里|中)?\s*(?:的)?\s*"
               r"(最新|最近|最后|最晚|最早|最先|最大|最小|前)\s*(?:拍)?(?:的)?\s*(\d+)\s*(张|个|份|条|段|部|只|项)")
        for m in self.finditer(pat):
            per = "dir1" if (m.group(1) or m.group(2)) else "none"
            self.limit = {"enabled": True, "per": per, "count": int(m.group(4)), "order": orders[m.group(3)], "by": None}
            self.consume(m.start(), m.end(), "%s只取%s %s 个" % ("每个一级文件夹" if per == "dir1" else "总共",
                                                            m.group(3) + ("的" if m.group(3) != "前" else ""), m.group(4)))
            if per == "dir1":
                self.dest.setdefault("template", "{一级文件夹}/{子目录}")
            break

    def step_group(self):
        for m in self.finditer(r"(?:每个|每一个|各个|各|分别按?|按|分)\s*(机位|子文件夹|文件夹|目录)\s*(?:分开|分别|分类|存放|存|放|建文件夹|对应)?"
                               r"|(?:各|每个)(?:机位|文件夹)(?:的|分别)?"):
            self.dest["template"] = "{一级文件夹}/{子目录}"
            self.consume(m.start(), m.end(), "按一级文件夹（如机位）分别存放")
        table = [
            (r"按(?:拍摄)?(?:年月|月份|月)(?:分|分开|整理|归档|放|存|建文件夹)?", "{拍摄日期:yyyy-MM}", "按拍摄月份分文件夹"),
            (r"按(?:拍摄)?(?:日期|天|日)(?:分|分开|整理|归档|放|存|建文件夹)?", "{拍摄日期:yyyy-MM-dd}", "按拍摄日期分文件夹"),
            (r"按(?:拍摄)?年(?:份)?(?:分|分开|整理|归档|放|存)?", "{拍摄日期:yyyy}", "按年份分文件夹"),
            (r"按(?:相机|机型|型号|机身)(?:分|分开|整理|归档|放|存)?", "{相机}", "按相机型号分文件夹"),
            (r"按(?:类型|格式|种类)(?:分|分开|分类|整理|归档|放|存)?", "{类型}", "按文件类型分文件夹"),
            (r"按扩展名(?:分|分开|分类|整理|归档|放|存)?", "{扩展名}", "按扩展名分文件夹"),
            (r"按(?:作者|创建人|人)(?:分|分开|整理|归档|放|存)?", "{作者}", "按作者分文件夹"),
            (r"保(?:持|留)(?:原来的|原有的|原有|原)?(?:目录|文件夹)?结构|原样(?:保留|拷贝)?|保持原样", "{相对目录}", "保留原有文件夹结构"),
            (r"平铺|不(?:要|用|需要)?(?:建|分)?子?文件夹|放(?:在)?一起|放(?:到|进)?同一个文件夹|全部放一起", "", "全部放进同一个文件夹"),
        ]
        for pat, tpl, meaning in table:
            for m in self.finditer(pat):
                self.dest["template"] = tpl
                self.consume(m.start(), m.end(), meaning)

    def detect_time_field(self):
        t = self.m
        if re.search(r"拍(?:摄|的|下|了|照)|摄于|拍照", t):
            self.time_field = "exif.time"
        elif re.search(r"修改|改(?:过|动|的)|编辑|更新(?:过|的)", t):
            self.time_field = "mtime"
        elif re.search(r"创建|新建|建(?:立)?的|生成的|产生的|新增", t):
            self.time_field = "ctime"
        elif re.search(r"下载|导入|(?:拷|复制|传)(?:进来|进|入|过来|到电脑)", t):
            self.time_field = "ctime"

    def consume_verbs(self):
        for m in self.finditer(r"拍摄的|拍摄|拍的|拍下的|拍了|修改过的|修改的|修改|改过的|改动过的|编辑过的|创建的|新建的|新增的|下载的|导入的|拷进来的"):
            self.consume(m.start(), m.end())

    def guess_name(self):
        """句末“……的合同”这类剩下的名词，推测为文件名包含。"""
        m = re.search(r"[的\x00][\s\x00]*([\u4e00-\u9fff]{2,6}|[A-Za-z0-9_\-]{2,20})\s*[。.!！]?\s*$", self.m)
        if not m:
            return
        v = m.group(1)
        if v in NAME_STOP or v in STOPWORDS or re.fullmatch(r"(?:文件|东西|素材|资料|内容|那些|这些|全部|所有)", v):
            return
        self.add("name", "contains", v)
        self.consume(m.start(1), m.end(1), "文件名包含“%s”（推测，可删除）" % v)

    TIME = (r"(上午|早上|早晨|清晨|中午|下午|傍晚|晚上|夜里|夜间|凌晨)?\s*(\d{1,2})\s*"
            r"(?:[:]\s*(\d{1,2})(?:\s*[:]\s*(\d{1,2}))?|点(?:\s*(半|整|(\d{1,2})\s*分?)(?:\s*(\d{1,2})\s*秒)?)?)")

    def _tod(self, m, base=0, inherit=None):
        """把 TIME 正则匹配结果变成 (时, 分, 秒, 是否写了秒, 时段词)。"""
        g = lambda i: m.group(base + i)
        period = g(1) or inherit
        h = int(g(2))
        if g(3) is not None:
            mi = int(g(3))
            sec = g(4)
        else:
            if g(5) == "半":
                mi = 30
            elif g(6):
                mi = int(g(6))
            else:
                mi = 0
            sec = g(7)
        if period in ("下午", "傍晚", "晚上", "夜里", "夜间") and h < 12:
            h += 12
        if period == "中午" and h < 6:
            h += 12
        if h > 24 or mi > 59:
            return None
        return (h, mi, int(sec) if sec else 0, bool(sec), period)

    @staticmethod
    def _fmt(t):
        return "%02d:%02d:%02d" % (t[0], t[1], t[2]) if t[3] else "%02d:%02d" % (t[0], t[1])

    def step_dates(self):
        f = lambda: self.time_field or "__T__"
        presets = [("今天|今日", "today"), ("昨天|昨日", "yesterday"), ("前天", "dayBefore"),
                   ("本周|这周|这个星期|本星期|这礼拜|这个礼拜", "thisWeek"), ("上周|上个星期|上星期|上礼拜|上个礼拜", "lastWeek"),
                   ("本月|这个月", "thisMonth"), ("上个月|上月", "lastMonth"), ("今年|本年", "thisYear"), ("去年", "lastYear")]
        for pat, key in presets:
            for m in self.finditer(pat):
                self.add(f(), "datePreset", key)
                self.consume(m.start(), m.end(), "日期：%s" % m.group(0))
        units = {"分钟": "minutes", "分": "minutes", "小时": "hours", "个小时": "hours", "钟头": "hours", "天": "days",
                 "日": "days", "周": "weeks", "星期": "weeks", "礼拜": "weeks", "个月": "months", "月": "months", "年": "years"}
        for m in self.finditer(r"(?:最近|近|过去|这)\s*(\d+(?:\.\d+)?)\s*(个小时|个月|分钟|小时|钟头|天|日|周|星期|礼拜|月|年|分)(?:之内|以内|内|里)?"
                               r"|(\d+(?:\.\d+)?)\s*(个小时|个月|分钟|小时|钟头|天|周|星期|月|年)(?:之内|以内|内)"):
            amt = m.group(1) or m.group(3)
            unit = units[m.group(2) or m.group(4)]
            self.add(f(), "recent", {"amount": float(amt) if "." in amt else int(amt), "unit": unit})
            self.consume(m.start(), m.end(), "时间：%s" % m.group(0))
        now = datetime.now()

        def mkdate(y, mo, d):
            y = int(y) if y else now.year
            try:
                return datetime(y, int(mo), int(d)).strftime("%Y-%m-%d")
            except ValueError:
                return None
        D = (r"(?:(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*[日号]?"
             r"|(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?"
             r"|(?<![\d:])(\d{1,2})/(\d{1,2})(?![\d:/]))")

        def dval(m, base):
            g = lambda i: m.group(base + i)
            if g(1):
                return mkdate(g(1), g(2), g(3))
            if g(4):
                return mkdate(None, g(4), g(5))
            return mkdate(None, g(6), g(7))
        for m in self.finditer(D + r"\s*(?:到|至|~|-|—|–)\s*" + D + r"(?:\s*(?:之间|期间))?"):
            a, b = dval(m, 0), dval(m, 7)
            if a and b:
                self.add(f(), "between", [a, b])
                self.consume(m.start(), m.end(), "日期：%s 到 %s" % (a, b))
        for m in self.finditer(D + r"\s*(之后|以后|后|起|开始|以来|之前|以前|前)?"):
            d = dval(m, 0)
            if not d:
                continue
            rel = m.group(8)
            if rel in ("之后", "以后", "后", "起", "开始", "以来"):
                self.add(f(), "after", d)
                self.consume(m.start(), m.end(), "日期：%s 之后" % d)
            elif rel in ("之前", "以前", "前"):
                self.add(f(), "before", d)
                self.consume(m.start(), m.end(), "日期：%s 之前" % d)
            else:
                self.add(f(), "onDate", [d])
                self.consume(m.start(), m.end(), "日期：%s" % d)

    def step_times(self):
        f = lambda: self.time_field or "__T__"
        T = self.TIME
        rng = T + r"\s*(?:到|至|~|-|—|–|和)\s*(?:" + T + r"|(\d{1,2})\s*(分|秒)?)(?:\s*(?:之间|期间|这段时间|这段|那段))?"
        for m in self.finditer(rng):
            a = self._tod(m, 0)
            if a is None:
                continue
            if m.group(9) is not None:
                b = self._tod(m, 7, inherit=a[4])
            else:
                x = int(m.group(15))
                unit = m.group(16)
                if unit == "秒":
                    b = (a[0], a[1], x, True, a[4])
                elif unit == "分" or (a[1] or m.group(3) or m.group(6)) and x <= 59:
                    b = (a[0], x, 0, False, a[4])
                    if x < a[1]:
                        b = (a[0] + 1, x, 0, False, a[4])
                else:
                    b = self._tod(re.match(T, "%d点" % x), 0, inherit=a[4]) if x <= 24 else None
            if b is None:
                continue
            if b[0] < a[0] and not m.group(8) and a[4] in (None,) and b[0] < 12 and a[0] >= 12:
                b = (b[0] + 12,) + b[1:]
            self.add(f(), "timeOfDay", [self._fmt(a), self._fmt(b)])
            self.consume(m.start(), m.end(), "时段：每天 %s–%s" % (self._fmt(a), self._fmt(b)))
        for m in self.finditer(T + r"\s*(之后|以后|后|开始|起|之前|以前|前)"):
            a = self._tod(m, 0)
            if a is None:
                continue
            if m.group(8) in ("之前", "以前", "前"):
                self.add(f(), "timeOfDay", ["00:00", self._fmt(a)])
                self.consume(m.start(), m.end(), "时段：每天 %s 之前" % self._fmt(a))
            else:
                self.add(f(), "timeOfDay", [self._fmt(a), "23:59:59"])
                self.consume(m.start(), m.end(), "时段：每天 %s 之后" % self._fmt(a))
        for m in self.finditer(r"(?<![\d:])(\d{1,2}):(\d{2})(?::(\d{2}))?(?![\d:])"):
            a = (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0), bool(m.group(3)), None)
            if a[0] > 23 or a[1] > 59:
                continue
            self.add(f(), "timeOfDay", [self._fmt(a), self._fmt(a)])
            self.consume(m.start(), m.end(), "时刻：%s（整分钟）" % self._fmt(a))
        periods = [("凌晨", "00:00", "06:00"), ("早上|早晨|清晨", "06:00", "09:00"), ("上午", "06:00", "11:59"),
                   ("中午", "11:00", "13:59"), ("下午", "12:00", "17:59"), ("傍晚", "17:00", "19:59"),
                   ("晚上|夜里|夜间", "18:00", "23:59")]
        for pat, a, b in periods:
            for m in self.finditer("(?:%s)(?!\\s*\\d)" % pat):
                self.add(f(), "timeOfDay", [a, b])
                self.consume(m.start(), m.end(), "时段：%s（%s–%s）" % (m.group(0), a, b))

    def step_numbers(self):
        for m in self.finditer(r"(?:编号|序号|号码|文件号|第)?\s*([A-Za-z][A-Za-z_\-]*?)?(\d{2,})\s*号?\s*(?:到|至|~|-|—|–)\s*"
                               r"(?:\1)?(\d{2,})\s*(?:号|之间)?"):
            pre = self.t[max(0, m.start() - 4):m.start()]
            whole = m.group(0)
            has_kw = re.match(r"\s*(编号|序号|号码|文件号|第)", whole) or m.group(1) or re.search(r"编号|序号", pre)
            if not has_kw:
                continue
            a, b = int(m.group(2)), int(m.group(3))
            self.add("num", "between", [min(a, b), max(a, b)])
            if m.group(1):
                self.add("stem", "startsWith", m.group(1))
            self.consume(m.start(), m.end(), "编号 %d–%d%s" % (min(a, b), max(a, b), ("，开头是 %s" % m.group(1)) if m.group(1) else ""))
        for m in self.finditer(r"(?:编号|序号)\s*(?:是|为)?\s*(\d+)(?!\d)"):
            self.add("num", "eq", int(m.group(1)))
            self.consume(m.start(), m.end(), "编号 %s" % m.group(1))

    def step_names(self):
        V, E = self.VAL, self.END
        rules = [
            (r"(?:文件名|名字|名称|命名|名)\s*(?:里|中)?\s*(?:不包含|不含|没有|不带|不能有|不要有)\s*" + V + E, "name", "notContains", "文件名不包含"),
            (r"(?:文件名|名字|名称|命名|名)\s*(?:里|中)?\s*(?:包含|含有|含|带有|带|有|里有|中有|里带)\s*" + V + E, "name", "contains", "文件名包含"),
            (r"(?:文件名|名字|名称|主文件名)?\s*以\s*" + V + r"\s*(?:开头|打头|起头)", "stem", "startsWith", "文件名开头是"),
            (r"(?:文件名|名字|名称|主文件名)?\s*以\s*" + V + r"\s*(?:结尾|结束|收尾)", "stem", "endsWith", "文件名结尾是"),
            (r"(?:文件名|名字|名称)\s*(?:开头|前缀)\s*(?:是|为)\s*" + V + E, "stem", "startsWith", "文件名开头是"),
            (r"(?:文件名|名字|名称)\s*(?:结尾|后缀)\s*(?:是|为)\s*" + V + E, "stem", "endsWith", "文件名结尾是"),
            (r"(\x01\d+\x02|[A-Za-z0-9_\-]+)\s*(?:开头|打头)", "stem", "startsWith", "文件名开头是"),
            (r"(\x01\d+\x02|[A-Za-z0-9_\-]+)\s*(?:结尾|收尾)", "stem", "endsWith", "文件名结尾是"),
            (r"(?:文件名|名字|名称)\s*(?:是|为|叫|等于)\s*" + V + E, "name", "eq", "文件名是"),
            (NEG + r"\s*(?:文件名|名字)?\s*(?:带|含|有|包含)\s*" + V + r"\s*(?:的|字样的)?", "name", "notContains", "文件名不包含"),
            (r"(?:带|含|有)\s*(\x01\d+\x02)\s*(?:的|字样的)?", "name", "contains", "文件名包含"),
        ]
        for pat, field, op, meaning in rules:
            for m in self.finditer(pat):
                v = self.qval(m.group(1))
                if not v or v in NAME_STOP:
                    continue
                if field == "stem" and op in ("startsWith", "endsWith") and "." in v and field != "name":
                    field2 = "name"
                else:
                    field2 = field
                self.add(field2, op, v)
                self.consume(m.start(), m.end(), "%s“%s”" % (meaning, v))
        for m in self.finditer(r"(?:文件名|名字)?\s*(?:符合|满足|匹配)\s*(?:正则|正则表达式)\s*(\x01\d+\x02|\S+)"):
            self.add("name", "regex", self.qval(m.group(1)))
            self.consume(m.start(), m.end(), "文件名符合正则")
        for m in self.finditer(r"(?:文件名|名字)?\s*(?:符合|满足|像)\s*(\x01\d+\x02|[\w\-.]*[*?][\w\-.*?]*)"):
            self.add("name", "wildcard", self.qval(m.group(1)))
            self.consume(m.start(), m.end(), "文件名符合通配符")

    def step_dirnames(self):
        dirs = [d for d in (self.context.get("dirs") or []) if d and len(d) >= 2]
        dirs.sort(key=len, reverse=True)
        inc, exc = [], []
        for d in dirs:
            for m in self.finditer(re.escape(d)):
                before = self.m[max(0, m.start() - 6):m.start()]
                (exc if re.search(NEG + r"\s*$", before) else inc).append(d)
                s = m.start()
                nm = re.search(NEG + r"\s*$", before)
                if nm:
                    s = m.start() - (len(before) - nm.start())
                self.consume(s, m.end(), ("排除文件夹“%s”" if nm else "文件夹“%s”") % d)
        if inc:
            self.add("dir1", "in", sorted(set(inc)))
        if exc:
            self.add("dir1", "notIn", sorted(set(exc)))

    def step_folders(self):
        V, E = self.VAL, self.END
        for m in self.finditer(r"(?:文件夹|目录|机位)(?:名|名字|名称)?\s*(?:里|中)?\s*(?:包含|含有|含|带有|带|有|叫|是|为)\s*" + V + E):
            v = self.qval(m.group(1))
            if v and v not in NAME_STOP:
                self.add("folders", "contains", v)
                self.consume(m.start(), m.end(), "路径中有文件夹名包含“%s”" % v)
        for m in self.finditer(r"(\x01\d+\x02|[A-Za-z0-9_\-]{2,}|[\u4e00-\u9fff]{2,8}?)\s*(?:这个|那个)?\s*(?:文件夹|目录|子文件夹)\s*(?:里|下|中|内|里面|下面)?"):
            v = self.qval(m.group(1))
            if not v or v in NAME_STOP or v in ("每个", "各个", "同一个", "子", "目标", "来源", "所有", "每一个", "这个", "那个", "一级", "二级"):
                continue
            self.add("folders", "contains", v)
            self.consume(m.start(), m.end(), "在名字包含“%s”的文件夹里" % v)

    def step_size(self):
        unit_map = {"K": "KB", "KB": "KB", "M": "MB", "MB": "MB", "兆": "MB", "G": "GB", "GB": "GB", "B": "B", "字节": "B", "T": "GB"}
        U = r"(KB|K|MB|M|兆|GB|G|字节|B)"
        for m in self.finditer(r"(\d+(?:\.\d+)?)\s*(?:到|至|-|~)\s*(\d+(?:\.\d+)?)\s*" + U + r"(?:之间)?", re.I):
            u = unit_map[m.group(3).upper() if m.group(3) not in ("兆", "字节") else m.group(3)]
            self.add("size", "between", [float(m.group(1)), float(m.group(2))], unit=u)
            self.consume(m.start(), m.end(), "大小 %s–%s %s" % (m.group(1), m.group(2), u))
        ops = [(r"(?:大于|超过|多于|大过|高于|>)\s*", "gt"), (r"(?:不小于|至少|>=|≥|起码)\s*", "gte"),
               (r"(?:小于|不到|少于|低于|<)\s*", "lt"), (r"(?:不超过|不大于|至多|最多|<=|≤)\s*", "lte")]
        for p, op in ops:
            for m in self.finditer(r"(?:大小|体积|文件)?\s*" + p + r"(\d+(?:\.\d+)?)\s*" + U, re.I):
                u = unit_map[m.group(2).upper() if m.group(2) not in ("兆", "字节") else m.group(2)]
                self.add("size", op, float(m.group(1)), unit=u)
                self.consume(m.start(), m.end(), "大小%s %s %s" % ({"gt": "大于", "gte": "至少", "lt": "小于", "lte": "至多"}[op], m.group(1), u))
        for m in self.finditer(r"(\d+(?:\.\d+)?)\s*" + U + r"\s*(以上|之上|往上|以下|以内|之内|往下)", re.I):
            u = unit_map[m.group(2).upper() if m.group(2) not in ("兆", "字节") else m.group(2)]
            op = "gte" if m.group(3) in ("以上", "之上", "往上") else "lte"
            self.add("size", op, float(m.group(1)), unit=u)
            self.consume(m.start(), m.end(), "大小 %s %s %s" % (m.group(1), u, m.group(3)))

    def step_camera(self):
        names = "|".join(re.escape(a) for a, _ in sorted(BRANDS, key=lambda x: -len(x[0])))
        bmap = dict((a.lower(), b) for a, b in BRANDS)
        for m in self.finditer(r"(?:用|由|拿)?\s*(" + names + r")\s*([A-Za-z]{0,4}\d{1,4}[A-Za-z0-9]{0,4}(?:\s?(?:II|III|IV|Mark\s?\w+))?)?\s*(?:相机|机身|手机|无人机)?\s*(?:拍(?:摄)?(?:的)?)?", re.I):
            brand = bmap.get(m.group(1).lower(), m.group(1))
            self.add("camera", "contains", brand if brand != "iPhone" else "iPhone")
            if m.group(2):
                self.add("camera", "contains", m.group(2).strip())
            self.consume(m.start(), m.end(), "相机：%s%s" % (brand, (" " + m.group(2)) if m.group(2) else ""))
        for m in self.finditer(r"(?:用|由|拿)\s*([A-Za-z]{0,4}\d{1,4}[A-Za-z0-9]{0,4})\s*(?:相机|机身)?\s*拍(?:摄)?(?:的)?"):
            self.add("camera", "contains", m.group(1))
            self.consume(m.start(), m.end(), "相机型号包含 %s" % m.group(1))
        for m in self.finditer(r"(?:相机|机型|型号|机身)\s*(?:是|为|:)\s*(\x01\d+\x02|[A-Za-z0-9\- ]{2,24})"):
            v = self.qval(m.group(1)).strip()
            self.add("camera", "contains", v)
            self.consume(m.start(), m.end(), "相机型号包含 %s" % v)
        for m in self.finditer(r"(?:序列号|机身号|机身编号)\s*(?:是|为|:)?\s*([A-Za-z0-9]+)"):
            self.add("exif.serial", "contains", m.group(1))
            self.consume(m.start(), m.end(), "机身序列号包含 %s" % m.group(1))

    def _person(self, raw):
        v = re.sub(r"的+$", "", self.qval(raw) or "")
        if not v:
            return None
        if v[-1] in "修创编上下新拷导整传发写改建做拍":
            return None
        v2 = TITLE_SUFFIX.sub("", v) if len(v) > 1 else v
        v = v2 or v
        if v in NAME_STOP or len(v) > 12:
            return None
        return v

    def step_people(self):
        for m in self.finditer(r"(?:作者|创建人|创建者|拍摄者|摄影师|上传者|上传人|所有者|负责人|制作人|编辑人|修改人|撰写人)\s*(?:是|为|:)?\s*"
                               r"(\x01\d+\x02|[\u4e00-\u9fff]{1,4}|[A-Za-z][A-Za-z0-9.\-_]{1,20})"):
            v = self._person(m.group(1))
            if v:
                self.add("creator", "contains", v)
                self.consume(m.start(), m.end(), "创建人包含“%s”" % v)
        for m in self.finditer(r"(\x01\d+\x02|[\u4e00-\u9fff]{2,5}|[A-Za-z][A-Za-z0-9.\-_]{1,20})[\s\x00]*"
                               r"(?:写|做|建|创建|上传|改|修改|编辑|整理|发|交|拍|负责)(?:过|好|完)?的"):
            v = self._person(m.group(1))
            if not v:
                continue
            self.add("creator", "contains", v)
            self.consume(m.start(), m.start() + len(m.group(1)), "创建人包含“%s”" % v)

    def step_content(self):
        V, E = self.VAL, self.END
        for m in self.finditer(r"(?:内容|正文|文中|文里|文档里|文件里|里面)\s*(?:包含|含有|含|提到|提及|写着|写了|有|涉及|出现)(?:了)?\s*" + V + E):
            v = self.qval(m.group(1))
            if v:
                self.add("content", "contains", v)
                self.consume(m.start(), m.end(), "内容包含“%s”" % v)
        for m in self.finditer(r"(?:提到|提及|涉及|写着)(?:了)?\s*" + V + r"\s*(?:的)"):
            v = self.qval(m.group(1))
            if v:
                self.add("content", "contains", v)
                self.consume(m.start(), m.end(), "内容包含“%s”" % v)

    def step_rating(self):
        for m in self.finditer(r"(\d)\s*星(?:级)?\s*(?:及|和)?\s*(以上|以下)?(?:的)?"):
            n = int(m.group(1))
            op = {"以上": "gte", "以下": "lte"}.get(m.group(2) or "", "eq")
            self.add("rating", op, n)
            self.consume(m.start(), m.end(), "星级%s %d" % ({"gte": "至少", "lte": "至多", "eq": "等于"}[op], n))

    def step_kinds(self):
        inc_k, exc_k, inc_e, exc_e = [], [], [], []
        for pat, kinds in KIND_WORDS:
            for m in self.finditer(r"(%s\s*(?:的)?\s*)?(?:%s)(?:格式|文件|类)?" % (NEG, pat)):
                neg = bool(m.group(1))
                (exc_k if neg else inc_k).extend(kinds)
                self.consume(m.start(), m.end())
        for m in self.finditer(r"(%s\s*(?:的)?\s*)?" % NEG + EXT_RE.pattern + r"(?:格式|文件)?", re.I):
            ext = m.group(2).lower()
            (exc_e if m.group(1) else inc_e).append(ext)
            self.consume(m.start(), m.end())
        kind_exts = dict((k, exts) for k, _, exts in KINDS)
        if inc_k or inc_e:
            if inc_e and inc_k:
                exts = []
                for k in inc_k:
                    exts += kind_exts.get(k, [])
                exts += inc_e
                self.add("ext", "in", sorted(set(exts)))
                self.understood.append({"text": "类型", "meaning": "类型：%s" % "、".join([KIND_LABEL.get(k, k) for k in sorted(set(inc_k))] + sorted(set(inc_e)))})
            elif inc_k:
                self.add("kind", "in", sorted(set(inc_k), key=inc_k.index))
                self.understood.append({"text": "类型", "meaning": "文件类型：%s" % "、".join(KIND_LABEL.get(k, k) for k in sorted(set(inc_k), key=inc_k.index))})
            else:
                self.add("ext", "in", sorted(set(inc_e)))
                self.understood.append({"text": "扩展名", "meaning": "扩展名：%s" % "、".join(sorted(set(inc_e)))})
        if exc_k:
            self.add("kind", "notIn", sorted(set(exc_k)))
            self.understood.append({"text": "排除类型", "meaning": "排除：%s" % "、".join(KIND_LABEL.get(k, k) for k in sorted(set(exc_k)))})
        if exc_e:
            self.add("ext", "notIn", sorted(set(exc_e)))
            self.understood.append({"text": "排除扩展名", "meaning": "排除扩展名：%s" % "、".join(sorted(set(exc_e)))})
        return inc_k, inc_e

    def finish_time_field(self, inc_k, inc_e):
        tf = self.time_field
        if not tf:
            photo_kinds = {"photo", "raw", "video"}
            from .index import EXT_KIND
            kinds = set(inc_k) | set(EXT_KIND.get(e, "other") for e in inc_e)
            mentions_camera = re.search(r"机位|相机|拍|摄", self.t)
            if (kinds and kinds <= photo_kinds) or mentions_camera:
                tf = "exif.time"
            else:
                tf = "mtime"
        for it in self.items:
            if it["field"] == "__T__":
                it["field"] = tf
        if self.limit is not None and not self.limit.get("by"):
            self.limit["by"] = tf if self.limit["order"] in ("newest", "oldest") else "mtime"
        return tf

    def leftovers(self):
        rest = self._restore(self.m.replace("\x00", " "))
        for w in sorted(STOPWORDS, key=len, reverse=True):
            rest = rest.replace(w, " ")
        parts = [p.strip() for p in re.split(r"[\s，。,.；;：:、！!？?（）()\[\]【】\"“”'‘’]+", rest)]
        return [p for p in parts if p and not re.fullmatch(r"[\W_]+", p)]

    def parse(self):
        self.protect_quotes()
        self.step_dest()
        self.step_action()
        self.step_dirnames()
        self.step_limit()
        self.step_group()
        self.detect_time_field()
        self.step_dates()
        self.step_times()
        self.step_numbers()
        self.step_camera()
        self.step_rating()
        self.step_size()
        self.step_content()
        self.step_names()
        self.step_people()
        self.step_folders()
        inc_k, inc_e = self.step_kinds()
        self.consume_verbs()
        self.guess_name()
        tf = self.finish_time_field(inc_k, inc_e)
        if any(it["field"] == tf for it in self.items) and tf == "exif.time":
            self.notes.append("时间按拍摄时间筛选；没有拍摄时间的文件会按设置改用修改时间。")
        ok, errs = validate_items(self.items)
        unknown = self.leftovers()
        rule = {"mode": "all", "items": ok}
        return {
            "items": ok,
            "limit": self.limit,
            "dest": self.dest,
            "dedupe": self.dedupe,
            "understood": self.understood,
            "unknown": unknown,
            "notes": self.notes + errs,
            "summary": describe_plan(rule, self.limit),
            "engine": "local",
        }


def parse(text, context=None):
    text = (text or "").strip()
    if not text:
        return {"items": [], "limit": None, "dest": {}, "dedupe": None, "understood": [], "unknown": [], "notes": [],
                "summary": "", "engine": "local"}
    return Parser(text, context).parse()
