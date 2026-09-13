#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
只读对账层：币安真实账户  vs  纸面记录
------------------------------------------------
• 全程只调用 GET 接口，**绝不下单、绝不平仓、绝不改任何设置**。
• 用途：在真实下单层写出来之前，先把「真实账户长什么样」和「纸面记了什么」摆在一起看，
        任何偏差立刻暴露 —— 尤其"真实有仓但纸面没记录"这种危险情况。

用法：
    python binance_reconcile.py            # 打印对账表
    python binance_reconcile.py --push     # 同时推到飞书
    python binance_reconcile.py --audit    # 额外复核 API Key 权限（提现是否关闭、IP 白名单）
"""
import os
import sys
import json
import time
import hmac
import hashlib
import datetime
import urllib.parse
import urllib.request
import urllib.error

BASE = os.environ.get("SIGNAL_BOT_BASE", "/home/ubuntu/signal-bot")
CFG = os.path.join(BASE, "config.json")
STATE = os.path.join(BASE, "v21", "state.json")
TRADES = os.path.join(BASE, "v21", "trades_dryrun.jsonl")
NOTIFY = os.path.join(BASE, "notify.json")
FAPI = "https://fapi.binance.com"
SAPI = "https://api.binance.com"
CST = datetime.timezone(datetime.timedelta(hours=8))

PUSH = "--push" in sys.argv
AUDIT = "--audit" in sys.argv


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


cfg = load_json(CFG)
B = cfg.get("binance") or {}
KEY = (B.get("api_key") or "").strip()
SEC = (B.get("api_secret") or "").strip()

OUT = []


def p(s=""):
    print(s)
    OUT.append(s)


def call(path, params=None, base=FAPI):
    """签名 GET。任何情况下都不发 POST。"""
    q = dict(params or {})
    q["timestamp"] = int(time.time() * 1000)
    q.setdefault("recvWindow", 5000)
    qs = urllib.parse.urlencode(q)
    q["signature"] = hmac.new(SEC.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = base + path + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"X-MBX-APIKEY": KEY}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            raw = json.loads(raw)
        except Exception:
            pass
        return None, raw
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)


def push(text):
    n = load_json(NOTIFY)
    url = n.get("webhook") or n.get("url") or ""
    if not url:
        return "notify.json 里没有 webhook"
    body = json.dumps({"msg_type": "text", "content": {"text": text}}).encode()
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode()[:80]
    except Exception as e:
        return "推送失败: %s" % e


# ---------------- 1. 纸面记录 ----------------
st = load_json(STATE)
paper = st.get("open") or {}
paper_hist = []
try:
    with open(TRADES, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    paper_hist.append(json.loads(line))
                except Exception:
                    pass
except Exception:
    pass

# ---------------- 2. 真实账户 ----------------
if not KEY or not SEC:
    p("=" * 66)
    p("❌ config.json 里没有 binance.api_key / api_secret，无法对账")
    p("=" * 66)
    print("（本次只读对账未执行）")
    sys.exit(2)

acct, err = call("/fapi/v2/account")
if err:
    p("=" * 66)
    p("❌ 币安账户读取失败：%s" % err)
    p("=" * 66)
    p("可能原因：IP 未加白名单（服务器 IP 必须绑定）/ key 被禁用 / 网络不通")
    sys.exit(3)

pos_raw, _ = call("/fapi/v2/positionRisk")
pos_raw = pos_raw or []
real = {}
for x in pos_raw:
    amt = float(x.get("positionAmt") or 0)
    if amt != 0:
        real[x["symbol"]] = x

orders, _ = call("/fapi/v1/openOrders")
orders = orders or []
inc, _ = call("/fapi/v1/income", {"incomeType": "REALIZED_PNL", "limit": 1000})
inc = inc or []
real_pnl = sum(float(i.get("income") or 0) for i in inc)
real_fee = None
fee_inc, _ = call("/fapi/v1/income", {"incomeType": "COMMISSION", "limit": 1000})
if isinstance(fee_inc, list):
    real_fee = sum(float(i.get("income") or 0) for i in fee_inc)

paper_pnl = 0.0
# 纸面成交是「事件流」：同一个持仓会被追加多条记录。
# 与 trade_table.py 用同一套口径：按 (coin, t_open) 取最后一条，再累加已结单盈亏。
_latest = {}
for _r in paper_hist:
    _latest[(_r.get("coin"), _r.get("t_open"))] = _r
paper_pnl = sum((_r.get("pnl") or 0) for _r in _latest.values() if _r.get("status") == "CLOSED")
paper_closed = [(_k[0], (_r.get("pnl") or 0)) for _k, _r in _latest.items() if _r.get("status") == "CLOSED"]

now = datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
p("=" * 66)
p("只读对账  币安真实账户 vs 纸面记录   %s" % now)
p("=" * 66)

# ---------------- 3. 真实账户 ----------------
p("")
p("【1】币安合约账户（真实）")
p("  可用余额      %s USDT" % (acct.get("availableBalance") or "0"))
p("  钱包余额      %s USDT" % (acct.get("totalWalletBalance") or "0"))
p("  未实现盈亏    %s USDT" % (acct.get("totalUnrealizedProfit") or "0"))
p("  维持保证金    %s USDT" % (acct.get("totalMaintMargin") or "0"))
p("  可交易        canTrade=%s" % acct.get("canTrade"))
p("  已实现盈亏(全历史, 最近1000条)  %+.4f USDT" % real_pnl)
if real_fee is not None:
    p("  手续费合计(全历史, 最近1000条)  %+.4f USDT" % real_fee)

# ---------------- 4. 真实持仓 ----------------
p("")
p("【2】真实持仓（%d 笔）" % len(real))
if not real:
    p("  （无）")
for sym, x in sorted(real.items()):
    amt = float(x.get("positionAmt") or 0)
    p("  %-12s %s 数量 %-12s 开仓 %-12s 标记 %-12s 未实现 %+.4fU 杠杆 %sx"
      % (sym, "多" if amt > 0 else "空", amt, x.get("entryPrice"), x.get("markPrice"),
         float(x.get("unRealizedProfit") or 0), x.get("leverage")))

p("")
p("【3】真实挂单（%d 笔）" % len(orders))
if not orders:
    p("  （无）")
for o in orders:
    p("  %-12s %s %s 价 %s 量 %s side=%s" % (o.get("symbol"), o.get("type"),
                                             o.get("positionSide"), o.get("price"),
                                             o.get("origQty"), o.get("side")))

# ---------------- 5. 纸面持仓 ----------------
p("")
p("【4】纸面持仓（%d 笔）" % len(paper))
if not paper:
    p("  （无）")
for coin, t in sorted(paper.items()):
    p("  %-8s %-5s 入场 %-12s 止损 %-12s 止盈 %-28s 剩余 %.0f%%"
      % (coin, t.get("dir"), t.get("entry"), t.get("sl"),
         str(t.get("tps") or "未读到")[:28], (t.get("remaining", 1.0)) * 100))

# ---------------- 6. 对账 ----------------
p("")
p("【5】对账结果")
paper_syms = set("%sUSDT" % c for c in paper)
real_syms = set(real)
only_paper = sorted(paper_syms - real_syms)
only_real = sorted(real_syms - paper_syms)
both = sorted(paper_syms & real_syms)

problems = 0
if only_paper:
    p("  · 纸面有仓、真实无仓（%d 笔）：%s" % (len(only_paper), "、".join(only_paper)))
    p("    ↳ 这是【预期】状态：真实下单层还没写，机器人目前 100% 纸面。")
for s in only_real:
    problems += 1
    p("  ⚠️ 真实有仓、纸面无记录：%s 数量 %s —— 手工开的仓，或代码丢了状态，必须人工确认！"
      % (s, real[s].get("positionAmt")))
for s in both:
    coin = s[:-4]
    t = paper.get(coin, {})
    amt = float(real[s].get("positionAmt") or 0)
    rdir = "LONG" if amt > 0 else "SHORT"
    same = (rdir == (t.get("dir") or "").upper())
    if not same:
        problems += 1
    p("  %s %s：真实 %s 数量 %s @%s ｜ 纸面 %s 入场 %s"
      % ("✅" if same else "⚠️", s, rdir, amt, real[s].get("entryPrice"),
         t.get("dir"), t.get("entry")))
    if not same:
        p("     ↳ 方向不一致，必须人工确认！")

# ---------------- 7. API Key 权限复核（--audit，安全项） ----------------
if AUDIT:
    p("")
    p("【7】API Key 权限复核（安全项）")
    rest, rerr = call("/sapi/v1/account/apiRestrictions", base=SAPI)
    if rerr or not isinstance(rest, dict):
        p("  ⚠️ 读不到权限信息：%s" % rerr)
    else:
        wd = rest.get("enableWithdrawals")
        fu = rest.get("enableFutures")
        ipr = rest.get("ipRestrict")
        spot = rest.get("enableSpotAndMarginTrading")
        p("  提现 enableWithdrawals = %s  %s"
          % (wd, "✅ 已关闭（必须一直保持）" if wd is False else "❌ 危险！提现是开着的，立刻去币安关掉"))
        p("  合约 enableFutures     = %s" % fu)
        p("  现货/杠杆              = %s  %s"
          % (spot, "✅ 已关闭" if spot is False else "⚠️ 开着（非必需）"))
        p("  IP 白名单 ipRestrict   = %s  %s"
          % (ipr, "✅ 已限制" if ipr else "⚠️ 未限制，建议开启"))
        p("  可交易 canTrade        = %s" % acct.get("canTrade"))
        if wd is not False or not ipr:
            problems += 1

p("")
p("【8】盈亏对照")
p("  纸面已实现盈亏   %+.2f USDT" % paper_pnl)
if paper_closed:
    p("    · 已结单明细：%s" % "、".join("%s %+.1fU" % (c, v) for c, v in paper_closed))
p("  真实已实现盈亏   %+.4f USDT" % real_pnl)
if not real and paper:
    p("  ↳ 真实账户为空是预期的（尚未接真单），两边不可比。")

p("")
if problems == 0:
    p("结论：未发现危险偏差。（真实账户为空 / 与纸面各自独立，属当前预期状态）")
else:
    p("结论：发现 %d 处需要人工确认的偏差 ⚠️" % problems)
p("=" * 66) 

if PUSH:
    txt = "\n".join(OUT)
    if len(txt) > 3500:
        txt = txt[:3500] + "\n…（已截断，完整内容在服务器运行输出里）"
    print("\n[推送飞书] " + str(push(txt)))
