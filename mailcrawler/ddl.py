# -*- coding: utf-8 -*-
"""ddl.py —— 从邮件「主题+正文」里用正则找候选日期，再把选中的候选解析成绝对时间。

设计原则：DDL 的「过没过期」不让 Jev 猜。
  1. 本模块用正则列出所有候选日期（最多 8 个），作为 choice 的选项交给 Jev；
  2. Jev 只负责「哪一个是对学生有意义的截止/活动日期」；
  3. 代码把选中的候选解析成绝对日期，和 datetime.now() 比较，得出 expired / open / none。
"""
import re
from datetime import date, datetime, time, timedelta

MAX_CANDIDATES = 8

MON = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
       "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
MON_RE = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|"
          r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")

# ---- 日期正则（按优先级排列；先匹配的占位，后面的不能重叠）----
# 每项： (名字, 正则, 是否低优先级)
_I = re.IGNORECASE
PATTERNS = [
    # 英文日期区间（16-18 Oct / Oct 16-18 / 16th–18th October）：取区间末日作为候选
    ("en_dm_range", re.compile(r"(?<![\d:/.\-])(?P<d1>\d{1,2})(?:st|nd|rd|th)?\s*[-–—~]\s*"
                               r"(?P<d2>\d{1,2})(?:st|nd|rd|th)?\s*(?:of\s+)?\b(?P<mon>" + MON_RE +
                               r")\b\.?(?:,?\s*(?P<y>20\d{2}))?(?!\d)", _I), False),
    ("en_md_range", re.compile(r"\b(?P<mon>" + MON_RE + r")\b\.?\s*(?P<d1>\d{1,2})(?:st|nd|rd|th)?"
                               r"\s*[-–—~]\s*(?P<d2>\d{1,2})(?:st|nd|rd|th)?\b(?![\d:])"
                               r"(?:,?\s*(?P<y>20\d{2}))?", _I), False),
    # 2026年10月16日 / 10月16日 / 10月16号
    ("cn", re.compile(r"(?<!\d)(?:(?P<y>20\d{2})\s*年\s*)?(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*[日号號]"), False),
    # 2026-10-16 / 2026/10/16 / 2026.10.16
    ("iso", re.compile(r"(?<![\d.\-/])(?P<y>20\d{2})\s?[-/.]\s?(?P<m>\d{1,2})\s?[-/.]\s?(?P<d>\d{1,2})(?!\d)"), False),
    # 16/10/2026（日/月/年；第一段>12 才一定是日，否则也按日/月处理，与国际惯例一致）
    ("dmy", re.compile(r"(?<![\d/.\-])(?P<a>\d{1,2})\s?[/.\-]\s?(?P<b>\d{1,2})\s?[/.\-]\s?(?P<y>20\d{2})(?!\d)"), False),
    # 16 Oct 2026 / 16th October / 16th of Oct
    ("en_dm", re.compile(r"(?<![\d:/.\-])(?P<d>\d{1,2})(?:st|nd|rd|th)?(?:\s+of)?\s+\b(?P<mon>" + MON_RE +
                         r")\b\.?(?:,?\s*(?P<y>20\d{2}))?(?!\d)", _I), False),
    # October 16 / Oct. 16th / Oct 16, 2026
    ("en_md", re.compile(r"\b(?P<mon>" + MON_RE + r")\b\.?\s*(?P<d>\d{1,2})(?:st|nd|rd|th)?\b(?![\d:])"
                         r"(?:,?\s*(?P<y>20\d{2}))?", _I), False),
    # 10/16（月/日）—— 噪音最大，低优先级
    ("md", re.compile(r"(?<![\d/.:])(?P<m>\d{1,2})/(?P<d>\d{1,2})(?![\d/.:])"), True),
]

# ---- 时间正则：17:00 / 23:59PM / 5pm / 下午3点 / 17点30分 ----
TIME_RE = re.compile(
    r"(?P<period>上午|下午|晚上|晚间|中午|凌晨|早上)?\s*(?<!\d)(?:"
    r"(?P<h1>\d{1,2})\s*[:：]\s*(?P<m1>\d{2})(?:\s*[:：]\d{2})?\s*(?P<ap1>[ap]\.?m\.?)?"
    r"|(?P<h2>\d{1,2})\s*(?P<ap2>[ap]\.?m\.?)(?![a-z])"
    r"|(?P<h3>\d{1,2})\s*[点時时](?:\s*(?P<m3>\d{1,2})\s*分?|\s*半)?"
    r")", _I)
TIME_BEFORE_RE = re.compile(r"(?<!\d)(?P<h>\d{1,2})\s*[:：]\s*(?P<m>\d{2})\s*(?P<ap>[ap]\.?m\.?)?\s*(?:on|,|，|\s)*$", _I)


def _to24(h, m, ap, period):
    """把 12/24 小时制统一成 24 小时制；非法返回 None。

    没写上午/下午的裸时间（如「2:00-3:30」）按常理推断：
      1:00–6:59 视为下午（学校邮件里极少有凌晨截止），7:00–11:59 视为上午。
    """
    if ap:
        ap = ap[0].lower()
        if ap == "a" and h == 12:
            h = 0
        elif ap == "p" and h < 12:
            h += 12
    elif period in ("下午", "晚上", "晚间") and h < 12:
        h += 12
    elif period == "中午" and h < 11:
        h += 12
    elif not period and 1 <= h <= 6:
        h += 12
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h, m
    return None


def _time_after(text, end):
    """日期之后 32 字符内找时间（间隔里不能有数字，防止把别的日期的时间认给它）。"""
    seg = text[end:end + 32]
    mt = TIME_RE.search(seg)
    if not mt or re.search(r"\d", seg[:mt.start()]):
        return None
    g = mt.groupdict()
    if g["h1"] is not None:
        ap = g["ap1"]
        if not ap and not g["period"]:
            # "2:00-6:00 pm" / "2-6 pm"：开头没写上下午，借用区间末尾的 am/pm（起点不晚于终点时）
            rng = re.match(r"\s*[-–—~至到]\s*(\d{1,2})(?:\s*[:：]\s*\d{2})?\s*([ap]\.?m\.?)", seg[mt.end():], re.I)
            if rng and int(g["h1"]) % 12 <= int(rng.group(1)) % 12:
                ap = rng.group(2)
        hm = _to24(int(g["h1"]), int(g["m1"]), ap, g["period"])
    elif g["h2"] is not None:
        hm = _to24(int(g["h2"]), 0, g["ap2"], g["period"])
    else:
        mm = 30 if "半" in mt.group(0) else int(g["m3"] or 0)
        hm = _to24(int(g["h3"]), mm, None, g["period"])
    if not hm:
        return None
    return hm[0], hm[1], end + mt.end()


def _time_before(text, start):
    seg = text[max(0, start - 18):start]
    mt = TIME_BEFORE_RE.search(seg)
    if not mt:
        return None
    # "18:00-20:00, 16 Oct"：时间区间在日期前面时，取区间起点
    rs = re.search(r"(?<!\d)(\d{1,2})\s*[:：]\s*(\d{2})\s*[-–—~至到]\s*$", seg[:mt.start()])
    if rs:
        hm = _to24(int(rs.group(1)), int(rs.group(2)), None, None)
        if hm:
            return hm[0], hm[1], start - (len(seg) - rs.start())
    hm = _to24(int(mt.group("h")), int(mt.group("m")), mt.group("ap"), None)
    if not hm:
        return None
    return hm[0], hm[1], start - (len(seg) - mt.start())


def resolve_date(y, m, d, sent):
    """补年份：取离发信日最近、且不早于发信日前 7 天的年份。"""
    try:
        if y:
            return date(y, m, d)
    except ValueError:
        return None
    base = sent.date() if sent else date.today()
    lo = base - timedelta(days=7)
    for yy in (base.year - 1, base.year, base.year + 1):
        try:
            dd = date(yy, m, d)
        except ValueError:
            continue
        if dd >= lo:
            return dd
    return None


def _groups_to_ymd(name, g):
    """返回 (y|None, m, d)；不合法返回 None。英文区间取末日。"""
    y = int(g["y"]) if g.get("y") else None
    if name in ("en_dm_range", "en_md_range"):
        m = MON[g["mon"][:3].lower()]
        d = int(g["d2"])          # 区间末日才是「截止」
        if not (1 <= d <= 31):
            return None
        if y is not None and not (2024 <= y <= 2030):
            return None
        return y, m, d
    if name == "dmy":
        a, b = int(g["a"]), int(g["b"])
        if b > 12 and a <= 12:      # 10/16/2026 这种只能是 月/日/年
            m, d = a, b
        else:                       # 其余按 日/月/年（16/10/2026；两段都 ≤12 时取国际惯例）
            d, m = a, b
        if not (1 <= m <= 12 and 1 <= d <= 31 and 2024 <= y <= 2030):
            return None
        return y, m, d
    if name in ("en_dm", "en_md"):
        m = MON[g["mon"][:3].lower()]
    else:
        m = int(g["m"])
    d = int(g["d"])
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return None
    if y is not None and not (2024 <= y <= 2030):
        return None
    return y, m, d


def extract_candidates(text, sent=None):
    """从 text 里找候选日期，返回最多 8 个 dict（按出现顺序）：
    {raw, ctx, date, hh, mm, has_time, dt(datetime), low}
    """
    text = text or ""
    taken = []  # 已占用区间
    found = []
    for name, rx, low in PATTERNS:
        for mt in rx.finditer(text):
            s, e = mt.span()
            if any(s < b and a < e for a, b in taken):
                continue
            ymd = _groups_to_ymd(name, mt.groupdict())
            if not ymd:
                continue
            y, m, d = ymd
            dd = resolve_date(y, m, d, sent)
            if not dd:
                continue
            taken.append((s, e))
            hh = mm = None
            te = e
            tm = _time_after(text, e)
            if tm:
                hh, mm, te = tm
            else:
                tb = _time_before(text, s)
                if tb:
                    hh, mm, _ = tb
            raw = re.sub(r"\s+", " ", text[s:te]).strip()
            ctx = re.sub(r"\s+", " ", text[max(0, s - 25):min(len(text), te + 25)]).strip()
            found.append({"start": s, "raw": raw, "ctx": ctx, "date": dd, "hh": hh, "mm": mm,
                          "has_time": hh is not None, "low": low})
    found.sort(key=lambda c: c["start"])
    # 同一天去重：保留先出现的；若后出现的带时间而先出现的没有，则升级
    out = []
    by_date = {}
    for c in found:
        k = c["date"]
        if k in by_date:
            o = by_date[k]
            if not o["has_time"] and c["has_time"]:
                o.update(hh=c["hh"], mm=c["mm"], has_time=True, raw=c["raw"], ctx=c["ctx"])
            continue
        by_date[k] = c
        out.append(c)
    if len(out) > MAX_CANDIDATES:  # 超出时先丢低优先级（纯 M/D 斜杠），再按出现顺序截断
        keep = [c for c in out if not c["low"]]
        if len(keep) < MAX_CANDIDATES:
            keep += [c for c in out if c["low"]][:MAX_CANDIDATES - len(keep)]
            keep.sort(key=lambda c: c["start"])
        out = keep[:MAX_CANDIDATES]
    for c in out:
        c["dt"] = datetime.combine(c["date"], time(c["hh"], c["mm"]) if c["has_time"] else time(23, 59))
    return out


def status_of(dt, now=None):
    """dt 为 None → none；早于现在 → expired；否则 open。"""
    if dt is None:
        return "none"
    return "expired" if dt < (now or datetime.now()) else "open"
