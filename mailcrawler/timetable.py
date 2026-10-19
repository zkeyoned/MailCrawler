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

EVENT_MINUTES = 60          # 邮件活动没有结束时间时默认占 1 小时


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
    """给日历条目加 kind / end_time / conflicts（原地修改并返回）。
    kind：event=有具体时间（画成时间块，默认 1 小时）；due=没具体时间（23:59 或只有日期，当天截止）。
    冲突：event 与同一天的课时间重叠，记重叠分钟数。"""
    by_day = {}
    for o in occurrences:
        by_day.setdefault(o["date"], []).append(o)
    for it in items:
        dt = store.parse_dt(it["deadline"])
        it["conflicts"] = []
        if not it.get("has_time"):
            it["kind"] = "due"
            continue
        it["kind"] = "event"
        s = dt.hour * 60 + dt.minute
        e = min(s + EVENT_MINUTES, 24 * 60)
        it["end_time"] = "24:00" if e == 24 * 60 else _fmt(e)
        for o in by_day.get(dt.strftime("%Y-%m-%d"), []):
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


def week_payload(start):
    """/api/week：一周（start 起 7 天）的课程、假期、邮件条目（含冲突）。不做星级/过期筛选，由前端按开关筛。"""
    end = start + timedelta(days=6)
    tt = payload(start, end)
    items = store.calendar_items(start.isoformat(), end.isoformat())
    classify_and_annotate(items, tt["occurrences"])
    return {"start": start.isoformat(), "end": end.isoformat(), "timetable": tt, "items": items}
