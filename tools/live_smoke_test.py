#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实小单联调（用户 2026-09-17 批准）——**唯一会动真钱的脚本，默认不跑，必须带 --yes**。

它做什么（每一步都打印报文与交易所返回，便于你复核）：
  0) 只读体检：账户可用余额、合约规格、标记价、将要发送的报文
  1) 市价开仓（带幂等键 clientOrderId）
  2) 挂真实止损（Algo STOP_MARKET，-stop-pct%）+ 真实止盈（限价，+tp-pct%）
  3) bundle 核对：止损在不在、止盈在不在（bracket_verify）
  4) 市价平仓
  5) 收尾：撤掉遗留的止盈单 / 条件单，再确认持仓为 0

**安全设计**：
  · 只在**本进程**把 binance_exec.LIVE 打开 → **机器人本体仍留在影子模式**，
    不会出现"测试期间机器人自己下真单"的窗口；
  · 全程带幂等键，任何"状态未知"都先用幂等键查（见 binance_exec.post_idempotent）；
  · 不发 reduceOnly、不用经典 stopPrice（那两个在币安会报 -1106 / -4120）。

用法：
    venv/bin/python live_smoke_test.py --symbol SOLUSDT --yes
"""
import os
import sys
import time
import json

# 项目根目录：环境变量优先（见 src/config.py）。
# ⚠️ 生产机上 .py 平铺在根目录，git 仓库里在 src/ —— 两个位置都加，两种布局都能跑。
_SB_BASE = os.environ.get("SIGNAL_BOT_BASE", "/home/ubuntu/signal-bot")
sys.path.insert(0, os.path.join(_SB_BASE, "src"))
sys.path.insert(0, _SB_BASE)
import binance_exec as bx          # noqa: E402


def arg(name, default=None):
    a = sys.argv
    if name in a and a.index(name) + 1 < len(a):
        return a[a.index(name) + 1]
    return default


def main():
    symbol = (arg("--symbol", "SOLUSDT") or "SOLUSDT").upper()
    margin = float(arg("--margin", "5"))
    lev = int(arg("--lev", "3"))
    dir_ = (arg("--dir", "LONG") or "LONG").upper()
    stop_pct = float(arg("--stop-pct", "2"))
    tp_pct = float(arg("--tp-pct", "3"))
    yes = "--yes" in sys.argv
    notional = margin * lev

    print("=" * 72)
    print("真实小单联调：%s %s ｜ 保证金 %.1fU × %d 倍 = 名义 %.1fU ｜ 止损 -%.1f%% / 止盈 +%.1f%%"
          % (symbol, dir_, margin, lev, notional, stop_pct, tp_pct))
    print("=" * 72)
    sp = bx.spec(symbol)
    px = bx.mark_price(symbol)
    qty = bx.fmt_qty(symbol, notional / px)
    stop = bx.fmt_price(symbol, px * (1 - stop_pct / 100.0) if dir_ == "LONG" else px * (1 + stop_pct / 100.0))
    tp = bx.fmt_price(symbol, px * (1 + tp_pct / 100.0) if dir_ == "LONG" else px * (1 - tp_pct / 100.0))
    print("合约规格 : tick=%s step=%s minQty=%s minNotional=%s"
          % (sp.get("tick"), sp.get("step"), sp.get("minQty"), sp.get("minNotional")))
    print("标记价   : %s" % px)
    print("将下数量 : %s（名义 ≈ %.2fU）" % (qty, float(qty) * float(px)))
    print("止损/止盈: %s / %s" % (stop, tp))
    mn = float(sp.get("minNotional") or 0)
    if float(qty) * float(px) < mn:
        print("❌ 名义 %.2fU 低于交易所最小额 %sU → 先调大保证金，终止。" % (float(qty) * float(px), mn))
        return 2
    if not yes:
        print("\n（这是演练：没带 --yes，**什么都没发**。确认无误后加 --yes 再跑。）")
        return 0

    bx.LIVE[0] = True                  # ⚠️ 只在本进程打开；机器人进程不受影响
    print("\n[0] 已在本进程打开下单开关（机器人本体仍是影子模式）")
    try:
        acct = bx.account() or {}
        print("    可用余额 : %s USDT" % acct.get("availableBalance"))
    except Exception as e:
        print("    ⚠️ 读账户失败（继续）：%s" % str(e)[:100])

    out = {"symbol": symbol, "notional": notional}
    # ---- 1) 市价开仓 ----
    print("\n[1] 市价开仓…")
    r1 = bx.market_open(symbol, dir_, notional, lev=lev)
    print("    交易所返回 : %s" % json.dumps({k: v for k, v in (r1 or {}).items()
                                              if k in ("orderId", "status", "executedQty",
                                                       "avgPrice", "_cid")}, ensure_ascii=False))
    out["open"] = r1
    time.sleep(2.5)
    filled = 0.0
    try:
        filled = float((r1 or {}).get("executedQty") or 0)
    except Exception:
        filled = 0.0
    if not filled:
        try:
            pos = bx.position_of(symbol, dir_) or {}
            filled = abs(float(pos.get("positionAmt") or 0))
        except Exception:
            pass
    if not filled:
        print("    ❌ 没读到成交量 → 终止（不挂止损/止盈，先人工核对）")
        return 1
    print("    成交数量 : %s" % filled)

    # ---- 2) 挂止损 + 止盈 ----
    print("\n[2] 挂真实止损（Algo STOP_MARKET %s）…" % stop)
    try:
        r2 = bx.place_sl_stop_market(symbol, dir_, float(stop), filled)
        print("    %s" % json.dumps({k: v for k, v in (r2 or {}).items()
                                     if k in ("algoId", "success", "code", "msg")}, ensure_ascii=False))
        out["sl"] = r2
    except Exception as e:
        print("    ❌ 止损挂单失败：%s" % str(e)[:200])
        out["sl_err"] = str(e)[:200]
    time.sleep(1.0)
    print("[2b] 挂真实止盈（限价 %s）…" % tp)
    try:
        r3 = bx.place_tp_limit(symbol, dir_, float(tp), filled)
        print("    %s" % json.dumps({k: v for k, v in (r3 or {}).items()
                                     if k in ("orderId", "status", "_cid")}, ensure_ascii=False))
        out["tp"] = r3
    except Exception as e:
        print("    ❌ 止盈挂单失败：%s" % str(e)[:200])
        out["tp_err"] = str(e)[:200]

    # ---- 3) 挂完回头核对 ----
    time.sleep(1.5)
    print("\n[3] 回头核对（止损在不在 / 止盈在不在）…")
    bv = bx.bracket_verify(symbol, dir_, stop=float(stop), tps=[{"price": float(tp)}])
    print("    核对结果 : %s" % json.dumps(bv, ensure_ascii=False))
    out["verify"] = bv

    # ---- 4) 平仓 ----
    print("\n[4] 市价平仓…")
    try:
        r4 = bx.close_position_market(symbol, dir_)
        print("    %s" % json.dumps({k: v for k, v in (r4 or {}).items()
                                     if k in ("orderId", "status", "executedQty", "_cid")}, ensure_ascii=False))
        out["close"] = r4
    except Exception as e:
        print("    ❌ 平仓失败（**需要人工处理**）：%s" % str(e)[:200])
        out["close_err"] = str(e)[:200]

    # ---- 5) 收尾：撤掉遗留挂单 ----
    time.sleep(2.0)
    print("\n[5] 撤掉遗留挂单并确认持仓…")
    try:
        print("    撤单 : %s" % str(bx.cancel_all(symbol))[:200])
    except Exception as e:
        print("    撤单异常：%s" % str(e)[:120])
    try:
        aals = bx.open_algo_orders(symbol) or []
        for a in aals:
            try:
                bx.cancel_algo(symbol, a.get("algoId"))
                print("    撤条件单 %s" % a.get("algoId"))
            except Exception as e:
                print("    条件单撤销失败（可能已随平仓失效）：%s" % str(e)[:80])
    except Exception as e:
        print("    读条件单失败：%s" % str(e)[:100])
    time.sleep(1.5)
    try:
        pos = bx.position_of(symbol, dir_) or {}
        print("    最终持仓 : %s（应为 0）" % pos.get("positionAmt"))
        out["final_amt"] = pos.get("positionAmt")
    except Exception as e:
        print("    读持仓失败：%s" % str(e)[:100])
    try:
        print("    剩余挂单 : %s" % json.dumps(bx.open_orders(symbol), ensure_ascii=False)[:200])
    except Exception:
        pass
    bx.audit("live_smoke_test", {"symbol": symbol, "notional": notional}, out)
    print("\n完成。完整审计记录：%s" % os.path.join(_SB_BASE, "v21", "real_orders.jsonl"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
