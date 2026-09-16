#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
空窗回补分析（只读）—— 2026-09-16 浏览器→API 切换留下的那段空窗里，到底漏了什么。

背景（实测，见 docs/待办与未解决问题-2026-09-15.md 第 0.16.18 节）：
  API 模式首次启动时游标只从"10 分钟前"开始、不回补更早历史，而浏览器模式的最后进度更早：
      机器人开单通知 09-16 16:00 ｜ 暴富龙 09-15 19:19 ｜ UA-nurseneil2 09-16 16:00 ｜ 黄金mansoor 09-16 13:52
  所以这些时间点到 09-17 00:00:36（API 起始游标）之间的消息**没有被处理**。

本脚本做什么（**只读**）：
  · 用官方 API 按时间范围把那段时间的消息拉回来（含卡片文字、图片 key）；
  · 对每条消息用**机器人自己的真实规则**复算：剥发送者前缀 → 跳过机器人自己的通知（SELF_MARKS）
    → 跑 `fast_parse`（生产在用的本地解析器）；
  · 输出"那段时间里有哪些像信号的消息、解析结果是什么"，并把原文落盘到 /tmp 供你逐条核对；
  · **不下单、不推送、不写机器人的任何文件**；并在跑前跑后比对生产状态，结论写在最后。

不做的事（避免用猜测冒充结论）：
  · 默认**不调用 AI 解析**、**不读图**（读图要花 AI 额度和时间）；要跑加 --charts。
    所以本报告的"漏了哪些信号"是**下界**：只含本地正则能认出来的那些。

用法：
    venv/bin/python backfill_analyze.py                 # 4 个群都用文档里记录的空窗区间
    venv/bin/python backfill_analyze.py --group 暴富龙     # 只看一个群
    venv/bin/python backfill_analyze.py --charts        # 顺带对带图消息读图（慢、耗额度）
"""
import os
import sys
import json
import time
import datetime

BASE = "/home/ubuntu/signal-bot"
RUN = BASE + "/v21"
OUT = "/tmp/backfill"
CST = datetime.timezone(datetime.timedelta(hours=8))
sys.path.insert(0, BASE)

# 文档里记录的真实空窗区间（浏览器模式最后进度 → API 起始游标）
WINDOWS = {
    "机器人开单通知": ("2026-09-16 16:00:00", "2026-09-17 00:10:39"),
    "暴富龙":         ("2026-09-15 19:19:00", "2026-09-17 00:00:36"),
    "UA-nurseneil2":  ("2026-09-16 16:00:00", "2026-09-17 00:00:36"),
    "黄金mansoor":    ("2026-09-16 13:52:00", "2026-09-17 00:00:36"),
}


def _ts(s):
    return int(datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
               .replace(tzinfo=CST).timestamp())


def _fingerprint():
    """生产侧最小指纹：持仓数 + 日志行数（机器人自己在写，允许增，不允许被本脚本改）。"""
    try:
        d = json.load(open(RUN + "/state.json", encoding="utf-8")) or {}
        n_open = len(d.get("open") or {})
    except Exception as e:
        n_open = "ERR:%s" % type(e).__name__
    try:
        with open(RUN + "/run.log", "rb") as f:
            n_lines = sum(1 for _ in f)
    except Exception:
        n_lines = -1
    return {"open": n_open, "log_lines": n_lines}


def main():
    args = sys.argv[1:]
    want_charts = "--charts" in args
    only = None
    if "--group" in args:
        i = args.index("--group")
        if i + 1 < len(args):
            only = args[i + 1]

    import feishu_api as fa
    import dryrun_bot2 as bot          # 只借它的纯函数：strip_sender_prefix / fast_parse / read_chart

    os.makedirs(OUT, exist_ok=True)
    # ⚠️ 必须把模块级路径**全部**改到 /tmp 再用它：
    #    · read_chart 会往 RUN 写"读图中间产物"（imgmerge 自检里专门为此重定向过 RUN）
    #    · log() 会往 LOGF 追加（否则本脚本的分析过程会污染生产 run.log）
    #    · notify() 读 NOTIFY_CFG —— 指到不存在的路径 → 无论如何都发不出飞书
    bot.RUN = OUT + "/run"
    bot.IMGDIR = OUT + "/imgs"
    bot.LOGF = OUT + "/run.log"
    bot.TRADES = OUT + "/trades.jsonl"
    bot.STATE = OUT + "/state.json"
    bot.RUNTIME = OUT + "/runtime_config.json"
    bot.NOTIFY_CFG = OUT + "/notify.json"
    os.makedirs(bot.IMGDIR, exist_ok=True)
    os.makedirs(bot.RUN, exist_ok=True)
    before = _fingerprint()
    print("=" * 78)
    print("空窗回补分析（只读）  开始 %s" % datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"))
    print("生产指纹(前)：持仓=%s ｜ run.log 行数=%s" % (before["open"], before["log_lines"]))
    print("读图：%s（--charts 才读，会花 AI 额度）" % ("开" if want_charts else "关"))
    print("=" * 78)

    names = [only] if only else list(WINDOWS)
    ids = fa.resolve_chat_ids(names)
    selftest_marks = bot._self_marks()
    total_msgs = 0
    summary = []

    for g in names:
        cid = ids.get(g)
        if not cid:
            print("\n### %s：API 里找不到这个群，跳过" % g)
            continue
        t0, t1 = WINDOWS.get(g, (None, None))
        if not t0:
            print("\n### %s：没有登记空窗区间，跳过" % g)
            continue
        print("\n" + "#" * 78)
        print("### 群「%s」空窗 %s → %s" % (g, t0, t1))
        items, err = fa.fetch_messages(cid, start_time=_ts(t0), end_time=_ts(t1),
                                       page_size=50, max_pages=20, asc=True)
        if err:
            print("    拉取失败：%s" % err)
            summary.append((g, t0, t1, -1, -1, -1))
            continue
        items = [it for it in (items or [])
                 if _ts(t0) <= int(it.get("create_time") or 0) / 1000.0 <= _ts(t1)]
        total_msgs += len(items)
        n_self, n_img_only, n_parsed, n_chart = 0, 0, 0, 0
        rows = []
        for it in items:
            when = datetime.datetime.fromtimestamp(
                int(it.get("create_time") or 0) / 1000.0, CST).strftime("%m-%d %H:%M:%S")
            raw_txt = fa.msg_text_of(it) or ""
            txt = bot.strip_sender_prefix(raw_txt)
            keys = fa.msg_images_of(it) or []
            if any(m in txt for m in selftest_marks):
                n_self += 1
                continue
            plan = None
            try:
                plan = bot.fast_parse(txt) if txt.strip() else None
            except Exception as e:
                plan = {"_err": str(e)[:80]}
            rec = {"when": when, "text": txt, "imgs": len(keys), "plan": plan}
            if (not txt.strip()) and keys:
                n_img_only += 1
            if plan:
                n_parsed += 1
                rec["_hit"] = True
            # 带图消息：下载原图（图片通道），要 --charts 才读图
            if keys:
                paths = []
                for k in keys:
                    p = fa.download_image(it.get("message_id"), k, bot.IMGDIR)
                    if p:
                        paths.append(p)
                rec["imgs_path"] = paths
                if want_charts and paths:
                    try:
                        rec["chart"] = bot.read_chart(paths[0])
                        n_chart += 1
                    except Exception as e:
                        rec["chart_err"] = str(e)[:120]
            rows.append(rec)

        fn = os.path.join(OUT, "%s.jsonl" % g)
        with open(fn, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        print("    拉到 %d 条（空窗内）｜ 机器人自己的通知 %d 条已排除 ｜ 纯图片消息 %d 条 ｜ 本地解析器认出 %d 条 ｜ 读图 %d 张"
              % (len(items), n_self, n_img_only, n_parsed, n_chart))
        print("    原文已落盘：%s" % fn)
        if "--list" in args:
            print("    ---- 逐条（越靠上越接近窗口起点＝可能是「已处理过」的那条）----")
            for r in rows:
                print("      · %s ｜ 图%d ｜ %s" % (r["when"], r["imgs"],
                                                (r["text"] or "(无文字)").replace("\n", " ")[:58]))
        for r in rows:
            if r.get("_hit"):
                p = r["plan"] or {}
                print("      ⚠️ %s ｜ %s" % (r["when"], (r["text"] or "").replace("\n", " ")[:70]))
                print("         解析：%s" % json.dumps(p, ensure_ascii=False)[:220])
            if r.get("chart") is not None or r.get("chart_err"):
                print("      🖼 %s ｜ 图 %s ｜ 读图结果：%s"
                      % (r["when"], r.get("imgs_path"), json.dumps(r.get("chart"), ensure_ascii=False)[:260]
                         if r.get("chart") is not None else ("读图失败 " + r.get("chart_err", ""))))
        summary.append((g, t0, t1, len(items), n_self, n_parsed))

    after = _fingerprint()
    print("\n" + "=" * 78)
    print("汇总（本地正则解析器口径，是**下界**；未跑 AI 解析；读图 %s）"
          % ("已开" if want_charts else "未开——带图消息的信号没被读出来，属已知缺口"))
    for g, t0, t1, n, n_self, n_parsed in summary:
        print("  %-14s %s → %s ：消息 %s ｜ 排除自身通知 %s ｜ 认出信号 %s"
              % (g, t0, t1, n, n_self, n_parsed))
    print("\n生产指纹(后)：持仓=%s ｜ run.log 行数=%s" % (after["open"], after["log_lines"]))
    if after["open"] == before["open"]:
        print("隔离结论：PASS ✅ 持仓数未变（本脚本只读，不写机器人任何文件）")
    else:
        print("隔离结论：FAIL ❌ 持仓数变了：%s → %s" % (before["open"], after["open"]))
    print("run.log 行数变化 %+d（机器人自己的心跳，属正常）" % (after["log_lines"] - before["log_lines"]))
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
