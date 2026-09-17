#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把归档图片分成两类（用户 2026-09-17 定的判据）：
    · **signal**（开单信号图）→ 放到 3_signal_开单信号，需要处理
    · **其他一切** → 放到 1_忽略_非开单信号，一律忽略不推送
      为方便你复核，CSV 里仍细分是哪种"其他"：pnl(晒收益) / status(走势状态) /
      tweet(推文群聊截图) / kline(纯K线无标注) / news(新闻公告提示) / other / unknown

用法：
    venv/bin/python classify_charts.py --dir chart_archive/images --csv classify.csv \
        [--move] [--skip-done] [--limit N]
设计要点：
  · 一张图一次调用；图片先缩到 ≤820px（够分类、且省 token 与时间）；
  · --skip-done：CSV 里已有的文件名不再重跑（断点续跑）；
  · 结果全部写 CSV（文件名/类别/一句话理由/是否有仓位工具/是否含账户字样）→ 可复核；
  · --move 才真移动文件；失败或看不清 → kind=unknown（归"忽略"，绝不硬塞进 signal）。
"""
import os
import io
import re
import csv
import sys
import json
import time
import base64
import shutil
import requests
from PIL import Image

BASE = "/home/ubuntu/signal-bot"
sys.path.insert(0, BASE)
CFG = {}
try:
    CFG = json.load(open(os.path.join(BASE, "config.json"), encoding="utf-8"))
except Exception:
    pass
DS_KEY = os.environ.get("DEEPSEEK_API_KEY") or CFG.get("deepseek_api_key", "")
DS_API = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-v4-flash-vision-exp"
MAXPX = 820                     # 分类够用；越小越快越省

FOLDERS = {
    "signal": "3_signal_开单信号",
    "pnl": "1_忽略_非开单信号",
    "status": "1_忽略_非开单信号",
    "tweet": "1_忽略_非开单信号",
    "kline": "1_忽略_非开单信号",
    "news": "1_忽略_非开单信号",
    "other": "1_忽略_非开单信号",
    "unknown": "1_忽略_非开单信号",
}
KINDS = ("signal", "pnl", "status", "tweet", "kline", "news", "other")

PROMPT = (
    "你要给一个加密货币跟单群里的图片分类。**只输出 JSON，不要解释**：\n"
    "{\"kind\":\"signal|pnl|status|tweet|kline|news|other\",\"why\":\"一句话中文理由\","
    "\"has_tool\":true/false}\n"
    "\n"
    "★ 只有一种算 signal，其余全部算忽略：\n"
    "· **signal＝开单信号图**：这张图是在**给出一条新的开仓信号**——图上用画图工具画出了"
    "仓位：一条开仓线 + 一段止损区间 + 一段止盈区间（通常是**两块以上颜色不同的填充色块**，"
    "并且带价格数字标注）。颜色无所谓（红绿、灰蓝都行）。目的：告诉别人现在该开仓、止损止盈放哪。\n"
    "· pnl＝晒单/晒收益：交易所或券商账户截图（Balance/Equity/Margin/Positions/余额/净值/盈亏），"
    "或平台的收益分享卡片（一个大大的 +65.7%、Entry/Mark Price、推荐链接），或持仓列表晒盈利。\n"
    "· status＝开单之后的走势状态图：这笔单**已经开好了**，只是发出来看行情走到哪了"
    "（可能还画着之前那个仓位工具，但**现价已经明显离开开仓价、朝止盈方向跑**，或写着浮盈/继续持有）。\n"
    "· tweet＝推文/群聊截图：里面是社交平台帖子或聊天记录（含转发、点赞、评论、头像、二维码等版式），"
    "哪怕里面夹着收益卡片也算 tweet。\n"
    "· kline＝纯K线图：只有蜡烛、均线、指标，**没有画任何仓位工具/色块，也没有点位数字标注**。\n"
    "· news＝新闻/公告/提示截图：纯文字通知、FOMC/数据日历提醒、公告、表情包、无关截图。\n"
    "· other＝以上都不是。\n"
    "\n"
    "判断顺序：先问「这张图是不是在**给一条新的开仓信号**」；不是 → 再按上面选一个（宁可选其他，不要错判成 signal）。\n"
    "**重要：把「事后看这笔单」（status/pnl）和「现在该开仓」（signal）分清楚，这是唯一关键。**"
)


def classify(path):
    try:
        im = Image.open(path).convert("RGB")
        if max(im.size) > MAXPX:
            r = float(MAXPX) / max(im.size)
            im = im.resize((max(1, int(im.width * r)), max(1, int(im.height * r))), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=86)
        b64 = base64.b64encode(buf.getvalue()).decode()
        body = {"model": MODEL, "temperature": 0,
                "messages": [{"role": "system", "content": "只输出 JSON。"},
                             {"role": "user", "content": [
                                 {"type": "text", "text": PROMPT},
                                 {"type": "image_url",
                                  "image_url": {"url": "data:image/jpeg;base64," + b64}}]}]}
        r = requests.post(DS_API, headers={"Authorization": "Bearer " + DS_KEY,
                                          "Content-Type": "application/json"},
                          json=body, timeout=120)
        j = r.json()
        classify.usage_calls += 1
        classify.usage_tokens += int(((j.get("usage") or {}).get("total_tokens")) or 0)
        classify.usage_in += int(((j.get("usage") or {}).get("prompt_tokens")) or 0)
        classify.usage_out += int(((j.get("usage") or {}).get("completion_tokens")) or 0)
        m = re.search(r"\{[\s\S]*\}", j["choices"][0]["message"]["content"])
        if not m:
            return {"kind": "unknown", "why": "模型没给出 JSON", "has_tool": ""}
        d = json.loads(m.group(0))
        k = str(d.get("kind") or "").lower().strip()
        if k not in KINDS:
            k = "unknown"
        return {"kind": k, "why": str(d.get("why") or "")[:80], "has_tool": bool(d.get("has_tool"))}
    except Exception as e:
        classify.usage_fail += 1
        return {"kind": "unknown", "why": "调用失败：%s" % str(e)[:60], "has_tool": ""}


classify.usage_calls = 0
classify.usage_tokens = 0
classify.usage_in = 0
classify.usage_out = 0
classify.usage_fail = 0


def main():
    a = sys.argv
    d = a[a.index("--dir") + 1] if "--dir" in a else "chart_archive/images"
    csv_p = a[a.index("--csv") + 1] if "--csv" in a else "classify.csv"
    do_move = "--move" in a
    skip_done = "--skip-done" in a
    limit = int(a[a.index("--limit") + 1]) if "--limit" in a else 0
    done = set()
    if skip_done and os.path.exists(csv_p):
        with open(csv_p, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if row.get("文件名"):
                    done.add(row["文件名"])
        print("断点续跑：CSV 里已有 %d 张，跳过它们" % len(done))
    files = sorted(f for f in os.listdir(d)
                   if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")) and f not in done)
    if limit:
        files = files[:limit]
    print("本次待分类 %d 张 ｜ 目录 %s ｜ 移动=%s ｜ 图片缩到 ≤%dpx" % (len(files), d, do_move, MAXPX))
    if not os.path.exists(csv_p):
        with open(csv_p, "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerow(["文件名", "kind", "why", "has_tool"])
    t0 = time.time()
    rows = []
    for i, fn in enumerate(files, 1):
        r = classify(os.path.join(d, fn))
        r["文件名"] = fn
        rows.append(r)
        with open(csv_p, "a", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerow([fn, r["kind"], r["why"], r["has_tool"]])
        print("  [%3d/%3d] %-34s → %-7s %s" % (i, len(files), fn, r["kind"], r["why"][:44]), flush=True)
    cnt = {}
    for r in rows:
        cnt[r["kind"]] = cnt.get(r["kind"], 0) + 1
    print("\n=== 本次分类结果 === %s" % json.dumps(cnt, ensure_ascii=False))
    print("调用 %d 次 ｜ 输入 %d + 输出 %d = %d tokens ｜ 失败 %d ｜ 用时 %.0f 秒"
          % (classify.usage_calls, classify.usage_in, classify.usage_out,
             classify.usage_tokens, classify.usage_fail, time.time() - t0))
    print("明细：%s" % csv_p)
    if do_move:
        root = os.path.dirname(os.path.abspath(d))
        for sub in set(FOLDERS.values()):
            os.makedirs(os.path.join(root, sub), exist_ok=True)
        moved = 0
        for r in rows:
            dst = os.path.join(root, FOLDERS[r["kind"]], r["文件名"])
            try:
                shutil.move(os.path.join(d, r["文件名"]), dst)
                moved += 1
            except Exception as e:
                print("   移动失败 %s：%s" % (r["文件名"], str(e)[:60]))
        print("已移动 %d 张到：%s" % (moved, " ｜ ".join(sorted(set(FOLDERS.values())))))
    else:
        print("（没带 --move，只输出 CSV，没动任何文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
