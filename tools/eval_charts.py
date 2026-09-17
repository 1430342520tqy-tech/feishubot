#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量测读图准确率（用户 2026-09-17 要求：把图片放一个文件夹，大批量真实测试）。

用法（在服务器上、生产目录里跑）：
    venv/bin/python eval_charts.py <图片文件夹>                      # 只跑，打印每张图的读数
    venv/bin/python eval_charts.py <图片文件夹> --csv 结果.csv        # 把结果写成 CSV
    venv/bin/python eval_charts.py <图片文件夹> --truth 标准答案.csv  # 有标准答案就算准确率
    venv/bin/python eval_charts.py <图片文件夹> --template 模板.csv   # 生成"标准答案模板"给你填

标准答案 CSV 格式（表头必须一致，一行一张图）：
    文件名,方向,开仓,止损,止盈1,止盈2,止盈3
    a.jpg,LONG,4.3167,4.156,4.6927,5.3083,6.0137
    留空 = 该项不判分（比如图上本来没有第三档止盈）。

判定口径（写死在这里，改口径就改这几个数）：
    · 开仓/止损：相对误差 ≤ 0.5% 算对
    · 每档止盈：只要**命中任意一档**（相对误差 ≤ 0.5%）就算这一档对
    · 方向：必须完全一致
⚠️ 隔离：RUN/LOGF/NOTIFY_CFG/STATE/TRADES/IMGDIR 全部重定向到 /tmp，**不碰生产**。
"""
import os
import sys
import csv
import glob
import json
import time

sys.path.insert(0, "/home/ubuntu/signal-bot")
import dryrun_bot2 as bot          # noqa: E402

_T = "/tmp/eval_charts"
bot.RUN = _T
bot.LOGF = _T + "/eval.log"
bot.NOTIFY_CFG = _T + "/notify.json"      # 不存在 → 不发飞书
bot.STATE = _T + "/state.json"
bot.TRADES = _T + "/eval.jsonl"
bot.IMGDIR = _T + "/imgs"
os.makedirs(bot.IMGDIR, exist_ok=True)

TOL = 0.005
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")


def _num(x):
    try:
        return float(x)
    except Exception:
        return None


def _close(a, b):
    a, b = _num(a), _num(b)
    if a is None or b is None or b == 0:
        return None
    return abs(a - b) / abs(b) <= TOL


def read_truth(path):
    d = {}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            fn = (row.get("文件名") or "").strip()
            if fn:
                d[fn] = row
    return d


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    folder = sys.argv[1]
    truth_p = csv_p = tmpl_p = None
    _args = sys.argv[2:]
    _i = 0
    while _i < len(_args):
        if _args[_i] == "--truth" and _i + 1 < len(_args):
            truth_p = _args[_i + 1]
            _i += 2
        elif _args[_i] == "--csv" and _i + 1 < len(_args):
            csv_p = _args[_i + 1]
            _i += 2
        elif _args[_i] == "--template" and _i + 1 < len(_args):
            tmpl_p = _args[_i + 1]
            _i += 2
        else:
            _i += 1
    files = sorted([p for p in glob.glob(os.path.join(folder, "*"))
                    if p.lower().endswith(IMG_EXT)])
    if not files:
        print("文件夹里没有图片：%s（支持 %s）" % (folder, "/".join(IMG_EXT)))
        return 2
    if tmpl_p:
        with open(tmpl_p, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["文件名", "方向", "开仓", "止损", "止盈1", "止盈2", "止盈3"])
            for p in files:
                w.writerow([os.path.basename(p), "", "", "", "", "", ""])
        print("已生成标准答案模板：%s（%d 行）——把正确答案填进去，再用 --truth 跑准确率"
              % (tmpl_p, len(files)))
        return 0

    truth = read_truth(truth_p) if truth_p else {}
    rows, ok = [], {"dir": 0, "entry": 0, "sl": 0, "tp_hit": 0, "tp_total": 0, "n": 0}
    print("共 %d 张图，开始逐张读（每张约 5~20 秒）…\n" % len(files))
    t0 = time.time()
    for p in files:
        fn = os.path.basename(p)
        t1 = time.time()
        try:
            r = bot.read_chart(p) or {}
        except Exception as e:
            r = {"ok": False, "why": "读图异常 %s" % str(e)[:80]}
        dt = time.time() - t1
        row = {"文件名": fn, "ok": r.get("ok"), "模式": r.get("mode"),
               "方向": r.get("dir") or (r.get("zones") or {}).get("dir"),
               "开仓": r.get("entry"), "止损": r.get("sl"),
               "止盈": " / ".join("%.8g" % x for x in (r.get("tps") or [])),
               "置信": (r.get("zones") or {}).get("conf"), "秒": round(dt, 1),
               "说明": r.get("why") or ""}
        t = truth.get(fn)
        if t:
            ok["n"] += 1
            _d = (str(t.get("方向") or "").strip().upper() or None)
            if _d:
                ok["dir"] += 1 if (row["方向"] or "").upper() == _d else 0
            if _num(t.get("开仓")) is not None:
                ok["entry"] += 1 if _close(row["开仓"], t.get("开仓")) else 0
            if _num(t.get("止损")) is not None:
                ok["sl"] += 1 if _close(row["止损"], t.get("止损")) else 0
            _tps = [row["止盈"]] and [x.strip() for x in str(row["止盈"] or "").split("/") if x.strip()]
            for k in ("止盈1", "止盈2", "止盈3"):
                want = _num(t.get(k))
                if want is None:
                    continue
                ok["tp_total"] += 1
                if any(_close(x, want) for x in _tps):
                    ok["tp_hit"] += 1
        rows.append(row)
        print("%-34s %-14s 方向=%-5s 开仓=%-12s 止损=%-12s 止盈=%s %s"
              % (fn, row["模式"] or "-", row["方向"] or "-",
                 ("%.8g" % row["开仓"]) if isinstance(row["开仓"], (int, float)) else "未读到",
                 ("%.8g" % row["止损"]) if isinstance(row["止损"], (int, float)) else "未读到",
                 row["止盈"] or "未读到", ("（%s）" % row["说明"]) if row["说明"] else ""))
    print("\n用时 %.0f 秒" % (time.time() - t0))
    if csv_p:
        with open(csv_p, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print("已写结果：%s" % csv_p)
    if truth:
        print("\n===== 准确率（拿标准答案算的）=====")
        print("  参与判分的图：%d 张" % ok["n"])
        print("  开仓价正确：%d / %d" % (ok["entry"], ok["n"]))
        print("  止损价正确：%d / %d" % (ok["sl"], ok["n"]))
        print("  止盈档命中：%d / %d" % (ok["tp_hit"], ok["tp_total"]))
        print("  方向正确　：%d / %d" % (ok["dir"], ok["n"]))
    else:
        print("\n（没给 --truth，所以只列读数不判分。先用 --template 生成模板填好答案，再用 --truth 跑。）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
