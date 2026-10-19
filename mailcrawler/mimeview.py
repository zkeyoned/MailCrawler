# -*- coding: utf-8 -*-
"""邮件原文渲染：把一封完整 MIME 原文变成「能安全显示的东西」。

输出三样：
  * html  —— 清理过的 HTML（白名单标签/属性），只用于放进「没有 allow-scripts / allow-same-origin 的沙箱 iframe」；
             cid: 内嵌图片已换成 data: URI；外部 http(s) 图片默认换成占位图（防追踪），allow_external=True 才保留；
  * text  —— 纯文本正文（没有 HTML 时显示它）；
  * attachments —— 附件清单（文件名、大小、类型），按稳定下标可以单独取出内容。

安全：清理是**白名单**式的（不认识的标签只留文字，不认识的属性一律丢），再加一层 CSP meta 兜底；
脚本、事件属性、javascript: 链接、iframe/object/embed/form、CSS 里的 expression/url(外部)/@import 全部去掉。
"""
import email
import html as htmllib
import mimetypes
import re
import urllib.parse
from email.header import decode_header, make_header
from html.parser import HTMLParser

# ------------------------------------------------------------------ 小工具


def dec_header(s):
    try:
        return str(make_header(decode_header(s or "")))
    except Exception:
        return str(s or "")


def _charset_alias(cs):
    cs = (cs or "").lower().strip()
    if cs in ("gb2312", "gbk", "gb-2312", "x-gbk"):
        return "gb18030"
    return cs


def decode_part_text(p):
    """把 text 部分解成 str：有声明字符集按声明；没声明先试 UTF-8 再试 GB18030。"""
    raw = p.get_payload(decode=True)
    if raw is None:
        return ""
    cs = _charset_alias(p.get_content_charset())
    if cs:
        try:
            return raw.decode(cs, "replace")
        except LookupError:
            pass
    for c in ("utf-8", "gb18030"):
        try:
            return raw.decode(c)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def safe_filename(name, fallback="attachment"):
    """只留文件名本身：去路径、控制字符、前导点，限长。"""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f:*?\"<>|]", "_", name).strip().lstrip(".")
    if len(name) > 150:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:140] + dot + ext[:8]) if dot and len(ext) <= 8 else name[:150]
    return name or fallback


# ------------------------------------------------------------------ MIME 解析
class Attachment:
    __slots__ = ("index", "filename", "content_type", "payload", "cid", "disposition")

    def __init__(self, index, filename, content_type, payload, cid, disposition):
        self.index, self.filename, self.content_type = index, filename, content_type
        self.payload, self.cid, self.disposition = payload, cid, disposition


class Parsed:
    def __init__(self):
        self.html = ""
        self.text = ""
        self.attachments = []     # 全部「非正文」部分（含只被 HTML 用作内嵌图片的），下标稳定
        self.cids = {}            # content-id（去尖括号、小写）→ (content_type, bytes)
        self.headers = {}


def _walk(msg):
    """递归遍历叶子部分；message/rfc822（整封转发的邮件）当作一个附件，不钻进去。"""
    if msg.get_content_type() != "message/rfc822" and msg.is_multipart():
        for p in msg.get_payload():
            yield from _walk(p)
    else:
        yield msg


def _part_filename(p):
    try:
        fn = p.get_filename()
    except Exception:
        fn = None
    return dec_header(fn) if fn else ""


def _cid_key(v):
    return (v or "").strip().strip("<>").strip().lower()


def parse(raw):
    """raw: 邮件完整原文（bytes）。永不抛异常：解析不动的部分跳过。"""
    out = Parsed()
    msg = email.message_from_bytes(raw)
    for k in ("From", "To", "Cc", "Subject", "Date"):
        out.headers[k.lower()] = dec_header(msg.get(k, ""))
    htmls, texts = [], []
    n = 0
    for p in _walk(msg):
        try:
            ct = p.get_content_type()
            disp = (p.get_content_disposition() or "").lower()
            fn = _part_filename(p)
            cid = _cid_key(p.get("Content-ID"))
            if ct in ("text/plain", "text/html") and disp != "attachment" and not fn:
                t = decode_part_text(p)
                if t.strip():
                    (htmls if ct == "text/html" else texts).append(t)
                continue
            if ct == "message/rfc822":
                subs = p.get_payload()
                payload = subs[0].as_bytes() if subs else b""
                if not fn:
                    fn = (dec_header(subs[0].get("Subject", "")) if subs else "") or "转发的邮件"
                    fn += ".eml"
            else:
                payload = p.get_payload(decode=True) or b""
            if not fn:
                ext = mimetypes.guess_extension(ct) or ""
                if ct == "text/calendar":
                    ext = ".ics"
                fn = "附件%d%s" % (n + 1, ext)
            att = Attachment(n, safe_filename(fn), ct, payload, cid, disp)
            n += 1
            out.attachments.append(att)
            if cid and ct.startswith("image/"):
                out.cids.setdefault(cid, (ct, payload))
        except Exception:
            continue
    out.html = "\n".join(htmls)
    out.text = "\n\n".join(texts)
    return out


# ------------------------------------------------------------------ CSS 清理
_CSS_ESC = re.compile(r"\\([0-9a-fA-F]{1,6})\s?|\\(.)", re.S)
_CSS_BAD = re.compile(r"expression\s*\(|javascript\s*:|vbscript\s*:|behaviou?r\s*:|-moz-binding|@import|"
                      r"image-set\s*\(|position\s*:\s*fixed|\bsrc\s*\(")
_CSS_URL = re.compile(r"url\s*\(\s*(['\"]?)(.*?)\1\s*\)", re.S)


def _css_norm(s):
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)

    def rep(m):
        if m.group(1):
            try:
                c = chr(int(m.group(1), 16))
                return c if c != "\x00" else ""
            except (ValueError, OverflowError):
                return ""
        return m.group(2)
    return _CSS_ESC.sub(rep, s).lower()


def _css_chunk_ok(chunk, allow_external):
    n = _css_norm(chunk)
    if _CSS_BAD.search(n):
        return False
    for m in _CSS_URL.finditer(n):
        u = m.group(2).strip()
        if u.startswith("data:image/"):
            continue
        if allow_external and re.match(r"https?://", u):
            continue
        return False
    if re.search(r"url\s*\(", _CSS_URL.sub("", n)):      # 没匹配完整的 url( …… 一律不要
        return False
    return True


def _css_chunks(css):
    """按 ; { } 切成片段（片段含结尾符号）；括号和引号里的符号不算（url(data:...;base64,...) 里有分号）。"""
    chunks, cur, depth, quote = [], [], 0, ""
    for ch in css:
        cur.append(ch)
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and ch in ";{}":
            chunks.append("".join(cur))
            cur = []
    if cur:
        chunks.append("".join(cur))
    return chunks


def clean_css(css, allow_external=False):
    """清理样式表文本或 style 属性值：逐条声明检查，危险的整条去掉。"""
    out = []
    for chunk in _css_chunks(css or ""):
        if _css_chunk_ok(chunk, allow_external):
            out.append(chunk)
        elif chunk.endswith("{"):
            out.append(".mc-x{")           # 选择器/at 规则本身有问题：换成无害选择器，保持括号配平
        elif chunk.endswith("}"):
            out.append("}")
    return "".join(out).replace("<", "\\3c ")


# ------------------------------------------------------------------ HTML 清理
ALLOWED_TAGS = {
    "a", "abbr", "acronym", "address", "article", "aside", "b", "bdi", "bdo", "big", "blockquote", "br", "caption",
    "center", "cite", "code", "col", "colgroup", "dd", "del", "dfn", "div", "dl", "dt", "em", "figcaption", "figure",
    "font", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "i", "img", "ins", "kbd", "li", "main", "mark",
    "nav", "ol", "p", "pre", "q", "s", "samp", "section", "small", "span", "strike", "strong", "sub", "sup", "table",
    "tbody", "td", "tfoot", "th", "thead", "time", "tr", "tt", "u", "ul", "var", "wbr",
}
VOID = {"br", "hr", "img", "col", "wbr"}
# 整段（含内容）丢掉的标签
DROP_CONTENT = {"script", "iframe", "object", "applet", "noscript", "template", "title", "svg", "math", "select",
                "textarea", "button", "frameset", "noframes", "audio", "video", "canvas", "datalist"}
# 没有结束标签的危险标签：直接忽略
DROP_VOID = {"embed", "input", "link", "meta", "base", "param", "source", "track", "area", "frame", "keygen"}
# 惰性属性白名单（href/src/style/background 另外处理）
SAFE_ATTRS = {"class", "title", "lang", "dir", "align", "valign", "bgcolor", "color", "width", "height", "border",
              "cellpadding", "cellspacing", "colspan", "rowspan", "nowrap", "bordercolor", "hspace", "vspace", "alt",
              "face", "size", "type", "start", "abbr", "scope", "span", "datetime"}
BG_TAGS = {"table", "tr", "td", "th", "body"}
MAX_STYLE_BYTES = 200_000

_PLACEHOLDER = ("data:image/svg+xml;charset=utf-8," + urllib.parse.quote(
    '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="32" viewBox="0 0 48 32">'
    '<rect width="48" height="32" fill="#e5e7eb"/><path d="M10 25l9-10 6 6 4-4 9 8z" fill="#9ca3af"/></svg>'))
_DATA_IMG = re.compile(r"data:image/(png|jpe?g|gif|webp|bmp|x-icon|svg\+xml)[;,]", re.I)


class Sanitizer(HTMLParser):
    def __init__(self, cids=None, allow_external=False):
        super().__init__(convert_charrefs=False)
        self.cids = cids or {}
        self.allow_external = allow_external
        self.out = []
        self.styles = []
        self.open = []              # [(原标签名, 输出标签名)]
        self.skip_tag = None
        self.skip_depth = 0
        self.in_style = False
        self.style_buf = []
        self.blocked = 0
        self.used_cids = set()

    # ---- 资源地址
    def resolve_img(self, url):
        """图片地址 → 可放进页面的地址；返回 (地址, 是否外部被屏蔽)。"""
        u = (url or "").strip()
        compact = re.sub(r"[\x00-\x20]", "", u)
        low = compact.lower()
        if low.startswith("cid:"):
            key = _cid_key(urllib.parse.unquote(compact[4:]))
            hit = self.cids.get(key)
            if hit:
                import base64
                self.used_cids.add(key)
                return "data:%s;base64,%s" % (hit[0], base64.b64encode(hit[1]).decode("ascii")), False
            return _PLACEHOLDER, False
        if _DATA_IMG.match(compact):
            return compact, False
        if low.startswith(("http://", "https://", "//")):
            if self.allow_external:
                return ("https:" + compact if low.startswith("//") else compact), False
            return _PLACEHOLDER, True
        return _PLACEHOLDER, False

    def safe_href(self, url):
        compact = re.sub(r"[\x00-\x20]", "", url or "")
        return compact if compact.lower().startswith(("http://", "https://", "mailto:", "tel:")) else None

    # ---- 属性
    def attrs_html(self, tag, attrs):
        parts = []
        have_target = False
        for k, v in attrs:
            k = (k or "").lower()
            v = "" if v is None else v
            if k in SAFE_ATTRS:
                parts.append(' %s="%s"' % (k, htmllib.escape(v, quote=True)))
            elif k == "style":
                cs = clean_css(v, self.allow_external).strip()
                if cs:
                    parts.append(' style="%s"' % htmllib.escape(cs, quote=True))
            elif k == "href" and tag == "a":
                h = self.safe_href(v)
                if h:
                    parts.append(' href="%s"' % htmllib.escape(h, quote=True))
                    have_target = True
            elif k == "src" and tag == "img":
                src, blocked = self.resolve_img(v)
                if blocked:
                    self.blocked += 1
                    parts.append(' class="mc-blocked" title="外部图片已屏蔽（防追踪）"')
                parts.append(' src="%s"' % htmllib.escape(src, quote=True))
                if src.startswith("http"):
                    parts.append(' referrerpolicy="no-referrer"')
            elif k == "background" and tag in BG_TAGS:
                src, blocked = self.resolve_img(v)
                if blocked:
                    self.blocked += 1
                elif src != _PLACEHOLDER:
                    parts.append(' style="background-image:url(%s)"' % htmllib.escape(src, quote=True))
        if tag == "a" and have_target:
            parts.append(' target="_blank" rel="noopener noreferrer"')
        if tag == "img" and not any(p.startswith(" src=") for p in parts):
            parts.append(' src="%s"' % _PLACEHOLDER)
        return "".join(parts)

    # ---- 解析回调
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        if tag == "style":
            self.in_style = True
            self.style_buf = []
            return
        if tag in DROP_CONTENT:
            self.skip_tag, self.skip_depth = tag, 1
            return
        if tag in DROP_VOID:
            return
        out_tag = "div" if tag == "body" else tag
        if tag != "body" and tag not in ALLOWED_TAGS:
            return                                  # 不认识的标签：去掉标签、留内容
        self.out.append("<%s%s>" % (out_tag, self.attrs_html(tag, attrs)))
        if tag not in VOID:
            self.open.append((tag, out_tag))

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if self.skip_depth <= 0:
                    self.skip_tag = None
            return
        if tag == "style":
            if self.in_style:
                self.styles.append("".join(self.style_buf))
            self.in_style = False
            return
        if tag in VOID or not any(t == tag for t, _ in self.open):
            return
        while self.open:
            t, o = self.open.pop()
            self.out.append("</%s>" % o)
            if t == tag:
                break

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.skip_tag:
            return
        if self.in_style:
            self.style_buf.append(data)
        else:
            self.out.append(htmllib.escape(data, quote=False))

    def handle_entityref(self, name):
        if not self.skip_tag and not self.in_style and re.fullmatch(r"[A-Za-z0-9]{1,32}", name):
            self.out.append("&%s;" % name)

    def handle_charref(self, name):
        if not self.skip_tag and not self.in_style and re.fullmatch(r"(?:[0-9]{1,7}|[xX][0-9a-fA-F]{1,6})", name):
            self.out.append("&#%s;" % name)

    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass

    def finish(self):
        try:
            self.close()
        except Exception:
            pass
        while self.open:
            self.out.append("</%s>" % self.open.pop()[1])
        if self.in_style:
            self.styles.append("".join(self.style_buf))
        return "".join(self.out)


def sanitize_html(src, cids=None, allow_external=False):
    """返回 (body_html, style_text, 被屏蔽的外部图片数, 用到的 cid 集合)。"""
    s = Sanitizer(cids, allow_external)
    try:
        s.feed(src)
    except Exception:
        pass
    body = s.finish()
    css = clean_css("\n".join(s.styles), allow_external)[:MAX_STYLE_BYTES]
    return body, css, s.blocked, s.used_cids


BASE_CSS = ("html,body{margin:0;background:#fff}body{padding:14px 16px;font:14px/1.6 -apple-system,BlinkMacSystemFont,"
            "'PingFang SC','Hiragino Sans GB','Microsoft YaHei',Arial,sans-serif;color:#1f2430;overflow-wrap:anywhere}"
            "img{max-width:100%;height:auto}a{color:#1a56db}table{max-width:100%}pre{white-space:pre-wrap}"
            "blockquote{margin:8px 0 8px 4px;padding-left:12px;border-left:3px solid #d5d9e0;color:#4b5565}"
            ".mc-blocked{background:#e5e7eb;outline:1px dashed #9ca3af}")


def wrap_document(body, css, allow_external):
    img = "data: https: http:" if allow_external else "data:"
    csp = "default-src 'none'; img-src %s; style-src 'unsafe-inline'; font-src data:" % img
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" content="%s"><base target="_blank">'
            '<style>%s</style><style>%s</style></head><body>%s</body></html>') % (csp, BASE_CSS, css, body)


# ------------------------------------------------------------------ 对外接口
def render(raw, allow_external=False):
    """完整原文 → 给前端的字典。"""
    p = parse(raw)
    html_doc, blocked, used = None, 0, set()
    if p.html.strip():
        body, css, blocked, used = sanitize_html(p.html, p.cids, allow_external)
        html_doc = wrap_document(body, css, allow_external)
    atts = []
    for a in p.attachments:
        if a.cid and a.cid in used and a.disposition != "attachment":
            continue                                  # 只是正文里的内嵌图片，不当附件列
        atts.append({"index": a.index, "filename": a.filename, "content_type": a.content_type, "size": len(a.payload)})
    return {"html": html_doc, "text": p.text, "blocked_images": blocked, "external_images": bool(allow_external),
            "attachments": atts, "to": p.headers.get("to", ""), "cc": p.headers.get("cc", "")}


def get_attachment(raw, index):
    """按下标取附件，返回 (文件名, 内容 bytes)；下标不存在返回 None。"""
    for a in parse(raw).attachments:
        if a.index == index:
            return a.filename, a.payload
    return None
