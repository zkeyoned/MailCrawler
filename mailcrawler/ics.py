# -*- coding: utf-8 -*-
"""把日历条目导出成 .ics（iCalendar，RFC 5545）。时区 Asia/Shanghai；没具体时间的做全天事件。"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import store

TZ = "Asia/Shanghai"
EVENT_MINUTES = 60

# 中国没有夏令时，固定 +0800
VTIMEZONE = [
    "BEGIN:VTIMEZONE", "TZID:" + TZ,
    "BEGIN:STANDARD", "DTSTART:19700101T000000", "TZOFFSETFROM:+0800", "TZOFFSETTO:+0800", "TZNAME:CST", "END:STANDARD",
    "END:VTIMEZONE",
]


def esc(s):
    """TEXT 值转义：反斜杠、分号、逗号、换行。"""
    s = (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return s.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")


def fold(line):
    """按 75 字节折行（续行以一个空格开头），不切断 UTF-8 字符。"""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return [line]
    out, cur, size, limit = [], "", 0, 75
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > limit:
            out.append(cur)
            cur, size, limit = " ", 1, 75
        cur += ch
        size += n
    out.append(cur)
    return out


def build(items, now=None):
    """items: store.calendar_items 的返回。返回 .ics 文本（CRLF 换行）。"""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//MailCrawler//Calendar//ZH", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:邮件爬虫", "X-WR-TIMEZONE:" + TZ] + VTIMEZONE
    for it in items:
        dt = store.parse_dt(it["deadline"])
        if dt is None:
            continue
        stars = it.get("stars")
        desc = "类别：%s" % (it.get("cat_zh") or it.get("cat") or "未分类")
        desc += "\n重要程度：%s" % (("★" * stars + "☆" * (5 - stars) + "（%d 星）" % stars) if stars else "未评")
        if it.get("deadline_text"):
            desc += "\n邮件原文：%s" % it["deadline_text"]
        ev = ["BEGIN:VEVENT", "UID:mailcrawler-%s@local" % it["uid"], "DTSTAMP:" + stamp]
        if it.get("has_time"):
            end = dt + timedelta(minutes=EVENT_MINUTES)
            ev += ["DTSTART;TZID=%s:%s" % (TZ, dt.strftime("%Y%m%dT%H%M%S")),
                   "DTEND;TZID=%s:%s" % (TZ, end.strftime("%Y%m%dT%H%M%S"))]
        else:
            ev += ["DTSTART;VALUE=DATE:" + dt.strftime("%Y%m%d"),
                   "DTEND;VALUE=DATE:" + (dt + timedelta(days=1)).strftime("%Y%m%d")]
        ev += ["SUMMARY:" + esc(it.get("subject") or "(无主题)"), "DESCRIPTION:" + esc(desc), "END:VEVENT"]
        lines += ev
    lines.append("END:VCALENDAR")
    folded = []
    for ln in lines:
        folded += fold(ln)
    return "\r\n".join(folded) + "\r\n"


def downloads_dir():
    """导出目录：用户「下载」文件夹；环境变量 MAILCRAWLER_EXPORT_DIR 可覆盖（测试用）。"""
    override = os.environ.get("MAILCRAWLER_EXPORT_DIR")
    d = Path(override) if override else Path.home() / "Downloads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def export_file(items):
    """写文件，返回 (绝对路径, 文件名)。"""
    name = "邮件爬虫-日历-%s.ics" % datetime.now().strftime("%Y%m%d-%H%M%S")
    p = downloads_dir() / name
    n = 1
    while p.exists():
        n += 1
        p = p.with_name("邮件爬虫-日历-%s-%d.ics" % (datetime.now().strftime("%Y%m%d-%H%M%S"), n))
    p.write_bytes(build(items).encode("utf-8"))
    return str(p), p.name
