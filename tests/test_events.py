# -*- coding: utf-8 -*-
"""活动（event_*）与截止（deadline）分开：两道 Jev 题的解析、状态、视图规则、旧库迁移、日历条目、.ics。不联网。"""
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from mailcrawler import config, ics, jev, store  # noqa: E402

LECTURE = {"date": "2026-10-15 10:32", "sender": "x@y.edu", "subject": "形势与政策大讲堂",
           "body": "主讲嘉宾：某教授 时间：2026年10月19日（星期一），14:00-15:30　地点：大学会堂 语言：中文 请注意：必修"}


@pytest.fixture()
def datadir(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("MAILCRAWLER_DATA_DIR", d)
        monkeypatch.setattr(config, "_cache", None)
        store.init_db()
        yield Path(d)
        config._cache = None


def fake_jev(choices, seen=None):
    """choices: {'event_date': ('d0', 0.9), 'deadline_date': ('none', 0.9)}"""
    def f(state, questions, key=None, count=True):
        if seen is not None:
            seen["q"], seen["state"] = questions, state
        ans = {"cat": {"choice": "event", "confidence": 0.9}, "action": {"noul": 0.7},
               "importance": {"score": 2.0, "confidence": 0.8}}
        for k, (ch, cf) in choices.items():
            ans[k] = {"choice": ch, "confidence": cf}
        return ans, {"input_tokens": 100, "output_tokens": 5}
    return f


def test_two_date_questions_in_one_request(monkeypatch):
    seen = {}
    monkeypatch.setattr(jev, "jev_call", fake_jev({"event_date": ("d0", 0.9), "deadline_date": ("none", 0.9)}, seen))
    r = jev.analyze(LECTURE, key="k", count=False)
    q = seen["q"]
    assert "ddl" not in q and {"cat", "action", "importance", "event_date", "deadline_date"} <= set(q)
    assert q["event_date"]["type"] == "choice" and q["deadline_date"]["type"] == "choice"
    assert "itself takes place" in q["event_date"]["instructions"] and "deadline" in q["deadline_date"]["instructions"]
    assert set(q["event_date"]["criteria"]) == {"d0", "none"} == set(q["deadline_date"]["criteria"])
    assert "2026-10-19 14:00–15:30" in q["event_date"]["criteria"]["d0"]
    assert r["event_start"] == "2026-10-19 14:00" and r["event_end"] == "2026-10-19 15:30"
    assert r["event_place"] == "大学会堂" and r["event_text"].startswith("2026年10月19日")
    assert r["deadline"] == "" and r["event_conf"] == 0.9


def test_deadline_only_and_low_confidence(monkeypatch):
    mail = {"date": "2026-10-15 10:00", "sender": "a@b.c", "subject": "HW5", "body": "请于10月19日 23:59 前提交作业"}
    monkeypatch.setattr(jev, "jev_call", fake_jev({"event_date": ("none", 0.9), "deadline_date": ("d0", 0.95)}))
    r = jev.analyze(mail, key="k", count=False)
    assert r["deadline"] == "2026-10-19 23:59" and r["event_start"] == "" and r["event_place"] == ""
    # 置信度 < 0.4 当没有
    monkeypatch.setattr(jev, "jev_call", fake_jev({"event_date": ("d0", 0.3), "deadline_date": ("d0", 0.39)}))
    r = jev.analyze(mail, key="k", count=False)
    assert r["deadline"] == "" and r["event_start"] == ""


def test_same_candidate_for_both(monkeypatch):
    monkeypatch.setattr(jev, "jev_call", fake_jev({"event_date": ("d0", 0.6), "deadline_date": ("d0", 0.9)}))
    r = jev.analyze(LECTURE, key="k", count=False)          # 候选带时间段 → 当活动
    assert r["event_start"] == "2026-10-19 14:00" and r["deadline"] == ""
    mail = {"date": "2026-10-15 10:00", "sender": "a@b.c", "subject": "x", "body": "10月19日 17:00 前提交"}
    r = jev.analyze(mail, key="k", count=False)             # 没有时间段，截止置信度高 → 当截止
    assert r["deadline"] == "2026-10-19 17:00" and r["event_start"] == ""


def test_date_only_event_is_all_day(monkeypatch):
    mail = {"date": "2026-10-15 10:00", "sender": "a@b.c", "subject": "x", "body": "展览：10月20日 欢迎参观"}
    monkeypatch.setattr(jev, "jev_call", fake_jev({"event_date": ("d0", 0.9), "deadline_date": ("none", 0.9)}))
    r = jev.analyze(mail, key="k", count=False)
    assert r["event_start"] == "2026-10-20 00:00" and r["event_end"] == "2026-10-20 23:59"
    assert store.is_all_day(r["event_start"], r["event_end"])


def test_dates_only_mode(monkeypatch):
    seen = {}
    monkeypatch.setattr(jev, "jev_call", fake_jev({"event_date": ("d0", 0.9), "deadline_date": ("none", 0.9)}, seen))
    r = jev.analyze(LECTURE, key="k", count=False, dates_only=True)
    assert set(seen["q"]) == {"event_date", "deadline_date"} and "cat" not in r
    monkeypatch.setattr(jev, "jev_call", lambda *a, **k: pytest.fail("没有候选日期不该调用 Jev"))
    assert jev.analyze({"date": "2026-10-15", "sender": "a", "subject": "s", "body": "没有日期"}, dates_only=True)["candidates"] == 0


def test_old_db_gets_event_columns(datadir):
    p = store.paths.db_path()
    p.unlink()
    c = sqlite3.connect(str(p))
    c.executescript("""CREATE TABLE emails(uid INTEGER PRIMARY KEY, date TEXT, sender TEXT, subject TEXT, body TEXT,
                         seen INTEGER DEFAULT 0, synced_at TEXT, trashed INTEGER DEFAULT 0);
                       CREATE TABLE triage(uid INTEGER PRIMARY KEY, cat TEXT, cat_conf REAL, action REAL, deadline TEXT,
                         deadline_text TEXT, ddl_status TEXT, classified_at TEXT, ddl_conf REAL,
                         input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
                         importance REAL, imp_conf REAL, ignored INTEGER DEFAULT 0);
                       INSERT INTO emails(uid,date,sender,subject,body) VALUES (1,'2026-10-01 09:00','a','old','x');
                       INSERT INTO triage(uid,cat,cat_conf,action,deadline,importance) VALUES (1,'event',0.5,0.9,'2026-10-19 14:00',3);""")
    c.commit()
    c.close()
    store.init_db()
    store.init_db()
    cols = {r[1] for r in sqlite3.connect(str(p)).execute("PRAGMA table_info(triage)")}
    assert {"event_start", "event_end", "event_text", "event_place", "event_conf"} <= cols
    it = store.list_emails("all", "", 10, 0)["items"][0]
    assert it["event_start"] == "" and it["ev_status"] == "none" and it["deadline"] == "2026-10-19 14:00"


def seed(rows):
    """rows: (uid, subject, action, deadline, event_start, event_end)"""
    for uid, subj, act, dl, es, ee in rows:
        store.insert_emails([(uid, "2026-10-01 10:00:00", "a@b.c", subj, "正文", 0, "x")])
        store.save_triage(uid, "event", 0.9, act, dl, "", 0.9, {}, importance=3, imp_conf=0.9,
                          event_start=es, event_end=ee, event_place="大学会堂" if es else "")


def f(dt):
    return dt.strftime("%Y-%m-%d %H:%M")


def test_status_and_views(datadir):
    now = datetime.now().replace(second=0, microsecond=0)
    seed([
        (1, "进行中活动", 0.9, "", f(now - timedelta(minutes=30)), f(now + timedelta(minutes=30))),
        (2, "已结束活动", 0.9, "", f(now - timedelta(hours=3)), f(now - timedelta(hours=1))),
        (3, "没结束时间刚开始", 0.9, "", f(now - timedelta(minutes=20)), ""),
        (4, "没结束时间早已过", 0.9, "", f(now - timedelta(minutes=90)), ""),
        (5, "未来活动", 0.9, "", f(now + timedelta(days=1)), f(now + timedelta(days=1, hours=1))),
        (6, "过期截止", 0.9, f(now - timedelta(days=1)), "", ""),
        (7, "截止已过活动未到", 0.9, f(now - timedelta(days=1)), f(now + timedelta(days=2)), f(now + timedelta(days=2, hours=1))),
        (8, "截止未过活动已结束", 0.9, f(now + timedelta(days=1)), f(now - timedelta(hours=5)), f(now - timedelta(hours=4))),
        (9, "没有时间要动手", 0.9, "", "", ""),
        (10, "不用动手的未来活动", 0.1, "", f(now + timedelta(days=3)), ""),
    ])
    by = {i["uid"]: i for i in store.list_emails("all", "", 50, 0)["items"]}
    assert by[1]["ev_status"] == "ongoing" and by[2]["ev_status"] == "ended"
    assert by[3]["ev_status"] == "ongoing" and by[4]["ev_status"] == "ended"
    assert by[5]["ev_status"] == "upcoming" and by[6]["ddl_status"] == "expired" and by[9]["ev_status"] == "none"
    assert by[1]["event_place"] == "大学会堂" and by[1]["finished"] is False and by[2]["finished"] is True
    exp = {i["uid"] for i in store.list_emails("expired", "", 50, 0)["items"]}
    assert exp == {2, 4, 6}                      # 7（活动没到）、8（截止没过）还没完
    todo = {i["uid"] for i in store.list_emails("todo", "", 50, 0)["items"]}
    assert todo == {1, 3, 5, 7, 8, 9}            # 要动手≥0.6 且 没有全部过期（没有时间点的也算）
    s = store.stats(list(jev.CATS))
    assert s["expired"] == 3 and s["todo"] == 6


def test_calendar_items_event_and_due(datadir):
    now = datetime.now().replace(second=0, microsecond=0)
    d = lambda n, hm: (now + timedelta(days=n)).strftime("%Y-%m-%d ") + hm
    seed([(1, "讲座报名", 0.9, d(1, "23:59"), d(3, "14:00"), d(3, "15:30")),
          (2, "全天展览", 0.9, "", d(2, "00:00"), d(2, "23:59")),
          (3, "只有开始的活动", 0.9, "", d(2, "10:00"), "")])
    items = store.calendar_items(d(0, "")[:10], d(5, "")[:10])
    assert [(i["uid"], i["kind"]) for i in items] == [(1, "due"), (2, "event"), (3, "event"), (1, "event")]
    by = {(i["uid"], i["kind"]): i for i in items}
    e1 = by[(1, "event")]
    assert e1["start"] == d(3, "14:00") and e1["end_time"] == "15:30" and e1["has_time"] and not e1["end_default"]
    assert e1["event_place"] == "大学会堂" and e1["past"] is False
    assert by[(1, "due")]["start"] == d(1, "23:59") and by[(1, "due")]["has_time"] is False
    assert by[(2, "event")]["all_day"] is True and by[(2, "event")]["has_time"] is False
    assert by[(3, "event")]["end_time"] == "11:00" and by[(3, "event")]["end_default"] is True
    # 只取截止那天的范围 → 只有 due；只取活动那天 → 只有 event
    assert [(i["uid"], i["kind"]) for i in store.calendar_items(d(1, "")[:10], d(1, "")[:10])] == [(1, "due")]
    assert [(i["uid"], i["kind"]) for i in store.calendar_items(d(3, "")[:10], d(3, "")[:10])] == [(1, "event")]


def test_calendar_hide_expired_uses_event_end(datadir):
    now = datetime.now().replace(second=0, microsecond=0)
    seed([(1, "进行中", 0.9, "", f(now - timedelta(minutes=10)), f(now + timedelta(minutes=50))),
          (2, "已结束", 0.9, "", f(now - timedelta(hours=3)), f(now - timedelta(hours=2)))])
    day = now.strftime("%Y-%m-%d")
    got = store.calendar_items(day, day, hide_expired=True)
    assert [i["uid"] for i in got] == [1] and got[0]["past"] is False
    assert [i["uid"] for i in store.calendar_items(day, day)] == [2, 1]


def test_ics_event_and_due(datadir):
    seed([(1, "讲座", 0.9, "2026-10-17 23:59", "2026-10-19 14:00", "2026-10-19 15:30"),
          (2, "全天", 0.9, "", "2026-10-20 00:00", "2026-10-20 23:59"),
          (3, "无结束时间", 0.9, "", "2026-10-21 10:00", "")])
    text = ics.build(store.calendar_items("2026-10-01", "2026-10-31"))
    flat = text.replace("\r\n ", "")
    assert flat.count("BEGIN:VEVENT") == 4
    assert "DTSTART;TZID=Asia/Shanghai:20261019T140000" in flat and "DTEND;TZID=Asia/Shanghai:20261019T153000" in flat
    assert "LOCATION:大学会堂" in flat and "UID:mailcrawler-1-event@local" in flat
    assert "DTSTART;VALUE=DATE:20261017" in flat and "SUMMARY:截止：讲座" in flat and "UID:mailcrawler-1@local" in flat
    assert "DTSTART;VALUE=DATE:20261020" in flat and "DTEND;VALUE=DATE:20261021" in flat
    assert "DTSTART;TZID=Asia/Shanghai:20261021T100000" in flat and "DTEND;TZID=Asia/Shanghai:20261021T110000" in flat


def test_backfill_script_updates_only_time_fields(datadir, monkeypatch, capsys):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import backfill_times
    store.insert_emails([(1, "2026-10-15 10:32:00", "x@y.edu", LECTURE["subject"], LECTURE["body"], 0, ""),
                         (2, "2026-10-15 10:32:00", "x@y.edu", "没日期", "没有任何日期", 0, ""),
                         (3, "2026-10-15 10:32:00", "x@y.edu", "被忽略", "10月19日 14:00-15:30", 0, "")])
    store.save_triage(1, "event", 0.9, 0.7, "2026-10-19 14:00", "旧", 0.9, {"input_tokens": 50}, importance=2.6, imp_conf=0.8)
    store.save_triage(2, "newsletter", 0.9, 0.1, "", "", 0.0, {}, importance=1.0, imp_conf=0.8)
    store.save_triage(3, "event", 0.9, 0.1, "", "", 0.0, {}, importance=1.0, imp_conf=0.8, ignored=True)
    monkeypatch.setattr(jev, "jev_call", fake_jev({"event_date": ("d0", 0.9), "deadline_date": ("none", 0.9)}))
    monkeypatch.setattr(backfill_times, "app_running", lambda: False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    monkeypatch.setattr(sys, "argv", ["backfill_times.py"])
    backfill_times.main()
    out = capsys.readouterr().out
    assert "回填 1 封" in out and "只有活动 1" in out
    r = store.triage_row(1)
    assert r["deadline"] == "" and r["event_start"] == "2026-10-19 14:00" and r["event_end"] == "2026-10-19 15:30"
    assert r["event_place"] == "大学会堂" and r["cat"] == "event" and r["importance"] == 2.6 and r["action"] == 0.7
    assert r["input_tokens"] == 150                     # 50 + 本次 100
    assert store.triage_row(3)["event_start"] == "" and store.triage_row(2)["event_start"] == ""
    # 应用在运行时拒绝执行
    monkeypatch.setattr(backfill_times, "app_running", lambda: True)
    with pytest.raises(SystemExit):
        backfill_times.main()
