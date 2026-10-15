# -*- coding: utf-8 -*-
"""读取文件内部信息：EXIF / RAW 预览图 / 视频时间 / Office 与 PDF 属性 / 文件所有者 / 正文 / 哈希。
全部只用标准库实现，任何解析失败都静默返回空值，不影响其他文件。"""
import hashlib
import html
import os
import re
import struct
import threading
import zipfile
import zlib
from collections import OrderedDict
from datetime import datetime

from .util import IS_WIN, long_path, local_ts

JPEG_EXTS = {"jpg", "jpeg", "jpe", "jfif"}
TIFF_RAW_EXTS = {"cr2", "nef", "nrw", "arw", "srf", "sr2", "dng", "orf", "rw2", "pef", "srw", "3fr", "erf", "kdc",
                 "mos", "mef", "iiq", "rwl", "dcr", "tif", "tiff", "fff", "cap", "eip"}
VIDEO_MP4_EXTS = {"mp4", "mov", "m4v", "3gp", "3g2", "mqv"}
BROWSER_IMAGES = {"png": "image/png", "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
                  "avif": "image/avif", "svg": "image/svg+xml", "ico": "image/x-icon",
                  "heic": "image/heic", "heif": "image/heif"}
VIDEO_TYPES = {"mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime", "webm": "video/webm",
               "ogv": "video/ogg"}
AUDIO_TYPES = {"mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4", "aac": "audio/aac", "flac": "audio/flac",
               "ogg": "audio/ogg"}
TEXT_EXTS = {"txt", "md", "csv", "tsv", "log", "json", "xml", "html", "htm", "ini", "cfg", "conf", "yaml", "yml", "srt",
             "ass", "lrc", "py", "js", "ts", "css", "sql", "bat", "sh", "ps1", "c", "h", "cpp", "java", "go", "rs"}
OFFICE_EXTS = {"docx", "docm", "xlsx", "xlsm", "pptx", "pptm", "dotx", "xltx", "potx"}
ODF_EXTS = {"odt", "ods", "odp"}
CONTENT_EXTS = TEXT_EXTS | OFFICE_EXTS | ODF_EXTS | {"pdf"}

TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}


# ================================================================== 字节来源
class MemSrc:
    def __init__(self, data, origin=0):
        self.data = data
        self.origin = origin  # data[0] 在文件中的位置
        self.size = origin + len(data)

    def read(self, off, n):
        if off < self.origin:
            return b""
        o = off - self.origin
        return self.data[o:o + n]


class FileSrc:
    def __init__(self, f, size):
        self.f = f
        self.size = size

    def read(self, off, n):
        if off < 0 or off >= self.size or n <= 0:
            return b""
        self.f.seek(off)
        return self.f.read(n)


# ================================================================== TIFF / EXIF
class Tiff:
    def __init__(self, src, base):
        self.src = src
        self.base = base
        hdr = src.read(base, 8)
        if len(hdr) < 8:
            raise ValueError("short")
        if hdr[:2] == b"II":
            self.e = "<"
        elif hdr[:2] == b"MM":
            self.e = ">"
        else:
            raise ValueError("not tiff")
        self.first = struct.unpack(self.e + "I", hdr[4:8])[0]

    def ifd(self, off):
        if off <= 0 or self.base + off + 2 > self.src.size:
            return {}, 0
        raw = self.src.read(self.base + off, 2)
        if len(raw) < 2:
            return {}, 0
        n = struct.unpack(self.e + "H", raw)[0]
        if n == 0 or n > 1000:
            return {}, 0
        data = self.src.read(self.base + off + 2, n * 12 + 4)
        out = {}
        for i in range(n):
            ent = data[i * 12:(i + 1) * 12]
            if len(ent) < 12:
                break
            tag, typ, cnt = struct.unpack(self.e + "HHI", ent[:8])
            out[tag] = (typ, cnt, ent[8:12])
        nxt = 0
        if len(data) >= n * 12 + 4:
            nxt = struct.unpack(self.e + "I", data[n * 12:n * 12 + 4])[0]
        return out, nxt

    def raw(self, ent, maxlen=65536):
        typ, cnt, val = ent
        size = TYPE_SIZE.get(typ, 1) * cnt
        if size <= 4:
            return val[:size]
        off = struct.unpack(self.e + "I", val)[0]
        return self.src.read(self.base + off, min(size, maxlen))

    def data_pos(self, ent):
        typ, cnt, val = ent
        size = TYPE_SIZE.get(typ, 1) * cnt
        if size <= 4:
            return None, size
        return self.base + struct.unpack(self.e + "I", val)[0], size

    def text(self, ent):
        b = self.raw(ent, 1024)
        b = b.split(b"\x00")[0].strip()
        if not b:
            return ""
        try:
            return b.decode("utf-8").strip()
        except UnicodeDecodeError:
            return b.decode("latin-1").strip()

    def ints(self, ent, limit=64):
        typ, cnt, _ = ent
        b = self.raw(ent, 4 * min(cnt, limit))
        if typ in (3, 8):
            n = min(cnt, len(b) // 2, limit)
            return list(struct.unpack(self.e + ("H" if typ == 3 else "h") * n, b[:n * 2]))
        if typ in (4, 9, 13):
            n = min(cnt, len(b) // 4, limit)
            return list(struct.unpack(self.e + ("I" if typ != 9 else "i") * n, b[:n * 4]))
        if typ in (1, 6, 7):
            return list(b[:limit])
        return []

    def int(self, ent):
        v = self.ints(ent, 1)
        return v[0] if v else None


def parse_exif_text(s):
    m = re.match(r"\s*(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})", s or "")
    if not m:
        return None
    try:
        vals = [int(x) for x in m.groups()]
        if vals[0] < 1971:
            return None
        return local_ts(datetime(*vals))
    except ValueError:
        return None


IFD0_TAGS = {0x010F: "make", 0x0110: "model", 0x013B: "artist", 0x8298: "copyright", 0xC62F: "serial"}


def tiff_meta(t, res, previews):
    """遍历 IFD0 链、SubIFD、EXIF IFD，收集信息与内嵌 JPEG 位置。"""
    seen = set()
    queue = [(t.first, "ifd0", 0)]
    times = {}
    ifd_index = 0
    while queue and len(seen) < 24:
        off, kind, depth = queue.pop(0)
        if off in seen or off <= 0:
            continue
        seen.add(off)
        ents, nxt = t.ifd(off)
        if not ents:
            continue
        if kind == "ifd0":
            ifd_index += 1
            if nxt and ifd_index < 5:
                queue.append((nxt, "ifd0", depth))
        for tag, key in IFD0_TAGS.items():
            if tag in ents and key not in res:
                v = t.text(ents[tag])
                if v:
                    res[key] = v
        for tag in (0x9003, 0x9004, 0x0132):
            if tag in ents and tag not in times:
                ts = parse_exif_text(t.text(ents[tag]))
                if ts:
                    times[tag] = ts
        if 0x0112 in ents and "orientation" not in res and kind == "ifd0" and ifd_index == 1:
            o = t.int(ents[0x0112])
            if o and 1 <= o <= 8:
                res["orientation"] = o
        if 0x4746 in ents and "rating" not in res:
            r = t.int(ents[0x4746])
            if r is not None and 0 <= r <= 5:
                res["rating"] = r
        if 0xA431 in ents and "serial" not in res:
            v = t.text(ents[0xA431])
            if v:
                res["serial"] = v
        if 0xA434 in ents and "lens" not in res:
            v = t.text(ents[0xA434])
            if v:
                res["lens"] = v
        if 0x8769 in ents and depth < 3:
            o = t.int(ents[0x8769])
            if o:
                queue.append((o, "exif", depth + 1))
        if 0x014A in ents and depth < 3:
            for o in t.ints(ents[0x014A], 16):
                queue.append((o, "sub", depth + 1))
        # 内嵌 JPEG 预览
        if 0x0201 in ents and 0x0202 in ents:
            o, n = t.int(ents[0x0201]), t.int(ents[0x0202])
            if o and n:
                previews.append((t.base + o, n))
        comp = t.int(ents[0x0103]) if 0x0103 in ents else None
        if comp in (6, 7, 99) and 0x0111 in ents and 0x0117 in ents:
            offs, lens = t.ints(ents[0x0111], 2), t.ints(ents[0x0117], 2)
            if len(offs) == 1 and len(lens) == 1 and lens[0] > 1024:
                previews.append((t.base + offs[0], lens[0]))
        if 0x002E in ents:  # Panasonic RW2 JpgFromRaw
            pos, size = t.data_pos(ents[0x002E])
            if pos:
                previews.append((pos, size))
    for tag in (0x9003, 0x9004, 0x0132):
        if tag in times:
            res["time"] = times[tag]
            break


def xmp_info(text):
    out = {}
    if not text:
        return out
    m = re.search(r'xmp:Rating\s*=\s*["\'](-?\d)["\']|<xmp:Rating>\s*(-?\d)\s*<', text)
    if m:
        r = int(m.group(1) or m.group(2))
        if 0 <= r <= 5:
            out["rating"] = r
    m = re.search(r'xmp:Label\s*=\s*["\']([^"\']*)["\']|<xmp:Label>([^<]*)<', text)
    if m:
        out["label"] = html.unescape((m.group(1) or m.group(2) or "").strip())
    m = re.search(r"<dc:creator>\s*<rdf:Seq>\s*<rdf:li[^>]*>([^<]*)</rdf:li>", text)
    if m:
        out["artist"] = html.unescape(m.group(1).strip())
    return out


def parse_jpeg(src, start, res, previews, limit):
    """解析 JPEG 段：APP1 EXIF / APP1 XMP。src 可以是内存或文件。"""
    i = start + 2
    end = min(limit, src.size)
    count = 0
    while i + 4 <= end and count < 40:
        count += 1
        hdr = src.read(i, 4)
        if len(hdr) < 4 or hdr[0] != 0xFF:
            break
        marker = hdr[1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker in (0xDA, 0xD9):
            break
        seglen = struct.unpack(">H", hdr[2:4])[0]
        if marker == 0xE1:
            ident = src.read(i + 4, 30)
            if ident.startswith(b"Exif\x00\x00"):
                try:
                    tiff_meta(Tiff(src, i + 10), res, previews)
                except (ValueError, struct.error):
                    pass
            elif ident.startswith(b"http://ns.adobe.com/xap/1.0/"):
                body = src.read(i + 4, seglen - 2)
                info = xmp_info(body.decode("utf-8", "ignore"))
                for k, v in info.items():
                    res.setdefault(k, v)
        i += 2 + seglen


def mp4_time(f, size):
    """读取 MP4 / MOV 的 mvhd 创建时间（UTC）。"""
    chunks = [(0, f.read(min(size, 1 << 20)))]
    if size > (1 << 20):
        tail = max(0, size - (4 << 20))
        f.seek(tail)
        chunks.append((tail, f.read(4 << 20)))
    for _, data in chunks:
        idx = data.find(b"mvhd")
        if idx < 0:
            continue
        ver = data[idx + 4] if idx + 4 < len(data) else 0
        try:
            if ver == 1:
                secs = struct.unpack(">Q", data[idx + 8:idx + 16])[0]
            else:
                secs = struct.unpack(">I", data[idx + 8:idx + 12])[0]
        except struct.error:
            continue
        ts = secs - 2082844800
        if 0 < ts < 4102444800:
            return float(ts)
    return None


def scan_jpegs(data, origin, maxn=8):
    out = []
    pos = 0
    while len(out) < maxn:
        s = data.find(b"\xff\xd8\xff", pos)
        if s < 0:
            break
        e = data.find(b"\xff\xd9", s + 3)
        if e < 0:
            break
        # 跳过内嵌缩略图的结束标记：继续寻找直到长度合理
        n = e + 2 - s
        if n > 2048:
            out.append((origin + s, n))
        pos = s + 3
    return out


def read_exif(path, ext, size):
    """返回 dict：time, make, model, serial, lens, artist, rating, orientation, previews[(偏移, 长度)]"""
    res = {}
    previews = []
    try:
        with open(long_path(path), "rb") as f:
            head = f.read(16)
            f.seek(0)
            if head[:3] == b"\xff\xd8\xff":
                data = f.read(196608)
                parse_jpeg(MemSrc(data), 0, res, previews, len(data))
            elif head.startswith(b"FUJIFILMCCD-RAW"):
                hdr = f.read(100)
                joff, jlen = struct.unpack(">II", hdr[84:92])
                f.seek(joff)
                data = f.read(196608)
                parse_jpeg(MemSrc(data, joff), joff, res, previews, joff + len(data))
                previews.append((joff, jlen))
            elif head[:4] in (b"II*\x00", b"MM\x00*", b"IIU\x00", b"IIRO", b"IIRS", b"MMOR"):
                src = FileSrc(f, size)
                try:
                    t = Tiff(src, 0)
                    tiff_meta(t, res, previews)
                except (ValueError, struct.error):
                    pass
            elif ext in VIDEO_MP4_EXTS or head[4:8] in (b"ftyp", b"moov", b"wide", b"mdat"):
                is_cr3 = head[8:12] == b"crx "
                if not is_cr3 and ext not in ("heic", "heif", "avif", "cr3"):
                    ts = mp4_time(f, size)
                    if ts:
                        res["time"] = ts
                    return res
                data = f.read(min(size, 8 << 20))
                _search_embedded(data, res, previews)
            else:
                data = f.read(min(size, 1 << 20))
                _search_embedded(data, res, previews)
    except (OSError, ValueError, struct.error):
        pass
    if previews:
        res["previews"] = previews
    return res


def _search_embedded(data, res, previews):
    for marker, skip in ((b"CMT1", 4), (b"CMT2", 4), (b"Exif\x00\x00", 6)):
        idx = data.find(marker)
        if idx >= 0:
            try:
                tiff_meta(Tiff(MemSrc(data), idx + skip), res, [])
            except (ValueError, struct.error):
                pass
    if "time" not in res:
        m = re.search(rb"(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})", data[:1 << 20])
        if m:
            ts = parse_exif_text(m.group(0).decode())
            if ts:
                res["time"] = ts
    previews.extend(scan_jpegs(data, 0))


def read_sidecar(path):
    """读取同名 .xmp 旁注文件（Lightroom 等软件写入的星级、标签）。"""
    base, _ = os.path.splitext(path)
    for p in (base + ".xmp", base + ".XMP", path + ".xmp"):
        try:
            with open(long_path(p), "rb") as f:
                return xmp_info(f.read(262144).decode("utf-8", "ignore"))
        except OSError:
            continue
    return {}


# ================================================================== 缩略图 / 预览
def _read_range(path, off, n):
    with open(long_path(path), "rb") as f:
        f.seek(off)
        return f.read(n)


def _valid_jpeg(b):
    return len(b) > 1024 and b[:3] == b"\xff\xd8\xff"


def preview_image(path, ext, size, exif, want="thumb"):
    """返回 (字节, MIME, 需要前端旋转的方向)。want = thumb 或 view。"""
    ext = (ext or "").lower()
    orient = (exif or {}).get("orientation") or 1
    cands = sorted(set((exif or {}).get("previews") or []), key=lambda x: x[1])
    if ext in JPEG_EXTS:
        if want == "thumb":
            for off, n in cands:
                if n < 600000:
                    b = _read_range(path, off, n)
                    if _valid_jpeg(b):
                        return b, "image/jpeg", orient
        if size <= 40 << 20:
            return _read_range(path, 0, size), "image/jpeg", 1
        return None
    if ext in BROWSER_IMAGES:
        if size <= 40 << 20:
            return _read_range(path, 0, size), BROWSER_IMAGES[ext], 1
        return None
    if cands:
        order = cands if want == "thumb" else list(reversed(cands))
        for off, n in order:
            if want == "thumb" and n < 2048:
                continue
            if n > 40 << 20:
                continue
            b = _read_range(path, off, n)
            if _valid_jpeg(b):
                return b, "image/jpeg", orient
    return None


# ================================================================== Office / PDF
def _xml_tag(xml, names):
    for n in names:
        m = re.search(r"<%s(?:\s[^>]*)?>(.*?)</%s>" % (re.escape(n), re.escape(n)), xml, re.S)
        if m:
            v = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
            if v:
                return v
    return ""


def _pdf_string(raw):
    raw = raw.strip()
    if raw.startswith(b"<") and not raw.startswith(b"<<"):
        hx = re.sub(rb"[^0-9A-Fa-f]", b"", raw[1:raw.find(b">")] if b">" in raw else raw[1:])
        if len(hx) % 2:
            hx += b"0"
        try:
            b = bytes.fromhex(hx.decode())
        except ValueError:
            return ""
    elif raw.startswith(b"("):
        out = bytearray()
        depth = 0
        i = 0
        while i < len(raw):
            c = raw[i]
            if c == 0x5C and i + 1 < len(raw):  # 反斜杠
                n = raw[i + 1]
                esc = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12}
                if n in esc:
                    out.append(esc[n])
                    i += 2
                    continue
                if 0x30 <= n <= 0x37:
                    m = re.match(rb"[0-7]{1,3}", raw[i + 1:i + 4])
                    out.append(int(m.group(0), 8) & 0xFF)
                    i += 1 + len(m.group(0))
                    continue
                if n in (0x0A, 0x0D):
                    i += 2
                    continue
                out.append(n)
                i += 2
                continue
            if c == 0x28:
                depth += 1
                if depth == 1:
                    i += 1
                    continue
            elif c == 0x29:
                depth -= 1
                if depth == 0:
                    break
            out.append(c)
            i += 1
        b = bytes(out)
    else:
        return ""
    if b.startswith(b"\xfe\xff"):
        return b[2:].decode("utf-16-be", "replace").strip("\x00 ")
    if b.startswith(b"\xef\xbb\xbf"):
        return b[3:].decode("utf-8", "replace").strip()
    try:
        return b.decode("utf-8").strip()
    except UnicodeDecodeError:
        return b.decode("latin-1").strip()


def doc_meta(path, ext, size):
    """返回 {author, modifiedBy, title}。"""
    ext = (ext or "").lower()
    out = {}
    try:
        if ext in OFFICE_EXTS:
            with zipfile.ZipFile(long_path(path)) as z:
                xml = z.read("docProps/core.xml").decode("utf-8", "ignore")
            out["author"] = _xml_tag(xml, ["dc:creator"])
            out["modifiedBy"] = _xml_tag(xml, ["cp:lastModifiedBy"])
            out["title"] = _xml_tag(xml, ["dc:title"])
        elif ext in ODF_EXTS:
            with zipfile.ZipFile(long_path(path)) as z:
                xml = z.read("meta.xml").decode("utf-8", "ignore")
            out["author"] = _xml_tag(xml, ["meta:initial-creator", "dc:creator"])
            out["modifiedBy"] = _xml_tag(xml, ["dc:creator"])
            out["title"] = _xml_tag(xml, ["dc:title"])
        elif ext == "pdf":
            with open(long_path(path), "rb") as f:
                head = f.read(min(size, 1 << 20))
                tail = b""
                if size > (1 << 20):
                    f.seek(max(0, size - (512 << 10)))
                    tail = f.read()
            data = head + tail
            xmp = data.find(b"<x:xmpmeta")
            if xmp >= 0:
                seg = data[xmp:xmp + 65536].decode("utf-8", "ignore")
                m = re.search(r"<dc:creator>.*?<rdf:li[^>]*>([^<]*)</rdf:li>", seg, re.S)
                if m:
                    out["author"] = html.unescape(m.group(1)).strip()
                m = re.search(r"<dc:title>.*?<rdf:li[^>]*>([^<]*)</rdf:li>", seg, re.S)
                if m:
                    out["title"] = html.unescape(m.group(1)).strip()
            for key, name in (("author", b"/Author"), ("title", b"/Title")):
                if out.get(key):
                    continue
                for m in re.finditer(re.escape(name) + rb"\s*([(<])", data):
                    s = _pdf_string(data[m.start(1):m.start(1) + 2048])
                    if s:
                        out[key] = s
                        break
    except (OSError, KeyError, zipfile.BadZipFile, ValueError, RuntimeError):
        pass
    return {k: v for k, v in out.items() if v}


# ================================================================== 文件所有者
_owner_cache = {}
_owner_lock = threading.Lock()


def file_owner(path, st):
    if IS_WIN:
        return _win_owner(path)
    try:
        uid = st.st_uid
    except AttributeError:
        return ""
    if uid in _owner_cache:
        return _owner_cache[uid]
    try:
        import pwd
        name = pwd.getpwuid(uid).pw_name
    except (KeyError, ImportError):
        name = str(uid)
    _owner_cache[uid] = name
    return name


def _win_owner(path):
    try:
        import ctypes
        from ctypes import wintypes
        adv = ctypes.windll.advapi32
        k32 = ctypes.windll.kernel32
        psid = ctypes.c_void_p()
        psd = ctypes.c_void_p()
        adv.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
                                              ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        rc = adv.GetNamedSecurityInfoW(long_path(path), 1, 1, ctypes.byref(psid), None, None, None, ctypes.byref(psd))
        if rc != 0:
            return ""
        try:
            sid_str = ctypes.c_wchar_p()
            adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
            key = ""
            if adv.ConvertSidToStringSidW(psid, ctypes.byref(sid_str)):
                key = sid_str.value or ""
                k32.LocalFree(sid_str)
            with _owner_lock:
                if key and key in _owner_cache:
                    return _owner_cache[key]
            name = ctypes.create_unicode_buffer(256)
            dom = ctypes.create_unicode_buffer(256)
            nlen = wintypes.DWORD(256)
            dlen = wintypes.DWORD(256)
            use = wintypes.DWORD()
            adv.LookupAccountSidW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPWSTR,
                                              ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
                                              ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
            if adv.LookupAccountSidW(None, psid, name, ctypes.byref(nlen), dom, ctypes.byref(dlen), ctypes.byref(use)):
                res = (dom.value + "\\" + name.value) if dom.value else name.value
            else:
                res = key
            with _owner_lock:
                if key:
                    _owner_cache[key] = res
            return res
        finally:
            k32.LocalFree(psd)
    except Exception:
        return ""


# ================================================================== 正文
def _decode_text(b):
    if b.startswith(b"\xef\xbb\xbf"):
        return b[3:].decode("utf-8", "replace")
    if b.startswith((b"\xff\xfe", b"\xfe\xff")):
        return b.decode("utf-16", "replace")
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("gb18030", "replace")


def _strip_xml(xml):
    xml = re.sub(r"</(w:p|a:p|text:p|text:h|si|row)>", "\n", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    return html.unescape(xml)


def _pdf_text(data):
    parts = []
    for m in re.finditer(rb"stream\r?\n", data):
        start = m.end()
        end = data.find(b"endstream", start)
        if end < 0:
            break
        dict_start = data.rfind(b"<<", max(0, m.start() - 600), m.start())
        hdr = data[dict_start:m.start()] if dict_start >= 0 else b""
        if b"/Image" in hdr or b"/Font" in hdr and b"/Length1" in hdr:
            continue
        body = data[start:end]
        if b"FlateDecode" in hdr:
            try:
                body = zlib.decompress(body)
            except zlib.error:
                try:
                    body = zlib.decompressobj().decompress(body)
                except zlib.error:
                    continue
        for t in re.findall(rb"\(((?:\\.|[^\\)])*)\)\s*T[jJ']|\[([^\]]*)\]\s*TJ", body):
            if t[0]:
                parts.append(_pdf_string(b"(" + t[0] + b")"))
            elif t[1]:
                parts.append("".join(_pdf_string(b"(" + x + b")") for x in re.findall(rb"\(((?:\\.|[^\\)])*)\)", t[1])))
        if sum(len(p) for p in parts) > 2000000:
            break
    return " ".join(parts)


def read_text(path, ext, size):
    ext = (ext or "").lower()
    try:
        if ext in TEXT_EXTS:
            with open(long_path(path), "rb") as f:
                return _decode_text(f.read(min(size, 8 << 20)))
        if ext in OFFICE_EXTS or ext in ODF_EXTS:
            texts = []
            with zipfile.ZipFile(long_path(path)) as z:
                names = z.namelist()
                if ext in ODF_EXTS:
                    wanted = ["content.xml"]
                elif ext.startswith("doc") or ext == "dotx":
                    wanted = [n for n in names if re.match(r"word/(document|header\d*|footer\d*|footnotes)\.xml$", n)]
                elif ext.startswith("xl"):
                    wanted = ["xl/sharedStrings.xml"] + [n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)]
                else:
                    wanted = [n for n in names if re.match(r"ppt/(slides|notesSlides)/\w+\d+\.xml$", n)]
                for n in wanted:
                    if n in names:
                        texts.append(_strip_xml(z.read(n).decode("utf-8", "ignore")))
            return "\n".join(texts)
        if ext == "pdf":
            with open(long_path(path), "rb") as f:
                return _pdf_text(f.read(min(size, 40 << 20)))
    except (OSError, KeyError, zipfile.BadZipFile, ValueError, RuntimeError, zlib.error):
        pass
    return ""


def file_hash(path, cancel=None):
    h = hashlib.sha1()
    with open(long_path(path), "rb") as f:
        while True:
            if cancel is not None and cancel.is_set():
                raise RuntimeError("cancelled")
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ================================================================== 缓存
class MetaCache:
    """按 (路径, 大小, 修改时间) 缓存，文件变化后自动失效。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.store = {}
        self.text = OrderedDict()

    def _key(self, rec):
        return (rec.path, rec.size, rec.mtime)

    def get(self, rec, part):
        key = self._key(rec)
        d = self.store.get(key)
        if d is not None and part in d:
            return d[part]
        if part == "exif":
            v = read_exif(rec.path, rec.ext, rec.size)
            if "rating" not in v or "artist" not in v:
                side = read_sidecar(rec.path) if rec.ext not in ("xmp",) else {}
                for k, val in side.items():
                    v.setdefault(k, val)
        elif part == "doc":
            v = doc_meta(rec.path, rec.ext, rec.size)
        elif part == "owner":
            try:
                st = os.stat(long_path(rec.path))
                v = file_owner(rec.path, st)
            except OSError:
                v = ""
        elif part == "hash":
            v = file_hash(rec.path)
        elif part == "content":
            with self.lock:
                if key in self.text:
                    self.text.move_to_end(key)
                    return self.text[key]
            v = read_text(rec.path, rec.ext, rec.size).lower()
            with self.lock:
                self.text[key] = v
                while len(self.text) > 300:
                    self.text.popitem(last=False)
            return v
        else:
            raise KeyError(part)
        with self.lock:
            self.store.setdefault(key, {})[part] = v
        return v


META = MetaCache()
