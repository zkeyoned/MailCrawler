# -*- coding: utf-8 -*-
"""敏感信息存取：密码和 Jev API key 一律放系统钥匙串（keyring 库），不落盘。

macOS → Keychain / Windows → Credential Locker / Linux → Secret Service。
钥匙串服务名固定为 "MailCrawler"，条目按用户名区分：imap（邮箱密码）、jev（API key）。
"""
import threading

import keyring

from . import config

SERVICE = "MailCrawler"
_lock = threading.Lock()
_cache = {}


def set_secret(username, value):
    with _lock:
        keyring.set_password(SERVICE, username, value)
        _cache.pop(username, None)


def get_secret(username):
    """取密码；没有返回 None。"""
    with _lock:
        if username not in _cache:
            try:
                _cache[username] = keyring.get_password(SERVICE, username)
            except Exception:
                _cache[username] = None
        return _cache[username]


def imap_password():
    return get_secret("imap")


def jev_key():
    return get_secret("jev")


def save_account(email, password, key):
    set_secret("imap", password)
    set_secret("jev", key)
    config.save(email=email)


def delete_secret(username):
    with _lock:
        try:
            keyring.delete_password(SERVICE, username)
        except Exception:
            pass
        _cache.pop(username, None)


def clear_account():
    """退出登录：删掉钥匙串里的两项 + 清空配置。"""
    with _lock:
        for username in ("imap", "jev", "qwen_key"):
            try:
                keyring.delete_password(SERVICE, username)
            except Exception:
                pass
            _cache.pop(username, None)
    config.clear()


def ready():
    """是否已登录（配置里有邮箱、钥匙串里两样都在）。"""
    if not config.load().get("email"):
        return False
    return bool(imap_password() and jev_key())
