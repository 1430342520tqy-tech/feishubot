#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证飞书自建应用凭据（`FEISHU_APP_ID` / `FEISHU_APP_SECRET`）是否有效。

只读：只调一次 `tenant_access_token` 接口，**不打印任何密钥、不打印 token 本体**（只打印长度）。

用法（服务器上、项目根目录）：
    venv/bin/python tools/feishu_app_check.py

凭据来源：**只从 `.env` / 环境变量读**（见 `src/config.py` 头部规则）。
"""
import json
import os
import sys
import urllib.request

BASE = os.environ.get("SIGNAL_BOT_BASE", "/home/ubuntu/signal-bot")
for _p in (os.path.join(BASE, "src"), BASE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import config
except Exception as e:                                   # 拿不到 config 就直接读环境变量
    config = None
    print("[warn] 无法加载 config（%s）→ 直接读环境变量" % str(e)[:60])


def main():
    creds = config.app_creds() if config else {
        "app_id": (os.environ.get("FEISHU_APP_ID") or "").strip(),
        "app_secret": (os.environ.get("FEISHU_APP_SECRET") or "").strip(),
    }
    aid, sec = creds.get("app_id", ""), creds.get("app_secret", "")
    if not aid or not sec:
        print("❌ 没拿到凭据：请在项目根目录的 .env 里填 FEISHU_APP_ID / FEISHU_APP_SECRET")
        return 2
    body = json.dumps({"app_id": aid, "app_secret": sec}).encode()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode())
    except Exception as e:
        print("❌ 请求失败：%s" % str(e)[:120])
        return 1
    code = d.get("code")
    print("app_id=%s  code=%s  msg=%s  token_len=%s"
          % (aid, code, str(d.get("msg"))[:40], len(str(d.get("tenant_access_token") or ""))))
    if code == 0:
        print("✅ 凭据有效")
        return 0
    print("❌ 凭据无效（code=%s）—— 若刚重置过密钥，请确认 .env 里是新的" % code)
    return 1


if __name__ == "__main__":
    sys.exit(main())
