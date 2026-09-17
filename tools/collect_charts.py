#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把指定群**过去 N 天**的所有图片归档到一个文件夹（按时间顺序命名）+ 生成"标准答案表"。

用户要求（2026-09-17）：「按照时间顺序，从一个月前到今天的图片，把 ua、黄金mansoor 过去一个月的
所有开单图，全部放在一个文件夹里，并准备好表格我要填数据。」

用法（在生产目录里跑）：
    venv/bin/python collect_charts.py --groups UA-nurseneil2,黄金mansoor --days 30 \
        --out /home/ubuntu/signal-bot/chart_archive

产出：
    <out>/images/  20260901_143252_<msgid>_0.jpg   （文件名开头就是时间 → 天生按时间排序）
    <out>/索引.csv       文件名,群,时间,消息文本前80字,图序号
    <out>/标准答案.csv   文件名,群,时间,方向,开仓,止损,止盈1,止盈2,止盈3   ← 你只需要填后 6 列
只读飞书（用户身份令牌），不下任何单；图片只写到 --out 目录。
"""
import os
import sys
import csv
import time
import datetime

sys.path.insert(0, "/home/ubuntu/signal-bot")
import feishu_api as fa          # noqa: E402

CST = datetime.timezone(datetime.timedelta(hours=8))


def arg(name, default=None):
    a = sys.argv
    if name in a and a.index(name) + 1 < len(a):
        return a[a.index(name) + 1]
    return default


def main():
    groups = [g.strip() for g in (arg("--groups", "UA-nurseneil2,黄金mansoor")).split(",") if g.strip()]
    days = int(arg("--days", "30"))
    out = arg("--out", "/home/ubuntu/signal-bot/chart_archive")
    imgdir = os.path.join(out, "images")
    os.makedirs(imgdir, exist_ok=True)
    start = int(time.time() - days * 86400)
    print("归档群：%s ｜ 回溯 %d 天（起点 %s）｜ 输出 %s"
          % ("、".join(groups), days,
             datetime.datetime.fromtimestamp(start, CST).strftime("%Y-%m-%d %H:%M"), out))
    rows, n_img = [], 0
    for g in groups:
        cid = (fa.resolve_chat_ids([g]) or {}).get(g)
        if not cid:
            print("  ⚠️ 找不到群：%s" % g)
            continue
        items, err = fa.fetch_messages(cid, start_time=start, page_size=50,
                                       max_pages=int(arg("--maxpages", "200")), asc=True)
        print("  [%s] 拉到 %d 条消息（err=%s）" % (g, len(items or []), err))
        for it in (items or []):
            try:
                cms = int(it.get("create_time") or 0)
            except Exception:
                continue
            when = datetime.datetime.fromtimestamp(cms / 1000.0, CST)
            mid = it.get("message_id") or ""
            keys = fa.msg_images_of(it)
            if not keys:
                continue
            txt = (fa.msg_text_of(it) or "").replace("\n", " ").strip()
            for i, k in enumerate(keys[:4]):
                fn = "%s_%s_%d.jpg" % (when.strftime("%Y%m%d_%H%M%S"), mid[-8:], i)
                p = os.path.join(imgdir, fn)
                if not os.path.exists(p):
                    try:
                        got = fa.download_image(mid, k, imgdir)
                    except Exception as e:
                        print("     下载失败 %s：%s" % (fn, str(e)[:60]))
                        got = None
                    if got and os.path.exists(got) and os.path.basename(got) != fn:
                        try:
                            os.rename(got, p)      # 统一成"时间在前"的文件名，天生按时间排序
                        except Exception:
                            p = got
                    elif not got:
                        continue
                    time.sleep(0.25)
                n_img += 1
                rows.append({"文件名": os.path.basename(p), "群": g,
                             "时间": when.strftime("%Y-%m-%d %H:%M:%S"),
                             "消息文本前80字": txt[:80], "图序号": i})
    rows.sort(key=lambda r: (r["文件名"]))
    idx_p = os.path.join(out, "索引.csv")
    with open(idx_p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["文件名", "群", "时间", "消息文本前80字", "图序号"])
        w.writeheader()
        w.writerows(rows)
    tru_p = os.path.join(out, "标准答案.csv")
    with open(tru_p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["文件名", "群", "时间", "方向", "开仓", "止损", "止盈1", "止盈2", "止盈3"])
        for r in rows:
            w.writerow([r["文件名"], r["群"], r["时间"], "", "", "", "", "", ""])
    print("\n完成：共归档 %d 张图（%d 条消息记录）" % (n_img, len(rows)))
    print("  图片目录：%s" % imgdir)
    print("  索引　　：%s" % idx_p)
    print("  待你填　：%s（只需填后 6 列：方向/开仓/止损/止盈1-3；图上没有的档留空）" % tru_p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
