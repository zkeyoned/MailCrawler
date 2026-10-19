# -*- coding: utf-8 -*-
"""邮件原文渲染：MIME 解析 / HTML 清理 / cid 替换 / 外部图片屏蔽 / 附件提取（全部用造的 .eml 样例，不连邮箱）。"""
import base64
import re
import sys
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from mailcrawler import mimeview  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def build_related(html, text="纯文本版本", with_att=True):
    """alternative(text, related(html + cid 图片)) + 附件"""
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "a@x.cn", "b@y.cn", "测试邮件"
    m.set_content(text)
    m.add_alternative(html, subtype="html")
    m.get_payload()[1].add_related(PNG, "image", "png", cid="<logo123@x>", filename="logo.png", disposition="inline")
    if with_att:
        m.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="作业说明.pdf")
    return m.as_bytes()


EVIL = """<html><head><title>x</title><meta http-equiv="refresh" content="0;url=http://evil">
<style>body{color:red;background:url(javascript:alert(1))} .a{width:expression(alert(2))} @import url(http://evil/x.css);
.ok{margin:0}.bg{background:url(http://t.example/p.png)}.d{background:url(data:image/png;base64,AAAA)}</style>
<script>alert(1)</script></head>
<body onload="alert(1)">
<p onclick="alert(2)" style="color:blue;width:expression(alert(3));background:url('http://t.example/q.gif')">hello <b>world</b></p>
<a href="javascript:alert(4)">js</a> <a href="  JaVa&#x09;script:alert(5)">js2</a> <a href="https://ok.example/p?q=1&amp;r=2">ok</a> <a href="mailto:a@b.c">m</a>
<a href="data:text/html;base64,PHNjcmlwdD4=">d</a>
<iframe src="http://evil"></iframe><object data="x"></object><embed src="x"><form action="http://evil"><input name="pw"><button>go</button>表单内文字</form>
<svg onload="alert(1)"><script>alert(2)</script></svg><img src="cid:logo123@x" alt="logo"><img src="http://t.example/pixel.gif" width="1" height="1">
<img src="//t2.example/a.png"><img src="x.png"><img src="javascript:alert(1)" onerror="alert(1)">
<table background="http://t.example/bg.png" bgcolor="#eee"><tr><td>cell</td></tr></table>
<noscript><img src="http://t.example/ns.gif"></noscript><div style="position:fixed;top:0">fixed</div>
<!--[if mso]><p>comment</p><![endif]-->
</body></html>"""


def render_evil(ext=False):
    return mimeview.render(build_related(EVIL), allow_external=ext)


def body_of(res):
    return res["html"].split("<body>", 1)[1]


def test_html_prefers_html_part_and_returns_plain_text():
    r = mimeview.render(build_related("<p>你好<b>世界</b></p>"))
    assert r["html"] and "你好<b>世界</b>" in r["html"]
    assert "纯文本版本" in r["text"]


def test_text_only_mail_has_no_html():
    m = EmailMessage()
    m["Subject"] = "plain"
    m.set_content("只有文字\nhttps://example.com/x")
    r = mimeview.render(m.as_bytes())
    assert r["html"] is None and "https://example.com/x" in r["text"]


def test_sanitizer_strips_active_content():
    b = body_of(render_evil())
    low = b.lower()
    for bad in ("<script", "alert(", "onload", "onclick", "onerror", "<iframe", "<object", "<embed", "<form", "<input", "<button",
                "<svg", "javascript:", "expression(", "@import", "position:fixed", "comment", "noscript", "ns.gif", "data:text/html"):
        assert bad not in low, bad
    assert "表单内文字" in b           # form 标签去掉、里面的字保留
    assert "<b>world</b>" in b
    # <head> 里的 style 被清理后保留：好声明留下、坏声明没了
    doc = render_evil()["html"]
    assert "margin:0" in doc and "color:red" in doc
    assert "http://evil" not in doc and "refresh" not in doc


def test_links_are_safe_and_open_externally():
    b = body_of(render_evil())
    assert 'href="https://ok.example/p?q=1&amp;r=2"' in b
    assert 'href="mailto:a@b.c"' in b
    anchors = re.findall(r"<a[^>]*>", b)
    assert len(anchors) == 5
    for a in anchors:
        if "href=" in a:
            assert 'target="_blank"' in a and "noopener" in a
    assert '<base target="_blank">' in render_evil()["html"]


def test_cid_image_inlined_as_data_uri():
    r = render_evil()
    b = body_of(r)
    assert "cid:" not in b
    assert "data:image/png;base64," + base64.b64encode(PNG).decode() in b
    # 只被正文引用的内嵌图片不当附件列；真正的附件还在
    assert [a["filename"] for a in r["attachments"]] == ["作业说明.pdf"]


def test_external_images_blocked_by_default_then_allowed():
    r = render_evil(False)
    b = body_of(r)
    assert "t.example" not in b and "t2.example" not in r["html"].split("</style>")[-1]
    assert r["blocked_images"] == 3          # pixel.gif、//t2.example、table background（x.png / javascript: 是无效地址，不算外部）
    assert "img-src data:;" in r["html"]
    assert "mc-blocked" in b
    r2 = render_evil(True)
    b2 = body_of(r2)
    assert 'src="http://t.example/pixel.gif"' in b2 and 'src="https://t2.example/a.png"' in b2
    assert r2["blocked_images"] == 0 and r2["external_images"] is True
    assert "img-src data: https: http:;" in r2["html"]
    assert 'referrerpolicy="no-referrer"' in b2
    # 即使放开外部图片，javascript: 之类仍然没有
    assert "javascript:" not in b2.lower() and "alert(" not in b2


def test_unresolvable_or_relative_images_get_placeholder_not_requests():
    b = body_of(render_evil(True))
    assert 'src="x.png"' not in b
    assert 'src="javascript' not in b


def test_csp_meta_present():
    doc = render_evil()["html"]
    assert "Content-Security-Policy" in doc and "default-src 'none'" in doc


def test_css_cleaning_units():
    assert mimeview.clean_css("color:red;width:expression(alert(1));margin:0") == "color:red;margin:0"
    assert "url(" not in mimeview.clean_css("background:url(http://x/y.png);color:red")
    assert "url(http://x/y.png)" in mimeview.clean_css("background:url(http://x/y.png)", allow_external=True)
    assert "javascript" not in mimeview.clean_css("background:url(javascript:alert(1))", allow_external=True)
    assert "\\65xpression" not in mimeview.clean_css("width:\\65xpression(alert(1))")      # CSS 转义绕过
    assert "expression" not in mimeview.clean_css("width:\\65 xpression(alert(1))")
    assert "@import" not in mimeview.clean_css("@import url(x.css);a{b:c}")
    out = mimeview.clean_css("a{background:url(data:image/png;base64,AAAA);color:red}")      # data URI 里有分号，不能被切碎
    assert "data:image/png;base64,AAAA" in out and "color:red" in out
    assert "</style" not in mimeview.clean_css("a{content:'</style><script>'}").lower()


def test_unclosed_and_malformed_html_does_not_leak():
    html = "<div><p>开头<script>alert(1)"            # script 没闭合：后面的全吞掉
    b = body_of(mimeview.render(build_related(html, with_att=False)))
    assert "alert" not in b and "开头" in b
    b = body_of(mimeview.render(build_related("a < b & c <b>粗</b></i></div></div>", with_att=False)))
    assert "a &lt; b &amp; c" in b and "<b>粗</b>" in b


def test_attachments_extracted_with_stable_index_and_content():
    raw = build_related('<p>x</p><img src="cid:logo123@x">')
    atts = mimeview.render(raw)["attachments"]
    assert len(atts) == 1 and atts[0]["content_type"] == "application/pdf" and atts[0]["size"] == len(b"%PDF-1.4 fake")
    name, data = mimeview.get_attachment(raw, atts[0]["index"])
    assert name == "作业说明.pdf" and data == b"%PDF-1.4 fake"
    assert mimeview.get_attachment(raw, 99) is None


def test_static_eml_gbk_chinese_filename_and_forwarded_mail():
    raw = (FIX / "gbk_mixed.eml").read_bytes()
    r = mimeview.render(raw)
    assert r["html"] is None and "请于本周五前提交实验报告" in r["text"]
    names = [a["filename"] for a in r["attachments"]]
    assert names[0] == "成绩单.txt" and names[1] == "inner mail.eml"
    assert "助教" in r["cc"]
    name, data = mimeview.get_attachment(raw, 0)
    assert data.decode("utf-8") == "成绩单内容"
    assert b"inner body" in mimeview.get_attachment(raw, 1)[1]


def test_inline_image_not_referenced_is_listed_as_attachment():
    m = EmailMessage()
    m.set_content("see")
    m.add_alternative("<p>no image reference</p>", subtype="html")
    m.get_payload()[1].add_related(PNG, "image", "png", cid="<unused>", filename="pic.png", disposition="inline")
    r = mimeview.render(m.as_bytes())
    assert [a["filename"] for a in r["attachments"]] == ["pic.png"]


@pytest.mark.parametrize("name,expect", [
    ("../../etc/passwd", "passwd"), ("a/b\\c.txt", "c.txt"), (".hidden", "hidden"), ("", "attachment"),
    ("x\x00y:z*.pdf", "x_y_z_.pdf"), ("/", "attachment")])
def test_safe_filename(name, expect):
    assert mimeview.safe_filename(name) == expect


def test_safe_filename_length_limit():
    n = mimeview.safe_filename("长" * 300 + ".pdf")
    assert len(n) <= 150 and n.endswith(".pdf")


def test_garbage_input_never_raises():
    r = mimeview.render(b"\xff\xfe not a mail at all")
    assert r["attachments"] == []
