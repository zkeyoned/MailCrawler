# -*- coding: utf-8 -*-
"""SQLite 存储层：邮件、分类结果、计数元数据。"""
import sqlite3
import threading
from datetime import datetime
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
    importance REAL, imp_conf REAL, ignored INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_emails_date ON emails(date);
"""


def db():
    c = sqlite3.connect(str(paths.db_path()), timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


# 老版本建的库没有这几列：打开时补上（importance 为 NULL 的老记录会被当成「还没打重要程度」重新送去判断）
MIGRATE_TRIAGE = (("importance", "REAL"), ("imp_conf", "REAL"), ("ignored", "INTEGER DEFAULT 0"))


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


def triage_public(r):
    """给前端的 triage 字段；ddl_status 按「现在」重新算，避免缓存里的状态过期。"""
    dl = r.get("deadline") or ""
    st = ddl.status_of(parse_dt(dl)) if dl else "none"
    return {"uid": r["uid"], "cat": r["cat"], "cat_zh": CAT_ZH.get(r["cat"], r["cat"]), "cat_conf": r["cat_conf"],
            "action": r["action"], "deadline": dl, "deadline_text": r.get("deadline_text") or "", "ddl_status": st,
            "classified_at": r.get("classified_at"), "importance": r.get("importance"), "stars": stars_of(r.get("importance")),
            "imp_conf": r.get("imp_conf"), "ignored": bool(r.get("ignored"))}


def save_triage(uid, cat, cat_conf, action, deadline, deadline_text, ddl_conf, usage,
                importance=None, imp_conf=None, ignored=False):
    status = ddl.status_of(parse_dt(deadline)) if deadline else "none"
    with DB_LOCK, db() as c:
        c.execute("""INSERT OR REPLACE INTO triage(uid,cat,cat_conf,action,deadline,deadline_text,ddl_status,
                     classified_at,ddl_conf,input_tokens,output_tokens,importance,imp_conf,ignored)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (uid, cat, cat_conf, action, deadline, deadline_text, status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   ddl_conf, int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0),
                   importance, imp_conf, 1 if ignored else 0))


# ------------------------------------------------------------------ 查询
def view_where(view, q, now):
    """返回 (WHERE 子句, 参数)。基表为 emails e LEFT JOIN triage t。"""
    w, p = ["e.trashed=0"], []
    expired = "(COALESCE(t.deadline,'')<>'' AND t.deadline < ?)"
    if view == "todo":
        w.append("t.uid IS NOT NULL AND t.action>=0.6 AND NOT " + expired)
        p.append(now)
    elif view == "expired":
        w.append(expired)
        p.append(now)
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
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
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
                         "t.cat,t.cat_conf,t.action,t.deadline,t.deadline_text,t.ddl_status,t.importance,t.imp_conf,t.ignored "
                         + sql_from + " ORDER BY " + order + " LIMIT ? OFFSET ?", p + [limit, offset]).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        if d["cat"] is None:
            d["classified"] = False
            d["ddl_status"] = None
        else:
            d["classified"] = True
            d["cat_zh"] = CAT_ZH.get(d["cat"], d["cat"])
            d["ddl_status"] = ddl.status_of(parse_dt(d["deadline"])) if d["deadline"] else "none"
        d["stars"] = stars_of(d["importance"])
        d["ignored"] = bool(d["ignored"])
        items.append(d)
    return {"total": total, "items": items}


def get_email_full(uid):
    with DB_LOCK, db() as c:
        r = c.execute("SELECT e.*, t.cat,t.cat_conf,t.action,t.deadline,t.deadline_text,t.importance,t.imp_conf,t.ignored FROM emails e "
                      "LEFT JOIN triage t ON e.uid=t.uid WHERE e.uid=?", (uid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["stars"] = stars_of(d.get("importance"))
    d["ignored"] = bool(d.get("ignored"))
    if d["cat"] is not None:
        d["cat_zh"] = CAT_ZH.get(d["cat"], d["cat"])
        d["ddl_status"] = ddl.status_of(parse_dt(d["deadline"])) if d["deadline"] else "none"
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
        todo = c.execute("SELECT COUNT(*) FROM emails e JOIN triage t ON e.uid=t.uid WHERE e.trashed=0 AND t.action>=0.6 "
                         "AND NOT (COALESCE(t.deadline,'')<>'' AND t.deadline<?)", (now,)).fetchone()[0]
        expired = c.execute("SELECT COUNT(*) FROM emails e JOIN triage t ON e.uid=t.uid WHERE e.trashed=0 "
                            "AND COALESCE(t.deadline,'')<>'' AND t.deadline<?", (now,)).fetchone()[0]
        toks = c.execute("SELECT COALESCE(SUM(input_tokens),0) FROM triage").fetchone()[0]
        stars = {k: 0 for k in range(1, 6)}
        for r in c.execute("SELECT t.importance FROM emails e JOIN triage t ON e.uid=t.uid "
                           "WHERE e.trashed=0 AND t.importance IS NOT NULL"):
            stars[stars_of(r[0])] += 1
    return {"total": total, "stars": stars, "classified": classified, "unclassified": total - classified, "cats": cats,
            "cat_zh": CAT_ZH, "todo": todo, "expired": expired, "input_tokens": toks}
