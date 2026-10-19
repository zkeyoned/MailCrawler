# -*- coding: utf-8 -*-
"""非敏感配置：邮箱账号、同步日期范围、自动同步开关等（存 config.json，明文无妨）。

密码和 Jev API key 不在这里，见 secrets.py（系统钥匙串）。
"""
import json
import threading

from .paths import config_path

DEFAULTS = {
    "email": "",            # 完整邮箱地址（登录名）
    "since": "",            # 同步起始日期 YYYY-MM-DD，空 = 不限
    "before": "",           # 同步截止日期 YYYY-MM-DD（含当天），空 = 不限
    "auto_sync": False,     # 新邮件自动同步开关（默认关）
    "auto_sync_minutes": 30,  # 自动同步间隔（分钟）
    "preferences": "",      # 「我的偏好」：多行文本，拼进「重要程度」那道题的 instructions（只存本机）
    "ignore_senders": "",   # 「总是忽略的发件人」：每行一个关键字，命中的邮件直接 1 星、不调 Jev（只存本机）
}

_lock = threading.RLock()   # save() 内部会回调 load()，必须可重入
_cache = None


def load():
    global _cache
    with _lock:
        if _cache is None:
            cfg = dict(DEFAULTS)
            p = config_path()
            if p.exists():
                try:
                    cfg.update({k: v for k, v in json.loads(p.read_text("utf-8")).items() if k in DEFAULTS})
                except Exception:
                    pass
            _cache = cfg
        return dict(_cache)


def save(**kwargs):
    global _cache
    with _lock:
        cfg = load()
        cfg.update({k: v for k, v in kwargs.items() if k in DEFAULTS})
        p = config_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(p)
        _cache = cfg
        return dict(cfg)


def clear():
    """退出登录时清空配置（邮箱地址也不留）。"""
    global _cache
    with _lock:
        p = config_path()
        if p.exists():
            p.unlink()
        _cache = None
