#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读图评测 v2 —— 三条路径在同一批图上对跑，产出**可用性**指标（不含准确率）。

为什么要有这个 v2（v1 是 `eval_charts.py`）：
  v1 只跑 `read_chart()`（= geo），一次一张、逐张打印，无法回答"开跑了 llm 之后到底
  是 llm 读出来的、还是 llm 读不出退回 geo 的"。而生产路径 `read_chart_cached()` 在
  llm 读不出时**会自动退回 geo**（09-18 新增）→ 只看"产出率"会把 geo 的功劳记在 llm 头上。

三条路径（同一张图，各自独立跑）：
  P1  llm×2 纯直读  —— 两次**独立**调用视觉模型，逐次记录，可看清"是第几次/哪一项不一致"
                       （生产里第一次失败就整张返回，看不到这些）
  P2  geo 纯几何    —— `bot.read_chart()`（像素/几何 + 标签 OCR）
  P3  生产真实路径  —— `bot.read_chart_cached()`（llm 读不出会自动退 geo，带 reader_fallback）

⚠️ 口径纪律（写死在这里）：
  · 本工具**不产出"准确率"**，除非给了 `--truth`。没给答案时，所有比例只能叫
    「可用性 / 采信率 / 一致率」，**不许**叫准确率。
  · 判定口径（有答案时才用）：开仓/止损 相对误差 ≤ 0.5% 算对；止盈**命中任意一档**算对；
    方向必须完全一致。
  · 每张图的读数**逐项标"未读到"**，绝不猜、绝不用别的图的数据补。

隔离：`SIGNAL_BOT_BASE` 指向 /tmp 沙箱 + `SIGNALBOT_SILENT=1`（一条飞书都不发），
      所有产物写 `--outdir`，**不碰生产 state/run.log**。

用法（在生产目录里跑，用生产的 .env）：
  venv/bin/python eval_charts2.py --dir /tmp/eval_imgs/chart_archive/<图片目录> \
      --limit 40 --outdir /tmp/chart_eval --tag batch1 \
      [--template 模板.csv] [--truth 标准答案.csv]
"""
import argparse
import csv
import glob
import hashlib
import json
import os
import sys
import time

_SB_BASE = os.environ.get("SIGNAL_BOT_BASE", "/home/ubuntu/signal-bot")
# ⚠️ 顺序不能反：生产目录根下**还留着旧的平铺版 dryrun_bot2.py**（无 CHART_READER /
#    无 llm 直读），`src/` 才是现在跑的那份。必须让 src 排在前面，否则会静默测到旧代码。
sys.path.insert(0, _SB_BASE)
sys.path.insert(0, os.path.join(_SB_BASE, "src"))

_T = "/tmp/eval_charts"
os.environ["SIGNALBOT_SILENT"] = "1"          # 一条飞书都不发
import dryrun_bot2 as bot                     # noqa: E402
import chart_llm                              # noqa: E402

bot.RUN = _T
bot.LOGF = _T + "/eval.log"
bot.STATE = _T + "/state.json"
bot.TRADES = _T + "/eval.jsonl"
bot.IMGDIR = _T + "/imgs"
os.makedirs(bot.IMGDIR, exist_ok=True)

# 自证：确认测的是生产在跑的那份代码（旧平铺版没有 CHART_READER，会静默测错）
if not hasattr(bot, "CHART_READER") or "/src/" not in bot.__file__.replace("\\", "/"):
    print("⚠️⚠️ 导入到的不是生产 src 版代码：%s（含 CHART_READER=%s）"
          % (bot.__file__, hasattr(bot, "CHART_READER")))
    print("    本工具会中止，避免拿旧代码的结果当结论。")
    sys.exit(3)

TOL = 0.005
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")


def _num(x):
    try:
        f = float(x)
        return f if f == f else None
    except Exception:
        return None


def _s(x):
    """读数转成 CSV 里的字符串；未读到 = 空。"""
    if x is None:
        return ""
    if isinstance(x, float):
        return "%.8g" % x
    return str(x)


def _tps_str(tps):
    return " / ".join("%.8g" % t for t in (tps or []))


def _all_items(r):
    """从读数里抽出五个判定项（方向/开仓/止损/止盈1/止盈2/止盈3），缺的返回 None。"""
    r = r or {}
    d = (r.get("dir") or (r.get("zones") or {}).get("dir") or None)
    d = (str(d).upper() if d else None)
    tps = list(r.get("tps") or [])
    return {
        "方向": d,
        "开仓": r.get("entry"),
        "止损": r.get("sl"),
        "止盈1": tps[0] if len(tps) > 0 else None,
        "止盈2": tps[1] if len(tps) > 1 else None,
        "止盈3": tps[2] if len(tps) > 2 else None,
    }


def _diff_items(a, b):
    """两项读数的分歧项（相对误差 > 0.5% 或方向不同）；返回字符串。"""
    out = []
    if not a or not b:
        return ""
    if a.get("方向") != b.get("方向"):
        out.append("方向(%s vs %s)" % (a.get("方向"), b.get("方向")))
    for k in ("开仓", "止损", "止盈1", "止盈2", "止盈3"):
        x, y = _num(a.get(k)), _num(b.get(k))
        if x is None or y is None:
            if (x is None) != (y is None):
                out.append("%s(一边未读到)" % k)
            continue
        base = max(abs(x), abs(y))
        if base and abs(x - y) / base > TOL:
            out.append("%s(%s vs %s)" % (k, _s(x), _s(y)))
    return " ｜ ".join(out)


def _llm_two(path):
    """P1：llm×2 纯直读，逐次记录（不退回 geo）。

    直接调 `chart_llm._call_once` + `_normalize`，因为 `chart_llm.read(agree=2)` 第一次
    不通过就整张返回，看不到到底是哪一项不一致。
    """
    key = chart_llm._cfg_key()
    if not key:
        return {"ok": False, "why": "没有 AI 密钥", "reads": [], "usage": [], "sec": 0.0}
    res, usage = {}, []
    t0 = time.time()

    def _worker(i):
        try:
            raw = chart_llm._call_once(path, key, timeout=180)
            res[i] = ("ok", chart_llm._normalize(raw), raw)
        except Exception as e:
            res[i] = ("err", str(e)[:120], None)

    import threading
    ths = [threading.Thread(target=_worker, args=(i,)) for i in range(2)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    sec = time.time() - t0
    reads = []
    for i in range(2):
        st, v, raw = res.get(i, ("err", "线程没有返回", None))
        reads.append({"i": i + 1, "调用": ("失败：%s" % v) if st == "err" else "成功",
                      "校验": (v or {}).get("why") if st == "ok" and not (v or {}).get("ok") else None,
                      "原始": raw})
        if st == "ok" and (v or {}).get("ok"):
            reads[-1]["读数"] = _all_items(v)
    good = [r["读数"] for r in reads if r.get("读数")]
    out = {"ok": False, "why": "", "reads": reads, "usage": usage, "sec": sec, "mode": "llm"}
    if len(good) < 2:
        bad = [("第%d次" % r["i"]) + (("（%s）" % r["校验"]) if r.get("校验") else "（调用失败）")
               for r in reads if not r.get("读数")]
        out["失败类型"] = "校验没通过：" + "、".join(bad)
        out["why"] = "两次里只有 %d 次通过校验" % len(good)
        if good:
            out["读数"] = good[0]
        return out
    ok, why = chart_llm._agree(chart_llm._normalize(reads[0]["原始"]),
                               chart_llm._normalize(reads[1]["原始"]))
    out["两次一致"] = bool(ok)
    out["分歧"] = _diff_items(good[0], good[1])
    # ⚠️ 与生产口径对齐：`chart_llm.read()` 返回的是**第 1 次**的读数（两次都通过校验时），
    #    分歧时只记录，采信与否看「两次一致」。所以这里也取第 1 次，绝不改成"哪个更准用哪个"。
    out["读数"] = good[0]
    if not ok:
        out["why"] = "两次直读不一致：%s" % why
        return out
    out["ok"] = True
    return out


def _one(fn):
    path = fn if os.path.isabs(fn) else os.path.abspath(fn)
    rec = {"文件名": os.path.basename(path),
           "图md5": hashlib.md5(open(path, "rb").read()).hexdigest(),
           "图字节": os.path.getsize(path)}
    # P1 llm×2 纯直读
    try:
        r1 = _llm_two(path)
    except Exception as e:
        r1 = {"ok": False, "why": "异常 %s" % str(e)[:100], "reads": [], "sec": 0.0}
    rec["P1_llm2_ok"] = bool(r1.get("ok"))
    rec["P1_llm2_说明"] = r1.get("why") or ""
    rec["P1_失败类型"] = r1.get("失败类型") or ""
    rec["P1_两次一致"] = r1.get("两次一致")
    rec["P1_分歧项"] = r1.get("分歧") or ""
    rec["P1_秒"] = round(r1.get("sec") or 0, 1)
    it1 = _all_items(r1.get("读数") or {})
    for k in ("方向", "开仓", "止损", "止盈1", "止盈2", "止盈3"):
        rec["P1_" + k] = _s(it1.get(k))
    rec["P1_第1次"] = json.dumps([rr.get("读数") for rr in r1.get("reads", [])],
                                 ensure_ascii=False)
    rec["P1_原始JSON"] = json.dumps([rr.get("原始") for rr in r1.get("reads", [])],
                                    ensure_ascii=False)

    # P2 geo 纯几何
    t0 = time.time()
    try:
        bot.CHART_READER[0] = "geo"
        r2 = bot.read_chart(path) or {}
    except Exception as e:
        r2 = {"ok": False, "why": "异常 %s" % str(e)[:100]}
    rec["P2_geo_ok"] = bool(r2.get("ok"))
    rec["P2_geo_说明"] = r2.get("why") or ""
    rec["P2_秒"] = round(time.time() - t0, 1)
    it2 = _all_items(r2)
    for k in ("方向", "开仓", "止损", "止盈1", "止盈2", "止盈3"):
        rec["P2_" + k] = _s(it2.get(k))

    # P3 生产真实路径（llm→geo 自动回退）
    t0 = time.time()
    try:
        bot.CHART_READER[0] = "llm"
        bot.CHART_LLM_READS[0] = 2
        r3 = bot.read_chart_cached(path) or {}
    except Exception as e:
        r3 = {"ok": False, "why": "异常 %s" % str(e)[:100]}
    rec["P3_生产_ok"] = bool(r3.get("ok"))
    rec["P3_reader"] = r3.get("mode") or ("回退geo" if r3.get("reader_fallback") else "")
    rec["P3_回退说明"] = r3.get("reader_fallback") or ""
    rec["P3_生产_说明"] = r3.get("why") or ""
    rec["P3_秒"] = round(time.time() - t0, 1)
    it3 = _all_items(r3)
    for k in ("方向", "开仓", "止损", "止盈1", "止盈2", "止盈3"):
        rec["P3_" + k] = _s(it3.get(k))
    rec["P1vsP2_分歧"] = _diff_items(it1, it2)
    return rec


def _agg(rows):
    n = len(rows)

    def _cnt(key):
        return sum(1 for r in rows if r.get(key) is True)

    full = lambda p: sum(1 for r in rows if r.get(p + "_方向") and r.get(p + "_开仓") and r.get(p + "_止损"))
    return [
        ("参与评测的图", n),
        ("其中图片内容重复（同 md5 出现多次）", n - len({r["图md5"] for r in rows})),
        ("P3 生产路径产出可用读数（=上线口径的可用性，**不是准确率**）", "%d/%d = %.1f%%" % (_cnt("P3_生产_ok"), n, 100.0 * _cnt("P3_生产_ok") / max(1, n))),
        ("P3 里真的由 llm 读出的", "%d/%d" % (sum(1 for r in rows if r["P3_生产_ok"] and r["P3_reader"] == "llm"), n)),
        ("P3 里靠 llm→geo 回退兜住的", "%d/%d" % (sum(1 for r in rows if r.get("P3_回退说明")), n)),
        ("P1 llm×2 通过校验（两次一致才采信）", "%d/%d = %.1f%%" % (_cnt("P1_llm2_ok"), n, 100.0 * _cnt("P1_llm2_ok") / max(1, n))),
        ("P1 两次成功但读数不一致（系统性分歧）", "%d/%d" % (sum(1 for r in rows if r.get("P1_两次一致") is False), n)),
        ("P1 因某次没通过校验而没采信", "%d/%d" % (sum(1 for r in rows if (r.get("P1_失败类型") or "").startswith("校验没通过")), n)),
        ("P2 geo 产出可用读数", "%d/%d = %.1f%%" % (_cnt("P2_geo_ok"), n, 100.0 * _cnt("P2_geo_ok") / max(1, n))),
        ("P1 可读全项率（方向+开仓+止损都读到）", "%d/%d" % (full("P1"), n)),
        ("P2 可读全项率（方向+开仓+止损都读到）", "%d/%d" % (full("P2"), n)),
        ("P3 可读全项率（方向+开仓+止损都读到）", "%d/%d" % (full("P3"), n)),
        ("P1-P3 秒数合计（llm 路径，含双读）", "%.1f 秒" % sum(r.get("P1_秒") or 0 for r in rows)),
        ("P2 秒数合计（geo 路径）", "%.1f 秒" % sum(r.get("P2_秒") or 0 for r in rows)),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--limit", type=int, default=0, help="0=全跑；>0 按固定抽样（时间分层）")
    ap.add_argument("--outdir", default="/tmp/chart_eval")
    ap.add_argument("--tag", default="run")
    ap.add_argument("--index", default="", help="index.csv（用于把消息文本带进模板，方便人工标注）")
    ap.add_argument("--template", default="", help="生成标准答案模板（答案列留空）")
    ap.add_argument("--truth", default="", help="给了才算准确率（本工具的可用性输出不受影响）")
    ap.add_argument("--shards", type=int, default=1, help="把本次样本切成 N 份并行跑（配合 --shard）")
    ap.add_argument("--shard", type=int, default=0, help="本进程跑第几份（0 基）")
    a = ap.parse_args()

    files = sorted(p for p in glob.glob(os.path.join(a.dir, "*")) if p.lower().endswith(IMG_EXT))
    if not files:
        print("目录里没有图片：%s" % a.dir)
        return 2
    if a.limit and a.limit < len(files):
        step = len(files) / float(a.limit)          # 固定等距抽样：可复现、覆盖整段时间
        files = [files[int(i * step)] for i in range(a.limit)]
    if a.shards > 1:
        files = [f for i, f in enumerate(files) if i % a.shards == a.shard]
    os.makedirs(a.outdir, exist_ok=True)
    print("图片目录 = %s" % a.dir)
    print("本次评测 %d 张（目录共 %d 张）" % (len(files), len(glob.glob(os.path.join(a.dir, "*")))))

    text = {}
    if a.index and os.path.exists(a.index):
        with open(a.index, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if (row.get("文件名") or "").strip():
                    text[row["文件名"].strip()] = row

    rows = []
    t_all = time.time()
    for n, p in enumerate(files, 1):
        t0 = time.time()
        try:
            rec = _one(p)
        except Exception as e:
            rec = {"文件名": os.path.basename(p), "P1_llm2_ok": False,
                   "P2_geo_ok": False, "P3_生产_ok": False,
                   "P1_llm2_说明": "整张异常 %s" % str(e)[:120]}
        rows.append(rec)
        print("[%3d/%3d] %-34s P1llm×2=%-5s P2geo=%-5s P3生产=%-5s%s ｜ 用时 %.0fs / 累计 %.0fs"
              % (n, len(files), rec["文件名"], rec.get("P1_llm2_ok"), rec.get("P2_geo_ok"),
                 rec.get("P3_生产_ok"),
                 ("（回退geo）" if rec.get("P3_回退说明") else ""),
                 time.time() - t0, time.time() - t_all))
        sys.stdout.flush()

    csv_p = os.path.join(a.outdir, "读数_%s.csv" % a.tag)
    keys = ["文件名", "图md5", "图字节",
            "P1_llm2_ok", "P1_两次一致", "P1_分歧项", "P1_失败类型", "P1_秒", "P1_llm2_说明",
            "P1_方向", "P1_开仓", "P1_止损", "P1_止盈1", "P1_止盈2", "P1_止盈3",
            "P2_geo_ok", "P2_秒", "P2_geo_说明",
            "P2_方向", "P2_开仓", "P2_止损", "P2_止盈1", "P2_止盈2", "P2_止盈3",
            "P3_生产_ok", "P3_reader", "P3_回退说明", "P3_秒", "P3_生产_说明",
            "P3_方向", "P3_开仓", "P3_止损", "P3_止盈1", "P3_止盈2", "P3_止盈3",
            "P1vsP2_分歧", "P1_第1次", "P1_原始JSON"]
    for r in rows:                                   # 补齐缺列（异常行会缺键）
        for k in keys:
            r.setdefault(k, "")
    with open(csv_p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    log_p = os.path.join(a.outdir, "原始日志_%s.jsonl" % a.tag)
    with open(log_p, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("\n已写：%s" % csv_p)
    print("已写：%s（原始读数，可追溯）" % log_p)

    if a.template:
        with open(a.template, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["文件名", "群", "时间", "方向", "开仓", "止损", "止盈1", "止盈2", "止盈3", "消息文本前80字"])
            for p in files:
                fn = os.path.basename(p)
                t = text.get(fn) or {}
                w.writerow([fn, t.get("群", ""), t.get("时间", ""), "", "", "", "", "", "",
                            (t.get("消息文本前80字") or "").strip()])
        print("已生成标准答案模板：%s（%d 行，答案列留空；填不了的**留空=该项不计分**）"
              % (a.template, len(files)))

    print("\n===== 可用性（**没有答案，所以这里没有任何准确率**）=====")
    for k, v in _agg(rows):
        print("  %-46s %s" % (k, v))

    if a.truth:
        truth = {}
        with open(a.truth, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if (row.get("文件名") or "").strip():
                    truth[row["文件名"].strip()] = row
        print("\n===== 准确率（有标准答案才算，口径：相对误差≤0.5% 算对、止盈命中任一档算对、方向必须一致）=====")
        for p in ("P1", "P2", "P3"):
            n = dn = en = sn = 0
            tn = th = 0
            for r in rows:
                t = truth.get(r["文件名"])
                if not t:
                    continue
                n += 1
                d = (t.get("方向") or "").strip().upper()
                if d:
                    dn += 1 if (r.get(p + "_方向") or "").upper() == d else 0
                e = _num(t.get("开仓"))
                if e is not None:
                    en += 1 if (_num(r.get(p + "_开仓")) and abs(_num(r.get(p + "_开仓")) - e) / e <= TOL) else 0
                s = _num(t.get("止损"))
                if s is not None:
                    sn += 1 if (_num(r.get(p + "_止损")) and abs(_num(r.get(p + "_止损")) - s) / s <= TOL) else 0
                got = [_num(r.get(p + "_止盈%d" % i)) for i in (1, 2, 3)]
                got = [x for x in got if x]
                for k in ("止盈1", "止盈2", "止盈3"):
                    want = _num(t.get(k))
                    if want is None:
                        continue
                    tn += 1
                    if any(abs(x - want) / want <= TOL for x in got):
                        th += 1
            print("  %s：参与 %d 张 ｜ 方向 %d ｜ 开仓 %d ｜ 止损 %d ｜ 止盈命中 %d/%d"
                  % (p, n, dn, en, sn, th, tn))
    else:
        print("\n（本次没给 --truth → 不出任何准确率数字。先把 40 张答案填进模板，再用 --truth 跑。）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
