# -*- coding: utf-8 -*-
"""SQLite 存储层：邮件、分类结果、计数元数据。"""
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

from . import ddl, paths

DB_LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails(
    uid INTEGER PRIMARY KEY, date TEXT, sender TEXT, subject TEXT, body TEXT,
    seen INTEGER DEFAULT 0, synced_at TEXT, trashed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS triage(
    uid INTEGER PRIMARY KEY, cat TEXT, cat_conf REAL, action REAL,
    deadline TEXT, deadline_text TEXT, ddl_status TEXT, classified_at TEXT,
    ddl_conf REAL, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
    importance REAL, imp_conf REAL, ignored INTEGER DEFAULT 0,
    event_start TEXT, event_end TEXT, event_text TEXT, event_place TEXT, event_conf REAL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_emails_date ON emails(date);
"""


def db():
    c = sqlite3.connect(str(paths.db_path()), timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


# 老版本建的库没有这几列：打开时补上（importance 为 NULL 的老记录会被当成「还没打重要程度」重新送去判断）
MIGRATE_TRIAGE = (("importance", "REAL"), ("imp_conf", "REAL"), ("ignored", "INTEGER DEFAULT 0"),
                  ("event_start", "TEXT"), ("event_end", "TEXT"), ("event_text", "TEXT"), ("event_place", "TEXT"),
                  ("event_conf", "REAL"))


def init_db():
    with DB_LOCK, db() as c:
        c.executescript(SCHEMA)
        have = {r[1] for r in c.execute("PRAGMA table_info(triage)")}
        for col, typ in MIGRATE_TRIAGE:
            if col not in have:
                c.execute("ALTER TABLE triage ADD COLUMN %s %s" % (col, typ))


def meta_add(key, n=1):
    with DB_LOCK, db() as c:
        c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=value+?",
                  (key, n, n))


def meta_get(key):
    with DB_LOCK, db() as c:
        r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else 0


def parse_dt(s):
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, f)
        except Exception:
            pass
    return None


def insert_emails(rows):
    """rows: [(uid,date,sender,subject,body,seen,synced_at)]，已存在的 UID 跳过。"""
    if not rows:
        return
    with DB_LOCK, db() as c:
        c.executemany("INSERT OR IGNORE INTO emails(uid,date,sender,subject,body,seen,synced_at) VALUES(?,?,?,?,?,?,?)", rows)


def known_uids(uids):
    if not uids:
        return set()
    with DB_LOCK, db() as c:
        return {r[0] for r in c.execute("SELECT uid FROM emails WHERE uid IN (%s)" % ",".join("?" * len(uids)), list(uids))}


def all_uids():
    with DB_LOCK, db() as c:
        return {r[0] for r in c.execute("SELECT uid FROM emails")}


def mark_seen(seen_set):
    with DB_LOCK, db() as c:
        c.execute("UPDATE emails SET seen=0")
        if seen_set:
            c.executemany("UPDATE emails SET seen=1 WHERE uid=?", [(u,) for u in seen_set])


def mark_trashed(uids):
    if not uids:
        return
    with DB_LOCK, db() as c:
        c.executemany("UPDATE emails SET trashed=1 WHERE uid=?", [(u,) for u in uids])


def get_email(uid):
    with DB_LOCK, db() as c:
        return c.execute("SELECT * FROM emails WHERE uid=?", (uid,)).fetchone()


# ------------------------------------------------------------------ 分类结果
CAT_ZH = {"coursework": "课程作业考试", "academic_admin": "教务行政", "campus_life": "校园生活与服务",
          "event": "讲座活动比赛", "career": "实习招聘", "newsletter": "群发通知与订阅", "personal": "写给你本人",
          "automated": "系统自动通知", "ignored": "已忽略（发件人规则）"}


def stars_of(importance):
    """重要程度（1~5 的小数）四舍五入成整数星级；没有则 None。"""
    if importance is None:
        return None
    return max(1, min(5, int(importance + 0.5)))


def triage_row(uid):
    with DB_LOCK, db() as c:
        r = c.execute("SELECT * FROM triage WHERE uid=?", (uid,)).fetchone()
    return dict(r) if r else None


def time_fields(r, now=None):
    """一行 triage（dict）里和时间相关的字段 + 现在的状态。
    活动 ev_status：upcoming/ongoing/ended/none；截止 ddl_status：open/expired/none。
    finished：该邮件所有已识别的时间点都过去了（有截止没过 / 有活动没结束 都算还没完）。"""
    now = now or datetime.now()
    dl = r.get("deadline") or ""
    es = r.get("event_start") or ""
    ee = r.get("event_end") or ""
    ddl_status = ddl.status_of(parse_dt(dl), now) if dl else "none"
    ev_status = ddl.event_status(parse_dt(es), parse_dt(ee), now) if es else "none"
    open_any = ddl_status == "open" or ev_status in ("upcoming", "ongoing")
    return {"deadline": dl, "deadline_text": r.get("deadline_text") or "", "ddl_status": ddl_status,
            "event_start": es, "event_end": ee, "event_text": r.get("event_text") or "",
            "event_place": r.get("event_place") or "", "ev_status": ev_status,
            "finished": (ddl_status != "none" or ev_status != "none") and not open_any}


def triage_public(r):
    """给前端的 triage 字段；各种状态按「现在」重新算，避免缓存里的状态过期。"""
    out = {"uid": r["uid"], "cat": r["cat"], "cat_zh": CAT_ZH.get(r["cat"], r["cat"]), "cat_conf": r["cat_conf"],
           "action": r["action"], "classified_at": r.get("classified_at"), "importance": r.get("importance"),
           "stars": stars_of(r.get("importance")), "imp_conf": r.get("imp_conf"), "ignored": bool(r.get("ignored"))}
    out.update(time_fields(r))
    return out


def save_triage(uid, cat, cat_conf, action, deadline, deadline_text, ddl_conf, usage,
                importance=None, imp_conf=None, ignored=False,
                event_start="", event_end="", event_text="", event_place="", event_conf=0.0):
    status = ddl.status_of(parse_dt(deadline)) if deadline else "none"
    with DB_LOCK, db() as c:
        c.execute("""INSERT OR REPLACE INTO triage(uid,cat,cat_conf,action,deadline,deadline_text,ddl_status,
                     classified_at,ddl_conf,input_tokens,output_tokens,importance,imp_conf,ignored,
                     event_start,event_end,event_text,event_place,event_conf)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (uid, cat, cat_conf, action, deadline, deadline_text, status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   ddl_conf, int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0),
                   importance, imp_conf, 1 if ignored else 0,
                   event_start or "", event_end or "", event_text or "", event_place or "", event_conf))


def update_times(uid, deadline, deadline_text, ddl_conf, event_start, event_end, event_text, event_place, event_conf,
                 usage=None):
    """只改时间相关字段（回填脚本用）；不动类别/星级。usage 的 token 累加到这封邮件上。"""
    status = ddl.status_of(parse_dt(deadline)) if deadline else "none"
    usage = usage or {}
    with DB_LOCK, db() as c:
        c.execute("""UPDATE triage SET deadline=?,deadline_text=?,ddl_status=?,ddl_conf=?,
                     event_start=?,event_end=?,event_text=?,event_place=?,event_conf=?,
                     input_tokens=COALESCE(input_tokens,0)+?, output_tokens=COALESCE(output_tokens,0)+? WHERE uid=?""",
                  (deadline or "", deadline_text or "", status, ddl_conf, event_start or "", event_end or "",
                   event_text or "", event_place or "", event_conf, int(usage.get("input_tokens") or 0),
                   int(usage.get("output_tokens") or 0), uid))


# ------------------------------------------------------------------ 查询
EV_END_SQL = "COALESCE(NULLIF(t.event_end,''), strftime('%Y-%m-%d %H:%M', t.event_start, '+1 hour'))"


def finished_sql(now):
    """SQL：这封邮件有时间点，并且全部过去了（截止已过；活动已结束）→ 已过期·可删。
    有截止没过，或有活动没结束，都还不算。基表别名 t（triage）。返回 (条件, 参数)。"""
    cond = ("((COALESCE(t.deadline,'')<>'' OR COALESCE(t.event_start,'')<>'')"
            " AND NOT (COALESCE(t.deadline,'')<>'' AND t.deadline >= ?)"
            " AND NOT (COALESCE(t.event_start,'')<>'' AND " + EV_END_SQL + " > ?))")
    return cond, [now, now]


def view_where(view, q, now):
    """返回 (WHERE 子句, 参数)。基表为 emails e LEFT JOIN triage t。"""
    w, p = ["e.trashed=0"], []
    fin, fp = finished_sql(now)
    if view == "todo":
        w.append("t.uid IS NOT NULL AND t.action>=0.6 AND NOT " + fin)
        p += fp
    elif view == "expired":
        w.append(fin)
        p += fp
    elif view.startswith("cat:"):
        w.append("t.cat=?")
        p.append(view[4:])
    if q:
        w.append("instr(lower(e.subject||' '||e.sender||' '||e.body), lower(?))>0")
        p.append(q)
    return " AND ".join(w), p


SORTS = {
    "date": "e.date DESC, e.uid DESC",
    # 重要程度从高到低；没打分的排最后；同分的新邮件在前
    "importance": "(t.importance IS NULL), t.importance DESC, e.date DESC, e.uid DESC",
}


def list_emails(view, q, limit, offset, sort="date", min_stars=None, max_stars=None):
    """sort: date | importance；min_stars / max_stars 按四舍五入后的星级筛选（含端点）。"""
    now_dt = datetime.now()
    now = now_dt.strftime("%Y-%m-%d %H:%M")
    where, p = view_where(view, q, now)
    if min_stars:
        where += " AND t.importance >= ?"
        p.append(int(min_stars) - 0.5)
    if max_stars:
        where += " AND t.importance < ?"
        p.append(int(max_stars) + 0.5)
    order = SORTS.get(sort, SORTS["date"])
    sql_from = "FROM emails e LEFT JOIN triage t ON e.uid=t.uid WHERE " + where
    with DB_LOCK, db() as c:
        total = c.execute("SELECT COUNT(*) " + sql_from, p).fetchone()[0]
        rows = c.execute("SELECT e.uid,e.date,e.sender,e.subject,substr(e.body,1,160) AS snippet,e.seen,"
                         "t.cat,t.cat_conf,t.action,t.deadline,t.deadline_text,t.ddl_status,t.importance,t.imp_conf,t.ignored,"
                         "t.event_start,t.event_end,t.event_text,t.event_place "
                         + sql_from + " ORDER BY " + order + " LIMIT ? OFFSET ?", p + [limit, offset]).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        if d["cat"] is None:
            d["classified"] = False
            d["ddl_status"] = None
            d["ev_status"] = None
        else:
            d["classified"] = True
            d["cat_zh"] = CAT_ZH.get(d["cat"], d["cat"])
            d.update(time_fields(d, now_dt))
        d["stars"] = stars_of(d["importance"])
        d["ignored"] = bool(d["ignored"])
        items.append(d)
    return {"total": total, "items": items}


def has_clock_time(deadline):
    """deadline 是否带具体时间。ddl.py 对只写了日期的候选统一补 23:59，这里当作「没具体时间」。"""
    dt = parse_dt(deadline or "")
    return bool(dt) and len(deadline) > 10 and not (dt.hour == 23 and dt.minute == 59)


def is_all_day(start, end):
    """只有日期没有时间的活动，存成 00:00 – 23:59。"""
    return str(start or "")[11:16] == "00:00" and str(end or "")[11:16] == "23:59"


def calendar_items(frm, to, min_stars=None, hide_expired=False, limit=2000):
    """日历用：截止或活动落在 [frm, to]（YYYY-MM-DD，含端点，to 为 None 表示不设上限）的邮件。
    一封邮件可以产生两条：kind='event'（活动，start/end 是活动起止）和 kind='due'（截止，start 是截止时间）。
    条目按 start 排序；状态按「现在」重新算；past=这一条已经过去（截止已过 / 活动已结束）。"""
    now = datetime.now()
    lim = max(1, min(int(limit), 5000))

    def rng(col):
        sql, p = "(COALESCE(%s,'')<>'' AND substr(%s,1,10)>=?" % (col, col), [frm]
        if to:
            sql += " AND substr(%s,1,10)<=?" % col
            p.append(to)
        return sql + ")", p
    dsql, dp = rng("t.deadline")
    esql, ep = rng("t.event_start")
    sql = ("SELECT e.uid,e.date,e.sender,e.subject,t.cat,t.action,t.deadline,t.deadline_text,t.event_start,t.event_end,"
           "t.event_text,t.event_place,t.importance,t.ignored FROM emails e JOIN triage t ON e.uid=t.uid "
           "WHERE e.trashed=0 AND (" + dsql + " OR " + esql + ") "
           "ORDER BY COALESCE(NULLIF(t.event_start,''), t.deadline), e.uid LIMIT ?")
    with DB_LOCK, db() as c:
        rows = c.execute(sql, dp + ep + [lim]).fetchall()
    out = []
    for r in rows:
        base = dict(r)
        base["ignored"] = bool(base.get("ignored"))
        base["stars"] = stars_of(base["importance"])
        base["cat_zh"] = CAT_ZH.get(base["cat"], base["cat"])
        tf = time_fields(base, now)
        base.update(tf)
        if min_stars and (base["stars"] is None or base["stars"] < min_stars):
            continue
        dl, es = tf["deadline"], tf["event_start"]
        if dl and frm <= dl[:10] and (not to or dl[:10] <= to):
            dt = parse_dt(dl)
            if dt is not None and not (hide_expired and tf["ddl_status"] == "expired"):
                d = dict(base, kind="due", start=dl, end="", has_time=has_clock_time(dl),
                         past=tf["ddl_status"] == "expired")
                out.append(d)
        if es and frm <= es[:10] and (not to or es[:10] <= to):
            st, en = parse_dt(es), parse_dt(tf["event_end"])
            if st is not None and not (hide_expired and tf["ev_status"] == "ended"):
                allday = is_all_day(es, tf["event_end"])
                eff = ddl.event_end_eff(st, en)
                end_time = "24:00" if eff.date() != st.date() else eff.strftime("%H:%M")   # 跨午夜的按当天 24:00 截断
                d = dict(base, kind="event", start=es, end=tf["event_end"], has_time=not allday, all_day=allday,
                         end_time=end_time, end_default=not (en and en > st), past=tf["ev_status"] == "ended")
                out.append(d)
    out.sort(key=lambda i: (i["start"], i["uid"], i["kind"]))
    return out[:lim]


def get_email_full(uid):
    with DB_LOCK, db() as c:
        r = c.execute("SELECT e.*, t.cat,t.cat_conf,t.action,t.deadline,t.deadline_text,t.importance,t.imp_conf,t.ignored,"
                      "t.event_start,t.event_end,t.event_text,t.event_place FROM emails e "
                      "LEFT JOIN triage t ON e.uid=t.uid WHERE e.uid=?", (uid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["stars"] = stars_of(d.get("importance"))
    d["ignored"] = bool(d.get("ignored"))
    if d["cat"] is not None:
        d["cat_zh"] = CAT_ZH.get(d["cat"], d["cat"])
        d.update(time_fields(d))
    return d


def next_unclassified(n, claims, fails, claim_ttl):
    """取 n 封未分类邮件（含正文）；已被别的爬虫占用的、多次失败的不发。
    老版本分过类、但还没有重要程度的邮件也算未分类（会被重新送去判断一次，补上星级）。"""
    n = max(1, min(n, 60))
    t = __import__("time").time()
    for u in [u for u, exp in list(claims.items()) if exp < t]:
        del claims[u]
    busy = set(claims) | {u for u, k in fails.items() if k >= 2}
    with DB_LOCK, db() as c:
        rows = c.execute("SELECT e.uid,e.date,e.sender,e.subject,e.body FROM emails e "
                         "LEFT JOIN triage t ON e.uid=t.uid WHERE e.trashed=0 AND (t.uid IS NULL OR t.importance IS NULL) "
                         "ORDER BY e.date DESC, e.uid DESC LIMIT ?", (n + len(busy) + 5,)).fetchall()
    out = []
    for r in rows:
        if r["uid"] in busy:
            continue
        claims[r["uid"]] = t + claim_ttl
        out.append(dict(r))
        if len(out) >= n:
            break
    return out


def stats(cats_keys):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with DB_LOCK, db() as c:
        total = c.execute("SELECT COUNT(*) FROM emails WHERE trashed=0").fetchone()[0]
        cats = {k: 0 for k in cats_keys}
        for r in c.execute("SELECT t.cat,COUNT(*) n FROM emails e JOIN triage t ON e.uid=t.uid WHERE e.trashed=0 GROUP BY t.cat"):
            cats[r["cat"]] = r["n"]
        classified = sum(cats.values())
        fin, fp = finished_sql(now)
        todo = c.execute("SELECT COUNT(*) FROM emails e JOIN triage t ON e.uid=t.uid WHERE e.trashed=0 AND t.action>=0.6 "
                         "AND NOT " + fin, fp).fetchone()[0]
        expired = c.execute("SELECT COUNT(*) FROM emails e JOIN triage t ON e.uid=t.uid WHERE e.trashed=0 AND " + fin,
                            fp).fetchone()[0]
        toks = c.execute("SELECT COALESCE(SUM(input_tokens),0) FROM triage").fetchone()[0]
        stars = {k: 0 for k in range(1, 6)}
        for r in c.execute("SELECT t.importance FROM emails e JOIN triage t ON e.uid=t.uid "
                           "WHERE e.trashed=0 AND t.importance IS NOT NULL"):
            stars[stars_of(r[0])] += 1
    return {"total": total, "stars": stars, "classified": classified, "unclassified": total - classified, "cats": cats,
            "cat_zh": CAT_ZH, "todo": todo, "expired": expired, "input_tokens": toks}
