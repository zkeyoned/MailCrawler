#!/bin/bash
# macOS 打包脚本：生成图标并打出 MailCrawler.app
# 用法：./build.sh        （产物在 dist/MailCrawler.app）
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "先建虚拟环境：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pyinstaller pillow"
  exit 1
fi

.venv/bin/python scripts/make_icon.py
.venv/bin/pyinstaller packaging/mailcrawler.spec --noconfirm
# 个人版：用本机固定证书签名（钥匙串里的「MailCrawler Local Signing」），
# 签名身份不变，钥匙串的「始终允许」重新打包后依然有效，不会每次都弹框
if security find-identity -p codesigning | grep -q "MailCrawler Local Signing"; then
  codesign --force --deep --sign "MailCrawler Local Signing" dist/MailCrawler.app
fi
echo "✅ 完成：dist/MailCrawler.app"
echo "首次打开如被 Gatekeeper 拦：右键 → 打开。要分发给别人前先签名/公证（见 README）。"
