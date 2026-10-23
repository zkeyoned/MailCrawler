# -*- coding: utf-8 -*-
"""本地 HTTP 服务（只监听 127.0.0.1 随机端口，仅本机可访问，由 pywebview 窗口加载）。

安全：
  * 只接受 Host 为 127.0.0.1/localhost 的请求，POST 校验 Origin，防止别的网页偷偷调用；
  * 密码/API key 只存在系统钥匙串，任何接口都不回传；
  * 同步只读（readonly + BODY.PEEK）；唯一会改邮箱的接口是 POST /api/trash：只做「移到已删除文件夹」。
"""
import imaplib
import json
import mimetypes
import re
import socket
import ssl
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, ics, jev, mailbox, mimeview, secrets, store, timetable, translate
from .paths import data_dir, log_path

STATIC = Path(__file__).resolve().parent / "static"

# ------------------------------------------------------------------ 自动同步
AUTO = {"last": 0.0}
_AUTO_LOCK = threading.Lock()


def _auto_loop():
    while True:
        time.sleep(30)
        try:
            cfg = config.load()
            if not cfg.get("auto_sync") or not secrets.ready():
                continue
            interval = max(5, int(cfg.get("auto_sync_minutes") or 30)) * 60
            with _AUTO_LOCK:
                if time.time() - AUTO["last"] < interval:
                    continue
                if mailbox.SYNC["syncing"]:
                    continue
                AUTO["last"] = time.time()
            mailbox.start_sync(auto=True)
        except Exception:
            pass


def start_auto_sync():
    threading.Thread(target=_auto_loop, daemon=True, name="autosync").start()


# ------------------------------------------------------------------ 数据组装
def progress():
    with store.DB_LOCK, store.db() as c:
        total = c.execute("SELECT COUNT(*) FROM emails WHERE trashed=0").fetchone()[0]
        classified = c.execute("SELECT COUNT(*) FROM triage WHERE importance IS NOT NULL").fetchone()[0]
    return {"syncing": mailbox.SYNC["syncing"], "synced": mailbox.SYNC["synced"], "to_sync": mailbox.SYNC["to_sync"],
            "total_found": mailbox.SYNC["total_found"], "emails_in_db": total, "classified": classified,
            "sent_to_jev": store.meta_get("sent_to_jev"), "words_read": store.meta_get("words_read"),
            "errors": store.meta_get("errors"), "sync_errors": mailbox.SYNC["errors"],
            "phase": mailbox.SYNC["phase"], "error": mailbox.SYNC["error"],
            "last_finished": mailbox.SYNC["last_finished"], "auto": mailbox.SYNC.get("auto", False)}


def setup_info():
    cfg = config.load()
    ready = secrets.ready()
    info = {"logged_in": ready,
            "email": cfg.get("email") if ready else "",
            "since": cfg.get("since") or "", "before": cfg.get("before") or "",
            "auto_sync": bool(cfg.get("auto_sync")), "auto_sync_minutes": int(cfg.get("auto_sync_minutes") or 30),
            "preferences": cfg.get("preferences") or "", "ignore_senders": cfg.get("ignore_senders") or ""}
    if ready:
        info["progress"] = progress()
        toks = 0
        with store.DB_LOCK, store.db() as c:
            toks = c.execute("SELECT COALESCE(SUM(input_tokens),0) FROM triage").fetchone()[0]
        info["input_tokens"] = toks
        info["cost_usd"] = round(toks * jev.USD_PER_M_TOKENS / 1e6, 5)
    return info


def wipe_local_data():
    """退出登录时删除本地邮件数据库（邮件正文属个人隐私，不留盘）。"""
    import os
    from .paths import db_path
    for suffix in ("", "-wal", "-shm"):
        p = str(db_path()) + suffix
        try:
            os.unlink(p)
        except OSError:
            pass


def log_line(msg):
    try:
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _star_arg(qs, name):
    """查询串里的星级筛选参数：1~5 的整数，其余当没传。"""
    try:
        v = int((qs.get(name) or [""])[0])
    except ValueError:
        return None
    return v if 1 <= v <= 5 else None


def friendly_fetch_error(e):
    """原文抓取失败 → 给用户看的话。"""
    if isinstance(e, mailbox.MessageGone):
        return "这封邮件已不在收件箱里（可能已被移走或删除）"
    if isinstance(e, (ssl.SSLError, socket.timeout, TimeoutError, ConnectionError, OSError)):
        return "连不上邮箱服务器，请检查网络后重试"
    if isinstance(e, imaplib.IMAP4.error):
        return "邮箱拒绝了这次请求（%s）" % str(e).strip("b'\" ")[:80]
    return "取原文失败（%s）" % e


def unique_path(d, name):
    """下载目录里不重名：已有就加序号（a.pdf → a (1).pdf）。"""
    p = d / name
    if not p.exists():
        return p
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    else:
        ext = "." + ext
    n = 1
    while True:
        p = d / ("%s (%d)%s" % (stem, n, ext))
        if not p.exists():
            return p
        n += 1


class SaveError(Exception):
    """写本地文件失败（和取原文的网络错误区分开）。"""


def save_attachment(uid, index):
    """把一封邮件的第 index 个附件写进「下载」文件夹（不自动打开）。返回 (路径, 文件名, 大小)。"""
    raw = mailbox.fetch_raw(uid)
    got = mimeview.get_attachment(raw, index)
    if got is None:
        raise KeyError(index)
    name, payload = got
    try:
        d = ics.downloads_dir()
        for _ in range(50):              # "xb" 独占创建：并发/竞态时也不会覆盖已有文件
            p = unique_path(d, mimeview.safe_filename(name))
            try:
                with open(p, "xb") as f:
                    f.write(payload)
                return str(p), p.name, len(payload)
            except FileExistsError:
                continue
        raise OSError("重名文件太多")
    except OSError as e:
        raise SaveError("保存失败：%s" % e)


# 个人版：点附件直接打开。只放行「打开也不会执行代码」的类型；其余只能保存到下载。
OPEN_OK_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".heic", ".webp", ".bmp", ".tif", ".tiff",
               ".txt", ".csv", ".md", ".rtf", ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt",
               ".pages", ".numbers", ".key", ".mp3", ".m4a", ".wav", ".mp4", ".mov", ".ics"}


def open_attachment(uid, index):
    """把附件写进缓存目录（打上「来自互联网」隔离标记）并用系统默认应用打开。返回 (路径, 文件名)。"""
    raw = mailbox.fetch_raw(uid)
    got = mimeview.get_attachment(raw, index)
    if got is None:
        raise KeyError(index)
    name, payload = got
    fname = mimeview.safe_filename(name)
    ext = Path(fname).suffix.lower()
    if ext not in OPEN_OK_EXT:
        raise SaveError("「%s」这类文件不直接打开（可能含可执行内容），请先保存到下载再自己确认" % (ext or "无扩展名"))
    d = data_dir() / "attachments" / str(uid)
    d.mkdir(parents=True, exist_ok=True)
    p = d / ("%d-%s" % (index, fname))
    try:
        if not p.exists() or p.stat().st_size != len(payload):
            p.write_bytes(payload)
        subprocess.run(["xattr", "-w", "com.apple.quarantine", "0081;%08x;MailCrawler;" % int(time.time()), str(p)],
                       capture_output=True, timeout=10)
        subprocess.run(["open", str(p)], check=True, capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        raise SaveError("打开失败：%s" % e)
    return str(p), fname


_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _date_arg(v, name):
    """YYYY-MM-DD（且是真实日期）；否则抛 ValueError。"""
    v = (v or "").strip()
    if not _DATE_RE.fullmatch(v):
        raise ValueError("%s 应为 YYYY-MM-DD" % name)
    datetime.strptime(v, "%Y-%m-%d")
    return v


# ------------------------------------------------------------------ HTTP
class H(BaseHTTPRequestHandler):
    server_version = "MailCrawler/1.0"

    def log_message(self, fmt, *a):
        pass

    # ---- 输出
    def send_json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def send_static(self, rel):
        p = (STATIC / rel).resolve()
        if STATIC.resolve() not in p.parents or not p.is_file():
            return self.send_json({"error": "not found"}, 404)
        b = p.read_bytes()
        ct = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        if ct.startswith("text/") or ct in ("application/javascript", "application/json"):
            ct += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def guard(self, post=False):
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host not in ("127.0.0.1", "localhost"):
            self.send_json({"error": "bad host"}, 403)
            return False
        if post:
            o = self.headers.get("Origin")
            if o:
                from urllib.parse import urlparse as _up
                if _up(o).hostname not in ("127.0.0.1", "localhost"):
                    self.send_json({"error": "bad origin"}, 403)
                    return False
        return True

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > 5_000_000:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

    # ---- GET
    def do_GET(self):
        if not self.guard():
            return
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        try:
            if path in ("/", "/index.html"):
                return self.send_static("index.html")
            if path.startswith("/static/"):
                return self.send_static(path[len("/static/"):])
            if path in ("/app.js", "/app.css"):
                return self.send_static(path[1:])
            if path == "/api/setup":
                return self.send_json(setup_info())
            if not secrets.ready():
                return self.send_json({"error": "not logged in"}, 401)
            if path == "/api/progress":
                return self.send_json(progress())
            if path == "/api/stats":
                s = store.stats(list(jev.CATS))
                toks = 0
                with store.DB_LOCK, store.db() as c:
                    toks = c.execute("SELECT COALESCE(SUM(input_tokens),0) FROM triage").fetchone()[0]
                s["cost_usd"] = round(toks * jev.USD_PER_M_TOKENS / 1e6, 5)
                return self.send_json(s)
            if path == "/api/emails":
                view = (qs.get("view") or ["all"])[0]
                q = (qs.get("q") or [""])[0].strip()
                limit = max(1, min(int((qs.get("limit") or ["200"])[0]), 1000))
                offset = max(0, int((qs.get("offset") or ["0"])[0]))
                sort = (qs.get("sort") or ["date"])[0]
                lo = _star_arg(qs, "min")
                hi = _star_arg(qs, "max")
                return self.send_json(store.list_emails(view, q, limit, offset, sort=sort, min_stars=lo, max_stars=hi))
            if path == "/api/calendar":
                try:
                    frm = _date_arg((qs.get("from") or [""])[0], "from")
                    to = _date_arg((qs.get("to") or [""])[0], "to")
                except ValueError as e:
                    return self.send_json({"error": str(e)}, 400)
                if to < frm or (datetime.strptime(to, "%Y-%m-%d") - datetime.strptime(frm, "%Y-%m-%d")).days > 400:
                    return self.send_json({"error": "日期范围不对（to 不能早于 from，最多 400 天）"}, 400)
                items = store.calendar_items(frm, to, min_stars=_star_arg(qs, "min"),
                                             hide_expired=(qs.get("hide_expired") or ["0"])[0] == "1")
                return self.send_json({"from": frm, "to": to, "total": len(items), "items": items})
            m = re.fullmatch(r"/api/email/(\d+)/full", path)
            if m:
                uid = int(m.group(1))
                local = store.get_email(uid)
                if local is None:
                    return self.send_json({"error": "not found"}, 404)
                ext = (qs.get("images") or ["0"])[0] == "1"
                try:
                    raw = mailbox.fetch_raw(uid)
                except Exception as e:
                    # 原文取不到：不报错，让前端退回显示库里的纯文本
                    return self.send_json({"ok": False, "error": friendly_fetch_error(e), "text": local["body"] or ""})
                try:
                    res = mimeview.render(raw, allow_external=ext)
                except Exception as e:
                    return self.send_json({"ok": False, "error": "原文解析失败（%s）" % e, "text": local["body"] or ""})
                res["ok"] = True
                return self.send_json(res)
            if path == "/api/timetable":
                try:
                    frm = _date_arg((qs.get("from") or [""])[0], "from")
                    to = _date_arg((qs.get("to") or [""])[0], "to")
                except ValueError as e:
                    return self.send_json({"error": str(e)}, 400)
                a, b = datetime.strptime(frm, "%Y-%m-%d").date(), datetime.strptime(to, "%Y-%m-%d").date()
                if b < a or (b - a).days > 400:
                    return self.send_json({"error": "日期范围不对（to 不能早于 from，最多 400 天）"}, 400)
                return self.send_json(timetable.payload(a, b))
            if path == "/api/week":
                try:
                    start = _date_arg((qs.get("start") or [""])[0], "start")
                except ValueError as e:
                    return self.send_json({"error": str(e)}, 400)
                return self.send_json(timetable.week_payload(datetime.strptime(start, "%Y-%m-%d").date()))
            m = re.fullmatch(r"/api/email/(\d+)", path)
            if m:
                e = store.get_email_full(int(m.group(1)))
                return self.send_json(e) if e else self.send_json({"error": "not found"}, 404)
            if path == "/api/next":
                n = int((qs.get("n") or ["1"])[0])
                return self.send_json({"emails": jev.next_unclassified(n)})
            return self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.send_json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

    # ---- POST
    def do_POST(self):
        if not self.guard(post=True):
            return
        path = urlparse(self.path).path
        try:
            body = self.read_json()
            # 登录 / 登出不需要已登录态
            if path == "/api/login":
                return self._login(body)
            if path == "/api/logout":
                wipe_local_data()
                secrets.clear_account()
                return self.send_json({"ok": True})
            if not secrets.ready():
                return self.send_json({"error": "not logged in"}, 401)
            if path == "/api/settings":
                return self._settings(body)
            if path == "/api/sync":
                return self.send_json({"started": mailbox.start_sync(auto=False), "syncing": True})
            m = re.fullmatch(r"/api/classify/(\d+)", path)
            if m:
                try:
                    return self.send_json(jev.classify(int(m.group(1))))
                except KeyError:
                    return self.send_json({"error": "not found"}, 404)
                except Exception as e:
                    return self.send_json({"error": "Jev 调用失败：%s" % e}, 502)
            if path == "/api/words":
                n = max(0, min(int(body.get("n") or 0), 1_000_000))
                if n:
                    store.meta_add("words_read", n)
                return self.send_json({"words_read": store.meta_get("words_read")})
            if path == "/api/calendar/ics":
                # 导出「现在起、未过期」的条目（可带 min 星级、from 起始日期）；写到「下载」文件夹后返回路径
                try:
                    frm = _date_arg(body.get("from") or datetime.now().strftime("%Y-%m-%d"), "from")
                except ValueError as e:
                    return self.send_json({"error": str(e)}, 400)
                try:
                    lo = int(body.get("min") or 0)
                except (TypeError, ValueError):
                    lo = 0
                items = store.calendar_items(frm, None, min_stars=lo if 1 <= lo <= 5 else None, hide_expired=True)
                if not items:
                    return self.send_json({"error": "当前筛选下没有未过期的条目可导出"}, 404)
                try:
                    full, name = ics.export_file(items)
                except OSError as e:
                    return self.send_json({"error": "写文件失败：%s" % e}, 500)
                log_line("calendar exported: %s (%d)" % (name, len(items)))
                return self.send_json({"path": full, "name": name, "count": len(items)})
            m = re.fullmatch(r"/api/email/(\d+)/attachment/(\d+)/open", path)
            if m:
                uid, idx = int(m.group(1)), int(m.group(2))
                if store.get_email(uid) is None:
                    return self.send_json({"error": "not found"}, 404)
                try:
                    full, name = open_attachment(uid, idx)
                except KeyError:
                    return self.send_json({"error": "没有这个附件"}, 404)
                except SaveError as e:
                    return self.send_json({"error": str(e)}, 400)
                except Exception as e:
                    return self.send_json({"error": friendly_fetch_error(e)}, 502)
                log_line("attachment opened: uid=%d #%d %s" % (uid, idx, name))
                return self.send_json({"path": full, "name": name})
            m = re.fullmatch(r"/api/email/(\d+)/translate", path)
            if m:
                uid = int(m.group(1))
                local = store.get_email(uid)
                if local is None:
                    return self.send_json({"error": "not found"}, 404)
                hit = translate.cached(uid)
                if hit:
                    return self.send_json({"text": hit, "cached": True})
                text = ""
                try:
                    p = mimeview.parse(mailbox.fetch_raw(uid))
                    text = p.text if (p.text or "").strip() else translate.html_to_text(p.html)
                except Exception:
                    text = local["body"] or ""
                if not text.strip():
                    return self.send_json({"error": "这封邮件没有可翻译的正文"}, 400)
                try:
                    out, was_cached = translate.translate(uid, local["subject"] or "", text)
                except RuntimeError as e:
                    return self.send_json({"error": str(e)}, 502)
                log_line("translated: uid=%d (%d chars)" % (uid, len(text)))
                return self.send_json({"text": out, "cached": was_cached})
            m = re.fullmatch(r"/api/email/(\d+)/attachment/(\d+)/save", path)
            if m:
                uid, idx = int(m.group(1)), int(m.group(2))
                if store.get_email(uid) is None:
                    return self.send_json({"error": "not found"}, 404)
                try:
                    full, name, size = save_attachment(uid, idx)
                except KeyError:
                    return self.send_json({"error": "没有这个附件"}, 404)
                except SaveError as e:
                    return self.send_json({"error": str(e)}, 500)
                except Exception as e:
                    return self.send_json({"error": friendly_fetch_error(e)}, 502)
                log_line("attachment saved: uid=%d #%d %s (%d bytes)" % (uid, idx, name, size))
                return self.send_json({"path": full, "name": name, "size": size})
            if path == "/api/trash":
                uids = body.get("uids")
                if not isinstance(uids, list) or not uids or len(uids) > 5000:
                    return self.send_json({"error": "uids 必须是 1~5000 个 UID 的数组"}, 400)
                try:
                    res = mailbox.trash_uids(uids)
                except Exception as e:
                    return self.send_json({"error": "移到已删除失败（邮箱未改动或部分改动，请刷新核对）：%s" % e}, 502)
                return self.send_json(res)
            return self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.send_json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

    def _login(self, body):
        email_addr = (body.get("email") or "").strip()
        password = body.get("password") or ""
        key = (body.get("jev_key") or "").strip()
        if not email_addr or "@" not in email_addr:
            return self.send_json({"error": "请输入完整的邮箱地址（如 name@example.edu.cn）"}, 400)
        if not password:
            return self.send_json({"error": "请输入邮箱密码"}, 400)
        if not key:
            return self.send_json({"error": "请输入 TypeSafe API key（Jev 分类用）"}, 400)
        try:
            mailbox.verify_login(email_addr, password)
        except mailbox.LoginError as e:
            log_line("login failed for %s: %s" % (email_addr, e))
            return self.send_json({"error": str(e)}, 401)
        secrets.save_account(email_addr, password, key)
        config.save(since=(body.get("since") or "").strip(), before=(body.get("before") or "").strip(),
                    auto_sync=bool(body.get("auto_sync")))
        store.init_db()
        log_line("login ok: %s" % email_addr)
        mailbox.start_sync(auto=False)   # 登录成功立刻开始第一轮同步，别让用户对着「等待同步」干等
        return self.send_json(setup_info())

    def _settings(self, body):
        kwargs = {}
        for k in ("since", "before"):
            if k in body:
                v = (body.get(k) or "").strip()
                if v and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                    return self.send_json({"error": "日期格式应为 YYYY-MM-DD"}, 400)
                kwargs[k] = v
        if "auto_sync" in body:
            kwargs["auto_sync"] = bool(body.get("auto_sync"))
        if "auto_sync_minutes" in body:
            try:
                kwargs["auto_sync_minutes"] = max(5, min(int(body.get("auto_sync_minutes")), 24 * 60))
            except (TypeError, ValueError):
                return self.send_json({"error": "同步间隔应是分钟数"}, 400)
        for k, limit in (("preferences", jev.PREF_MAX), ("ignore_senders", 4000)):
            if k in body:
                v = body.get(k)
                if not isinstance(v, str):
                    return self.send_json({"error": "%s 应是文本" % k}, 400)
                v = v.strip()
                if len(v) > limit:
                    return self.send_json({"error": "太长了（最多 %d 个字符）" % limit}, 400)
                kwargs[k] = v
        return self.send_json(config.save(**kwargs))


def create_server():
    """绑定 127.0.0.1:0（随机端口），返回 (server, port)。"""
    store.init_db()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    return srv, srv.server_address[1]
