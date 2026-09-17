#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
币安 USDT-M 真实下单层（双向持仓 / hedge 模式）
================================================================
设计原则（按用户 2026-09-13 确认）：
  1. **双向持仓**：同一币种可同时持多与持空（用户选的博主覆盖山寨/主流/黄金/美股四类）。
     → 所有订单都带 positionSide，**不发送 reduceOnly**（币安在 hedge 模式下不接受该参数）。
  2. **杠杆固定 3 倍**，名义 = 保证金 × 3。
  3. 开仓按既有规则：市价单 or 限价单（沿用 dryrun_bot2.py 的「不追高/不追空」判定）。
  4. **止盈 = 限价单**（挂在目标价，吃 maker 万2）；
     **止损 = STOP_MARKET 触发市价**（见下方重要说明）。

⚠️ 重要说明：为什么止损不能用「限价单」
   做多时止损价在市价**下方**。若挂 SELL LIMIT 在该价，币安规则是「市价 >= 限价即可成交」，
   而当前市价本来就高于止损价 → **会立刻以市价成交，等于一开仓就平仓**。
   所以止损必须是触发单：STOP_MARKET（触发后市价平仓）。这是技术必然，不是偷懒。

安全开关：
  LIVE[0] = False（默认）→ 影子模式：**只记录"将要发什么单"，绝不发送任何 POST/DELETE**。
  切换方式：runtime_config.json 的 "live_trading": true，或群内指令「真实模式 开」。
  ⚠️ 账户里没钱时即使打开开关也只会报错，不会成交。

自检（不下单，用币安官方 order/test 接口校验签名与参数）：
  python binance_exec.py --selftest
只读检查：
  python binance_exec.py --check
将现有纸面持仓按真实规则"下单"（影子模式记录，便于人工复核）：
  python binance_exec.py --shadow BTC LONG 0.08438 0.0828 0.09965,0.11703
"""
import os
import sys
import json
import time
import hmac
import math
import hashlib
import datetime
import urllib.parse
import urllib.request
import urllib.error
import threading

BASE = os.environ.get("SIGNAL_BOT_BASE", "/home/ubuntu/signal-bot")
CFG = os.path.join(BASE, "config.json")
RUNTIME = os.path.join(BASE, "runtime_config.json")
AUDITF = os.path.join(BASE, "v21", "real_orders.jsonl")
FAPI = "https://fapi.binance.com"
CST = datetime.timezone(datetime.timedelta(hours=8))

LIVE = [False]          # 总开关：True 才会真正发单
LEV = 3                 # 杠杆（用户当前只用 3 倍）
WORKING_TYPE = "MARK_PRICE"   # 止损触发口径：与纸面逻辑一致（纸面用 premiumIndex=标记价）
DRY = [False]           # True = 连读接口都不调，纯本地演练

_SPEC = {}              # symbol -> {"tick":..,"step":..,"minQty":..,"minNotional":..}
_DUAL = [None]          # 账户是否双向持仓（None=未探测）


# ============================ 底层 HTTP ============================
def _load_cfg():
    try:
        return json.load(open(CFG, encoding="utf-8"))
    except Exception:
        return {}


_C = _load_cfg()
_B = _C.get("binance") or {}
KEY = (_B.get("api_key") or "").strip()
SEC = (_B.get("api_secret") or "").strip()


class BinanceError(Exception):
    pass


def audit(kind, payload, resp=None, mode=None):
    """所有下单意图/结果都留痕，便于事后核对"""
    rec = {"ts": datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
           "mode": mode or ("LIVE" if LIVE[0] else "shadow"),
           "kind": kind, "payload": payload, "resp": resp}
    try:
        os.makedirs(os.path.dirname(AUDITF), exist_ok=True)
        with open(AUDITF, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return rec


def _req(method, path, params=None, signed=True, test=False, timeout=20):
    """唯一出口。非 GET 且 LIVE=False 且非 test → 直接拒绝，绝不放行。"""
    method = method.upper()
    if method != "GET" and not LIVE[0] and not test:
        raise BinanceError(
            "已阻止非只读请求 %s %s：LIVE 开关为关闭（影子模式）。"
            "请先把 runtime_config.json 的 live_trading 设为 true。" % (method, path))
    if DRY[0]:
        raise BinanceError("DRY 模式：不发起任何网络请求（%s %s）" % (method, path))
    p = dict(params or {})
    headers = {"X-MBX-APIKEY": KEY}
    if signed:
        if not KEY or not SEC:
            raise BinanceError("config.json 缺少 binance.api_key / api_secret")
        p["timestamp"] = int(time.time() * 1000)
        p.setdefault("recvWindow", 5000)
        qs = urllib.parse.urlencode(p)
        p["signature"] = hmac.new(SEC.encode(), qs.encode(), hashlib.sha256).hexdigest()
    qs = urllib.parse.urlencode(p)
    url = FAPI + path + ("?" + qs if qs else "")
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            raw = json.loads(raw)
        except Exception:
            pass
        raise BinanceError("HTTP %s: %s" % (e.code, raw))
    except Exception as e:
        raise BinanceError("%s: %s" % (type(e).__name__, e))


# ============================ 合约规格 ============================
def load_specs(force=False):
    """从 exchangeInfo 取 tickSize / stepSize / minQty / minNotional"""
    if _SPEC and not force:
        return _SPEC
    ex = _req("GET", "/fapi/v1/exchangeInfo", signed=False)
    for s in ex.get("symbols", []):
        if s.get("status") != "TRADING" or s.get("quoteAsset") != "USDT":
            continue
        d = {"tick": None, "step": None, "minQty": None, "minNotional": None}
        for flt in s.get("filters", []):
            ft = flt.get("filterType")
            if ft == "PRICE_FILTER":
                d["tick"] = float(flt.get("tickSize") or 0) or None
            elif ft == "LOT_SIZE":
                d["step"] = float(flt.get("stepSize") or 0) or None
                d["minQty"] = float(flt.get("minQty") or 0) or None
            elif ft == "MIN_NOTIONAL":
                d["minNotional"] = float(flt.get("notional") or flt.get("minNotional") or 0) or None
        _SPEC[s["symbol"]] = d
    return _SPEC


def spec(symbol):
    load_specs()
    return _SPEC.get(symbol) or {}


def _decimals(step):
    """由 stepSize 推小数位数（0.001 -> 3）"""
    s = ("%.12f" % step).rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def round_tick(price, tick, mode="nearest"):
    if not tick or tick <= 0:
        return price
    q = price / tick
    n = {"nearest": round(q), "ceil": math.ceil(q), "floor": math.floor(q)}[mode]
    return round(n * tick, _decimals(tick) + 2)


def round_step(qty, step, mode="floor"):
    """数量必须向下取整到 stepSize（币安要求）"""
    if not step or step <= 0:
        return qty
    q = qty / step
    n = {"floor": math.floor(q), "nearest": round(q), "ceil": math.ceil(q)}[mode]
    return round(n * step, _decimals(step))


def fmt_price(symbol, price):
    t = spec(symbol).get("tick")
    v = round_tick(price, t) if t else price
    return ("%.{}f".format(_decimals(t)) % v) if t else ("%.10g" % v)


def fmt_qty(symbol, qty):
    st = spec(symbol).get("step")
    v = round_step(qty, st)
    return ("%.{}f".format(_decimals(st)) % v) if st else ("%.10g" % v)


# ============================ 账户状态 ============================
def is_hedge():
    """账户是否双向持仓模式（hedge）。None=异常"""
    if _DUAL[0] is None:
        try:
            _DUAL[0] = bool(_req("GET", "/fapi/v1/positionSide/dual", signed=True).get("dualSidePosition"))
        except Exception:
            _DUAL[0] = None
    return _DUAL[0]


def mark_price(symbol):
    d = _req("GET", "/fapi/v1/premiumIndex", {"symbol": symbol}, signed=False)
    return float(d["markPrice"])


def account():
    return _req("GET", "/fapi/v2/account", signed=True)


def positions(symbol=None):
    p = {"symbol": symbol} if symbol else None
    return _req("GET", "/fapi/v2/positionRisk", p, signed=True)


def position_of(symbol, position_side):
    for x in positions(symbol):
        if x["symbol"] == symbol and x.get("positionSide") == position_side:
            return x
    return None


def open_orders(symbol=None):
    p = {"symbol": symbol} if symbol else None
    return _req("GET", "/fapi/v1/openOrders", p, signed=True)


def order_status(symbol, order_id):
    """查单笔订单状态（成交监听用）。返回 (订单dict, 错误字符串)"""
    try:
        return _req("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}, signed=True), None
    except BinanceError as e:
        return None, str(e)


def set_leverage(symbol, lev=LEV):
    return _req("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": lev}, signed=True)


# ============================ 下单原语 ============================
def _side_for(dir_, closing=False):
    """dir_: LONG/SHORT（持仓方向）。closing=True 表示平仓方向。"""
    long_ = (dir_.upper() == "LONG")
    if closing:
        long_ = not long_
    return "BUY" if long_ else "SELL"


def _ps(dir_):
    """hedge 模式下必须显式给 positionSide"""
    return dir_.upper() if is_hedge() else "BOTH"


def market_open(symbol, dir_, notional_usdt, lev=LEV):
    """市价开仓。notional_usdt = 保证金 × 杠杆"""
    px = mark_price(symbol)
    qty = fmt_qty(symbol, notional_usdt / px)
    p = {"symbol": symbol, "side": _side_for(dir_), "type": "MARKET",
         "quantity": qty, "positionSide": _ps(dir_)}
    rec = audit("market_open", p, mode="LIVE" if LIVE[0] else "shadow")
    if not LIVE[0]:
        return {"shadow": True, "would_send": p, "mark": px}
    set_leverage(symbol, lev)
    r = post_idempotent("/fapi/v1/order", p, tag="mo")      # 🆕 B7：带幂等键，状态未知不盲目重试
    audit("market_open_resp", p, r, mode="LIVE")
    return r


def limit_open(symbol, dir_, price, notional_usdt, lev=LEV):
    """限价开仓（用于「不追高/不追空」的两笔挂单）"""
    qty = fmt_qty(symbol, notional_usdt / price)
    p = {"symbol": symbol, "side": _side_for(dir_), "type": "LIMIT",
         "timeInForce": "GTC", "price": fmt_price(symbol, price),
         "quantity": qty, "positionSide": _ps(dir_)}
    audit("limit_open", p, mode="LIVE" if LIVE[0] else "shadow")
    if not LIVE[0]:
        return {"shadow": True, "would_send": p}
    set_leverage(symbol, lev)
    r = post_idempotent("/fapi/v1/order", p, tag="lo")      # 🆕 B7
    audit("limit_open_resp", p, r, mode="LIVE")
    return r


def place_tp_limit(symbol, dir_, price, qty):
    """止盈：挂对手方限价单（reduceOnly 语义由 positionSide 表达，hedge 模式不传 reduceOnly）"""
    p = {"symbol": symbol, "side": _side_for(dir_, closing=True), "type": "LIMIT",
         "timeInForce": "GTC", "price": fmt_price(symbol, price),
         "quantity": fmt_qty(symbol, qty), "positionSide": _ps(dir_)}
    audit("place_tp", p, mode="LIVE" if LIVE[0] else "shadow")
    if not LIVE[0]:
        return {"shadow": True, "would_send": p}
    r = post_idempotent("/fapi/v1/order", p, tag="tp")      # 🆕 B7
    audit("place_tp_resp", p, r, mode="LIVE")
    return r


def place_sl_stop_market(symbol, dir_, stop_price, qty):
    """止损：走**币安 Algo Order 接口**（2025-12-09 起 STOP/STOP_MARKET/TAKE_PROFIT/
    TRAILING_STOP_MARKET 已从经典 /fapi/v1/order 下线，旧写法返回 -4120）。

    实测确认（2026-09-13，第一手）：
      POST /fapi/v1/algoOrder 必填 = symbol, side, type, algoType, quantity, triggerPrice
        注意是 **triggerPrice**，不是经典接口的 stopPrice。
      positionSide 非必填但双向持仓下必须给，才能打到正确的那一边。
      **reduceOnly 会被拒**：{"code":-1106,"msg":"Parameter 'reduceonly' sent when not required"}。
      closePosition=true 走 GTE，要求"已有持仓"，无仓时返回 -4509 —— 因此这里用显式 quantity，
      更可验证；持仓被分批止盈削减后要重新挂（见 sync_sl）。
      ⚠️ 该接口**没有 /test 版本**（404），所以只能在有真实持仓时才能端到端验证。
    """
    p = {"symbol": symbol, "side": _side_for(dir_, closing=True), "type": "STOP_MARKET",
         "algoType": "CONDITIONAL", "triggerPrice": fmt_price(symbol, stop_price),
         "quantity": fmt_qty(symbol, qty), "workingType": WORKING_TYPE,
         "positionSide": _ps(dir_)}
    audit("place_sl", p, mode="LIVE" if LIVE[0] else "shadow")
    if not LIVE[0]:
        return {"shadow": True, "would_send": p, "endpoint": "POST /fapi/v1/algoOrder"}
    # 🆕 B7：Algo 条件单接口不支持按幂等键查 → 状态未知时改用「按触发价核对在不在」
    r = post_idempotent("/fapi/v1/algoOrder", p, tag="sl",
                        verify=lambda: algo_present(symbol, stop_price))
    audit("place_sl_resp", p, r, mode="LIVE")
    return r


def open_algo_orders(symbol=None):
    p = {"symbol": symbol} if symbol else None
    return _req("GET", "/fapi/v1/openAlgoOrders", p, signed=True)


def cancel_algo(symbol, algo_id):
    p = {"symbol": symbol, "algoId": algo_id}
    audit("cancel_algo", p, mode="LIVE" if LIVE[0] else "shadow")
    if not LIVE[0]:
        return {"shadow": True, "would_send": p, "endpoint": "DELETE /fapi/v1/algoOrder"}
    return _req("DELETE", "/fapi/v1/algoOrder", p, signed=True)


def sync_sl(symbol, dir_, new_stop, qty):
    """移动/重挂止损（TP1 后移保本损、分批止盈后修正数量）。

    ⚠️ 顺序已改为【先挂新止损 → 确认成功 → 才撤旧止损】。
    评估 P0-2：原来的"先撤后挂"在重挂失败时会让仓位**裸奔**，
    而每笔走到 TP1 的盈利单都必然经过这里 —— 不是小概率意外，是必经路径。
    """
    if not LIVE[0]:
        audit("sync_sl_plan", {"symbol": symbol, "dir": dir_, "new_stop": new_stop,
                               "qty": fmt_qty(symbol, qty)}, mode="shadow")
        return place_sl_stop_market(symbol, dir_, new_stop, qty)

    old_algo = [o.get("algoId") for o in (open_algo_orders(symbol) or [])]
    old_cls = [o.get("orderId") for o in (open_orders(symbol) or [])
               if o.get("type") in ("STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET")]

    new = place_sl_stop_market(symbol, dir_, new_stop, qty)          # ① 先挂新
    new_id = None
    if isinstance(new, dict):
        new_id = new.get("algoId") or new.get("clientAlgoId") or new.get("orderId")
    if not new_id:
        audit("sync_sl_abort", {"symbol": symbol, "new": new,
                               "kept_algo": old_algo, "kept_classic": old_cls},
              "新止损未确认成功 → 保留旧止损不动，避免裸奔")
        raise BinanceError("sync_sl 中止：新止损未确认成功，已保留旧止损（避免裸奔）")

    for aid in old_algo:                                             # ② 确认成功后才撤旧
        try:
            cancel_algo(symbol, aid)
        except BinanceError:
            pass
    for oid in old_cls:
        try:
            cancel_order(symbol, oid)
        except BinanceError:
            pass
    audit("sync_sl_done", {"symbol": symbol, "new_id": new_id,
                           "cancelled_algo": old_algo, "cancelled_classic": old_cls})
    return new


def move_sl(symbol, dir_, new_stop, qty=None):
    """移动止损（TP1 后移保本损）。完整实现见 sync_sl。"""
    if qty is None:
        pos = position_of(symbol, _ps(dir_))
        qty = abs(float(pos.get("positionAmt") or 0)) if pos else 0
    return sync_sl(symbol, dir_, new_stop, qty)


def close_position_market(symbol, dir_, qty=None):
    """市价平仓：qty=None 表示全平（**自己读持仓数量后用显式 quantity**）。

    🔴 2026-09-17 真实小单联调当场抓到的 bug（必须记住）：
      原来 qty=None 时发的是 `closePosition=true`，币安现在直接拒单：
        HTTP 400 {'code': -4136, 'msg': 'Target strategy invalid for orderType MARKET,closePosition true'}
      后果很严重：**平不掉仓**。当场留下 0.14 SOL 的多头持仓，而且那一刻止损/止盈已经
      被"撤单收尾"撤掉了 → 成了一个**没有任何保护的裸仓**（我立刻用手工显式数量的方式平掉了）。
      现在：一律用**显式 quantity**（先读持仓数量）——这也和止损接口用显式 quantity 的理由一致。
    """
    if qty is None:
        try:
            _pos = position_of(symbol, dir_) or {}
            qty = abs(float(_pos.get("positionAmt") or 0))
        except Exception as e:
            raise BinanceError("平仓前读不到持仓数量（不敢用 closePosition=true，币安会报 -4136）：%s"
                               % str(e)[:120])
        if not qty:
            raise BinanceError("平仓失败：交易所当前没有 %s 的 %s 持仓（数量 0）" % (symbol, dir_))
    p = {"symbol": symbol, "side": _side_for(dir_, closing=True), "type": "MARKET",
         "quantity": fmt_qty(symbol, qty), "positionSide": _ps(dir_)}
    audit("close_market", p, mode="LIVE" if LIVE[0] else "shadow")
    if not LIVE[0]:
        return {"shadow": True, "would_send": p}
    r = post_idempotent("/fapi/v1/order", p, tag="cl")      # 🆕 B7：平仓同样带幂等键
    audit("close_market_resp", p, r, mode="LIVE")
    return r


def cancel_order(symbol, order_id):
    p = {"symbol": symbol, "orderId": order_id}
    audit("cancel", p, mode="LIVE" if LIVE[0] else "shadow")
    if not LIVE[0]:
        return {"shadow": True, "would_send": p}
    return _req("DELETE", "/fapi/v1/order", p, signed=True)


def cancel_all(symbol):
    """撤掉该币种**全部**挂单：经典挂单（止盈限价）＋ Algo 挂单（止损）"""
    p = {"symbol": symbol}
    audit("cancel_all", p, mode="LIVE" if LIVE[0] else "shadow")
    if not LIVE[0]:
        return {"shadow": True, "would_send": p,
                "endpoints": ["DELETE /fapi/v1/allOpenOrders", "DELETE /fapi/v1/algoOrder ×N"]}
    r1 = _req("DELETE", "/fapi/v1/allOpenOrders", p, signed=True)
    r2 = []
    for o in (open_algo_orders(symbol) or []):
        try:
            r2.append(cancel_algo(symbol, o.get("algoId")))
        except Exception as e:
            r2.append(str(e))
    return {"classic": r1, "algo": r2}


# ============================ 一笔完整开仓 ============================
def split_tp_qty(symbol, total_qty, tiers):
    """把总数量按档位切分：前 n-1 档向下取整，最后一档吃掉余数（保证不剩零头）"""
    n = max(len(tiers), 1)
    base = round_step(total_qty / n, spec(symbol).get("step"))
    parts, acc = [], 0.0
    for i in range(n):
        if i == n - 1:
            parts.append(round(total_qty - acc, 10))
        else:
            parts.append(base)
            acc += base
    return parts


def open_full_position(symbol, dir_, entry_price, stop, tps, margin=300.0, lev=LEV):
    """
    按用户既有规则开一整笔：
      entry_price 为 None -> 纯市价；有值则按「不追高/不追空」规则决定市价 or 两笔限价
      stop  -> STOP_MARKET
      tps   -> 每档 LIMIT（前 3 档）
    返回执行计划 dict（影子模式下也能完整看到将要发生什么）
    """
    notional = margin * lev
    plan = {"symbol": symbol, "dir": dir_.upper(), "notional": notional,
            "margin": margin, "lev": lev, "entry_legs": [], "tps": [], "sl": None,
            "shadow": not LIVE[0]}
    cur = mark_price(symbol)
    plan["mark_at_plan"] = cur

    # ---- 开仓腿 ----
    if entry_price is None:
        plan["entry_legs"].append({"kind": "market", "notional": notional, "ref_price": cur})
        if not LIVE[0]:
            plan["entry_legs"][-1]["would_send"] = {
                "symbol": symbol, "side": _side_for(dir_), "type": "MARKET",
                "quantity": fmt_qty(symbol, notional / cur), "positionSide": _ps(dir_)}
    else:
        diff = (cur - entry_price) / entry_price
        chase = (dir_.upper() == "LONG" and diff > 0.02) or (dir_.upper() == "SHORT" and diff < -0.02)
        if not chase:
            plan["entry_legs"].append({"kind": "market", "notional": notional,
                                       "ref_price": cur, "why": "差值≤2%，直接市价满仓"})
        else:
            px1 = entry_price * (1.01 if dir_.upper() == "LONG" else 0.99)
            plan["entry_legs"] = [
                {"kind": "limit", "price": fmt_price(symbol, px1), "notional": notional / 2,
                 "why": "不追%s：第1笔" % ("高" if dir_.upper() == "LONG" else "空")},
                {"kind": "limit", "price": fmt_price(symbol, entry_price), "notional": notional / 2,
                 "why": "不追%s：第2笔" % ("高" if dir_.upper() == "LONG" else "空")}]
    # ---- 止盈 + 止损 ----
    ref = float(plan["entry_legs"][0].get("price") or cur)
    tot_qty = round_step(spec(symbol).get("step") and (notional / ref), spec(symbol).get("step"))
    tps = [t for t in (tps or []) if isinstance(t, (int, float))][:3]
    if tps:
        parts = split_tp_qty(symbol, tot_qty, tps)
        for i, (t, q) in enumerate(zip(tps, parts)):
            leg = {"tier": i + 1, "price": fmt_price(symbol, t), "qty": fmt_qty(symbol, q)}
            if not LIVE[0]:
                leg["would_send"] = {"symbol": symbol, "side": _side_for(dir_, True), "type": "LIMIT",
                                     "timeInForce": "GTC", "price": leg["price"],
                                     "quantity": leg["qty"], "positionSide": _ps(dir_)}
            plan["tps"].append(leg)
    else:
        plan["tps_note"] = "无有效止盈位 → 按底线规则不挂止盈，等人工确认"
    if isinstance(stop, (int, float)) and stop > 0:
        plan["sl"] = {"type": "STOP_MARKET", "endpoint": "POST /fapi/v1/algoOrder",
                      "algoType": "CONDITIONAL", "triggerPrice": fmt_price(symbol, stop),
                      "quantity": fmt_qty(symbol, tot_qty), "workingType": WORKING_TYPE,
                      "why": "止损必须用触发单（限价单会立即成交）；且必须走 Algo 接口"}
        if not LIVE[0]:
            plan["sl"]["would_send"] = {
                "symbol": symbol, "side": _side_for(dir_, True), "type": "STOP_MARKET",
                "algoType": "CONDITIONAL", "triggerPrice": plan["sl"]["triggerPrice"],
                "quantity": plan["sl"]["quantity"], "workingType": WORKING_TYPE,
                "positionSide": _ps(dir_)}
    else:
        plan["sl_note"] = "无止损 → 按底线规则不下单"
    audit("open_full_position_plan", plan)
    if LIVE[0]:
        # ===== 先下入场腿 =====
        # ⚠️ 评估 M3：绝不能"有腿失败还照挂全额保护"。止损数量按全额 900U 挂出去，
        #    而实际只成交一半 → 数量错、被拒、或只保护一部分。必须全成功才继续。
        _oids, _fail = [], []
        _has_limit = any(l["kind"] != "market" for l in plan["entry_legs"])
        for leg in plan["entry_legs"]:
            try:
                if leg["kind"] == "market":
                    r = market_open(symbol, dir_, leg["notional"], lev)
                else:
                    r = limit_open(symbol, dir_, float(leg["price"]), leg["notional"], lev)
                if isinstance(r, dict) and r.get("orderId"):
                    _oids.append(r["orderId"])
                else:
                    _fail.append({"leg": leg, "err": "下单未返回 orderId: %s" % str(r)[:120]})
            except BinanceError as e:
                _fail.append({"leg": leg, "err": str(e)[:160]})
                audit("entry_leg_fail", {"leg": leg}, str(e))
        plan["order_ids"] = _oids
        plan["entry_failed"] = _fail
        if _fail:
            audit("entry_abort", {"symbol": symbol, "ok": _oids, "failed": _fail},
                  "有入场腿失败 → 不挂止盈止损，交由上层告警 + 裸仓看门狗接管")
            raise BinanceError(
                "入场腿 %d 条失败（成功 %d 条）：%s —— 已中止挂保护，交由告警+看门狗处理"
                % (len(_fail), len(_oids), _fail[0].get("err")))
        if _has_limit:
            # ⚠️ 限价入场必须【先等成交】再挂止盈止损：
            #    没持仓时挂止损/止盈会被币安拒（或语义错误），所以交给成交监听接管。
            plan["watch_fill"] = True
            audit("await_fill", {"symbol": symbol, "order_ids": _oids,
                                 "note": "等成交后再挂止盈/止损"})
            return plan
        # ===== 市价入场：立刻挂止盈/止损，且【逐个容错并记录成败】=====
        _prot = {"tps": [], "sl": None}
        for t in plan["tps"]:
            try:
                _prot["tps"].append(place_tp_limit(symbol, dir_, float(t["price"]), float(t["qty"])))
            except BinanceError as e:
                _prot["tps"].append({"err": str(e)[:160]})
        if plan.get("sl"):
            try:
                _prot["sl"] = place_sl_stop_market(symbol, dir_, float(plan["sl"]["triggerPrice"]), tot_qty)
            except BinanceError as e:
                _prot["sl"] = {"err": str(e)[:160]}
        plan["protection"] = _prot
        audit("protection_result", {"symbol": symbol}, _prot)
        if plan.get("sl") and isinstance(_prot["sl"], dict) and _prot["sl"].get("err"):
            # 止损没挂上 = 裸奔 → 必须让上层告警（不能静默）
            raise BinanceError("市价仓已建立，但【止损挂单失败】：%s" % _prot["sl"]["err"])
    return plan


# ==================== 🆕 2026-09-17 B7：幂等键 / 503·限频 / 挂单后核对 ====================
# 用户要求：「能够真实下单、**不重复下单**、能够**准确无误挂好单**」。
#
# 为什么必须有幂等键（clientOrderId）：
#   币安返回 503/网关错误/网络超时时，**订单可能已经成交**（官方说法叫"执行状态未知"）。
#   没有幂等键就没法安全重试 —— 盲目重试就是**下重单**（真金白银的重复仓位）。
#   有了幂等键：任何"状态未知"都能**先拿它去查这笔单到底在不在**，再决定重试还是收手。
# 三类错误必须区别对待（这是本模块的核心）：
#   ① 明确被拒（余额不足 -2019 / 低于最小额 -4164 / 参数错）→ **绝不重试**，直接抛
#   ② 限频（-1003 / 429）→ 退避重试（这种是"没下进去"，重试安全）
#   ③ 状态未知（5xx / 超时 / -1006 / -1007）→ **先用幂等键查**：查到=已下单；查不到=用同一个键重试
_CID_SEQ = [0]
_CID_LOCK = threading.Lock()
AMBIG_MARKERS = ("HTTP 5", "HTTP 429", "timed out", "TimeoutError", "URLError",
                 "ConnectionReset", "RemoteDisconnected", "IncompleteRead",
                 "-1007", "-1006", "Unknown", "服务暂时不可用", "Internal error")
RATE_MARKERS = ("-1003", "TOO_MANY_REQUESTS", "-1015")


def new_cid(tag="o"):
    """交易所幂等键。币安硬要求：≤36 字符、只允许 [A-Za-z0-9_-]。"""
    with _CID_LOCK:
        _CID_SEQ[0] += 1
        n = _CID_SEQ[0]
    t = "".join(ch for ch in str(tag or "o") if ch.isalnum())[:8]
    return ("dsh%s%s%04d" % (t, datetime.datetime.utcnow().strftime("%m%d%H%M%S"), n % 10000))[:36]


def order_by_cid(symbol, cid, quiet=False):
    """按幂等键查订单（用来判断"这笔单到底下没下"）。查不到返回 None。"""
    try:
        return _req("GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": cid})
    except BinanceError as e:
        if not quiet:
            audit("order_by_cid_miss", {"symbol": symbol, "cid": cid}, str(e))
        return None


def algo_present(symbol, trigger_price, qty=None, tol=0.002):
    """在"未成交的条件单"里找这笔止损在不在。
    （Algo 条件单接口不支持按幂等键查，所以按**触发价**匹配；数量可选用来看是否一致。）"""
    try:
        rows = open_algo_orders(symbol) or []
    except BinanceError as e:
        audit("algo_present_fail", {"symbol": symbol}, str(e))
        return None
    for r in (rows or []):
        try:
            tp = float(r.get("triggerPrice") or r.get("stopPrice") or 0)
        except Exception:
            tp = 0.0
        if tp and abs(tp - float(trigger_price)) / max(1e-9, float(trigger_price)) <= tol:
            return r
    return None


def post_idempotent(path, params, tag="o", tries=3, verify=None):
    """**带幂等键的下单唯一安全入口**（三处区别对待见文件顶部说明）。
    verify：状态未知时用来核对"在不在"的函数（默认按幂等键查订单；止损传 algo_present）。"""
    p = dict(params)
    cid = p.get("newClientOrderId") or new_cid(tag)
    p["newClientOrderId"] = cid
    last = None
    for i in range(max(1, int(tries))):
        try:
            r = _req("POST", path, p, signed=True)
            if isinstance(r, dict):
                r["_cid"] = cid
            return r
        except BinanceError as e:
            last = e
            msg = str(e)
            if any(m in msg for m in RATE_MARKERS):
                audit("post_ratelimit_retry", {"path": path, "cid": cid, "try": i + 1}, msg)
                time.sleep(1.5 * (i + 1))
                continue
            if not any(m in msg for m in AMBIG_MARKERS):
                raise                       # 明确被拒 → 绝不重试（避免真下重单）
            got = (verify or (lambda: order_by_cid(p.get("symbol"), cid, quiet=True)))()
            if got:
                if isinstance(got, dict):
                    got["_cid"] = cid
                    got["_recovered"] = True
                audit("post_recovered_by_cid", {"path": path, "cid": cid}, got)
                return got
            audit("post_unknown_retry", {"path": path, "cid": cid, "try": i + 1}, msg)
            time.sleep(1.2 * (i + 1))
    raise BinanceError(
        "下单状态未知（已用幂等键 %s 查过、重试 %d 次仍未确认）：%s —— **不要再自动重试**，"
        "请人工到币安核对这笔单是否存在。" % (cid, tries, last))


def bracket_verify(symbol, dir_, stop=None, tps=None, tol=0.004):
    """挂完单后**回头核对**（用户要求"准确无误挂好单"）：
    止损在不在（条件单）、每一档止盈在不在（未成交委托）。
    返回 {"sl": True/False/None, "tps": [...], "missing": [...]}；None = 查不到（读接口失败）。"""
    out = {"sl": None, "tps": [], "missing": []}
    if stop:
        got = algo_present(symbol, float(stop))
        out["sl"] = bool(got)
        if not got:
            out["missing"].append("止损 %s（条件单）" % stop)
    rows = None
    try:
        rows = open_orders(symbol) or []
    except BinanceError as e:
        audit("bracket_verify_fail", {"symbol": symbol}, str(e))
    for t in (tps or []):
        try:
            want = float(t.get("price") if isinstance(t, dict) else t)
        except Exception:
            continue
        hit = None
        for r in (rows or []):
            try:
                px = float(r.get("price") or 0)
            except Exception:
                px = 0.0
            if px and abs(px - want) / max(1e-9, want) <= tol:
                hit = r
                break
        out["tps"].append({"price": want, "ok": bool(hit), "orderId": (hit or {}).get("orderId")})
        if not hit and rows is not None:
            out["missing"].append("止盈 %s（限价单）" % want)
    return out


def after_entry_filled(symbol, dir_, tps, stop, qty):
    """入场成交后：挂止盈（限价）+ 止损（Algo STOP_MARKET），**挂完立刻回头核对**。

    🆕 2026-09-17（B7 配套，用户要求"准确无误挂好单"）：
      · 每一笔都带幂等键（见 place_tp_limit / place_sl_stop_market）；
      · 挂完调 bracket_verify 核对"止损在不在、每一档止盈在不在"，
        把 missing 一起返回 —— 上层会据此**告警**（实盘下这是必须人来看的情况）。
    """
    out = {"tps": [], "sl": None, "verify": None, "missing": []}
    tp_specs = []
    for t in (tps or [])[:3]:
        try:
            _px = float(t["price"]) if isinstance(t, dict) else float(t)
            _q = float(t["qty"]) if isinstance(t, dict) else float(qty)
            out["tps"].append(place_tp_limit(symbol, dir_, _px, _q))
            tp_specs.append({"price": _px})
        except BinanceError as e:
            out["tps"].append(str(e))
            out["missing"].append("止盈挂单失败：%s" % str(e)[:80])
    if stop:
        try:
            out["sl"] = place_sl_stop_market(symbol, dir_, float(stop), qty)
        except BinanceError as e:
            out["sl"] = str(e)
            out["missing"].append("止损挂单失败：%s" % str(e)[:80])
    # ===== 🆕 B7：挂完**回头核对**，别只看返回值就以为挂上了 =====
    try:
        _bv = bracket_verify(symbol, dir_, stop=(float(stop) if stop else None), tps=tp_specs)
        out["verify"] = _bv
        if _bv.get("missing"):
            out["missing"].extend(_bv["missing"])
    except Exception as e:
        out["missing"].append("挂单核对异常：%s" % str(e)[:80])
    audit("after_entry_filled", {"symbol": symbol, "dir": dir_, "tps": tps,
                                 "stop": stop, "qty": qty}, out)
    return out


# ============================ 自检 / 命令行 ============================
def selftest(symbol="BTCUSDT", dir_="LONG", notional=900.0, stop=0.0, tp=None):
    """用币安官方 POST /fapi/v1/order/test 校验签名与参数（**不会真正下单**）"""
    print("=" * 66)
    print("下单层自检（币安 order/test 接口：校验参数但绝不下单）")
    print("=" * 66)
    print("LIVE 总开关 : %s" % ("开着（注意）" if LIVE[0] else "关闭（影子模式）"))
    print("持仓模式    : %s" % ("双向 hedge" if is_hedge() else "单向 one-way"))
    print("币种        : %s" % symbol)
    sp = spec(symbol)
    print("合约规格    : tick=%s step=%s minQty=%s minNotional=%s"
          % (sp.get("tick"), sp.get("step"), sp.get("minQty"), sp.get("minNotional")))
    cur = mark_price(symbol)
    print("当前标记价  : %s" % cur)
    qty = fmt_qty(symbol, notional / cur)
    print("名义 %.0fU -> 数量 %s（%d 倍杠杆，保证金 %.0fU）" % (notional, qty, LEV, notional / LEV))
    classic = [
        ("市价开仓", {"symbol": symbol, "side": _side_for(dir_), "type": "MARKET",
                      "quantity": qty, "positionSide": _ps(dir_)}),
        ("止盈限价", {"symbol": symbol, "side": _side_for(dir_, True), "type": "LIMIT",
                      "timeInForce": "GTC", "price": fmt_price(symbol, cur * 1.05),
                      "quantity": fmt_qty(symbol, float(qty) / 3), "positionSide": _ps(dir_)}),
    ]
    ok = 0
    for name, p in classic:
        try:
            _req("POST", "/fapi/v1/order/test", p, signed=True, test=True)
            print("[ OK ] %-14s 参数被币安接受  %s" % (name, json.dumps(p, ensure_ascii=False)))
            ok += 1
        except BinanceError as e:
            print("[FAIL] %-14s %s" % (name, e))
            print("       参数: %s" % json.dumps(p, ensure_ascii=False))

    # 止损走 Algo 接口，且该接口没有 /test 版本 → 用 quantity="0" 做参数校验：
    # 若返回"数量<=0"说明其余参数全部被接受；若返回 -1102 则说明还有必填参数缺失。
    sl_payload = {"symbol": symbol, "side": _side_for(dir_, True), "type": "STOP_MARKET",
                  "algoType": "CONDITIONAL", "triggerPrice": fmt_price(symbol, cur * 0.95),
                  "quantity": "0", "workingType": WORKING_TYPE, "positionSide": _ps(dir_)}
    try:
        _req("POST", "/fapi/v1/algoOrder", sl_payload, signed=True, test=True)
        print("[WARN] 止损参数校验：意外成功，请人工确认（不应该发生）")
    except BinanceError as e:
        s = str(e)
        if "-4003" in s or "less than or equal to zero" in s:
            print("[ OK ] %-14s 参数全部被接受（用 quantity=0 挡下，未创建委托）" % "止损STOP_MARKET")
            print("       实际会发: %s" % json.dumps(
                {**sl_payload, "quantity": fmt_qty(symbol, float(qty))}, ensure_ascii=False))
            ok += 1
        else:
            print("[FAIL] %-14s %s" % ("止损STOP_MARKET", s))
            print("       参数: %s" % json.dumps(sl_payload, ensure_ascii=False))

    print("-" * 66)
    print("通过 %d/3。全程未创建任何真实委托（订单簿已复核为空）。" % ok)
    return ok == 3


def check():
    """只读体检"""
    a = account()
    print("=" * 66)
    print("下单层只读检查")
    print("=" * 66)
    print("LIVE 总开关     : %s" % ("开着" if LIVE[0] else "关闭（影子模式，不会发单）"))
    print("持仓模式        : %s" % ("双向 hedge ✅" if is_hedge() else "单向 one-way"))
    print("可交易 canTrade : %s" % a.get("canTrade"))
    print("可用余额        : %s USDT ｜ 钱包 %s USDT"
          % (a.get("availableBalance"), a.get("totalWalletBalance")))
    ps_ = positions()
    live = [x for x in ps_ if float(x.get("positionAmt") or 0) != 0]
    print("真实持仓        : %d 笔" % len(live))
    for x in live:
        print("   %s %s 数量 %s 开仓 %s 杠杆 %sx"
              % (x["symbol"], x.get("positionSide"), x["positionAmt"],
                 x.get("entryPrice"), x.get("leverage")))
    oo = open_orders()
    print("真实挂单(经典): %d 笔" % len(oo))
    for o in oo:
        print("   %s %s %s 价 %s 量 %s side=%s ps=%s"
              % (o["symbol"], o["type"], o.get("status"), o.get("price"),
                 o.get("origQty"), o.get("side"), o.get("positionSide")))
    try:
        ao = open_algo_orders()
        print("真实挂单(Algo止损): %d 笔" % len(ao))
        for o in ao:
            print("   %s %s 触发价 %s 量 %s ps=%s algoId=%s"
                  % (o.get("symbol"), o.get("type") or o.get("orderType"),
                     o.get("triggerPrice") or o.get("stopPrice"), o.get("quantity") or o.get("origQty"),
                     o.get("positionSide"), o.get("algoId")))
    except Exception as e:
        print("真实挂单(Algo止损): 读取失败 %s" % e)
    bal = float(a.get("availableBalance") or 0)
    if bal <= 0:
        print("")
        print("⚠️ 可用余额为 0 → 即使打开 LIVE 也无法真实成交，请先充值 USDT。")
    return True


def _load_runtime_live():
    global LEV
    try:
        cfg = json.load(open(RUNTIME, encoding="utf-8"))
        LIVE[0] = bool(cfg.get("live_trading", False))
        if cfg.get("leverage"):
            LEV = int(cfg["leverage"])
        return cfg
    except Exception:
        return {}


if __name__ == "__main__":
    _load_runtime_live()
    args = sys.argv[1:]
    if "--check" in args:
        check()
    elif "--selftest" in args:
        sym = args[args.index("--selftest") + 1] if len(args) > args.index("--selftest") + 1 else "BTCUSDT"
        selftest(symbol=sym)
    elif "--shadow" in args:
        i = args.index("--shadow")
        coin = args[i + 1] if len(args) > i + 1 else "BTC"
        d = args[i + 2] if len(args) > i + 2 else "LONG"
        entry = float(args[i + 3]) if len(args) > i + 3 else None
        stop = float(args[i + 4]) if len(args) > i + 4 else None
        tps = [float(x) for x in args[i + 5].split(",")] if len(args) > i + 5 else []
        plan = open_full_position(coin.upper() + "USDT", d, entry, stop, tps)
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        print(__doc__)
