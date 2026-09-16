#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
联调：往「机器人开单通知」群里发一条**测试消息**，验证"发开单消息有反应"这条链路。

为什么用 webhook 而不是机器人自己发：
  · 用户授权的是**用户身份**令牌，范围里**没有** `im:message.send_as_user`，机器人不能以你的身份发言；
  · 机器人自带的 `notify()` 发出去的消息带【跟单机器人】等前缀，会被自己的 SELF_MARKS 过滤掉
    （这是**故意设计**，防止自环），所以走 notify() 测不出"能不能解析外部信号"。
  · 因此直接用 notify.json 里的自定义机器人 webhook 发一条**普通文本**，它看起来就是一条普通群消息，
    走的是和真实博主信号完全相同的处理路径。

安全约定：
  · **绝不打印 webhook 地址**（它带密钥），只打印飞书返回的 code/msg；
  · 默认 `--dry-run`：只读出 webhook 是否存在、并打印将要发送的文本，不发；
  · 只有显式加 `--send` 才真的发。
  · 测试消息带「【联调测试】」字样，且**刻意不含任何 SELF_MARKS 前缀**（否则会被自环防护忽略）。
    机器人是影子模式 + 每单需你审批 → 最坏情况也只是推一条待确认，不会下任何真单。

用法：
    venv/bin/python send_test_msg.py                    # 只看不发
    venv/bin/python send_test_msg.py --send             # 真发（用内置默认测试文本）
    venv/bin/python send_test_msg.py --send --textfile /tmp/t.txt   # 真发（文本从文件读，推荐）
    venv/bin/python send_test_msg.py --send --text "…"  # 真发（单行无空格才可靠）
"""
import os
import sys
import json
import requests

BASE = "/home/ubuntu/signal-bot"
NOTIFY_CFG = BASE + "/notify.json"

DEFAULT_TEXT = "【联调测试】UNI 做多 CMP 6.719 止损 6.39 止盈 7.180 / 8.216 / 9.302"


def main():
    args = sys.argv[1:]
    send = "--send" in args
    text = DEFAULT_TEXT
    if "--text" in args:
        i = args.index("--text")
        if i + 1 < len(args):
            text = args[i + 1]
    # ⚠️ 2026-09-17 实测教训：从 Windows 用 ssh 传带空格的中文参数时，
    #    PowerShell 会把内层引号吃掉 → bash 按空格拆词 → 只传进来第一个词，
    #    结果往群里发了一条被截断的"【联调测试2】UNI"（无害，但白测一轮）。
    #    所以文本一律走**文件**，不再走命令行参数。
    if "--textfile" in args:
        i = args.index("--textfile")
        if i + 1 < len(args):
            with open(args[i + 1], encoding="utf-8") as f:
                text = f.read().strip()

    try:
        cfg = json.load(open(NOTIFY_CFG, encoding="utf-8"))
    except Exception as e:
        print("读不到 %s：%s" % (NOTIFY_CFG, e))
        return 2
    hook = cfg.get("feishu_webhook")
    print("notify.json 里有 feishu_webhook：%s" % ("是（地址不打印）" if hook else "否"))
    if not hook:
        return 2

    print("将要发送的文本（%d 字）：\n  %s" % (len(text), text))
    # 自查：文本里绝不能含机器人自己的通知前缀，否则会被自环防护忽略，测不出东西
    SELFMARKS = ["【跟单机器人】", "【信号·", "【手工仓护栏】", "【机器人告警】", "【取消息】",
                 "【对账闸门】", "【失联看门狗】", "【博主指令】", "【指令】", "【持仓情况】"]
    bad = [m for m in SELFMARKS if m in text]
    if bad:
        print("⚠️ 文本含自环前缀 %s → 会被机器人忽略，换了再发" % bad)
        return 3
    print("自环前缀自查：通过（不会被当成机器人自己的通知而忽略）")

    if not send:
        print("\n[dry-run] 未发送。要真发请加 --send")
        return 0

    r = requests.post(hook, json={"msg_type": "text", "content": {"text": text}}, timeout=20)
    try:
        j = r.json()
    except Exception:
        j = {"raw": r.text[:120]}
    print("\n已发送：HTTP %s ｜ 飞书返回 code=%s msg=%s"
          % (r.status_code, j.get("code"), j.get("msg") or j.get("StatusCode")))
    return 0 if r.status_code == 200 and j.get("code") in (0, None) else 1


if __name__ == "__main__":
    sys.exit(main())
