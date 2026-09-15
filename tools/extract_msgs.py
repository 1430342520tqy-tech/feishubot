#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从生产日志里抽取【真实群消息】，为建立"标准答案集"(golden set) 做准备。
只读，不写生产任何文件；输出到 /tmp。
"""
import os, re, json, collections

LOG = "/home/ubuntu/signal-bot/v21/run.log"
OUT = "/tmp/golden_msgs.json"
# ⚠️ 日志已被 logrotate 轮转过：历史在 run.log.1 / 更早的 bak 里，必须一起读，
#    否则标准答案集只剩轮转之后那几条（第一次跑就踩到了）。
LOG_FILES = ["/home/ubuntu/signal-bot/v21/run.log"]
_d = "/home/ubuntu/signal-bot/v21"
for _f in sorted(os.listdir(_d)):
    if _f.startswith("run.log.") and _f not in ("run.log",):
        LOG_FILES.append(os.path.join(_d, _f))

GROUPS = ["机器人开单通知", "暴富龙", "UA-nurseneil2", "黄金mansoor"]
# 我们自己发的通知前缀（不算真实群消息）
SELF = ("【跟单机器人】", "【信号·", "【博主指令】", "【止盈成交", "【已结单", "【已开单",
        "【指令】", "【待确认】", "【机器人状态】", "【挂单情况】", "【持仓", "【你的持仓】")

RE_FOUND = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[([^\]]+)\] 发现新消息 \| 发出=([^|]+)\| ?(.*)$")
RE_SIGKW = ("long", "Long", "LONG", "short", "Short", "SHORT", "Entry", "CMP",
            "做多", "做空", "止损", "止盈", "平仓", "减仓", "close", "Closed", "TP", "SL")

rows = []
for _lf in LOG_FILES:
    if not os.path.exists(_lf):
        continue
    for line in open(_lf, encoding="utf-8", errors="replace"):
        m = RE_FOUND.match(line.rstrip("\n"))
        if not m:
            continue
        ts, grp, sent, txt = m.group(1), m.group(2), m.group(3).strip(), m.group(4).strip()
        if grp not in GROUPS:
            continue
        if not txt or txt.startswith(SELF):
            continue
        # 是否像信号（含关键词）—— 与机器人主循环同一套判据
        if not any(k in txt for k in RE_SIGKW):
            continue
        rows.append({"ts": ts, "group": grp, "sent": sent, "text": txt})

# 去重（同一条消息可能被抓到多次）
seen, uniq = set(), []
for r in rows:
    k = (r["group"], r["text"][:120])
    if k in seen:
        continue
    seen.add(k)
    uniq.append(r)

print("抽到候选信号消息 %d 条（去重前 %d）" % (len(uniq), len(rows)))
print("按群分布：", dict(collections.Counter(r["group"] for r in uniq)))
print()
print("=" * 78)
for i, r in enumerate(uniq, 1):
    print("[%2d] %s  %s" % (i, r["ts"][5:16], r["group"]))
    print("     %s" % r["text"][:220].replace("\n", " "))
json.dump(uniq, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print()
print("已写入 %s（%d 条）" % (OUT, len(uniq)))
