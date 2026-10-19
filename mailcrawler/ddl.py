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

# ---- 时间正则：17:00 / 23:59PM / 5pm / 下午3点 / 17点30分 / 下午两点半，以及它们组成的区间 ----
_PERIODS = r"上午|下午|晚上|晚间|中午|凌晨|早上"
_CNUM = "一二两三四五六七八九十"
_CNV = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn(s):
    """'3' / '三' / '十二' / '二十五' → int；认不出返回 None。"""
    if s.isdigit():
        return int(s)
    try:
        if "十" in s:
            a, b = s.split("十", 1)
            return (_CNV[a] if a else 1) * 10 + (_CNV[b] if b else 0)
        return _CNV[s] if len(s) == 1 else None
    except KeyError:
        return None


def _tok(s, bare=False):
    """一个时间点的正则片段；s 是命名组后缀（同一个正则里出现两次时要区分）。
    bare=True 时额外允许「只有小时的数字」（2-3:30pm 的开头），由后面的校验限制使用场合。"""
    rx = (r"(?P<period%(s)s>" + _PERIODS + r")?\s*(?<!\d)(?:"
          r"(?P<h1%(s)s>\d{1,2})\s*[:：]\s*(?P<m1%(s)s>\d{2})(?:\s*[:：]\d{2})?\s*(?P<ap1%(s)s>[ap]\.?m\.?)?"
          r"|(?P<h2%(s)s>\d{1,2})\s*(?P<ap2%(s)s>[ap]\.?m\.?)(?![a-z])"
          r"|(?P<h3%(s)s>\d{1,2}|[" + _CNUM + r"]{1,3})\s*[点時时]"
          r"(?:\s*(?:(?P<m3%(s)s>\d{1,2}|[" + _CNUM + r"]{1,3})\s*分?|(?P<half%(s)s>半)))?")
    if bare:
        rx += r"|(?P<hb%(s)s>\d{1,2})(?![\d:：点時时分a-zA-Z])"
    return (rx + ")") % {"s": s}


_SEP = r"\s*(?:[-–—~～至到]|\bto\b)\s*"
TIME_RE = re.compile(_tok(""), re.I)
RANGE_RE = re.compile(_tok("a", True) + _SEP + _tok("b"), re.I)
RANGE_BEFORE_RE = re.compile(_tok("a", True) + _SEP + _tok("b") + r"\s*(?:on|,|，|\s)*$", re.I)
TIME_BEFORE_RE = re.compile(r"(?<!\d)(?P<h>\d{1,2})\s*[:：]\s*(?P<m>\d{2})\s*(?P<ap>[ap]\.?m\.?)?\s*(?:on|,|，|\s)*$", re.I)


def _read_tok(g, s):
    """从命名组里读出一个时间点：{h, m, ap, period, cn, bare}；没有返回 None。"""
    period = g.get("period" + s)
    if g.get("h1" + s) is not None:
        return {"h": int(g["h1" + s]), "m": int(g["m1" + s]), "ap": g.get("ap1" + s), "period": period, "cn": False, "bare": False}
    if g.get("h2" + s) is not None:
        return {"h": int(g["h2" + s]), "m": 0, "ap": g.get("ap2" + s), "period": period, "cn": False, "bare": False}
    if g.get("h3" + s) is not None:
        h = _cn(g["h3" + s])
        m = 30 if g.get("half" + s) else (_cn(g["m3" + s]) if g.get("m3" + s) else 0)
        if h is None or m is None:
            return None
        return {"h": h, "m": m, "ap": None, "period": period, "cn": not g["h3" + s].isdigit(), "bare": False}
    if g.get("hb" + s) is not None:
        return {"h": int(g["hb" + s]), "m": 0, "ap": None, "period": period, "cn": False, "bare": True}
    return None


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


def _resolve_range(a, b):
    """起止两个时间点 → (sh, sm, eh, em)；起点认不出返回 None，终点认不出/早于起点则 eh/em 为 None。

    规则：一端写了上午/下午或 am/pm 而另一端没写，另一端借用它；都没写按「1–6 点算下午」。
    只在结尾写了一个 pm 导致开始晚于结束（11:00-1:00 pm）时，起点退回不借用。"""
    a_has = bool(a["ap"] or a["period"])
    b_has = bool(b["ap"] or b["period"])
    inherited = False
    sa, sp, ea, ep = a["ap"], a["period"], b["ap"], b["period"]
    if not a_has and b_has:
        sa, sp = ea, ep
        inherited = True
    elif a_has and not b_has:
        ea, ep = sa, sp
    s = _to24(a["h"], a["m"], sa, sp)
    e = _to24(b["h"], b["m"], ea, ep)
    if s is None:
        return None
    if e is not None and e <= s and e[0] < 12 and not (ea or ep):
        e = (e[0] + 12, e[1]) if e[0] + 12 < 24 else e
    if e is not None and e <= s and inherited:
        s0 = _to24(a["h"], a["m"], None, None)
        if s0 is not None and s0 < e:
            s = s0
    if e is None or e <= s:
        return s[0], s[1], None, None
    return s[0], s[1], e[0], e[1]


def _range_from_match(mt):
    """RANGE_RE 的匹配 → (sh, sm, eh, em) 或 None（校验裸小时/中文数字的使用场合）。"""
    g = mt.groupdict()
    a, b = _read_tok(g, "a"), _read_tok(g, "b")
    if not a or not b:
        return None
    # 只有小时的开头（2-3:30pm、10-12点）：结尾必须带 am/pm 或「点」，否则太容易把别的数字当时间
    if a["bare"] and not (b["ap"] or g.get("h3b") is not None):
        return None
    return _resolve_range(a, b)


def _time_after(text, end):
    """日期之后 40 字符内找时间或时间段（间隔里不能有数字，防止把别的日期的时间认给它）。
    返回 (hh, mm, eh, em, 结束位置)；eh/em 为 None 表示只有开始时间。"""
    seg = text[end:end + 40]
    mr = RANGE_RE.search(seg)
    if mr and not re.search(r"\d", seg[:mr.start()]):
        r = _range_from_match(mr)
        if r:
            return r[0], r[1], r[2], r[3], end + mr.end()
    mt = TIME_RE.search(seg)
    if not mt or re.search(r"\d", seg[:mt.start()]):
        return None
    t = _read_tok(mt.groupdict(), "")
    if not t or (t["cn"] and not t["period"]):      # 「有一点」「差一点」这类不是时间
        return None
    hm = _to24(t["h"], t["m"], t["ap"], t["period"])
    if not hm:
        return None
    return hm[0], hm[1], None, None, end + mt.end()


def _time_before(text, start):
    """日期前面紧挨着的时间或时间段（"18:00-20:00, 16 Oct"）。返回 (hh, mm, eh, em, 起始位置) 或 None。"""
    base = max(0, start - 44)
    seg = text[base:start]
    mr = RANGE_BEFORE_RE.search(seg)
    if mr:
        r = _range_from_match(mr)
        if r:
            return r[0], r[1], r[2], r[3], base + mr.start()
    seg = text[max(0, start - 18):start]
    mt = TIME_BEFORE_RE.search(seg)
    if not mt:
        return None
    hm = _to24(int(mt.group("h")), int(mt.group("m")), mt.group("ap"), None)
    if not hm:
        return None
    return hm[0], hm[1], None, None, start - (len(seg) - mt.start())


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


def clean_text(text):
    """邮件正文里残留的 HTML 空格实体换成普通空格（长度会变，所以只在提取前统一做一次）。"""
    return re.sub(r"&nbsp;?| ", " ", text or "")


def extract_candidates(text, sent=None):
    """从 text 里找候选日期，返回最多 8 个 dict（按出现顺序）：
    {raw, ctx, date, hh, mm, eh, em, has_time, has_end, dt(开始 datetime), dt_end(结束 datetime 或 None), low}
    时间段（14:00-15:30、下午2点到3点半）同时取起止；只有开始时 eh/em 为 None。
    """
    text = clean_text(text)
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
            found.append({"start": s, "end": e, "date": dd, "low": low,
                          "hh": None, "mm": None, "eh": None, "em": None, "te": e, "has_time": False})
    found.sort(key=lambda c: c["start"])
    # 先找「日期后面的时间」，占用的时间段不能再被别的日期当作「日期前面的时间」
    used = []
    for c in found:
        tm = _time_after(text, c["end"])
        if tm:
            c.update(hh=tm[0], mm=tm[1], eh=tm[2], em=tm[3], te=tm[4], has_time=True)
            used.append((c["end"], tm[4]))
    for c in found:
        if c["has_time"]:
            continue
        tb = _time_before(text, c["start"])
        if tb and not any(tb[4] < b and a < c["start"] for a, b in used):
            c.update(hh=tb[0], mm=tb[1], eh=tb[2], em=tb[3], has_time=True)
    for c in found:
        s, te = c["start"], c["te"]
        c["raw"] = re.sub(r"\s+", " ", text[s:te]).strip()
        c["ctx"] = re.sub(r"\s+", " ", text[max(0, s - 25):min(len(text), te + 25)]).strip()
    # 同一天去重：保留先出现的；若后出现的带时间而先出现的没有，则升级
    out = []
    by_date = {}
    for c in found:
        k = c["date"]
        if k in by_date:
            o = by_date[k]
            if not o["has_time"] and c["has_time"]:
                o.update(hh=c["hh"], mm=c["mm"], eh=c["eh"], em=c["em"], has_time=True, raw=c["raw"], ctx=c["ctx"])
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
        c["has_end"] = c["eh"] is not None
        c["dt_end"] = datetime.combine(c["date"], time(c["eh"], c["em"])) if c["has_end"] else None
    return out


def read_as(c):
    """候选在给 Jev 的选项里怎么写：2026-10-19 14:00–15:30 / 2026-10-19 17:00 / 2026-10-19（只有日期）。"""
    if not c["has_time"]:
        return c["date"].isoformat()
    s = c["dt"].strftime("%Y-%m-%d %H:%M")
    return s + ("–" + c["dt_end"].strftime("%H:%M") if c["has_end"] else "")


# ---- 活动地点：地点：/ 地址：/ Venue: / Location: 后面的一小段 ----
_PLACE_LABEL = re.compile(r"(?:地点|地址|活动地点|会议地点|Venue|Location|Place|Where)\s*[:：]\s*", re.I)
# 下一个字段名（「语言：」「嘉宾简介：」「Time:」……）：地点到它前面为止
_NEXT_FIELD = re.compile(r"\s+[^\s:：]{1,10}[:：]|[　\t¶]|\s{2,}")
PLACE_MAX = 48
# 地点后面紧跟的正文开头词：遇到就停（Please see… / The session… / 现场提供… / 主讲人简介…）
_PLACE_STOP = re.compile(r"(?:Please|Registration|Register|Language|The|This|Abstract|Curious|Speaker|Facilitator|Time|"
                         r"Date|Note|Details|For|Dear|If|主讲|嘉宾|现场|欢迎|语言|扫码|请|报名|详情|届时)")


def extract_place(text):
    """取第一个「地点：xxx」里的 xxx；没有返回 ""。
    正文换行在库里已被压成空格，所以靠下一个字段名（「语言：」）、括号收尾、正文开头词（Please…）来截断，最多 8 个词。"""
    text = clean_text(text)
    m = _PLACE_LABEL.search(text)
    if not m:
        return ""
    rest = text[m.end():m.end() + 120].lstrip()
    cut = _NEXT_FIELD.search(rest)
    if cut:
        rest = rest[:cut.start()]
    rest = re.split(r"[。；;]|\.\s|[，,]\s*(?:请|欢迎)", rest)[0]
    out = []
    for t in rest.split():
        if out and (_PLACE_STOP.match(t) or t[0] in "●·•"):
            break
        out.append(t)
        if t[-1] in ")）" or len(out) >= 8:      # 括号一般是地点的收尾：Faculty Club (AD402)
            break
    return " ".join(out)[:PLACE_MAX].strip(" ,，、.。:：-—")


# ---- 状态 ----
EVENT_DEFAULT_MINUTES = 60       # 活动没有结束时间，按开始后 1 小时处理


def status_of(dt, now=None):
    """截止：dt 为 None → none；早于现在 → expired；否则 open。"""
    if dt is None:
        return "none"
    return "expired" if dt < (now or datetime.now()) else "open"


def event_end_eff(start, end):
    """活动的有效结束时间：没有结束时间（或结束不晚于开始）按开始 + 1 小时。"""
    if end is not None and end > start:
        return end
    return start + timedelta(minutes=EVENT_DEFAULT_MINUTES)


def event_status(start, end, now=None):
    """活动：start 为 None → none；没开始 upcoming；进行中 ongoing；结束 ended。"""
    if start is None:
        return "none"
    now = now or datetime.now()
    if now < start:
        return "upcoming"
    return "ongoing" if now < event_end_eff(start, end) else "ended"
