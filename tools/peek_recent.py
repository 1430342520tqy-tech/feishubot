#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把某个群里**最近的真实消息**原样读回来（只读，官方 API），用来核对"机器人到底推了什么"。

为什么需要它：机器人日志里的 `[通知]` 行为了不刷屏，**把正文截断到 200 字**
（`log("[通知] " + text.replace("\\n", " | ")[:200])`）—— 于是像"审批推送里有没有耗时行"
这种事，从日志里**看不到完整原文**。那就别猜，直接把群里那条消息读回来。

用法：
    venv/bin/python peek_recent.py                      # 「机器人开单通知」最近 5 条
    venv/bin/python peek_recent.py 暴富龙 3              # 指定群 + 条数
    venv/bin/python peek_recent.py 机器人开单通知 5 120   # 第 3 个参数=回看多少分钟（默认 120）
"""
import os
import sys
import time
import datetime

# 项目根目录：环境变量优先（见 src/config.py）。
# ⚠️ 生产机上 .py 平铺在根目录，git 仓库里在 src/ —— 两个位置都加，两种布局都能跑。
_SB_BASE = os.environ.get("SIGNAL_BOT_BASE", "/home/ubuntu/signal-bot")
sys.path.insert(0, os.path.join(_SB_BASE, "src"))
sys.path.insert(0, _SB_BASE)
import feishu_api as fa          # noqa: E402

CST = datetime.timezone(datetime.timedelta(hours=8))


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "机器人开单通知"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    back_min = int(sys.argv[3]) if len(sys.argv) > 3 else 120

    ids = fa.resolve_chat_ids([name])
    cid = ids.get(name)
    if not cid:
        print("找不到群「%s」；可用群数量=%d" % (name, len(ids)))
        return 2

    start_s = int(time.time() - back_min * 60)
    items, err = fa.fetch_messages(cid, start_time=start_s, page_size=50, max_pages=3, asc=True)
    print("群「%s」最近 %d 分钟拉到 %d 条（err=%s）" % (name, back_min, len(items or []), err))
    for it in (items or [])[-n:]:
        try:
            ms = int(it.get("create_time") or 0)
            when = datetime.datetime.fromtimestamp(ms / 1000.0, CST).strftime("%m-%d %H:%M:%S")
        except Exception:
            when = "?"
        txt = fa.msg_text_of(it) or ""
        imgs = fa.msg_images_of(it) or []
        print("\n---- %s ｜ 图 %d 张 ｜ 全文 %d 字 ----" % (when, len(imgs), len(txt)))
        print(txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
