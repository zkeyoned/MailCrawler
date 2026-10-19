# -*- coding: utf-8 -*-
"""课表：解析 / 按日期展开（学期范围、exclude 假期）/ 与邮件活动的冲突计算 / /api/timetable /api/week 接口。"""
import json
import sys
import tempfile
import threading
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from mailcrawler import config, secrets, server, store, timetable  # noqa: E402

TT = {
    "说明": "测试课表",
    "semester": {"start": "2026-09-01", "end": "2026-12-15",
                 "exclude": [["2026-10-01", "2026-10-07"], ["2026-10-25", "2026-10-31"], "2026-09-26"],
                 "exclude_labels": ["国庆", "Reading Week"]},
    "courses": [
        {"name": "常微分方程", "code": "MATH2043", "teacher": "Dr. W", "slots": [
            {"weekday": 1, "start": "10:00", "end": "11:50", "room": "T29-505"},
            {"weekday": 2, "start": "11:00", "end": "11:50", "room": "T4-504"}]},
        {"name": "数学分析", "code": "MATH2053", "teacher": "Dr. Z", "slots": [
            {"weekday": 3, "start": "9:00", "end": "10:50", "room": "T5-605"},
            {"weekday": 9, "start": "09:00", "end": "10:00", "room": "bad weekday"},
            {"weekday": 3, "start": "12:00", "end": "11:00", "room": "end before start"},
            {"weekday": "x", "start": "09:00", "end": "10:00"}]},
    ],
}


def tt():
    return timetable.normalize(TT)


def test_normalize_drops_bad_slots_and_labels():
    n = tt()
    assert [len(c["slots"]) for c in n["courses"]] == [2, 1]
    assert n["courses"][1]["slots"][0]["start"] == "09:00"               # 9:00 规范成 09:00
    assert [e["label"] for e in n["exclude"]] == ["国庆", "Reading Week", "假期"]   # 没写标签的用默认名
    assert n["start"] == date(2026, 9, 1) and n["end"] == date(2026, 12, 15)


def test_expand_regular_week():
    occ, hol = timetable.expand(tt(), date(2026, 10, 19), date(2026, 10, 25))
    assert hol == [{"date": "2026-10-25", "label": "Reading Week"}]           # 周日 10/25 起 Reading Week
    assert [(o["date"], o["start"], o["name"]) for o in occ] == [
        ("2026-10-19", "10:00", "常微分方程"), ("2026-10-20", "11:00", "常微分方程"), ("2026-10-21", "09:00", "数学分析")]
    assert occ[0]["room"] == "T29-505" and occ[0]["teacher"] == "Dr. W" and occ[0]["course_index"] == 0


def test_expand_excludes_holidays_and_semester_bounds():
    occ, hol = timetable.expand(tt(), date(2026, 9, 28), date(2026, 10, 8))
    days = {o["date"] for o in occ}
    assert days == {"2026-09-28", "2026-09-29", "2026-09-30"}                 # 周一二三有课；10/8 周四没课
    assert not any("2026-10-01" <= d <= "2026-10-07" for d in days)          # 国庆整周没课
    assert [h["date"] for h in hol] == ["2026-10-0%d" % i for i in range(1, 8)]
    assert all(h["label"] == "国庆" for h in hol)
    occ, _ = timetable.expand(tt(), date(2026, 8, 24), date(2026, 8, 31))     # 学期开始前
    assert occ == []
    occ, _ = timetable.expand(tt(), date(2026, 12, 16), date(2026, 12, 20))   # 学期结束后
    assert occ == []
    occ, hol = timetable.expand(tt(), date(2026, 9, 26), date(2026, 9, 26))   # 单个日期的 exclude
    assert occ == [] and hol == [{"date": "2026-09-26", "label": "假期"}]


def test_overlap_minutes():
    f = timetable.overlap_minutes
    assert f(600, 710, 660, 720) == 50 and f(600, 660, 660, 720) == 0 and f(600, 700, 610, 620) == 10 and f(0, 10, 20, 30) == 0


def items_for(*specs):
    out = []
    for uid, dl, has in specs:
        out.append({"uid": uid, "deadline": dl, "has_time": has})
    return out


def test_conflicts_and_kinds():
    occ, _ = timetable.expand(tt(), date(2026, 10, 19), date(2026, 10, 25))
    items = timetable.classify_and_annotate(items_for(
        (1, "2026-10-19 11:00", True),     # 课 10:00-11:50，活动 11:00-12:00 → 重叠 50
        (2, "2026-10-19 12:00", True),     # 课已结束 → 无冲突
        (3, "2026-10-19 09:30", True),     # 9:30-10:30 → 重叠 30
        (4, "2026-10-19 23:59", False),    # 当天截止
        (5, "2026-10-21 10:30", True),     # 数分 9:00-10:50，10:30-11:30 → 20
        (6, "2026-10-22 23:30", True),     # 跨午夜：只算到 24:00
    ), occ)
    by = {i["uid"]: i for i in items}
    assert by[1]["kind"] == "event" and by[1]["end_time"] == "12:00"
    assert by[1]["conflicts"] == [{"course_index": 0, "name": "常微分方程", "code": "MATH2043", "minutes": 50}]
    assert by[2]["conflicts"] == [] and by[3]["conflicts"][0]["minutes"] == 30
    assert by[4]["kind"] == "due" and by[4]["conflicts"] == [] and "end_time" not in by[4]
    assert by[5]["conflicts"][0]["name"] == "数学分析" and by[5]["conflicts"][0]["minutes"] == 20
    assert by[6]["end_time"] == "24:00"


def test_no_timetable_file_means_empty(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("MAILCRAWLER_DATA_DIR", d)
        assert timetable.load() == (None, "")
        p = timetable.payload(date(2026, 10, 19), date(2026, 10, 25))
        assert p["exists"] is False and p["occurrences"] == [] and p["error"] == ""
        (Path(d) / "timetable.json").write_text("{not json", encoding="utf-8")
        tt_, err = timetable.load()
        assert tt_ is None and "读不了" in err
        (Path(d) / "timetable.json").write_text("[1,2]", encoding="utf-8")
        assert timetable.load()[0] is None


@pytest.fixture()
def http(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("MAILCRAWLER_DATA_DIR", d)
        monkeypatch.setattr(config, "_cache", None)
        monkeypatch.setattr(secrets, "ready", lambda: True)
        store.init_db()
        (Path(d) / "timetable.json").write_text(json.dumps(TT, ensure_ascii=False), encoding="utf-8")
        for uid, subj, imp, dl in [(1, "与常微分冲突的讲座", 4, "2026-10-19 11:00"),
                                   (2, "作业提交", 5, "2026-10-21 23:59"),
                                   (3, "下周的事", 3, "2026-10-27 14:00")]:
            store.insert_emails([(uid, "2026-10-01 10:00:00", "a@b.c", subj, "正文", 0, "x")])
            store.save_triage(uid, "event", 0.9, 0.8, dl, "", 0.9, {}, importance=imp, imp_conf=0.9)
        srv, port = server.create_server()
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % port

        def call(path, headers=None):
            req = urllib.request.Request(base + path, headers=headers or {})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read().decode())
            except HTTPError as e:
                return e.code, json.loads(e.read().decode())
        yield call
        srv.shutdown()
        srv.server_close()
        config._cache = None


def test_api_timetable(http):
    code, r = http("/api/timetable?from=2026-10-19&to=2026-10-25")
    assert code == 200 and r["exists"] and len(r["occurrences"]) == 3 and r["holidays"][0]["label"] == "Reading Week"
    assert [c["name"] for c in r["courses"]] == ["常微分方程", "数学分析"] and r["semester"]["start"] == "2026-09-01"
    assert http("/api/timetable?from=2026-10-19&to=bad")[0] == 400
    assert http("/api/timetable?from=2026-10-25&to=2026-10-19")[0] == 400
    assert http("/api/timetable?from=2025-01-01&to=2027-12-31")[0] == 400
    assert http("/api/timetable?from=2026-10-19&to=2026-10-25", {"Host": "evil.com"})[0] == 403


def test_api_week(http):
    code, r = http("/api/week?start=2026-10-19")
    assert code == 200 and r["end"] == "2026-10-25"
    by = {i["uid"]: i for i in r["items"]}
    assert set(by) == {1, 2}                                        # 下周的 3 号不在这一周
    assert by[1]["kind"] == "event" and by[1]["conflicts"][0]["minutes"] == 50
    assert by[2]["kind"] == "due"
    assert len(r["timetable"]["occurrences"]) == 3
    assert http("/api/week?start=nope")[0] == 400
    assert http("/api/week?start=2026-10-19", {"Host": "evil.com"})[0] == 403


def test_api_week_without_timetable_file(http, monkeypatch):
    (timetable.file_path()).unlink()
    code, r = http("/api/week?start=2026-10-19")
    assert code == 200 and r["timetable"]["exists"] is False and r["timetable"]["occurrences"] == []
    assert {i["uid"] for i in r["items"]} == {1, 2}


def test_normalize_subject():
    n = timetable.normalize_subject
    assert n("Reminder: 数据结构讲座") == "数据结构讲座"
    assert n("  【Reminder】海洋讲座 ") == "海洋讲座"
    assert n("[Reminder] 海洋讲座") == "海洋讲座" and n("Reminder 海洋讲座") == "海洋讲座"
    assert n("提醒：海洋讲座") == "海洋讲座" and n("提醒: 海洋讲座") == "海洋讲座"
    assert n("RE: Re: 转发：Fwd: 海洋讲座") == "海洋讲座"       # 前缀可叠加
    assert n("Reminder: Re: 提醒：海洋讲座") == "海洋讲座"
    assert n("Reminders of things") == "Reminders of things"    # 不误伤以 Reminder 开头的单词
    assert n("Research seminar") == "Research seminar"


def _it(uid, subj, dl, stars, cat="event", ignored=False):
    return {"uid": uid, "subject": subj, "deadline": dl, "stars": stars, "cat": cat, "ignored": ignored}


def test_week_filter_exclusions_and_dedup():
    items = [
        _it(1, "AI 讲座", "2026-10-20 15:00", 4),
        _it(2, "Reminder: AI 讲座", "2026-10-20 10:00", 4),         # 同星级、时间更早 → 留这条
        _it(3, "提醒：AI 讲座", "2026-10-20 09:00", 3),              # 星级低 → 丢
        _it(4, "【Reminder】AI 讲座", "2026-10-21 15:00", 4),        # 另一天 → 各留一条
        _it(5, "发件人已撤回：某活动", "2026-10-20 16:00", 5),        # 撤回 → 永远排除
        _it(6, "Re: 发件人已撤回：某活动", "2026-10-20 16:00", 5),
        _it(7, "系统通知", "2026-10-20 12:00", 5, cat="automated"),
        _it(8, "被忽略发件人", "2026-10-20 12:30", 5, ignored=True),
        _it(9, "ignored 类别", "2026-10-20 12:40", 5, cat="ignored"),
        _it(10, "另一个活动", "2026-10-20 13:00", 2),
        _it(11, "Fwd: ai 讲座", "2026-10-20 08:00", 5),              # 大小写不敏感、星级最高 → 压过 2、1、3
    ]
    out = timetable.week_filter(items)
    assert [i["uid"] for i in out] == [11, 10, 4]
    # 同星级时取时间最早
    out = timetable.week_filter([_it(1, "AI 讲座", "2026-10-20 15:00", 4), _it(2, "Reminder: AI 讲座", "2026-10-20 10:00", 4)])
    assert [i["uid"] for i in out] == [2]
    # 没星级（None）排最后
    out = timetable.week_filter([_it(1, "X", "2026-10-20 10:00", None), _it(2, "Re: X", "2026-10-20 11:00", 1)])
    assert [i["uid"] for i in out] == [2]


def test_api_week_filters_and_dedups(http):
    for uid, subj, cat, imp, dl in [(10, "Reminder: 与常微分冲突的讲座", "event", 3, "2026-10-19 10:30"),
                                     (11, "发件人已撤回：讲座", "event", 5, "2026-10-20 10:00"),
                                     (12, "系统通知", "automated", 5, "2026-10-20 11:00")]:
        store.insert_emails([(uid, "2026-10-01 10:00:00", "a@b.c", subj, "正文", 0, "x")])
        store.save_triage(uid, cat, 0.9, 0.8, dl, "", 0.9, {}, importance=imp, imp_conf=0.9)
    code, w = http("/api/week?start=2026-10-19")
    assert code == 200
    assert [i["uid"] for i in w["items"]] == [1, 2]        # 10 是 1 的 Reminder 重复（星级低）、11 撤回、12 自动通知；3 在下一周
    assert w["items"][0]["conflicts"][0]["name"] == "常微分方程"
