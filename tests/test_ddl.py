# -*- coding: utf-8 -*-
"""ddl.py 的回归测试：裸时间推断、英文日期区间、既有行为。

运行：python -m pytest tests/test_ddl.py  或  python tests/test_ddl.py
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mailcrawler import ddl  # noqa: E402

SENT = datetime(2026, 10, 5, 9, 0)


def one(text):
    cands = ddl.extract_candidates(text, SENT)
    assert len(cands) == 1, "期望恰好 1 个候选，实际 %d：%r" % (len(cands), [(c["raw"], str(c["dt"])) for c in cands])
    return cands[0]


def test_cn_date_with_time():
    c = one("作业请于2026年10月16日 17:00前提交")
    assert str(c["dt"]) == "2026-10-16 17:00:00"


def test_bare_range_time_is_pm_not_am():
    # 「2:00-3:30」没写上下午：应推断为下午 14:00，不是凌晨 2:00
    c = one("Workshop on 16 Oct 2026, 2:00-3:30")
    assert str(c["dt"]) == "2026-10-16 14:00:00"


def test_bare_single_time_pm():
    c = one("请于 10月16日 6:00 前到场")
    assert str(c["dt"]) == "2026-10-16 18:00:00"


def test_bare_morning_time_unchanged():
    c = one("Meeting at 9:00 on 16 Oct 2026")
    assert str(c["dt"]) == "2026-10-16 09:00:00"


def test_explicit_am_not_bumped():
    c = one("Quiz 2:00 am, 16 Oct 2026")
    assert str(c["dt"]) == "2026-10-16 02:00:00"


def test_borrow_pm_from_range_end():
    c = one("Seminar 16 Oct 2026 2:00-6:00 pm")
    assert str(c["dt"]) == "2026-10-16 14:00:00"


def test_en_range_day_month():
    c = one("Lectures 16-18 Oct 2026")
    assert str(c["dt"]) == "2026-10-18 23:59:00"


def test_en_range_month_day():
    c = one("October 16-18, 2026, registration required")
    assert str(c["dt"]) == "2026-10-18 23:59:00"


def test_en_range_with_ordinals():
    c = one("16th–18th October")
    assert str(c["dt"]) == "2026-10-18 23:59:00"


def test_en_range_does_not_double_count():
    # 区间只算一次，末日的单独匹配不能再出现
    cands = ddl.extract_candidates("16-18 Oct 2026", SENT)
    assert len(cands) == 1 and str(cands[0]["dt"]).startswith("2026-10-18")


def test_time_before_date():
    c = one("18:00-20:00, 16 Oct 2026")
    assert str(c["dt"]) == "2026-10-16 18:00:00"


def test_year_filling_near_sent():
    c = one("ddl 10月16日")
    assert c["date"] == datetime(2026, 10, 16).date()


def test_year_roll_next_year():
    # 12 月的邮件提 1 月 10 日 → 应落到下一年
    sent = datetime(2026, 12, 20)
    c = one("Jan 10 提交")
    cands = ddl.extract_candidates("Jan 10 提交", sent)
    assert cands[0]["date"].year == 2027


def test_dmy_vs_mdy():
    c = one("截止 16/10/2026")
    assert str(c["dt"]).startswith("2026-10-16")


def test_status():
    assert ddl.status_of(None) == "none"
    assert ddl.status_of(datetime(2026, 1, 1), now=SENT) == "expired"
    assert ddl.status_of(datetime(2027, 1, 1), now=SENT) == "open"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("ok  %s" % fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL %s: %s" % (fn.__name__, e))
    print("%d/%d passed" % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)
