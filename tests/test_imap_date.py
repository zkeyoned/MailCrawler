import locale
from mailcrawler import mailbox


def test_imap_date_is_english_regardless_of_locale():
    assert mailbox._imap_date("2026-10-09") == "09-Oct-2026"
    assert mailbox._imap_date("2026-01-02") == "02-Jan-2026"
    for loc in ("zh_CN.UTF-8", "ja_JP.UTF-8"):
        try:
            locale.setlocale(locale.LC_TIME, loc)
        except locale.Error:
            continue
        assert mailbox._imap_date("2026-10-09") == "09-Oct-2026"
    locale.setlocale(locale.LC_TIME, "C")
