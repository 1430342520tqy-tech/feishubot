# -*- coding: utf-8 -*-
"""把评测分片 CSV 合并成一份，并重算可用性汇总。

用法（服务器上跑）：
    venv/bin/python merge_shards.py --pattern /tmp/chart_eval/读数_p1s*.csv \
        --out /tmp/chart_eval/读数_p1_40.csv [--jsonl-out /tmp/chart_eval/原始日志_p1_40.jsonl]

纪律：只做"可用性/一致率"统计；没有 --truth 就**不出任何准确率**数字。
"""
import argparse
import csv
import glob
import json
import os
import sys

RS = ["文件名", "图md5", "图字节",
      "P1_llm2_ok", "P1_两次一致", "P1_分歧项", "P1_失败类型", "P1_秒", "P1_llm2_说明",
      "P1_方向", "P1_开仓", "P1_止损", "P1_止盈1", "P1_止盈2", "P1_止盈3",
      "P2_geo_ok", "P2_秒", "P2_geo_说明",
      "P2_方向", "P2_开仓", "P2_止损", "P2_止盈1", "P2_止盈2", "P2_止盈3",
      "P3_生产_ok", "P3_reader", "P3_回退说明", "P3_秒", "P3_生产_说明",
      "P3_方向", "P3_开仓", "P3_止损", "P3_止盈1", "P3_止盈2", "P3_止盈3",
      "P1vsP2_分歧", "P1_第1次", "P1_原始JSON"]


def _t(r, k):
    v = (r.get(k) or "").strip()
    return v.lower() in ("true", "1", "yes")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pattern", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--jsonl-out", default="")
    a = ap.parse_args()
    files = sorted(glob.glob(a.pattern))
    rows = []
    for p in files:
        with open(p, encoding="utf-8-sig") as f:
            got = list(csv.DictReader(f))
        print("  %s → %d 行" % (os.path.basename(p), len(got)))
        rows += got
    seen, uniq = set(), []
    for r in rows:                                  # 去重（分片本不该重叠，防手滑）
        fn = r.get("文件名")
        if fn in seen:
            print("  ⚠️ 重复行，已丢弃：%s" % fn)
            continue
        seen.add(fn)
        uniq.append(r)
    rows = sorted(uniq, key=lambda r: r.get("文件名") or "")
    for r in rows:
        for k in RS:
            r.setdefault(k, "")
    with open(a.out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    if a.jsonl_out:
        with open(a.jsonl_out, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    n = len(rows)
    cnt = lambda k: sum(1 for r in rows if _t(r, k))
    full = lambda p: sum(1 for r in rows if r.get(p + "_方向") and r.get(p + "_开仓") and r.get(p + "_止损"))
    print("\n合并 %d 张 → %s" % (n, a.out))
    print("\n===== 可用性汇总（**没有答案，所以这里没有任何准确率**）=====")
    lines = [
        ("参与评测的图", n),
        ("图片内容重复（同 md5 出现多次）", n - len({r["图md5"] for r in rows})),
        ("P3 生产路径产出可用读数（上线口径可用性）", "%d/%d = %.1f%%" % (cnt("P3_生产_ok"), n, 100.0 * cnt("P3_生产_ok") / max(1, n))),
        ("P3 里真的由 llm 读出的", "%d/%d = %.1f%%" % (sum(1 for r in rows if r.get("P3_reader") == "llm"), n, 100.0 * sum(1 for r in rows if r.get("P3_reader") == "llm") / max(1, n))),
        ("P3 里靠 llm→geo 回退兜住的", "%d/%d = %.1f%%" % (sum(1 for r in rows if r.get("P3_回退说明")), n, 100.0 * sum(1 for r in rows if r.get("P3_回退说明")) / max(1, n))),
        ("P1 llm×2 两次一致才采信", "%d/%d = %.1f%%" % (cnt("P1_llm2_ok"), n, 100.0 * cnt("P1_llm2_ok") / max(1, n))),
        ("P1 两次都通过校验但读数不一致", "%d/%d" % (sum(1 for r in rows if not (r.get("P1_失败类型") or "") and not _t(r, "P1_两次一致")), n)),
        ("P1 因某次没通过校验而未采信", "%d/%d" % (sum(1 for r in rows if (r.get("P1_失败类型") or "").startswith("校验没通过")), n)),
        ("P2 geo 产出可用读数", "%d/%d = %.1f%%" % (cnt("P2_geo_ok"), n, 100.0 * cnt("P2_geo_ok") / max(1, n))),
        ("P1 可读全项率（方向+开仓+止损）", "%d/%d" % (full("P1"), n)),
        ("P2 可读全项率（方向+开仓+止损）", "%d/%d" % (full("P2"), n)),
        ("P3 可读全项率（方向+开仓+止损）", "%d/%d" % (full("P3"), n)),
        ("P1 秒数合计 / P2 秒数合计", "%.0fs / %.0fs" % (sum(float(r.get("P1_秒") or 0) for r in rows), sum(float(r.get("P2_秒") or 0) for r in rows))),
    ]
    for k, v in lines:
        print("  %-42s %s" % (k, v))
    print("\n===== 需要人工看图复核的清单（分歧/未读出）=====")
    for r in rows:
        tag = []
        if r.get("P3_回退说明"):
            tag.append("P3回退geo")
        if not _t(r, "P1_llm2_ok"):
            tag.append("llm×2未采信")
        if not _t(r, "P2_geo_ok"):
            tag.append("geo未读出")
        if r.get("P1vsP2_分歧"):
            tag.append("llm/geo分歧")
        if tag:
            print("  %-34s %s ｜ %s" % (r["文件名"], "、".join(tag), (r.get("P1vsP2_分歧") or r.get("P1_失败类型") or r.get("P3_生产_说明") or "")[:110]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
