# -*- coding: utf-8 -*-
"""可选功能：把邮件正文翻译成简体中文（阿里云百炼 Qwen，北京地域）。

隐私：正文会发送到阿里云百炼。key 由用户在设置里填写，存在钥匙串（MailCrawler / qwen_key），不写进配置文件。
译文缓存在数据目录的 translations.json，同一封只译一次。
"""
import html
import json
import re
import threading
from html.parser import HTMLParser
from urllib import error, request

from . import paths, secrets
from .mailbox import ssl_ctx

QWEN_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
MODELS = ("qwen-flash", "qwen-plus")      # 前一个便宜；不可用时退到后一个
MAX_CHARS = 12000                         # 太长的邮件只译前面这些
PROMPT = ("你是邮件翻译。把用户给的邮件正文翻译成简体中文，给中国大学生看。"
          "保留原来的分段和列表；日期、时间、金额、地点、人名、链接、邮箱地址原样保留（日期后可括号补中文星期）；"
          "课程名、专有名词第一次出现时写成「中文（English）」。只输出译文，不要解释。")

_lock = threading.Lock()


class _Text(HTMLParser):
    """HTML → 纯文本：块级标签换行，丢掉 script/style。"""
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
        if tag == "li":
            self.out.append("• ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(s):
    p = _Text()
    p.feed(s or "")
    t = html.unescape("".join(p.out))
    t = re.sub(r"[ \t ]+", " ", t)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", t).strip()


def _cache_path():
    return paths.data_dir() / "translations.json"


def _load():
    try:
        return json.loads(_cache_path().read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def cached(uid):
    with _lock:
        return _load().get(str(uid))


def _save(uid, text):
    with _lock:
        d = _load()
        d[str(uid)] = text
        tmp = _cache_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), "utf-8")
        tmp.replace(_cache_path())


def _call(model, key, text):
    body = {"model": model, "messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": text}]}
    req = request.Request(QWEN_URL, data=json.dumps(body).encode("utf-8"),
                          headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with request.urlopen(req, timeout=120, context=ssl_ctx()) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()


def translate(uid, subject, text):
    """返回 (译文, 是否来自缓存)。失败抛 RuntimeError（中文说明）。"""
    hit = cached(uid)
    if hit:
        return hit, True
    key = secrets.get_secret("qwen_key")
    if not key:
        raise RuntimeError("还没设置翻译 key：点右上角 ⚙ 设置 →「翻译（可选）」，填阿里云百炼的 API key")
    src = ("主题：" + subject + "\n\n" if subject else "") + (text or "").strip()
    truncated = len(src) > MAX_CHARS
    src = src[:MAX_CHARS]
    last = None
    for model in MODELS:
        try:
            out = _call(model, key, src)
            if truncated:
                out += "\n\n（邮件太长，只翻译了前 %d 个字符）" % MAX_CHARS
            _save(uid, out)
            return out, False
        except error.HTTPError as e:
            last = "HTTP %s" % e.code
            if e.code in (401, 403):
                break
        except Exception as e:
            last = str(e)
    raise RuntimeError("翻译失败：%s" % last)
