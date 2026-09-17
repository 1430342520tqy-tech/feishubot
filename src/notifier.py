#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞书推送的**唯一出口**。

职责：告诉调用方“该往哪个地址发”（`resolve_webhook`），以及“真的发出去”（`send`）。
所有告警都从这里出去 —— 这是“绝不静默丢弃”这条铁律的落地点，所以只能有一个地方能发。

配置来源：**只从环境变量（含 `.env`）读** `FEISHU_WEBHOOK`。详见 `config.py` 头部规则。
本模块**不缓存任何配置**，每次调用现取。
"""
import json
import os

import requests


def resolve_webhook():
    """该往哪个地址发 —— 从环境变量（含 `.env`）读 `FEISHU_WEBHOOK`。

    ⚠️ **静默开关**：下面两种情况一律返回空串（**一条都不发**）：
      1. 命令行带 `--selftest-` —— 自检绝不能往外发飞书（项目硬规矩）；
      2. 环境变量 `SIGNALBOT_SILENT` 非空 —— 供 `tools/` 里的离线工具与自检运行器使用。

    为什么把开关放在这里：这是**唯一出口**，放在出口上就能保证“不管哪个分支忘了隔离，
    都不会发出去”；靠每个分支自己设变量是不可靠的。
    """
    if (os.environ.get("SIGNALBOT_SILENT") or "").strip():
        return ""
    import sys
    for _a in sys.argv:
        if str(_a).startswith("--selftest-"):
            return ""
    return (os.environ.get("FEISHU_WEBHOOK") or "").strip()


def send(hook, text, log=None):
    """真正发出去。返回 `(是否已发, 说明)`。

    `log` 是可选的日志回调 —— 传进来是为了保持主程序原来的日志格式与落盘位置。
    行为与抽取前逐字对齐：成功不打印；失败/非 0 返回码各打印一行。
    """
    if not hook:
        return False, "没有配置 webhook"
    try:
        r = requests.post(hook, json={"msg_type": "text", "content": {"text": text}},
                          timeout=20).json()
    except Exception as e:
        if log:
            log("   webhook 失败: " + str(e)[:120])
        return False, str(e)[:120]
    code = (r or {}).get("code")
    if code not in (0, None):
        if log:
            log("   webhook 返回: " + json.dumps(r, ensure_ascii=False)[:160])
        return False, "code=%s" % code
    return True, "ok"
