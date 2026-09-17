#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实层账本 + 对账器。

## 要解决的洞（本项目最要命的那个）

纸面账一直在记（`entry` / `remaining` / `sl` / `tps` / `realized`），但
**真实层从来没有账**：
  · `tr` 里跟真实层有关的只有 `real_layer`（一个模式名）和 `pending_fill`（一个真假位），
    **没有任何地方存"交易所上到底还剩多少"**；真实数量每次临时查一次、用完就丢；
  · 启动对账只比**币种集合**、不比数量：
        consistent = (not only_real) and (not LIVE or not only_paper)

于是"纸面认为已平 1/3、交易所其实还是满仓"这种**数量级漂移永远发现不了** ——
重启后两边币种都还在，对账照样判"一致"。

## 本模块的两条铁律

1. **`real_ledger()` 是唯一允许写真实账的地方** —— 数据只能来自交易所接口读回，
   绝不允许由纸面逻辑推测写入。
2. **对账必须比数量**，不只比币种集合。

## 设计约束

本模块是**纯逻辑**：不 import `dryrun_bot2`、不发通知、不碰全局状态。
读交易所需通过 `broker` 参数传进来（即 `binance_exec` 模块对象），
所以它能在本机用假数据单测 —— 见 `dryrun_bot2.py --selftest-ledger`。
"""


def real_ledger(broker, with_sl=True):
    """从交易所读回**真实持仓**（必要时连保护单一并读回）。**只读，绝不写。**

    返回 `{SYMBOL: {...}}`（**键是合约全名，如 BTCUSDT** —— 与纸面那种 ``%sUSDT``
    的写法保持一致，否则对账时两边对不上），其中：
        symbol       币安全名（如 BTCUSDT）
        coin         币种简称（如 BTC），供展示/手工仓名单用
        side         "LONG" / "SHORT"（双向持仓模式下按 positionSide）
        qty          abs(positionAmt)：真实剩余数量
        entry        entryPrice
        has_sl       是否找到**止损类**挂单（STOP*，含 Algo 条件单）—— ⇐ 严格含义
        stop_orders  止损类挂单条数
        tp_orders    止盈类挂单条数
        has_protection  止损或止盈任一存在（与旧 watch_naked 的口径一致，仅兼容用）

    ⚠️ **读失败直接抛异常**，绝不返回 `{}`。
       因为"读不到"和"没有仓"是两件完全不同的事：把读失败当成空仓，
       会让上层以为交易所什么都没有 —— 那是灾难性的误判。
    """
    rows = broker.positions() or []
    out = {}
    for x in rows:
        if not isinstance(x, dict):
            continue
        try:
            amt = float(x.get("positionAmt") or 0)
        except Exception:
            continue
        if amt == 0:
            continue
        sym = str(x.get("symbol") or "").upper()
        if not sym:
            continue
        base = sym[:-4] if sym.endswith("USDT") else sym
        side = (x.get("positionSide") or "").upper()
        if side not in ("LONG", "SHORT"):
            side = "LONG" if amt > 0 else "SHORT"
        try:
            entry = float(x.get("entryPrice") or 0)
        except Exception:
            entry = 0.0
        out[sym] = {"symbol": sym, "coin": base, "side": side, "qty": abs(amt),
                    "entry": entry, "has_sl": None, "stop_orders": 0,
                    "tp_orders": 0, "has_protection": None, "raw": x}
    if with_sl:
        for sym, rec in out.items():
            n_sl = n_tp = 0
            for fn in ("open_algo_orders", "open_orders"):
                try:
                    rows2 = getattr(broker, fn)(sym) or []
                except Exception:
                    rows2 = []
                for o in rows2:
                    t = str((o or {}).get("type") or "").upper()
                    # ⚠️ 必须把止损与止盈**分开数**（自查后修正）：
                    #    旧 watch_naked 把 TAKE_PROFIT 也当成“有保护”，那是宽松口径；
                    #    但本字段叫 `has_sl`（有没有止损）——
                    #    一个“只有止盈单、没有止损单”的仓位**就是裸仓**，
                    #    用旧口径会把它报成 has_sl=True（定时炸弹）。
                    if fn == "open_algo_orders" or t.startswith("STOP"):
                        n_sl += 1
                    elif t.startswith("TAKE_PROFIT"):
                        n_tp += 1
            rec["stop_orders"], rec["tp_orders"] = n_sl, n_tp
            rec["has_sl"] = n_sl > 0
            rec["has_protection"] = (n_sl + n_tp) > 0
    return out


def reconcile(paper_syms, real, paper_qty=None, live=True, tol=0.05, ignored=()):
    """把"纸面持有的币种/数量"与"交易所有什么"逐条比对。

    paper_syms  纸面持有的 symbol 集合（如 `{"BTCUSDT"}`）
    real        `real_ledger()` 的返回
    paper_qty   `{symbol: 纸面折算数量}` —— 给了才比数量；不给就只比集合（旧行为）
    live        是否实盘。**影子模式下，纸面的仓本来就不该出现在交易所**，
                所以 `only_paper` / 数量不符只在实盘下才算"不一致"（沿用原有语义）
    tol         数量相对误差容差。默认 5%：
                  纸面数量是"名义 ÷ 开仓价"的**估算**（真实成交价有滑点、数量有位进制取整），
                  所以本来就有几个百分点的天然误差；
                  而"纸面以为平了 1/3、真实满仓"是 33% 的差 —— 远超容差，抓得到
    ignored     不参与比对的币（用户**手工仓**：机器人不得干涉，也就没资格评判它的数量）

    返回 `{"ok": bool, "diffs": [ {kind, symbol, coin, detail} ], "blocked": bool}`
        kind ∈ {"only_paper", "only_real", "qty_mismatch"}
    """
    paper_syms = {str(s).upper() for s in (paper_syms or ())}
    ignored = {str(s).upper() for s in (ignored or ())}
    real_syms = set(real or {})

    def base_of(sym):
        s = str(sym).upper()
        return s[:-4] if s.endswith("USDT") else s

    diffs = []

    for s in sorted(real_syms - paper_syms):
        rec = real.get(s) or {}
        diffs.append({"kind": "only_real", "symbol": s, "coin": rec.get("coin") or base_of(s),
                      "detail": "⚠️ 交易所有仓、纸面无记录（孤儿仓/手工仓）：%s 数量 %s"
                                % (s, rec.get("qty"))})

    for s in sorted(paper_syms - real_syms):
        diffs.append({"kind": "only_paper", "symbol": s, "coin": base_of(s),
                      "detail": "纸面有仓、交易所无仓：%s" % s})

    # ===== 新增：两边都有 → 比数量 =====
    if paper_qty:
        for s in sorted(paper_syms & real_syms):
            if s in ignored:
                continue
            try:
                pq = float(paper_qty.get(s))
                rq = float((real.get(s) or {}).get("qty"))
            except (TypeError, ValueError):
                continue
            if pq <= 0 or rq <= 0:
                continue
            rel = abs(rq - pq) / pq
            if rel > tol:
                diffs.append({
                    "kind": "qty_mismatch", "symbol": s, "coin": base_of(s),
                    "detail": ("⚠️ 数量不符：纸面认为 %.8g、交易所实际 %.8g"
                               "（差 %.1f%%，容差 %.0f%%）—— 纸面账与真实持仓已脱节"
                               % (pq, rq, rel * 100, tol * 100))})

    only_real = any(d["kind"] == "only_real" for d in diffs)
    bad_under_live = live and any(d["kind"] in ("only_paper", "qty_mismatch") for d in diffs)
    consistent = not (only_real or bad_under_live)
    return {"ok": consistent, "diffs": diffs, "blocked": (not consistent) and bool(live)}


def summary(paper_syms, real, diffs):
    """一行摘要，便于日志核对。"""
    return ("纸面 %d 笔 ｜ 交易所 %d 笔 ｜ 差异 %d 条 ｜ 数量级差异 %d 条"
            % (len(paper_syms or ()), len(real or {}), len(diffs or []),
               sum(1 for d in (diffs or []) if d.get("kind") == "qty_mismatch")))
