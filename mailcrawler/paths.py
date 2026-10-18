# -*- coding: utf-8 -*-
"""平台相关的路径：应用数据目录（配置、数据库、日志都放这里，绝不进仓库）。"""
import os
import sys
from pathlib import Path

APP_NAME = "MailCrawler"


def data_dir():
    """跨平台的应用数据目录。

    macOS:  ~/Library/Application Support/MailCrawler
    Windows: %APPDATA%\\MailCrawler
    Linux:  ~/.local/share/mailcrawler
    环境变量 MAILCRAWLER_DATA_DIR 可覆盖（调试用）。
    """
    override = os.environ.get("MAILCRAWLER_DATA_DIR")
    if override:
        d = Path(override)
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / APP_NAME
    elif sys.platform.startswith("win"):
        d = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME
    else:
        d = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME.lower()
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path():
    return data_dir() / "config.json"


def db_path():
    return data_dir() / "mailcrawler.sqlite"


def lock_path():
    return data_dir() / "app.lock"


def log_path():
    return data_dir() / "mailcrawler.log"
