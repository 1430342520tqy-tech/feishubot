#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""整图直读的读图器。

## 它和现有读图法的区别（这一点必须说清楚）

现有 `read_chart()` **已经在调用大模型**了 —— 但只让模型当"读数员"：
像素几何负责**判断**（哪条线是止损、哪条是止盈、哪个方向），模型只负责**把数字念出来**
（`_ocr_tags_batch` / `_ocr_one_label` / `_edge_price_by_vision`）。

本模块是另一条路：把**判断权也交给模型** —— 看整张图，直接给出
方向 / 开仓 / 止损 / 止盈。

## 为什么做成"可选开关、默认关"

现有那条路是 4 天里返工 9 次、用真实事故换来的（`6.513` 被读成 `6513`、
19:20 那笔假成交、多色块图读不出……）。本模块**还没有在任何真实图上量过准确率**，
所以：

* 默认仍走现有读图（`chart_reader = "geo"`）；
* 要切换，必须在 `runtime_config.json` 里把 `chart_reader` 改成 `"llm"`；
* ⚠️ **切换前必须先用 `tools/eval_charts.py` 在同一批图上对比准确率**，
  新路径不差于基线才切。

## 保留了什么

两道与"谁能看图"无关的下游关卡**不在本模块里，也绝不能被一起换掉**：
  · 方向-价位自洽校验（做多时止损必须低于开仓）
  · "止盈已被现价越过 → 挂上去会立刻假成交"的拒绝
本模块自己也会做一遍方向-价位自洽（`_normalize`），但那只是**多一道**，不是替代。

## 双读数（`agree=2`）

用户对读图的硬要求是"必须 100% 正确"。单次直读**没有任何交叉校验**，
所以默认让模型对同一张图**独立读两次、必须一致才采信**，不一致就按"没读到"处理
（与现有 `two_read_ok` 的纪律完全一致：宁可不下单，绝不猜）。
成本/延迟翻倍，可用 `agree=1` 关掉 —— 但那是拿"100% 正确"去赌。

## ⚠️ 已知弱点（必须知道，不要以为它和现有路径一样稳）

| 弱点 | 说明 |
|---|---|
| **双读数只能挡随机错，挡不住系统性错** | 如果模型两次都因同一原因读错（比如习惯性地丢掉小数点），两次会"一致"→ 照样采信。而历史上真实发生过的那次事故（`6.513` → `6513`）**正是系统性错**。已加"止损与开仓的比例必须在 0.5~2 倍之间"这道量级校验作为缓解，但**止盈的小数点错误仍然挡不住** |
| **没有像素几何自洽校验** | 现有路径能用"像素距离比 = 价格比"去反推读数对不对（不依赖任何标签），本模块没有像素信息，**做不到** |
| **双读数要求止盈档数也一致** | 模型对"图上有几档"本身就容易不一致 → 拒绝率可能偏高。这是**偏安全**的代价，可用 `eval_charts.py` 量出来再调 |
| 未在真实图上量过 | 所以默认关（`chart_reader="geo"`） |
"""
import base64
import json
import os
import re

import requests

DS_API = "https://api.deepseek.com/chat/completions"
VISION_MODEL = "deepseek-v4-flash-vision-exp"

# 两次读数的相对容差（与 dryrun_bot2.two_read_ok 同口径）
TWO_READ_TOL = 0.005

_SYSTEM = "你在读加密货币合约交易图。只输出 JSON，不要解释。"

_USER = """这张图是博主发的合约交易信号图。请读出**开仓价、止损价、分档止盈价、方向**。

图上的画法惯例（同一个画图工具画的）：
· 有两片**填充色块**：较小的那片是"止损区"，较大的那片是"止盈区"；
· **两片色块相接的那条边 = 开仓价**；
· 止损区在下方 → 做多(LONG)；止损区在上方 → 做空(SHORT)；
· 止盈区**内部**画的横线 = 分档止盈位（通常 1~3 条）；
· 也有可能不是色块而是**红绿框**或**带价签的横线**，那就按图上的语义读。

严格只输出这个 JSON：
{"is_trading_chart":true/false,"direction":"LONG"|"SHORT"|null,
 "entry":数字或null,"stop":数字或null,"targets":[数字...]}

规则（很重要）：
1. 数字必须是图上**真实可见**的价格。**不要自己换算、不要补小数点、不要臆造。**
2. 读不准或看不到就写 null。**宁可为 null，也不要猜。**
3. `targets` 按价格**从近到远**排列，最多 3 档；没有就写 []。"""


def _cfg_key():
    """取 AI 密钥：**只看环境变量（含 `.env`）**：`DEEPSEEK_API_KEY`。"""
    k = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if k:
        return k
    try:
        import config
        return config.secrets()["deepseek_api_key"]
    except Exception:
        return ""


def _num(v):
    """把模型可能写脏的数值洗干净（`"$1,234.5"` / `"1.2e3"` / `null`）。"""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = re.sub(r"[^0-9eE.+-]", "", v.strip())
        try:
            f = float(s)
            return f if f > 0 else None
        except Exception:
            return None
    return None


def _norm_dir(v):
    if not isinstance(v, str):
        return None
    s = v.strip().upper()
    if s in ("LONG", "L", "BUY", "多", "做多", "看多"):
        return "LONG"
    if s in ("SHORT", "S", "SELL", "空", "做空", "看空"):
        return "SHORT"
    return None


def _normalize(d):
    """把模型返回的 JSON 归一化成 `read_chart()` 的返回结构，并**严格校验**。

    这是本模块里唯一能单测的部分（不需要网络）—— 见 `--selftest-chartllm`。

    校验不通过一律 `ok=False`（当作"没读到"），**绝不降级成猜**：
      · 方向必须能认出来
      · 开仓/止损必须都有且为正、且两者不相等
      · 做多必须 止损 < 开仓 < 止盈；做空必须 止损 > 开仓 > 止盈
         （与代码里既有的"方向-价位自洽"同一口径）
      · 止盈按离开仓价由近到远排序、去重、最多 3 档
    """
    if not isinstance(d, dict):
        return {"ok": False, "why": "模型没返回 JSON", "mode": "llm"}
    if d.get("is_trading_chart") is False:
        return {"ok": False, "why": "模型判定这不是交易信号图", "mode": "llm"}
    direction = _norm_dir(d.get("direction"))
    if not direction:
        return {"ok": False, "why": "方向没读出来（绝不猜方向）", "mode": "llm"}
    entry = _num(d.get("entry"))
    stop = _num(d.get("stop"))
    if not entry or not stop:
        return {"ok": False, "why": "开仓价或止损价没读到（绝不猜价）", "mode": "llm",
                "dir": direction}
    if abs(entry - stop) < 1e-12:
        return {"ok": False, "why": "开仓价与止损价相同（不合理）", "mode": "llm",
                "dir": direction}
    # ⚠️ 量级自洽（自查后补的）：**止损不可能离入场超过一倍**。
    #    这条专治历史上真实发生过的那类错误：小数点丢失（`6.513` → `6513`）。
    #    那种误差会让 stop/entry ≈ 1000 或 0.001，这里直接拦下来。
    #    （现有像素路径有更严的几何自洽校验；本模块没有像素信息，只能做这一层。）
    _ratio = stop / entry
    if not (0.5 <= _ratio <= 2.0):
        return {"ok": False, "why": "止损与开仓的比例 %.4g 不合理（疑似小数点丢失）" % _ratio,
                "mode": "llm", "dir": direction}
    # 方向-价位自洽（做多止损必在下、做空止损必在上）
    if direction == "LONG" and stop > entry:
        return {"ok": False, "why": "做多但止损 %.8g 高于开仓 %.8g（不自洽）" % (stop, entry),
                "mode": "llm", "dir": direction}
    if direction == "SHORT" and stop < entry:
        return {"ok": False, "why": "做空但止损 %.8g 低于开仓 %.8g（不自洽）" % (stop, entry),
                "mode": "llm", "dir": direction}
    tps = []
    for x in (d.get("targets") or []):
        v = _num(x)
        if not v:
            continue
        good = (v > entry) if direction == "LONG" else (v < entry)
        if not good:
            continue                      # 止盈落错边的直接丢（下游还有一道校验）
        if any(abs(v - t) / max(1e-9, t) <= 1e-9 for t in tps):
            continue
        tps.append(v)
    tps.sort(key=lambda v: abs(v - entry))
    tps = tps[:3]
    return {"ok": True, "dir": direction, "entry": entry, "sl": stop,
            "tps": tps, "tps_all": tps, "why": None, "mode": "llm",
            "llm_raw": d}


def _call_once(path, key, timeout=120):
    """调一次视觉模型，返回原始 dict（不校验）。"""
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    ext = "png" if str(path).lower().endswith(".png") else "jpeg"
    body = {"model": VISION_MODEL, "temperature": 0,
            "messages": [{"role": "system", "content": _SYSTEM},
                         {"role": "user", "content": [
                             {"type": "text", "text": _USER},
                             {"type": "image_url",
                              "image_url": {"url": "data:image/%s;base64,%s" % (ext, b64)}}]}]}
    r = requests.post(DS_API, headers={"Authorization": "Bearer " + key,
                                       "Content-Type": "application/json"},
                      json=body, timeout=timeout)
    txt = r.json()["choices"][0]["message"]["content"]
    m = re.search(r"\{[\s\S]*\}", txt)
    if not m:
        raise ValueError("模型没给出 JSON：%s" % str(txt)[:120])
    return json.loads(m.group(0))


def _agree(a, b, tol=TWO_READ_TOL):
    """两次读数是否一致（方向必须相同；开仓/止损/止盈逐项在容差内）。"""
    if not (a or {}).get("ok") or not (b or {}).get("ok"):
        return False, "其中一次没读出可用结果"
    if a["dir"] != b["dir"]:
        return False, "两次方向不一致（%s vs %s）" % (a["dir"], b["dir"])
    for k, name in (("entry", "开仓"), ("sl", "止损")):
        if abs(a[k] - b[k]) / max(1e-9, b[k]) > tol:
            return False, "%s两次读数差 %.2f%%" % (name, abs(a[k] - b[k]) / b[k] * 100)
    if len(a["tps"]) != len(b["tps"]):
        return False, "止盈档数不一致（%d vs %d）" % (len(a["tps"]), len(b["tps"]))
    for i, (x, y) in enumerate(zip(a["tps"], b["tps"])):
        if abs(x - y) / max(1e-9, y) > tol:
            return False, "止盈第%d档两次读数不一致" % (i + 1)
    return True, None


def read(path, agree=2, key=None, timeout=120, log=None):
    """整图直读。返回与 `dryrun_bot2.read_chart()` 同构的 dict。

    `agree=2`（默认）：同一张图**独立读两次**，不一致就 `ok=False`（绝不猜）。
    `agree=1`：只读一次 —— 没有交叉校验，用于成本/延迟敏感场景，风险自负。
    """
    k = key or _cfg_key()
    if not k:
        return {"ok": False, "why": "没有 AI 密钥（.env 里的 DEEPSEEK_API_KEY）", "mode": "llm"}
    n = 2 if int(agree or 1) >= 2 else 1
    first = None
    for i in range(n):
        try:
            raw = _call_once(path, k, timeout=timeout)
        except Exception as e:
            return {"ok": False, "why": "直读失败：%s" % str(e)[:100], "mode": "llm"}
        got = _normalize(raw)
        if not got.get("ok"):
            if log:
                log("   ⚠️ 大模型直读未通过校验：%s" % got.get("why"))
            return got
        if first is None:
            first = got
            continue
        ok, why = _agree(first, got)
        if not ok:
            if log:
                log("   ⚠️ 大模型两次直读不一致 → 按未读到处理：%s" % why)
            return {"ok": False, "why": "两次直读不一致：%s" % why, "mode": "llm",
                    "dir": first.get("dir")}
    first["verify"] = {"reads": n, "agreed": n >= 2}
    return first
