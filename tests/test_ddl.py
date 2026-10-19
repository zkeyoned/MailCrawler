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


def rng(text):
    """候选的「开始–结束」，如 '2026-10-19 14:00–15:30'；只有开始时没有后半段。"""
    return ddl.read_as(one(text))


def test_time_ranges_separators():
    for sep in ("-", "–", "—", "~", "至", "到", " - ", " to "):
        assert rng("时间：2026年10月19日（星期一），14:00%s15:30" % sep) == "2026-10-19 14:00–15:30", sep


def test_key_example_lecture():
    c = one("时间：2026年10月19日（星期一），14:00-15:30　地点：大学会堂")
    assert str(c["dt"]) == "2026-10-19 14:00:00" and str(c["dt_end"]) == "2026-10-19 15:30:00" and c["has_end"]


def test_time_ranges_12h():
    assert rng("Oct 19 2:00-3:30 pm") == "2026-10-19 14:00–15:30"
    assert rng("Oct 19, 2-3:30pm") == "2026-10-19 14:00–15:30"
    assert rng("Oct 19 2:00 PM - 3:30 PM") == "2026-10-19 14:00–15:30"
    assert rng("Oct 19 9am-11am") == "2026-10-19 09:00–11:00"
    assert rng("Oct 19 11:00-1:00 pm") == "2026-10-19 11:00–13:00"      # 只在结尾写 pm 会让开始晚于结束 → 开始不借用


def test_time_ranges_bare_and_24h():
    assert rng("全国大学生数学建模竞赛培训讲座_10月20日_2:00-3:30_T7-104") == "2026-10-20 14:00–15:30"
    assert rng("10月19日 19:00-20:30") == "2026-10-19 19:00–20:30"
    assert rng("10月19日 18:30–20:00") == "2026-10-19 18:30–20:00"
    assert rng("10月19日 10:00-12:00") == "2026-10-19 10:00–12:00"


def test_time_ranges_chinese():
    assert rng("10月19日 下午2点到3点半") == "2026-10-19 14:00–15:30"
    assert rng("10月19日 下午两点至三点") == "2026-10-19 14:00–15:00"
    assert rng("10月19日 晚上7点到9点") == "2026-10-19 19:00–21:00"
    assert rng("10月19日 10-12点") == "2026-10-19 10:00–12:00"


def test_range_in_front_of_date():
    c = one("18:00-20:00, 16 Oct 2026")
    assert str(c["dt"]) == "2026-10-16 18:00:00" and str(c["dt_end"]) == "2026-10-16 20:00:00"


def test_single_time_has_no_end():
    c = one("请于10月19日 17:00前提交")
    assert str(c["dt"]) == "2026-10-19 17:00:00" and c["dt_end"] is None and not c["has_end"]
    c = one("10月19日 有一点难")                     # 「一点」不是 1:00
    assert not c["has_time"]


def test_range_not_inherited_by_next_date():
    cands = ddl.extract_candidates("10月19日 14:00-15:30，10月20日 提交", SENT)
    assert [ddl.read_as(c) for c in cands] == ["2026-10-19 14:00–15:30", "2026-10-20"]


def test_nbsp_entity_is_cleaned():
    assert rng("时间：&nbsp; &nbsp;&nbsp;10月20日，星期二，14:00-15:30 地点：") == "2026-10-20 14:00–15:30"


def test_extract_place():
    f = ddl.extract_place
    assert f("时间：2026年10月19日（星期一），14:00-15:30　地点：大学会堂　语言：中文") == "大学会堂"
    assert f("时间：10月19日 14:00 地点：大学会堂 语言：中文 请注意： 1、") == "大学会堂"
    assert f("地点：&nbsp; &nbsp; T7-104 嘉宾简介： 嘉宾1") == "T7-104"
    assert f("Venue: Room 101, Building A Language: English") == "Room 101, Building A"
    assert f("Location: Lecture Hall. Thanks") == "Lecture Hall"
    assert f("没有写地点这个字段，只有时间") == ""
    assert f("Location: T3-501-R5 Please prepare a few questions") == "T3-501-R5"
    assert f("Venue: Faculty Club (AD402) Registration: open from") == "Faculty Club (AD402)"
    assert f("Venue: T4-203, Staff and Student Activity Centre (SSAC) Curious about") == "T4-203, Staff and Student Activity Centre (SSAC)"
    assert f("地点：CC126 现场提供茶歇，欢迎各位师生") == "CC126"
    assert f("地点：BNBU一期校园 T29-604 主讲人简介 James") == "BNBU一期校园 T29-604"
    assert f("地点：Performance Stage ·周边领取与照片活动：") == "Performance Stage"


def test_event_status():
    st = datetime(2026, 10, 19, 14, 0)
    en = datetime(2026, 10, 19, 15, 30)
    f = ddl.event_status
    assert f(None, None) == "none"
    assert f(st, en, datetime(2026, 10, 19, 13, 59)) == "upcoming"
    assert f(st, en, datetime(2026, 10, 19, 14, 0)) == "ongoing"
    assert f(st, en, datetime(2026, 10, 19, 15, 19)) == "ongoing"          # Key 的截图：15:1x 还在进行
    assert f(st, en, datetime(2026, 10, 19, 15, 30)) == "ended"
    assert f(st, None, datetime(2026, 10, 19, 14, 59)) == "ongoing"        # 没有结束时间按 1 小时
    assert f(st, None, datetime(2026, 10, 19, 15, 0)) == "ended"


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
