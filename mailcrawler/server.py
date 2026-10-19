# -*- coding: utf-8 -*-
"""本地 HTTP 服务（只监听 127.0.0.1 随机端口，仅本机可访问，由 pywebview 窗口加载）。

安全：
  * 只接受 Host 为 127.0.0.1/localhost 的请求，POST 校验 Origin，防止别的网页偷偷调用；
  * 密码/API key 只存在系统钥匙串，任何接口都不回传；
  * 同步只读（readonly + BODY.PEEK）；唯一会改邮箱的接口是 POST /api/trash：只做「移到已删除文件夹」。
"""
import json
import mimetypes
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, jev, mailbox, secrets, store
from .paths import log_path

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
