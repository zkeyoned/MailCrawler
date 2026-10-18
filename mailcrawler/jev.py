# -*- coding: utf-8 -*-
"""Jev（TypeSafe）分类：把邮件主题+正文发给 api.typesafe.ai 做分类/是否要动手/截止日期选择。

隐私：邮件正文会发送到 TypeSafe（美国公司）的服务器，见登录页与 README 的隐私说明。
"""
import json
import re
import threading
import time
from urllib import error, request

from . import ddl, secrets, store
from .mailbox import ssl_ctx

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_STATE_BODY = 4000     # 送给 Jev 的正文长度
JEV_CONCURRENCY = 8
USD_PER_M_TOKENS = 0.042
DDL_MIN_CONF = 0.4        # 选日期这一题置信度低于它，按「无截止」处理
CLAIM_TTL = 180           # /api/next 占用时长（秒）

CATS = {
    "coursework": "Homework, quizzes, exams, grades, or course deadlines for a specific course",
    "academic_admin": "Registration, course selection, credit transfer, scholarships, fees, graduation, student status, forms the student must submit",
    "campus_life": "Dormitory, campus services, IT accounts, health, security, facilities",
    "event": "Talks, workshops, competitions, activities, clubs, volunteer opportunities",
    "career": "Internships, jobs, career fairs, recruitment",
    "newsletter": "Newsletters, mass announcements, promotions, subscriptions with no action needed",
    "personal": "Written personally to this student, expecting a reply",
}

JEV_SEM = threading.Semaphore(JEV_CONCURRENCY)
UID_LOCKS = {}
UID_LOCKS_GUARD = threading.Lock()
FAILS = {}          # uid -> 本次运行失败次数，失败 ≥2 次的 /api/next 不再发
CLAIMS = {}         # uid -> 过期时间戳，/api/next 占用
CLAIM_LOCK = threading.Lock()


def uid_lock(uid):
    with UID_LOCKS_GUARD:
        return UID_LOCKS.setdefault(uid, threading.Lock())


def jev_call(state, questions):
    """调用 Jev。并发上限 8；429/5xx/网络错误指数退避重试 3 次。返回 (answers, usage)。"""
    payload = json.dumps({"state": state, "model": "jev-latest", "questions": questions}).encode()
    key = secrets.jev_key()
    last = "unknown"
    with JEV_SEM:
        store.meta_add("sent_to_jev")     # 真实调用次数：每次真正发出的分类请求计 1 次（重试不重复计）
        for attempt in range(4):
            req = request.Request(JEV_URL, data=payload, headers={
                "Authorization": "Bearer " + key, "Content-Type": "application/json"})
            try:
                with request.urlopen(req, timeout=60, context=ssl_ctx()) as r:
                    j = json.load(r)
                return j["answers"], j.get("usage") or {}
            except error.HTTPError as e:
                last = "HTTP %d" % e.code
                if e.code == 429 or e.code >= 500:
                    if attempt < 3:
                        store.meta_add("jev_retries")
                        time.sleep(2 ** (attempt + 1))
                        continue
                break
            except Exception as e:  # 网络抖动、超时
                last = "%s: %s" % (type(e).__name__, e)
                if attempt < 3:
                    store.meta_add("jev_retries")
                    time.sleep(2 ** (attempt + 1))
                    continue
                break
    raise RuntimeError(last)


def classify(uid):
    """对一封邮件调用 Jev（已分类则直接返回缓存）。cat/action/ddl 同一次请求。"""
    with uid_lock(uid):
        cached = store.triage_row(uid)
        if cached:
            with CLAIM_LOCK:
                CLAIMS.pop(uid, None)
            return store.triage_public(cached)
        e = store.get_email(uid)
        if not e:
            raise KeyError("no such uid")
        e = dict(e)
        sent = store.parse_dt(e["date"] or "")
        subject, body = e["subject"] or "", e["body"] or ""
        cands = ddl.extract_candidates(subject + " ¶ " + body, sent)
        state = "Email received by a university student.\nDate: %s\nFrom: %s\nSubject: %s\n\n%s" % (
            e["date"], e["sender"], subject, body[:JEV_STATE_BODY])
        questions = {
            "cat": {"type": "choice", "instructions": "What kind of email is this", "criteria": CATS},
            "action": {"type": "noul",
                       "instructions": "The student must personally do something (submit, register, reply, pay, attend) because of this email"},
        }
        if cands:
            crit = {}
            for i, cd in enumerate(cands):
                crit["d%d" % i] = '"%s" (read as %s)' % (cd["ctx"], cd["dt"].strftime("%Y-%m-%d %H:%M") if cd["has_time"] else cd["date"].isoformat())
            crit["none"] = "No deadline or event date that matters to the student"
            questions["ddl"] = {"type": "choice", "instructions": "Which date is the deadline (or the event date) that matters to the student",
                                "criteria": crit}
        try:
            answers, usage = jev_call(state, questions)
            cat = answers["cat"]["choice"]
            cat_conf = float(answers["cat"].get("confidence") or 0)
            action = float(answers["action"]["noul"])
        except Exception:
            FAILS[uid] = FAILS.get(uid, 0) + 1
            store.meta_add("errors")
            raise
        deadline = deadline_text = ""
        ddl_conf = 0.0
        if cands and "ddl" in answers:
            ch = str(answers["ddl"].get("choice") or "none")
            ddl_conf = float(answers["ddl"].get("confidence") or 0)
            m = re.fullmatch(r"d(\d)", ch)
            # 置信度太低（Jev 自己都拿不准）就当没有截止，避免把不确定的日期误判成「已过期」后建议删除
            if m and int(m.group(1)) < len(cands) and ddl_conf >= DDL_MIN_CONF:
                cd = cands[int(m.group(1))]
                deadline = cd["dt"].strftime("%Y-%m-%d %H:%M")
                deadline_text = cd["raw"]
        store.save_triage(uid, cat, cat_conf, action, deadline, deadline_text, ddl_conf, usage)
        with CLAIM_LOCK:
            CLAIMS.pop(uid, None)
        r = store.triage_public(store.triage_row(uid))
        r["candidates"] = len(cands)
        return r


def next_unclassified(n):
    with CLAIM_LOCK:
        return store.next_unclassified(n, CLAIMS, FAILS, CLAIM_TTL)
