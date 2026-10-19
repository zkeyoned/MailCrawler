# -*- coding: utf-8 -*-
"""按需取原文：假 IMAP 对象（只读 select + BODY.PEEK[]、缓存、失败回退）+ HTTP 接口（全文 / 附件保存）。"""
import base64
import json
import sys
import tempfile
import threading
import urllib.request
from email.message import EmailMessage
from pathlib import Path
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from mailcrawler import config, mailbox, secrets, server, store  # noqa: E402

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def make_eml(subject="测试", html='<p>hi</p><img src="cid:c1"><img src="http://t.example/p.gif">'):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "a@x.cn", "me@y.cn", subject
    m.set_content("纯文本")
    m.add_alternative(html, subtype="html")
    m.get_payload()[1].add_related(PNG, "image", "png", cid="<c1>", disposition="inline")
    m.add_attachment(b"DATA-1", maintype="application", subtype="pdf", filename="a.pdf")
    m.add_attachment(b"DATA-2", maintype="application", subtype="pdf", filename="a.pdf")
    return m.as_bytes()


class FakeIMAP:
    """记录所有命令的假 IMAP 连接。mails: {uid: raw bytes}"""
    log = []
    mails = {}
    fail = None

    def __init__(self):
        self.selected = None

    def select(self, box, readonly=False):
        FakeIMAP.log.append(("select", box, readonly))
        self.selected = (box, readonly)
        return "OK", [b"1"]

    def uid(self, cmd, *args):
        FakeIMAP.log.append(("uid", cmd, args))
        if FakeIMAP.fail:
            raise FakeIMAP.fail
        assert cmd.lower() == "fetch"
        uid = int(args[0])
        raw = FakeIMAP.mails.get(uid)
        if raw is None:
            return "OK", [None]                       # 真实 IMAP：UID 不存在时返回 OK + [None]
        head = ("1 (UID %d BODY[] {%d}" % (uid, len(raw))).encode()
        return "OK", [(head, raw), b")"]

    def logout(self):
        FakeIMAP.log.append(("logout",))


@pytest.fixture()
def fake(monkeypatch):
    FakeIMAP.log, FakeIMAP.mails, FakeIMAP.fail = [], {}, None
    monkeypatch.setattr(mailbox, "_connect", lambda: FakeIMAP())
    mailbox.clear_raw_cache()
    yield FakeIMAP
    mailbox.clear_raw_cache()


def test_fetch_raw_is_readonly_peek_and_cached(fake):
    fake.mails[7] = make_eml()
    raw = mailbox.fetch_raw(7)
    assert raw == fake.mails[7]
    assert ("select", "INBOX", True) in fake.log                                   # 只读选择
    fetches = [x for x in fake.log if x[0] == "uid"]
    assert len(fetches) == 1 and fetches[0][1].lower() == "fetch" and fetches[0][2] == ("7", "(BODY.PEEK[])")
    assert not any(x[0] == "uid" and x[1].lower() in ("store", "copy", "move", "expunge") for x in fake.log)
    assert fake.log[-1] == ("logout",)
    n = len(fake.log)
    assert mailbox.fetch_raw(7) == raw and len(fake.log) == n                      # 第二次走缓存，不碰网络


def test_cache_keeps_only_recent_50(fake):
    for u in range(1, 61):
        fake.mails[u] = b"Subject: x\r\n\r\nbody %d" % u
        mailbox.fetch_raw(u)
    assert len(mailbox._RAW) == 50 and 1 not in mailbox._RAW and 60 in mailbox._RAW and 11 in mailbox._RAW
    mailbox.fetch_raw(11)                              # 访问一次变成最新
    fake.mails[61] = b"Subject: y\r\n\r\nz"
    mailbox.fetch_raw(61)
    assert 11 in mailbox._RAW and 12 not in mailbox._RAW


def test_cache_byte_cap(fake, monkeypatch):
    monkeypatch.setattr(mailbox, "RAW_CACHE_BYTES", 1000)
    for u in (1, 2, 3):
        fake.mails[u] = b"x" * 600
        mailbox.fetch_raw(u)
    assert list(mailbox._RAW) == [3]


def test_missing_message_raises_gone(fake):
    with pytest.raises(mailbox.MessageGone):
        mailbox.fetch_raw(999)
    assert fake.log[-1] == ("logout",)                 # 失败也要退出登录


@pytest.fixture()
def http(monkeypatch, fake):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("MAILCRAWLER_DATA_DIR", d)
        monkeypatch.setenv("MAILCRAWLER_EXPORT_DIR", str(Path(d) / "dl"))
        monkeypatch.setattr(config, "_cache", None)
        monkeypatch.setattr(secrets, "ready", lambda: True)
        store.init_db()
        store.insert_emails([(5, "2026-10-01 10:00:00", "a@x.cn", "测试", "本地保存的纯文本", 0, "x")])
        fake.mails[5] = make_eml()
        srv, port = server.create_server()
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % port

        def call(path, body=None, headers=None):
            req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                         headers=dict({"Content-Type": "application/json"}, **(headers or {})))
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read().decode())
            except HTTPError as e:
                return e.code, json.loads(e.read().decode())
        yield call, Path(d) / "dl"
        srv.shutdown()
        srv.server_close()
        config._cache = None


def test_api_full_renders_html_blocks_images(http, fake):
    call, _ = http
    code, r = call("/api/email/5/full")
    assert code == 200 and r["ok"] is True
    assert "data:image/png;base64," in r["html"] and "t.example" not in r["html"]
    assert r["blocked_images"] == 1 and r["external_images"] is False
    assert [a["filename"] for a in r["attachments"]] == ["a.pdf", "a.pdf"]
    code, r = call("/api/email/5/full?images=1")
    assert r["blocked_images"] == 0 and "http://t.example/p.gif" in r["html"] and r["external_images"] is True
    assert len([x for x in fake.log if x[0] == "uid"]) == 1          # 两次请求只下载一次（缓存）
    assert call("/api/email/404/full")[0] == 404
    assert call("/api/email/5/full", headers={"Host": "evil.com"})[0] == 403


def test_api_full_fetch_failure_falls_back_to_local_text(http, fake):
    call, _ = http
    fake.fail = TimeoutError("timed out")
    code, r = call("/api/email/5/full")
    assert code == 200 and r["ok"] is False and "网络" in r["error"] and r["text"] == "本地保存的纯文本"
    fake.fail = None
    del fake.mails[5]                                                  # 邮件已被移走
    code, r = call("/api/email/5/full")
    assert r["ok"] is False and "已不在收件箱" in r["error"] and r["text"] == "本地保存的纯文本"


def test_api_attachment_save_unique_names_no_open(http, monkeypatch):
    call, dl = http
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda *a, **k: opened.append(a))
    code, r1 = call("/api/email/5/attachment/1/save", {})        # 下标 0 是 cid 图片，1、2 是两个同名 pdf
    assert code == 200 and r1["name"] == "a.pdf" and Path(r1["path"]).read_bytes() == b"DATA-1" and r1["size"] == 6
    code, r2 = call("/api/email/5/attachment/2/save", {})
    assert r2["name"] == "a (1).pdf" and Path(r2["path"]).read_bytes() == b"DATA-2"
    code, r3 = call("/api/email/5/attachment/1/save", {})
    assert r3["name"] == "a (2).pdf"
    assert sorted(p.name for p in dl.iterdir()) == ["a (1).pdf", "a (2).pdf", "a.pdf"]
    assert all(Path(r["path"]).parent == dl for r in (r1, r2, r3)) and not opened
    assert call("/api/email/5/attachment/99/save", {})[0] == 404
    assert call("/api/email/404/attachment/1/save", {})[0] == 404
    assert call("/api/email/5/attachment/1/save", {}, headers={"Origin": "http://evil.com"})[0] == 403


def test_api_attachment_save_network_error(http, fake):
    call, dl = http
    fake.fail = ConnectionResetError("reset")
    code, r = call("/api/email/5/attachment/1/save", {})
    assert code == 502 and "网络" in r["error"]
    assert not dl.exists() or not list(dl.iterdir())


def test_malicious_filename_stays_inside_downloads(http, fake):
    call, dl = http
    m = EmailMessage()
    m["Subject"] = "evil"
    m.set_content("x")
    m.add_attachment(b"X", maintype="application", subtype="octet-stream", filename="../../../../tmp/pwned.sh")
    fake.mails[5] = m.as_bytes()
    mailbox.clear_raw_cache()
    code, r = call("/api/email/5/full")
    assert r["attachments"][0]["filename"] == "pwned.sh"
    code, r = call("/api/email/5/attachment/0/save", {})
    assert Path(r["path"]).parent == dl and Path(r["path"]).name == "pwned.sh"
