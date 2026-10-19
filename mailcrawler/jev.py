# -*- coding: utf-8 -*-
"""Jev（TypeSafe）分类：把邮件主题+正文发给 api.typesafe.ai 做分类/是否要动手/重要程度(1~5星)/截止日期选择。

隐私：邮件正文会发送到 TypeSafe（美国公司）的服务器，见登录页与 README 的隐私说明。
"""
import json
import re
import threading
import time
from urllib import error, request

from . import config, ddl, secrets, store
from .mailbox import ssl_ctx

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_STATE_BODY = 4000     # 送给 Jev 的正文长度
JEV_CONCURRENCY = 8
USD_PER_M_TOKENS = 0.042
DDL_MIN_CONF = 0.4        # 选日期这一题置信度低于它，按「无截止」处理
CLAIM_TTL = 180           # /api/next 占用时长（秒）

CATS = {
    "coursework": "Homework, quizzes, exams, grades, feedback or content changes for a specific course the student takes. "
                  "Not automatic receipts such as 'you have submitted your assignment' (those are system notices)",
    "academic_admin": "Registration, add/drop of courses, credit transfer, scholarships, fees, graduation, student status, "
                      "surveys or forms the student must submit",
    "campus_life": "Dormitory, campus facilities and services, IT accounts, health, security. Also student clubs, "
                   "student unions, orchestras, volunteering and interest-group recruitment or activities",
    "event": "One-off talks, lectures, seminars, workshops, competitions, exhibitions and information sessions. "
             "Not recruitment for clubs or student organizations (those are campus life)",
    "career": "Internships, jobs, career fairs, employer talks, research-assistant openings",
    "newsletter": "Newsletters, digests, mass announcements, promotions, subscriptions with no action needed",
    "personal": "Written personally to this student by a real person, expecting a reply",
    "automated": "Automatically generated system message: submission receipts and confirmations, login or security "
                 "alerts, verification codes, recalled-message notices, system maintenance notices. "
                 "Sent by software, not written by a person",
}

# 重要程度：5 级（score 题的 criteria，从低到高），每级是一个能直接对照的情形。
# 默认定义对所有人通用；个人口味写进设置里的「我的偏好」，会拼进这道题的 instructions。
IMPORTANCE_LEVELS = [
    "Not worth reading: an automatic receipt or confirmation, a login or security alert, a used verification code, "
    "a recalled message, a system maintenance notice, or a mass mailing about something that does not involve this student",
    "Optional: a general announcement, newsletter, or a talk, workshop, event, or club / student-organization / "
    "interest-group recruitment or activity that most students could skip; nothing is required of the student",
    "Worth a quick look: general information or an opportunity that may be useful (a talk, an info session, an exchange "
    "program), with no sign-up deadline and nothing the student has to do",
    "Needs handling soon: a change in a course or an assignment to follow up, or a sign-up, registration or application "
    "with a deadline (a competition, a project application, an internship or research opening, a mandatory procedure) "
    "that matters for the student's studies or career",
    "Must be handled right away: an exam or quiz, something about a required (compulsory) course or a graduation "
    "requirement, a mandatory procedure, or any deadline due within a day or two",
]
IMPORTANCE_INSTR = ("How much attention does this email deserve from this particular student? Judge by what the student "
                    "must or should actually do or know, not by how formal, urgent-sounding or official the wording is.")
PREF_MAX = 3000           # 「我的偏好」拼进 instructions 的最大字符数

JEV_SEM = threading.Semaphore(JEV_CONCURRENCY)
UID_LOCKS = {}
UID_LOCKS_GUARD = threading.Lock()
FAILS = {}          # uid -> 本次运行失败次数，失败 ≥2 次的 /api/next 不再发
CLAIMS = {}         # uid -> 过期时间戳，/api/next 占用
CLAIM_LOCK = threading.Lock()


def uid_lock(uid):
    with UID_LOCKS_GUARD:
        return UID_LOCKS.setdefault(uid, threading.Lock())


def jev_call(state, questions, key=None, count=True):
    """调用 Jev。并发上限 8；429/5xx/网络错误指数退避重试 3 次。返回 (answers, usage)。

    key 不传则从系统钥匙串取；count=False 时不写本机计数（评估脚本用）。
    """
    payload = json.dumps({"state": state, "model": "jev-latest", "questions": questions}).encode()
    key = key or secrets.jev_key()
    last = "unknown"
    with JEV_SEM:
        if count:
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
                        if count:
                            store.meta_add("jev_retries")
                        time.sleep(2 ** (attempt + 1))
                        continue
                break
            except Exception as e:  # 网络抖动、超时
                last = "%s: %s" % (type(e).__name__, e)
                if attempt < 3:
                    if count:
                        store.meta_add("jev_retries")
                    time.sleep(2 ** (attempt + 1))
                    continue
                break
    raise RuntimeError(last)


def ignored_by(sender, ignore_text):
    """「总是忽略的发件人」：每行一个关键字，不分大小写，命中发件人（名字或地址）任意位置即忽略。
    返回命中的关键字，没命中返回 None。"""
    s = (sender or "").lower()
    for line in (ignore_text or "").splitlines():
        kw = line.strip().lower()
        if kw and kw in s:
            return kw
    return None


def importance_instructions(prefs):
    """重要程度那道题的 instructions：通用说明 + 用户自己的偏好（可空）。"""
    prefs = (prefs or "").strip()[:PREF_MAX]
    if not prefs:
        return IMPORTANCE_INSTR
    return (IMPORTANCE_INSTR + "\n\nThis student's own preferences (they take priority over the general guidance "
            "whenever they apply):\n" + prefs)


def date_questions(cands):
    """有日期候选时的两道选择题（同一次请求里问）：活动本身是哪天 / 学生要在哪天前动手。"""
    crit = {}
    for i, cd in enumerate(cands):
        crit["d%d" % i] = '"%s" (read as %s)' % (cd["ctx"], ddl.read_as(cd))
    return {
        "event_date": {"type": "choice",
                       "instructions": "Which candidate is when the event/lecture/session itself takes place",
                       "criteria": dict(crit, none="None of these is when an event, lecture or session takes place")},
        "deadline_date": {"type": "choice",
                          "instructions": "Which candidate is the deadline by which the student must act "
                                          "(submit, register, apply, pay, reply)",
                          "criteria": dict(crit, none="None of these is a deadline the student must act by")},
    }


def build_questions(cands, prefs="", dates_only=False):
    """一封邮件对应的全部问题：类别、要不要动手、重要程度，有日期候选时再加「活动是哪天」「截止是哪天」两题。
    dates_only=True 只问日期那两题（回填时用）。"""
    questions = {}
    if not dates_only:
        questions = {
            "cat": {"type": "choice", "instructions": "What kind of email is this", "criteria": CATS},
            "action": {"type": "noul",
                       "instructions": "The student must personally do something (submit, register, reply, pay, attend) because of this email"},
            "importance": {"type": "score", "instructions": importance_instructions(prefs), "criteria": IMPORTANCE_LEVELS},
        }
    if cands:
        questions.update(date_questions(cands))
    return questions


def pick_candidate(answers, name, cands):
    """从 answers[name] 里取选中的候选（置信度太低、选了 none、缺答案都当没有）。返回 (候选或 None, 置信度)。"""
    a = answers.get(name)
    if not cands or not a:
        return None, 0.0
    conf = float(a.get("confidence") or 0)
    m = re.fullmatch(r"d(\d)", str(a.get("choice") or "none"))
    # 置信度太低（Jev 自己都拿不准）就当没有，避免把不确定的日期误判成「已过期」后建议删除
    if m and int(m.group(1)) < len(cands) and conf >= DDL_MIN_CONF:
        return cands[int(m.group(1))], conf
    return None, conf


def time_result(cands, answers, text):
    """把两道日期题的答案解析成 deadline / event 字段。text=主题+正文（取地点用）。

    活动：event_start/event_end 为 'YYYY-MM-DD HH:MM'；没写时间的活动存成当天 00:00–23:59（=全天）；只有开始时间时 event_end 为空。
    两题选了同一个候选时：它带时间段（14:00-15:30）就当活动；否则谁置信度高算谁（平手算截止）。"""
    ev, ev_conf = pick_candidate(answers, "event_date", cands)
    dl, dl_conf = pick_candidate(answers, "deadline_date", cands)
    if ev is not None and ev is dl:
        if ev["has_end"] or ev_conf > dl_conf:
            dl, dl_conf = None, 0.0
        else:
            ev, ev_conf = None, 0.0
    out = {"deadline": "", "deadline_text": "", "ddl_conf": dl_conf, "event_start": "", "event_end": "",
           "event_text": "", "event_place": "", "event_conf": ev_conf}
    if dl is not None:
        out["deadline"] = dl["dt"].strftime("%Y-%m-%d %H:%M")
        out["deadline_text"] = dl["raw"]
    if ev is not None:
        if ev["has_time"]:
            out["event_start"] = ev["dt"].strftime("%Y-%m-%d %H:%M")
            out["event_end"] = ev["dt_end"].strftime("%Y-%m-%d %H:%M") if ev["has_end"] else ""
        else:
            out["event_start"] = ev["date"].strftime("%Y-%m-%d") + " 00:00"
            out["event_end"] = ev["date"].strftime("%Y-%m-%d") + " 23:59"
        out["event_text"] = ev["raw"]
        out["event_place"] = ddl.extract_place(text)
    return out


def analyze(e, prefs="", ignore_text="", key=None, count=True, dates_only=False):
    """对一封邮件（dict：date/sender/subject/body）做完整判断，不碰数据库。

    返回 dict：cat, cat_conf, action, deadline, deadline_text, ddl_conf, event_start, event_end, event_text,
    event_place, event_conf, importance(1~5 的小数), imp_conf, ignored(是否被发件人规则直接忽略), usage, candidates。
    命中忽略规则时不调用 Jev。dates_only=True（回填用）只问日期两题，返回里没有 cat/importance 等。
    """
    sender = e.get("sender") or ""
    blank = {"deadline": "", "deadline_text": "", "ddl_conf": 0.0, "event_start": "", "event_end": "", "event_text": "",
             "event_place": "", "event_conf": 0.0}
    if ignored_by(sender, ignore_text):
        return dict(blank, cat="ignored", cat_conf=1.0, action=0.0, importance=1.0, imp_conf=1.0, ignored=True,
                    usage={}, candidates=0)
    sent = store.parse_dt(e.get("date") or "")
    subject, body = e.get("subject") or "", e.get("body") or ""
    text = subject + " ¶ " + body
    cands = ddl.extract_candidates(text, sent)
    state = "Email received by a university student.\nDate: %s\nFrom: %s\nSubject: %s\n\n%s" % (
        e.get("date"), sender, subject, ddl.clean_text(body)[:JEV_STATE_BODY])
    questions = build_questions(cands, prefs, dates_only=dates_only)
    if not questions:                       # 回填：没有候选日期，不用问
        return dict(blank, ignored=False, usage={}, candidates=0)
    answers, usage = jev_call(state, questions, key=key, count=count)
    out = dict(blank, ignored=False, usage=usage, candidates=len(cands))
    out.update(time_result(cands, answers, text))
    if not dates_only:
        out["cat"] = answers["cat"]["choice"]
        out["cat_conf"] = float(answers["cat"].get("confidence") or 0)
        out["action"] = float(answers["action"]["noul"])
        out["importance"] = clamp_importance(float(answers["importance"]["score"]) + 1)   # Jev 的 score 从 0 起，+1 = 1~5 星
        out["imp_conf"] = float(answers["importance"].get("confidence") or 0)
    return out


def clamp_importance(x):
    return max(1.0, min(5.0, x))


def classify(uid):
    """对一封邮件调用 Jev（已分类且有重要程度则直接返回缓存）。类别/动手/重要程度/截止日期同一次请求。"""
    with uid_lock(uid):
        cached = store.triage_row(uid)
        if cached and cached.get("importance") is not None:
            with CLAIM_LOCK:
                CLAIMS.pop(uid, None)
            return store.triage_public(cached)
        e = store.get_email(uid)
        if not e:
            raise KeyError("no such uid")
        cfg = config.load()
        try:
            r = analyze(dict(e), cfg.get("preferences") or "", cfg.get("ignore_senders") or "")
        except Exception:
            FAILS[uid] = FAILS.get(uid, 0) + 1
            store.meta_add("errors")
            raise
        store.save_triage(uid, r["cat"], r["cat_conf"], r["action"], r["deadline"], r["deadline_text"], r["ddl_conf"],
                          r["usage"], importance=r["importance"], imp_conf=r["imp_conf"], ignored=r["ignored"],
                          event_start=r["event_start"], event_end=r["event_end"], event_text=r["event_text"],
                          event_place=r["event_place"], event_conf=r["event_conf"])
        with CLAIM_LOCK:
            CLAIMS.pop(uid, None)
        out = store.triage_public(store.triage_row(uid))
        out["candidates"] = r["candidates"]
        return out


def next_unclassified(n):
    with CLAIM_LOCK:
        return store.next_unclassified(n, CLAIMS, FAILS, CLAIM_TTL)
