# -*- coding: utf-8 -*-
"""重要程度 / 忽略名单 / 老库迁移 / 列表排序筛选 的回归测试（不联网，Jev 调用用假的）。

运行：python -m pytest tests/
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from mailcrawler import config, jev, store  # noqa: E402


@pytest.fixture()
def datadir(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("MAILCRAWLER_DATA_DIR", d)
        monkeypatch.setattr(config, "_cache", None)
        yield Path(d)
        config._cache = None


def test_categories_and_levels():
    assert len(jev.CATS) == 8 and "automated" in jev.CATS
    assert len(jev.IMPORTANCE_LEVELS) == 5
    q = jev.build_questions([], "")
    assert q["importance"]["type"] == "score" and q["importance"]["criteria"] == jev.IMPORTANCE_LEVELS
    assert set(store.CAT_ZH) >= set(jev.CATS)


def test_preferences_go_into_instructions():
    assert jev.importance_instructions("") == jev.IMPORTANCE_INSTR
    ins = jev.importance_instructions("  我喜欢 AI 实习 ")
    assert "我喜欢 AI 实习" in ins and ins.startswith(jev.IMPORTANCE_INSTR)
    assert len(jev.importance_instructions("字" * 10000)) < len(jev.IMPORTANCE_INSTR) + jev.PREF_MAX + 200


def test_ignored_by():
    sender = '" Red Cross " <redcross@example.edu>'
    assert jev.ignored_by(sender, "redcross") == "redcross"
    assert jev.ignored_by(sender, "\n  \nRED CROSS\n") == "red cross"
    assert jev.ignored_by(sender, "library\nfoo") is None
    assert jev.ignored_by(sender, "") is None and jev.ignored_by("", "x") is None


def test_stars_rounding():
    assert store.stars_of(None) is None
    assert [store.stars_of(x) for x in (1.0, 1.49, 1.5, 2.6, 4.5, 5.0, 7)] == [1, 1, 2, 3, 5, 5, 5]


def test_analyze_ignored_skips_jev(monkeypatch):
    monkeypatch.setattr(jev, "jev_call", lambda *a, **k: pytest.fail("被忽略的发件人不应调用 Jev"))
    r = jev.analyze({"date": "2026-10-01 10:00", "sender": "Club <club@x.edu>", "subject": "s", "body": "b"}, ignore_text="club@")
    assert r["ignored"] and r["importance"] == 1.0 and r["cat"] == "ignored"


def test_analyze_maps_score_to_stars(monkeypatch):
    seen = {}

    def fake(state, questions, key=None, count=True):
        seen["q"], seen["count"] = questions, count
        return {"cat": {"choice": "automated", "confidence": 0.9}, "action": {"noul": 0.1},
                "importance": {"score": 3.6, "confidence": 0.8}}, {"input_tokens": 10, "output_tokens": 2}
    monkeypatch.setattr(jev, "jev_call", fake)
    r = jev.analyze({"date": "2026-10-01 10:00", "sender": "a@x.edu", "subject": "s", "body": "b"}, prefs="偏好X", key="k", count=False)
    assert abs(r["importance"] - 4.6) < 1e-9 and r["cat"] == "automated" and not r["ignored"]
    assert "偏好X" in seen["q"]["importance"]["instructions"] and seen["count"] is False


def test_old_db_migrates_and_rescored(datadir):
    c = sqlite3.connect(str(store.paths.db_path()))
    c.executescript("""CREATE TABLE emails(uid INTEGER PRIMARY KEY, date TEXT, sender TEXT, subject TEXT, body TEXT,
                         seen INTEGER DEFAULT 0, synced_at TEXT, trashed INTEGER DEFAULT 0);
                       CREATE TABLE triage(uid INTEGER PRIMARY KEY, cat TEXT, cat_conf REAL, action REAL, deadline TEXT,
                         deadline_text TEXT, ddl_status TEXT, classified_at TEXT, ddl_conf REAL,
                         input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0);
                       INSERT INTO emails(uid,date,sender,subject,body) VALUES (1,'2026-10-01 09:00','a','old','x');
                       INSERT INTO triage(uid,cat,cat_conf,action) VALUES (1,'event',0.5,0.2);""")
    c.commit()
    c.close()
    store.init_db()
    store.init_db()   # 幂等
    # 老记录没有重要程度 → 仍算「待分类」
    got = store.next_unclassified(5, {}, {}, 60)
    assert [r["uid"] for r in got] == [1]
    assert store.list_emails("all", "", 10, 0)["items"][0]["stars"] is None


def _seed(rows):
    store.init_db()
    store.insert_emails([(u, "2026-10-%02d 09:00" % u, "s", "subj%d" % u, "b", 0, "") for u, _ in rows])
    for u, imp in rows:
        store.save_triage(u, "event", 0.5, 0.1, "", "", 0.0, {}, importance=imp, imp_conf=0.5)


def test_list_sort_and_filter(datadir):
    _seed([(1, 4.6), (2, 1.2), (3, 3.4), (4, 5.0), (5, 2.4)])
    uids = lambda **k: [i["uid"] for i in store.list_emails("all", "", 50, 0, **k)["items"]]
    assert uids() == [5, 4, 3, 2, 1]                                     # 默认按日期新→旧
    assert uids(sort="importance") == [4, 1, 3, 5, 2]
    assert uids(sort="importance", min_stars=4) == [4, 1]                  # 4.6 四舍五入 5 星、5.0 → 都 ≥4
    assert sorted(uids(max_stars=2)) == [2, 5]                             # 1.2→1 星、2.4→2 星
    assert uids(sort="importance", min_stars=3, max_stars=3) == [3]
    item = store.list_emails("all", "", 50, 0, sort="importance")["items"][0]
    assert item["stars"] == 5 and item["importance"] == 5.0
    assert store.stats(list(jev.CATS))["stars"][5] == 2


def test_config_roundtrip(datadir):
    assert config.load()["preferences"] == "" and config.load()["ignore_senders"] == ""
    config.save(preferences="偏好", ignore_senders="a\nb", bogus=1)
    config._cache = None
    c = config.load()
    assert c["preferences"] == "偏好" and c["ignore_senders"] == "a\nb" and "bogus" not in c
