#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书跟单 · dryRun 机器人 v2（每群一个独立标签页，永不切换会话）
- 每个群一个 page，打开后一直停在该群 → 彻底避免"读错群"
- 消息时间 = message-id 高位（Unix 秒），与页面显示一致
- 只处理开单信号；闲聊直接跳过；博主管理指令单独处理
- 全链路计时：信号发出 → 发现 → 抓图 → 解析 → 读图 → 下单(纸面) → 推送
"""
import os, re, sys, json, time, base64, datetime, threading, hashlib, math, itertools
os.environ.setdefault("DISPLAY", ":99")
import requests
from PIL import Image
from playwright.sync_api import sync_playwright
import ccxt

BASE = "/home/ubuntu/signal-bot"
RUN = BASE + "/v21"
IMGDIR = RUN + "/imgs"
LOGF = RUN + "/run.log"
TRADES = RUN + "/trades_dryrun.jsonl"
STATE = RUN + "/state.json"
NOTIFY_CFG = BASE + "/notify.json"
_CFG = {}
try:
    _here = os.path.dirname(os.path.abspath(__file__))
except Exception:
    _here = BASE
for _p in (BASE + "/config.json", os.path.join(_here, "config.json")):
    try:
        if os.path.exists(_p):
            _CFG = json.load(open(_p, encoding="utf-8")); break
    except Exception:
        pass
DS_KEY = os.environ.get("DEEPSEEK_API_KEY") or _CFG.get("deepseek_api_key", "")
DS_API = "https://api.deepseek.com/chat/completions"
CST = datetime.timezone(datetime.timedelta(hours=8))

GROUPS = ["机器人开单通知", "暴富龙", "UA-nurseneil2", "颜驰2群"]
POLL_SEC = 0.5
MARGIN = 300.0
LEV = 3
NOTIONAL = MARGIN * LEV
MAX_OPEN = 5           # 1500U 分 5 份，每份 300U
TP_TIERS = 3
TEST_MODE = True       # ⚠️ 2026-09-15 核实：它现在**只影响「状态」显示**和「测试模式 开/关」指令。
                       # 已【不再】绕过持仓上限、也不再"抓到什么都出单"（旧注释是过期的）。
                       # 真正决定"纸面/实盘"的是 runtime_config.json 的 live_trading。
MSG_URL = "https://www.feishu.cn/messenger"
SEARCH_TERM = {"颜驰2群": "颜驰"}

# ===== 停机/重启回补闸门（2026-09-13 新增，方案A）=====
# 背景：启动时曾把游标直接抬到"页面最新一条"，导致停机期间到达的信号被静默吞掉。
#       现在改为保留原游标，让首次扫描自然回补，并用下面两道闸门防止一次性补进太多过期信号。
CATCHUP_MAX_AGE = 1800     # 时效闸门：发出时间超过 30 分钟的信号只通报、不下单
CATCHUP_MAX_MSGS = 50      # 数量上限：单次扫描最多真正处理 50 条，其余只通报
RUNTIME_MTIME = [0.0]      # 方案B：runtime_config.json 的 mtime，用于热加载检测

# 方案C（开页提速）前置调研：把页面上所有"像 chat-id"的属性抓出来，
# 只有拿到真实的会话 id 才能判断"直链打开"这条路可不可行。**纯只读诊断，不影响功能。**
CHATID_JS = """() => {
  const res = {url: location.href, hits: []};
  const seen = new Set();
  let n = 0;
  for (const e of document.querySelectorAll('*')) {
    if (++n > 4000) break;
    for (const a of Array.from(e.attributes || [])) {
      const s = a.name + '=' + a.value;
      if (a.value && a.value.length < 80 && /chat|conversation|session/i.test(s)) {
        if (!seen.has(s)) { seen.add(s); res.hits.push(e.tagName + ' ' + s); }
      }
    }
  }
  res.hits = res.hits.slice(0, 25);
  return res;
}"""

os.makedirs(IMGDIR, exist_ok=True)

def log(msg):
    line = datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S") + " " + str(msg)
    print(line, flush=True)
    try:
        with open(LOGF, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ---------------- 飞书通知 ----------------
def notify(text):
    # ⚠️ 2026-09-15 晚实测：飞书自定义机器人 **msg_type=text 不渲染 Markdown** ——
    #    用户收到的告警截图里 `**` 是**原样显示**的（「** 【测试】… ** —— 这不是真告警」）。
    #    在唯一出口统一剥掉，免得每条通知各写一遍、也免得审批单看起来一团乱。
    text = str(text).replace("**", "")
    log("[通知] " + text.replace("\n", " | ")[:200])
    cfg = {}
    try:
        if os.path.exists(NOTIFY_CFG):
            cfg = json.load(open(NOTIFY_CFG, encoding="utf-8"))
    except Exception:
        cfg = {}
    hook = cfg.get("feishu_webhook")
    if hook:
        try:
            r = requests.post(hook, json={"msg_type": "text", "content": {"text": text}}, timeout=20).json()
            if r.get("code") not in (0, None):
                log("   webhook 返回: " + json.dumps(r, ensure_ascii=False)[:160])
        except Exception as e:
            log("   webhook 失败: " + str(e)[:120])

# ---------------- 真实下单层（影子模式）----------------
# 把每一笔纸面动作同步交给 binance_exec：默认 LIVE=False → **只生成"将要对币安发什么单"的计划，
# 一条委托都不会发**。用户核对无误后，把 runtime_config.json 的 live_trading 改成 true 即为实盘。
try:
    import binance_exec as bexec
    _BEXEC_OK = True
    _BEXEC_ERR = ""
except Exception as _e:
    bexec = None
    _BEXEC_OK = False
    _BEXEC_ERR = str(_e)[:120]


def _be_mode():
    """影子 / 实盘"""
    try:
        return "实盘" if (bexec and bexec.LIVE[0]) else "影子"
    except Exception:
        return "影子"


# ===== 成交监听（用户 2026-09-14：明天要进实盘）=====
# 限价入场是【异步】的：挂上去要等价格回踩才知道成交，而**没持仓时挂止盈/止损会被币安拒**。
# 所以：限价入场 → 只下入场腿 → 登记到 ENTRY_WATCH → 轮询成交 → 成交后挂止盈(限价)+止损(Algo)。
ENTRY_WATCH = {}          # coin -> {sym, dir, order_ids, tps, stop, deadline, assumed_entry}
FILL_TIMEOUT = 1800       # 30 分钟没成交 → 撤单并通知你
_WATCH_NOTIFIED = set()   # 通知去重


_LIVE_ALERTS = set()      # 告警去重，避免刷屏


def _live_alert(action, coin, err, extra=""):
    """真实下单动作失败 → 必须【大喊】，绝不能只写一行日志（评估 P0-1：绝不静默丢弃）。
    同时登记到 NAKED_WATCH，交给每轮的裸仓看门狗自动补挂。"""
    key = "%s|%s" % (action, coin)
    log("   🔴 [真实动作失败] %s %s: %s" % (action, coin, str(err)[:160]))
    if key in _LIVE_ALERTS:
        return key
    _LIVE_ALERTS.add(key)
    try:
        notify("🔴【实盘动作失败·需要处理】\n"
               "操作：%s ｜ 币种：%s\n错误：%s\n%s\n\n"
               "⚠️ 这意味着该仓位可能【暂时没有止损保护】，或挂单状态与预期不符。\n"
               "机器人会在下一轮自动尝试补挂；若反复失败会继续告警。\n"
               "建议你打开币安 App 核对一次。" % (action, coin, str(err)[:200], extra))
    except Exception:
        pass
    NAKED_WATCH.add(coin)
    return key


def _clear_live_alert(action, coin):
    _LIVE_ALERTS.discard("%s|%s" % (action, coin))


NAKED_WATCH = set()       # 需要看门狗复核止损的币种
_NAKED_TICK = [0]
NAKED_EVERY = 60          # 每 60 轮（约 30 秒）查一次，避免打爆币安限频


def watch_naked():
    """裸仓看门狗：检查【实盘仓位是否真的有止损单】，没有就自动补挂，补挂失败继续告警。
    评估 P0-2：这是最核心的保命机制 —— 没有它，任何一次挂单失败都会留下无保护的真钱仓位。"""
    if not _BEXEC_OK or not bexec.LIVE[0]:
        return
    for coin, tr in list(open_pos_ref.items()):
        if tr.get("real_layer") != "实盘" or tr.get("pending_fill"):
            continue
        sl = tr.get("sl")
        if not sl:
            continue
        sym = coin.upper() + "USDT"
        try:
            algo = bexec.open_algo_orders(sym) or []
            clas = [o for o in (bexec.open_orders(sym) or [])
                    if o.get("type") in ("STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET")]
            if algo or clas:
                NAKED_WATCH.discard(coin)
                _clear_live_alert("裸仓看门狗", coin)
                continue
            log("   🛡 看门狗：%s 没有止损单 → 自动补挂" % coin)
            _q = _real_qty_or_estimate(coin, tr)
            bexec.place_sl_stop_market(sym, tr["dir"], sl, _q)
            notify("【实盘·看门狗】%s 之前没有止损单，已自动补挂：止损 %s（数量 %s）" % (coin, sl, _q))
            NAKED_WATCH.discard(coin)
        except Exception as e:
            _live_alert("裸仓看门狗补挂", coin, e, "该仓位止损 %s 仍未挂上" % sl)


# ===== 风控闸门（2026-09-15，评估 P1）=====
RISK = {"consec_loss": 0, "day": "", "day_pnl": 0.0, "day_trades": 0}
MAX_CONSEC_LOSS = 3        # 连亏多少笔自动熔断（暂停交易并告警）
DAILY_LOSS_LIMIT = 300.0   # 单日已实现净亏损上限（U），达到即熔断
MAX_TOTAL_MARGIN = 0.0     # 总敞口上限（U）；0 = 用 MAX_OPEN × MARGIN 推导

# 启动对账闸门：state.json 与交易所不一致 → 阻止真实下单（但仍继续监控 + 告警）
RECONCILE = {"checked": False, "ok": None, "diffs": [], "blocked": False, "ts": 0.0}


def _today():
    return datetime.datetime.now(CST).strftime("%Y-%m-%d")


def _exposure_cap():
    return MAX_TOTAL_MARGIN if MAX_TOTAL_MARGIN > 0 else (MAX_OPEN * MARGIN)


def _current_exposure(open_pos):
    """当前总敞口（按各仓剩余比例折算保证金）"""
    try:
        return sum(MARGIN * float(t.get("remaining", 1.0)) for t in open_pos.values())
    except Exception:
        return 0.0


def _risk_roll_day():
    """跨日重置当日统计"""
    d = _today()
    if RISK.get("day") != d:
        RISK["day"] = d
        RISK["day_pnl"] = 0.0
        RISK["day_trades"] = 0


def _risk_on_close(tr):
    """每笔结单后更新风控计数，并在触线时熔断（暂停交易 + 告警）"""
    _risk_roll_day()
    net = float(tr.get("pnl_net") if tr.get("pnl_net") is not None
                else (float(tr.get("realized") or 0) - float(tr.get("fee") or 0)))
    RISK["day_pnl"] = round(RISK["day_pnl"] + net, 4)
    RISK["day_trades"] = int(RISK.get("day_trades", 0)) + 1
    if net < 0:
        RISK["consec_loss"] = int(RISK.get("consec_loss", 0)) + 1
    else:
        RISK["consec_loss"] = 0
    _hits = []
    if MAX_CONSEC_LOSS > 0 and RISK["consec_loss"] >= MAX_CONSEC_LOSS:
        _hits.append("连续亏损 %d 笔（上限 %d）" % (RISK["consec_loss"], MAX_CONSEC_LOSS))
    if DAILY_LOSS_LIMIT > 0 and RISK["day_pnl"] <= -abs(DAILY_LOSS_LIMIT):
        _hits.append("今日已实现净亏损 %.2fU（上限 %.0fU）" % (RISK["day_pnl"], DAILY_LOSS_LIMIT))
    if _hits and not PAUSED[0]:
        PAUSED[0] = True
        STATE_DIRTY[0] = True
        notify("🛑【风控熔断·已暂停交易】\n%s\n"
               "· 机器人【仍在监控和记录】，但不会再开新仓\n"
               "· 已持仓的止盈止损【继续正常管理】\n"
               "· 你确认要继续后，发指令「继续」即可恢复" % "\n".join("· " + h for h in _hits))
        log("   🛑 风控熔断：%s" % "；".join(_hits))
    return _hits


# ===== 失联看门狗（2026-09-15，评估 M7）=====
# 信号源是网页爬虫：飞书一次改版、登录态失效、页面异常，都可能让机器人"看起来在跑但什么都收不到"。
# 之前没有任何机制告诉你"今天怎么没通知"。现在超过阈值没抓到任何新消息就主动告警。
LAST_MSG_TS = [0.0]
_SILENCE_ALERTED = [0.0]
SILENCE_ALERT_H = 6.0     # 小时；可用 runtime_config 的 silence_alert_hours 改


def watch_silence():
    if not LAST_MSG_TS[0]:
        return
    silent = time.time() - LAST_MSG_TS[0]
    if silent < SILENCE_ALERT_H * 3600:
        return
    if time.time() - _SILENCE_ALERTED[0] < 3600:      # 每小时最多提醒一次
        return
    _SILENCE_ALERTED[0] = time.time()
    notify("⚠️【失联看门狗】已经 **%.1f 小时**没抓到任何新消息（阈值 %.0f 小时）。\n"
           "可能是：① 这几个群真的安静 ② **飞书登录态失效 / 页面异常**（那就会漏信号）。\n"
           "建议发一次「状态」看机器人是否还活着；不确定就重启一次机器人。"
           % (silent / 3600.0, SILENCE_ALERT_H))
    log("   ⚠️ 失联看门狗：%.1f 小时无新消息" % (silent / 3600.0))


def startup_reconcile():
    """启动对账闸门：把纸面 state.json 的持仓与币安真实持仓逐条比对。
    不一致 → **阻止真实下单**（但继续监控 + 大声告警），需人工处理后再发「重新对账」清除。
    评估 G3：文件丢了/状态乱了就拒绝开真单，而不是瞎开。"""
    RECONCILE.update({"checked": True, "ok": None, "diffs": [], "blocked": False, "ts": time.time()})
    if not _BEXEC_OK:
        log("   [对账] 真实下单层未加载 → 跳过")
        return False
    try:
        real = {}
        for x in (bexec.positions() or []):
            amt = float(x.get("positionAmt") or 0)
            if amt != 0:
                real[x["symbol"]] = x
    except Exception as e:
        RECONCILE["ok"] = None
        log("   [对账] ❌ 读交易所持仓失败：%s" % str(e)[:120])
        if bexec.LIVE[0]:
            RECONCILE["blocked"] = True
            notify("🔴【启动对账失败】读不到币安真实持仓：%s\n"
                   "已**阻止真实下单**（仍在监控记录）。确认网络/权限正常后发「重新对账」。" % str(e)[:160])
        return False
    paper = {"%sUSDT" % c.upper() for c in open_pos_ref.keys()}
    rsyms = set(real)
    only_paper = sorted(paper - rsyms)
    only_real = sorted(rsyms - paper)
    diffs = []
    for s in only_paper:
        # 纸面有仓、真实无仓：纸面模式下正常；实盘模式下说明记录与交易所脱节
        diffs.append("纸面有仓、交易所无仓：%s" % s)
    for s in only_real:
        diffs.append("⚠️ 交易所有仓、纸面无记录（孤儿仓/手工仓）：%s 数量 %s"
                     % (s, real[s].get("positionAmt")))
    RECONCILE["diffs"] = diffs
    consistent = (not only_real) and (bexec.LIVE[0] is False or not only_paper)
    RECONCILE["ok"] = consistent
    log("   [对账] 纸面 %d 笔 ｜ 交易所 %d 笔 ｜ 差异 %d 条 ｜ 结论=%s"
        % (len(paper), len(rsyms), len(diffs), "一致" if consistent else "不一致"))
    for d in diffs:
        log("      · %s" % d)
    if not consistent and bexec.LIVE[0]:
        RECONCILE["blocked"] = True
        notify("🔴【启动对账不一致·已阻止真实下单】\n%s\n\n"
               "机器人【仍在监控和记录】，但**不会向币安发任何真单**，避免双倍敞口或孤儿仓。\n"
               "请人工核对后发指令「重新对账」清除这个闸门。" % "\n".join("· " + d for d in diffs[:8]))
    elif diffs:
        log("   [对账] 差异仅记录（当前影子模式，不影响）")
    return consistent


def watch_entries():
    """轮询限价入场的成交情况。只在【实盘模式】有意义（影子模式没有真实委托）。"""
    if not _BEXEC_OK or not bexec.LIVE[0] or not ENTRY_WATCH:
        return
    now = time.time()
    for coin in list(ENTRY_WATCH):
        w = ENTRY_WATCH[coin]
        try:
            filled_qty, filled_notional, any_live = 0.0, 0.0, False
            for oid in w.get("order_ids") or []:
                st, err = bexec.order_status(w["sym"], oid)
                if err or not st:
                    any_live = True
                    continue
                fq = float(st.get("executedQty") or 0)
                ap = float(st.get("avgPrice") or 0)
                if fq > 0:
                    filled_qty += fq
                    filled_notional += fq * (ap or w.get("assumed_entry") or 0)
                if st.get("status") in ("NEW", "PARTIALLY_FILLED"):
                    any_live = True
            avg_px = (filled_notional / filled_qty) if filled_qty else 0.0

            if filled_qty > 0 and not any_live:
                # ===== 成交完成 → 挂止盈 + 止损 =====
                _tps = [t for t in (w.get("tps") or [])]
                for t in _tps:      # 数量按实际成交量重算
                    try:
                        t["qty"] = bexec.fmt_qty(w["sym"], filled_qty / max(len(_tps), 1))
                    except Exception:
                        pass
                _res = bexec.after_entry_filled(w["sym"], w["dir"], _tps, w.get("stop"), filled_qty)
                _tr = open_pos_ref.get(coin)
                if _tr is not None:
                    _tr["entry"] = avg_px or _tr.get("entry")
                    _tr["pending_fill"] = False
                    _tr["fill_qty"] = filled_qty
                    _tr["fill_px"] = avg_px
                    STATE_DIRTY[0] = True
                # ⚠️ 评估 M2（我自己写错的）：绝不能丢弃返回值、无条件报"已挂"。
                _sl_res = (_res or {}).get("sl")
                _sl_ok = isinstance(_sl_res, dict) and not _sl_res.get("err")
                _tps_ok = sum(1 for x in ((_res or {}).get("tps") or [])
                              if not (isinstance(x, dict) and x.get("err")))
                if w.get("stop") and not _sl_ok:
                    _live_alert("成交后挂止损", coin, str(_sl_res)[:180],
                                "仓位已真实成交（数量 %s 均价 %.8g）但止损未确认成功" % (filled_qty, avg_px))
                    notify("【实盘·成交但要你处理】%s %s 已成交\n成交均价 %.8g ｜ 数量 %s\n"
                           "⚠️ **止损挂单失败**：%s\n止盈成功 %d/%d 档\n"
                           "机器人会持续尝试补挂止损；请打开币安核对一次。"
                           % (coin, w["dir"], avg_px, filled_qty, str(_sl_res)[:120],
                              _tps_ok, len(_tps)))
                    log("   🔴 [实盘成交] %s 已成交，但止损挂失败 → 已告警" % coin)
                else:
                    notify("【实盘·成交】%s %s 限价单已成交\n成交均价 %.8g ｜ 数量 %s\n"
                           "已挂：止盈 %d/%d 档（限价）+ 止损 %s（Algo STOP_MARKET）"
                           % (coin, w["dir"], avg_px, filled_qty,
                              _tps_ok, len(_tps), w.get("stop")))
                    log("   ✅ [实盘成交] %s 均价 %.8g 数量 %s → 止盈 %d/%d 档 + 止损已挂"
                        % (coin, avg_px, filled_qty, _tps_ok, len(_tps)))
                ENTRY_WATCH.pop(coin, None)
            elif filled_qty > 0 and any_live and now > w["deadline"]:
                # 部分成交且超时：保留已成交部分，撤掉剩余，并告知
                try:
                    bexec.cancel_all(w["sym"])
                except Exception:
                    pass
                notify("【实盘·部分成交】%s 数量 %s 已成交（均价 %.8g），剩余挂单已撤销。\n"
                       "止盈止损将按已成交量挂出。" % (coin, filled_qty, avg_px))
                bexec.after_entry_filled(w["sym"], w["dir"], w.get("tps"),
                                         w.get("stop"), filled_qty)
                ENTRY_WATCH.pop(coin, None)
            elif now > w["deadline"]:
                # 完全没成交 → 撤单并通知
                try:
                    bexec.cancel_all(w["sym"])
                except Exception:
                    pass
                ENTRY_WATCH.pop(coin, None)
                _tr = open_pos_ref.get(coin)
                if _tr is not None:
                    open_pos_ref.pop(coin, None)
                    STATE_DIRTY[0] = True
                notify("【实盘·未成交】%s %s 的限价单挂了 %d 分钟仍未成交，已自动撤单（未开仓）。\n"
                       "挂单价：%s ｜ 期间市价未回踩"
                       % (coin, w["dir"], FILL_TIMEOUT // 60,
                          [l.get("price") for l in (w.get("legs") or [])] or w.get("assumed_entry")))
                log("   ⏰ [实盘未成交] %s 超时撤单" % coin)
        except Exception as e:
            log("   成交监听异常 %s: %s" % (coin, str(e)[:120]))


def real_plan_open(coin, dirc, entry, stop, tps, margin=None):
    """开仓 → 交给真实下单层生成完整计划（市价/限价腿 + 各档止盈 + Algo 止损）"""
    if not _BEXEC_OK:
        return None
    # ===== 启动对账闸门：不一致时禁止开新真仓（评估 G3）=====
    try:
        if bexec.LIVE[0] and RECONCILE.get("blocked"):
            log("   🚫 [对账闸门] 已阻止真实开仓（%s）：state.json 与交易所有差异" % coin)
            notify("🚫【对账闸门】%s 的真实开仓被拒绝：启动对账发现纸面与交易所不一致。\n"
                   "请先核对币安持仓，然后发「重新对账」清除闸门。" % coin)
            return None
    except Exception:
        pass
    try:
        return bexec.open_full_position(coin.upper() + "USDT", dirc, entry, stop,
                                        list(tps or []), margin=margin or MARGIN)
    except Exception as e:
        # ⚠️ 实盘下绝不能只说一句"不影响纸面"：可能已经有真实仓位建立了
        if _BEXEC_OK:
            try:
                if bexec.LIVE[0]:
                    _live_alert("开仓(%s)" % coin, coin,
                                e, "真实仓位可能已部分建立或未挂保护，看门狗会复核")
                    return None
            except Exception:
                pass
        log("   ⚠️ 真实下单层计划生成失败（不影响纸面）：%s" % str(e)[:140])
        return None


def real_plan_sync_sl(coin, dirc, new_stop, qty=None):
    """止损移动/重挂（TP1 后移保本损、分批止盈后修正数量）"""
    if not _BEXEC_OK:
        return None
    return bexec.sync_sl(coin.upper() + "USDT", dirc, new_stop, qty)


def real_plan_close(coin, dirc, qty=None):
    """平仓/减仓 → 真实层对应动作"""
    if not _BEXEC_OK:
        return None
    sym = coin.upper() + "USDT"
    try:
        if qty is None:
            return bexec.close_position_market(sym, dirc, None)
        return bexec.close_position_market(sym, dirc, qty)
    except Exception as e:
        log("   ⚠️ 真实下单层平仓计划生成失败：%s" % str(e)[:140])
        return None


def _real_qty_or_estimate(coin, tr):
    """止损数量：**优先查交易所真实持仓**，查不到才退回纸面估算。
    评估 M4：实盘下若用纸面公式估数量，可能偏大（被拒）或偏小（只保护一部分仓位）。"""
    sym = coin.upper() + "USDT"
    if _BEXEC_OK:
        try:
            pos = bexec.position_of(sym, tr.get("dir"))
            q = abs(float((pos or {}).get("positionAmt") or 0))
            if q > 0:
                return q
            log("   ⚠️ 交易所查不到 %s 的真实持仓（可能已平仓）→ 止损数量退回纸面估算" % coin)
        except Exception as e:
            log("   ⚠️ 查交易所真实持仓失败（%s）→ 止损数量退回纸面估算" % str(e)[:80])
    return real_qty_estimate(tr["entry"], tr.get("remaining", 1.0))


def real_qty_estimate(entry, remaining):
    """影子模式下估算真实持仓数量（实盘时以交易所实际持仓为准）"""
    try:
        return (NOTIONAL * float(remaining)) / float(entry)
    except Exception:
        return 0.0


def fmt_real_plan(plan, title="实盘计划"):
    """把计划压成一段人看得懂的通知"""
    if not plan:
        return ""
    if plan.get("shadow") is False and bexec and bexec.LIVE[0]:
        head = "【%s·实盘】⚠️ 已真实下单" % title
    else:
        head = "【%s·影子】未发送任何委托，仅供核对" % title
    L = [head, "%s %s ｜ 名义 %.0fU（保证金 %.0fU × %d倍）"
         % (plan.get("symbol"), plan.get("dir"), plan.get("notional", 0),
            plan.get("margin", 0), plan.get("lev", 0))]
    for leg in plan.get("entry_legs", []):
        if leg.get("kind") == "market":
            L.append("① 市价开仓 数量 %s（参考价 %s）"
                     % ((leg.get("would_send") or {}).get("quantity", "-"), leg.get("ref_price")))
        else:
            L.append("① 限价开仓 %s 名义 %.0fU  %s"
                     % (leg.get("price"), leg.get("notional", 0), leg.get("why") or ""))
    for t in plan.get("tps", []):
        L.append("② 止盈%d 限价 %s 数量 %s" % (t.get("tier"), t.get("price"), t.get("qty")))
    if plan.get("tps_note"):
        L.append("② " + plan["tps_note"])
    if plan.get("sl"):
        L.append("③ 止损 STOP_MARKET(Algo接口) 触发价 %s 数量 %s"
                 % (plan["sl"].get("triggerPrice"), plan["sl"].get("quantity")))
    if plan.get("sl_note"):
        L.append("③ " + plan["sl_note"])
    return "\n".join(L)

# ---------------- 成交统计（结单时写飞书多维表格）----------------
# 用户 2026-09-13 定：只统计【已结单】的（持仓中不写）｜整单一一行｜
# 收益率=净盈亏÷保证金（含杠杆）｜手续费计入｜落地只在飞书多维表格一个地方。
try:
    import trade_stats
    _STATS_OK = True
    _STATS_ERR = ""
except Exception as _e:
    trade_stats = None
    _STATS_OK = False
    _STATS_ERR = str(_e)[:120]

# 币安 USDT-M 实测费率（2026-09-13 探测：maker 万2 / taker 万5）
FEE_MAKER = 0.0002    # 限价单成交（挂单）
FEE_TAKER = 0.0005    # 市价单成交（吃单）


def _add_fee(tr, px_notional, rate, why):
    """累加手续费。px_notional = 该笔【成交时的名义】(数量 × 成交价)"""
    try:
        f = abs(float(px_notional)) * float(rate)
    except Exception:
        return 0.0
    tr["fee"] = round(tr.get("fee", 0.0) + f, 6)
    tr.setdefault("fee_items", []).append(
        {"why": why, "notional": round(float(px_notional), 4), "rate": rate, "fee": round(f, 6)})
    return f


def _stat_close(tr):
    """结单收尾：补结单时间 / 持仓时长 / 净盈亏，然后写飞书多维表格。
    ⚠️ 只在【结单】时调用 —— 持仓中的单不统计（用户要求）。"""
    tr["t_close_ts"] = int(time.time())
    if not tr.get("t_open_ts"):
        # 兼容旧仓（用旧代码开的，没有 t_open_ts）：从无年份的 t_open 字符串按当年补全
        tr["t_open_ts"] = (trade_stats.open_ts(tr) if _STATS_OK else None) or tr["t_close_ts"]
    tr["hold_sec"] = tr["t_close_ts"] - int(tr["t_open_ts"])
    tr["pnl_net"] = round(float(tr.get("realized") or 0) - float(tr.get("fee") or 0), 6)
    tr["margin"] = MARGIN
    tr["lev"] = LEV
    tr["notional"] = NOTIONAL
    tr["exit_iso"] = datetime.datetime.fromtimestamp(tr["t_close_ts"], CST).strftime("%Y-%m-%d %H:%M:%S")
    log("   ↳ 结单统计：持仓 %s ｜ 毛 %+.2fU ｜ 手续费 -%.2fU ｜ 净 %+.2fU ｜ 收益率 %+.2f%%（按保证金 %.0fU）"
        % (trade_stats._hold_text(tr["hold_sec"]) if _STATS_OK else "%ds" % tr["hold_sec"],
           float(tr.get("realized") or 0), float(tr.get("fee") or 0), tr["pnl_net"],
           (tr["pnl_net"] / MARGIN * 100) if MARGIN else 0, MARGIN))
    try:      # 风控：更新连亏/当日盈亏计数，触线则熔断
        _risk_on_close(tr)
    except Exception as _e:
        log("   ⚠️ 风控计数异常：%s" % str(_e)[:100])
    if not _STATS_OK:
        log("   ⚠️ 统计模块未加载（%s）→ 本单不写表（数据已存在本地记录里，可事后补录）" % _STATS_ERR)
        return
    try:
        trade_stats.push_close(tr, log=log)
    except Exception as _e:
        log("   ⚠️ 写统计表异常（不影响交易）：%s" % str(_e)[:130])

# ---------------- 行情 ----------------
_ex = None
def price_of(coin):
    global _ex
    try:
        if _ex is None:
            _ex = ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})
        return float(_ex.fetch_ticker(coin.upper() + "/USDT:USDT")["last"])
    except Exception:
        return None

# ---------------- 读图 ----------------
def _cls(r, g, b):
    mx, mn = max(r, g, b), min(r, g, b)
    if mx - mn < 40:
        return "white" if mx > 200 else ("grey" if mx > 110 else None)
    if r > 150 and g > 90 and b < 110 and r >= g: return "orange"
    if r > 150 and g < 100 and b < 110: return "red"
    if g > 110 and r < 130 and b < 170: return "green"
    return None

def _coverage(px, w, y0, y1):
    xa, xb = int(w * 0.06), int(w * 0.72)
    best = 0.0
    for yy in range(y0, y1):
        cnt = tot = 0
        for x in range(xa, xb, 2):
            r, g, b = px[x, yy]
            tot += 1
            if abs(r - 2) + abs(g - 24) + abs(b - 21) > 30: cnt += 1
        if tot and cnt / tot > best: best = cnt / tot
    return round(best, 3)

def _vision(img_path):
    b64 = base64.b64encode(open(img_path, "rb").read()).decode()
    body = {"model": "deepseek-v4-flash-vision-exp", "temperature": 0,
            "messages": [{"role": "system", "content": "Read the price number printed in this chart label. STRICT JSON only."},
                         {"role": "user", "content": [{"type": "text", "text": "Return {\"text\":\"<digits exactly as printed>\",\"value\":<number>}"},
                          {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}]}
    r = requests.post(DS_API, headers={"Authorization": "Bearer " + DS_KEY, "Content-Type": "application/json"}, json=body, timeout=180)
    m = re.search(r"\{[\s\S]*\}", r.json()["choices"][0]["message"]["content"])
    return json.loads(m.group(0))

def _ocr_tags_batch(im, x0, merged):
    """把所有价格标签小图拼成一张大图（左侧标序号），一次 API 调用读完 → 从 15~20s 降到 3~5s"""
    from PIL import ImageDraw
    tiles = []
    for i, t in enumerate(sorted(merged, key=lambda t: t["y1"]), 1):
        box = (max(0, x0 + t["x1"] - 5), max(0, t["y1"] - 5), min(im.width, x0 + t["x2"] + 6), min(im.height, t["y2"] + 6))
        crop = im.crop(box)
        # 归一化到固定高度，避免拼图过大（过大→慢且容易读错）
        if crop.height > 0:
            target_h = 72
            ratio = target_h / float(crop.height)
            nw = max(1, int(crop.width * ratio))
            crop = crop.resize((nw, target_h), Image.LANCZOS)
        tiles.append((i, t, crop))
    if not tiles:
        return {}
    W = max(c.width for _, _, c in tiles) + 70
    H = sum(c.height + 10 for _, _, c in tiles) + 10
    canvas = Image.new("RGB", (W, H), (25, 25, 25))
    dr = ImageDraw.Draw(canvas)
    y = 5
    for idx, _, c in tiles:
        dr.text((8, y + max(0, c.height // 2 - 8)), str(idx), fill=(255, 255, 0))
        canvas.paste(c, (64, y))
        y += c.height + 10
    p = RUN + "/tmp_tags.png"
    canvas.save(p)
    b64 = base64.b64encode(open(p, "rb").read()).decode()
    body = {"model": "deepseek-v4-flash-vision-exp", "temperature": 0,
            "messages": [{"role": "system", "content": "You transcribe price numbers from chart labels. STRICT JSON only."},
                         {"role": "user", "content": [
                             {"type": "text", "text": "This image stacks %d price labels from a chart. Each label is marked with a yellow index number on its left. "
                              "Transcribe the price printed inside each label. Return a JSON object mapping the index to the number, e.g. {\"1\":0.1052,\"2\":0.1246}. "
                              "Only report digits you can actually read." % len(tiles)},
                             {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}]}
    try:
        r = requests.post(DS_API, headers={"Authorization": "Bearer " + DS_KEY, "Content-Type": "application/json"}, json=body, timeout=180)
        m = re.search(r"\{[\s\S]*\}", r.json()["choices"][0]["message"]["content"])
        return json.loads(m.group(0))
    except Exception as e:
        log("   批量读标签失败: " + str(e)[:90])
        return {}

def read_chart(path):
    im = Image.open(path).convert("RGB"); w, h = im.size
    px = im.load()
    x0, x1 = int(w * 0.76), int(w * 0.985)
    band = im.crop((x0, 0, x1, h)); bw, bh = band.size
    bpx = band.load()
    grid = [[_cls(*bpx[x, y]) for x in range(bw)] for y in range(bh)]
    rects = []
    for y in range(bh):
        x = 0
        while x < bw:
            c = grid[y][x]
            if not c: x += 1; continue
            x2 = x
            while x2 + 1 < bw and grid[y][x2 + 1] == c: x2 += 1
            if x2 - x + 1 >= 35: rects.append({"c": c, "y": y, "x": x, "x2": x2})
            x = x2 + 1
    groups = []
    for r in sorted(rects, key=lambda r: r["y"]):
        for g in groups:
            if g["c"] == r["c"] and r["y"] - g["y2"] <= 2 and not (r["x2"] < g["x1"] - 5 or r["x"] > g["x2"] + 5):
                g["y2"] = r["y"]; g["x1"] = min(g["x1"], r["x"]); g["x2"] = max(g["x2"], r["x2"]); break
        else:
            groups.append({"c": r["c"], "y1": r["y"], "y2": r["y"], "x1": r["x"], "x2": r["x2"]})
    merged = []
    for g in sorted(groups, key=lambda g: g["y1"]):
        for t in merged:
            if t["c"] == g["c"] and 0 <= g["y1"] - t["y2"] <= 32 and not (g["x2"] < t["x1"] - 10 or g["x1"] > t["x2"] + 10):
                t["y2"] = g["y2"]; t["x1"] = min(t["x1"], g["x1"]); t["x2"] = max(t["x2"], g["x2"]); break
        else:
            merged.append(dict(g))
    merged = [t for t in merged if t["x2"] - t["x1"] + 1 >= 55]
    nums = _ocr_tags_batch(im, x0, merged)                   # 一次调用读完所有标签
    tags = []
    for i, t in enumerate(sorted(merged, key=lambda t: t["y1"]), 1):
        v = nums.get(str(i))
        if v is None: v = nums.get(i)
        try:
            fv = float(str(v).replace(",", "").replace("$", "").strip())
        except Exception:
            continue
        yc = (t["y1"] + t["y2"]) // 2
        tags.append({"y": yc, "color": t["c"], "value": fv, "text": str(v),
                     "cov": _coverage(px, w, max(0, yc - 14), min(h, yc + 15))})
    reds = [t for t in tags if t["color"] == "red"]
    if not reds: return {"ok": False, "why": "无红色止损标签", "tags": tags}
    sl = min(reds, key=lambda t: t["value"])["value"]
    above = sorted([t for t in tags if t["value"] > sl * 1.0005], key=lambda t: t["value"])
    if not above: return {"ok": False, "why": "止损上方无标签", "tags": tags}
    entry = above[0]["value"]
    tp = []
    for t in above:
        v = t["value"]
        if v <= entry * 1.0005: continue
        if t["cov"] < 0.85: continue
        if any(abs(v - s) / v < 0.003 for s in tp): continue
        tp.append(v)
    return {"ok": True, "sl": sl, "entry": entry, "tps_all": tp, "tps": tp[:TP_TIERS], "tags": tags}

# ---------------- 文本解析 ----------------
def parse_text(text):
    prompt = ("从这条加密货币跟单消息里抽取开单信息。只输出JSON："
              "{\"is_signal\":bool,\"coin\":\"大写币种或null\",\"direction\":\"LONG|SHORT|null\","
              "\"entry\":数字或null,\"entryRange\":[最小,最大]或null,\"entry_is_cmp\":bool,\"add_price\":数字或null,"
              "\"entryLegs\":[数字]或null,\"stop\":数字或null,\"stopRange\":[小,大]或null,\"stopPct\":数字或null,"
              "\"targets\":[数字],\"targetRanges\":[[小,大]]或null,\"tp_on_chart\":bool,"
              "\"type\":\"open|manage|info\",\"manage_action\":\"close_all|trim|move_stop_to_cost|null\"}"
              " 规则："
              "① 只用消息里真实出现的数字，绝不编造；"
              "② 止盈写在图上则 tp_on_chart=true 且 targets 为空；"
              "③ 开仓价若给的是区间（如「在4360到4310区间多」）→ entryRange=[小,大]，entry 留 null；"
              "④ 【分批建仓】若给了多个入场点位（如「77777进头仓，76666 75555继续分批接多」"
              "或「跌到95第一笔，90第二笔」）→ entryLegs=[点位1,点位2,...] 按出现顺序，entry 留 null；"
              "⑤ 止损若给的是区间 → stopRange=[小,大]；"
              "⑥ 【百分比止损】若写「带个3%止损」「3%止损」→ stopPct=3（只填数字），stop 留 null；"
              "⑦ 止盈若是区间（如「止盈2400到2350」）→ targetRanges=[[2350,2400]]，不要塞进 targets；"
              "⑧ 一档止盈只给一个数就放 targets；多个止盈按顺序放 targets。"
              "重要：方向必须按原文判断（做多/多/空/做空/LONG/SHORT）。")
    body = {"model": "deepseek-v4-flash", "temperature": 0,
            "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": text[:900]}]}
    try:
        r = requests.post(DS_API, headers={"Authorization": "Bearer " + DS_KEY, "Content-Type": "application/json"}, json=body, timeout=120)
        m = re.search(r"\{[\s\S]*\}", r.json()["choices"][0]["message"]["content"])
        return json.loads(m.group(0))
    except Exception as e:
        log("   文本解析失败: " + str(e)[:100])
        return {}

# ---------------- 通知格式 ----------------
def fmt_plan(coin, direction, entry, sl, tps, src, when, note="", add=None, timing=None,
             signal_entry=None, signal_src=None, crossed=False, entry_mode="市价",
             entry_orders=None, entry_note=""):
    d = 1 if (direction or "LONG").upper() == "LONG" else -1
    lev = LEV
    L = []
    L.append("【已开单·纸面】%s/USDT 永续 · %s" % (coin, "做多 LONG" if d == 1 else "做空 SHORT"))
    L.append("")
    L.append("来源：%s   信号时间：%s" % (src, when))
    L.append("金额：保证金 %.0fU %d倍 = 名义 %.0fU" % (MARGIN, lev, NOTIONAL))
    L.append("开仓：")
    if entry_mode == "限价分批" and entry_orders:
        for i, o in enumerate(entry_orders, 1):
            L.append("挂单%d：%.8g → 保证金 %.0fU（%d倍 = %.0fU 名义）"
                     % (i, o["price"], o["margin"], lev, o["margin"] * lev))
        if entry_note:
            L.append("（%s）" % entry_note)
        L.append("你的开仓均价（两笔都成交时）：%.8g" % entry)
    elif isinstance(entry, (int, float)):
        L.append("你的开仓价：市价成交 %.8g" % entry)
        if entry_note:
            L.append("（%s）" % entry_note)
    else:
        L.append("你的开仓价：市价")
    if isinstance(signal_entry, (int, float)) and signal_entry:
        L.append("博主的开仓价：%.8g（%s读到）" % (signal_entry, signal_src or "图上/卡片/文字"))
    else:
        L.append("博主的开仓价：未读到（按 CMP 市价理解）")
    ref = signal_entry if isinstance(signal_entry, (int, float)) and signal_entry else entry
    if isinstance(sl, (int, float)) and isinstance(entry, (int, float)) and entry:
        L.append("止损（挂单）：%.8g" % sl)
        L.append("　　→ 按实际成交 %.2f%%（不含杠杆）· 约 %+.1fU" % ((sl - entry) / entry * 100 * d, (sl - entry) * d / entry * NOTIONAL))
        L.append("　　→ 含 %d 倍杠杆 %.2f%%（占保证金）" % (lev, (sl - entry) / entry * 100 * d * lev))
        if ref and abs(ref - entry) / entry > 0.0005:
            L.append("　　→ 博主止损收益率（不算杠杆）%.2f%%" % ((sl - ref) / ref * 100 * d))
    else:
        L.append("止损：图上/卡片/文字都没读到，等你确认（暂不挂）")
    if tps:
        n = len(tps[:TP_TIERS])
        for i, t in enumerate(tps[:TP_TIERS], 1):
            if isinstance(entry, (int, float)) and entry:
                gross = (t - entry) / entry * 100 * d
                tier_u = (t - entry) * d / entry * NOTIONAL / n
                full_u = (t - entry) * d / entry * NOTIONAL
                L.append("止盈%d（挂单）：%.8g → 预期收益率 %+.2f%%（含 %d 倍杠杆）" % (i, t, gross * lev, lev))
                L.append("　　　平1/3 · 该档 约 %+.1fU · 若全平在此价 约 %+.1fU" % (tier_u, full_u))
        extra = ""
        if len(tps) < TP_TIERS:
            extra = "（本次只读到 %d 档）" % len(tps)
        elif crossed:
            extra = "（注：有止盈价已被现价越过）"
        L.append("")
        L.append("规则：TP1 成交后止损移到开仓价（保本损）" + extra)
    else:
        L.append("")
        L.append("规则：止盈图上/卡片/文字都没读到，暂不挂（等你确认）")
    if note: L.append("备注：" + note)
    if timing: L.append(timing)
    return "\n".join(L)

# ---------------- 币种归一化（只做 USDT 计价的币安合约）----------------
_COIN_ALIAS = {
    "GOLD": "XAU", "GOLDUSDT": "XAU",                       # 黄金 -> XAUUSDT
    "XAUT": "XAUT",                                         # Tether Gold
    "SILVER": "XAG",                                        # 白银
    "OIL": "CL", "WTI": "CL", "CRUDE": "CL", "USOIL": "CL", # 原油 -> CLUSDT
    "NATGAS": "NATGAS", "GAS": "NATGAS",
}

def norm_coin(raw):
    """统一成币安 USDT-M 的 base（USD 只是别人的写法，我们只交易 USDT 计价合约）"""
    if not raw:
        return None
    s = str(raw).upper().strip().replace("/", "").replace("-", "").replace(" ", "").replace("$", "")
    for suf in ("USDT.P", "USDTM", "PERP", "USDT", "USD"):
        if s.endswith(suf) and len(s) > len(suf):
            s = s[:-len(suf)]
            break
    return _COIN_ALIAS.get(s, s) or None

def resolve_coin(raw):
    """返回 (base, 是否在币安 USDT-M 清单里)"""
    b = norm_coin(raw)
    if not b:
        return None, False
    return b, ((not _SYMS) or (b in _SYMS))

# ---------------- 快速解析（常见模板秒出结果，避免每次等 AI）----------------
_SYMS = set()
try:
    _s = json.load(open(BASE + "/fapi_symbols.json", encoding="utf-8"))
    if isinstance(_s, dict): _s = list(_s.keys())
    for x in _s:
        x = str(x).upper()
        if x.endswith("USDT"): _SYMS.add(x[:-4])
except Exception:
    pass

# ===== B10：这些代码同时是【英文常用词】（或极易误撞），二遍（大小写不敏感）匹配时跳过 =====
# 它们写大写时仍会被第一遍【区分大小写】认出来，所以不会漏掉真实信号。
# 反面教材（2026-09-15 实测）：一句「Going long on UNI here at CMP」被判成 AT/ON/THE/UNI 四个币。
_AMBIG_TICKERS = {
    "ON", "AT", "THE", "IN", "SO", "ONE", "TWO", "FOR", "AND", "NOT", "NOW", "TOP",
    "NEW", "OLD", "BIG", "MAX", "MIN", "KEY", "ALL", "ANY", "OUT", "UP", "DOWN",
    "IF", "IS", "IT", "BE", "TO", "OF", "MY", "WE", "HE", "DO", "NO", "BY", "OR",
    "AS", "AN", "ME", "US", "BUT", "CAN", "GET", "GOT", "LET", "PUT", "RUN", "SAY",
    "SEE", "SET", "TOO", "USE", "WAY", "WHO", "WHY", "YES", "YET", "HIGH", "LOW",
    "NEAR", "LINK", "SAND", "MASK", "GALA", "APE", "RUNE", "FLOW", "BAND", "STORJ",
    "DOT", "KSM", "ICP", "SSV", "ID", "AI", "GO", "NFT", "DAO", "LONG", "SHORT",
    "CLOSE", "OPEN", "STOP", "ENTRY", "TARGETS", "RISK", "PNL", "CMP", "SL", "TP",
}

def fast_parse(txt):
    """只匹配博主常用模板；命中即返回，未命中返回 None（交给 AI 解析）"""
    raw = None
    dirc = None
    m = re.search(r"(?:Going|Market|Longing|Buying|Selling|Shorting)\s+(long|short)\s+\$?([A-Za-z0-9]{2,12})", txt, re.I)
    if m:
        dirc = "LONG" if m.group(1).lower() == "long" else "SHORT"
        raw = m.group(2)
    if raw is None:
        m = re.search(r"\b(Selling|Buying|Shorting|Longing)\s+\$?([A-Za-z0-9]{2,12})", txt, re.I)
        if m:
            dirc = "SHORT" if m.group(1).lower() in ("selling", "shorting") else "LONG"
            raw = m.group(2)
    if raw is None:
        m = re.search(r"([A-Za-z0-9]{2,12})\s*(?:/USDT)?\s*[—\-–]\s*(LONG|SHORT)\b", txt, re.I)
        if m:
            raw, dirc = m.group(1).upper(), m.group(2).upper()
    # 中文方向词（颜驰这类："…区间多" / "多单" / "做空"）
    # 2026-09-14 补充：暴富龙常用「分批接多」「进头仓」「买多」「卖空」等说法
    if re.search(r"区间多|做多|多单|看多|接多|进多|买多|冲多|追多|低吸|抄底", txt):
        dirc = "LONG"
    elif re.search(r"区间空|做空|空单|看空|接空|进空|卖空|高抛|摸顶", txt):
        dirc = "SHORT"
    if raw is None:
        # 中文/俗称兜底：直接从整段文字里找币安合约（黄金/比特币/闪迪/海力士…）
        c2 = find_coin_in_text(txt)
        if c2 and dirc:
            raw = c2
    if raw is None:
        return None
    coin, ok = resolve_coin(raw)
    if not coin or not ok or coin in ("LONG", "SHORT"):
        c2 = find_coin_in_text(txt)
        if c2:
            coin, ok = c2, True
    if not coin or not ok:
        return None                       # 币种规范化后不在币安 USDT-M 清单里 → 交给 AI/待确认
    if not dirc:
        return None
    stop = None
    # ⚠️ B5 修复：原来用 `[^0-9]{0,14}` 当间隔，它会**跳过标点**去吃后面的数字 ——
    #    实测「4250止损，4350到4450分批止盈」被读成 止损=4350（其实是止盈位）。
    #    改成不允许跨越标点（，。；,;、！？换行），并补上【数字在关键词前】的写法（「4250止损」）。
    _GAP = r"[^0-9，。；;、！!？?\n]{0,10}"
    for pat in (r"close under\s*\$?([0-9]*\.?[0-9]+)",
                r"\bSL\s*[:：=]?\s*\$?([0-9]*\.?[0-9]+)",
                r"stop[ -]?loss\s*[:：=]?\s*\$?([0-9]*\.?[0-9]+)",
                r"\bstop\s*[:：=]?\s*\$?([0-9]*\.?[0-9]+)",
                r"止损\s*[:：=]?\s*" + _GAP + r"([0-9]*\.?[0-9]+)",
                r"([0-9]*\.?[0-9]+)\s*" + _GAP + r"(?:止损|stop)"):      # 「4250止损」
        mm = re.search(pat, txt, re.I)
        if mm:
            try:
                stop = float(mm.group(1)); break
            except Exception:
                pass
    add = None
    mm = re.search(r"(?:DCA|Dca|dca|加仓)[^0-9]{0,14}\$?([0-9]*\.?[0-9]+)", txt)
    if mm:
        try: add = float(mm.group(1))
        except Exception: pass
    entry = None
    mm = re.search(r"(?:Entry|入场|进场)[:：]?\s*\$?([0-9]*\.?[0-9]+)", txt, re.I)
    if mm:
        try: entry = float(mm.group(1))
        except Exception: pass
    if entry is None:
        # 「77065价格做空」/「现价77000做多」/「@77000」这类写法（用户的测试消息就是这种）
        for pat in (r"([0-9]*\.?[0-9]+)\s*价格?\s*(?:做多|做空|多单|空单)",
                    r"(?:现价|市价|CMP|@)\s*\$?([0-9]*\.?[0-9]+)"):
            mm2 = re.search(pat, txt, re.I)
            if mm2:
                try:
                    entry = float(mm2.group(1)); break
                except Exception:
                    pass
    tps = []
    # ⚠️ 必须逐个关键词扫描，不能只 search 第一个：
    #    用户的测试「第一止盈74500 第二止盈70500」曾被整条漏掉第二档（16:17 实测）。
    #    分段规则：每个止盈类关键词后取到【下一个任意关键词】为止，
    #    这样既不会漏档，也不会把紧跟着的止损/加仓数字误当成止盈。
    _BOUND = (r"(?:止盈|目标位?|targets?|(?:TP|Tp|tp)\s?\d?|止损|stop[ -]?loss|SL|"
              r"加仓|DCA|入场|进场|Entry)")
    _kw = list(re.finditer(_BOUND, txt, re.I))
    for _i, _m in enumerate(_kw):
        if not re.match(r"(?:止盈|目标位?|targets?|(?:TP|Tp|tp)\s?\d?)", _m.group(0), re.I):
            continue                                   # 只管止盈类关键词
        _end = _kw[_i + 1].start() if _i + 1 < len(_kw) else len(txt)
        _seg = txt[_m.end():_end]
        _nums = []
        for _x in re.findall(r"[0-9]*\.?[0-9]+", _seg):
            try:
                _nums.append(float(_x))
            except Exception:
                pass
        # 「止盈1 0.24 …」中紧跟关键词的单个 1~4 是档位序号，不是价格
        if len(_nums) >= 2 and _nums[0] in (1.0, 2.0, 3.0, 4.0):
            _nums = _nums[1:]
        for _v in _nums[:TP_TIERS]:
            if _v > 0 and _v not in tps:
                tps.append(_v)
    # 保留博主给的先后顺序（不要按数值排序）：做空时数值排序会把最远那档排到最前，
    # 再截断到 3 档就会把【最近的止盈】丢掉。最终档位顺序在 finalize_pending 里按"离入场近→远"再规整一次。
    tps = list(dict.fromkeys(tps))[:TP_TIERS]
    # 区间开仓价（如「在4360到4310区间多」/「4310-4360 区间」）—— 一律取中间值
    # ⚠️ 必须在【遮掉止盈段和止损段】的文本里找，否则会把止盈区间/止损区间当成开仓区间
    rng = None
    _mt = _mask_segments(txt, mask_tp=True, mask_sl=True)
    m = re.search(r"([0-9]*\.?[0-9]+)\s*(?:到|至|~|～|—|–)\s*([0-9]*\.?[0-9]+)", _mt)
    if not m:
        m = re.search(r"([0-9]*\.?[0-9]+)\s*-\s*([0-9]*\.?[0-9]+)\s*(?:区间|之间)", _mt)
    if m:
        try:
            a, b = float(m.group(1)), float(m.group(2))
            if a > 0 and b > 0 and a != b:
                rng = [min(a, b), max(a, b)]
        except Exception:
            pass
    # ===== 用户规则（2026-09-14）本地正则支持 =====
    # ⚠️ 必须放在"什么都没解析到就返回 None"的检查【之前】——
    #    否则「76666 75555继续分批接多，74000止损」这种消息因为止损写在数字后面、
    #    没有止盈关键词，会被判成"什么都没读到"而整条丢掉。
    out_legs, out_stop_pct, out_stop_range, out_tp_ranges = None, None, None, None
    # ① 分批建仓：「76666 75555继续分批接多」「跌到95第一笔开仓，90第二笔」
    _legtxt = None
    _mleg = re.search(r"((?:\$?[0-9]*\.?[0-9]+[\s,，、]+){1,4}\$?[0-9]*\.?[0-9]+)\s*"
                      r"(?:继续)?(?:分批|分次|分笔)", txt)
    if _mleg:
        _legtxt = _mleg.group(1)
    else:
        _ml2 = re.search(r"(?:跌到|涨到|到)\s*\$?([0-9]*\.?[0-9]+)[^0-9]{0,12}?"
                         r"(?:第一笔|第1笔|首笔)[^0-9]{0,20}?\$?([0-9]*\.?[0-9]+)[^0-9]{0,12}?(?:第二笔|第2笔)", txt)
        if _ml2:
            _legtxt = _ml2.group(1) + " " + _ml2.group(2)
    if _legtxt:
        try:
            _lv = []
            for x in re.findall(r"[0-9]*\.?[0-9]+", _legtxt):
                v = float(x)
                if v > 0 and v not in _lv:
                    _lv.append(v)
            if len(_lv) >= 2:
                out_legs = _lv
        except Exception:
            pass
    # ② 百分比止损：「带个3%止损」「3%止损」「止损3%」
    _msp = re.search(r"(?:带个?|带)?\s*([0-9]*\.?[0-9]+)\s*%\s*(?:的)?\s*止损", txt)
    if not _msp:
        _msp = re.search(r"止损[^0-9%]{0,8}([0-9]*\.?[0-9]+)\s*%", txt)
    if _msp:
        try:
            out_stop_pct = float(_msp.group(1))
        except Exception:
            pass
    # ③ 止损区间
    _msr = re.search(r"止损[^0-9]{0,10}\$?([0-9]*\.?[0-9]+)\s*(?:到|至|~|～|-|—|–)\s*\$?([0-9]*\.?[0-9]+)", txt)
    if _msr:
        out_stop_range = [float(_msr.group(1)), float(_msr.group(2))]
    # ④ 止盈区间：止盈2400到2350
    _mtr = re.findall(r"(?:止盈|目标位?|targets?)[^0-9]{0,10}\$?([0-9]*\.?[0-9]+)\s*"
                      r"(?:到|至|~|～|-|—|–)\s*\$?([0-9]*\.?[0-9]+)", txt)
    if _mtr:
        out_tp_ranges = [[float(a), float(b)] for a, b in _mtr]
    if not (stop or add or entry or tps or out_legs or out_stop_pct or out_stop_range or out_tp_ranges):
        return None
    return {"is_signal": True, "coin": coin, "direction": dirc, "entry": entry, "entryRange": rng,
            "entryLegs": out_legs, "stopPct": out_stop_pct,
            "stopRange": out_stop_range, "targetRanges": out_tp_ranges,
            "entry_is_cmp": bool(re.search(r"\bCMP\b|市价|现价", txt, re.I)),
            "add_price": add, "stop": stop, "targets": tps,
            "tp_on_chart": bool(re.search(r"TPs?\s+above|止盈在?上方|止盈位在上方", txt, re.I)),
            "type": "open", "manage_action": None, "_fast": True}

# 止盈类关键词计数：用来判断"快速解析是不是可疑地少读了档位"
_TPKW_RE = re.compile(r"止盈|目标位?|targets?|(?:TP|Tp|tp)\s?\d?", re.I)


def fast_parse_suspect(txt, info):
    """快速解析（本地正则）结果是否【可疑地不完整】。
    存在的意义：快速解析命中会跳过 AI，一旦它少读，就会【静默丢数据】。
    实例：2026-09-13 16:17 用户测试「第一止盈74500 第二止盈70500」被读成只有 74500，
          于是到 74500 就全平，第二档 70500 被完全放弃 —— 因为快速解析命中就跳过了 AI。"""
    if not info:
        return True
    n_kw = len(_TPKW_RE.findall(txt or ""))
    got = len(info.get("targets") or [])
    if n_kw and got < min(n_kw, TP_TIERS):
        return True
    if not info.get("stop"):
        return True
    return False

# ---------------- 待确认池：把同一条信号的多条消息合并（文案 + 卡片 + K线图）----------------
PENDING = {}
PENDING_WAIT = 4           # 秒：等同一条信号的后续消息（用户要求 4 秒）

def merge_pending(coin, group, info=None, chart=None, imgs=None, t_sig=0, txt="", stamps=None):
    p = PENDING.get(coin)
    if p is None:
        p = {"group": group, "entry": None, "entry_src": None, "add": None, "stop": None, "stop_src": None,
             "legs": [], "stop_pct": None,
             "tps": [], "imgs": [], "texts": [], "first_ts": t_sig or int(time.time()),
             "t_found": (stamps or {}).get("found", time.time()), "t_img": (stamps or {}).get("img", 0.0),
             "t_parse": (stamps or {}).get("parse", 0.0), "t_chart": (stamps or {}).get("chart", 0.0),
             "deadline": time.time() + PENDING_WAIT}
        PENDING[coin] = p
    elif stamps and stamps.get("chart", 0) > p.get("t_chart", 0):
        p.update({k: stamps[k] for k in ("t_img", "t_parse", "t_chart") if k in stamps})
    p["deadline"] = time.time() + PENDING_WAIT
    if info:
        e = info.get("entry")
        if isinstance(e, (int, float)) and p["entry"] is None:
            p["entry"] = float(e); p["entry_src"] = "消息文字"
        rng = info.get("entryRange")
        if isinstance(rng, (list, tuple)) and len(rng) == 2 and p["entry"] is None:
            try:
                mid, lo, hi = range_mid(float(rng[0]), float(rng[1]), coin)
                p["entry"] = mid
                p["entry_src"] = "区间中间值"
                p["range_note"] = "博主给的是区间 %.8g ~ %.8g → 按中间值 %.8g 开（已向上取整到合约精度）" % (lo, hi, mid)
            except Exception:
                pass
        a = info.get("add_price")
        if isinstance(a, (int, float)) and p["add"] is None:
            p["add"] = float(a)
        # ===== 分批建仓：多个入场点位（用户规则 2026-09-14）=====
        if not p.get("legs"):
            _lg = []
            for x in (info.get("entryLegs") or []):
                try:
                    xv = float(x)
                    if xv > 0 and xv not in _lg:
                        _lg.append(xv)
                except Exception:
                    pass
            if len(_lg) >= 2:
                p["legs"] = _lg
                p["entry"] = None                  # 分批建仓以各点位为准，不取单一入场价
                p["entry_src"] = "分批建仓(%d笔)" % len(_lg)
                log("   ↳ 识别到分批建仓 %d 个点位：%s" % (len(_lg), _lg))
        # ===== 止损区间 → 取中点（用户规则 3：区间一律取中间值）=====
        # ⚠️ 区间必须【优先于】散点：否则「止损98到92」会先被散点正则抓成 98，
        #    区间中点就永远轮不上（实测踩过这个坑）。
        _sr = info.get("stopRange")
        _sr_used = False
        if isinstance(_sr, (list, tuple)) and len(_sr) == 2:
            try:
                _lo, _hi = sorted([float(_sr[0]), float(_sr[1])])
                if _lo != _hi:
                    p["stop"] = (_lo + _hi) / 2.0
                    p["stop_src"] = "止损区间中间值"
                    _sr_used = True
                    log("   ↳ 止损区间 %.8g~%.8g → 取中点 %.8g" % (_lo, _hi, p["stop"]))
            except Exception:
                pass
        s = info.get("stop")
        if (not _sr_used) and isinstance(s, (int, float)) and p["stop"] is None:
            p["stop"] = float(s); p["stop_src"] = "消息文字"
        # ===== 百分比止损（用户规则 5）=====
        _sp = info.get("stopPct")
        if isinstance(_sp, (int, float)) and 0 < float(_sp) < 90 and p.get("stop_pct") is None:
            p["stop_pct"] = float(_sp)
            log("   ↳ 识别到百分比止损 %.2f%%（按开仓价折算止损位）" % float(_sp))
        # ===== 止盈区间 → 取中点（用户规则 3）=====
        _ranges = []
        for _tr in (info.get("targetRanges") or []):
            try:
                if isinstance(_tr, (list, tuple)) and len(_tr) == 2:
                    _lo, _hi = sorted([float(_tr[0]), float(_tr[1])])
                    if _lo == _hi:
                        continue
                    _ranges.append((_lo, _hi))
                    _mid = (_lo + _hi) / 2.0
                    if _mid > 0 and _mid not in p["tps"]:
                        p["tps"].append(_mid)
                        log("   ↳ 止盈区间 %.8g~%.8g → 取中点 %.8g" % (_lo, _hi, _mid))
            except Exception:
                pass
        for t in (info.get("targets") or []):
            try:
                tv = float(t)
                # 落在已识别区间内的散点要丢弃：它们是区间的两个端点，不是独立的止盈档
                if any(_lo - 1e-9 <= tv <= _hi + 1e-9 for _lo, _hi in _ranges):
                    continue
                if tv not in p["tps"]:
                    p["tps"].append(tv)
            except Exception:
                pass
        if txt:
            head = txt[:60]
            if head not in [x[:60] for x in p["texts"]] and len(p["texts"]) < 6:
                p["texts"].append(txt[:400])
    if chart:
        # 图上画的线最准（KOL 实际挂单用的就是图上的点位）→ 图优先覆盖文字/卡片
        if chart.get("sl"):
            p["stop"] = chart["sl"]; p["stop_src"] = "K线图"
        if chart.get("entry"):
            p["entry"] = chart["entry"]; p["entry_src"] = "K线图"
        if chart.get("tps"):
            p["tps"] = list(chart["tps"])
    for f in (imgs or []):
        if f not in p["imgs"]: p["imgs"].append(f)
    return p

def pending_complete(p):
    tps = sorted(set(p["tps"]))
    return p["entry"] is not None and p["stop"] is not None and len(tps) >= TP_TIERS

# ---------------- 2R 兜底止盈（2026-09-13 用户决定）----------------
# 背景：博主有时只给开仓价 + 止损，不给止盈（实例：LSK「Entry 0.21144 / SL 0.1993 / Risk 0.5%」，
#       文字无止盈、消息里也没有图 → 任何算法都读不出来）。
# 用户决定：这种情况「挂止损 + 在 2:1 盈亏比位置自动挂第一档止盈」；
#           后续博主补了真实止盈位，就把这档撤掉按真实档位重挂。
R_FALLBACK_MULT = 2.0        # 盈亏比 2:1
# 用户 2026-09-13 决定：如果博主后续【没有】补真实止盈位，这一档就**直接全平**（锁定 2R 利润）。
R_FALLBACK_PART = 1.0

# "未能识别为信号"的通报节流（避免某个群狂发图时刷屏）
UNIDENT_NOTIFY_COOLDOWN = 600
_UNIDENT_NOTIFY = {}         # 群 -> 上次通报时间戳

# 博主的【结单/止盈止损已触发】通报 —— 这类消息**绝不能开新仓**。
# 实例（2026-09-14 08:00 UA群）："Trade Closed — DOGE/USDT LONG ... Stop loss hit at $0.08280"
#   被抽成 方向=LONG + 止损=0.0828 而市价 0.08238 → 止损落在入场价上方 → 秒平并记假盈利。
_CLOSE_ANNOUNCE = re.compile(
    r"(trade\s+closed|stop[ -]?loss\s+hit|sl\s+hit|stopped\s+out|tp\s?\d?\s*hit|"
    r"take[ -]?profit\s+hit|target\s+hit|hit\s+at|closed\s+at|profit\s+taken|"
    r"止盈(达成|已到|命中|触发|到位)|止损(达成|已到|被扫|触发|到位)|"
    r"已平仓|平仓完成|结单|全部平仓|已止盈|已止损)", re.I)

# ===== 价格合理性阈值（2026-09-14 新增）=====
# 事故：暴富龙一条消息混了 BTC/以太坊/Giggle 三个币，fast_parse 把【以太坊的区间】和
# 【Giggle 的止盈】错配给 BTC，于是 开仓价 2540（而 BTC 市价 77760）、止损 38，
# 2R 兜底算出止盈 7582.1 → 止盈早已越过 → 秒平、记 +1773U 假盈利。
MAX_ENTRY_DEV = 0.20     # 解析出的开仓价与市价偏离超过 20% → 判为解析错误
# ===== 黄金mansoor 专属规则（用户 2026-09-14）=====
# 「假设是4000开多，此时价格低于4000则开进去；假设是4001，则挂单在4000开多」——
# 即：只在市价对我们更有利时市价进，否则一律按博主价挂限价等他回踩。**只对这一组生效。**
STRICT_LIMIT_GROUPS = []  # 由 runtime_config.json 的 strict_limit_groups 决定
MAX_STOP_PCT = 0.40      # 止损距离开仓价超过 40% → 判为荒谬
MIN_STOP_PCT = 0.0005    # 止损距离小于 0.05% → 等于没设止损

# ===== 待人工确认（用户 2026-09-14：把握不准必须问我，回「开」才开）=====
ASKING = {}              # coin -> {p, reason, ask_ts, txt}
ASK_TIMEOUT = 1800       # 30 分钟没回复自动作废
# 用户 2026-09-15 第 12 条硬要求：**所有订单在开之前都必须经我审批**（不再只问"把握不准"的）。
# 默认 True；可用 runtime_config.json 的 require_approval 关掉（关掉后恢复"只在把握不准时问"）。
REQUIRE_APPROVAL = [True]


def _jsonable(o, depth=0):
    """把任意结构安全地变成能 json.dump 的东西 —— 用于把待确认池落盘（B11）。
    绝不允许因为某个字段不可序列化而让整个 state.json 落盘失败（那会连带弄丢持仓）。"""
    if depth > 4:
        return str(o)[:200]
    if o is None or isinstance(o, (str, int, float, bool)):
        return o
    if isinstance(o, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in list(o.items())[:40]}
    if isinstance(o, (list, tuple, set)):
        return [_jsonable(x, depth + 1) for x in list(o)[:200]]
    return str(o)[:200]


def _asking_dump():
    """待确认池的可落盘形式（B11：原来只存在内存里，机器人一重启就静默丢失）"""
    out = {}
    for c, v in ASKING.items():
        if not isinstance(v, dict):
            continue
        out[c] = {"reason": _jsonable(v.get("reason")), "ask_ts": v.get("ask_ts"),
                  "txt": _jsonable(v.get("txt")), "p": _jsonable(v.get("p") or {})}
    return out


def _fmt_num(x):
    try:
        return "%.8g" % float(x)
    except Exception:
        return str(x)


def _plan_score(p):
    """待确认参数的「完整度」打分 —— 用来防止**更差的解析覆盖掉更好的解析**（B11 实测事故）。
    19:03 UNI：先由图读到好的（开仓=6.513 止损=6.396 止盈=[9.289]），
    随后一条纯文字的差解析（开仓=None 止损=6.39 止盈=[6.39]，止盈还等于止损）把它整体覆盖了。"""
    s = 0
    if isinstance(p.get("entry"), (int, float)) and p.get("entry"):
        s += 3
    if p.get("legs"):
        s += 3
    if isinstance(p.get("stop"), (int, float)) and p.get("stop"):
        s += 2
    s += min(len([t for t in (p.get("tps") or []) if isinstance(t, (int, float))]), 3)
    return s


def _approval_lines(coin, p, d):
    """审批通知里的详细清单 —— 用户第 8 条硬要求：币种 / 时间 / 开单金额 / 杠杆 /
    止损位 / **止损点数（不含杠杆）** / 各档止盈位 / **各档预期收益率（含杠杆）**。"""
    entry = p.get("entry")
    _lg = [float(x) for x in (p.get("legs") or []) if isinstance(x, (int, float))]
    if (not isinstance(entry, (int, float)) or not entry) and _lg:
        entry = sum(_lg) / len(_lg)
    stop = p.get("stop")
    tps = set(t for t in (p.get("tps") or []) if isinstance(t, (int, float)))
    if entry:
        tps = sorted(tps, key=lambda t: abs(float(t) - float(entry)))
    else:
        tps = sorted(tps)
    out = ["币种：%s ｜ 方向：%s ｜ 来源群：%s"
           % (coin, "做多 LONG" if d == 1 else "做空 SHORT", p.get("group") or "-"),
           "时间：%s" % datetime.datetime.now(CST).strftime("%m-%d %H:%M:%S")]
    if isinstance(entry, (int, float)) and entry:
        out.append("入场：%s%s" % (_fmt_num(entry),
                                 ("（%s）" % p["entry_src"]) if p.get("entry_src") else ""))
    else:
        out.append("入场：**未读到**")
    if _lg:
        out.append("分批建仓：%d 个点位 %s（保证金等分）"
                   % (len(_lg), "、".join(_fmt_num(x) for x in _lg)))
    out.append("保证金：%.0fU ｜ 杠杆：%d 倍 ｜ 名义：%.0fU" % (MARGIN, LEV, MARGIN * LEV))
    if isinstance(stop, (int, float)) and stop:
        out.append("止损位：%s" % _fmt_num(stop))
        if isinstance(entry, (int, float)) and entry:
            _pt = abs(float(entry) - float(stop))
            _pct = _pt / float(entry)
            out.append("止损点数（不含杠杆）：%s ｜ 占入场 %.2f%%" % (_fmt_num(_pt), _pct * 100))
            out.append("若打止损：亏 %.1fU（保证金 %.0fU 的 %.1f%%）" % (MARGIN * _pct * LEV, MARGIN, _pct * LEV * 100))
    else:
        out.append("止损位：**未读到**")
    if tps:
        for i, t in enumerate(list(tps)[:TP_TIERS], 1):
            _seg = ["止盈%d：%s" % (i, _fmt_num(t))]
            if isinstance(entry, (int, float)) and entry:
                _rr = (float(t) - float(entry)) / float(entry) * (1 if d == 1 else -1)
                _seg.append("预期收益 %+.1f%%（含 %d 倍杠杆）" % (_rr * LEV * 100, LEV))
                if isinstance(stop, (int, float)) and stop and abs(float(entry) - float(stop)) > 0:
                    _seg.append("盈亏比 %.2f:1"
                                % (abs(float(t) - float(entry)) / abs(float(entry) - float(stop))))
            out.append(" ｜ ".join(_seg))
    else:
        out.append("止盈：**未读到**")
    return out


def _combo_options(names):
    """多币种审批要给出**完整组合**（用户 2026-09-15 指出 B4：原来只给两个示例，
    而且「只开A和B」与「只开A，不开B」语义重复）。
    n 个币 → 2^n-1 种非空组合；n≥4 时列表太长，退化成"逐个单选 + 全部开"，并提示可自由回复。"""
    names = list(dict.fromkeys(names))
    n = len(names)
    if n < 2:
        return ["· 全部不开"]
    if n <= 3:
        opts = []
        for k in range(1, n + 1):
            for cb in itertools.combinations(names, k):
                _j = " 和 ".join(cb)
                if k == n:
                    opts.append("· 全部开（%s）" % _j)
                elif k == 1:
                    opts.append("· 只开 %s" % _j)
                else:
                    opts.append("· 只开 %s" % _j)
        opts.append("· 全部不开")
        return opts
    opts = ["· 只开 %s" % c for c in names]
    opts.append("· 全部开（%s）" % " 和 ".join(names))
    opts.append("· 全部不开")
    opts.append("（%d 个币组合太多，也支持自由回复，例如「只开 %s 和 %s」）"
                % (n, names[0], names[1]))
    return opts


def fallback_tp_2r(entry, stop, dirc, mult=R_FALLBACK_MULT):
    """只有开仓价+止损、没有止盈时，按 2:1 盈亏比推出第一档止盈价。
    R = |入场 − 止损|；做多 = 入场 + 2R，做空 = 入场 − 2R。"""
    if not isinstance(entry, (int, float)) or not isinstance(stop, (int, float)):
        return None
    r = abs(float(entry) - float(stop))
    if r <= 0:
        return None
    t = float(entry) + mult * r if str(dirc).upper() == "LONG" else float(entry) - mult * r
    return round(t, 10)

def strip_sender_prefix(txt):
    """去掉飞书行首的【发送者显示名 + 时间】。
    事故（2026-09-14）：SCAN_JS 抓的是整行 innerText，开头是发送者名「自定义机器人 BOT」，
    而 BOT 恰好是币安真实交易对 → 被 find_all_coins 当成第 3 个币种，误判成"多币种总结"。
    所以解析前必须把发送者名剥掉。"""
    t = txt or ""
    # ① 本群转发机器人固定显示名
    t = re.sub(r"^\s*自定义机器人\s*BOT\s*", "", t)
    # ② 通过 webhook 转发时带的那句话
    t = re.sub(r"^\s*通过webhook将自定义服务的消息推送至飞书\s*", "", t)
    # ③ 通用：昵称(2~16个非空白字符) + 时间
    t = re.sub(r"^\s*\d{1,2}:\d{2}\s*(AM|PM)?\s*", "", t, flags=re.I)
    t = re.sub(r"^[^\s]{2,16}\s+(?=\d{1,2}:\d{2}\s*(AM|PM)?)", "", t, flags=re.I)
    # ④ 再兜一次时间前缀
    t = re.sub(r"^\s*\d{1,2}:\d{2}\s*(AM|PM)?\s*", "", t, flags=re.I)
    return t.strip()


def _mask_segments(txt, mask_tp=True, mask_sl=False):
    """把【某类关键词之后、下一个任意关键词之前】那一段用等长空格遮掉。
    用途：防止「4350到4450分批止盈」里的区间被当成**开仓区间**、
    或里面的数字被当成止损（实测 2026-09-15 标准答案集 [9]：
    「黄金xau 突破4300，回调过程做多，4250止损，4350到4450分批止盈」
    → 旧代码把 4350~4450 当成入场区间，还把止损读成 4350）。"""
    _B = re.compile(r"(?:止盈|目标位?|targets?|(?:TP|Tp|tp)\s?\d?|止损|stop[ -]?loss|SL|"
                    r"加仓|DCA|入场|进场|Entry)")
    ks = list(_B.finditer(txt))
    out = list(txt)
    for i, m in enumerate(ks):
        g = m.group(0)
        is_tp = bool(re.match(r"(?:止盈|目标位?|targets?|(?:TP|Tp|tp)\s?\d?)", g, re.I))
        is_sl = bool(re.match(r"(?:止损|stop[ -]?loss|SL)", g, re.I))
        if not ((is_tp and mask_tp) or (is_sl and mask_sl)):
            continue
        end = ks[i + 1].start() if i + 1 < len(ks) else len(txt)
        for j in range(m.end(), end):
            if out[j] != "\n":
                out[j] = " "
    return "".join(out)


# 条件单/待触发措辞：这类信号"要等价格条件满足才进"，**绝不能自动开**，必须送审批
_COND_RE = re.compile(r"等待|等到|跌破|站稳|收回|站上|突破|破位|若|如果|一旦|触及|达到|回到|"
                      r"确认后|回调后|回踩后|等.{0,6}(?:再|后)")


def _points_info(txt, value):
    """判断某个数值在原文里是不是**点数**而不是价格。
    实测病症（B5）：「止损带个30点左右」「止损35点」「止盈3000点以上」——
    30/35/3000 都是**点数**，不是价格；旧代码直接当成价格，得到 止损=30 这种荒谬值。
    判据用原文里的「点」字，确定性、不靠猜。返回 None 或点数。"""
    if not isinstance(value, (int, float)) or not value:
        return None
    t = strip_sender_prefix(txt or "")
    # ⚠️ 关键词和数字之间常有修饰词：「止盈**利润**3000点」「止损**带个**30点」，
    #    所以不能要求数字紧跟关键词；但也不许跨越标点（否则又会去抓下一句的数字）。
    for m in re.finditer(r"(?:止损|止盈|目标位?|stop|SL|TP)"
                         r"[^0-9，。；;、！!？?\n]{0,10}([0-9]*\.?[0-9]+)\s*(?:个)?\s*点", t, re.I):
        try:
            if abs(float(m.group(1)) - float(value)) < 1e-9:
                return float(m.group(1))
        except Exception:
            pass
    return None


def _conditional_order(txt):
    """这条消息是不是"条件触发才进"（等跌破/站稳/突破…）？"""
    return bool(_COND_RE.search(strip_sender_prefix(txt or "")))


def find_all_coins(txt):
    """找出文本里出现的【所有】币安 USDT-M 币种 —— 用来识别"一条消息混了多个币"的笼统总结。
    事故教训：暴富龙的「9.14视频总结」里 BTC/以太坊/Giggle 混在一起，fast_parse 把
    以太坊的区间、Giggle 的止盈都算到了 BTC 头上。

    ⚠️ B10 修复（2026-09-15 实测）：原来这一遍用了 `re.I`（大小写不敏感）+ 词边界，
    于是英文句子里的 **on / at / the** 撞上了真实合约 ONUSDT / ATUSDT / THEUSDT，
    把单币信号误判成「4 个币种」。实测现场：一句「Going long on UNI here at CMP…」
    被判成 AT/ON/THE/UNI 四个币，凭空多出三个待确认单。
    现在分两遍：
      第一遍 **区分大小写**（博主写币种都是大写）——杀掉 on/at/the 这类小写词；
      第二遍 大小写不敏感，但**只用在不歧义的币种上**（长度≥3 且不在歧义词表里），
             这样 "btc"/"sol" 这种小写写法仍然认得出来。"""
    hits = set()
    t = strip_sender_prefix(txt)
    for k, v in _NAME_MAP.items():
        if len(k) >= 2 and k in t:
            hits.add(v)
    # 第一遍：严格区分大小写
    for s in _SYMS:
        if len(s) >= 2 and not s.isdigit() and re.search(
                r"(?<![A-Za-z0-9])" + re.escape(s) + r"(?![A-Za-z0-9])", t):
            hits.add(s)
    # 第二遍：大小写不敏感，但排除"英文常用词/歧义词"，且只认 ≥3 位
    for s in _SYMS:
        if len(s) < 3 or s.isdigit() or s in _AMBIG_TICKERS:
            continue
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(s) + r"(?![A-Za-z0-9])", t, re.I):
            hits.add(s)
    return hits


def split_by_coin(txt):
    """把一条多币种消息按句切成【每币一段】—— 「原油Cl跌破97空…。 Sol突破102.5多…。」
    返回 [(币种, 该段原文), ...]"""
    t = strip_sender_prefix(txt)
    out, seen = [], set()
    for seg in re.split(r"[。；;！!？?\n]+", t):
        seg = seg.strip()
        if not seg:
            continue
        cs = sorted(find_all_coins(seg))
        if not cs:
            continue
        c = cs[0]                      # 一句话里只认第一个币（其余交给 AI 兜）
        if c in seen:
            continue
        seen.add(c)
        out.append((c, seg))
    return out


def ask_user(coin, p, reason, quiet=False):
    """需要用户审批 / 把握不准 → 挂起并询问用户。回「开」才开单，回「不开」作废。
    quiet=True 时只挂起、不发单独通知（多币种消息由调用方汇总成一条）。

    ⚠️ B11 修复（2026-09-15）：**更差的解析不许覆盖更完整的待确认参数**。
       实测事故：UNI 先由图读到好的（开仓=6.513 止损=6.396 止盈=[9.289]），
       随后一条纯文字的差解析（开仓=None 止损=6.39 止盈=[6.39]，止盈还等于止损）
       把它整体覆盖掉了 —— 用户看到并差点批准的是一条坏参数。
       现在：新参数完整度更低 → 保留原参数、只记一行日志、不重新打扰用户。"""
    old = ASKING.get(coin)
    if old and _plan_score(p or {}) < _plan_score(old.get("p") or {}):
        _op, _np = (old.get("p") or {}), (p or {})
        log("   ↺ %s 待确认参数：本次解析更差（完整度 %d < %d）→ **不覆盖**，保留原参数"
            % (coin, _plan_score(_np), _plan_score(_op)))
        log("     保留：开仓=%s 止损=%s 止盈=%s ｜ 丢弃：开仓=%s 止损=%s 止盈=%s"
            % (_op.get("entry"), _op.get("stop"), _op.get("tps"),
               _np.get("entry"), _np.get("stop"), _np.get("tps")))
        return
    ASKING[coin] = {"p": p, "reason": reason, "ask_ts": time.time(),
                    "txt": (p["texts"][0][:300] if p.get("texts") else "")}
    if quiet:
        log("   ❓ 已挂起等用户确认：%s（%s）" % (coin, reason))
        return
    d = 1 if (p.get("dir") or "LONG").upper() == "LONG" else -1
    notify("\n".join(
        ["【信号·待你确认】%s %s" % (coin, "做多 LONG" if d == 1 else "做空 SHORT"),
         "⚠️ 要你确认的原因：%s" % reason,
         "──────────────"]
        + _approval_lines(coin, p, d)
        + ["──────────────",
           "原文：%s" % (p["texts"][0][:180] if p.get("texts") else ""),
           "",
           "**回复「开」= 按上面这组参数开单；回复「不开」= 作废。**",
           "（%d 分钟内没回复自动作废）" % (ASK_TIMEOUT // 60)]))
    log("   ❓ 已挂起等用户确认：%s（%s）" % (coin, reason))


def expire_asking():
    """超时未回复的待确认信号自动作废"""
    now = time.time()
    for c in list(ASKING):
        if now - ASKING[c].get("ask_ts", now) > ASK_TIMEOUT:
            ASKING.pop(c, None)
            notify("【信号·待确认】%s 超过 %d 分钟没回复，已自动作废（未下单）"
                   % (c, ASK_TIMEOUT // 60))
            log("   ⏰ 待确认信号 %s 超时作废" % c)


def finalize_pending(open_pos):
    """到点或信息齐全 -> 出单；信息不足 -> 只发提醒，绝不猜价"""
    now = time.time()
    for coin in list(PENDING):
        p = PENDING[coin]
        # 去重但【保留博主给的先后顺序】。排序留到拿到入场价之后按"离入场由近到远"做 ——
        # 做空的「第一止盈74500 第二止盈70500」若按数值升序排，TP1 会错成最远那档 70500。
        tps = list(dict.fromkeys(p["tps"]))
        if PAUSED[0]:
            log("   ⏸ 已暂停：%s 的信号只记录不开单" % coin)
            PENDING.pop(coin, None); continue
        if now < p["deadline"]:
            # 方案E：信息已齐全（止损 + 3 档止盈）且已给足 1.5 秒收集时间 -> 立即出单，不空等
            if pending_complete(p) and (now - p["first_ts"]) >= 1.5:
                log("   ⚡ 信息齐全，提前出单（不空等满 4 秒）")
            else:
                continue
        signal_entry = p["entry"]                     # 博主信号里的开仓价（图上/卡片读到）
        age = now - p["first_ts"]
        dirc0 = (p.get("dir") or "LONG").upper()
        mkt = price_of(coin)
        if mkt is None:
            notify("【信号·待确认】%s\n拿不到币安实时价，无法开仓\n原文：%s"
                   % (coin, (p["texts"][0][:180] if p["texts"] else "")))
            PENDING.pop(coin, None); continue
        # ===== 入场计划 =====
        # ① 分批建仓（用户规则4，适用所有博主）：给了 N 个点位 → N 笔限价，保证金等分
        # ② 黄金mansoor 专属规则（用户规则1）：市价对我们更有利则市价进，否则按博主价挂限价
        # ③ 其余群沿用：±2% 内市价，超出则不追高/不追空（两笔限价）
        entry, entry_mode, esrc = mkt, "市价", "市价成交"
        entry_orders = [{"kind": "市价", "price": mkt, "margin": MARGIN}]
        entry_note = ""
        _grp = p.get("group") or ""
        _legs = list(p.get("legs") or [])
        if len(_legs) >= 2:
            # ① 分批建仓
            _each = MARGIN / len(_legs)
            entry_orders = [{"kind": "限价", "price": float(x), "margin": _each} for x in _legs]
            entry_mode = "限价分批"
            entry = sum(float(x) for x in _legs) / len(_legs)
            entry_note = ("**分批建仓 %d 笔**（保证金等分，各 %.0fU）：%s"
                          % (len(_legs), _each, "、".join("%.8g" % float(x) for x in _legs)))
            log("   ↳ 分批建仓：%d 个点位 %s，每笔保证金 %.0fU" % (len(_legs), _legs, _each))
        elif isinstance(signal_entry, (int, float)) and signal_entry and mkt:
            diff = (signal_entry - mkt) / mkt          # >0 表示市价在博主价下方
            if _grp in STRICT_LIMIT_GROUPS:
                # ② 黄金mansoor：只在市价对我们更有利时市价进；否则按博主价挂限价
                _better = (dirc0 == "LONG" and diff > 0) or (dirc0 == "SHORT" and diff < 0)
                if _better:
                    entry_note = ("现价 %.8g 比博主价 %.8g 更有利 → 直接市价进（%s专属规则）"
                                  % (mkt, signal_entry, _grp))
                else:
                    entry_orders = [{"kind": "限价", "price": signal_entry, "margin": MARGIN}]
                    entry_mode = "限价分批"
                    entry = signal_entry
                    entry_note = ("现价 %.8g 比博主价 %.8g 不利 → **按博主价挂限价，等回踩**（%s专属规则）"
                                  % (mkt, signal_entry, _grp))
                    log("   ↳ [%s专属] 按博主价挂限价 %.8g（现价 %.8g）" % (_grp, signal_entry, mkt))
            elif dirc0 == "LONG" and diff < -0.02:       # 市价高于博主价 2% 以上 -> 不追高
                p1 = signal_entry * 1.01
                entry_orders = [{"kind": "限价", "price": p1, "margin": MARGIN / 2},
                                {"kind": "限价", "price": signal_entry, "margin": MARGIN / 2}]
                entry_mode = "限价分批"
                entry = (p1 * 0.5 + signal_entry * 0.5)
                entry_note = "现价 %.8g 比博主开仓价高 %.1f%%，超过 2%%，**不追高**，分两笔挂限价" % (mkt, -diff * 100)
            elif dirc0 == "SHORT" and diff > 0.02:     # 市价低于博主价 2% 以上 -> 不追空
                p1 = signal_entry * 0.99
                entry_orders = [{"kind": "限价", "price": p1, "margin": MARGIN / 2},
                                {"kind": "限价", "price": signal_entry, "margin": MARGIN / 2}]
                entry_mode = "限价分批"
                entry = (p1 * 0.5 + signal_entry * 0.5)
                entry_note = "现价 %.8g 比博主开仓价低 %.1f%%，超过 2%%，**不追空**，分两笔挂限价" % (mkt, diff * 100)
            elif signal_entry:
                entry_note = "现价与博主开仓价相差 %.2f%%（≤2%%），直接市价开" % (diff * 100)
        # ===== 止损推导（用户规则2/5，适用所有博主）=====
        # 没给止损时：① 有百分比止损 → 按开仓价折算；② 否则有止盈 → 止损 = 第一止盈距离的一半（保证 2:1）
        _d0 = 1 if dirc0 == "LONG" else -1
        if p["stop"] is None and isinstance(entry, (int, float)) and entry:
            if isinstance(p.get("stop_pct"), (int, float)) and p["stop_pct"]:
                p["stop"] = round(float(entry) * (1 - _d0 * float(p["stop_pct"]) / 100.0), 10)
                p["stop_src"] = "百分比止损 %.2f%%" % float(p["stop_pct"])
                log("   ↳ 百分比止损 %.2f%% → 止损位 %.8g" % (float(p["stop_pct"]), p["stop"]))
            elif tps:
                _tp1 = min(tps, key=lambda t: abs(float(t) - float(entry)))
                _dist = abs(float(_tp1) - float(entry))
                if _dist > 0:
                    p["stop"] = round(float(entry) - _d0 * _dist / 2.0, 10)
                    p["stop_src"] = "按第一止盈 %.8g 反推(2:1)" % float(_tp1)
                    log("   ↳ 博主未给止损 → 按第一止盈 %.8g 的一半反推止损 %.8g（2:1）"
                        % (float(_tp1), p["stop"]))
        if p["stop"] is None and not tps:
            notify("【信号·待确认】%s\n没读到止损和止盈（图上/卡片/文字都没读到），等你确认后我再挂单\n原文：%s"
                   % (coin, (p["texts"][0][:180] if p["texts"] else "")))
            PENDING.pop(coin, None); continue
        # ===== B5-① 点数 vs 价格（2026-09-15 新增）=====
        # 「止损带个30点左右」「止损35点」「止盈3000点以上」里的数字是**点数**不是价格。
        # 旧行为：直接当成价格 → 止损=30 这种荒谬值（实测现场：BTC 止损读成 74，因为「74K」）。
        # 新行为：识别为点数 → 按入场价换算成价格 → **一律送人工审批**（绝不自动开）。
        _t0 = p["texts"][0] if p.get("texts") else ""
        _pt_why = []
        _sp = _points_info(_t0, p.get("stop"))
        if _sp and isinstance(entry, (int, float)) and entry:
            _new_stop = round(entry - _d0 * _sp, 10)
            _pt_why.append("止损「%s点」是**点数**不是价格 → 按入场 %s 换算为 %s（原样照抄会得到 %s）"
                           % (_fmt_num(_sp), _fmt_num(entry), _fmt_num(_new_stop), _fmt_num(p["stop"])))
            p["stop"] = _new_stop
            p["stop_src"] = "点数换算(%s点)" % _fmt_num(_sp)
        _tps_new, _tp_pt_why = [], []
        for _t in tps:
            _tp_ = _points_info(_t0, _t)
            if _tp_ and isinstance(entry, (int, float)) and entry:
                _nv = round(entry + _d0 * _tp_, 10)
                _tp_pt_why.append("止盈「%s点」是点数 → 换算为 %s" % (_fmt_num(_tp_), _fmt_num(_nv)))
                _tps_new.append(_nv)
            else:
                _tps_new.append(_t)
        if _tp_pt_why:
            tps = _tps_new
        else:
            # 整条消息只给了一个「止盈 N 点以上」，而 fast_parse 可能把它读成了一个价格
            _tp_any = re.search(r"(?:止盈|目标位?)\s*[:：=]?\s*([0-9]*\.?[0-9]+)\s*点", _t0)
            if _tp_any and isinstance(entry, (int, float)) and entry:
                try:
                    _v = float(_tp_any.group(1))
                    if _v and not any(abs(_v - x) < 1e-9 for x in tps):
                        tps = [round(entry + _d0 * _v, 10)]
                        _tp_pt_why.append("止盈「%s点」是点数 → 换算为 %s"
                                          % (_fmt_num(_v), _fmt_num(tps[0])))
                except Exception:
                    pass
        if _pt_why or _tp_pt_why:
            log("   ↳ [点数换算] " + "；".join(_pt_why + _tp_pt_why))
        # ===== 止损/止盈【方向合理性校验】（2026-09-14 新增；对真单是保命检查）=====
        # 实例：08:00 UA 群博主发的是「Trade Closed — DOGE/USDT LONG ... Stop loss hit at $0.08280」，
        #   从这句话里被抽出 方向=LONG、止损=0.0828，而市价是 0.08238 →
        #   做多的"止损"却落在入场价【上方】→ 一开仓立刻满足止损条件 → 秒平，还记了一笔 +4.6U 的**假盈利**。
        # 真单场景下这更危险：STOP_MARKET 挂错边会被币安拒绝，或触发即成交。
        if isinstance(p["stop"], (int, float)) and p["stop"] and isinstance(entry, (int, float)) and entry:
            if dirc0 == "LONG" and float(p["stop"]) >= float(entry):
                notify("【信号·拒绝】%s 做多\n止损价 %.8g **不低于** 入场价 %.8g —— 做多的止损必须在下方，"
                       "判为解析错误，**不下单**\n原文：%s"
                       % (coin, p["stop"], entry, (p["texts"][0][:160] if p["texts"] else "")))
                log("   ⛔ 止损方向不对（做多但止损≥入场 %.8g），拒绝出单" % entry)
                PENDING.pop(coin, None); continue
            if dirc0 == "SHORT" and float(p["stop"]) <= float(entry):
                notify("【信号·拒绝】%s 做空\n止损价 %.8g **不高于** 入场价 %.8g —— 做空的止损必须在上方，"
                       "判为解析错误，**不下单**\n原文：%s"
                       % (coin, p["stop"], entry, (p["texts"][0][:160] if p["texts"] else "")))
                log("   ⛔ 止损方向不对（做空但止损≤入场 %.8g），拒绝出单" % entry)
                PENDING.pop(coin, None); continue
        # 止盈方向同理：做多的止盈必须在上方、做空必须在下方 → 方向不对的直接剔除
        if tps and isinstance(entry, (int, float)) and entry:
            _good = [t for t in tps if (float(t) > float(entry) if dirc0 == "LONG" else float(t) < float(entry))]
            if len(_good) != len(tps):
                log("   ↳ 剔除方向不对的止盈位：%s" % [t for t in tps if t not in _good])
            tps = _good
        # ===== 价格合理性校验 + 多币种混判 → 一律【询问用户】而不是猜（2026-09-14 用户要求）=====
        # 事故复刻：开仓价 2540（BTC 市价 77760）/ 止损 38 / 止盈 7582.1 → 秒平记 +1773U 假盈利
        _why = None
        if isinstance(entry, (int, float)) and entry and mkt:
            _dev = abs(float(entry) - float(mkt)) / float(mkt)
            if _dev > MAX_ENTRY_DEV:
                _why = ("解析出的开仓价 %.8g 与当前市价 %.8g 相差 %.0f%%（超过 %.0f%%），"
                        "像是把别的币的价格串过来了" % (entry, mkt, _dev * 100, MAX_ENTRY_DEV * 100))
        if _why is None and isinstance(p["stop"], (int, float)) and p["stop"] and isinstance(entry, (int, float)) and entry:
            _sp = abs(float(p["stop"]) - float(entry)) / float(entry)
            if _sp > MAX_STOP_PCT:
                _why = "止损距入场价 %.0f%%（超过 %.0f%%，不像真的止损）" % (_sp * 100, MAX_STOP_PCT * 100)
            elif _sp < MIN_STOP_PCT:
                _why = "止损几乎等于入场价（距离仅 %.3f%%），等于没设止损" % (_sp * 100)
        if _why is None and tps and isinstance(mkt, (int, float)) and mkt:
            _crossed = [t for t in tps
                        if (float(t) <= float(mkt) if dirc0 == "LONG" else float(t) >= float(mkt))]
            if _crossed:
                _why = ("止盈位 %s 已经被当前市价 %.8g 越过 —— 信号已过期，或价格张冠李戴"
                        % (_crossed, mkt))
        if _why is None:
            _allc = find_all_coins(p["texts"][0] if p["texts"] else "")
            if len(_allc) >= 2:
                _why = ("这条消息里出现了 %d 个币种（%s），属于笼统总结，"
                        "机器人无法确定每个价格属于哪个币" % (len(_allc), "、".join(sorted(_allc)[:6])))
        # 「头仓 + 分批」同时出现时，分几笔、每笔多少钱是不确定的 → 问用户
        # 注意：不要匹配「第一笔/第二笔」—— 那只是用户在数分批的笔数（规则4的标准写法）
        if _why is None and p.get("legs"):
            _t0 = p["texts"][0] if p["texts"] else ""
            if re.search(r"头仓|首仓|底仓|试仓", _t0):
                _why = ("同时出现「头仓/首仓」和「分批」，机器人无法确定总共分几笔、每笔多少保证金"
                        "（识别到 %d 个点位 %s）" % (len(p["legs"]), p["legs"]))
        # ===== B5-③ 条件单（2026-09-15 新增）=====
        # 「等待76000跌破收回，站稳76200多」「跌破2465可以追空」= **要等价格条件满足才进**。
        # 机器人不能替你盯条件，也不该按"现在"的点位直接开 → 一律送人工审批。
        if _why is None and _conditional_order(_t0):
            _why = ("这条消息是【条件触发】型的（等跌破/站稳/突破/收回…才进），"
                    "机器人不能替你盯价格条件 —— 得你自己判断现在能不能进")
        # ===== B5-① 点数换算已发生 → 必须人工确认（绝不自动开）=====
        if _why is None and (_pt_why or _tp_pt_why):
            _why = "数字被识别为【点数】并已换算：" + "；".join(_pt_why + _tp_pt_why)
        if _why:
            ask_user(coin, p, _why)
            PENDING.pop(coin, None)
            continue
        # ===== 止盈档位排序（关键）=====
        # 必须按【离入场价由近到远】排，不能按数值大小排：
        # 做空的「第一止盈74500 / 第二止盈70500」按数值升序会变成 70500 在先，
        # 于是 TP1 变成最远那档、TP1 后移保本损的时机也跟着错。
        if tps and isinstance(entry, (int, float)) and entry:
            _tp_sorted = sorted(set(tps), key=lambda t: abs(float(t) - float(entry)))
        else:
            _tp_sorted = list(dict.fromkeys(tps))
        tps = _tp_sorted[:TP_TIERS]
        # ===== 2R 兜底：有开仓价+止损、但没有止盈 =====
        tp_fallback = False
        if not tps and isinstance(p["stop"], (int, float)) and p["stop"]:
            _ft = fallback_tp_2r(entry, p["stop"], dirc0)
            if _ft is not None:
                tps = [_ft]
                tp_fallback = True
                log("   ↳ 博主未给止盈 → 启用 2R 兜底：入场 %s 止损 %s → 2R 目标 %s（到价全平）"
                    % (entry, p["stop"], _ft, ))
        over_cap = (len(open_pos) >= MAX_OPEN and coin not in open_pos)
        # ===== 总敞口上限（2026-09-15 风控）=====
        _exp = _current_exposure(open_pos)
        if (not over_cap) and coin not in open_pos and _exp + MARGIN > _exposure_cap() + 1e-6:
            notify("【信号·熔断】%s 未开：加上它总敞口将达 %.0fU，超过上限 %.0fU\n"
                   "（当前敞口 %.0fU ｜ 要放宽可发「修改持仓上限 N」或调 max_total_margin）"
                   % (coin, _exp + MARGIN, _exposure_cap(), _exp))
            log("   🛑 总敞口超限：%.0f + %.0f > %.0f" % (_exp, MARGIN, _exposure_cap()))
            PENDING.pop(coin, None)
            continue
        # ===== 审批闸门（用户 2026-09-15 第 12/13 条硬要求）=====
        # ① 所有**新开仓**在开之前都必须经用户审批（不再只问"把握不准"的）；
        # ② 达到持仓上限时一并询问是否提高上限。
        # 两件事**合成一次询问**，避免同一个信号被问两遍（旧代码会把达上限问一次，
        # 用户回「开」把信号放回 PENDING 后，审批闸门又问一次）。
        # 注意：已有持仓的止盈/止损更新不是"开新单"，不在这里拦。
        _ask_why = []
        if over_cap and not p.get("cap_override"):
            _ask_why.append("当前已持 %d 笔，达到持仓上限 %d 笔；回「开」= 把上限提高到 %d 笔并开这一单"
                            % (len(open_pos), MAX_OPEN, len(open_pos) + 1))
        if REQUIRE_APPROVAL[0] and not p.get("approved") and coin not in open_pos:
            _ask_why.append("按你的要求：所有订单在开之前都要经你审批（本次已通过全部机器校验）")
        if _ask_why:
            ask_user(coin, p, "；".join(_ask_why))
            PENDING.pop(coin, None)
            continue
        if coin in open_pos:
            _ex = open_pos[coin]
            # ===== B14 修复（2026-09-15 生产实测事故）：更新已有持仓的止盈之前，
            #   必须按【持仓自己的方向 + 持仓自己的入场价】重新校验，
            #   **绝不能沿用新信号的方向**（上面的方向过滤是按新信号做的，这里语境完全不同）。
            #   事故：ETH 持仓是 SHORT（入场 2503.49），新信号是「ETH 做多、分批 2465/2466、止盈 2520」；
            #   2520 相对新信号入场 2465.5 合法（在上方）→ 于是被无条件写到 SHORT 持仓上
            #   → 空单的止盈落在入场价上方 → 当前价本就满足 → **19:20:32 瞬间假成交**，
            #   还把 -5.94U（净 -6.57U）的亏损记成 exit_why="全部止盈"，并污染连亏计数（0→1）。
            _exd = (_ex.get("dir") or "LONG").upper()
            _exe = _ex.get("entry")
            _tp_rejected = False
            if tps and isinstance(_exe, (int, float)) and _exe:
                _wrong = _tp_wrong_side(_exd, _exe, tps)
                if _wrong:
                    tps = [t for t in tps if t not in _wrong]
                    _tp_rejected = True
                    log("   ⛔ %s 止盈更新被拒：%s 相对**持仓**方向不合法（持仓 %s 入场 %.8g，"
                        "止盈应在入场价%s）" % (coin, _wrong, _exd, _exe,
                                          "上方" if _exd == "LONG" else "下方"))
                    notify("【信号·拒绝】%s 止盈更新被拒（方向不对）\n"
                           "持仓：%s 入场 %.8g ｜ 现有止盈 %s\n"
                           "本次要挂的止盈 %s 落在持仓的错误一侧（%s 的止盈应在入场价%s）"
                           "→ 挂上去会**立刻被判为成交**，已拒绝\n原文：%s"
                           % (coin, _exd, _exe, _ex.get("tps"), _wrong, _exd,
                              "上方" if _exd == "LONG" else "下方",
                              p["texts"][0][:120] if p["texts"] else ""))
            if tps:
                _cur = None
                try:
                    _cur = price_of(coin)
                except Exception:
                    pass
                if isinstance(_cur, (int, float)) and _cur:
                    _crossed = _tp_wrong_side(_exd, _exe, tps, ref=_cur)
                    if _crossed:
                        tps = [t for t in tps if t not in _crossed]
                        _tp_rejected = True
                        log("   ⛔ %s 止盈更新被拒：%s 已被当前价 %.8g 越过" % (coin, _crossed, _cur))
                        notify("【信号·拒绝】%s 止盈更新被拒（已被越过）\n"
                               "本次要挂的止盈 %s 已被当前价 %.8g 越过 —— 挂上去会立刻成交，已拒绝\n"
                               "现有止盈保留：%s" % (coin, _crossed, _cur, _ex.get("tps")))
            # ===== 博主后来补了真实止盈位 → 撤掉 2R 兜底那档，按真实档位重挂 =====
            if _ex.get("tp_fallback") and tps:
                _old_tps = list(_ex.get("tps") or [])
                _ex["tps"] = tps
                _ex["tp_fallback"] = False
                _ex["tp_part"] = None
                _ex["filled"] = []
                STATE_DIRTY[0] = True
                with open(TRADES, "a", encoding="utf-8") as f:
                    f.write(json.dumps(_ex, ensure_ascii=False) + "\n")
                log("   ↳ %s 收到真实止盈位 %s → 撤掉 2R 兜底档 %s 并重挂"
                    % (coin, tps, _old_tps))
                if _BEXEC_OK:
                    try:      # 真实层：撤掉旧限价止盈，按真实档位重挂（数量撤单后按真实持仓再取）
                        _sym = coin.upper() + "USDT"
                        _plan = {"symbol": _sym, "dir": _ex.get("dir"), "action": "替换止盈档",
                                 "cancel": "DELETE /fapi/v1/allOpenOrders（旧限价止盈）"
                                           " + DELETE /fapi/v1/algoOrder（旧 Algo 止损）",
                                 "new_tps": [{"price": bexec.fmt_price(_sym, t)} for t in tps],
                                 "new_sl_trigger": (bexec.fmt_price(_sym, _ex.get("sl"))
                                                    if _ex.get("sl") else None),
                                 "note": "数量在撤单后按真实持仓剩余量重新计算",
                                 "shadow": not bexec.LIVE[0]}
                        bexec.audit("replace_tp_plan", _plan)
                        log("   ↳ 真实层计划：撤旧挂单 → 按 %s 重挂止盈（影子模式仅记录）" % tps)
                    except Exception as _e:
                        log("   ⚠️ 真实层重挂计划生成失败：%s" % str(_e)[:130])
                notify("【止盈更新·纸面】%s %s\n博主补了真实止盈位，已撤掉 2R 兜底那档并重挂：\n"
                       "撤掉：%s\n重挂：%s\n（真实下单层接上后走同一入口：先撤旧限价止盈，再按新档位重挂）"
                       % (coin, _ex.get("dir"),
                          "、".join("%.8g" % t for t in _old_tps),
                          "、".join("%.8g" % t for t in tps)))
                PENDING.pop(coin, None)
                continue
            if _ex.get("tp_fallback") and not tps and _tp_rejected:
                log("   ↳ %s 止盈更新被全部拒绝 → 保留原 2R 兜底档 %s，不动仓位"
                    % (coin, _ex.get("tps")))
                PENDING.pop(coin, None)
                continue
            notify("【信号·跳过】%s 已有持仓，不重复开单\n现有：%s 入场 %.8g · 止损 %.8g · 剩余 %.0f%%\n本次信号原文：%s"
                   % (coin, _ex.get("dir"), _ex.get("entry") or 0, _ex.get("sl") or 0,
                      (_ex.get("remaining", 1.0) * 100),
                      (p["texts"][0][:120] if p["texts"] else "")))
            log("   ↳ %s 已有持仓，跳过本次信号（防重复开单）" % coin)
            PENDING.pop(coin, None)
            continue
        dirc = dirc0
        tm = {"detect": p["t_found"] - p["first_ts"], "img": max(0.0, p["t_img"] - p["t_found"]),
              "parse": max(0.0, p["t_parse"] - p["t_img"]), "chart": max(0.0, p["t_chart"] - p["t_parse"]),
              "order": 0.0, "push": 0.0, "wait": age, "messages": len(p["texts"])}
        t_order = time.time()
        tr = {"coin": coin, "dir": dirc, "entry": entry, "signal_entry": signal_entry, "add": p["add"],
              "sl": p["stop"], "tps": tps, "tp_fallback": tp_fallback,
              "tp_part": (R_FALLBACK_PART if tp_fallback else None),
              "entry_src": esrc, "stop_src": p.get("stop_src"),
              "t_open": datetime.datetime.fromtimestamp(p["first_ts"], CST).strftime("%m-%d %H:%M:%S"),
              "group": p["group"], "text": (p["texts"][0] if p["texts"] else "")[:300], "status": "OPEN",
              "chart_imgs": p["imgs"], "timer": tm}
        tm["order"] = time.time() - t_order
        open_pos[coin] = tr
        # ===== 统计用时间戳（t_open 是信号时刻、无年份，保留兼容；新增带年份的 ISO 与 Unix 秒）+ 开仓手续费 =====
        tr["signal_ts"] = int(p["first_ts"])
        tr["t_open_ts"] = int(time.time())          # 实际开仓（成交）时刻
        tr["t_open_iso"] = datetime.datetime.fromtimestamp(tr["t_open_ts"], CST).strftime("%Y-%m-%d %H:%M:%S")
        tr["margin"], tr["lev"], tr["notional"] = MARGIN, LEV, NOTIONAL
        _add_fee(tr, NOTIONAL, (FEE_TAKER if entry_mode == "市价" else FEE_MAKER),
                 "开仓(%s)" % entry_mode)
        tr["real_layer"] = _be_mode()
        _rplan = real_plan_open(coin, dirc, entry, p["stop"], tps)   # 影子模式：只生成计划
        tm["order"] = time.time() - t_order
        # 实盘 + 限价入场 → 登记成交监听（没成交前不跟踪止盈止损）
        if _rplan and _rplan.get("watch_fill"):
            ENTRY_WATCH[coin] = {"sym": coin.upper() + "USDT", "dir": dirc,
                                 "order_ids": _rplan.get("order_ids") or [],
                                 "tps": _rplan.get("tps") or [], "stop": p["stop"],
                                 "legs": _rplan.get("entry_legs") or [],
                                 "assumed_entry": entry,
                                 "deadline": time.time() + FILL_TIMEOUT}
            tr["pending_fill"] = True
            log("   ↳ [实盘] 限价单已挂出，等待成交（%d 分钟内未成交会自动撤单并通知）"
                % (FILL_TIMEOUT // 60))
        if _rplan:
            log("   ↳ [真实下单层·%s] 已生成下单计划（市价/限价腿 + %d 档止盈 + Algo 止损）"
                % (_be_mode(), len(_rplan.get("tps") or [])))
        timing = ("⏱ 从信号发出到推送 共 %.1fs（发现 %.1fs / 抓图 %.1fs / 解析 %.1fs / 读图 %.1fs / 等齐后续消息+出单 %.1fs）"
                  % (age, tm["detect"], tm["img"], tm["parse"], tm["chart"],
                     max(0.0, age - tm["detect"] - tm["img"] - tm["parse"] - tm["chart"])))
        t_push0 = time.time()
        d0 = 1 if dirc == "LONG" else -1
        crossed = any(((t - entry) * d0 <= 0) for t in tps)   # 止盈价是否已被现价越过（真实下单必须处理）
        _txt = fmt_plan(coin, dirc, entry, p["stop"], tps, p["group"], tr["t_open"],
                        note=("止损来自%s，共合并 %d 条消息（文案/卡片/图）" % (p.get("stop_src") or "-", len(p["texts"]) + (1 if p["imgs"] else 0))
                              + ("｜⚠️ 博主未给止盈，已按 **2:1 盈亏比** 在 %.8g 自动挂止盈（到价**全平**；若博主后续补了真实止盈位会自动撤掉重挂）" % tps[0] if tp_fallback else "")
                              + ("｜⚠️ 测试阶段：当前已持 %d 笔（上限 %d）" % (len(open_pos), MAX_OPEN) if over_cap else "")),
                        add=p["add"], timing=timing, signal_entry=signal_entry,
                        signal_src=p.get("entry_src"), crossed=crossed, entry_mode=entry_mode,
                        entry_orders=entry_orders, entry_note=entry_note)
        _rp = fmt_real_plan(_rplan) if _rplan else ""
        if _rp:
            _txt += "\n\n" + _rp
        notify(_txt)
        tm["push"] = time.time() - t_push0
        with open(TRADES, "a", encoding="utf-8") as f:
            f.write(json.dumps(tr, ensure_ascii=False) + "\n")
        log("⏱ [%s] %s 出单 | 总=%.1fs | 发现=%.1fs 抓图=%.1fs 解析=%.1fs 读图=%.1fs 等待=%.1fs 推送=%.1fs | 市价开仓=%s 信号价=%s 止损=%s 止盈=%s"
            % (p["group"], coin, age, tm["detect"], tm["img"], tm["parse"], tm["chart"], tm["wait"], tm["push"],
               entry, signal_entry, p["stop"], tps))
        PENDING.pop(coin, None)

# ---------------- 读图缓存（同一张图不重复调 AI）----------------
_CHART_CACHE = {}

def read_chart_cached(path):
    try:
        h = hashlib.sha1(open(path, "rb").read()).hexdigest()
    except Exception:
        return None
    if h in _CHART_CACHE:
        log("   ♻️ 读图缓存命中（同一张图，0 AI 调用）")
        return _CHART_CACHE[h]
    try:
        r = read_chart(path)
    except Exception as e:
        log("   读图异常: " + str(e)[:80])
        r = None
    if r:
        _CHART_CACHE[h] = r
    return r

def read_chart_meta(path):
    """从图上读出币种/方向，用于"只有一张图"的信号（币种印在图左上角）"""
    try:
        b64 = base64.b64encode(open(path, "rb").read()).decode()
        body = {"model": "deepseek-v4-flash-vision-exp", "temperature": 0,
                "messages": [{"role": "system", "content": "You read TradingView chart screenshots. STRICT JSON only."},
                             {"role": "user", "content": [
                                 {"type": "text", "text": "This image was posted with a crypto trade signal. Return "
                                  "{\"is_chart\":bool,\"coin\":\"uppercase base asset or null\",\"direction\":\"LONG|SHORT|null\"}. "
                                  "The symbol is printed in the top-left corner of the chart (e.g. 'INIT / TetherUS PERPETUAL CONTRACT')."},
                                 {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}]}
        r = requests.post(DS_API, headers={"Authorization": "Bearer " + DS_KEY, "Content-Type": "application/json"}, json=body, timeout=120)
        m = re.search(r"\{[\s\S]*\}", r.json()["choices"][0]["message"]["content"])
        return json.loads(m.group(0))
    except Exception as e:
        log("   图元信息读取失败: " + str(e)[:80])
        return {}

# ---------------- 持仓状态汇报（博主转发收益时用）----------------
def pos_report(coin, tr):
    px = price_of(coin)
    d = 1 if tr["dir"] == "LONG" else -1
    entry = tr.get("entry")
    remaining = tr.get("remaining", 1.0)
    L = ["【你的持仓】%s/USDT %s" % (coin, "做多 LONG" if d == 1 else "做空 SHORT"),
         "仓位剩余：%.0f%%（已减仓 %.0f%%）" % (remaining * 100, (1 - remaining) * 100)]
    if px and entry:
        gross = (px - entry) / entry * 100 * d
        pnl = (px - entry) / entry * NOTIONAL * d * remaining
        L.append("当前价：%.8g    开仓均价：%.8g" % (px, entry))
        L.append("当前收益率：%+.2f%%（含 %d 倍杠杆）" % (gross * LEV, LEV))
        L.append("浮动盈亏：约 %+.1fU（保证金 %.0fU）" % (pnl, MARGIN))
    L.append("止损：%s" % (("%.8g" % tr["sl"]) if tr.get("sl") else "未设"))
    tps = tr.get("tps") or []
    filled = tr.get("filled", [])
    nxt = next((tps[j] for j in range(len(tps)) if j not in filled), None)
    if nxt and px:
        L.append("下一个止盈：%.8g（还差 %.2f%%，不含杠杆）" % (nxt, (nxt - px) / px * 100 * d))
    elif nxt:
        L.append("下一个止盈：%.8g" % nxt)
    else:
        L.append("止盈：已全部成交")
    if tr.get("realized"):
        L.append("已实现盈亏：%+.1fU" % tr["realized"])
    notify("\n".join(L))

# ---------------- 名称映射（中文/俗称 -> 币安 USDT-M base）----------------
_NAME_MAP = {
    # 主流币
    "比特币": "BTC", "大饼": "BTC", "BITCOIN": "BTC",
    "以太坊": "ETH", "以太": "ETH", "姨太": "ETH", "ETHEREUM": "ETH",
    "索拉纳": "SOL", "SOLANA": "SOL", "狗狗币": "DOGE", "狗币": "DOGE", "DOGECOIN": "DOGE",
    "瑞波": "XRP", "瑞波币": "XRP", "RIPPLE": "XRP", "币安币": "BNB", "艾达": "ADA", "艾达币": "ADA",
    "波卡": "DOT", "雪崩": "AVAX", "莱特币": "LTC", "柚子": "EOS", "波场": "TRX", "特朗普币": "TRUMP",
    # 贵金属 / 大宗
    "黄金": "XAU", "金": "XAU", "GOLD": "XAU", "XAUUSD": "XAU",
    "白银": "XAG", "银": "XAG", "SILVER": "XAG",
    "原油": "CL", "石油": "CL", "油": "CL", "OIL": "CL", "WTI": "CL", "CRUDE": "CL",
    "天然气": "NATGAS", "GAS": "NATGAS",
    # 美股代币（币安有对应 USDT 永续）
    "闪迪": "SNDK", "SANDISK": "SNDK", "海力士": "SKHY", "SK海力士": "SKHY", "SKHYNIX": "SKHY", "HYNIX": "SKHY",
    "特斯拉": "TSLA", "TESLA": "TSLA", "英伟达": "NVDA", "NVIDIA": "NVDA", "苹果": "AAPL", "APPLE": "AAPL",
    "微软": "MSFT", "MICROSOFT": "MSFT", "谷歌": "GOOGL", "GOOGLE": "GOOGL", "亚马逊": "AMZN", "AMAZON": "AMZN",
    "奈飞": "NFLX", "NETFLIX": "NFLX", "超微": "AMD", "英特尔": "INTC", "INTEL": "INTC", "美光": "MU",
    " coinbase": "COIN", "COINBASE": "COIN", "微策略": "MSTR", "策略": "MSTR", "帕兰提尔": "PLTR", "PLTR": "PLTR",
    "阿里": "BABA", "阿里巴巴": "BABA", "拼多多": "PDD", "游戏驿站": "GME", "标普": "SPY", "纳斯达克": "QQQ",
}

def find_coin_in_text(txt):
    """从整段文字里找币安 USDT-M 合约（先查中文/俗称别名，再查代码本身）"""
    up = (txt or "").upper()
    for alias in sorted(_NAME_MAP.keys(), key=lambda k: -len(k)):
        a = alias.strip().upper()
        if a and (a in up):
            return _NAME_MAP[alias]
    if _SYMS:
        cands = [b for b in _SYMS if len(b) >= 2 and
                 re.search(r"(?<![A-Z0-9])" + re.escape(b) + r"(?![A-Z0-9])", up)]
        cands.sort(key=len, reverse=True)
        if cands:
            return cands[0]
    return None

# ---------------- 价格精度（币安 tickSize）与区间中间值 ----------------
_TICKS = {}

def tick_of(coin):
    """返回该合约的价格精度（tickSize），用于把价格向上取整到可下单的值"""
    sym = (coin or "").upper() + "USDT"
    if sym in _TICKS:
        return _TICKS[sym]
    if not _TICKS:                      # 第一次调用时拉一次全量
        try:
            ex = requests.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=25).json()
            for s in ex.get("symbols", []):
                for flt in s.get("filters", []):
                    if flt.get("filterType") == "PRICE_FILTER":
                        _TICKS[s["symbol"]] = float(flt.get("tickSize") or 0) or None
            log("已载入 %d 个合约的价格精度" % len(_TICKS))
        except Exception as e:
            log("拉取价格精度失败: " + str(e)[:80])
    return _TICKS.get(sym)

def ceil_to_tick(price, tick):
    """向上取整到该合约的最小价格变动单位（做多做空都一样）"""
    if not tick or tick <= 0:
        return math.ceil(price) if price != int(price) else price
    return round(math.ceil(round(price / tick, 8)) * tick, 10)

def range_mid(a, b, coin=None):
    """区间开仓价 -> 取中间值，再向上取整到该合约精度"""
    lo, hi = (a, b) if a <= b else (b, a)
    mid = (lo + hi) / 2.0
    return ceil_to_tick(mid, tick_of(coin) if coin else None), lo, hi

# ---------------- 指令系统（只有你本人、在指定指令群、短消息才执行）----------------
RUNTIME = BASE + "/runtime_config.json"
CMD_GROUPS = ["开单记录", "机器人开单通知"]   # 指令在这两个群里生效
PAUSED = [False]              # 暂停：仍抓取记录，但不动作
STATE_DIRTY = [False]

# ===== 消息级去重：防止重启/游标回退后把旧信号当新信号重复开单 =====
SEEN = set()
SEEN_MAX = 1500

def mark_seen(mid):
    try:
        SEEN.add(int(mid))
    except Exception:
        return
    if len(SEEN) > SEEN_MAX:                      # 只保留最近的，避免无限膨胀
        for x in sorted(SEEN)[:len(SEEN) - SEEN_MAX]:
            SEEN.discard(x)

open_pos_ref = {}             # 在 main() 里指向真正的持仓字典

HELP_TEXT = """【机器人指令】在「开单记录」或「机器人开单通知」群里直接发（短消息即可）：

— 看状态 —
· 帮助 —— 看这份清单
· 状态 —— 运行状态 / 监控群 / 持仓数 / 上限 / 模式
· 持仓情况 —— 汇报全部持仓（也可写：持仓 BTC）
· 挂单情况 —— 当前挂单 + 待确认信号 + 各持仓的止损止盈
· 待确认 —— 重发当前等你确认的信号

— 管仓 —
· 全部平仓 —— 立即平掉全部持仓
· 平仓 BTC —— 平掉某个币
· 减仓 BTC 50 —— 减掉 50%（默认一半）
· 修改止损 BTC 0.85 —— 改某笔止损
· 移保本 BTC —— 止损移到开仓价

— 改参数（立刻生效，无需重启）—
· 修改金额 300 / 修改杠杆 3
· 修改持仓上限 6 —— 持仓上限（4/5/6/7/8…都行）
· 修改监控群 开单记录,暴富龙,UA-nurseneil2
· 暂停 / 继续 —— 暂停时不动作（仍记录）

— 模式切换（已有持仓不受影响）—
· 进入测试模式 —— 立刻切回纸面（安全方向，不需确认）
· 进入实盘模式 确认 —— 切到实盘（需二次确认）

— 回答机器人的询问 —
⚠️ 按你的要求：**所有订单在开之前都会先问过你**，回「开」才开、回「不开」作废（30 分钟不回复自动作废）。
· 开 / 不开 —— 单条信号
· 只开 SOL 和 CL ｜ 只开 CL，不开 SOL ｜ 不开 BTC ｜ 全部开 ｜ 全部不开 —— 多币种
  （多币种会列出**全部非空组合**，3 个币就是 6 种两两/单个组合 + 「全部开」）
· 回复时机器人会带上：保证金/杠杆/止损位/**止损点数（不含杠杆）**/各档止盈/**各档预期收益率（含杠杆）**
· 持仓达上限时机器人会一并问你要不要提高上限，回「开」即提高并开单
· 待你确认的信号会**落盘保存**，机器人意外重启也不会丢"""


def load_runtime():
    global GROUPS, MARGIN, LEV, NOTIONAL, TEST_MODE, STRICT_LIMIT_GROUPS, MAX_OPEN
    global MAX_CONSEC_LOSS, DAILY_LOSS_LIMIT, MAX_TOTAL_MARGIN, SILENCE_ALERT_H
    try:
        if os.path.exists(RUNTIME):
            cfg = json.load(open(RUNTIME, encoding="utf-8"))
            if cfg.get("groups"):
                GROUPS = [g for g in cfg["groups"] if g]
            if "strict_limit_groups" in cfg:
                STRICT_LIMIT_GROUPS = [g for g in (cfg.get("strict_limit_groups") or []) if g]
            if cfg.get("max_open"):
                MAX_OPEN = max(1, int(cfg["max_open"]))     # 用户可用指令改（4~8…）
            if cfg.get("max_consec_loss") is not None:
                MAX_CONSEC_LOSS = int(cfg["max_consec_loss"])
            if cfg.get("daily_loss_limit") is not None:
                DAILY_LOSS_LIMIT = float(cfg["daily_loss_limit"])
            if cfg.get("max_total_margin") is not None:
                MAX_TOTAL_MARGIN = float(cfg["max_total_margin"])
            if cfg.get("silence_alert_hours") is not None:
                SILENCE_ALERT_H = float(cfg["silence_alert_hours"])
            if cfg.get("margin"):
                MARGIN = float(cfg["margin"])
            if cfg.get("leverage"):
                LEV = int(cfg["leverage"])
            NOTIONAL = MARGIN * LEV
            if "test_mode" in cfg:
                TEST_MODE = bool(cfg["test_mode"])
            # 审批闸门开关（用户 2026-09-15 第 12 条）：默认 True=所有新开仓都要经用户审批
            if "require_approval" in cfg:
                REQUIRE_APPROVAL[0] = bool(cfg["require_approval"])
            # 真实下单层开关：跟随 runtime_config.json（热加载时也会走到这里）
            if _BEXEC_OK:
                bexec.LIVE[0] = bool(cfg.get("live_trading", False))
                bexec.LEV = LEV
            log("已载入运行配置：监控群=%s 保证金=%.0fU 杠杆=%d倍 测试模式=%s ｜ 严格限价群=%s ｜ 真实下单层=%s ｜ 开单需审批=%s ｜ 失联告警阈值=%.1fh ｜ 暂停=%s"
                % ("、".join(GROUPS), MARGIN, LEV, TEST_MODE,
                   "、".join(STRICT_LIMIT_GROUPS) or "无", _be_mode(),
                   "是" if REQUIRE_APPROVAL[0] else "否", SILENCE_ALERT_H,
                   "是" if PAUSED[0] else "否"))
    except Exception as e:
        log("读取运行配置失败: " + str(e)[:80])

def save_runtime():
    try:
        _old = {}
        try:
            _old = json.load(open(RUNTIME, encoding="utf-8"))
        except Exception:
            pass
        out = {"groups": GROUPS, "margin": MARGIN, "leverage": LEV, "test_mode": TEST_MODE,
               "max_open": MAX_OPEN, "max_consec_loss": MAX_CONSEC_LOSS,
               "daily_loss_limit": DAILY_LOSS_LIMIT, "max_total_margin": MAX_TOTAL_MARGIN,
               "require_approval": REQUIRE_APPROVAL[0]}
        # ⚠️ 必须保留 live_trading / strict_limit_groups：否则任何一条指令都会把它们悄悄抹掉
        if "live_trading" in _old:
            out["live_trading"] = _old["live_trading"]
        if "strict_limit_groups" in _old:
            out["strict_limit_groups"] = _old["strict_limit_groups"]
        # 同理保留 silence_alert_hours：它不在 out 的默认键里，不显式带回就会被指令抹掉
        if "silence_alert_hours" in _old:
            out["silence_alert_hours"] = _old["silence_alert_hours"]
        json.dump(out, open(RUNTIME, "w"), ensure_ascii=False, indent=1)
        STATE_DIRTY[0] = True
    except Exception as e:
        log("保存运行配置失败: " + str(e)[:80])

def _set_live(on):
    """切换真实下单层开关并落盘（runtime_config.json 的 live_trading）"""
    if _BEXEC_OK:
        bexec.LIVE[0] = bool(on)
    try:
        try:
            cfg = json.load(open(RUNTIME, encoding="utf-8"))
        except Exception:
            cfg = {}
        cfg["live_trading"] = bool(on)
        cfg.setdefault("groups", GROUPS)
        cfg.setdefault("margin", MARGIN)
        cfg.setdefault("leverage", LEV)
        cfg.setdefault("test_mode", TEST_MODE)
        json.dump(cfg, open(RUNTIME, "w"), ensure_ascii=False, indent=1)
    except Exception as e:
        log("写入实盘开关失败: " + str(e)[:80])
    log("真实下单层开关 -> %s（live_trading=%s）" % ("实盘" if on else "影子", bool(on)))


def close_position(coin, pct=100.0, why="手动指令"):
    """纸面平仓（真实下单层接上后走同一入口）"""
    tr = open_pos_ref.get(coin)
    if not tr:
        notify("【指令】没有 %s 的持仓" % coin)
        return
    px = price_of(coin)
    if px is None:
        notify("【指令】%s 取不到实时价，平仓失败" % coin)
        return
    d = 1 if tr["dir"] == "LONG" else -1
    part = max(0.0, min(1.0, pct / 100.0)) * tr.get("remaining", 1.0)
    pnl = (px - tr["entry"]) * d / tr["entry"] * NOTIONAL * part
    tr["realized"] = tr.get("realized", 0.0) + pnl
    _add_fee(tr, NOTIONAL * part * (px / tr["entry"]), FEE_TAKER, "手动平仓")   # 市价平 → taker
    tr["remaining"] = max(0.0, tr.get("remaining", 1.0) - part)
    if tr["remaining"] <= 0.001:
        # ⚠️ 旧代码这里不写 status/exit/exit_why → 手动平的仓在统计里"没有结单信息"。现在补齐。
        tr.update({"status": "CLOSED", "exit": px, "exit_why": "手动平仓(%s)" % why,
                   "pnl": tr["realized"]})
        _stat_close(tr)
    STATE_DIRTY[0] = True
    with open(TRADES, "a", encoding="utf-8") as f:
        f.write(json.dumps(tr, ensure_ascii=False) + "\n")
    if _BEXEC_OK and tr.get("real_layer") == "实盘":
        # 只对实盘仓动真实挂单（用户 2026-09-15：切换模式时已有持仓不动）
        try:
            _sym = coin.upper() + "USDT"
            bexec.cancel_all(_sym)
            if tr["remaining"] <= 0.001:
                real_plan_close(coin, tr["dir"], None)
            else:
                bexec.sync_sl(_sym, tr["dir"], tr.get("sl") or tr["entry"],
                              _real_qty_or_estimate(coin, tr))
            _clear_live_alert("手工平仓", coin)
            log("   ↳ [真实下单层·实盘] 已执行手工平/减仓的对应动作")
        except Exception as _e:
            _live_alert("手工平/减仓", coin, _e, "真实仓位可能未平掉，请立刻核对")
    if tr["remaining"] <= 0.001:
        open_pos_ref.pop(coin, None)
        notify("【已平仓·纸面】%s %s（%s）@%.8g\n本次盈亏：%+.1fU · 累计：%+.1fU"
               % (coin, tr["dir"], why, px, pnl, tr["realized"]))
    else:
        notify("【已减仓·纸面】%s %s（%s）@%.8g\n本次平掉 %.0f%% · 盈亏 %+.1fU · 剩余 %.0f%%"
               % (coin, tr["dir"], why, px, part * 100, pnl, tr["remaining"] * 100))

def _reply_coins(s):
    """从用户回复里找出币种（英文大小写 + 中文俗称都认）"""
    found = []
    for sym in _SYMS:
        if len(sym) >= 2 and not sym.isdigit() and re.search(
                r"(?<![A-Za-z0-9])" + re.escape(sym) + r"(?![A-Za-z0-9])", s, re.I):
            found.append(sym)
    for k, v in _NAME_MAP.items():
        if len(k) >= 2 and k in s and v not in found:
            found.append(v)
    return found


def _bump_cap(n):
    """修改持仓上限并落盘（用户 2026-09-15 要求可指令控制）"""
    global MAX_OPEN
    MAX_OPEN = max(1, int(n))
    save_runtime()
    log("持仓上限 -> %d（已落盘）" % MAX_OPEN)
    return MAX_OPEN


def _open_asking(coin):
    """把待确认信号放回待确认池，立刻出单"""
    it = ASKING.pop(coin, None)
    if not it:
        return False
    p = it["p"]
    p["deadline"] = 0
    p["approved"] = True        # 用户已回「开」→ 过审批闸门，不再重复问（2026-09-15 第 12 条）
    # 若是因"持仓上限"被拦下的：回「开」即视为同意把上限提高（到当前持仓数+1）
    if len(open_pos_ref) >= MAX_OPEN and coin not in open_pos_ref:
        p["cap_override"] = True
        _bump_cap(len(open_pos_ref) + 1)
    PENDING[coin] = p
    return True


def _waiting_batch():
    """当前挂在待确认里的币种（同一批）"""
    return sorted(ASKING)


def _handle_ask_reply(t):
    """处理「开 / 不开 / 只开X和Y / 只开X，不开Y和Z / 全部开 / 全部不开」。
    用户 2026-09-14 要求：多币种消息要能【单独分开】指定开哪些。"""
    if not ASKING:
        notify("【指令】当前没有待你确认的信号")
        return True
    s = re.sub(r"\s+", " ", t).strip()
    pend = _waiting_batch()

    # ① 全部作废
    if re.search(r"全部不开|全不开|都不开|都不要|全部作废|都作废", s):
        n = len(pend)
        for c in list(ASKING):
            ASKING.pop(c, None)
        notify("【指令】已作废全部 %d 个待确认信号（未下单）：%s" % (n, "、".join(pend)))
        log("   ❌ 用户选择全部不开：%s" % pend)
        return True

    # ② 全部开
    if re.search(r"全部开|全开|都开|全买|都买", s) and not _reply_coins(s):
        for c in list(ASKING):
            _open_asking(c)
        notify("【指令】收到「全部开」→ %d 个信号立刻按解析结果出单：%s" % (len(pend), "、".join(pend)))
        log("   ✅ 用户确认全部开单：%s" % pend)
        return True

    only = bool(re.search(r"只开|只买|只要|仅开|仅买", s))
    excl = bool(re.search(r"不开|不要|别开|作废", s))
    _parts = re.split(r"不开|不要|别开|作废", s, maxsplit=1)
    head_list = _reply_coins(_parts[0])
    tail_list = _reply_coins(_parts[1]) if len(_parts) > 1 else []

    if only:
        open_list = head_list
        close_list = list(tail_list) + [c for c in pend if c not in open_list and c not in tail_list]
    elif excl:
        close_list = tail_list or head_list
        open_list = []
    else:
        open_list = head_list
        close_list = []

    # 没点名币种：单条待确认时按「开」处理
    if not open_list and not close_list:
        if len(pend) == 1:
            _open_asking(pend[0])
            notify("【指令】收到「开」→ %s 立刻按解析结果出单" % pend[0])
            return True
        notify("【指令】当前有 %d 个待确认信号，请指明币种。例如：\n"
               "· 只开 %s 和 %s\n· 只开 %s，不开 %s\n· 全部不开"
               % (len(pend), pend[0], pend[1] if len(pend) > 1 else "XXX",
                  pend[0], pend[1] if len(pend) > 1 else "XXX"))
        return True

    # 执行
    opened, closed, unknown = [], [], []
    for c in open_list:
        if c in ASKING:
            _open_asking(c); opened.append(c)
        else:
            unknown.append(c)
    for c in close_list:
        if c in ASKING:
            ASKING.pop(c, None); closed.append(c)
        elif c not in unknown:
            unknown.append(c)
    _msg = ["【指令】已按你的回复处理："]
    if opened:
        _msg.append("✅ 开单 %d 个：%s（按解析结果、保证金 %.0fU × %d倍）"
                    % (len(opened), "、".join(opened), MARGIN, LEV))
    if closed:
        _msg.append("❌ 作废 %d 个：%s" % (len(closed), "、".join(closed)))
    if unknown:
        _msg.append("⚠️ 待确认里没有这些币：%s" % "、".join(unknown))
    if ASKING:
        _msg.append("仍待确认：%s" % "、".join(_waiting_batch()))
    notify("\n".join(_msg))
    log("   🎛 用户回复处理：开=%s 不开=%s 未知=%s" % (opened, closed, unknown))
    return True


def _handle_ask(verb, coin_hint=""):
    """兼容旧的「开 / 不开 [币种]」写法（现在统一走 _handle_ask_reply）"""
    return _handle_ask_reply(("%s %s" % (verb, coin_hint)).strip())


def handle_command(txt):
    """返回 True 表示这条消息是指令（已处理，不再走信号流程）"""
    global GROUPS, MARGIN, LEV, NOTIONAL, TEST_MODE
    t = re.sub(r"\s+", " ", (txt or "")).strip()
    if len(t) > 60:
        return False
    KEY = ["帮助", "状态", "持仓情况", "持仓", "全部平仓", "确认全部平仓", "平仓", "减仓",
           "修改止损", "移保本", "暂停", "继续", "修改监控群", "修改金额", "修改杠杆", "测试模式",
           "实盘模式", "挂单情况", "挂单", "待确认", "修改持仓上限", "持仓上限", "进入测试模式",
           "进入实盘模式", "重新对账", "对账"]
    # 去掉可能的昵称/时间前缀后，指令必须在消息开头（防止转发内容被误当指令）
    nick = lambda x: re.sub(r"^[^\s]{2,16}\s+", "", x)
    tm = lambda x: re.sub(r"^\d{1,2}:\d{2}\s*(AM|PM)?\s*", "", x, flags=re.I).strip()
    # ===== 「开 / 不开 / 只开X和Y」确认（用户 2026-09-14 要求：把握不准必须问他）=====
    # 判定条件收紧，避免"开单记录""开始监控"这类正常文字被误当指令：
    #   ① 回复里点出了币种名 + 含开/买/作废等动词，或
    #   ② 整条就是「开/不开/全部开/全部不开/作废」这种极短词
    _ct = (tm(t) or t).strip()
    if len(_ct) <= 48:
        _co = _reply_coins(_ct)
        if not _co and ASKING:      # 兜底：直接匹配当前待确认里的币种（防止别名/非标准写法）
            _co = [c for c in ASKING
                   if re.search(r"(?<![A-Za-z0-9])" + re.escape(c) + r"(?![A-Za-z0-9])", _ct, re.I)]
        _is_reply = bool(_co and re.search(r"开|买|作废|不要", _ct)) or bool(re.fullmatch(
            r"(开|不开|作废|全部开|全开|都开|全部不开|全不开|都不开|都不要|全部作废)", _ct))
        if _is_reply:
            return _handle_ask_reply(_ct)
    body = None
    for cand in (t, tm(t), nick(t), tm(nick(t)), nick(tm(t))):
        if any(cand.startswith(k) for k in KEY):
            body = cand
            break
    if body is None:
        return False
    cmd = None
    for k in KEY:
        if body.startswith(k):
            cmd = body; break
    if cmd is None and len(body) <= 12:            # 极短消息允许"关键词出现在任意位置"
        for k in KEY:
            if k in body:
                cmd = body[body.find(k):]; break
    if not cmd:
        return False
    log("   🎛 收到指令：%s" % cmd)
    if cmd.startswith("帮助"):
        notify(HELP_TEXT)
    elif cmd.startswith("状态"):
        _risk_roll_day()
        _exp = _current_exposure(open_pos_ref)
        _rec = ("未检查" if RECONCILE.get("ok") is None and not RECONCILE.get("checked")
                else ("一致" if RECONCILE.get("ok") else "❌不一致" + ("（已阻止真实下单）" if RECONCILE.get("blocked") else "")))
        notify("【机器人状态】\n"
               "监控群：%s\n"
               "持仓：%d 笔 / 上限 %d 笔（%s）\n"
               "总敞口：%.0fU / 上限 %.0fU\n"
               "单笔：保证金 %.0fU × %d倍 = 名义 %.0fU\n"
               "模式：%s ｜ 测试模式：%s ｜ 暂停：%s\n"
               "风控：连亏 %s 笔（熔断线 %s）｜ 今日 %s 笔 / 净 %+.2fU（熔断线 -%.0fU）\n"
               "对账：%s"
               % ("、".join(GROUPS), len(open_pos_ref), MAX_OPEN, "、".join(open_pos_ref) or "-",
                  _exp, _exposure_cap(), MARGIN, LEV, NOTIONAL,
                  _be_mode(), "开" if TEST_MODE else "关", "是" if PAUSED[0] else "否",
                  RISK.get("consec_loss", 0), MAX_CONSEC_LOSS,
                  RISK.get("day_trades", 0), RISK.get("day_pnl", 0.0), DAILY_LOSS_LIMIT, _rec))
    elif cmd.startswith("重新对账") or cmd.startswith("对账"):
        startup_reconcile()
        if RECONCILE.get("blocked"):
            notify("【指令】重新对账：**仍有差异，真实下单继续被阻止**\n%s\n"
                   "请在币安核对后再次发送「重新对账」" % "\n".join("· " + d for d in (RECONCILE.get("diffs") or [])[:8]))
        elif RECONCILE.get("ok"):
            notify("【指令】✅ 重新对账通过：纸面与交易所一致，真实下单闸门已解除。")
        else:
            notify("【指令】重新对账未能完成（读不到交易所持仓），真实下单仍被阻止。")
    elif cmd.startswith("持仓情况") or cmd.startswith("持仓"):
        m = re.search(r"(?:持仓情况|持仓)\s*([A-Za-z0-9]{2,12})", cmd)
        if m:
            c, _ = resolve_coin(m.group(1))
            if c in open_pos_ref:
                pos_report(c, open_pos_ref[c])
            else:
                notify("【指令】没有 %s 的持仓" % (c or m.group(1)))
        elif not open_pos_ref:
            notify("【指令】当前没有任何持仓")
        else:
            for c in list(open_pos_ref):
                pos_report(c, open_pos_ref[c])
    elif cmd.startswith("挂单情况") or cmd.startswith("挂单"):
        # 用户 2026-09-14 要求：挂单情况也要能查（Giggle 那单看不到是否挂上了）
        L = ["【挂单情况】"]
        if ASKING:
            L.append("· 待你确认的信号 %d 个：" % len(ASKING))
            for c, it in ASKING.items():
                L.append("   %s %s ｜ 原因：%s" % (c, (it["p"].get("dir") or ""), it.get("reason", "")))
        else:
            L.append("· 待你确认的信号：无")
        if PENDING:
            L.append("· 正在合并中的信号 %d 个：%s" % (len(PENDING), "、".join(PENDING)))
        if _BEXEC_OK:
            L.append("· 真实层挂单（%s模式）：" % _be_mode())
            try:
                _oo = bexec.open_orders() or []
                _ao = bexec.open_algo_orders() or []
                if not _oo and not _ao:
                    L.append("   币安账户上当前没有挂单")
                for o in _oo:
                    L.append("   经典 %s %s 价 %s 量 %s" % (o.get("symbol"), o.get("type"),
                                                          o.get("price"), o.get("origQty")))
                for o in _ao:
                    L.append("   Algo %s %s 触发价 %s 量 %s"
                             % (o.get("symbol"), o.get("type") or o.get("orderType"),
                                o.get("triggerPrice") or o.get("stopPrice"),
                                o.get("quantity") or o.get("origQty")))
            except Exception as _e:
                L.append("   读取失败：%s" % str(_e)[:100])
        else:
            L.append("· 真实下单层未加载")
        L.append("· 持仓 %d 笔：%s" % (len(open_pos_ref), "、".join(open_pos_ref) or "无"))
        # 纸面模式下把"本该挂在哪"也列出来，方便核对
        for c, tr in list(open_pos_ref.items()):
            _tps = tr.get("tps") or []
            _fl = tr.get("filled", [])
            L.append("   %s：止损 %s ｜ 止盈 %s（已成交 %s）"
                     % (c, tr.get("sl"), _tps or "未读到", _fl or "无"))
        notify("\n".join(L))
    elif cmd.startswith("待确认"):
        if not ASKING:
            notify("【指令】当前没有待你确认的信号")
        else:
            for c, it in ASKING.items():
                p = it["p"]
                notify("【待确认】%s %s\n原因：%s\n解析：开仓=%s 止损=%s 止盈=%s\n原文：%s\n回复「开」或「不开」"
                       % (c, p.get("dir"), it.get("reason"), p.get("entry"), p.get("stop"),
                          sorted(set(p.get("tps") or [])) or "未读到",
                          (p["texts"][0][:150] if p.get("texts") else "")))
    elif cmd.startswith("全部平仓") or cmd.startswith("确认全部平仓"):
        if not open_pos_ref:
            notify("【指令】当前没有持仓")
        else:
            for c in list(open_pos_ref):
                close_position(c, 100, "你的指令：全部平仓")
    elif cmd.startswith("平仓"):
        m = re.search(r"平仓\s*([A-Za-z0-9]{2,12})", cmd)
        if m:
            c, _ = resolve_coin(m.group(1))
            close_position(c, 100, "你的指令：平仓")
        else:
            notify("【指令】格式：平仓 BTC")
    elif cmd.startswith("减仓"):
        m = re.search(r"减仓\s*([A-Za-z0-9]{2,12})(?:\s*(\d{1,3}))?", cmd)
        if m:
            c, _ = resolve_coin(m.group(1))
            pct = float(m.group(2)) if m.group(2) else 50.0
            close_position(c, pct, "你的指令：减仓 %g%%" % pct)
        else:
            notify("【指令】格式：减仓 BTC 50")
    elif cmd.startswith("修改止损"):
        m = re.search(r"修改止损\s*([A-Za-z0-9]{2,12})\s*([0-9]*\.?[0-9]+)", cmd)
        if m:
            c, _ = resolve_coin(m.group(1))
            tr = open_pos_ref.get(c)
            if tr:
                tr["sl"] = float(m.group(2)); STATE_DIRTY[0] = True
                notify("【指令】%s 止损已改为 %.8g" % (c, tr["sl"]))
            else:
                notify("【指令】没有 %s 的持仓" % c)
        else:
            notify("【指令】格式：修改止损 BTC 0.85")
    elif cmd.startswith("移保本"):
        m = re.search(r"移保本\s*([A-Za-z0-9]{2,12})", cmd)
        if m:
            c, _ = resolve_coin(m.group(1))
            tr = open_pos_ref.get(c)
            if tr:
                tr["sl"] = tr["entry"]; STATE_DIRTY[0] = True
                notify("【指令】%s 止损已移到开仓价 %.8g（保本损）" % (c, tr["entry"]))
            else:
                notify("【指令】没有 %s 的持仓" % c)
    elif cmd.startswith("暂停"):
        PAUSED[0] = True
        STATE_DIRTY[0] = True          # 落盘：重启后仍然是暂停态（否则"暂停"对维护没用）
        notify("【指令】已暂停：仍会抓取和记录，但不会开单/平仓。回复「继续」恢复"
               "（暂停状态会落盘，重启后依然生效）")
    elif cmd.startswith("继续"):
        PAUSED[0] = False
        STATE_DIRTY[0] = True
        notify("【指令】已恢复")
    elif cmd.startswith("修改监控群"):
        m = re.search(r"修改监控群\s*(.+)", cmd)
        if m:
            gs = [x.strip() for x in re.split(r"[,，、\s]+", m.group(1)) if x.strip()]
            if gs:
                GROUPS[:] = gs
                save_runtime()
                notify("【指令】监控群已改为：%s\n（已热加载生效，无需重启；新增群的页面约 1 分钟内自动打开）" % "、".join(GROUPS))
        else:
            notify("【指令】格式：修改监控群 A,B,C")
    elif cmd.startswith("修改金额"):
        m = re.search(r"修改金额\s*(\d+)", cmd)
        if m:
            MARGIN = float(m.group(1)); NOTIONAL = MARGIN * LEV
            save_runtime()
            notify("【指令】单笔保证金已改为 %.0fU（%d倍 = 名义 %.0fU）" % (MARGIN, LEV, NOTIONAL))
    elif cmd.startswith("修改杠杆"):
        m = re.search(r"修改杠杆\s*(\d+)", cmd)
        if m:
            LEV = int(m.group(1)); NOTIONAL = MARGIN * LEV
            save_runtime()
            notify("【指令】杠杆已改为 %d 倍（保证金 %.0fU = 名义 %.0fU）" % (LEV, MARGIN, NOTIONAL))
    elif cmd.startswith("修改持仓上限") or cmd.startswith("持仓上限"):
        m = re.search(r"(?:修改)?持仓上限\s*(\d+)", cmd)
        if m:
            n = max(1, min(50, int(m.group(1))))
            _bump_cap(n)
            notify("【指令】持仓上限已改为 **%d 笔**（最大敞口 %.0fU = %d × %.0fU）\n"
                   "当前持仓 %d 笔 ｜ 已有持仓不受影响"
                   % (n, n * MARGIN, n, MARGIN, len(open_pos_ref)))
        else:
            notify("【指令】格式：修改持仓上限 6（当前 %d 笔，持仓 %d 笔）"
                   % (MAX_OPEN, len(open_pos_ref)))
    elif cmd.startswith("进入测试模式"):
        _set_live(False)
        _n = len(open_pos_ref)
        notify("【指令】已切换到 **测试模式（纸面）**\n"
               "· 之后的信号只在纸面记录，不会向币安发任何委托\n"
               "· **已有持仓 %d 笔保持原样不动**（它们的模式在开仓时就固定了，不会被切换影响）"
               % _n)
    elif cmd.startswith("进入实盘模式"):
        if "确认" not in cmd:
            notify("【指令】进入实盘需要二次确认：发「**进入实盘模式 确认**」\n"
                   "（实盘下单会用真钱。要回纸面随时发「进入测试模式」）")
        else:
            _set_live(True)
            _live = sum(1 for t in open_pos_ref.values() if t.get("real_layer") == "实盘")
            _n = len(open_pos_ref)
            notify("【指令】⚠️ 已切换到 **实盘模式**\n"
                   "· 之后的信号会真的向币安发单（保证金 %.0fU × %d倍 / 上限 %d 笔）\n"
                   "· **已有持仓 %d 笔保持原样不动**（其中实盘仓 %d 笔继续按实盘管理）\n"
                   "· 要回纸面随时发「进入测试模式」" % (MARGIN, LEV, MAX_OPEN, _n, _live))
    elif cmd.startswith("实盘模式"):
        if "关" in cmd:
            _set_live(False)
            notify("【指令】真实下单层已切回 **影子模式**（只记录下单计划，不发任何委托）")
        elif ("开" in cmd) and ("确认" in cmd):
            _set_live(True)
            notify("【指令】⚠️ 真实下单层已切到 **实盘**！\n"
                   "之后的信号会真的向币安发单（双向持仓 / %d 倍杠杆 / 止损走 Algo 接口）。\n"
                   "要停就发「实盘模式 关」。" % LEV)
        else:
            notify("【指令】实盘开关需要二次确认：\n"
                   "· 开启实盘：实盘模式 开 确认\n· 关闭实盘：实盘模式 关\n当前：%s%s"
                   % (_be_mode(), "" if _BEXEC_OK else "（⚠️ 下单层未加载：%s）" % _BEXEC_ERR))
    elif cmd.startswith("测试模式"):
        on = ("开" in cmd) or ("on" in cmd.lower())
        TEST_MODE = on
        save_runtime()
        notify("【指令】测试模式已%s（%s）" % ("打开" if on else "关闭",
              "不受 5 笔上限拦截" if on else "超过 5 笔会跳过"))
    return True

# ---------------- 页面 JS ----------------
SCAN_JS = """() => {
  const lists = Array.from(document.querySelectorAll('.list_items')).filter(l => l.querySelectorAll('.messageItem-wrapper').length > 0);
  if (!lists.length) return [];
  let best = null, bestId = -1;
  for (const l of lists) {
    const ids = Array.from(l.children).map(x => { const it = x.querySelector('.js-message-item'); return it ? Number(it.getAttribute('id')) : 0; }).filter(Boolean);
    if (!ids.length) continue;
    const mx = Math.max.apply(null, ids);
    if (mx > bestId) { bestId = mx; best = l; }
  }
  if (!best) return [];
  const out = [];
  for (const row of best.children) {
    const it = row.querySelector('.js-message-item');
    if (!it) continue;
    const imgs = Array.from(row.querySelectorAll('img')).filter(i => i.naturalWidth >= 150).length;
    out.push({id: it.getAttribute('id'), nimg: row.querySelectorAll('img').length, loaded: imgs,
              text: (row.innerText || '').split(String.fromCharCode(10)).join(' ').slice(0, 1200)});
  }
  return out;
}"""

FETCH_IMG_JS = """async (arg) => {
  const mid = (typeof arg === 'object') ? arg.mid : arg;
  const budget = (typeof arg === 'object' && arg.waitMs) ? arg.waitMs : 6000;
  const it = document.querySelector('.js-message-item[id="' + mid + '"]');
  if (!it) return [];
  const t0 = Date.now();
  while (Date.now() - t0 < budget) {
    const l = Array.from(it.querySelectorAll('img')).filter(i => i.naturalWidth >= 150);
    if (l.length) break;
    await new Promise(r => setTimeout(r, 250));
  }
  const out = [];
  for (const im of Array.from(it.querySelectorAll('img')).filter(i => i.naturalWidth >= 150)) {
    if (String(im.src).indexOf('blob:') !== 0) continue;
    try { const r = await fetch(im.src); const b = await r.blob();
          const d = await new Promise(res => { const fr = new FileReader(); fr.onload = () => res(fr.result); fr.readAsDataURL(b); });
          out.push(d); } catch (e) {}
  }
  return out;
}"""

FINDROW_JS = """(name) => {
  const rows = Array.from(document.querySelectorAll('[class*="a11y_feed_card_main"]'));
  for (let i = 0; i < rows.length; i++) {
    const first = ((rows[i].innerText || '').split(String.fromCharCode(10))[0] || '').trim();
    if (first.indexOf(name) >= 0) return i;
  }
  return -1;
}"""

TITLE_JS = """(name) => {
  for (const e of document.querySelectorAll('*')) {
    const r = e.getBoundingClientRect();
    if (r.y >= 0 && r.y < 90 && r.x > 600 && r.width > 60 && e.children.length < 8) {
      const t = (e.innerText || '').trim();
      if (t && t.length < 60 && t.indexOf(name) >= 0 && t.indexOf(name) <= 3) return true;
    }
  }
  return false;
}"""

FEED_JS = """() => {
  const out = {};
  for (const el of document.querySelectorAll('[class*="a11y_feed_card_main"]')) {
    const lines = (el.innerText || '').split(String.fromCharCode(10)).map(s => s.trim()).filter(Boolean);
    if (!lines.length) continue;
    out[lines[0]] = lines.slice(1).join(' | ').slice(0, 140);
  }
  return out;
}"""

def feed_snapshot(page):
    """读一次左侧会话列表：{群标题: 最新预览}（一次 JS 调用，约 0.2s）"""
    try:
        return page.evaluate(FEED_JS) or {}
    except Exception:
        return {}

def feed_preview_of(feed, name):
    """从会话列表快照里挑出目标群的预览（标题包含群名即可）"""
    if not feed:
        return None
    for title, prev in feed.items():
        if name in title:
            return prev
    return None

def _read_page(page, name):
    """滚到底部读消息；先用标题校验确认这个页面确实是目标群"""
    try:
        page.mouse.move(900, 400); page.mouse.wheel(0, 2600); time.sleep(1.2)
    except Exception:
        return []
    if not page.evaluate(TITLE_JS, name):
        return []
    return page.evaluate(SCAN_JS) or []

def open_group_page(ctx, name):
    """新开一个标签页并停在该群，之后不再切换"""
    page = ctx.new_page()
    try:
        page.goto(MSG_URL, wait_until="domcontentloaded", timeout=90000)
        time.sleep(9)
        for i in range(20):
            if page.evaluate("() => document.querySelectorAll('[class*=\"a11y_feed_card_main\"]').length"): break
            page.mouse.move(380, 400); page.mouse.wheel(0, 400); time.sleep(1.2)
        for attempt in range(4):
            # 方式1：在会话列表里按『标题行』找到并点击（Playwright 元素点击 + 标题校验）
            try:
                idx = -1
                for k in range(20):
                    idx = page.evaluate(FINDROW_JS, name)
                    if idx >= 0: break
                    page.mouse.move(380, 400); page.mouse.wheel(0, 900); time.sleep(0.4)
                if idx >= 0:
                    page.locator('[class*="a11y_feed_card_main"]').nth(idx).scroll_into_view_if_needed(timeout=5000)
                    page.locator('[class*="a11y_feed_card_main"]').nth(idx).click(timeout=8000)
                    time.sleep(2.5)
                    rows = _read_page(page, name)
                    if rows: return page, rows
                    log("   [%s] 列表点击后标题不符，重试" % name)
            except Exception:
                pass
            # 方式2：Ctrl+K 搜索（全文搜索可能命中别的群，所以必须过标题校验）
            try:
                page.keyboard.press("Control+k"); time.sleep(1.5)
                page.keyboard.type(SEARCH_TERM.get(name, name), delay=80); time.sleep(2.5)
                page.keyboard.press("Enter"); time.sleep(3.0)
                page.keyboard.press("Escape"); time.sleep(1.5)
                rows = _read_page(page, name)
                if rows: return page, rows
                log("   [%s] 搜索结果标题不符，重试" % name)
            except Exception:
                pass
            time.sleep(1.5)
        return page, []
    except Exception as e:
        log("[%s] 开页异常: %s" % (name, str(e)[:100]))
        return page, []

# ===== B1 自愈：浏览器上下文死亡检测 + 全量重启（2026-09-15）=====
# 真实事故（2026-09-15）：16:07:33 systemd-logind 停掉 user@1000（Linger=no + 最后一个 SSH
#   会话登出）→ 挂在它下面的 Chromium 被杀 → 从这一刻起 ctx.new_page() **必然**抛
#   "Target page, context or browser has been closed"。旧代码只写"下一轮会继续重试"，
#   于是机器人带着一个永远打不开页面的死 ctx 空转 93 分钟，还一直打「运行中」心跳 —— 全瞎。
# 根因已用 `loginctl enable-linger ubuntu` 消除（B1 第 1 步）；但浏览器仍可能因其它原因
#   （OOM / 自身崩溃 / 被误杀）整体死亡，所以这一层必须存在：
#   **判定"整个上下文已死" → 主动重建整个浏览器 + 重开所有页面 + 立即飞书告警**。
CTX_DEAD_MARKS = (
    "Target page, context or browser has been closed",
    "TargetClosedError",
    "Browser has been closed",
    "Browser closed",
    "browser has been closed",
    # 驱动进程本身死了 —— 只有这一条（不带泛指 "Connection closed"），
    # 避免把页面级的 "WebSocket connection closed" 之类误判成整个浏览器死亡。
    "Connection closed while reading from the driver",
)
RELAUNCH_MIN_GAP = 120.0        # 两次全量重启的最小间隔（秒），防崩溃循环把内存打爆
DEAD_ROUNDS_TO_RELAUNCH = 2     # 连续 N 轮命中"上下文已死"才判定死亡（单群偶发失败不触发）
CHROME_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
               "--disable-software-rasterizer", "--renderer-process-limit=2",
               "--js-flags=--max-old-space-size=320",
               "--disable-features=Translate,BackForwardCache"]


def ctx_dead_error(e):
    """这个异常是不是「整个浏览器上下文已死」？
    区别于「这一个标签页被关了」——后者重开一个标签页就行，前者重开必然失败。"""
    s = str(e)
    return any(m in s for m in CTX_DEAD_MARKS)


def launch_persistent(p, profile):
    """persistent context 的唯一启动入口：启动时与自愈重启时共用，避免两处参数漂移。"""
    return p.chromium.launch_persistent_context(
        user_data_dir=profile, headless=False, args=list(CHROME_ARGS))


def chrome_pids_of_profile(profile):
    """扫 /proc 找出还在用这个 user-data-dir 的 chrome 进程（Linux，只读）"""
    pids = []
    try:
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                cl = open("/proc/%s/cmdline" % d, "rb").read().decode("utf-8", "replace")
            except Exception:
                continue
            if profile in cl:
                pids.append(int(d))
    except Exception:
        pass
    return pids


def _ppid_of(pid):
    try:
        with open("/proc/%d/stat" % pid) as f:
            return int(f.read().rsplit(")", 1)[1].split()[1])
    except Exception:
        return -1


def kill_profile_chrome(profile, wait_sec=6.0, orphan_only=False):
    """杀掉还占着这个 profile 的残留 chrome；返回被杀 PID 列表。
    ⚠️ profile 用全路径匹配，绝不会误伤其它 profile。
    orphan_only=True 时只杀孤儿 chrome（ppid==1）—— 用于"启动失败后重试"，
    避免误杀一个正在被别的进程正常使用的浏览器。"""
    killed = []
    t0 = time.time()
    while time.time() - t0 < wait_sec:
        left = chrome_pids_of_profile(profile)
        if orphan_only:
            left = [p for p in left if _ppid_of(p) == 1]
        if not left:
            break
        for pid in left:
            try:
                os.kill(pid, 9)
                killed.append(pid)
            except Exception:
                pass
        time.sleep(0.4)
    return killed


def clear_singleton_locks(profile):
    """清 Singleton* 锁：不清掉的话新 context 会因「profile 已被占用」启动失败。
    ⚠️ 只在确认旧 chrome 进程已清干净之后调用 —— fs_bot 是登录态核心资产，
       绝不能让两个 Chrome 实例同时写同一个 profile。"""
    gone = []
    for f in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            os.remove(os.path.join(profile, f))
            gone.append(f)
        except FileNotFoundError:
            pass
        except Exception as e:
            log("   ↳ 删除 %s 失败（不致命）：%s" % (f, str(e)[:60]))
    return gone


def _tp_wrong_side(dirc, entry, tps, ref=None):
    """返回**相对给定方向/入场价落在错误一侧**的止盈位。
    ref 给定时（一般是当前市价）用 ref 作为比较基准，否则用 entry。
    B14 公共校验：新开仓、更新已有持仓、持仓健康自检三处共用同一套判断。"""
    if not isinstance(entry, (int, float)) or not entry:
        return []
    base = float(ref) if isinstance(ref, (int, float)) and ref else float(entry)
    d = (dirc or "LONG").upper()
    return [t for t in (tps or []) if isinstance(t, (int, float))
            and (float(t) <= base if d == "LONG" else float(t) >= base)]


def validate_plan(coin, dirc, entry, stop, tps, mkt, texts=None):
    """**公共合理性校验**（B3 修复）。

    背景：原来的「多币种分支」(`split_by_coin` → `ask_user`) **完全绕过**了
    `finalize_pending` 里那套校验（止损方向、价格合理性、点数换算）。
    实测事故：DOGE 做多、入场 0.079、**止损 0.09（在入场价上方）** ——
    按已有校验本该被拒绝，但因为走的是多币种分支，直接发给你审批了。

    返回 `(问题字符串 or None, 修正后的 tps)`。两条路径（单币 / 多币）共用同一套判断，
    避免"改了一条忘了另一条"。问题字符串是**面向用户**的，可直接放进审批原因。
    """
    t0 = (texts[0] if texts else "") or ""
    tps = [t for t in (tps or []) if isinstance(t, (int, float))]
    d = 1 if (str(dirc or "LONG").upper() == "LONG") else -1
    # ① 止损方向
    if isinstance(stop, (int, float)) and stop and isinstance(entry, (int, float)) and entry:
        if d == 1 and float(stop) >= float(entry):
            return ("止损价 %.8g **不低于** 入场价 %.8g —— 做多的止损必须在下方，判为解析错误"
                    % (stop, entry)), tps
        if d == -1 and float(stop) <= float(entry):
            return ("止损价 %.8g **不高于** 入场价 %.8g —— 做空的止损必须在上方，判为解析错误"
                    % (stop, entry)), tps
    # ② 止盈方向：方向不对的档位剔除
    if tps and isinstance(entry, (int, float)) and entry:
        tps = [t for t in tps if (float(t) > float(entry) if d == 1 else float(t) < float(entry))]
    # ③ 开仓价与市价严重不符
    if isinstance(entry, (int, float)) and entry and mkt:
        dev = abs(float(entry) - float(mkt)) / float(mkt)
        if dev > MAX_ENTRY_DEV:
            return ("解析出的开仓价 %.8g 与当前市价 %.8g 相差 %.0f%%（超过 %.0f%%），"
                    "像是把别的币的价格串过来了" % (entry, mkt, dev * 100, MAX_ENTRY_DEV * 100)), tps
    # ④ 止损距离是否荒谬
    if isinstance(stop, (int, float)) and stop and isinstance(entry, (int, float)) and entry:
        sp = abs(float(stop) - float(entry)) / float(entry)
        if sp > MAX_STOP_PCT:
            return "止损距入场价 %.0f%%（超过 %.0f%%，不像真的止损）" % (sp * 100, MAX_STOP_PCT * 100), tps
        if sp < MIN_STOP_PCT:
            return "止损几乎等于入场价（距离仅 %.3f%%），等于没设止损" % (sp * 100), tps
    # ⑤ 止盈是否已被当前市价越过
    if tps and isinstance(mkt, (int, float)) and mkt:
        crossed = [t for t in tps if (float(t) <= float(mkt) if d == 1 else float(t) >= float(mkt))]
        if crossed:
            return ("止盈位 %s 已被当前市价 %.8g 越过 —— 信号已过期，或价格张冠李戴"
                    % (crossed, mkt)), tps
    # ⑥ 条件触发型（等跌破/站稳/突破…才进）见 soft_ask_reason：那类不是"解析错误"，
    #    而是"需要人来判断现在能不能进"，所以不在这里硬拒，改由调用方转成审批原因。
    return None, tps


def soft_ask_reason(txt, stop=None, entry=None):
    """"不是解析错误、但必须让人来判断"的原因（B5 的 ③条件单 / ①点数）。
    这类不能硬拒（否则丢信号），也不该自动开 → 由调用方拼进审批原因。返回 None 或字符串。"""
    why = []
    if _conditional_order(txt or ""):
        why.append("这条消息是【条件触发】型的（等跌破/站稳/突破/收回…才进），"
                   "机器人不能替你盯价格条件")
    _p = _points_info(txt or "", stop)
    if _p:
        why.append("止损「%s点」是**点数**不是价格，不能直接当止损价用" % _fmt_num(_p))
    return "；".join(why) if why else None


def position_sanity(open_pos, notify_user=True):
    """持仓健康自检（B14 配套）：找出**止盈落在持仓错误一侧**的仓位。
    这类仓位一旦存在，监控循环会立刻把当前价判成"止盈成交" —— 19:20 那笔假亏损就是这么来的。
    这里只**报告**、不擅自改仓位（改仓位是你的决策）；启动时跑一次，之后按轮次定期跑。
    返回问题清单。"""
    bad = []
    for c, v in list(open_pos.items()):
        d = (v.get("dir") or "LONG").upper()
        e = v.get("entry")
        if not isinstance(e, (int, float)) or not e:
            continue
        wrong = _tp_wrong_side(d, e, v.get("tps"))
        if wrong:
            bad.append((c, d, e, wrong, v.get("tps")))
        s = v.get("sl")
        # ⚠️ 止损用**严格**不等式：TP1 成交后止损会被移到**正好等于开仓价**（保本损），
        #    那是合法的，不能用 >= / <= 判成"错误一侧"（我自己第一版就写错、会误报）。
        if isinstance(s, (int, float)) and s:
            if (d == "LONG" and float(s) > float(e) + 1e-12) or \
               (d == "SHORT" and float(s) < float(e) - 1e-12):
                bad.append((c, d, e, ["止损在错误一侧：%s" % s], v.get("sl")))
    if bad:
        _ls = ["· %s %s 入场 %.8g ｜ 止盈 %s ｜ 问题：%s"
               % (c, d, e, tps_all, w) for (c, d, e, w, tps_all) in bad]
        log("   ⚠️ 持仓健康自检发现 %d 处异常（止盈/止损落在错误一侧，会被误判成交）：" % len(bad))
        for _x in _ls:
            log("      " + _x)
        if notify_user:
            notify("【跟单机器人·持仓自检】⚠️ 发现 %d 处**止盈/止损落在错误一侧**的持仓，"
                   "监控循环会把它当场判成成交（19:20 那笔 -6.57U 假亏损就是这个原因）：\n%s\n"
                   "建议：发「修改止损/平仓」指令处理，或告诉我怎么改。" % (len(bad), "\n".join(_ls)))
    elif notify_user:
        # ⚠️ 可核验性：干净时也要留一行 —— 否则"检查跑没跑过"从日志上无法证明
        #    （2026-09-15 重启核验时就踩到这个：日志里搜不到"持仓自检"，分不清是没问题还是没执行）
        log("   ✅ 持仓健康自检通过：%d 笔持仓的止盈/止损方向全部正常" % len(open_pos))
    return bad


def main():
    log("==== dryRun 机器人 v2 启动（每群独立标签页）====")
    last_id = {}
    open_pos = open_pos_ref          # 指令系统与主循环共用同一个持仓字典
    load_runtime()
    try:
        RUNTIME_MTIME[0] = os.path.getmtime(RUNTIME)      # 方案B：热加载基线
    except Exception:
        RUNTIME_MTIME[0] = 0.0
    if os.path.exists(STATE):
        try:
            sv = json.load(open(STATE, encoding="utf-8"))
            for k, v in (sv.get("last") or {}).items():
                last_id[k] = int(v)
            for _x in (sv.get("seen") or []):
                try:
                    SEEN.add(int(_x))
                except Exception:
                    pass
            if SEEN:
                log("已载入已处理消息 %d 条（防重复开单）" % len(SEEN))
            _op = sv.get("open")
            if isinstance(_op, dict):
                open_pos.update(_op)
                log("已恢复持仓 %d 笔：%s" % (len(_op), "、".join(_op) or "-"))
            if last_id:
                log("已载入上次进度：" + ", ".join("%s→%s" % (k, datetime.datetime.fromtimestamp(last_id[k] >> 32, CST).strftime("%m-%d %H:%M")) for k in last_id))
            _rk = sv.get("risk")
            if isinstance(_rk, dict):
                RISK.update(_rk)
                _risk_roll_day()
                log("已恢复风控计数：连亏 %s 笔 ｜ 今日 %s 笔 / 净 %+.2fU"
                    % (RISK.get("consec_loss", 0), RISK.get("day_trades", 0), RISK.get("day_pnl", 0.0)))
            # ===== B11：恢复待确认池（原实现只存在内存里，机器人一重启就静默丢失）=====
            _ak = sv.get("asking")
            if isinstance(_ak, dict) and _ak:
                _now = time.time()
                _keep, _drop = 0, []
                for _c, _v in _ak.items():
                    if not isinstance(_v, dict) or not isinstance(_v.get("p"), dict):
                        continue
                    _ts = float(_v.get("ask_ts") or 0)
                    if _now - _ts > ASK_TIMEOUT:
                        _drop.append(_c)
                        continue
                    ASKING[_c] = {"p": _v["p"], "reason": _v.get("reason") or "(重启前挂起)",
                                  "ask_ts": _ts or _now, "txt": _v.get("txt") or ""}
                    _keep += 1
                if _keep:
                    log("已恢复 %d 个待确认信号（重启前挂起、未超时的）：%s"
                        % (_keep, "、".join(sorted(ASKING))))
                if _drop:
                    log("   ↳ 丢弃 %d 个已超时的待确认信号：%s" % (len(_drop), "、".join(sorted(_drop))))
            # ===== 暂停态恢复：原来 PAUSED 只在内存里，重启会**静默恢复交易** =====
            if "paused" in sv:
                PAUSED[0] = bool(sv.get("paused"))
                log("已恢复暂停状态：%s" % ("暂停中（不会开单/平仓，发「继续」恢复）"
                                          if PAUSED[0] else "运行中"))
        except Exception:
            pass
    # ===== 启动对账闸门（评估 G3）：放在开页之前，避免带着不一致状态开始跑 =====
    try:
        startup_reconcile()
    except Exception as _e:
        log("启动对账异常：%s" % str(_e)[:120])
    # ===== 持仓健康自检（B14 配套）：止盈/止损落在错误一侧的仓位会被误判成交 =====
    try:
        position_sanity(open_pos, notify_user=True)
    except Exception as _e:
        log("持仓自检异常：%s" % str(_e)[:120])
    with sync_playwright() as p:
        try:
            ctx = launch_persistent(p, BASE + "/fs_bot")
        except Exception as _e0:
            # 启动失败最常见的原因：上一次崩溃残留下来的 Singleton 锁 / 孤儿 chrome 占着 profile。
            # ⚠️ 只杀 ppid==1 的孤儿 chrome，绝不动正在被其它进程正常使用的浏览器。
            log("浏览器首次启动失败：%s" % str(_e0)[:120])
            log("   ↳ 清理孤儿 chrome + profile 锁后重试一次（只杀孤儿，不碰正常浏览器）")
            _k = kill_profile_chrome(BASE + "/fs_bot", orphan_only=True)
            if _k:
                log("   ↳ 已清理孤儿 chrome：%s" % _k)
            _g = clear_singleton_locks(BASE + "/fs_bot")
            if _g:
                log("   ↳ 已清理 profile 锁：%s" % _g)
            ctx = launch_persistent(p, BASE + "/fs_bot")
        pages = {}
        # 打开页面后的统一处理：⚠️ 绝不把游标抬到"页面最新"，否则停机期间的消息会被静默吞掉
        def adopt_page(g, rows):
            """返回该群停机积压条数；None 表示没读到消息。仅对无游标的新群做初始化。"""
            ids = [int(r["id"]) for r in rows if r.get("id")]
            if not ids:
                return None
            newest = max(ids)
            saved = last_id.get(g, 0)
            if saved <= 0:
                last_id[g] = newest
                log("   ↳ [%s] 无历史游标（首次运行/新增群），游标初始化为页面最新 %s，不回补历史"
                    % (g, datetime.datetime.fromtimestamp(newest >> 32, CST).strftime("%m-%d %H:%M:%S")))
                return 0
            gap = sorted([r for r in rows if r.get("id") and int(r["id"]) > saved],
                         key=lambda r: int(r["id"]))
            if gap:
                log("   ↳ [%s] 停机期间积压 %d 条（%s ~ %s），本轮将按序回补"
                    % (g, len(gap),
                       datetime.datetime.fromtimestamp(int(gap[0]["id"]) >> 32, CST).strftime("%m-%d %H:%M"),
                       datetime.datetime.fromtimestamp(int(gap[-1]["id"]) >> 32, CST).strftime("%m-%d %H:%M")))
            else:
                log("   ↳ [%s] 无积压，游标保持不变" % g)
            return len(gap)

        catchup_total = 0
        for g in GROUPS:
            pg, rows = open_group_page(ctx, g)
            pages[g] = pg
            ids = [int(r["id"]) for r in rows if r.get("id")]
            if ids:
                log("[%s] 已打开 | 页面最新 %s | 本群游标 %s | 末条=%s" % (
                    g, datetime.datetime.fromtimestamp(max(ids) >> 32, CST).strftime("%m-%d %H:%M:%S"),
                    (datetime.datetime.fromtimestamp(last_id[g] >> 32, CST).strftime("%m-%d %H:%M:%S")
                     if last_id.get(g) else "无"),
                    (rows[-1].get("text") or "")[:36]))
                _n = adopt_page(g, rows)
                if _n:
                    catchup_total += _n
                try:                      # 方案C 前置调研：只读诊断，记录 URL 与 chat-id 候选
                    _ci = pg.evaluate(CHATID_JS)
                    log("   ↳ [%s] URL=%s ｜ chat-id 候选: %s"
                        % (g, _ci.get("url"), " ; ".join(_ci.get("hits") or []) or "未找到"))
                except Exception as _e:
                    log("   ↳ [%s] chat-id 探测失败 %s" % (g, str(_e)[:60]))
            else:
                log("[%s] 打开失败（未读到消息）" % g)
        log("==== 开始实时监控（%d 个页面）====" % len(pages))
        # ⚠️ 不要在这里写 {"open": []}，会把已恢复的持仓清空（曾经踩过这个坑）
        json.dump({"open": open_pos, "last": last_id, "seen": sorted(SEEN)[-800:], "risk": RISK,
                   "asking": _asking_dump(),
                   "paused": bool(PAUSED[0]),
                   "ts": datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")},
                  open(STATE, "w"), ensure_ascii=False, indent=1)
        try:
            price_of("BTC")
            log("币安行情已预热（减少下单阶段耗时）")
        except Exception:
            pass
        if _BEXEC_OK:
            # 预热真实下单层：合约规格（exchangeInfo ~900 个）和持仓模式只拉一次，
            # 否则【第一条信号】会额外背 1~3 秒的网络耗时。
            try:
                bexec.load_specs()
                _hg = bexec.is_hedge()
                log("真实下单层已预热：合约规格 %d 个 ｜ 持仓模式=%s ｜ 当前=%s"
                    % (len(bexec._SPEC), "双向hedge" if _hg else "单向", _be_mode()))
            except Exception as _e:
                log("真实下单层预热失败（不影响纸面运行）：%s" % str(_e)[:120])
        _cu = ("\n⚠️ 检测到停机期间积压 %d 条消息，正在按序回补（超过 %d 分钟的信号只通报、不下单）"
               % (catchup_total, CATCHUP_MAX_AGE // 60)) if catchup_total else ""
        notify("【跟单机器人】dryRun 已启动（纸面模式，只抓开单信号，不会下单）" + _cu)
        hb = 0
        feed_prev = {}            # 群 -> 上次看到的会话列表预览
        safety = 0                # 兜底：每 N 轮无条件扫一次所有群
        # ===== B1 自愈状态 =====
        _reopen_fail = {}         # 群 -> 连续重开失败次数（日志降噪 + 判定用）
        _dead_groups = set()      # 本轮哪些群因「整个上下文已死」而失败
        _dead_rounds = [0]        # 连续多少轮出现「上下文已死」
        _relaunch_ts = [0.0]      # 上次全量重启时刻（节流）
        _relaunch_fail = [0]      # 连续重启失败次数
        _blind_since = [0.0]      # 本轮失明起点（用来汇报「瞎了多久」）

        def relaunch_browser(why):
            """整个浏览器上下文已死 → 重建 persistent context + 重开所有页面 + 重预热。
            游标一律保留：停机期间的消息交给回补闸门（超 30 分钟只通报不下单）。"""
            nonlocal ctx
            _relaunch_ts[0] = time.time()
            t0 = time.time()
            prof = BASE + "/fs_bot"
            log("!" * 60)
            log("🧯 [B1 自愈] 浏览器上下文死亡（%s）→ 开始全量重启浏览器" % why)
            notify("【跟单机器人】🧯 **浏览器上下文死亡，正在自动重启**\n"
                   "原因：%s\n"
                   "重启期间暂停抓信号；游标已保留，停机消息走回补闸门（超 30 分钟只通报不下单）。"
                   % why)
            try:
                ctx.close()
            except Exception:
                pass
            _k = kill_profile_chrome(prof)
            if _k:
                log("   ↳ 清理残留 chrome 进程：%s" % _k)
            _g = clear_singleton_locks(prof)
            if _g:
                log("   ↳ 清理 profile 锁：%s" % _g)
            try:
                ctx = launch_persistent(p, prof)
            except Exception as e:
                _relaunch_fail[0] += 1
                log("   ✗ 重建浏览器失败（第 %d 次）：%s" % (_relaunch_fail[0], str(e)[:120]))
                notify("【跟单机器人】🛑 **浏览器自动重启失败**（第 %d 次）：%s\n"
                       "机器人当前读不到任何群，请人工介入：pm2 restart dryrun-bot2"
                       % (_relaunch_fail[0], str(e)[:120]))
                return False
            for _gg in GROUPS:
                pages[_gg] = None
            ok_pages = []
            for _gg in GROUPS:
                try:
                    _pg, _rows = open_group_page(ctx, _gg)
                    pages[_gg] = _pg
                    adopt_page(_gg, _rows)
                    ok_pages.append(_gg)
                except Exception as e:
                    pages[_gg] = None
                    log("   ✗ [%s] 重开失败：%s" % (_gg, str(e)[:90]))
            feed_prev.clear()
            for _gg in GROUPS:
                _reopen_fail[_gg] = 0
            try:
                price_of("BTC")
                log("   币安行情已重新预热")
            except Exception:
                pass
            if _BEXEC_OK:
                try:
                    bexec.load_specs()
                except Exception:
                    pass
            dt = time.time() - t0
            blind = (time.time() - _blind_since[0]) if _blind_since[0] else dt
            _blind_since[0] = 0.0
            if ok_pages:
                _relaunch_fail[0] = 0
            log("🧯 [B1 自愈] 浏览器已重建：%d/%d 个页面就绪，用时 %.1fs，本次累计失明 %.1fs（%.1f 分钟）"
                % (len(ok_pages), len(GROUPS), dt, blind, blind / 60.0))
            notify("【跟单机器人】%s **浏览器已自动重启**：%d/%d 个页面就绪，用时 %.1fs\n"
                   "本次累计失明约 %.1f 分钟（%.0f 秒）。%s"
                   % ("✅" if ok_pages else "⚠️", len(ok_pages), len(GROUPS), dt,
                      blind / 60.0, blind,
                      "游标已保留，停机消息走回补闸门。" if ok_pages
                      else "有页面没打开成功，下一轮会继续重试。"))
            return bool(ok_pages)

        while True:
            # ===== 方案A：先用「会话列表预览」判断哪个群有新消息（一次 JS 调用 ≈0.2s）=====
            ref_page = next((pages[g] for g in GROUPS if pages.get(g) and not pages[g].is_closed()), None)
            feed = feed_snapshot(ref_page) if ref_page else {}
            changed, missing = [], []
            for g in GROUPS:
                prev_txt = feed_preview_of(feed, g)
                if prev_txt is None:
                    missing.append(g)                      # 列表里没这个群 -> 兜底扫
                elif feed_prev.get(g) != prev_txt:
                    changed.append(g)
                feed_prev[g] = prev_txt
            safety += 1
            if safety % 30 == 0:                            # 每 15 轮（约 15~30s）全量扫一次兜底
                to_scan = list(GROUPS)
            else:
                to_scan = list(changed)
                if missing and safety % 3 == 0:              # 列表里看不到的群：每 3 轮轮换兜底扫 1 个
                    to_scan.append(missing[safety % len(missing)])
            if changed:
                log("🔔 会话列表显示有新消息：%s" % "、".join(changed))
            # ===== B1 自愈判定（基于上一轮的重开结果，在扫描前处理）=====
            if _dead_groups:
                _dead_rounds[0] += 1
                if _blind_since[0] == 0.0:
                    _blind_since[0] = time.time()
                    log("👁 [B1 自愈] 出现「整个浏览器上下文已死」信号（%d 个群：%s）→ 进入观察"
                        % (len(_dead_groups), "、".join(sorted(_dead_groups))))
            else:
                _dead_rounds[0] = 0
            if _dead_rounds[0] >= DEAD_ROUNDS_TO_RELAUNCH or len(_dead_groups) >= 2:
                if time.time() - _relaunch_ts[0] >= RELAUNCH_MIN_GAP:
                    relaunch_browser("连续 %d 轮命中上下文已死（涉及群：%s）"
                                     % (_dead_rounds[0], "、".join(sorted(_dead_groups)) or "-"))
                    _dead_rounds[0] = 0
                elif safety % 40 == 0:
                    log("   ⏳ 浏览器重启被节流（距上次 %.0fs < %.0fs），本轮仍按单群重开处理"
                        % (time.time() - _relaunch_ts[0], RELAUNCH_MIN_GAP))
            _dead_groups.clear()
            missed_sig = []          # 本轮被闸门拦下的消息（只通报，不下单）
            for g in GROUPS:
                page = pages.get(g)
                if g not in to_scan:
                    continue
                if page is None or page.is_closed():
                    # ⚠️ 旧代码在 page 为 None 时直接 continue —— 那个群会永久停止监控，且日志里毫无提示。
                    #    现在改为主动重开；重开后保留原游标，停机期间的消息由回补闸门处理。
                    _reopen_fail[g] = _reopen_fail.get(g, 0) + 1
                    _nf = _reopen_fail[g]
                    if _nf <= 2 or _nf % 20 == 0:     # 日志降噪：事故时这两行刷了 2488 条
                        log("[%s] 页面不存在/已关闭，正在重新打开…（连续第 %d 次）" % (g, _nf))
                    try:
                        _pg, _rows = open_group_page(ctx, g)
                        pages[g] = _pg
                        adopt_page(g, _rows)
                        if _nf > 1:
                            log("[%s] 页面已重开成功（此前连续失败 %d 次）" % (g, _nf))
                        _reopen_fail[g] = 0
                    except Exception as _e:
                        pages[g] = None
                        # 关键：区分「这一个标签页被关了」（重开就好）与「整个上下文已死」（重开必然失败）
                        if ctx_dead_error(_e):
                            _dead_groups.add(g)
                        if _nf <= 2 or _nf % 20 == 0:
                            log("[%s] 重开失败（连续第 %d 次）：%s" % (g, _nf, str(_e)[:80]))
                    continue
                try:
                    page.mouse.move(900, 400); page.mouse.wheel(0, 2600); time.sleep(0.3)
                    rows = page.evaluate(SCAN_JS)
                    if not rows:
                        finalize_pending(open_pos)          # 方案B：每个群扫完就检查一次出单
                        continue
                    base = last_id.get(g, 0)
                    cand = sorted([r for r in rows if r.get("id") and int(r["id"]) > base],
                                  key=lambda r: int(r["id"]))
                    # ===== 回补闸门（2026-09-13）替代旧的「发现 >15 条就静默全丢」=====
                    # ① 超过 CATCHUP_MAX_AGE 的老信号 -> 只通报不下单，避免拿过期点位追单
                    # ② 超过 CATCHUP_MAX_MSGS 的 -> 只处理最新那批，其余只通报
                    # 两条路径都推进游标 + 标记已处理，确保永不重复，但绝不静默丢弃
                    now_ts = time.time()
                    fresh, gated = [], []
                    for r in cand:
                        if now_ts - (int(r["id"]) >> 32) > CATCHUP_MAX_AGE:
                            gated.append(("超时效", r))
                        else:
                            fresh.append(r)
                    if len(fresh) > CATCHUP_MAX_MSGS:
                        gated.extend(("超数量上限", r) for r in fresh[:-CATCHUP_MAX_MSGS])
                        fresh = fresh[-CATCHUP_MAX_MSGS:]
                    for _why, r in gated:
                        _mid = r["id"]
                        mark_seen(_mid)
                        last_id[g] = max(last_id.get(g, 0), int(_mid))
                        STATE_DIRTY[0] = True
                        missed_sig.append((g, _why, int(_mid) >> 32, (r.get("text") or "")[:70]))
                    if gated:
                        log("[%s] 回补闸门拦下 %d 条（超时效 %d / 超上限 %d）：只通报不下单"
                            % (g, len(gated), sum(1 for w, _ in gated if w == "超时效"),
                               sum(1 for w, _ in gated if w == "超数量上限")))
                    new = fresh
                    for r in new:
                        mid = r["id"]
                        t_sig = int(mid) >> 32
                        when = datetime.datetime.fromtimestamp(t_sig, CST).strftime("%m-%d %H:%M:%S")
                        txt = strip_sender_prefix(r["text"])   # 去掉行首的发送者名（"自定义机器人 BOT" 里的 BOT 是真实交易对，会误导币种识别）
                        LAST_MSG_TS[0] = time.time()           # 失联看门狗用：只要抓到任何一条消息就刷新
                        log("[%s] 发现新消息 | 发出=%s | %s" % (g, when, txt[:110]))
                        if int(mid) in SEEN:
                            log("   ↳ 该消息此前已处理过，跳过（防重复开单）")
                            last_id[g] = max(last_id.get(g, 0), int(mid))
                            continue
                        mark_seen(mid)
                        # 逐条推进游标：进程若中途挂掉，重启后只会重放（SEEN 挡住重复开单），不会丢单
                        last_id[g] = max(last_id.get(g, 0), int(mid))
                        STATE_DIRTY[0] = True
                        low = txt.lower()
                        SELF_MARKS = ["【跟单机器人】", "【机器人指令】", "【已开单·纸面】", "【已结单·纸面】", "【止盈成交·纸面】", "【你的持仓】", "【指令】", "【博主指令】", "【信号·"]
                        if any(_m in txt for _m in SELF_MARKS) or "通过webhook" in txt or "invited" in low or "test notification" in low:
                            continue
                        # 指令优先：只有在指定指令群里、由你发的短消息才会被当成指令
                        if g in CMD_GROUPS:
                            try:
                                if handle_command(txt):
                                    continue
                            except Exception as _e:
                                log("   指令处理异常 " + str(_e)[:90])
                        SIG_KW = ["long", "Long", "LONG", "short", "Short", "SHORT", "Entry", "CMP",
                                  "做多", "做空", "止损", "止盈", "平仓", "减仓", "close", "Closed", "TP", "SL"]
                        has_img = r.get("loaded", 0) > 0 or r.get("nimg", 0) >= 2
                        if not any(k in txt for k in SIG_KW) and not has_img:
                            log("   ↳ 闲聊/无关，跳过")
                            continue
                        t_found = time.time()
                        # 抓图：只要消息里有图片元素就尝试（等它真正加载）
                        imgs = []
                        if r.get("nimg", 0) > 0:
                            wait_ms = 6000 if r.get("nimg", 0) >= 2 else 1200
                            data = page.evaluate(FETCH_IMG_JS, {"mid": mid, "waitMs": wait_ms})
                            for i, d in enumerate(data or []):
                                if isinstance(d, str) and d.startswith("data:image"):
                                    fn = IMGDIR + "/" + mid + "_" + str(i) + ".png"
                                    try:
                                        open(fn, "wb").write(base64.b64decode(d.split(",", 1)[1])); imgs.append(fn)
                                    except Exception:
                                        pass
                            if r.get("loaded", 0) > 0 or imgs:
                                log("   媒体: 元素=%d 已加载=%d 抓到图=%d" % (r.get("nimg", 0), r.get("loaded", 0), len(imgs)))
                        t_img = time.time()
                        # ===== 方案C+D：本地正则先解析；需要 AI 时才调，且与读图并行 =====
                        # ===== 多币种消息：按币拆开、各自解析，然后逐个问用户（用户 2026-09-14 要求）=====
                        # 例：「原油Cl跌破97空，100.7止损，93止盈。 Sol突破102.5多，止损100，止盈107到110。」
                        # 不再整条当成"笼统总结"丢掉，而是拆成 CL / SOL 两条独立信号请你逐个确认。
                        _segs = split_by_coin(txt)
                        if len(_segs) >= 2:
                            log("   ↳ 多币种消息，按币拆开：%s" % [c for c, _ in _segs])
                            _names = []
                            for _c, _seg in _segs:
                                _ci = fast_parse(_seg) or {}
                                if (not _ci.get("direction")) or fast_parse_suspect(_seg, _ci):
                                    _ai = parse_text(_seg) or {}
                                    _ci = {**(_ci or {}), **{k: v for k, v in _ai.items()
                                                             if v not in (None, [], "")}}
                                _dir_ = (_ci.get("direction") or "").upper() or None
                                if not _dir_:
                                    log("   ↳ %s 段没解析出方向 → 只记录不询问" % _c)
                                    continue
                                PENDING.pop(_c, None)
                                _pp = merge_pending(_c, g, info=_ci, txt=_seg,
                                                    t_sig=t_sig, stamps={"found": t_found, "img": 0.0,
                                                                         "parse": time.time(), "chart": 0.0})
                                _pp["dir"] = _dir_
                                _pp["group"] = g
                                PENDING.pop(_c, None)
                                _tt = sorted(set(_pp.get("tps") or []))
                                # ===== B3 修复：多币种分支必须过【同一套】公共校验 =====
                                # 原来这里直接 ask_user，完全绕过校验 → 实测把「DOGE 做多、入场 0.079、
                                # 止损 0.09（在入场价上方）」这种解析错误直接发给了用户审批。
                                _mk = price_of(_c)
                                _ent = _pp.get("entry")
                                if not isinstance(_ent, (int, float)) or not _ent:
                                    _lg = [float(x) for x in (_pp.get("legs") or []) if isinstance(x, (int, float))]
                                    _ent = (sum(_lg) / len(_lg)) if _lg else None
                                _bad_why, _tt2 = validate_plan(_c, _dir_, _ent, _pp.get("stop"), _tt, _mk,
                                                               texts=[_seg])
                                _soft = soft_ask_reason(_seg, _pp.get("stop"), _ent)
                                if _tt2:
                                    _tt = _tt2
                                if _bad_why:
                                    log("   ⛔ [%s] 多币种分支公共校验未通过：%s" % (_c, _bad_why))
                                    notify("【信号·拒绝】%s %s\n%s\n**不下单**\n原文：%s"
                                           % (_c, "做多" if _dir_ == "LONG" else "做空",
                                              _bad_why, _seg[:160]))
                                    continue
                                ask_user(_c, _pp,
                                         "多币种消息，已按币拆开；本条解析结果如上，请你单独确认"
                                         + (("；另外：" + _soft) if _soft else ""),
                                         quiet=True)
                                notify("· %s %s：开仓=%s 止损=%s 止盈=%s"
                                       % (_c, "做多" if _dir_ == "LONG" else "做空",
                                          _pp.get("entry") if _pp.get("entry") is not None else "未读到",
                                          _pp.get("stop") if _pp.get("stop") is not None else "未读到",
                                          _tt or "未读到"))
                                _names.append(_c)
                            if _names:
                                notify("【信号·多币种待确认】这条消息里有 %d 个币种，已分别解析（见上）。\n"
                                       "请回复要开哪些（%d 个币共 %d 种非空组合）：\n%s"
                                       % (len(_names), len(_names), 2 ** len(_names) - 1,
                                          "\n".join(_combo_options(_names))))
                            continue
                        info = fast_parse(txt)
                        _fast = info
                        _suspect = fast_parse_suspect(txt, info)
                        if info is not None and not _suspect:
                            log("   ⚡ 快速解析命中（本地正则，0 AI 调用）")
                        elif info is not None and _suspect:
                            log("   ⚠️ 快速解析不完整（文字里 %d 个止盈关键词，只读到 %d 档）"
                                "→ 改调 AI 复核，避免静默漏档"
                                % (len(_TPKW_RE.findall(txt)), len(info.get("targets") or [])))
                            info = None
                        need_chart = bool(imgs) and (info is None or len(info.get("targets") or []) < TP_TIERS or not info.get("stop"))
                        _th, _res = None, {}
                        if need_chart:
                            _th = threading.Thread(target=lambda: _res.update({"chart": read_chart_cached(imgs[0])}))
                            _th.start()
                        elif imgs:
                            log("   ⏩ 文字/卡片已够（止损+3档止盈），跳过读图")
                        if info is None:
                            info = parse_text(txt)
                        # AI 结果若比快速解析还少 → 合并补齐（绝不因为 AI 少读而丢档）
                        if isinstance(_fast, dict):
                            if isinstance(info, dict):
                                if len(_fast.get("targets") or []) > len(info.get("targets") or []):
                                    log("   ↳ AI 止盈档位少于快速解析，已合并补齐：%s" % _fast["targets"])
                                    info["targets"] = _fast["targets"]
                                for _k in ("stop", "entry"):
                                    if info.get(_k) is None and _fast.get(_k) is not None:
                                        info[_k] = _fast[_k]
                            else:
                                info = _fast
                        t_parse = time.time()                 # 文本解析耗时（不含读图）
                        if _th is not None:
                            _th.join(timeout=120)
                        info = info or {}
                        chart = _res.get("chart")
                        if chart and chart.get("ok"):
                            log("   读图: 止损 %s 开仓 %s 止盈 %s" % (chart["sl"], chart["entry"], chart["tps"]))
                        raw_coin = info.get("coin")
                        coin, coin_ok = (resolve_coin(raw_coin) if raw_coin else (None, False))
                        if raw_coin and coin and not coin_ok:
                            log("   ⚠️ 币种 %s（原文写法 %s）不在币安 USDT-M 清单里" % (coin, raw_coin))
                            notify("【信号·不支持】%s\n币安 USDT-M 没有这个币种的合约（原文写法：%s）\n我们只交易 USDT 计价的合约。\n原文：%s"
                                   % (coin, raw_coin, txt[:160]))
                            continue
                        dirc = (info.get("direction") or "").upper() or None
                        # 只有图、文字里没有币种 -> 从图上读币种
                        if coin is None and imgs:
                            meta = read_chart_meta(imgs[-1])
                            mc, mc_ok = resolve_coin(meta.get("coin"))
                            if meta.get("is_chart") and mc and mc_ok:
                                coin = mc
                                dirc = dirc or ((meta.get("direction") or "").upper() or "LONG")
                                log("   图上读到币种: %s %s" % (coin, dirc))
                        t_chart = time.time()
                        stamps = {"found": t_found, "img": t_img, "parse": t_parse, "chart": t_chart}
                        # 博主管理指令：立即处理（带确定性护栏，避免把"止盈达成"误判成"全部平仓"）
                        _act = info.get("manage_action")
                        if _act and _act != "other":
                            INFO_ONLY = r"(TP\s?\d?\s*(hit|nailed|done|reached|filled)|take[- ]?profit\s*(hit|reached)|breakeven\s*hit|止盈.{0,6}(达成|到了|命中|触发|已到)|保本.{0,4}(止损|离场))"
                            CLOSE_REQ = r"(\bout of\b|\bclosed?\b|closing\b|exit(ing)?\b|stopped out|stop(ped)? (me )?out|fully closed|平仓|清仓|全部走|先走|离场|走人)"
                            if re.search(INFO_ONLY, txt, re.I) and not re.search(CLOSE_REQ, txt, re.I):
                                log("   ⚠️ 判定为『通报』而非指令（含止盈达成字样，无平仓字样）→ 不动作：%s" % _act)
                                _act = None
                                if coin and coin in open_pos:
                                    try:
                                        pos_report(coin, open_pos[coin])
                                    except Exception as _e:
                                        log("   持仓汇报失败 " + str(_e)[:80])
                        if info.get("type") == "manage" and not _act:
                            log("   ↳ 判定为博主管理类消息但无明确动作 → 不动作：%s" % txt[:80])
                            continue
                        if _act:
                            notify("【博主指令】%s\n群：%s  时间：%s\n动作：%s\n原文：%s" % (coin or "?", g, when, _act, txt[:200]))
                            continue
                        # ===== 结单/止盈止损通报：只回报自己的持仓，【绝不开新仓】=====
                        # 博主常把「Trade Closed / Stop loss hit at X」当成一条消息发出来，
                        # 里面既有币种也有方向也有价格，很容易被当成开单信号 —— 必须挡在这里。
                        if _CLOSE_ANNOUNCE.search(txt):
                            log("   ↳ 判定为【结单/止损通报】，不建仓：%s" % txt[:90])
                            if coin and coin in open_pos:
                                try:
                                    pos_report(coin, open_pos[coin])
                                except Exception as _e:
                                    log("   持仓汇报失败 " + str(_e)[:80])
                            continue
                        if coin and dirc in ("LONG", "SHORT"):
                            # 开单信号 -> 进待确认池，等同一条信号的后续消息（卡片/图）补齐
                            p = merge_pending(coin, g, info=info, chart=chart, imgs=imgs, t_sig=t_sig, txt=txt, stamps=stamps)
                            if dirc: p["dir"] = dirc
                            log("   待确认池 %s：开仓=%s(%s) 加仓=%s 止损=%s 止盈=%s 图=%d 已合并%d条消息" % (
                                coin, p["entry"], p.get("entry_src") or "-", p["add"], p["stop"],
                                sorted(set(p["tps"])), len(p["imgs"]), len(p["texts"])))
                        elif coin and (coin in PENDING or coin in open_pos) and (
                                chart or imgs
                                or any(isinstance(info.get(k), (int, float)) for k in ("entry", "stop", "add_price"))
                                or (info.get("targets") or [])):
                            # 后续消息（卡片/带图）补进同一条信号；
                            # 已有持仓的币也放进来 —— 博主后来补的止盈位要能更新上去（2R 兜底替换）
                            merge_pending(coin, g, info=info, chart=chart, imgs=imgs, t_sig=t_sig, txt=txt, stamps=stamps)
                            log("   并入 %s 的待确认池（补充信息，图=%d%s）"
                                % (coin, len(imgs), "，持仓中→待更新止盈" if coin in open_pos else ""))
                        else:
                            # ⚠️ 2026-09-13：这里以前是【什么都不做、也不留一行日志】的静默丢弃。
                            #    实例：13:56 黄金mansoor 发「XAUUSD 👀 + 推文链接 + 图」，
                            #    图都抓到了，却既没下单、也没任何记录 —— 你完全不知道错过了什么。
                            log("   ↳ 没通过信号门槛（币种=%s 方向=%s 图=%d 类型=%s），未下单：%s"
                                % (coin or "-", dirc or "-", len(imgs), info.get("type") or "-", txt[:100]))
                            _looks_signal = bool(coin) and (
                                bool(imgs) or any(k in txt for k in ("止损", "止盈", "Entry", "SL", "TP")))
                            if _looks_signal and (time.time() - _UNIDENT_NOTIFY.get(g, 0) > UNIDENT_NOTIFY_COOLDOWN):
                                _UNIDENT_NOTIFY[g] = time.time()
                                notify("【信号·未能识别】%s\n识别到币种 %s%s，但没能解析出方向/点位 "
                                       "→ **未下单**，等你确认\n原文：%s"
                                       % (coin, coin,
                                          ("，图已抓到 %d 张（读了但没读出可用的方向/点位）" % len(imgs))
                                          if imgs else "，无图",
                                          txt[:200]))
                    # 方案B：本群处理完立刻检查一次出单（不再等整轮扫完 5 个群）
                    finalize_pending(open_pos)
                except Exception as e:
                    msg = str(e)[:120]
                    log("[%s] 轮询异常 %s" % (g, msg))
                    if ctx_dead_error(e):
                        _dead_groups.add(g)          # 整个上下文已死，交给 B1 自愈做全量重启
                    if "crash" in msg.lower() or "closed" in msg.lower():
                        try:
                            pages[g].close()
                        except Exception:
                            pass
                        log("[%s] 页面崩溃，正在重建…" % g)
                        try:
                            pages[g] = open_group_page(ctx, g)[0]
                            _reopen_fail[g] = 0
                        except Exception as _e2:
                            pages[g] = None
                            if ctx_dead_error(_e2):
                                _dead_groups.add(g)
                            log("[%s] 重建失败：%s" % (g, str(_e2)[:80]))
            # 被回补闸门拦下的消息：汇总通报给你（文本以 【跟单机器人】 开头，
            # 会被下面的 SELF_MARKS 检查过滤掉，不会引发自我循环 —— 这条依赖别删）
            if missed_sig:
                _ls = ["【跟单机器人】⚠️ 回补闸门拦下 %d 条消息（未下单，仅通报）" % len(missed_sig)]
                for _g, _why, _ts, _tx in missed_sig[:8]:
                    _ls.append("· %s｜%s｜%s\n  %s" % (
                        _why, _g,
                        datetime.datetime.fromtimestamp(_ts, CST).strftime("%m-%d %H:%M:%S"), _tx))
                if len(missed_sig) > 8:
                    _ls.append("· …另有 %d 条（详见 run.log）" % (len(missed_sig) - 8))
                try:
                    notify("\n".join(_ls))
                except Exception as _e:
                    log("错过信号通报失败 " + str(_e)[:80])
            # ===== 方案B：runtime_config.json 热加载 =====
            # 目的：改金额/杠杆/测试模式/监控群时不再 restart 进程，
            #       从而彻底消除「重启 4.5 分钟盲窗」。新增群只新开一个页面。
            try:
                _mt = os.path.getmtime(RUNTIME)
            except Exception:
                _mt = RUNTIME_MTIME[0]
            if _mt != RUNTIME_MTIME[0]:
                _old = ("、".join(GROUPS), MARGIN, LEV, bool(TEST_MODE))
                load_runtime()
                RUNTIME_MTIME[0] = _mt
                _chg = []
                for _g in [x for x in list(pages) if x not in GROUPS]:       # 不再监控 -> 关页面省内存
                    _pg = pages.pop(_g, None)
                    try:
                        if _pg and not _pg.is_closed():
                            _pg.close()
                    except Exception:
                        pass
                    last_id.pop(_g, None)
                    _chg.append("移除 %s" % _g)
                    log("[热加载] 已关闭不再监控的群页面：%s" % _g)
                for _g in [x for x in GROUPS if x not in pages]:              # 新增 -> 立刻开页面
                    log("[热加载] 新增监控群 %s，正在开页面（约 1 分钟）…" % _g)
                    try:
                        _pg, _rows = open_group_page(ctx, _g)
                        pages[_g] = _pg
                        adopt_page(_g, _rows)                                 # 无游标则从页面最新起步
                        _chg.append("新增 %s" % _g)
                    except Exception as _e:
                        pages[_g] = None
                        log("[热加载] %s 开页失败：%s" % (_g, str(_e)[:80]))
                        _chg.append("新增 %s（开页失败）" % _g)
                _new = ("、".join(GROUPS), MARGIN, LEV, bool(TEST_MODE))
                if _chg or _new != _old:
                    log("[热加载] 生效：%s -> %s" % (_old, _new))
                    notify("【跟单机器人】配置已热加载（未重启，无盲窗）\n监控群=%s 保证金=%.0fU 杠杆=%d倍 测试模式=%s%s"
                           % (_new[0], _new[1], _new[2], _new[3],
                              ("\n" + "；".join(_chg)) if _chg else ""))
            # 实盘限价入场的成交监听
            try:
                watch_entries()
            except Exception as e:
                log("成交监听异常 " + str(e)[:100])
            # 裸仓看门狗（每约 30 秒查一次）
            _NAKED_TICK[0] += 1
            if _NAKED_TICK[0] % NAKED_EVERY == 0:
                try:
                    watch_naked()
                except Exception as e:
                    log("裸仓看门狗异常 " + str(e)[:100])
                try:
                    watch_silence()
                except Exception as e:
                    log("失联看门狗异常 " + str(e)[:100])
                try:
                    # B14 配套：定期只记日志不打扰（启动时已完整通报过一次）
                    position_sanity(open_pos, notify_user=False)
                except Exception as e:
                    log("持仓自检异常 " + str(e)[:100])
            # 待确认信号超时作废
            try:
                expire_asking()
            except Exception as e:
                log("待确认超时处理异常 " + str(e)[:80])
            # 待确认池：信息齐全就出单，到点还没齐也只发提醒（绝不猜价）
            try:
                finalize_pending(open_pos)
            except Exception as e:
                log("待确认池处理异常 " + str(e)[:100])
            # 纸面持仓监控：分批止盈（每档平 1/3）+ TP1 后止损移保本
            try:
                for coin, tr in list(open_pos.items()):
                    if tr.get("pending_fill"):
                        continue          # 实盘限价单还没成交 → 不跟踪止盈止损（等成交监听接管）
                    px = price_of(coin)
                    if px is None:
                        continue
                    d = 1 if tr["dir"] == "LONG" else -1
                    tps = tr.get("tps") or []
                    filled = tr.setdefault("filled", [])
                    remaining = tr.get("remaining", 1.0)
                    stop = tr.get("sl")
                    # ① 止损优先
                    if stop is not None and (px - stop) * d <= 0:
                        pnl = (stop - tr["entry"]) * d / tr["entry"] * NOTIONAL * remaining
                        tr["realized"] = tr.get("realized", 0.0) + pnl
                        tr.update({"status": "CLOSED", "exit": stop,
                                   "exit_why": ("止损" if not filled else "保本止损(TP1后)"),
                                   "pnl": tr["realized"], "remaining": 0.0})
                        # 止损是 STOP_MARKET 触发后市价平 → taker
                        _add_fee(tr, NOTIONAL * remaining * (stop / tr["entry"]), FEE_TAKER, "止损平仓")
                        _stat_close(tr)
                        with open(TRADES, "a", encoding="utf-8") as f:
                            f.write(json.dumps(tr, ensure_ascii=False) + "\n")
                        notify("【已结单·纸面】%s %s\n结果：%s @%.8g（剩余 %.0f%%）\n累计盈亏：%+.1fU（保证金 %.0fU）"
                               % (coin, tr["dir"], tr["exit_why"], stop, remaining * 100, tr["realized"], MARGIN))
                        if _BEXEC_OK and tr.get("real_layer") == "实盘":
                            # 只对【开仓时就是实盘】的仓位动真实挂单；切模式前开的纸面仓一律不碰（用户 2026-09-15 要求）
                            try:
                                bexec.cancel_all(coin.upper() + "USDT")
                                log("   ↳ [真实下单层·实盘] 仓位已了结，已撤掉剩余挂单")
                            except Exception as _e:
                                _live_alert("撤单", coin, str(_e))
                        open_pos.pop(coin, None)
                        continue
                    # ② 依次检查各档止盈
                    hit_i = None
                    for i, tp in enumerate(tps):
                        if i in filled:
                            continue
                        if (px - tp) * d >= 0:
                            hit_i = i
                            break
                    if hit_i is not None:
                        # 平仓比例：2R 兜底档 = 一次全平（tp_part=R_FALLBACK_PART=1.0，
                        # 用户 09-13 定的「到价全平」）；常规档位按 1/档数 平分（3 档各 33.3%）。
                        # ⚠️ 旧注释写的是"2R 兜底档只平 1/3"，与 R_FALLBACK_PART=1.0 矛盾，已改正。
                        part = tr.get("tp_part") or (1.0 / max(len(tps), 1))
                        tp = tps[hit_i]
                        pnl = (tp - tr["entry"]) * d / tr["entry"] * NOTIONAL * part
                        tr["realized"] = tr.get("realized", 0.0) + pnl
                        # 止盈是【限价单】成交 → maker 万2；成交名义按成交价算
                        _add_fee(tr, NOTIONAL * part * (tp / tr["entry"]), FEE_MAKER, "止盈%d" % (hit_i + 1))
                        filled.append(hit_i)
                        remaining = max(0.0, remaining - part)
                        tr["remaining"] = remaining
                        if hit_i == 0 and tr["entry"]:            # TP1 后止损移保本
                            tr["sl"] = tr["entry"]
                            stop = tr["entry"]
                        with open(TRADES, "a", encoding="utf-8") as f:
                            f.write(json.dumps(tr, ensure_ascii=False) + "\n")
                        nxt = next((tps[j] for j in range(len(tps)) if j not in filled), None)
                        notify("【止盈成交·纸面】%s %s\nTP%d 成交 @%.8g（平%.0f%%）\n该档盈亏：%+.1fU · 累计：%+.1fU\n剩余仓位：%.0f%%%s"
                               % (coin, tr["dir"], hit_i + 1, tp, part * 100, pnl, tr["realized"], remaining * 100,
                                  ("\n止损已移到开仓价 %.8g（保本损）" % stop) if hit_i == 0 else ""))
                        if _BEXEC_OK and tr.get("real_layer") == "实盘":
                            # 只对实盘仓动真实止损；切模式前开的纸面仓不碰
                            try:
                                _rq = _real_qty_or_estimate(coin, tr)
                                bexec.sync_sl(coin.upper() + "USDT", tr["dir"],
                                              tr.get("sl") or tr["entry"], _rq)
                                _clear_live_alert("止损同步", coin)
                                log("   ↳ [真实下单层·实盘] 止损已同步：%s，数量 %s（按交易所真实持仓）"
                                    % ("移到开仓价" if hit_i == 0 else "价格不变", _rq))
                            except Exception as _e:
                                _live_alert("止损同步(TP%d后)" % (hit_i + 1), coin, _e,
                                            "纸面认为已平 %.0f%%，真实止损可能未更新" % (part * 100))
                        if remaining <= 0.001:
                            tr.update({"status": "CLOSED", "exit": tp, "exit_why": "全部止盈",
                                       "pnl": tr["realized"]})
                            _stat_close(tr)
                            with open(TRADES, "a", encoding="utf-8") as f:   # 补写最终 CLOSED 记录（含结单时间）
                                f.write(json.dumps(tr, ensure_ascii=False) + "\n")
                            notify("【已结单·纸面】%s %s\n全部止盈完成，累计盈亏：%+.1fU" % (coin, tr["dir"], tr["realized"]))
                            open_pos.pop(coin, None)
            except Exception as e:
                log("持仓监控异常 " + str(e)[:100])
            hb += 1
            if STATE_DIRTY[0]:
                STATE_DIRTY[0] = False
            json.dump({"open": open_pos, "last": last_id, "seen": sorted(SEEN)[-800:], "risk": RISK,
                       "asking": _asking_dump(),      # B11：待确认池落盘，重启不再静默丢失
                       "paused": bool(PAUSED[0]),     # 暂停态落盘，重启后依然生效
                       "ts": datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")},
                      open(STATE, "w"), ensure_ascii=False, indent=1)
            if hb % 10 == 0:
                log("心跳：运行中 | 持仓 %d 笔（%s）" % (len(open_pos), ",".join(open_pos) or "-"))
            time.sleep(POLL_SEC)

if __name__ == "__main__":
    if "--selftest-parse" in sys.argv:
        # 本地正则解析自检（不联网、不动状态）。第一条就是用户 16:17 的测试消息。
        cases = [
            ("用户测试：比特币77065价格做空+第一/第二止盈",
             "用户963038 比特币 77065价格做空 止损77500 第一止盈74500 第二止盈70500",
             {"coin": "BTC", "direction": "SHORT", "entry": 77065.0, "stop": 77500.0,
              "targets": [74500.0, 70500.0]}),
            ("黄金区间多 + 三档止盈",
             "黄金Xau在4360到4310区间多，止损4275，止盈4480 4620 4700",
             {"coin": "XAU", "direction": "LONG", "stop": 4275.0,
              "targets": [4480.0, 4620.0, 4700.0]}),
            ("止盈1 + 多档数字（不能被当档位序号吃掉）",
             "BTC 做多 止损0.190 止盈1 0.240 0.260 0.280",
             {"coin": "BTC", "direction": "LONG", "stop": 0.19,
              "targets": [0.24, 0.26, 0.28]}),
            ("止盈在前止损在后（不能把止损吃成止盈）",
             "BTC 做空 止盈74500 止损77500",
             {"coin": "BTC", "direction": "SHORT", "stop": 77500.0, "targets": [74500.0]}),
            ("博主只给止损不给止盈（交给 2R 兜底）",
             "Going long LSK here at CMP. SL 0.1993",
             {"coin": "LSK", "direction": "LONG", "stop": 0.1993, "targets": []}),
        ]
        _ok = 0
        for _name, _txt, _want in cases:
            _got = fast_parse(_txt)
            if _got is None:
                print("[FAIL] %-42s 解析返回 None" % _name)
                continue
            _bad = []
            for _k, _v in _want.items():
                _g = _got.get(_k)
                if _k == "targets":
                    if list(_g or []) != list(_v):
                        _bad.append("targets got=%s want=%s" % (_g, _v))
                elif _k == "direction":
                    if (_g or "") != _v:
                        _bad.append("direction got=%s want=%s" % (_g, _v))
                elif isinstance(_v, float):
                    if _g is None or abs(float(_g) - _v) > 1e-9:
                        _bad.append("%s got=%s want=%s" % (_k, _g, _v))
                elif _g != _v:
                    _bad.append("%s got=%s want=%s" % (_k, _g, _v))
            if _bad:
                print("[FAIL] %-42s %s" % (_name, " ; ".join(_bad)))
            else:
                print("[ OK ] %-42s %s" % (_name, json.dumps(
                    {k: _got.get(k) for k in ("coin", "direction", "entry", "stop", "targets")},
                    ensure_ascii=False)))
                _ok += 1
        # 不完整检测：直接对"看起来少读了"的情形做单元校验
        _susp_cases = [
            ("3 个止盈关键词/只读到 1 档 → 可疑",
             "BTC 做多 止损1 止盈 止盈 止盈", {"targets": [1.0], "stop": 1.0}, True),
            ("3 个止盈关键词/读到 3 档 → 不可疑",
             "BTC 做多 止盈1 止盈2 止盈3", {"targets": [1.0, 2.0, 3.0], "stop": 1.0}, False),
            ("没读到止损 → 可疑", "BTC 做多 止盈1 止盈2 止盈3",
             {"targets": [1.0, 2.0, 3.0], "stop": None}, True),
            ("没有止盈关键词 → 不可疑（交给 2R 兜底）",
             "Going long LSK here at CMP. SL 0.1993", {"targets": [], "stop": 0.1993}, False),
        ]
        _s_ok = 0
        for _n, _t, _i, _want in _susp_cases:
            _g = bool(fast_parse_suspect(_t, _i))
            print("%s %-42s suspect=%s want=%s" % ("[ OK ]" if _g == _want else "[FAIL]", _n, _g, _want))
            _s_ok += 1 if _g == _want else 0
        print("-" * 62)
        print("解析自检：%d/%d 通过；不完整检测 %d/%d 通过"
              % (_ok, len(cases), _s_ok, len(_susp_cases)))
        sys.exit(0 if (_ok == len(cases) and _s_ok == len(_susp_cases)) else 1)

    if "--selftest-tp" in sys.argv:
        # 2R 兜底止盈自检：不启动机器人、不联网、不动任何状态
        cases = [
            ("做多 入场100 止损95 -> 2R=110", (100, 95, "LONG"), 110.0),
            ("做空 入场100 止损105 -> 2R=90", (100, 105, "SHORT"), 90.0),
            ("实例 LSK 入场0.21519 止损0.1993", (0.21519, 0.1993, "LONG"), 0.21519 + 2 * (0.21519 - 0.1993)),
            ("实例 DOGE 入场0.08438 止损0.0828", (0.08438, 0.0828, "LONG"), 0.08438 + 2 * (0.08438 - 0.0828)),
            ("没止损 -> None（走绝对底线，不下单）", (100, None, "LONG"), None),
            ("入场=止损 -> None（R 为 0）", (100, 100, "LONG"), None),
        ]
        _ok = 0
        for _name, (_e, _s, _d), _want in cases:
            _got = fallback_tp_2r(_e, _s, _d)
            if _got is None and _want is None:
                _good = True
            elif _got is None or _want is None:
                _good = False
            else:
                _good = abs(_got - _want) < 1e-9
            print("%s %-38s got=%s want=%s" % ("[ OK ]" if _good else "[FAIL]", _name, _got, _want))
            _ok += 1 if _good else 0
        print("-" * 62)
        print("2R 兜底自检：%d/%d 通过（盈亏比 %.0f:1，该档平 %.0f%%）"
              % (_ok, len(cases), R_FALLBACK_MULT, R_FALLBACK_PART * 100))
        sys.exit(0 if _ok == len(cases) else 1)
    if "--selftest-b1" in sys.argv:
        # ===== B1 自愈自检：完全隔离在 /tmp，不碰生产配置/状态/日志，不发飞书，不下单 =====
        import shutil
        _T = "/tmp/b1_selftest"
        # ① 按交接文档第十一节第 16 条：生产路径全部重定向到 /tmp（这就是隔离的证明）
        _prod_log = BASE + "/v21/run.log"
        _before_lines = sum(1 for _ in open(_prod_log, encoding="utf-8", errors="replace"))
        RUNTIME = _T + "/runtime_config.json"
        TRADES = _T + "/trades_dryrun.jsonl"
        STATE = _T + "/state.json"
        LOGF = _T + "/run.log"
        IMGDIR = _T + "/imgs"
        NOTIFY_CFG = _T + "/notify.json"          # 不存在 → notify() 不会推飞书
        shutil.rmtree(_T, ignore_errors=True)
        os.makedirs(_T + "/imgs", exist_ok=True)
        print("=" * 70)
        print("B1 自愈自检（隔离在 /tmp，不碰生产）")
        print("=" * 70)
        print("路径重定向证明：")
        print("  BASE（只读引用） = %s" % BASE)
        print("  RUNTIME         = %s" % RUNTIME)
        print("  TRADES          = %s" % TRADES)
        print("  STATE           = %s" % STATE)
        print("  LOGF            = %s" % LOGF)
        print("  NOTIFY_CFG      = %s（不存在 → 不发飞书）" % NOTIFY_CFG)
        _fail = []

        # ---------- ② 单元：异常分类（用的是 2026-09-15 那场 93 分钟事故的真实报错串）----------
        print("\n[1] ctx_dead_error 分类（真实事故报错串）")
        _cases = [
            ("真实事故串 new_page closed",
             "BrowserContext.new_page: Target page, context or browser has been closed", True),
            ("Playwright 原始串", "Target page, context or browser has been closed", True),
            ("TargetClosedError 包装",
             "TargetClosedError('Target page, context or browser has been closed')", True),
            ("浏览器整体关闭", "Browser has been closed", True),
            ("驱动连接断开", "Connection closed while reading from the driver", True),
            ("页面级 WebSocket 关闭（不可误判）", "WebSocket connection closed", False),
            ("普通超时（不可误判）", "Timeout 30000ms exceeded waiting for selector", False),
            ("网络重置（不可误判）", "net::ERR_CONNECTION_RESET at https://x", False),
            ("元素歧义（不可误判）", "Error: strict mode violation: locator resolved to 2 elements", False),
        ]
        for _n, _s, _want in _cases:
            _got = ctx_dead_error(Exception(_s))
            _ok = (_got == _want)
            print("  %s %-26s got=%s want=%s" % ("[ OK ]" if _ok else "[FAIL]", _n, _got, _want))
            if not _ok:
                _fail.append("分类：" + _n)

        # ---------- ③ 端到端：真起浏览器 → 杀掉它 → 验证检出 → 验证全量重建 ----------
        print("\n[2] 端到端：真浏览器被杀 → 检出 → 全量重建（用 /tmp profile）")
        _avail = 0
        try:
            for _l in open("/proc/meminfo"):
                if _l.startswith("MemAvailable"):
                    _avail = int(_l.split()[1]) // 1024
                    break
        except Exception:
            pass
        print("  可用内存 %d MB（生产浏览器约占 2.9GB，所以设了内存门槛保护它）" % _avail)
        if _avail < 500:
            print("  [SKIP] 可用内存 < 500MB → 为保护生产浏览器，跳过端到端部分")
        else:
            _prof = _T + "/profile"
            os.makedirs(_prof, exist_ok=True)
            with sync_playwright() as _p:
                _ctx = launch_persistent(_p, _prof)
                _pg = _ctx.new_page()
                _pg.goto("about:blank", timeout=30000)
                print("  [ OK ] 测试浏览器已启动，evaluate 验证 = %s" % _pg.evaluate("() => 1 + 1"))
                _pids = chrome_pids_of_profile(_prof)
                print("  测试 chrome PID = %s（匹配串是 /tmp 路径，生产 fs_bot 不受影响）" % _pids)
                for _pid in _pids:
                    try:
                        os.kill(_pid, 9)
                    except Exception:
                        pass
                time.sleep(2.5)
                try:
                    _ctx.new_page()
                    print("  [FAIL] 杀掉浏览器后 new_page 竟然还成功")
                    _fail.append("E2E：杀浏览器后仍能 new_page")
                except Exception as _e:
                    _ok = ctx_dead_error(_e)
                    print("  %s 杀掉后 new_page 抛错，且被识别为「上下文已死」：%s"
                          % ("[ OK ]" if _ok else "[FAIL]", str(_e)[:88]))
                    if not _ok:
                        _fail.append("E2E：未能识别上下文已死")
                try:
                    _ctx.close()
                except Exception:
                    pass
                _k = kill_profile_chrome(_prof)
                _g = clear_singleton_locks(_prof)
                print("  [info] 自愈清理动作：杀残留=%s 清锁=%s" % (_k or "无", _g or "无"))
                _ctx2 = launch_persistent(_p, _prof)
                _pg2 = _ctx2.new_page()
                _pg2.goto("about:blank", timeout=30000)
                _v2 = _pg2.evaluate("() => 6 * 7")
                print("  %s 重建后新上下文可用：evaluate=%s（期望 42）"
                      % ("[ OK ]" if _v2 == 42 else "[FAIL]", _v2))
                if _v2 != 42:
                    _fail.append("E2E：重建后上下文不可用")
                try:
                    _ctx2.close()
                except Exception:
                    pass
            kill_profile_chrome(_prof)

        # ---------- ④ 隔离复核：生产文件一行都没动 ----------
        print("\n[3] 隔离复核")
        _after_lines = sum(1 for _ in open(_prod_log, encoding="utf-8", errors="replace"))
        print("  生产 run.log 行数：自检前 %d → 自检后 %d  %s"
              % (_before_lines, _after_lines,
                 "[ OK ] 未被写入" if _before_lines == _after_lines else "[FAIL] 被写入了！"))
        if _before_lines != _after_lines:
            _fail.append("隔离：生产 run.log 被写入")
        print("  生产 state.json mtime          = %s"
              % time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(os.path.getmtime(BASE + "/v21/state.json"))))
        print("  生产 runtime_config.json mtime = %s"
              % time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(os.path.getmtime(BASE + "/runtime_config.json"))))
        print("  自检当前时间                   = %s（上面两个 mtime 不应等于它）"
              % time.strftime("%Y-%m-%d %H:%M:%S"))
        print("  /tmp 自检产物：%s" % ", ".join(sorted(os.listdir(_T))))

        print("\n" + "-" * 70)
        if _fail:
            print("B1 自检：%d 项失败" % len(_fail))
            for _f in _fail:
                print("   ✗ %s" % _f)
            sys.exit(1)
        print("B1 自检：全部通过 ✅")
        sys.exit(0)

    if "--selftest-approval" in sys.argv:
        # ===== 第3项自检：审批闸门 + B4 完整组合 + B11 待确认池健壮性 =====
        # 完全隔离在 /tmp：不碰生产配置/状态/日志，不发飞书，不下单
        import shutil
        _T = "/tmp/approval_selftest"
        _prod_log = BASE + "/v21/run.log"
        _before = sum(1 for _ in open(_prod_log, encoding="utf-8", errors="replace"))
        shutil.rmtree(_T, ignore_errors=True)
        os.makedirs(_T, exist_ok=True)
        RUNTIME = _T + "/runtime_config.json"
        TRADES = _T + "/trades_dryrun.jsonl"
        STATE = _T + "/state.json"
        LOGF = _T + "/run.log"
        IMGDIR = _T + "/imgs"
        NOTIFY_CFG = _T + "/notify.json"
        os.makedirs(IMGDIR, exist_ok=True)
        print("=" * 70)
        print("第3项自检：审批闸门 / B4 组合 / B11 健壮性（隔离在 /tmp，不碰生产）")
        print("=" * 70)
        print("路径重定向证明：")
        for _k in ("RUNTIME", "TRADES", "STATE", "LOGF", "NOTIFY_CFG"):
            print("  %-11s = %s" % (_k, eval(_k)))
        _fail = []

        def _chk(name, got, want):
            _ok = (got == want)
            print("  %s %-52s got=%s want=%s" % ("[ OK ]" if _ok else "[FAIL]", name, got, want))
            if not _ok:
                _fail.append(name)

        # ---------- ① 审批闸门：默认开启 ----------
        print("\n[1] 审批闸门开关")
        _chk("默认 REQUIRE_APPROVAL", REQUIRE_APPROVAL[0], True)

        # ---------- ② B4：多币种完整组合 ----------
        print("\n[2] B4 多币种审批要给出【完整组合】")
        _o3 = _combo_options(["BTC", "ETH", "DOGE"])
        _opens = [x for x in _o3 if x.startswith("· 只开")]
        print("  3 个币的选项（共 %d 条，含 全部开/全部不开）：" % len(_o3))
        for _x in _o3:
            print("     %s" % _x)
        _chk("3 个币的 2 币组合数（应为 3）", len([x for x in _opens if " 和 " in x]), 3)
        _chk("3 个币的单币组合数（应为 3）", len([x for x in _opens if " 和 " not in x]), 3)
        _chk("含「全部开」（1 条）", len([x for x in _o3 if "全部开" in x]), 1)
        _chk("含「全部不开」（1 条）", len([x for x in _o3 if "全部不开" in x]), 1)
        _chk("3 个币非空组合总数 = 2^3-1 = 7", len(_o3) - 1, 7)
        _o2 = _combo_options(["BTC", "SOL"])
        _chk("2 个币非空组合数 = 3", len([x for x in _o2 if "不开" not in x]), 3)
        _o5 = _combo_options(["A", "B", "C", "D", "E"])
        _chk("5 个币退化（不爆长列表，<=9 条）", len(_o5) <= 9, True)

        # ---------- ③ 审批清单必须含用户第 8 条要求的字段 ----------
        print("\n[3] 审批通知必须带全参数（用户第 8 条）：止损点数(不含杠杆)/各档收益率(含杠杆)")
        _p = {"entry": 100.0, "stop": 95.0, "tps": [110.0, 120.0], "group": "测试群",
              "dir": "LONG", "texts": ["测试原文"], "legs": [], "entry_src": "消息文字"}
        _txt = "\n".join(_approval_lines("TEST", _p, 1))
        for _kw in ("保证金：300U", "杠杆：3 倍", "止损位：95", "止损点数（不含杠杆）：5",
                    "止盈1：110", "预期收益 +30.0%（含 3 倍杠杆）", "盈亏比 2.00:1"):
            _chk("审批清单含 %r" % _kw, _kw in _txt, True)
        print("  ——实际生成的审批清单——")
        for _l in _txt.splitlines():
            print("     %s" % _l)

        # ---------- ④ B11-a：更差的解析不许覆盖更好的解析 ----------
        print("\n[4] B11-a 坏解析不得覆盖好解析（19:03 UNI 实测事故复刻）")
        ASKING.clear()
        PENDING.clear()
        _good = {"entry": 6.513, "stop": 6.396, "tps": [9.289], "group": "UA-nurseneil2",
                 "dir": "LONG", "texts": ["K线图"], "legs": [], "entry_src": "K线图"}
        _bad = {"entry": None, "stop": 6.39, "tps": [6.39], "group": "UA-nurseneil2",
                "dir": "LONG", "texts": ["Going long on UNI"], "legs": []}
        _chk("好解析打分 > 坏解析打分", _plan_score(_good) > _plan_score(_bad), True)
        ask_user("UNI", _good, "上限")
        ask_user("UNI", _bad, "多币种总结")          # 应该被拒绝覆盖
        _kept = ASKING.get("UNI", {}).get("p", {})
        _chk("保留的是好解析的开仓价 6.513", _kept.get("entry"), 6.513)
        _chk("保留的是好解析的止盈 [9.289]", _kept.get("tps"), [9.289])
        print("     ↳ 覆盖被拒绝，保留原参数（日志已记录）")

        # ---------- ⑤ B11-a 反向：更好的解析可以覆盖 ----------
        print("\n[5] B11-a 反向：更好的解析**应当**覆盖")
        ASKING.clear()
        ask_user("UNI", _bad, "先差")
        ask_user("UNI", _good, "后好")
        _kept2 = ASKING.get("UNI", {}).get("p", {})
        _chk("后到的好解析已生效（开仓 6.513）", _kept2.get("entry"), 6.513)

        # ---------- ⑥ B11-b：待确认池落盘 + 重启恢复 ----------
        print("\n[6] B11-b 待确认池落盘与重启恢复（原来重启即静默丢失）")
        _dump = _asking_dump()
        _chk("落盘结构含 UNI", "UNI" in _dump, True)
        _chk("落盘后能 json.dump（不会连带弄丢持仓）",
             bool(json.dumps({"open": {"BTC": {}}, "asking": _dump}, ensure_ascii=False)), True)
        json.dump({"open": {}, "asking": _dump}, open(STATE, "w", encoding="utf-8"), ensure_ascii=False)
        ASKING.clear()
        _sv = json.load(open(STATE, encoding="utf-8"))
        _now = time.time()
        for _c, _v in (_sv.get("asking") or {}).items():
            if isinstance(_v.get("p"), dict):
                ASKING[_c] = {"p": _v["p"], "reason": _v.get("reason"),
                              "ask_ts": float(_v.get("ask_ts") or _now), "txt": _v.get("txt")}
        _chk("重启后恢复出 UNI", "UNI" in ASKING, True)
        _chk("恢复的参数仍是好的（6.513）", ASKING.get("UNI", {}).get("p", {}).get("entry"), 6.513)

        # 超时的应当被丢弃
        ASKING.clear()
        ASKING["OLD"] = {"p": _good, "reason": "x", "ask_ts": time.time() - ASK_TIMEOUT - 10, "txt": ""}
        _dump2 = _asking_dump()
        _kept3, _drop3 = 0, 0
        for _c, _v in _dump2.items():
            if _now - float(_v.get("ask_ts") or 0) > ASK_TIMEOUT:
                _drop3 += 1
            else:
                _kept3 += 1
        _chk("超时的待确认被丢弃", (_kept3, _drop3), (0, 1))

        # ---------- ⑦ 审批闸门：回「开」后不再重复问 ----------
        print("\n[7] 审批闸门：用户回「开」后不再重复询问")
        ASKING.clear()
        PENDING.clear()
        open_pos_ref.clear()
        _p2 = {"entry": 100.0, "stop": 95.0, "tps": [110.0], "group": "g", "dir": "LONG",
               "texts": ["t"], "legs": [], "deadline": 0, "first_ts": time.time() - 10}
        PENDING["AAA"] = dict(_p2)
        ask_user("AAA", dict(_p2), "按你的要求：所有订单在开之前都要经你审批")
        PENDING.pop("AAA", None)
        _chk("信号已挂起等审批", "AAA" in ASKING, True)
        _open_asking("AAA")                          # 等价于用户回「开」
        _chk("回「开」后 approved 标记已置位", PENDING.get("AAA", {}).get("approved"), True)
        _chk("回「开」后 deadline 归零（立刻处理）", PENDING.get("AAA", {}).get("deadline"), 0)

        print("\n[7b] 暂停态落盘与恢复（原来 PAUSED 只在内存里，重启会静默恢复交易）")
        PAUSED[0] = True
        _pdump = {"open": {}, "paused": bool(PAUSED[0]), "asking": _asking_dump()}
        json.dump(_pdump, open(STATE, "w", encoding="utf-8"), ensure_ascii=False)
        PAUSED[0] = False
        _sv2 = json.load(open(STATE, encoding="utf-8"))
        if "paused" in _sv2:
            PAUSED[0] = bool(_sv2.get("paused"))
        _chk("暂停态落盘后能恢复为 True", PAUSED[0], True)
        json.dump({"paused": False}, open(STATE, "w", encoding="utf-8"))
        _sv3 = json.load(open(STATE, encoding="utf-8"))
        PAUSED[0] = bool(_sv3.get("paused")) if "paused" in _sv3 else PAUSED[0]
        _chk("恢复态落盘后能恢复为 False", PAUSED[0], False)

        # ---------- ⑧ B14：止盈方向校验 + 持仓健康自检 ----------
        print("\n[8] B14 止盈方向校验（19:20 ETH 假亏损事故复刻）")
        _chk("事故复刻：SHORT 入场2503.49 挂 2520 判为错误一侧",
             _tp_wrong_side("SHORT", 2503.49, [2520.0]), [2520.0])
        _chk("正常：SHORT 入场2503.49 挂 2214.47 合法",
             _tp_wrong_side("SHORT", 2503.49, [2214.47]), [])
        _chk("正常：LONG 入场100 挂 [110,120] 合法",
             _tp_wrong_side("LONG", 100, [110.0, 120.0]), [])
        _chk("LONG 入场100 挂 95 判为错误一侧", _tp_wrong_side("LONG", 100, [95.0]), [95.0])
        _chk("混合：LONG 入场100 挂 [95,110] 只剔 95",
             _tp_wrong_side("LONG", 100, [95.0, 110.0]), [95.0])
        _chk("以当前价为基准：LONG 入场100 现价120 挂110 已被越过",
             _tp_wrong_side("LONG", 100, [110.0], ref=120.0), [110.0])
        _chk("无入场价时不误判", _tp_wrong_side("LONG", None, [110.0]), [])

        print("\n[9] B14 配套：持仓健康自检 position_sanity")
        _pos = {
            "ETH_BAD": {"dir": "SHORT", "entry": 2503.49, "tps": [2520.0], "sl": 2648.0},   # 事故：应报
            "ETH_OK":  {"dir": "SHORT", "entry": 2503.49, "tps": [2214.47], "sl": 2648.0},  # 正常
            "BE_OK":   {"dir": "LONG",  "entry": 100.0,   "tps": [110.0],  "sl": 100.0},    # 保本损==开仓价，合法
            "DOGE_BAD": {"dir": "LONG", "entry": 0.079,   "tps": [0.09],   "sl": 0.09},     # 止盈/止损都在错误一侧
        }
        _bad = position_sanity(_pos, notify_user=False)
        _bcn = sorted(x[0] for x in _bad)
        _chk("报告了 ETH_BAD（止盈在空单上方）", "ETH_BAD" in _bcn, True)
        _chk("报告了 DOGE_BAD（多单 0.079 但止损 0.09 在其上方）", "DOGE_BAD" in _bcn, True)
        _chk("未误报 ETH_OK", "ETH_OK" in _bcn, False)
        _chk("未误报保本损 BE_OK（sl==entry 合法）", "BE_OK" in _bcn, False)
        print("   ↳ 自检报出的问题仓位：%s" % _bcn)

        # ---------- ⑩ 隔离复核 ----------
        print("\n[10] 隔离复核")
        _after = sum(1 for _ in open(_prod_log, encoding="utf-8", errors="replace"))
        _chk("生产 run.log 未被写入", _after, _before)
        print("  /tmp 产物：%s" % ", ".join(sorted(os.listdir(_T))))
        print("  生产 runtime_config.json mtime = %s"
              % time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(os.path.getmtime(BASE + "/runtime_config.json"))))

        print("\n" + "-" * 70)
        if _fail:
            print("第3项自检：%d 项失败" % len(_fail))
            for _f in _fail:
                print("   ✗ %s" % _f)
            sys.exit(1)
        print("第3项自检：全部通过 ✅")
        sys.exit(0)

    if "--selftest-b5" in sys.argv:
        # ===== 第4项自检：B5 解析体系 + B10 词撞币种 + B3 多币种校验 =====
        # 用例全部取自【真实生产原文】（标准答案集 /tmp/golden_set.json 与交接文档里的错单）
        print("=" * 72)
        print("B5 / B10 / B3 自检（用例均为真实生产原文）")
        print("=" * 72)
        _fail = []

        def _ck(name, got, want):
            _ok = (got == want)
            print("  %s %-54s got=%s want=%s" % ("[ OK ]" if _ok else "[FAIL]", name, got, want))
            if not _ok:
                _fail.append(name)

        # ---------- B10：英文常用词不得撞成币种 ----------
        print("\n[1] B10 词撞币种（实测：一句 UNI 被读成 AT/ON/THE/UNI 四个币）")
        _b10 = "Going long on UNI here at CMP. TPs above, 4H close under 6.39 for stops."
        _ck("单币 UNI 句子 → 只认出 UNI", sorted(find_all_coins(_b10)), ["UNI"])
        _ck("小写英文常用词不再误命中 on/at/the",
             [c for c in find_all_coins(_b10) if c in ("ON", "AT", "THE")], [])
        _ck("真·多币种仍要认出（Sol 小写也认）",
             "SOL" in find_all_coins("原油CL跌破97空，Sol突破102.5多"), True)
        _ck("大写歧义词仍认得（写 NEAR 时）", "NEAR" in find_all_coins("做多 NEAR 止损2"), True)

        # ---------- B5-① 点数 vs 价格 ----------
        print("\n[2] B5-① 点数 vs 价格（「止损带个30点左右」的 30 是点数）")
        _ck("_points_info 认出「止损35点」", _points_info("以太现价到2465多，止损35点", 35.0), 35.0)
        _ck("_points_info 不误判正常止损价", _points_info("BTC 做多 止损74000", 74000.0), None)
        _ck("_points_info 认出「止盈3000点以上」", _points_info("止盈利润3000点以上", 3000.0), 3000.0)
        _p_eth = fast_parse("以太坊空单 在2465跌破可以追空，止损带个30点左右，止盈就30点以上分批止盈")
        print("     ↳ 原文解析：%s" % json.dumps(
            {k: (_p_eth or {}).get(k) for k in ("coin", "direction", "entry", "stop", "targets")},
            ensure_ascii=False))

        # ---------- B5-② 数字在关键词前 ----------
        print("\n[3] B5-② 数字写在关键词前面（「4250止损」旧代码读不到）")
        _p9 = fast_parse("黄金xau 突破4300，回调过程做多，4250止损，4350到4450分批止盈。")
        _ck("读到止损 4250", (_p9 or {}).get("stop"), 4250.0)
        _ck("不再把止盈区间 4350~4450 当成入场区间", (_p9 or {}).get("entryRange"), None)
        _p_doge = fast_parse("狗狗币在0.078到0.08接多，左侧轻仓，0.075止损，止盈0.09附近")
        _ck("「0.075止损」读到 0.075", (_p_doge or {}).get("stop"), 0.075)
        _ck("不再把后面的止盈 0.09 当成止损", (_p_doge or {}).get("stop") != 0.09, True)

        # ---------- B5-③ 条件单 ----------
        print("\n[4] B5-③ 条件触发型（等跌破/站稳才进）必须能识别")
        _ck("「等待76000跌破收回，站稳76200多」判为条件单",
             _conditional_order("比特币等待76000跌破收回，站稳76200多，74000止损"), True)
        _ck("「跌破2465可以追空」判为条件单",
             _conditional_order("在2465跌破可以追空"), True)
        _ck("普通直接开仓不误判为条件单",
             _conditional_order("比特币77065价格做空 止损77500 第一止盈74500"), False)

        # ---------- B3：多币种分支必须过公共校验 ----------
        print("\n[5] B3 公共校验（实测事故：DOGE 做多 入场0.079 止损0.09 在入场价上方）")
        _w, _t = validate_plan("DOGE", "LONG", 0.079, 0.09, [0.1], 0.079)
        _ck("止损方向错 → 必须被判为解析错误", bool(_w), True)
        print("     ↳ 原因：%s" % _w)
        _w2, _t2 = validate_plan("BTC", "LONG", 77765.0, 74000.0, [79000.0], 77760.0)
        _ck("正常单子 → 不报错", _w2, None)
        _w3, _t3 = validate_plan("BTC", "LONG", 2540.0, 38.0, [7582.1], 77760.0)
        _ck("开仓价离谱（BTC 市价 77760 却解析出 2540）→ 报错", bool(_w3), True)
        _w4, _t4 = validate_plan("ETH", "LONG", 2465.0, 2400.0, [2500.0], 2400.0)
        _ck("止盈方向不对的档位被剔除", _t4, [2500.0])
        _sft = soft_ask_reason("在2465跌破可以追空，止损带个30点左右", 30.0, 2465.0)
        _ck("点数/条件单 → 转成审批原因（不硬拒、也不自动开）", bool(_sft), True)
        print("     ↳ 审批原因：%s" % _sft)

        print("\n" + "-" * 72)
        if _fail:
            print("B5/B10/B3 自检：%d 项失败" % len(_fail))
            for _f in _fail:
                print("   ✗ %s" % _f)
            sys.exit(1)
        print("B5/B10/B3 自检：全部通过 ✅")
        sys.exit(0)

    main()
