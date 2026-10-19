# -*- coding: utf-8 -*-
"""课表（可选）：读数据目录里的 timetable.json，按日期展开成每节课，并和邮件里的活动算时间冲突。

文件不存在就当没有课表（通用 app，别报错）。文件只存在用户本机的数据目录，不在仓库里。
格式（宽松）：
  {"semester": {"start": "2026-09-01", "end": "2026-12-15",
                "exclude": [["2026-10-01","2026-10-07"], "2026-09-25"], "exclude_labels": ["国庆", "中秋"]},
   "courses": [{"name": "…", "code": "…", "teacher": "…",
                "slots": [{"weekday": 1, "start": "10:00", "end": "11:50", "room": "T29-505"}]}]}
weekday：1=周一 … 7=周日。exclude 是整天停课/放假的日期（含端点），exclude_labels 与 exclude 按顺序对应。
"""
import json
import re
from datetime import datetime, timedelta

from . import paths, store



def file_path():
    return paths.data_dir() / "timetable.json"


def _d(s):
    try:
        return datetime.strptime(str(s).strip()[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _hm(s):
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(s or ""))
    if not m or int(m.group(1)) > 24 or int(m.group(2)) > 59:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _fmt(mins):
    return "%02d:%02d" % (mins // 60, mins % 60)


def normalize(raw):
    """宽松解析；返回规范化的课表字典。"""
    sem = raw.get("semester") or {}
    labels = sem.get("exclude_labels") or []
    exclude = []
    for i, e in enumerate(sem.get("exclude") or []):
        a, b = (_d(e), _d(e)) if isinstance(e, str) else ((_d(e[0]), _d(e[-1])) if e else (None, None))
        if a and b and b >= a:
            label = labels[i] if i < len(labels) and labels[i] else "假期"
            exclude.append({"from": a, "to": b, "label": str(label)})
    courses = []
    for ci, c in enumerate(raw.get("courses") or []):
        slots = []
        for s in c.get("slots") or []:
            try:
                wd = int(s.get("weekday"))
            except (TypeError, ValueError):
                continue
            st, en = _hm(s.get("start")), _hm(s.get("end"))
            if not 1 <= wd <= 7 or st is None or en is None or en <= st:
                continue
            slots.append({"weekday": wd, "start": _fmt(st), "end": _fmt(en), "room": str(s.get("room") or "")})
        courses.append({"index": ci, "name": str(c.get("name") or c.get("code") or "课程%d" % (ci + 1)),
                        "code": str(c.get("code") or ""), "teacher": str(c.get("teacher") or ""), "slots": slots})
    return {"start": _d(sem.get("start")), "end": _d(sem.get("end")), "exclude": exclude, "courses": courses}


def load(path=None):
    """读课表。返回 (规范化课表或 None, 错误信息)。文件不存在：(None, "")。"""
    p = path or file_path()
    if not p.is_file():
        return None, ""
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("顶层应是对象")
        return normalize(raw), ""
    except (OSError, ValueError, TypeError, AttributeError, IndexError) as e:
        return None, "timetable.json 读不了（%s）" % e


def holiday_label(tt, d):
    for e in tt["exclude"]:
        if e["from"] <= d <= e["to"]:
            return e["label"]
    return None


def expand(tt, frm, to):
    """把 [frm, to]（date，含端点）内的每节课展开。返回 (occurrences, holidays)。
    学期范围外、exclude 里的日期不排课；exclude 的日期进 holidays（带名称）。"""
    occ, hol = [], []
    d = frm
    while d <= to:
        label = holiday_label(tt, d)
        if label:
            hol.append({"date": d.isoformat(), "label": label})
        elif (not tt["start"] or d >= tt["start"]) and (not tt["end"] or d <= tt["end"]):
            wd = d.isoweekday()
            for c in tt["courses"]:
                for s in c["slots"]:
                    if s["weekday"] == wd:
                        occ.append({"date": d.isoformat(), "weekday": wd, "start": s["start"], "end": s["end"],
                                    "room": s["room"], "course_index": c["index"], "name": c["name"],
                                    "code": c["code"], "teacher": c["teacher"]})
        d += timedelta(days=1)
    occ.sort(key=lambda o: (o["date"], o["start"], o["course_index"]))
    return occ, hol


def course_meta(tt):
    return [{"index": c["index"], "name": c["name"], "code": c["code"], "teacher": c["teacher"]} for c in tt["courses"]]


def overlap_minutes(a_start, a_end, b_start, b_end):
    """两个时间段（分钟数）的重叠分钟数，不重叠为 0。"""
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def classify_and_annotate(items, occurrences):
    """给日历条目加 conflicts（原地修改并返回）。条目的 kind 已由 store.calendar_items 定好：
    event=邮件里的活动（start→end_time 真实起止，没写结束时间按 1 小时）；due=截止（不占时间段）。
    冲突：有具体时间的 event 与同一天的课时间重叠，记重叠分钟数。"""
    by_day = {}
    for o in occurrences:
        by_day.setdefault(o["date"], []).append(o)
    for it in items:
        it["conflicts"] = []
        if it.get("kind") != "event" or not it.get("has_time"):
            continue
        s = _hm(str(it["start"])[11:16])
        e = _hm(it.get("end_time"))
        if s is None or e is None:
            continue
        for o in by_day.get(str(it["start"])[:10], []):
            ov = overlap_minutes(s, e, _hm(o["start"]), _hm(o["end"]))
            if ov > 0:
                it["conflicts"].append({"course_index": o["course_index"], "name": o["name"], "code": o["code"],
                                        "minutes": ov})
    return items


def payload(frm, to):
    """/api/timetable 的内容。"""
    tt, err = load()
    if not tt:
        return {"exists": False, "error": err, "courses": [], "occurrences": [], "holidays": []}
    occ, hol = expand(tt, frm, to)
    sem = {"start": tt["start"].isoformat() if tt["start"] else "", "end": tt["end"].isoformat() if tt["end"] else ""}
    return {"exists": True, "error": "", "semester": sem, "courses": course_meta(tt), "occurrences": occ, "holidays": hol}


# 去重时要剥掉的前缀（循环剥，大小写不敏感）：Reminder / 【Reminder】 / [Reminder] / 提醒： / Re: / Fwd: / 转发：
_PREFIX = re.compile(r"^\s*(?:\[\s*reminder\s*\]|【\s*reminder\s*】|reminder\b\s*[:：]?|提醒\s*[:：]|re\s*[:：]|fwd?\s*[:：]|转发\s*[:：])\s*",
                     re.IGNORECASE)
RECALLED = "发件人已撤回"


def normalize_subject(subject):
    """去掉 Reminder:/提醒：/Re:/Fwd:/转发： 等前缀和首尾空白（前缀可叠加），用来判断「同一个活动的重复邮件」。"""
    s = str(subject or "").strip()
    while True:
        t = _PREFIX.sub("", s, count=1).strip()
        if t == s:
            return s
        s = t


def week_filter(items):
    """周视图/议程用的默认过滤（永远生效，不受前端开关影响）：
    排除 系统自动通知 / 被忽略发件人命中的 / 主题以「发件人已撤回」开头的；
    同一天、同一类（活动/截止）、同一标准化主题只留一条：星级最高、时间最早（再按 uid）。"""
    best = {}
    for it in items:
        subj = str(it.get("subject") or "").strip()
        if it.get("cat") in ("automated", "ignored") or it.get("ignored"):
            continue
        if subj.startswith(RECALLED) or normalize_subject(subj).startswith(RECALLED):
            continue
        key = (str(it["start"])[:10], it.get("kind"), normalize_subject(subj).casefold())
        rank = (-(it.get("stars") or 0), str(it["start"]), it.get("uid") or 0)
        if key not in best or rank < best[key][0]:
            best[key] = (rank, it)
    out = [v[1] for v in best.values()]
    out.sort(key=lambda i: (str(i["start"]), i.get("uid") or 0))
    return out


def week_payload(start):
    """/api/week：一周（start 起 7 天）的课程、假期、邮件条目（含冲突）。
    条目已按 week_filter 去掉自动通知/忽略/撤回并去重；星级和过期筛选仍由前端按开关做。"""
    end = start + timedelta(days=6)
    tt = payload(start, end)
    items = week_filter(store.calendar_items(start.isoformat(), end.isoformat()))
    classify_and_annotate(items, tt["occurrences"])
    return {"start": start.isoformat(), "end": end.isoformat(), "timetable": tt, "items": items}
