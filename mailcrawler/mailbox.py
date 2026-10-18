# -*- coding: utf-8 -*-
"""IMAP 邮箱访问。安全红线（对应 README 的承诺）：

  * 同步全程只读：select(readonly=True) + BODY.PEEK，绝不把邮件标成已读；
  * 唯一会改邮箱的操作是 move_to_trash（移到「已删除」文件夹）：
    优先 UID MOVE，退化为 UID COPY + 标记 \\Deleted + 只针对这些 UID 的 EXPUNGE；
    绝不永久删除、绝不清空「已删除」文件夹。
"""
import base64
import email
import html as htmllib
import imaplib
import re
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

from . import config, secrets, store

IMAP_HOST, IMAP_PORT = "imap.exmail.qq.com", 993   # 腾讯企业邮箱
FETCH_PREFIX = 30000      # 每封只取前 30KB，跳过大附件
FETCH_BATCH = 50
SYNC_WORKERS = 3          # 并发连接数（每个 worker 一条 IMAP 连接，错开慢速串行取信）

SSL_CTX = ssl.create_default_context()
try:  # python.org 版 Python 在 macOS 上可能不带根证书，补一份系统证书
    SSL_CTX.load_verify_locations("/etc/ssl/cert.pem")
except Exception:
    pass

# ------------------------------------------------------------------ SSL / 登录
_tls = threading.local()


def ssl_ctx():
    return SSL_CTX


def _connect():
    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=SSL_CTX, timeout=90)
    M.login(config.load()["email"], secrets.imap_password())
    return M


def connection():
    """当前线程的 IMAP 连接（线程池里每个 worker 复用一条）。"""
    M = getattr(_tls, "M", None)
    if M is None:
        M = _connect()
        _tls.M = M
    return M


def drop_connection():
    M = getattr(_tls, "M", None)
    _tls.M = None
    if M is not None:
        try:
            M.logout()
        except Exception:
            pass


class LoginError(Exception):
    """登录失败：message 已经是可以直接给用户看的提示。"""


def verify_login(email_addr, password):
    """试登录一次。成功返回 None，失败抛 LoginError（带可读的提示）。"""
    M = None
    try:
        M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=SSL_CTX, timeout=30)
        M.login(email_addr, password)
        return None
    except imaplib.IMAP4.error as e:
        hint = ""
        if "exmail.qq.com" in IMAP_HOST:
            hint = ("如果网页邮箱「设置 → 微信绑定」里开了「安全登录」，需要用「客户端专用密码」登录，"
                    "或者先在网页邮箱里把安全登录关掉。")
        raise LoginError("邮箱登录失败（%s）。%s" % (str(e).strip("b'\" "), hint))
    except (ssl.SSLError, OSError) as e:
        raise LoginError("连不上邮箱服务器 %s:%d（%s）。请检查网络，或换个网络环境再试。" % (IMAP_HOST, IMAP_PORT, e))
    finally:
        if M is not None:
            try:
                M.logout()
            except Exception:
                pass


# ------------------------------------------------------------------ 邮件解析
def dec(s):
    try:
        return str(make_header(decode_header(s or "")))
    except Exception:
        return str(s or "")


def _decode_payload(p):
    raw = p.get_payload(decode=True)
    if raw is None:
        return ""
    cs = (p.get_content_charset() or "utf-8").lower()
    if cs in ("gb2312", "gbk"):
        cs = "gb18030"
    try:
        return raw.decode(cs, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def body_text(msg, limit=6000):
    """提取纯文本正文：优先 text/plain，否则把 text/html 剥标签。"""
    try:
        parts = msg.walk() if msg.is_multipart() else [msg]
        html = ""
        for p in parts:
            ct = p.get_content_type()
            if p.get_content_maintype() == "multipart" or str(p.get("Content-Disposition", "")).lower().startswith("attachment"):
                continue
            try:
                t = _decode_payload(p)
            except Exception:
                continue
            if ct == "text/plain" and t.strip():
                return re.sub(r"\s+", " ", t).strip()[:limit]
            if ct == "text/html" and not html:
                html = t
        t = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", html, flags=re.I)
        t = re.sub(r"<[^>]+>", " ", t)
        t = htmllib.unescape(t).replace("\xa0", " ")
        return re.sub(r"\s+", " ", t).strip()[:limit]
    except Exception:
        return ""


def parse_fetch(data):
    """把 imaplib 的 fetch 结果拆成记录：[(uid, seen, raw_bytes)]。"""
    recs = []
    for item in data:
        if isinstance(item, tuple):
            recs.append({"head": item[0], "raw": item[1] or b"", "tail": b""})
        elif isinstance(item, (bytes, bytearray)) and recs:
            recs[-1]["tail"] += bytes(item)
    out = []
    for r in recs:
        meta = r["head"] + b" " + r["tail"]
        m = re.search(rb"UID (\d+)", meta)
        if not m:
            continue
        out.append((int(m.group(1)), b"\\Seen" in meta, r["raw"]))
    return out


def parse_message(raw):
    """解析一封邮件；失败也返回空壳，保证能入库。"""
    date_s = sender = subject = body = ""
    try:
        msg = email.message_from_bytes(raw)
        try:
            d = parsedate_to_datetime(msg["Date"])
            d = d.astimezone() if d.tzinfo else d
            date_s = d.strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
        sender = dec(msg["From"])
        subject = dec(msg["Subject"])
        body = body_text(msg)
    except Exception:
        pass
    return date_s, sender, subject, body


def imap_utf7_decode(s):
    """IMAP 文件夹名用的 modified UTF-7 解码，仅用于识别「已删除」。"""
    def rep(m):
        t = m.group(1)
        if t == "":
            return "&"
        try:
            return base64.b64decode(t.replace(",", "/") + "=" * (-len(t) % 4)).decode("utf-16-be")
        except Exception:
            return m.group(0)
    return re.sub(r"&([^-]*)-", rep, s)


# ------------------------------------------------------------------ 同步（只读）
SYNC = {"syncing": False, "synced": 0, "to_sync": 0, "total_found": 0, "errors": 0,
        "phase": "", "error": "", "last_finished": "", "auto": False}
SYNC_LOCK = threading.Lock()


def _imap_date(d):
    """YYYY-MM-DD → 01-Jan-2026。"""
    return datetime.strptime(d, "%Y-%m-%d").strftime("%d-%b-%Y")


def search_window(auto=False):
    """按设置里的日期范围生成 IMAP 搜索参数 [(关键字, 值), ...]。"""
    args = []
    cfg = config.load()
    since = cfg.get("since") or ""
    if auto:
        # 自动同步只扫最近 7 天，避免每次都全量搜索；去重靠数据库 UID
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        since = max(since, week_ago) if since else week_ago
    if since:
        args += ["SINCE", _imap_date(since)]
    before = (cfg.get("before") or "").strip()
    if before:
        # IMAP BEFORE 是「早于当天」，含当天要 +1
        args += ["BEFORE", _imap_date((datetime.strptime(before, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d"))]
    return args or ["ALL"]          # 没设范围时必须给 ALL：空的 UID SEARCH 会被腾讯邮箱回 BAD


def _fetch_chunk(args_desc, chunk):
    """worker 线程函数：取一批邮件（自带重连重试），入库，返回本批新增数。"""
    last_err = None
    for attempt in range(3):
        try:
            M = connection()
            M.select("INBOX", readonly=True)               # 只读选择
            typ, data = M.uid("fetch", ",".join(map(str, chunk)),
                              "(UID FLAGS BODY.PEEK[]<0.%d>)" % FETCH_PREFIX)
            if typ != "OK":
                raise RuntimeError("fetch failed: %s" % typ)
            rows = []
            for uid, seen, raw in parse_fetch(data):
                date_s, sender, subject, body = parse_message(raw)
                rows.append((uid, date_s, sender, subject, body, 1 if seen else 0,
                             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            store.insert_emails(rows)
            with SYNC_LOCK:
                SYNC["synced"] += len(rows)
            return len(rows)
        except Exception as e:
            last_err = e
            drop_connection()
            time.sleep(1 + attempt)
    with SYNC_LOCK:
        SYNC["errors"] += 1
    store.meta_add("errors")
    return 0


def sync_worker(auto=False):
    SYNC.update(syncing=True, synced=0, to_sync=0, total_found=0, errors=0, phase="连接邮箱", error="", auto=auto)
    try:
        args = search_window(auto=auto)
        M = _connect()
        try:
            M.select("INBOX", readonly=True)               # 只读选择
            typ, d = M.uid("search", None, *args)
            if typ != "OK":
                raise RuntimeError("搜索失败：%s" % d)
            uids = sorted((int(x) for x in d[0].split()), reverse=True)  # 新的在前，前端能先拿到新邮件
            SYNC["total_found"] = len(uids)
        finally:
            try:
                M.logout()
            except Exception:
                pass
        have = store.all_uids()
        new = [u for u in uids if u not in have]
        SYNC["to_sync"] = len(new)
        SYNC["phase"] = "同步中"
        if new:
            chunks = [new[i:i + FETCH_BATCH] for i in range(0, len(new), FETCH_BATCH)]
            with ThreadPoolExecutor(max_workers=SYNC_WORKERS) as ex:
                list(ex.map(lambda ch: _fetch_chunk(args, ch), chunks))
            drop_connection()
        # 顺手刷新一遍已读状态（只读搜索）
        try:
            M = _connect()
            try:
                M.select("INBOX", readonly=True)
                typ, d = M.uid("search", None, "SEEN", *args)
                store.mark_seen({int(x) for x in d[0].split()})
            finally:
                try:
                    M.logout()
                except Exception:
                    pass
        except Exception:
            pass
        SYNC["phase"] = "完成"
    except Exception as e:
        SYNC["error"] = "%s: %s" % (type(e).__name__, e)
        SYNC["errors"] += 1
        store.meta_add("errors")
        SYNC["phase"] = "出错"
    finally:
        SYNC["syncing"] = False
        SYNC["last_finished"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def start_sync(auto=False):
    with SYNC_LOCK:
        if SYNC["syncing"]:
            return False
        SYNC["syncing"] = True
        threading.Thread(target=sync_worker, args=(auto,), daemon=True, name="sync").start()
        return True


# ------------------------------------------------------------------ 移到「已删除」（唯一会改邮箱的操作）
TRASH_LOCK = threading.Lock()


def find_trash_folder(M):
    """找「已删除」文件夹：优先 \\Trash 特殊用途，其次名字含 Deleted / 已删除 / Trash。找不到返回 None。"""
    typ, lines = M.list()
    special, named = None, []
    for raw in lines or []:
        if raw is None:
            continue
        s = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        m = re.match(r'\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$', s)
        if not m:
            continue
        flags = m.group("flags").lower()
        name = m.group("name").strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        if "\\noselect" in flags or name.upper() == "INBOX":
            continue
        if "\\trash" in flags:
            special = special or name
        elif re.search(r"deleted|已删除|trash|废纸篓", imap_utf7_decode(name), re.I):
            named.append(name)
    if special:
        return special
    for pref in ("Deleted Messages", "Deleted Items", "Trash"):
        if pref in named:
            return pref
    return named[0] if named else None


def quote_mailbox(name):
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def uid_set(uids):
    return ",".join(str(int(u)) for u in uids)


def still_in_inbox(M, uids):
    typ, d = M.uid("search", None, "UID", uid_set(uids))
    return {int(x) for x in (d[0] or b"").split()}


def move_to_trash_on(M, uids):
    """在已 select 为可写 INBOX 的连接上，把 uids 移到「已删除」。
    优先 UID MOVE；不行就 UID COPY → 原邮件 +FLAGS \\Deleted → 只 UID EXPUNGE 这些 UID（需 UIDPLUS；
    没有 UIDPLUS 就只复制+打标记，绝不做全箱 EXPUNGE）。永远不碰已删除文件夹里的东西。
    返回 (moved, failed, folder, method)。"""
    folder = find_trash_folder(M)
    if not folder:
        raise RuntimeError("找不到『已删除』文件夹，未做任何操作")
    caps = {str(x).upper() for x in M.capabilities}
    moved, failed, methods = [], [], set()
    for i in range(0, len(uids), 100):
        chunk = [int(u) for u in uids[i:i + 100]]
        us = uid_set(chunk)
        method = None
        if "MOVE" in caps:
            imaplib.Commands.setdefault("MOVE", ("SELECTED",))   # imaplib 默认不认识 MOVE
            try:
                typ, _ = M.uid("MOVE", us, quote_mailbox(folder))
                if typ == "OK":
                    method = "UID MOVE"
            except Exception:
                method = None
        if not method:
            try:
                typ, _ = M.uid("COPY", us, quote_mailbox(folder))
                if typ == "OK":
                    M.uid("STORE", us, "+FLAGS.SILENT", "(\\Deleted)")
                    if "UIDPLUS" in caps:
                        M.uid("EXPUNGE", us)
                        method = "UID COPY+STORE+EXPUNGE"
                    else:
                        method = "UID COPY+STORE"          # 已复制并标记删除，但没真正移出收件箱
            except Exception:
                method = None
        if not method:
            failed += chunk
            continue
        methods.add(method)
        if method == "UID COPY+STORE":
            moved += chunk
            continue
        try:
            left = still_in_inbox(M, chunk)               # 核对：这些 UID 是否确实离开了收件箱
        except Exception:
            left = set(chunk)
        moved += [u for u in chunk if u not in left]
        failed += [u for u in chunk if u in left]
    return moved, failed, folder, "+".join(sorted(methods))


def trash_uids(uids):
    """把 uids 移到「已删除」文件夹；只处理本地库里确实存在的 UID。"""
    uids = sorted({int(u) for u in uids})
    if not uids:
        return {"moved": [], "failed": [], "folder": "", "method": ""}
    uids = [u for u in uids if u in store.known_uids(uids)]
    if not uids:
        return {"moved": [], "failed": [], "folder": "", "method": ""}
    with TRASH_LOCK:
        M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=SSL_CTX, timeout=90)
        try:
            M.login(config.load()["email"], secrets.imap_password())
            M.select("INBOX")  # 此处必须可写
            moved, failed, folder, method = move_to_trash_on(M, uids)
        finally:
            try:
                M.logout()
            except Exception:
                pass
    if moved:
        store.mark_trashed(moved)
    return {"moved": moved, "failed": failed, "folder": folder, "method": method}
