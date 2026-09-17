#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解析准确率评测：拿【标准答案集】给指定版本的解析器打分。

用法：
  eval_parse.py <解析器文件路径>        # 例如 /tmp/newbot.py 或 /home/ubuntu/signal-bot/dryrun_bot2.py
  eval_parse.py <path> --lib            # 视作库文件（复用本脚本内的答案抽取与判分）

只读、隔离：不写生产任何文件。
"""
import os, re, json, sys, importlib.util

_SB_BASE = os.environ.get("SIGNAL_BOT_BASE", "/home/ubuntu/signal-bot")

MSGS_F = "/tmp/golden_msgs.json"
SELF_ANY = ("【跟单机器人】", "【信号·", "【博主指令】", "【止盈成交", "【已结单", "【已开单",
            "【指令】", "【待确认】", "【机器人状态】", "【挂单情况】", "【你的持仓】",
            "【已平仓", "通过webhook将自定义服务的消息推送至飞书", "Photo strip")


def is_self(t):
    return any(s in t for s in SELF_ANY)


def _num(s):
    try:
        return float(s.replace(",", "").replace("$", "").strip())
    except Exception:
        return None


def extract_labels(txt):
    """只从【原文里明确标注】的字段抽标准答案 —— 答案就写在原文里，客观、不靠猜。"""
    t = txt
    exp = {}
    if re.search(r"SELLING|卖出|做空|SHORT|Short|:\s*Short", t):
        exp["direction"] = "SHORT"
    elif re.search(r"BUYING|买入|做多|LONG|Long|:\s*Long", t):
        exp["direction"] = "LONG"
    m = re.search(r"\$([A-Z]{2,12})/USDT|([A-Z]{2,12})/USDT|([A-Z]{2,12})USDT", t)
    if m:
        exp["coin"] = (m.group(1) or m.group(2) or m.group(3)).upper()
    else:
        m = re.search(r"\b([A-Z]{2,10})\s*[:：]?\s*(?:Entry|入场)", t)
        if m:
            exp["coin"] = m.group(1).upper()
    m = re.search(r"(?:ENTRY|Entry|入场|进场)\s*[:：=]?\s*\$?([0-9]+\.?[0-9]*)"
                  r"(?:\s*[-~—–到至]\s*\$?([0-9]+\.?[0-9]*))?", t, re.I)
    if m:
        a = _num(m.group(1)); b = _num(m.group(2))
        exp["entry_low"], exp["entry_high"] = (a, b if b is not None else a)
    for pat in (r"(?:SL|Stop\s*-?\s*Loss|止损)\s*[:：=]?\s*\$?([0-9]+\.?[0-9]*)",
                r"([0-9]+\.?[0-9]*)\s*止损"):
        m = re.search(pat, t, re.I)
        if m:
            v = _num(m.group(1))
            if v is not None:
                exp["stop"] = v
                break
    if "stop" not in exp:
        m = re.search(r"close under\s*\$?([0-9]+\.?[0-9]*)", t, re.I)
        if m:
            exp["stop"] = _num(m.group(1))
    # ⚠️ 答案集自身的坑（实测踩过）：`止盈\d?` 会把「第一止盈74500」的 "7" 当档位序号吃掉
    #    → 答案变成 4500，反而把正确的机器人判成错。改为档位数字必须是 [1-4] 且后面不跟数字/小数点。
    tps = []
    for m in re.finditer(r"(?:TARGETS?|TP|止盈|目标位?)\s*(?:[1-4](?![0-9.])\s*)?"
                         r"[:：=]?\s*\$?([0-9]+\.?[0-9]*)", t, re.I):
        v = _num(m.group(1))
        if v is not None and v not in tps:
            tps.append(v)
    if tps:
        exp["tps"] = tps
    for m in re.finditer(r"(?:止损|止盈|目标)\s*[:：=]?\s*([0-9]+\.?[0-9]*)\s*点", t):
        v = _num(m.group(1))
        if v is not None and "止损" in m.group(0):
            exp["stop_points"] = v
    if exp.get("stop_points") and "stop" in exp:
        exp.pop("stop")          # 被标成点数的不是价格 → 交给专门的"点数识别"检查
    if "direction" not in exp:
        return None
    if not any(k in exp for k in ("entry_low", "stop", "tps", "stop_points")):
        return None
    return exp


def cmp_parse(exp, got):
    res = {}
    if not got:
        return {"_empty": False}
    gd = (got.get("direction") or "").upper()
    res["direction"] = (gd == exp["direction"])
    if "entry_low" in exp:
        ge = got.get("entry"); er = got.get("entryRange"); el = got.get("entryLegs")
        lo, hi = exp["entry_low"], exp["entry_high"]
        tol = max(abs(hi) * 0.01, 1e-9)
        if isinstance(ge, (int, float)):
            res["entry"] = (lo - tol) <= ge <= (hi + tol)
        elif isinstance(er, (list, tuple)) and len(er) == 2:
            res["entry"] = (abs(float(er[0]) - lo) <= tol and abs(float(er[1]) - hi) <= tol) or \
                           (abs(float(er[0]) - hi) <= tol and abs(float(er[1]) - lo) <= tol)
        elif isinstance(el, (list, tuple)) and el:
            res["entry"] = any(abs(float(x) - lo) <= tol for x in el)
        else:
            res["entry"] = False
    if "stop" in exp:
        gs = got.get("stop")
        res["stop"] = isinstance(gs, (int, float)) and abs(float(gs) - exp["stop"]) <= max(abs(exp["stop"]) * 0.001, 1e-9)
    if "stop_points" in exp:
        # 点数场景：机器人**不该**把点数当成价格；正确表现是"识别为点数→换算→送审批"
        gs = got.get("stop")
        res["stop_points_not_price"] = (not isinstance(gs, (int, float))) or \
                                       abs(float(gs) - exp["stop_points"]) > max(exp["stop_points"] * 0.001, 1e-9)
    if "tps" in exp:
        gt = [float(x) for x in (got.get("targets") or []) if isinstance(x, (int, float))]
        ok = 0
        for want in exp["tps"][:3]:
            if any(abs(x - want) <= max(abs(want) * 0.001, 1e-9) for x in gt):
                ok += 1
        res["tps"] = (ok == len(exp["tps"][:3]))
    return res


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_SB_BASE, "src", "dryrun_bot2.py")
    spec = importlib.util.spec_from_file_location("boteval", path)
    bot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bot)

    msgs = json.load(open(MSGS_F, encoding="utf-8"))
    cases, skipped = [], 0
    for r in msgs:
        if is_self(r["text"]):
            skipped += 1
            continue
        e = extract_labels(r["text"])
        if not e:
            skipped += 1
            continue
        got = bot.fast_parse(r["text"]) or {}
        cases.append({"ts": r["ts"], "group": r["group"], "text": r["text"], "expected": e,
                      "parsed": {k: got.get(k) for k in ("coin", "direction", "entry", "entryRange",
                                                         "entryLegs", "stop", "targets")},
                      "cmp": cmp_parse(e, got)})
    print("=" * 78)
    print("解析器：%s" % path)
    print("标准答案集：%d 条可客观判分（跳过 %d 条：机器人自己的通知 / 原文无明确标注）" % (len(cases), skipped))
    print("=" * 78)
    fields = {}
    for c in cases:
        for k, v in c["cmp"].items():
            if k.startswith("_"):
                continue
            fields.setdefault(k, [0, 0])
            fields[k][1] += 1
            fields[k][0] += 1 if v else 0
    for k, (ok, n) in sorted(fields.items()):
        print("  %-22s %2d/%2d  %5.0f%%" % (k, ok, n, 100.0 * ok / max(n, 1)))
    tot = sum(1 for c in cases if all(v for k, v in c["cmp"].items() if not k.startswith("_")))
    print("  %-22s %2d/%2d  %5.0f%%" % ("整条全对", tot, len(cases), 100.0 * tot / max(len(cases), 1)))
    print()
    print("【失败明细】")
    for i, c in enumerate(cases, 1):
        bad = {k: v for k, v in c["cmp"].items() if not k.startswith("_") and not v}
        if bad:
            print("  [%2d] %s %s" % (i, c["ts"][5:16], c["group"]))
            print("       原文: %s" % c["text"][:140].replace("\n", " "))
            print("       标准: %s" % json.dumps(c["expected"], ensure_ascii=False))
            print("       解析: %s" % json.dumps(c["parsed"], ensure_ascii=False))
            print("       错在: %s" % json.dumps(bad, ensure_ascii=False))
    json.dump(cases, open("/tmp/eval_result.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)


main()
