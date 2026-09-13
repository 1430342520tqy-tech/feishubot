# -*- coding: utf-8 -*-
"""最小输出：只验证飞书自建应用凭据是否有效（不打印任何大字段）"""
import json, urllib.request

APPS = [
    ("cli_aa12b548bdf85cc0", "l5xGGlQkvPoCVpu0dPpC6dmkTLocnZM2"),
]

for aid, sec in APPS:
    body = json.dumps({"app_id": aid, "app_secret": sec}).encode()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode())
        print("app_id=%s  code=%s  msg=%s  token_len=%s"
              % (aid, d.get("code"), str(d.get("msg"))[:40],
                 len(str(d.get("tenant_access_token") or ""))))
    except Exception as e:
        print("app_id=%s  请求失败: %s" % (aid, str(e)[:80]))
