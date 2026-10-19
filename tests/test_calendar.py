# -*- coding: utf-8 -*-
"""日历视图：store.calendar_items / ics 生成与导出 / HTTP 接口（临时数据目录，不碰真实数据库）。"""
import json
import sys
import tempfile
import threading
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from mailcrawler import config, ics, secrets, server, store  # noqa: E402


def fmt(dt, with_time=True):
    return dt.strftime("%Y-%m-%d %H:%M") if with_time else dt.strftime("%Y-%m-%d 23:59")


@pytest.fixture()
def env(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("MAILCRAWLER_DATA_DIR", d)
        monkeypatch.setenv("MAILCRAWLER_EXPORT_DIR", str(Path(d) / "dl"))
        monkeypatch.setattr(config, "_cache", None)
        monkeypatch.setattr(secrets, "ready", lambda: True)
        store.init_db()
        now = datetime.now().replace(second=0, microsecond=0)
        rows = [  # uid, subject, cat, importance, deadline
            (1, "期末考试, 地点;T6", "coursework", 5, fmt(now + timedelta(days=2, hours=1))),
            (2, "讲座(全天)", "event", 3, fmt(now + timedelta(days=3), False)),
            (3, "已过期的作业", "coursework", 4, fmt(now - timedelta(days=2))),
            (4, "低优先级活动", "campus_life", 1.2, fmt(now + timedelta(days=5))),
            (5, "没有截止", "newsletter", 2, ""),
            (6, "很远的比赛", "event", 4, fmt(now + timedelta(days=60))),
            (7, "已删除的", "event", 5, fmt(now + timedelta(days=4))),
        ]
        for uid, subj, cat, imp, dl in rows:
            store.insert_emails([(uid, "2026-10-01 10:00:00", "a@b.c", subj, "正文", 0, "x")])
            store.save_triage(uid, cat, 0.9, 0.8, dl, "原文" + str(uid), 0.9, {}, importance=imp, imp_conf=0.9)
        store.mark_trashed([7])
        yield now
        config._cache = None


def day(now, n):
    return (now + timedelta(days=n)).strftime("%Y-%m-%d")


def test_calendar_items_range_and_filters(env):
    now = env
    items = store.calendar_items(day(now, -3), day(now, 10))
    assert [i["uid"] for i in items] == [3, 1, 2, 4]            # 按时间排序；无截止、已删除、超范围的不在
    by = {i["uid"]: i for i in items}
    assert by[3]["ddl_status"] == "expired" and by[1]["ddl_status"] == "open"
    assert by[1]["has_time"] is True and by[2]["has_time"] is False   # 23:59 当作没具体时间
    assert by[1]["stars"] == 5 and by[4]["stars"] == 1
    assert [i["uid"] for i in store.calendar_items(day(now, -3), day(now, 10), min_stars=3)] == [3, 1, 2]
    assert [i["uid"] for i in store.calendar_items(day(now, -3), day(now, 10), hide_expired=True)] == [1, 2, 4]
    assert [i["uid"] for i in store.calendar_items(day(now, 0), None)] == [1, 2, 4, 6]


def test_ics_build(env):
    now = env
    items = store.calendar_items(day(now, 0), None, hide_expired=True)
    text = ics.build(items)
    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n")
    assert text.count("BEGIN:VEVENT") == 4 == text.count("END:VEVENT")
    assert "TZID:Asia/Shanghai" in text
    assert "SUMMARY:截止：期末考试\\, 地点\\;T6" in text              # 逗号、分号被转义；截止条目带「截止：」前缀
    d1 = store.parse_dt(items[0]["deadline"])
    assert "DTSTART;TZID=Asia/Shanghai:" + d1.strftime("%Y%m%dT%H%M%S") in text
    assert "DTEND;TZID=Asia/Shanghai:" + d1.strftime("%Y%m%dT%H%M%S") in text     # 截止时刻 = 零长度事件
    d2 = store.parse_dt(items[1]["deadline"])
    assert "DTSTART;VALUE=DATE:" + d2.strftime("%Y%m%d") in text      # 全天
    assert "DTEND;VALUE=DATE:" + (d2 + timedelta(days=1)).strftime("%Y%m%d") in text
    assert "类别：课程作业考试" in text.replace("\r\n ", "") and "5 星" in text.replace("\r\n ", "")
    for line in text.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75


def test_ics_fold_keeps_utf8():
    long = "SUMMARY:" + "考" * 60
    parts = ics.fold(long)
    assert len(parts) > 1 and all(len(p.encode("utf-8")) <= 75 for p in parts)
    assert "".join([parts[0]] + [p[1:] for p in parts[1:]]) == long


@pytest.fixture()
def http(env):
    srv, port = server.create_server()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port

    def call(path, body=None, headers=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers=dict({"Content-Type": "application/json"}, **(headers or {})))
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read().decode())
        except HTTPError as e:
            return e.code, json.loads(e.read().decode())
    yield call, env
    srv.shutdown()
    srv.server_close()


def test_api_calendar(http):
    call, now = http
    code, r = call("/api/calendar?from=%s&to=%s" % (day(now, 0), day(now, 10)))
    assert code == 200 and r["total"] == 3 and r["items"][0]["subject"].startswith("期末考试")
    code, r = call("/api/calendar?from=%s&to=%s&min=4&hide_expired=1" % (day(now, -3), day(now, 10)))
    assert [i["uid"] for i in r["items"]] == [1]
    assert call("/api/calendar?from=bad&to=2026-01-01")[0] == 400
    assert call("/api/calendar?from=2026-02-30&to=2026-03-01")[0] == 400
    assert call("/api/calendar?from=2026-03-02&to=2026-03-01")[0] == 400
    assert call("/api/calendar?from=2026-01-01&to=2028-01-01")[0] == 400


def test_api_ics_export_and_guard(http):
    call, now = http
    code, r = call("/api/calendar/ics", {"min": 3})
    assert code == 200 and r["count"] == 3 and r["name"].endswith(".ics")   # 1、2、6（4 号 1 星被筛掉，3 号已过期）
    p = Path(r["path"])
    assert p.is_file() and p.parent.name == "dl"
    assert p.read_text(encoding="utf-8").count("BEGIN:VEVENT") == 3
    code, r = call("/api/calendar/ics", {})
    assert code == 200 and r["count"] == 4
    assert call("/api/calendar/ics", {"from": "xx"})[0] == 400
    # Host / Origin 校验
    assert call("/api/calendar?from=2026-01-01&to=2026-01-31", headers={"Host": "evil.com"})[0] == 403
    assert call("/api/calendar/ics", {}, headers={"Origin": "http://evil.com"})[0] == 403
