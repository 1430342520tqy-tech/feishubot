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


# ---------------- 取消息层：官方 API（首选） / 爬网页（兜底）----------------
# 用户 2026-09-16 授权了「用户身份」权限，于是机器人可以走飞书官方 API 取消息：
#   · 不需要把机器人拉进群（那些 KOL 群是别人的，拉不进去）
#   · 不需要浏览器 → 不再有"开页失败"、重启盲窗从 4.5 分钟降到几秒、省约 2.7GB 内存
#   · 不受飞书网页前端改版/崩溃影响（9/15、9/16 已被它坑了三次）
#   · 图片是**原图**（实测与网页 blob 抓下来的字节完全相同）
# 浏览器那条路完整保留：runtime_config.json 里 `fetch_mode` 改成 "browser" 即可一键回滚（热加载）。
try:
    import feishu_api as fapi
    fapi.set_logger(log)
    _FAPI_OK, _FAPI_ERR = True, ""
except Exception as _e:
    fapi = None
    _FAPI_OK, _FAPI_ERR = False, str(_e)[:120]

FETCH_MODE = "api"                 # api | browser，由 runtime_config.json 的 fetch_mode 决定
API_READY = [False]                # 启动自检通过后才置 True
API_CURSOR = {}                    # 群 -> 已处理到的 create_time(毫秒)
API_LAST = {}                      # 群 -> 上次轮询时间（控制轮询频率，别把接口打爆）
API_FAILS = [0]                    # 连续失败次数（用于告警/自动回退）


def _row_t_sig(r):
    """消息的"发出时间"（秒）。API 行直接带 t_sig；网页行用 message-id 高位算。"""
    try:
        if r.get("t_sig"):
            return int(r["t_sig"])
        return int(r.get("id") or 0) >> 32
    except Exception:
        return int(time.time())


def api_bootstrap():
    """启动时自检官方 API：可用就把 API_READY 置 True（并跳过浏览器）。"""
    global API_CURSOR
    if not _FAPI_OK:
        log("   [取消息] 官方 API 模块不可用（%s）→ 用浏览器兜底" % _FAPI_ERR)
        return False
    ok, why = fapi.health()
    if not ok:
        log("   [取消息] 官方 API 自检未通过：%s" % why)
        # 令牌类失败 → 直接把"重新授权链接"发给他（用户要求：刷新失败要提醒重新授权）
        _hint = ""
        if ("令牌" in why) or ("授权" in why) or ("token" in why.lower()):
            try:
                _hint = "\n\n" + fapi.reauth_hint()
            except Exception:
                _hint = ""
        notify("⚠️【取消息】飞书官方 API 自检未通过：%s\n"
               "机器人会先用浏览器兜底（如果浏览器也打不开，就会暂时收不到群消息）。%s" % (why, _hint))
        return False
    log("   [取消息] 官方 API 自检通过：%s" % why)
    ids = fapi.resolve_chat_ids(GROUPS)
    miss = [g for g in GROUPS if g not in ids]
    if miss:
        log("   [取消息] ⚠️ 这些群在 API 里没找到：%s" % "、".join(miss))
        notify("⚠️【取消息】这些监控群在飞书 API 里没找到：%s\n（群名要和飞书里完全一致，或你已退出该群）"
               % "、".join(miss))
    API_CHAT_IDS.clear()
    API_CHAT_IDS.update(ids)
    API_READY[0] = True
    return True


API_CHAT_IDS = {}


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
# ⚠️ 用户 2026-09-15 明确要求：熔断**按金额**，不按笔数 → MAX_CONSEC_LOSS 默认 0（=不启用）
MAX_CONSEC_LOSS = 0           # 连亏笔数熔断：0 = 关闭（仅保留计数用于展示）
# 单日净亏熔断（绝对金额）。**默认关闭**：用户 09-15 选了"按总敞口的百分比熔断"，
# 若这里再留 300U，它会在 420U 之前先触发、让你选的规则失效（我实测踩到过）。要用就填数字。
DAILY_LOSS_LIMIT = 0.0
# **主熔断线**：总敞口上限（max_open × 保证金）的 20%。现在 7×300=2100U → 熔断线 420U
LOSS_LIMIT_PCT = [0.20]
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
    # ===== 用户 2026-09-15 明确要求：熔断**按金额**，不按笔数 =====
    # 基准 = 总敞口上限（max_open × 保证金，现在 7 × 300 = 2100U），亏到它的 20%（=420U）即熔断。
    # 连亏笔数熔断已关闭（MAX_CONSEC_LOSS 默认 0 → 下面那个分支不再触发），
    # 但计数仍保留用于「状态」展示。
    if MAX_CONSEC_LOSS > 0 and RISK["consec_loss"] >= MAX_CONSEC_LOSS:
        _hits.append("连续亏损 %d 笔（上限 %d）" % (RISK["consec_loss"], MAX_CONSEC_LOSS))
    if DAILY_LOSS_LIMIT > 0 and RISK["day_pnl"] <= -abs(DAILY_LOSS_LIMIT):
        _hits.append("今日已实现净亏损 %.2fU（上限 %.0fU）" % (RISK["day_pnl"], DAILY_LOSS_LIMIT))
    _cap = _exposure_cap()
    if LOSS_LIMIT_PCT[0] > 0 and _cap > 0:
        _line = -abs(_cap * float(LOSS_LIMIT_PCT[0]))
        if RISK["day_pnl"] <= _line:
            _hits.append("今日已实现净亏损 %.2fU ｜ 熔断线 = 总敞口上限 %.0fU 的 %.0f%% = %.0fU"
                         % (RISK["day_pnl"], _cap, float(LOSS_LIMIT_PCT[0]) * 100, abs(_line)))
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
        # 用户 2026-09-16 明确要求：交易所有、纸面没有的仓 = **用户手工仓，机器人不得有任何干涉**
        MANUAL_COINS.add(s[:-4] if s.endswith("USDT") else s)
    # 🆕 2026-09-17：名单只会加不会减 → 按交易所实时持仓把"已经平仓的"移出去（用户实测报障）
    _stale = _manual_prune(rsyms)
    if _stale:
        notify("ℹ️【手工仓护栏】已把**已经平仓**的币从名单移除：%s\n"
               "（按交易所实时持仓核对；这些币以后有新信号会照常走审批）" % "、".join(_stale))
    if MANUAL_COINS:
        log("   [手工仓] 已登记 %d 个币为你的手工仓（机器人不会对它们发任何真单）：%s"
            % (len(MANUAL_COINS), "、".join(sorted(MANUAL_COINS))))
        STATE_DIRTY[0] = True
        for _c in sorted(MANUAL_COINS):
            notify("ℹ️【手工仓护栏】检测到交易所有你的手工仓：**%s**\n"
                   "机器人不会对它做任何事（不开新仓、不平仓、不改止损、也不拿它的数量去挂保护）。\n"
                   "手工仓平掉后发「解除手工仓 %s」即可解除这条护栏。" % (_c, _c))
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


# ===== 手工仓护栏（用户 2026-09-16 明确要求：机器人在交易所有手工仓时必须毫无干涉）=====
# 背景：用户在币安手工开了 LSKUSDT 多单（588 张）。机器人若对它做任何事都会变成"干涉"：
#   · 用交易所持仓数量去挂止损 → 止损会覆盖到用户手工仓的数量（M4 的 _real_qty_or_estimate 有这个能力！）
#   · 平仓/减仓/改止损 → 直接动用户的手工仓
#   · 同币再开一笔真仓 → 在双向持仓下与手工仓同向合并，等于加仓用户的仓
# 所以：对账时把"交易所有仓、纸面无记录"的币记入 MANUAL_COINS，落盘保存；
#       任何真实下单动作（开/平/减/移止损）遇到这些币 → **一律硬拦 + 告警**，绝不发单。
MANUAL_COINS = set()


def manual_guard(coin, action="真实下单"):
    """该币是否有用户手工仓 → 返回拦截原因（有的话）。"""
    c = (coin or "").upper()
    if c and c in MANUAL_COINS:
        return ("%s 在交易所有**你的手工仓**（机器人不干涉），所以拒绝执行「%s」"
                % (c, action))
    return None


def manual_block(coin, action):
    """拦下来并告警（只告警、不下单）。"""
    why = manual_guard(coin, action)
    if not why:
        return False
    log("   🛑 [手工仓护栏] %s" % why)
    notify("🛑【手工仓护栏】%s\n机器人**不会碰这个币的任何真实仓位**（你的手工仓）。\n"
           "（纸面记录/信号解析照常，只是不发真单。手工仓平掉后发「解除手工仓 %s」即可解除。）"
           % (why, (coin or "").upper()))
    return True


def _manual_dump():
    return sorted(MANUAL_COINS)


def _manual_prune(real_syms):
    """按**交易所实时持仓**核对手工仓名单，把已经平仓的移出去。返回被移除的币（已排序）。

    🆕 2026-09-17 用户实测报障：名单里还留着 LSK / XAU / XAUT（他早已平仓），
    原因是这套名单**只会加、不会减**（每次启动从 state.json 原样恢复 + 只 add 新发现的），
    唯一删除途径是手动发「解除手工仓 X」。危害不只是显示：
    护栏里的币会被 `manual_block` **拒绝开新仓** → 这三个币即使来了真信号也会被拒。
    ⚠️ 只在"读交易所持仓成功"时才会被调用（读失败时 startup_reconcile 已经 return，不会误删）。
    """
    _have = {str(s)[:-4] if str(s).endswith("USDT") else str(s) for s in (real_syms or [])}
    _stale = sorted(c for c in list(MANUAL_COINS) if c not in _have)
    for _c in _stale:
        MANUAL_COINS.discard(_c)
    if _stale:
        log("   [手工仓] 按交易所实时持仓移除已平仓的 %d 个：%s（名单现在=%s）"
            % (len(_stale), "、".join(_stale), "、".join(sorted(MANUAL_COINS)) or "空"))
        STATE_DIRTY[0] = True
    return _stale


def real_plan_open(coin, dirc, entry, stop, tps, margin=None):
    """开仓 → 交给真实下单层生成完整计划（市价/限价腿 + 各档止盈 + Algo 止损）"""
    if not _BEXEC_OK:
        return None
    # ===== 手工仓护栏：用户的仓，绝不动（优先级高于一切）=====
    if bexec.LIVE[0] and manual_block(coin, "开新仓"):
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
    if bexec.LIVE[0] and manual_block(coin, "改止损"):
        return None
    return bexec.sync_sl(coin.upper() + "USDT", dirc, new_stop, qty)


def real_plan_close(coin, dirc, qty=None):
    """平仓/减仓 → 真实层对应动作"""
    if not _BEXEC_OK:
        return None
    if bexec.LIVE[0] and manual_block(coin, "平仓/减仓"):
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
    评估 M4：实盘下若用纸面公式估数量，可能偏大（被拒）或偏小（只保护一部分仓位）。
    ⚠️ 手工仓护栏：该币若有用户手工仓，**绝不允许**把交易所数量（含手工仓）拿来挂止损 ——
       否则机器人的止损会覆盖到用户手工仓的数量，那就是"干涉"。"""
    sym = coin.upper() + "USDT"
    if manual_guard(coin):
        log("   🛑 [手工仓护栏] %s 有你的手工仓 → 止损数量只用纸面估算，绝不用交易所数量" % coin)
        return real_qty_estimate(tr["entry"], tr.get("remaining", 1.0))
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

def _coverage(px, w, y0, y1, bg=(2, 24, 21)):
    """一行里"与背景色差异足够大"的像素占比（= 这条线上有多少像素是被画出来的）。
    🆕 2026-09-17：背景色改成**可传参** —— 浅色主题（白底）的图必须传白底，
    否则"与深色底不同"对白底恒成立，覆盖率会恒等于 1.0，线判定全乱（实测过）。"""
    xa, xb = int(w * 0.06), int(w * 0.72)
    best = 0.0
    for yy in range(y0, y1):
        cnt = tot = 0
        for x in range(xa, xb, 2):
            r, g, b = px[x, yy]
            tot += 1
            if abs(r - bg[0]) + abs(g - bg[1]) + abs(b - bg[2]) > 30: cnt += 1
        if tot and cnt / tot > best: best = cnt / tot
    return round(best, 3)

# 🆕 2026-09-17 清理：原来的 `_vision(img_path)` 是**死代码**（定义后无人调用），
#   而且它把图片格式写死成 PNG（实际是 jpg）。已删除；读标签走 `_ocr_tags_batch` /
#   `_ocr_one_label`，浅色图的价格走 `_edge_price_by_vision`。



def _ocr_one_label(im, x0, t):
    """**单个标签单独一次 OCR**（用户 2026-09-16 选定方案）。
    为什么必须这样：批量 OCR 是"拼图 + 黄色序号"，模型一旦漏答/错位，
    `nums.get(str(i))` 就会把数值贴到**错误的标签**上 —— 实测同一张图多跑几次，
    同一个 y 上的标签会拿到不同数值、甚至整张图标签全丢。单标签裁图没有"序号→数值"这一步，
    从机制上消除了错位，代价是每个关键标签多一次调用（只对关键标签做，见 read_chart）。"""
    from PIL import Image as _I
    box = (max(0, x0 + t["x1"] - 5), max(0, t["y1"] - 5),
           min(im.width, x0 + t["x2"] + 6), min(im.height, t["y2"] + 6))
    try:
        crop = im.crop(box)
        if crop.width < 8 or crop.height < 4:
            return None
        crop = crop.resize((crop.width * 3, crop.height * 3), _I.LANCZOS)
        buf = RUN + "/tmp_one_label.png"
        crop.save(buf)
        b64 = base64.b64encode(open(buf, "rb").read()).decode()
        body = {"model": "deepseek-v4-flash-vision-exp", "temperature": 0,
                "messages": [{"role": "system",
                              "content": "You transcribe the price number printed in this chart screenshot crop. STRICT JSON only."},
                             {"role": "user", "content": [
                                 {"type": "text", "text": "This crop contains ONE price label from a price axis. "
                                  "Return {\"value\": <number>} with the number exactly as printed. "
                                  "If you cannot read any number, return {\"value\": null}."},
                                 {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}]}
        r = requests.post(DS_API, headers={"Authorization": "Bearer " + DS_KEY,
                                           "Content-Type": "application/json"}, json=body, timeout=45)
        m = re.search(r"\{[\s\S]*\}", r.json()["choices"][0]["message"]["content"])
        v = json.loads(m.group(0)).get("value")
        return float(str(v).replace(",", "").replace("$", "").strip())
    except Exception:
        return None


def _same_number_loose(v1, v2):
    """两次读数是不是**同一个数**（容忍小数点/千分位解析差异）。
    🆕 2026-09-17 实测（黄金群那张浅色图）：同一个标签，两次读数分别是
      `4257.925` 与 `4257925.0` —— 数字序列完全相同，只是小数点在解析时丢了。
    旧逻辑按"相对差 ≤0.5%"判 → 差 1000 倍 → 判"不一致 → 未读到"，
    结果**整张图的开仓/止损/止盈全部读不出**。这里按"数字序列"再判一次。"""
    d1 = re.sub(r"[^0-9]", "", str(v1))
    d2 = re.sub(r"[^0-9]", "", str(v2))
    return bool(d1) and d1 == d2


def two_read_ok(v_batch, v_single, tol=0.005):
    """两次**互相独立**的读数（批量拼图 / 单标签单独读）是否一致。
    返回 (是否可用, 采用值, 说明)。用户要求：不一致就标「未读到」，绝不猜。"""
    if v_single is None and v_batch is None:
        return False, None, "两次都没读出来"
    if v_single is None:
        return True, float(v_batch), "只有批量读数"
    if v_batch is None:
        return True, float(v_single), "只有单标签读数"
    v1, v2 = float(v_batch), float(v_single)
    if abs(v1 - v2) / max(abs(v2), 1e-9) <= tol:
        return True, v2, "两次一致"
    if _same_number_loose(v_batch, v_single):
        # 数字序列一致、只是小数点位置不同 → 取带小数点的那个（图上价格都带小数）
        pick = v1 if ("." in str(v_batch) and "." not in str(v_single)) else v2
        return True, pick, "两次数字序列一致（小数点解析差异）"
    return False, None, "两次读数不一致（%s vs %s）→ 按未读到处理" % (v1, v2)


def drop_nonmonotonic(tags):
    """价格随 y 增大必须单调下降（价格轴的几何性质）。返回被剔除的 tags。
    ⚠️ 前提：tags 必须来自**同一张图**的横线，且已按 y 排序。"""
    order = sorted([t for t in tags if t.get("y") is not None and t.get("value")],
                   key=lambda t: t["y"])
    drop = []
    for i in range(1, len(order)):
        if order[i]["value"] >= order[i - 1]["value"]:
            # 越靠下反而越贵 → 至少有一个读错了：剔除覆盖率低的那个（更可能是误读/短线干扰）
            bad = order[i - 1] if order[i - 1].get("cov", 0) < order[i].get("cov", 0) else order[i]
            if bad not in drop:
                drop.append(bad)
    return drop


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
                              "You MUST return exactly %d entries, one per index. Only report digits you can actually read." % (len(tiles), len(tiles))},
                             {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}]}
    # ⚠️ 2026-09-16 实测（同一份代码、同一张图，跑两次结果就不同）：
    #    批量 OCR 是"拼图 + 黄色序号"，一旦模型漏答/错位，`nums.get(str(i))` 就会把**数值对到
    #    错误的标签上**，或者整张图的标签全丢（实测 7685319584487951562_0.png：
    #    第一次读出 sl=0.0/entry=0.04/tps=[88.0]，第二次直接变成"无红色止损标签"）。
    #    对策（最小改动）：返回条数不等于标签数就重试一次，并把"没读全"写进日志 ——
    #    绝不静默丢标签（项目原则「绝不静默丢弃」）。
    def _ask_once():
        r = requests.post(DS_API, headers={"Authorization": "Bearer " + DS_KEY,
                                           "Content-Type": "application/json"},
                          json=body, timeout=180)
        m = re.search(r"\{[\s\S]*\}", r.json()["choices"][0]["message"]["content"])
        return json.loads(m.group(0))

    nums = {}
    for _att in (1, 2):
        try:
            nums = _ask_once() or {}
        except Exception as e:
            log("   批量读标签失败（第 %d 次）: %s" % (_att, str(e)[:90]))
            nums = {}
        if len(nums) >= len(tiles):
            break
        # ⚠️ 性能（2026-09-16）：原来"缺一个就重试"会几乎每次都多打一次调用（5~15 秒/次）。
        #    改成**只回来不到一半**才重试；个别缺的由"关键标签单读复核"那一层补回来。
        if len(nums) >= max(1, len(tiles) // 2):
            log("   ↳ 批量读标签缺 %d 个（不足一半，不再重试；关键标签会单独复核）"
                % (len(tiles) - len(nums)))
            break
        log("   ⚠️ 批量读标签大面积缺失：标签 %d 个，只回来 %d 个 → %s"
            % (len(tiles), len(nums), "重试一次" if _att == 1 else "仍不完整，按读到的用（不静默丢）"))
    return nums

# ===== 读图：横线判定为"止盈线"的最低覆盖率 =====
# ⚠️ 2026-09-15 实测（真实图 7685715944936623383_0.png，UNI 那张）：
#    图上三条横线的实测覆盖率是 0.743 / 0.787 / 0.863 —— 原来的门槛 0.85
#    把前两档（7.180、8.216）直接判掉了，只读出 9.289 → **TP1/TP2 静默丢失**。
#    横线被 K 线遮挡时覆盖率天然会低于 1.0，所以门槛必须按"明显长于蜡烛"来定，
#    不能要求 85% 满幅。0.55 保留了对零星色块/标签的过滤能力。
TP_MIN_COV = 0.55

# ===== 几何读图（用户 2026-09-16：「图明明很清楚，为什么会读不出」）=====
# 实测根因（把用户发的那张原始图逐像素量出来的）：
#   ① TradingView 多单工具画的是**半透明实心矩形**：绿框填充≈(27,77,40)、红框≈(96,36,38)。
#      现有 _cls 要 g>110 / r>150 才认绿/红 → **对填充一律返回 None，等于看不见框**，
#      于是旧规则（"红色横线=止损"）只能靠旁边那些文字标签去猜角色。
#   ② 标签的 y 会被 TradingView **挪位防重叠**：实测 6.719/6.676/6.513/6.396 四个标签的
#      y 间距是 71/50/51（几乎相等），但价格差是 0.043/0.163/0.117 →
#      **标签的 y 已经不代表它那条线的 y**，"按标签 y 猜角色"必错。
#   ③ 价格轴是**线性**的（用三条分得开的止盈线拟合，斜率 0.002561 vs 0.002596，一致）。
# 正确语义（= TradingView 多单工具的通用语义）：
#      红框下边 = 止损 ｜ 红绿框交界 = 开仓 ｜ 绿框上边 = 末档目标 ｜ 绿框内长横线 = 分档止盈
#   角色**由几何决定**；数字优先取"与该边几何值最接近的标签"（标签是精确价位），
#   轴换算做交叉校验；差太多就标「未读到」——不猜。
FILL_MAX_BRIGHT = 160      # 填充是暗的（≤160）；EMA 曲线/蜡烛是亮的（>200）→ 用它把"框"和"线"分开


def _fill_kind(r, g, b):
    """半透明工具色框的填充色判定（背景约 (2,24,21)）。"""
    if abs(r - 2) + abs(g - 24) + abs(b - 21) <= 30:
        return None
    if max(r, g, b) > FILL_MAX_BRIGHT:
        return None
    if g - r >= 6 and g - b >= 8 and g >= 30:
        return "green"
    if r - g >= 8 and r - b >= 8 and r >= 30:
        return "red"
    return None


def find_fill_boxes(px, w, h):
    """找工具画的色框：返回 {'green': (x0,y0,x1,y1), 'red': (...)}（只取最长的连续 y 段）。"""
    out = {}
    for kind in ("green", "red"):
        rows = {}
        for y in range(h):
            xs = [x for x in range(0, w, 2) if _fill_kind(*px[x, y]) == kind]
            if len(xs) >= max(6, int(w * 0.01)):
                rows[y] = (min(xs), max(xs), len(xs))
        if len(rows) < max(8, int(h * 0.015)):
            continue
        ys = sorted(rows)
        best, cur = None, [ys[0], ys[0]]
        for y in ys[1:]:
            if y - cur[1] <= 4:
                cur[1] = y
            else:
                if best is None or cur[1] - cur[0] > best[1] - best[0]:
                    best = list(cur)
                cur = [y, y]
        if best is None or cur[1] - cur[0] > best[1] - best[0]:
            best = list(cur)
        y0, y1 = best
        mid = [rows[y] for y in ys if y0 <= y <= y1]
        x0 = sorted(a for a, _, _ in mid)[len(mid) // 2]
        x1 = sorted(b for _, b, _ in mid)[len(mid) // 2]
        out[kind] = (x0, y0, x1, y1)
    return out


def fit_axis_scale(pairs):
    """(y, 价格) 线性拟合 + 剔除离群。返回 {'a','b','n','max'} 或 None。"""
    cur = [(float(y), float(v)) for y, v in pairs if v]
    if len(cur) < 2:
        return None
    for _ in range(3):
        if len(cur) < 2:
            return None
        xs = [p[0] for p in cur]
        ys = [p[1] for p in cur]
        n = len(xs)
        mx = sum(xs) / n
        my = sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx <= 0:
            return None
        a = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
        b = my - a * mx
        res = [abs((a * y + b) - v) / v for y, v in cur]
        if max(res) <= 0.008 or len(cur) <= 2:
            return {"a": a, "b": b, "n": n, "max": max(res)}
        worst = max(range(len(res)), key=lambda i: res[i])
        cur.pop(worst)
    return None


def axis_price(f, y):
    try:
        return f["a"] * float(y) + f["b"]
    except Exception:
        return None


def nearest_label_value(tags, predicted, tol=0.015):
    """在标签里挑与该边几何值最接近的价位（标签是精确价、轴换算是估算）。
    只在相差 ≤1.5% 时采信，否则 None（宁可未读到）。"""
    best, best_d = None, None
    for t in tags:
        v = t.get("value")
        if not isinstance(v, (int, float)) or v <= 0:
            continue
        d = abs(v - predicted) / predicted if predicted else 9
        if d <= tol and (best_d is None or d < best_d):
            best, best_d = v, d
    return best


# ================= 🆕 2026-09-17 门口的"像不像信号"关键词表 =================
# 用户要求（2026-09-17）：「把解析支持的关键词都补进门口表（只少丢消息，不会误开单）」。
# 背景：这张表原来只有 18 个词，导致「Stop 76000」「Buy BTC 77000」「入场 4250」「多单」
#   「目标位」这类消息**在送 AI 之前**就被判"闲聊"丢掉 —— 这就是"关键词永远补不完"的根源。
# ⚠️ 放宽只会"少丢消息"：真正决定开单的仍是 解析 → 数值校验 → 你审批。
# ⚠️ 闲聊不会因此被推送：推送只发生在"有币种+图/价位"或"有数字+价位关键词"这两类（见主循环）。
# 提到模块级是为了能被自检直接查（原来的局部变量测不到）。
SIG_KW_GATE = [
    "long", "Long", "LONG", "short", "Short", "SHORT",
    "Entry", "entry", "CMP", "cmp", "Buy", "buy", "Buying",
    "Sell", "sell", "Selling", "Stop", "stop", "SL", "sl",
    "Target", "target", "Targets", "TP", "tp",
    "take profit", "Take Profit", "Limit", "limit",
    "close", "Close", "Closed", "DCA", "dca",
    "做多", "做空", "多单", "空单", "多头", "空头", "买多", "卖空",
    "接多", "接空", "追多", "追空", "看多", "看空",
    "止损", "止盈", "止损位", "止盈位", "目标位", "目标",
    "入场", "进场", "开仓", "加仓", "补仓", "挂单", "限价",
    "条件单", "平仓", "减仓", "跌破", "站稳", "突破",
]


# ================= 🆕 2026-09-17 通用色块读图（**不看颜色**）=================
# 用户原话（2026-09-17）：
#   「第一张和第二张，分别是 ua 和黄金群发的开单图，ua 他用的是红色和绿色框，黄金群用的是灰色和蓝色框，
#     你不管他是什么颜色，你要能够读出止盈止损和开仓价…你不能够死板地只用红色和绿色去区分是否为开仓信号。」
# 实测（用户给的 4 张真图 + 服务器上的原图）：
#   · 黄金那张（浅色主题）：蓝区 y=60..569（509px 高）、灰区 y=573..652（79px 高）→ 交界≈570
#     = 开仓 4258 ｜ 止盈 4380（上面那块大的）｜ 止损 4239（下面那块小的）✓ 与图上标注一致
#   · ua 那张（深色主题，半透明填充）：绿区在上、红区在下，共用边 = 开仓 4.3167
# ⇒ 通用判据只有两条：**两片颜色均匀的填充区** + **谁大谁小**。颜色只当交叉校验。
ZONE_QUANT = 8            # 颜色量化步长（把近似色归成一档）
ZONE_TOL = 16             # 同色容差
ZONE_MIN_COVER = 0.10     # 一行里该颜色至少占扫描宽度的 10% 才算"大片"
ZONE_MIN_H = 18           # 一个区至少这么高（像素）
ZONE_GAP = 10             # 两个区相隔多少像素内算"共用一条边"
ZONE_MERGE_GAP = 30       # 同一块填充被横线（实测是那条白色虚线）切成两段时，隔多远仍算同一块


def _zone_family(rgb):
    """颜色只给一个**提示**：红/橙/褐/灰 → 偏止损空间；绿/蓝/青/白 → 偏止盈空间。
    ⚠️ 它**只做交叉校验**，绝不单独用来判断角色 —— 每个群的配色不一样（用户 2026-09-17 明确要求）。
    实测两组配色都能对上：ua 红+绿、黄金群 灰+蓝。"""
    r, g, b = rgb[0], rgb[1], rgb[2]
    mx, mn = max(rgb), min(rgb)
    if mx - mn < 18:                     # 灰/白/黑（无彩色）
        return "loss_hint" if mx < 245 else "profit_hint"
    if r >= g and r >= b:
        return "loss_hint"               # 红 / 橙 / 褐
    if g >= r and g >= b:
        return "profit_hint"             # 绿
    if b >= r and b >= g:
        return "profit_hint"             # 蓝 / 青
    return "unknown"


def find_zones(px, w, h):
    """**不看颜色**地找博主用工具画的两片填充区（止损空间 / 止盈空间）。

    做法：逐行统计"这一行里哪个颜色占了足够宽度"，再把颜色接近、纵向相连的行并成一个个"区"；
    最后把整张图的底色（占行数最多的那个颜色）排除掉。
    返回按高度从大到小排序的 [{"rgb","y0","y1","h","x0","x1"}]。
    """
    xa, xb = int(w * 0.04), int(w * 0.80)          # 只扫图表区，排除右侧价格轴
    xs = list(range(xa, xb, 3))
    if len(xs) < 10:
        return []
    need = max(8, int(len(xs) * ZONE_MIN_COVER))
    bycol = {}
    for y in range(h):
        cnt = {}
        for x in xs:
            c = px[x, y]
            k = (c[0] // ZONE_QUANT * ZONE_QUANT,
                 c[1] // ZONE_QUANT * ZONE_QUANT,
                 c[2] // ZONE_QUANT * ZONE_QUANT)
            cnt[k] = cnt.get(k, 0) + 1
        for k, n in cnt.items():
            if n >= need:
                bycol.setdefault(k, []).append(y)
    if not bycol:
        return []
    # 底色 = 出现行数最多的颜色（整张图的背景），排除
    bg = max(bycol.items(), key=lambda kv: len(kv[1]))[0]
    segs = []
    for k, ys in bycol.items():
        if k == bg:
            continue
        # 🆕 2026-09-17 两种配色各踩过一个坑，判据必须同时避开：
        #   ① 深色主题：背景还有第二个色调（(0,24,16) 旁边有 (0,16,16)），
        #      它被当成"止损区" → 止损读成 4.2731（正确是 4.156）；
        #   ② 浅色主题：灰色止损区的颜色是 (240,240,240)，离白底(248,248,248)只有 8 ——
        #      **绝不能用"与背景接近"排除**，否则把真止损区杀掉（实测就是这么错的）。
        #   所以只排除"**又深又无彩**"的颜色（那是背景/UI 底色），不碰任何浅色。
        _mx, _mn = max(k), min(k)
        if _mx < 40 and (_mx - _mn) < 20:
            continue
        ys = sorted(set(ys))
        cur = [ys[0], ys[0]]
        for y in ys[1:]:
            if y - cur[1] <= 3:
                cur[1] = y
            else:
                segs.append((k, cur[0], cur[1]))
                cur = [y, y]
        segs.append((k, cur[0], cur[1]))
    # 合并"颜色接近 + 纵向重叠"的段（量化会把同一片区域拆成相邻几档色）
    merged = []
    for k, y0, y1 in sorted(segs, key=lambda t: (t[1], t[2])):
        hit = None
        for m in merged:
            if (abs(m["rgb"][0] - k[0]) <= ZONE_QUANT
                    and abs(m["rgb"][1] - k[1]) <= ZONE_QUANT
                    and abs(m["rgb"][2] - k[2]) <= ZONE_QUANT
                    and not (y1 < m["y0"] - ZONE_MERGE_GAP or y0 > m["y1"] + ZONE_MERGE_GAP)):
                hit = m
                break
        if hit:
            hit["y0"] = min(hit["y0"], y0)
            hit["y1"] = max(hit["y1"], y1)
        else:
            merged.append({"rgb": k, "y0": y0, "y1": y1})
    out = []
    _scan_w = max(1, xb - xa)
    for m in merged:
        mh = m["y1"] - m["y0"]
        if mh < ZONE_MIN_H:
            continue
        # 🆕 2026-09-17 实测（ua 那张状态图）：手机截图的**顶栏/底栏**也是大片同色，
        #   结果被当成"止盈区"→ 开仓/止损/止盈全读成垃圾（5.3083/5.3083/5.3083）。
        #   判据：贴住图片最上/最下边的、或者几乎占满整个扫描宽度的，都是 UI 而不是博主画的框。
        if m["y0"] <= 3 or m["y1"] >= h - 4:
            continue
        ymid = (m["y0"] + m["y1"]) // 2
        xh = [x for x in xs
              if abs(px[x, ymid][0] - m["rgb"][0]) <= ZONE_TOL
              and abs(px[x, ymid][1] - m["rgb"][1]) <= ZONE_TOL
              and abs(px[x, ymid][2] - m["rgb"][2]) <= ZONE_TOL]
        _w = (max(xh) - min(xh)) if xh else 0
        # 🆕 2026-09-17 实测（黄金群那张）：**止损区横向占满整幅图**（这笔单子画得更早），
        #   所以"占满整宽就是 UI 条"这条判据会误杀真色块 → 必须再加"很扁"这个条件才排除。
        if _w >= _scan_w * 0.95 and mh < h * 0.08:
            continue                                  # 又宽又扁 = UI 条/分隔线，不是仓位框
        out.append({"rgb": m["rgb"], "y0": m["y0"], "y1": m["y1"], "h": mh,
                    "x0": (min(xh) if xh else 0), "x1": (max(xh) if xh else 0), "w": _w})
    out.sort(key=lambda z: -z["h"])
    return out


def read_zones(tags, zones, f, tp_tiers=3):
    """用"两片填充区"的几何关系读出 方向 / 开仓 / 止损 / 止盈（**不看颜色**）。

    规则（用户 2026-09-17 定）：
      · 两个区**共用的那条边 = 开仓价**
      · **小的那块 = 止损空间**，其远端边 = 止损价
      · **大的那块 = 止盈空间**，远端边 = 末档止盈；框内的长横线 = 分档止盈
      · 止损空间在**下方** → 做多；在**上方** → 做空
      · 颜色只做交叉校验：颜色提示与"大小关系"矛盾 → conf="conflict"（不猜，标"需你确认"送审批）
    """
    if len(zones) < 2:
        return None
    # 取"上下相邻"的两块里最高的一对（相邻 = 共用一条边）
    cand = None
    for i in range(len(zones)):
        for j in range(i + 1, len(zones)):
            a, b = zones[i], zones[j]
            top, bot = (a, b) if a["y0"] <= b["y0"] else (b, a)
            gap = bot["y0"] - top["y1"]
            if -ZONE_GAP <= gap <= ZONE_GAP * 2:
                area = top["h"] + bot["h"]
                if cand is None or area > cand[0]:
                    cand = (area, top, bot)
    if not cand:
        return None
    _area, top, bot = cand
    if top["h"] == bot["h"]:
        return None
    if bot["h"] < top["h"]:
        loss, prof, direction = bot, top, "LONG"      # 止损空间在下方 → 做多
    else:
        loss, prof, direction = top, bot, "SHORT"     # 止损空间在上方 → 做空
    junc_y = (top["y1"] + bot["y0"]) // 2
    sl_y = loss["y1"] if direction == "LONG" else loss["y0"]
    tp_y = prof["y0"] if direction == "LONG" else prof["y1"]
    pred = {"entry": axis_price(f, junc_y) if f else None,
            "sl": axis_price(f, sl_y) if f else None,
            "tp": axis_price(f, tp_y) if f else None}
    entry = nearest_label_value(tags, pred["entry"], 0.02) if pred["entry"] else None
    sl = nearest_label_value(tags, pred["sl"], 0.02) if pred["sl"] else None
    tp_far = nearest_label_value(tags, pred["tp"], 0.02) if pred["tp"] else None
    info = {"dir": direction, "junction_y": junc_y,
            "edges": {"entry": junc_y, "sl": sl_y, "tp": tp_y},
            "zone_loss": [loss["y0"], loss["y1"]], "zone_profit": [prof["y0"], prof["y1"]],
            "rgb_loss": list(loss["rgb"]), "rgb_profit": list(prof["rgb"]), "pred": pred}
    if not (entry and sl and tp_far):
        info["why"] = "认出了两片色块，但边上的价格标签没读准（开仓=%s 止损=%s 止盈=%s）" % (entry, sl, tp_far)
        info["ok"] = False
        return info
    rr = prof["h"] / float(max(1, loss["h"]))
    fam_loss, fam_prof = _zone_family(loss["rgb"]), _zone_family(prof["rgb"])
    conf = "high"
    if fam_loss == "profit_hint" and fam_prof == "loss_hint":
        conf = "conflict"                     # 颜色提示与"大小关系"完全相反 → 不猜
    elif fam_loss == fam_prof:
        conf = "low"
    if rr < 1.0:
        conf = "conflict"                     # 止盈空间比止损空间还小，不合常理
    # 止盈空间里的长横线 = 分档止盈（按离入场由近到远）
    inside = []
    for t in tags:
        v = t.get("value")
        if not isinstance(v, (int, float)) or v <= 0 or t.get("cov", 0) < TP_MIN_COV:
            continue
        ly = t.get("line_y") or t.get("y")
        if min(junc_y, tp_y) + 5 < ly < max(junc_y, tp_y) - 5:
            if (v > entry) if direction == "LONG" else (v < entry):
                inside.append(v)
    near = sorted({round(x, 8) for x in inside}, key=lambda v: abs(v - entry))
    tps = sorted(set(near[:tp_tiers - 1]) | {round(tp_far, 8)})
    info.update({"ok": True, "entry": entry, "sl": sl, "tps": tps, "tps_all": tps,
                 "conf": conf, "rr": round(rr, 2), "fam": [fam_loss, fam_prof]})
    return info


def _line_y(px, w, y0, y1, bg=(2, 24, 21)):
    """在 [y0,y1] 行里找"覆盖最好"的那一行 —— 那就是**线自己的行**。
    为什么不能用标签区域中心：区域里还含"印在线上方的那行标签文字"，
    中心会被文字往上拽十几到几十像素（实测这就是 0.5%~5% 的价格误差来源）。"""
    best, best_y = 0.0, (y0 + y1) // 2
    for yy in range(max(0, y0), y1 + 1):
        c = _coverage(px, w, yy, yy + 1, bg)
        if c > best:
            best, best_y = c, yy
    return round(best, 3), best_y


def _self_marks():
    """机器人**自己发出的所有通知前缀**。任何机器人会发的通知都必须登记，
    否则它会把自己的通知当成博主信号重新解析（2026-09-15 出过一次，2026-09-16 又出过一次）。
    🆕 2026-09-16 实测：新加的「ℹ️【手工仓护栏】…LSK…」没登记 → 13:33 被当成一条 LSK 信号，
    播报出【信号·未能识别】。所以要有一条自检：**源码里 notify 用到的前缀必须都在这里**。"""
    return ["【跟单机器人】", "【机器人指令】", "【已开单·纸面】",
            "【已结单·纸面】", "【止盈成交·纸面】", "【已平仓·纸面】",
            "【已减仓·纸面】", "【你的持仓】", "【指令】", "【博主指令】",
            "【信号·", "【待确认】", "【挂单情况】", "【机器人状态】",
            "【持仓情况】", "【风控熔断", "【对账闸门】", "【持仓自检】",
            "【手工仓护栏】", "【机器人告警】",
            # 🆕 2026-09-16：自检（源码里 notify 用到的前缀必须都在表里）一次抓出这 9 个漏登记的，
            #    它们每一个都可能让机器人把自己的通知当信号重解析。
            "【止盈更新·纸面】", "【实盘·成交】", "【实盘·成交但要你处理】", "【实盘·未成交】",
            "【实盘·看门狗】", "【实盘·部分成交】", "【实盘动作失败·需要处理】",
            "【失联看门狗】", "【启动对账失败】",
            # 🆕 2026-09-16 官方 API 取消息层新增的两条告警（自检 8g 抓出来的）
            "【取消息】", "【取消息告警】",
            # 🆕 2026-09-17：新加的"没看懂就回话"提示（[8g] 自检当场抓出来的，见 [8l]）
            "【指令·没看懂】"]


SELF_MARKS = _self_marks()


# ================= 🆕 2026-09-17 主题自适应（浅色底图也要能读）=================
# 实测（用户 2026-09-17 给的黄金群那张）：它是**浅色主题（白底）**，而整套读图代码一直假设深色底：
#   · `_cls` 是给"白字印在深底上"写的 → 浅底上的深色数字一律返回 None → **标签一个都找不到**；
#   · `_coverage` 把"与 (2,24,21) 不同"当成"有线" → 白底上整行都算有线 → 覆盖率恒为 1.0 → 线判定全乱。
# 处理：先判主题，浅色底换一个"与背景差异够大就算一类"的分类器；深色底**一行不改**（不破坏已调好的读法）。
BG_DARK = (2, 24, 21)


def _theme_and_bg(px, w, h):
    """返回 (theme, bg)：theme ∈ {"dark","light"}；bg = 采样里最常见的那种颜色。"""
    cnt = {}
    for y in range(0, h, 7):
        for x in range(int(w * 0.03), int(w * 0.75), 7):
            c = px[x, y]
            k = (c[0] // 8 * 8, c[1] // 8 * 8, c[2] // 8 * 8)
            cnt[k] = cnt.get(k, 0) + 1
    if not cnt:
        return "dark", BG_DARK
    bg = max(cnt.items(), key=lambda kv: kv[1])[0]
    return ("light" if max(bg) > 190 else "dark"), bg


def _cls_light(r, g, b, bg):
    """浅色底的标签分类：**与背景差异够大**就归类，否则算背景。"""
    if abs(r - bg[0]) + abs(g - bg[1]) + abs(b - bg[2]) <= 36:
        return None
    mx, mn = max(r, g, b), min(r, g, b)
    if mx - mn < 40:
        return "white" if mx > 200 else ("grey" if mx > 110 else "dark")
    if r >= g and r >= b:
        return "red" if (g < 110 and b < 110) else "orange"
    if g >= r and g >= b:
        return "green"
    return "blue"


def _edge_price_by_vision(im, w, h, edges):
    """把三条边所在的**横条**裁出来，一次调用让视觉模型读"每条线右边对应的价格"。

    用户原话（2026-09-17）：「重点在于那条线右边对应的价格，就是开仓价格」。
    为什么需要它：浅色主题（白底）的图上，工具自己的小标签是**细字**，批量 OCR 经常读空，
    但右侧价格轴 / 线上标注的数字是清楚的 —— 让模型只看这一条横条，成功率完全不同。
    edges = {"entry": y, "sl": y, "tp": y}；返回 {"entry":..,"sl":..,"tp":..}（缺的键不出现）。
    """
    try:
        tiles = []
        for k in ("entry", "sl", "tp"):
            y = int(edges.get(k) or 0)
            y0 = max(0, y - 18)
            y1 = min(h, y + 18)
            if y1 - y0 < 8:
                return {}
            c = im.crop((0, y0, w, y1))
            sc = 3 if c.width * 3 <= 2200 else 2
            tiles.append(c.resize((c.width * sc, c.height * sc), Image.LANCZOS))
        _hdr = ("这是**同一张 K 线图**上三条水平线各自所在的横条，顺序是：第1张=开仓线、"
                "第2张=止损线、第3张=止盈线。请读出**每条线右边对应的价格数字**"
                "（可能是线右侧的标签，也可能是最右侧价格轴上的数字）。"
                "只输出 JSON：{\"entry\":<数字>,\"sl\":<数字>,\"tp\":<数字>}；读不到就写 null。")
        content = [{"type": "text", "text": _hdr}]
        for t in tiles:
            import io
            buf = io.BytesIO()
            t.convert("RGB").save(buf, format="PNG")
            content.append({"type": "image_url",
                            "image_url": {"url": "data:image/png;base64,"
                                                 + base64.b64encode(buf.getvalue()).decode()}})
        body = {"model": "deepseek-v4-flash-vision-exp", "temperature": 0,
                "messages": [{"role": "system", "content": "只读图上真实可见的数字，不确定就 null。"},
                             {"role": "user", "content": content}]}
        r = requests.post(DS_API, headers={"Authorization": "Bearer " + DS_KEY,
                                          "Content-Type": "application/json"},
                          json=body, timeout=180)
        txt = r.json()["choices"][0]["message"]["content"]
        m = re.search(r"\{[\s\S]*\}", txt)
        if not m:
            log("   ⚠️ 按位置读数：模型没给出 JSON：%s" % str(txt)[:120])
            return {}
        d = json.loads(m.group(0))
        out = {}
        for k in ("entry", "sl", "tp"):
            v = d.get(k)
            if isinstance(v, str):
                v = re.sub(r"[^0-9.]", "", v)
            try:
                fv = float(v)
                if fv > 0:
                    out[k] = fv
            except Exception:
                pass
        return out
    except Exception as e:
        log("   ⚠️ 按位置读数失败：%s" % str(e)[:120])
        return {}


def read_chart(path):
    im = Image.open(path).convert("RGB"); w, h = im.size
    px = im.load()
    _theme, _bg = _theme_and_bg(px, w, h)
    if _theme == "light":
        # 浅色底的图（实测黄金群那张）：价格标签是右侧的**普通文字**、且贴在最右边一列，
        # 不像深色主题那样是一块实心色块 → 扫描带要放宽到最右，连续像素门槛要放低。
        x0, x1, _run_min = int(w * 0.74), w, 12
    else:
        x0, x1, _run_min = int(w * 0.76), int(w * 0.985), 35
    band = im.crop((x0, 0, x1, h)); bw, bh = band.size
    bpx = band.load()
    if _theme == "light":
        grid = [[_cls_light(bpx[x, y][0], bpx[x, y][1], bpx[x, y][2], _bg)
                 for x in range(bw)] for y in range(bh)]
        log("   🎨 浅色主题图（白底）→ 用与背景差异判标签；背景=%s" % (_bg,))
    else:
        grid = [[_cls(*bpx[x, y]) for x in range(bw)] for y in range(bh)]
    rects = []
    for y in range(bh):
        x = 0
        while x < bw:
            c = grid[y][x]
            if not c: x += 1; continue
            x2 = x
            while x2 + 1 < bw and grid[y][x2 + 1] == c: x2 += 1
            if x2 - x + 1 >= _run_min: rects.append({"c": c, "y": y, "x": x, "x2": x2})
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
    merged = sorted(merged, key=lambda t: t["y1"])
    nums = _ocr_tags_batch(im, x0, merged)                   # 一次调用读完所有标签（第 1 次读数）
    tags = []
    for i, t in enumerate(merged, 1):
        v = nums.get(str(i))
        if v is None: v = nums.get(i)
        try:
            fv = float(str(v).replace(",", "").replace("$", "").strip())
        except Exception:
            fv = None                      # ⚠️ 读不出也不许丢标签（下面会用单标签复核救回来）
        yc = (t["y1"] + t["y2"]) // 2
        _cov, _ly = _line_y(px, w, max(0, yc - 25), min(h - 1, yc + 25), _bg)
        tags.append({"y": yc, "line_y": _ly, "color": t["c"], "value": fv,
                     "text": str(v) if v is not None else None, "_reg": t, "cov": _cov})
    # ===== 关键标签逐个复核（用户 2026-09-16 选定）=====
    # 只对"关键"标签做单标签单独 OCR：红色止损线 + 够长的止盈候选横线（≤6 个）。
    # 与批量读数**互相独立**：一致 → 采信；不一致 → 标「未读到」并记日志（绝不猜）。
    verify = {"checked": 0, "agreed": 0, "single_only": 0, "batch_only": 0, "dropped": 0, "notes": []}
    # ⚠️ 性能（2026-09-16 实测）：逐个复核很准但**每次调用要 5~15 秒**，图上关键标签一多就
    #    把一次信号拖到 40 秒以上。所以只复核**真正决定下单参数**的标签：
    #      · 红色止损线（最多 2 条）
    #      · 覆盖率最高的 3 条长横线（止盈候选）
    #    其余标签只用批量读数（它们只出现在"图上其余横线"的参考列表里，不值得多花时间）。
    _long_sorted = sorted([t for t in tags if t["cov"] >= TP_MIN_COV and t["color"] != "red"],
                          key=lambda t: -t["cov"])[:3]
    _key, _seen_key = [], set()
    for t in ([t for t in tags if t["color"] == "red"][:2] + _long_sorted):
        if id(t) not in _seen_key:
            _seen_key.add(id(t)); _key.append(t)
    verify["essential"] = len(_key)
    for t in _key:
        v1 = _ocr_one_label(im, x0, t["_reg"])
        verify["checked"] += 1
        ok, val, why = two_read_ok(t.get("value"), v1)
        if ok:
            if v1 is not None and t.get("value") is not None:
                verify["agreed"] += 1
            elif v1 is not None:
                verify["single_only"] += 1
            else:
                verify["batch_only"] += 1
            t["value"] = val
            t["verified"] = (v1 is not None and t.get("text") is not None)
        else:
            verify["dropped"] += 1
            verify["notes"].append("%s：%s" % (t["color"], why))
            t["value"] = None
            t["unverified"] = why
            log("   ⚠️ 读图标签两次读数不一致 → 按未读到处理（%s，y=%d）：%s"
                % (t["color"], t["y"], why))
    # 单调性校验：同一张图上，价格必须随 y 增大而下降（价格轴的几何性质）
    _mono_bad = drop_nonmonotonic(tags)
    for t in _mono_bad:
        if t.get("value"):
            t["value"] = None
            t["unverified"] = "违反价格轴单调性（y 越大价格反而越高）"
            verify["dropped"] += 1
            log("   ⚠️ 读图标签违反单调性 → 按未读到处理（%s，y=%d）" % (t["color"], t["y"]))
    tags = [t for t in tags if t.get("value")]
    # ===== 🆕 2026-09-17 通用色块读图（**不看颜色**）—— 先走这条 =====
    # 用户明确要求：「ua 用红绿框、黄金群用灰蓝框，你不管他什么颜色，都要读出止盈/止损/开仓价」。
    # 判据只有"两片颜色均匀的填充区 + 谁大谁小"，颜色仅作交叉校验。旧的"只认暗红/暗绿"退居兜底。
    geo = {}
    _axis = fit_axis_scale([(t.get("line_y") or t["y"], t["value"])
                            for t in tags if t["cov"] >= TP_MIN_COV])
    _zones = find_zones(px, w, h)
    _zr = read_zones(tags, _zones, _axis) if len(_zones) >= 2 else None
    if _zr and _zr.get("ok"):
        log("   🧩 色块读图: %s | 开仓 %s 两区交界 y=%d | 止损 %s 止损区 y=%s | 止盈 %s | "
            "置信=%s 盈亏比=%s 颜色提示=%s"
            % (_zr["dir"], _zr["entry"], _zr["junction_y"], _zr["sl"], _zr["zone_loss"],
               _zr["tps"], _zr["conf"], _zr["rr"], _zr["fam"]))
        return {"ok": True, "sl": _zr["sl"], "entry": _zr["entry"],
                "tps_all": _zr["tps_all"], "tps": _zr["tps"][:TP_TIERS], "tags": tags,
                "lines": [{"value": t["value"], "color": t["color"], "cov": t["cov"]}
                          for t in tags if t["cov"] >= TP_MIN_COV],
                "verify": verify, "geo": geo, "zones": _zr, "mode": "zones"}
    if _zr and not _zr.get("ok") and _theme == "light":
        # 🆕 浅色主题兜底：色块认出来了、但小标签读不出 → 按"**那条线右边对应的价格**"再读一次
        #    （用户 2026-09-17 原话：「重点在于那条线右边对应的价格，就是开仓价格」）
        #    ⚠️ 只在浅色主题图上用这一条：深色主题的标签本来就读得好，没必要冒险。
        _vp = _edge_price_by_vision(im, w, h, _zr.get("edges") or {})
        _e, _s, _t = _vp.get("entry"), _vp.get("sl"), _vp.get("tp")
        _good = bool(_e and _s and _t and
                     ((_s < _e < _t) if _zr["dir"] == "LONG" else (_s > _e > _t)))
        if _good:
            # ⚠️ 2026-09-17 自检当场抓到的坑：视觉读数把 `6.513` 读成 `6513`（小数点丢了），
            #   而"止损<开仓<止盈"的**大小关系照样成立** → 光看顺序会静默采用错价位。
            #   所以再加三道校验：
            #   ① 与图上其它标签量级相符 —— 但**只在标签自身自洽时**才用它当参照
            #      （浅色图上标签经常读得很脏，实测中位数都能是 43 万，那种参照没意义）；
            #   ② 与**像素几何**自洽 —— 价格比例必须跟 y 距离的比例一致，**不依赖任何标签**
            #      （黄金那张：像素比 0.157 / 价格比 0.155 = 0.99 ✓；UNI 读错那次：0.108 vs 1.05 → 差 9.7 倍 ✗）；
            #   ③ 三个价位不许差得离谱。
            _ref = [t["value"] for t in tags
                    if isinstance(t.get("value"), (int, float)) and t["value"] > 0]
            if _ref and max(_ref) / max(1e-9, min(_ref)) <= 20:
                _med = sorted(_ref)[len(_ref) // 2]
                if not all(0.2 <= float(v) / _med <= 5.0 for v in (_e, _s, _t)):
                    _good = False
                    log("   ⚠️ 按位置读数与图上其它标签量级不符（读数 %s/%s/%s ｜ 图上标签中位 %s）→ 不采用"
                        % (_e, _s, _t, _med))
            if _good:
                _junc = _zr["junction_y"]
                _dpx_sl = abs(_junc - _zr["edges"]["sl"])
                _dpx_tp = abs(_junc - _zr["edges"]["tp"])
                _dpr_sl = abs(float(_e) - float(_s))
                _dpr_tp = abs(float(_t) - float(_e))
                if _dpx_sl > 5 and _dpx_tp > 5 and _dpr_tp > 0:
                    _r_px = _dpx_sl / float(_dpx_tp)
                    _r_pr = _dpr_sl / float(_dpr_tp)
                    if not (0.5 <= (_r_pr / _r_px) <= 2.0):
                        _good = False
                        log("   ⚠️ 按位置读数与像素几何不自洽（像素比 %.3f vs 价格比 %.3f，差 %.1f 倍）→ 不采用"
                            % (_r_px, _r_pr, (_r_pr / _r_px)))
            if _good:
                _rng = max(_e, _s, _t) / max(1e-9, min(_e, _s, _t))
                if _rng > 3.0:
                    _good = False
                    log("   ⚠️ 按位置读数三个价位相差 %.2f 倍（不合常理）→ 不采用" % _rng)
            if _good:
                # ⚠️⚠️ 2026-09-17 二次自检抓到的坑：上面两道校验把 _good 置 False 之后，
                #   这段**照样继续构造并返回**了那个读数 —— 等于"检查了但没拦住"。
                #   现在整个"采用"分支都在 if _good 里面（校验不通过就绝不返回）。
                _ty = _zr["edges"]["tp"]
                _inside = []
                for t in tags:
                    v = t.get("value")
                    ly = t.get("line_y") or t.get("y")
                    if not isinstance(v, (int, float)) or v <= 0 or t.get("cov", 0) < TP_MIN_COV:
                        continue
                    if min(_zr["junction_y"], _ty) + 5 < ly < max(_zr["junction_y"], _ty) - 5:
                        _inside.append(v)
                _tps = sorted(set([round(x, 8) for x in _inside][:TP_TIERS - 1]) | {round(_t, 8)})
                log("   🧩 色块 + 按位置读数：%s ｜ 开仓 %s（两区交界）｜ 止损 %s（止损区远端）｜ 止盈 %s"
                    % (_zr["dir"], _e, _s, _tps))
                _zr2 = dict(_zr)
                _zr2.update({"ok": True, "entry": _e, "sl": _s, "tps": _tps, "tps_all": _tps,
                             "conf": "vision_edges", "why": None})
                return {"ok": True, "sl": _s, "entry": _e, "tps_all": _tps,
                        "tps": _tps[:TP_TIERS], "tags": tags,
                        "lines": [{"value": t["value"], "color": t["color"], "cov": t["cov"]}
                                  for t in tags if t["cov"] >= TP_MIN_COV],
                        "verify": verify, "geo": geo, "zones": _zr2, "mode": "zones+vision"}
        log("   ↳ 按位置读数没通过校验（开仓=%s 止损=%s 止盈=%s 方向=%s）→ 退回旧的按颜色/标签规则"
            % (_e, _s, _t, _zr["dir"]))
        log("   ⚠️ 认出了两片色块但边上价格没读准 → 退回旧的按颜色/标签规则：%s" % _zr.get("why"))
    if len(_zones) >= 2:
        log("   ↳ 色块识别（不看颜色）找到 %d 片填充区：%s"
            % (len(_zones), [(z["rgb"], z["y0"], z["y1"]) for z in _zones[:4]]))
    # ===== 几何优先（旧）：认得出"工具色框"就按框的语义定角色 =====
    try:
        boxes = find_fill_boxes(px, w, h)
        if "green" in boxes and "red" in boxes:
            _gx0, gy0, _gx1, gy1 = boxes["green"]
            _rx0, ry0, _rx1, ry1 = boxes["red"]
            long_up = gy0 < ry0                       # 绿框在红框上方 → 做多
            if long_up:
                stop_y, entry_y, target_y = ry1, ry0, gy0
            else:
                stop_y, entry_y, target_y = ry0, ry1, gy1
            # 价格轴：用"覆盖率够长的横线 + 它的标签值"拟合 —— 但坐标必须用**线自己的行 y**
            # （不能用合并区域中心，否则被线上方的标签文字拽偏，实测差 0.5%~5%）
            _long = [t for t in tags if t["cov"] >= TP_MIN_COV]
            f = fit_axis_scale([(t.get("line_y") or t["y"], t["value"]) for t in _long])
            pred_stop = axis_price(f, stop_y) if f else None
            pred_target = axis_price(f, target_y) if f else None
            pred_entry = axis_price(f, entry_y) if f else None
            # 数字优先取"与该边几何值最接近的标签"（标签是精确价），没有就用轴换算
            stop = nearest_label_value(tags, pred_stop) if pred_stop else None
            target = nearest_label_value(tags, pred_target) if pred_target else None
            geo = {"boxes": {k: list(v) for k, v in boxes.items()},
                   "dir": "LONG" if long_up else "SHORT",
                   "stop_y": stop_y, "entry_y": entry_y, "target_y": target_y,
                   "pred": {"stop": pred_stop, "entry": pred_entry, "target": pred_target},
                   "scale": ({"a": f["a"], "b": f["b"], "n": f["n"], "max": round(f["max"], 5)}
                             if f else None)}
            if stop and target and stop > 0 and target > 0:
                # 绿框内的长横线 = 分档止盈；**绿框远端（上边的做多 / 下边的做空）必进**
                # —— 那是 KOL 画的最终目标（2026-09-16 实测 BTC 空单图：按距离截断会把它挤掉，
                #    当时给的是 [77366.6,77824.6,78667.4]，而真正的目标 76094（绿框底边）被丢了）
                inside = [t for t in tags
                          if t["cov"] >= TP_MIN_COV and t["value"]
                          and min(entry_y, target_y) + 5 < (t.get("line_y") or t["y"]) < max(entry_y, target_y) - 5
                          and (t["value"] > stop if long_up else t["value"] < stop)]
                _near = sorted({round(t["value"], 8) for t in inside},
                               key=lambda v: abs(v - (pred_entry or stop)))
                tps = sorted(set(_near[:TP_TIERS - 1]) | {round(target, 8)})
                geo["used"] = True
                log("   🧭 几何读图：%s ｜ 止损 %.8g（红框下边 y=%d）｜ 开仓 %.8g（红绿交界 y=%d）｜ "
                    "止盈 %s（绿框内横线 + 绿框上边）"
                    % (geo["dir"], stop, stop_y, pred_entry or 0, entry_y, tps))
                return {"ok": True, "sl": stop, "entry": (pred_entry or None),
                        "tps_all": tps, "tps": tps[:TP_TIERS], "tags": tags,
                        "lines": [{"value": t["value"], "color": t["color"], "cov": t["cov"]}
                                  for t in tags if t["cov"] >= TP_MIN_COV],
                        "verify": verify, "geo": geo, "mode": "geometry"}
            log("   ⚠️ 认出了色框，但框边价格没读准（止损=%s 目标=%s）→ 退回标签规则"
                % (stop, target))
    except Exception as _e:
        log("   ⚠️ 几何读图异常（不影响旧逻辑）：%s" % str(_e)[:100])
    reds = [t for t in tags if t["color"] == "red"]
    if not reds:
        return {"ok": False, "why": "无红色止损标签（标签区域 %d 个，两次读数一致可用 %d 个）"
                                    % (len(merged), len(tags)), "tags": tags, "verify": verify,
                "geo": geo, "mode": "legacy"}
    sl = min(reds, key=lambda t: t["value"])["value"]
    above = sorted([t for t in tags if t["value"] > sl * 1.0005], key=lambda t: t["value"])
    if not above: return {"ok": False, "why": "止损上方无标签", "tags": tags, "verify": verify}
    entry = above[0]["value"]
    # ---- 止盈候选：止损上方、覆盖率够长的横线 ----
    _cand = []
    for t in above:
        v = t["value"]
        if v <= entry * 1.0005: continue
        if t["cov"] < TP_MIN_COV: continue
        if any(abs(v - c["value"]) / v < 0.003 for c in _cand): continue     # 同一档（含轴刻度重复读数）
        _cand.append(t)
    tps_all = [t["value"] for t in _cand]
    # ⚠️ 2026-09-15 实测（真实 UNI 图）：候选有 4 档（6.676 / 7.180 / 8.216 / 9.289），
    #    若只按"价格由近到远"取前 3 档，会把下方的 6.676 当 TP1，真正最远的 9.289 被挤掉。
    #    实测该图上 KOL 画的止盈线是【最长的三条横线】（覆盖率 0.743/0.787/0.863），
    #    而 6.676 是更短的参考线（0.674）、6.513（0.2）与 6.396（0.195）是入场/止损这种短线。
    #    所以：候选多于 3 档时，先按横线长度取最长的 3 档，再按价格由近到远排序。
    _sel = sorted(_cand, key=lambda t: -t["cov"])[:TP_TIERS] if len(_cand) > TP_TIERS else _cand
    tp = sorted([t["value"] for t in _sel])
    # 供"只识别到图"的单独推送用：把图上所有可读横线按价位列出来（含没进 TP 的那些），
    # 让用户能看到图上到底有什么，而不是只看到一个数字。
    all_lines = [{"value": t["value"], "color": t["color"], "cov": t["cov"]} for t in above]
    return {"ok": True, "sl": sl, "entry": entry, "tps_all": tps_all, "tps": tp[:TP_TIERS],
            "tags": tags, "lines": all_lines, "verify": verify, "geo": geo, "mode": "legacy"}

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

# ===== B10 残留修复：模板「Going long <X>」里 X 位置可能是个英文常用词 =====
# ONUSDT / ATUSDT / THEUSDT / INUSDT / SOUSDT … 都是币安真实合约，所以「Going long on UNI」
# 会被读成 ON。这些词出现在这个位置时一律跳过，继续往后找真正的币种词；
# 全程跳过之后 raw 仍为空 → 会走 find_coin_in_text() 兜底，不会因此丢信号。
# ⚠️ 与下面 _AMBIG_TICKERS 的分工：这里是【位置性】跳过表，故意只放介词/冠词/副词，
#    不放 NEAR/LINK/APE 这类**博主真会写**的币种词（那些由 _AMBIG_TICKERS 在二遍匹配时处理）。
_GOING_LONG_SKIP = {
    "ON", "AT", "THE", "IN", "INTO", "TO", "OF", "AND", "OR", "FOR", "AS", "BY", "WITH",
    "HERE", "NOW", "THIS", "THAT", "THEN", "IT", "IS", "BE", "ARE", "WAS", "WERE",
    "SO", "ALL", "MY", "AN", "UP", "DOWN", "DO", "NO", "IF", "WE", "ME", "YOU",
}

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
    # 🆕 2026-09-17 生产实测（用户 LSK 卡片）：正文里的 `Take Profits:` 被当成币种 TAKE
    #   （TAKEUSDT 是真实合约）→ 单币消息被误判成"多币种"，走进了不读图的分支。
    #   放进歧义词表只影响"大小写不敏感的第二遍"；真正大写写 TAKE 时仍会被认出（第一遍区分大小写）。
    "TAKE", "PROFIT", "PROFITS", "HARD", "RISK", "LOSS", "TARGET", "TARGETS",
    "LONG", "SHORT", "ENTRY", "BREAK", "ABOVE", "BELOW", "SUPPORT", "RESISTANCE",
}

def fast_parse(txt):
    """只匹配博主常用模板；命中即返回，未命中返回 None（交给 AI 解析）"""
    raw = None
    dirc = None
    # ⚠️ B10 残留（2026-09-15 实测）：模板「Going long on UNI here at CMP」里，
    #    `Going (long|short) (\w+)` 抓到的第一个词是英文介词 **on**，而 ONUSDT 是
    #    币安真实合约 → 整条 UNI 信号被解析成 coin=ON。实测（部署版本，23:5x）：
    #    fast_parse("Going long on UNI here at CMP…") → {"coin": "ON", …}
    #    现在：介词/常用词跳过，继续往后找真正的币种词。
    m = re.search(r"(?:Going|Market|Longing|Buying|Selling|Shorting)\s+(long|short)\s+"
                  r"(.{1,40})", txt, re.I)
    if m:
        dirc = "LONG" if m.group(1).lower() == "long" else "SHORT"
        for _tok in re.findall(r"\$?([A-Za-z0-9]{2,12})", m.group(2)):
            if _tok.upper() in _GOING_LONG_SKIP:
                continue
            raw = _tok
            break
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
    # 🆕 2026-09-17 用户要求：「% 不参与价格」。
    #   原来「止损3%」「带个3%止损」会被这条价格正则读成 **止损=3**（一个荒谬的价位），
    #   而正确的百分比止损正则（下面 ②）反而拿不到值 → 百分比止损基本都走审批或被拦。
    #   现在把 `%` 从"间隔"里排除 → 带 % 的写法不再被当价格，交给 ② 的 stopPct 处理。
    _GAP = r"[^0-9%，。；;、！!？?\n]{0,10}"
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
    # 🆕 2026-09-17：与止损那条保持一致 —— 不许跨越标点/百分号去吃下一句的数字
    mm = re.search(r"(?:DCA|Dca|dca|加仓|补仓)\s*[^0-9%，。；;、！!？?\n]{0,6}"
                   r"(?:价|位|价格)?\s*[:：=]?\s*\$?([0-9]*\.?[0-9]+)", txt)
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
    _BOUND = (r"(?:止盈|目标位?|targets?|take\s*profits?|(?:TP|Tp|tp)\s?\d?|止损|stop[ -]?loss|SL|"
              r"加仓|DCA|入场|进场|Entry)")
    _kw = list(re.finditer(_BOUND, txt, re.I))
    for _i, _m in enumerate(_kw):
        if not re.match(r"(?:止盈|目标位?|targets?|take\s*profits?|(?:TP|Tp|tp)\s?\d?)",
                        _m.group(0), re.I):
            continue                                   # 只管止盈类关键词
        _end = _kw[_i + 1].start() if _i + 1 < len(_kw) else len(txt)
        _seg = txt[_m.end():_end]
        _nums = []
        # 🆕 2026-09-17 用户要求「% 不参与价格」：卡片里 `TP1: $0.6690 (+52.00%)` 的 `52.00%`
        #   以前被当成止盈档（实测 targets = [0.669, 52.0, 1.0]）。现在先把**整段百分比表达式**
        #   （含 `1-3%` 这种区间写法）从候选文本里剔掉，再做数字扫描 —— 否则 `1-3%` 里的 `1`
        #   因为后面不是紧跟 % 而漏网（自检第一版实测到的坑）。
        _seg = re.sub(r"[0-9]*\.?[0-9]+\s*(?:[-~～至]\s*[0-9]*\.?[0-9]+\s*)?%", " ", _seg)
        for _mx in re.finditer(r"([0-9]*\.?[0-9]+)(\s*%)?", _seg):
            if _mx.group(2):
                continue
            try:
                _nums.append(float(_mx.group(1)))
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
    if out_stop_pct and stop is not None:
        # 🆕 2026-09-17：明确写了百分比止损时，**以百分比为准**。
        #   否则「入场100 止损3%」里"数字+止损"那条规则会把入场价 100 当成止损。
        #   （与 AI prompt 一致：给了 stopPct 就 stop 留 null。）
        log("   ↳ 同时读到百分比止损 %.2f%% 与一个价位止损 %s → 以百分比为准（那个数字更可能是入场价）"
            % (out_stop_pct, stop))
        stop = None
    # ③ 止损区间（⚠️ 同样不许跨越标点：实测「4250止损，4350到4450分批止盈」里
    #    旧写法把止盈区间 4350~4450 读成了止损区间）
    _msr = re.search(r"止损\s*[:：=]?\s*\$?([0-9]*\.?[0-9]+)\s*"
                     r"(?:到|至|~|～|-|—|–)\s*\$?([0-9]*\.?[0-9]+)", txt)
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
        # ⚠️ 2026-09-15 实测发现（真 bug）：`entry_is_cmp`（文本写 CMP/现价/市价）**从来没有被
        #    拷进待确认池** —— merge_pending 只拷了 entry/stop/targets 等字段，于是
        #    finalize_pending 里那句 `if p.get("entry_is_cmp")` 是**死代码**：
        #    0.12 声称"已修 CMP 取当前市价"其实没生效（实测 21:57 UNI 日志用的是图上标签 6.505）。
        if info.get("entry_is_cmp"):
            p["entry_is_cmp"] = True
        if info.get("tp_on_chart"):
            p["tp_on_chart"] = True
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

# ================= B16：原始消息暂存区（按「群 + 时间窗」）=================
# 用户要求（2026-09-15）：① 图片必须能被读到、不能丢 ② 合并窗口保持 4 秒 ③ 4 秒内没关联上
# 就【分别推送】（只识别到图 → 分析图并推送；有文字 → 分析文字并推送；各自只说自己的信息，
# 缺失的一律"未读到"，绝不瞎猜）。
#
# 为什么需要它（两条实测根因，都有生产证据）：
#   ① 图片常常是**独立一条、没有文字**的消息 → 没有币种 → 挂不进按币种索引的 PENDING[coin]
#      → 在"挂载环节"被丢掉：20:21-20:22 黄金mansoor 两条真实图（文件都在 v21/imgs 里）
#      只留了一行「没通过信号门槛」日志，**零推送**，用户完全不知道错过了什么。
#   ② 更早一层：单张图还没加载完时（nimg=1 / loaded=0）连"有图"都不算 → 在关键词门槛就跳过，
#      连抓图都不尝试（21:57 UNI 那条图消息就是这样，v21/imgs 里根本没有这张文件）。
# 本暂存区解决 ①：先按"群 + 最近 N 秒"把图原样存下（不要币种），文字消息带来币种后回填关联；
# 4 秒内没等到 → 按用户要求单独推送图自己的信息。
RAWQ = {}                       # group -> [rec]
RAWQ_TTL = 90                   # 秒：暂存记录最长保留（清理用；关联窗口见 RAWQ_WAIT）
RAWQ_MAX = 20                   # 每群最多保留条数
RAWQ_WAIT = PENDING_WAIT        # 关联窗口 = 合并窗口 = 4 秒（用户要求：不能拖慢出单）

def msg_has_image(row):
    """B16：这条消息是否带图。**只要有图元素就算**（nimg>0），
    不再要求"已经加载完"（loaded>0）——21:57 UNI 的图就是死在这个额外要求上：
    单张图还没加载完时 nimg=1 / loaded=0，被判成"没图、也没信号词"直接跳过，
    连抓图都不会尝试，v21/imgs 里根本没留下这张文件。
    实测纯文本消息 nimg=0，所以 nimg>0 不会把纯文字消息也拖进抓图流程。"""
    try:
        return int((row or {}).get("nimg", 0) or 0) > 0
    except Exception:
        return False

def img_wait_ms(row, has_kw):
    """抓图等待预算。FETCH_IMG_JS 一旦拿到可用的图就立即返回，
    所以这个预算只在"图确实加载不出来"时才真的等满。"""
    try:
        nimg = int((row or {}).get("nimg", 0) or 0)
        nblob = int((row or {}).get("nblob", 0) or 0)
    except Exception:
        nimg = nblob = 0
    return 6000 if (nimg >= 2 or nblob > 0 or not has_kw) else 1200

def rawq_add(group, rec):
    """把一条原始消息（目前是带图的消息）暂存；先不要求币种。"""
    rec = dict(rec)
    rec.setdefault("ts", time.time())
    rec.setdefault("used", False)
    rec.setdefault("pushed", False)
    q = list(RAWQ.get(group, []))
    q.append(rec)
    now = time.time()
    RAWQ[group] = [r for r in q if (now - r.get("ts", 0)) <= RAWQ_TTL][-RAWQ_MAX:]
    return rec

def rawq_pending_imgs(group, now=None):
    """该群【窗口内、还没被用掉】的图记录（按时间正序）"""
    now = now or time.time()
    return [r for r in RAWQ.get(group, [])
            if r.get("kind") == "img" and not r.get("used")
            and (now - r.get("ts", 0)) <= RAWQ_WAIT]

def _apply_chart(p, chart):
    """把图上的线并入待确认池 —— 与 merge_pending 里的「图优先覆盖文字」规则保持一致。"""
    if not (chart and chart.get("ok")):
        return False
    if chart.get("sl"):
        p["stop"] = chart["sl"]; p["stop_src"] = "K线图"
    # 文本写 CMP/现价时，入场价按规则取当前市价，不让图上的标签覆盖（0.12 已修的语义）
    if chart.get("entry") and not p.get("entry_is_cmp"):
        p["entry"] = chart["entry"]; p["entry_src"] = "K线图"
    if chart.get("tps"):
        p["tps"] = list(chart["tps"])
    return True

def rawq_bind(group, coin, p, now=None):
    """文字消息带来币种后，把窗口内暂存的图【回填关联】到这条信号上。返回关联到的图片张数。"""
    now = now or time.time()
    n = 0
    for r in rawq_pending_imgs(group, now):
        r["used"] = True
        r["bound_to"] = coin
        _mc = (r.get("meta") or {}).get("coin")
        for f in (r.get("imgs") or []):
            if f not in (p.get("imgs") or []):
                p.setdefault("imgs", []).append(f); n += 1
        if r.get("text") and r["text"] not in (p.get("texts") or []):
            p.setdefault("texts", []).append(r["text"][:400])
        if _mc and str(_mc).upper() != str(coin).upper():
            # 保护：图上币种和文字币种不一致时，只把图留档，**不用**图上的线覆盖文字点位
            log("   📎 [图] 已关联到 %s，但图上币种=%s 与文字不一致 → 只留图、不用图上的线"
                % (coin, _mc))
        elif _apply_chart(p, r.get("chart")):
            p["chart"] = r["chart"]
        if n:
            log("   📎 [图] 已回填关联到 %s（%d 张，距图 %.1fs）" % (coin, n, now - r.get("ts", now)))
    return n

def rawq_attach_pending(group, imgs, chart, meta=None, txt="", mid=None, now=None):
    """文字先到、图后到：把刚抓到的图挂到【本群还没结束的待确认信号】上（反向顺序也要覆盖）。"""
    now = now or time.time()
    cands = [c for c, p in PENDING.items()
             if (p.get("group") == group) and (not p.get("imgs"))
             and (now - p.get("first_ts", 0)) <= (RAWQ_WAIT + 1.5)
             and float(p.get("deadline") or 0) >= now]
    if not cands:
        return None
    coin = cands[-1]
    p = PENDING[coin]
    n = 0
    for f in (imgs or []):
        if f not in (p.get("imgs") or []):
            p.setdefault("imgs", []).append(f); n += 1
    if txt and txt not in (p.get("texts") or []):
        p.setdefault("texts", []).append(txt[:400])
    _mc = (meta or {}).get("coin")
    if _mc and str(_mc).upper() != str(coin).upper():
        log("   📎 [图] 已并入 %s 的待确认池（%d 张），但图上币种=%s 不一致 → 只留图" % (coin, n, _mc))
    elif _apply_chart(p, chart):
        p["chart"] = chart
    log("   📎 [图] 文字先到、图后到 → 已并入 %s 的待确认池（%d 张，消息 %s）" % (coin, n, mid))
    return coin

def fmt_price(v):
    return ("%.8g" % v) if isinstance(v, (int, float)) else "未读到"

# ================= 🆕 2026-09-17 "已持仓状态图 / 账户截图"不推送 =================
# 用户原话：「第三张和第四张，分别是黄金群和 ua 群的已持仓状态，他们就是发群里说一下开仓情况，
#   这种不要理会。」实测（用户给的 4 张真图）：
#   · 状态图上的点位与**刚发过的那张开单图完全相同**（黄金 4258/4239/4380、ua 4.32/4.156/…）；
#   · 券商账户截图（MT4 那种）连价位都读不出。
# 判据两条：① 点位与最近见过的同一笔重复 → 持仓进展通报；② 读不出点位 + 视觉判为账户截图。
# 两条都**不推送，但只写日志、绝不静默消失**（可用指令调出来看；图仍会并入同群文字信号）。
IMG_SIG_SEEN = []           # 最近见过的"图上信号"点位：[{coin, entry, sl, tps, ts}]
IMG_SIG_TTL = 12 * 3600     # 记忆保留 12 小时
IMG_SIG_MAX = 80


def _img_sig_dump():
    _now = time.time()
    keep = [r for r in IMG_SIG_SEEN if _now - float(r.get("ts") or 0) <= IMG_SIG_TTL]
    return keep[-IMG_SIG_MAX:]


def _img_sig_remember(chart, coin=None):
    """把这次从图上读到的点位记下来，供后面识别"同一笔的进展通报"。"""
    if not (chart and chart.get("ok") and chart.get("entry")):
        return
    IMG_SIG_SEEN.append({"coin": (str(coin).upper() if coin else None),
                         "entry": chart.get("entry"), "sl": chart.get("sl"),
                         "tps": list(chart.get("tps") or []), "ts": time.time()})
    del IMG_SIG_SEEN[:-IMG_SIG_MAX]
    STATE_DIRTY[0] = True


def _near(a, b, tol=0.005):
    try:
        return bool(a and b and abs(float(a) - float(b)) / max(abs(float(b)), 1e-9) <= tol)
    except Exception:
        return False


def _img_is_repeat(chart, coin=None):
    """这张图的点位是不是"最近刚见过的那一笔"？是 → 视为持仓进展通报。

    ⚠️ 2026-09-17 实测修正：原来要求"**开仓价**先对上"才继续比，但状态图的读数会偏
    （黄金那张的状态图把开仓读成 4298，而正确是 4258；止损位反而对上了 4258.022）。
    改成：**两个价位对得上就算同一笔**（候选的 开仓/止损/各档止盈 与记忆里的集合两两比），
    这样"读偏一项、其余对得上"也能识别出来；真正的新信号不会有两项都撞上。
    """
    if not (chart and chart.get("ok") and chart.get("entry")):
        return False
    _lv = [chart.get("entry"), chart.get("sl")] + list(chart.get("tps") or [])
    _lv = [v for v in _lv if isinstance(v, (int, float)) and v > 0]
    if len(_lv) < 2:
        return False
    for r in _img_sig_dump():
        _rv = [r.get("entry"), r.get("sl")] + list(r.get("tps") or [])
        _rv = [v for v in _rv if isinstance(v, (int, float)) and v > 0]
        _hit = 0
        for a in _lv:
            if any(_near(a, b, 0.005) for b in _rv):
                _hit += 1
        if _hit >= 2:
            return True
    return False


def _img_kind_by_vision(path):
    """读不出点位时问一次视觉模型：这张图是 K 线图，还是券商/交易所账户截图？
    用途：把"账户截图"这类**非信号图**识别出来（用户要求不要推送）。"""
    try:
        ext = str(path).rsplit(".", 1)[-1].lower()
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        body = {"model": "deepseek-v4-flash-vision-exp", "temperature": 0,
                "messages": [{"role": "system", "content": "只输出 JSON，不要解释。"},
                             {"role": "user", "content": [
                                 {"type": "text", "text":
                                  "这张图是：①一张K线/行情图（可能有博主画的开仓/止损/止盈线）"
                                  "②券商或交易所的账户/持仓截图（有余额、净值、保证金、持仓列表）"
                                  "③其他。只输出 {\"kind\":\"chart\" 或 \"account\" 或 \"other\"}"},
                                 {"type": "image_url",
                                  "image_url": {"url": "data:image/%s;base64,%s"
                                                       % ("png" if ext == "png" else "jpeg", b64)}}]}],
                }
        r = requests.post(DS_API, headers={"Authorization": "Bearer " + DS_KEY,
                                          "Content-Type": "application/json"},
                          json=body, timeout=120)
        m = re.search(r"\{[\s\S]*\}", r.json()["choices"][0]["message"]["content"])
        return (json.loads(m.group(0)).get("kind") or "").lower() if m else ""
    except Exception as e:
        log("   ⚠️ 图片类型判定失败：%s" % str(e)[:100])
        return ""


def _img_should_skip(g, r):
    """True = 判为"已持仓状态图/账户截图"→ 不推送（只写日志）。"""
    ch = r.get("chart") or {}
    meta = r.get("meta") or {}
    if _img_is_repeat(ch, meta.get("coin")):
        log("   📎 [图] 点位与最近刚见过的同一笔一致 → 判为「持仓进展通报」，不推送")
        return True
    if not ch.get("ok"):
        _imgs = r.get("imgs") or []
        _k = _img_kind_by_vision(_imgs[0]) if _imgs else ""
        if _k == "account":
            log("   📎 [图] 视觉判定=券商账户/持仓截图 → 不推送")
            return True
    return False


def _push_img_only(group, rec, now=None):
    """用户要求③：4 秒内没关联上文字 → 单独推送【这张图自己包含的信息】，缺失的一律"未读到"。"""
    now = now or time.time()
    ch = rec.get("chart") or {}
    meta = rec.get("meta") or {}
    when = rec.get("when") or datetime.datetime.fromtimestamp(
        float(rec.get("t_sig") or rec.get("ts") or now), CST).strftime("%m-%d %H:%M:%S")
    files = [os.path.basename(x) for x in (rec.get("imgs") or [])]
    L = ["【信号·只识别到图】%s" % group, ""]
    L.append("时间：%s ｜ 图：%s" % (when, "、".join(files) or "（未落盘）"))
    if ch.get("ok"):
        coin = meta.get("coin") or None
        dirc = (meta.get("direction") or "").upper()
        dirc_cn = "做多 LONG" if dirc == "LONG" else ("做空 SHORT" if dirc == "SHORT" else "未读到")
        L.append("图上读到（来源：chart）：币种 %s ｜ 方向 %s" % (coin or "未读到", dirc_cn))
        if ch.get("entry"):
            # 🆕 2026-09-17：开仓价**读到了就要说出来**（用户当天报障"开仓价为空"）
            L.append("开仓：%s（图上两区交界 = 开仓价，来源：chart）" % fmt_price(ch.get("entry")))
        L.append("止损：%s（图上止损空间远端，来源：chart）" % fmt_price(ch.get("sl")))
        if ch.get("tps"):
            L.append("止盈（图上横线，来源：chart，按离入场由近到远）：%s"
                     % " / ".join(fmt_price(x) for x in ch["tps"]))
        else:
            L.append("止盈：未读到（图上没有可确认的横线）")
        _others = [x for x in (ch.get("lines") or [])
                   if x.get("value", 0) > (ch.get("sl") or 0) * 1.0005
                   and all(abs(x["value"] - t) / max(abs(t), 1e-9) >= 0.003
                           for t in (ch.get("tps") or []))]
        if _others:
            L.append("图上其余横线（不当止盈，供你参考）：%s"
                     % "、".join(fmt_price(x["value"]) for x in _others[:4]))
        _zc = (ch.get("zones") or {}).get("conf")
        if _zc and _zc != "high":
            L.append("⚠️ 图上「哪个框是止损、哪个是止盈」的判断置信度=%s"
                     "（颜色提示与大小关系不完全一致）→ 请你确认" % _zc)
    else:
        L.append("这张图已抓到并保存，但没能读出可用的点位（来源：chart）：%s"
                 % (ch.get("why") or "未知原因"))
    # 🆕 2026-09-17：这一段原来是**写死的**（读到了也照样印"未读到的：开仓/入场价"）——
    #   用户当天报障"开仓价识别为空"就是被这句话误导的。现在按**实读**写。
    _miss = []
    if not (ch.get("ok") and ch.get("entry")):
        _miss.append("开仓/入场价")
    _miss += ["加仓点位", "来源文字"]
    L.append("")
    L.append("**未读到的（绝不猜）**：%s —— %s。"
             % ("、".join(_miss),
                "4 秒内本群没有等到带币种的文字，图上也没读出可确认的开仓价"
                if not (ch.get("ok") and ch.get("entry")) else "这几项图上没有"))
    L.append("本条只做通报，**不会下单**。要开单请补发文字信号（机器人不会替你猜缺失的点位）。")
    notify("\n".join(L))

def rawq_sweep(group=None, now=None):
    """到点还没被关联的图 → 单独推送（用户要求③）。返回推送条数。"""
    now = now or time.time()
    n = 0
    for g in ([group] if group else list(RAWQ)):
        for r in list(RAWQ.get(g, [])):
            if r.get("kind") != "img" or r.get("used") or r.get("pushed"):
                continue
            if (now - r.get("ts", 0)) < RAWQ_WAIT:
                continue
            r["pushed"] = True
            # 🆕 迁为持仓进展通报 / 账户截图 → 不推送（只写日志，绝不静默丢）
            if _img_should_skip(g, r):
                continue
            _push_img_only(g, r, now)
            _img_sig_remember(r.get("chart"), (r.get("meta") or {}).get("coin"))
            log("   📎 [图] %.0f 秒内没等到带币种的文字 → 已单独推送（图=%d 张，图上有止损=%s）"
                % (now - r.get("ts", now), len(r.get("imgs") or []),
                   bool((r.get("chart") or {}).get("sl"))))
            n += 1
    return n

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
# ===== AI 优先解析（2026-09-15，用户要求"让机器人理解人话而不是抓关键词"）=====
# 现状的致命缺陷：本地正则先跑，**然后自己判断自己可不可信** —— 正则的盲区就是系统的盲区
#   （点数、语序、条件单全在盲区里，正则还能"很自信地"给错答案，于是永远不会去叫 AI）。
# 改造后：AI 当**主解析器**（结构化 JSON，temperature=0），本地正则降级为
#   ①交叉校验 ②补齐 AI 漏读的档位。并加**反幻觉硬约束**：AI 给的每个数字都必须在原文里
#   逐字出现，否则判为可疑 → 强制送人工审批（绝不自动开）。
# 开关：runtime_config.json 的 ai_first_parse（默认 True）；关掉即回到"正则优先"老行为。
AI_FIRST = [True]


def _ai_numbers(d, depth=0):
    """把解析结果里所有【数值】收集出来（用于反幻觉校验）。只收价位类字段，不收元数据。"""
    out = []
    if depth > 3:
        return out
    if isinstance(d, dict):
        for k, v in d.items():
            if k in ("type", "manage_action", "direction", "coin", "is_signal", "tp_on_chart",
                     "entry_is_cmp", "_fast", "_ai", "_ai_unverified"):
                continue
            out += _ai_numbers(v, depth + 1)
    elif isinstance(d, (list, tuple)):
        for x in d:
            out += _ai_numbers(x, depth + 1)
    elif isinstance(d, bool):
        pass
    elif isinstance(d, (int, float)):
        out.append(float(d))
    return out


def verify_ai_numbers(txt, d):
    """**反幻觉硬约束**：AI 给出的每个数字都必须在原文里逐字出现。
    这是 AI 优先方案能成立的前提 —— 没有它，AI 编一个点位就会变成一笔真单。
    返回"在原文里找不到"的数字列表（空列表 = 全部有据可查）。"""
    t = (txt or "").replace(",", "").replace("$", "").replace("，", "")
    # ⚠️ 原文里的「74K」「1.2M」这类缩写也要算"有据可查"。
    #    实测：原文写「74K」，AI 返回 74000，旧校验因为找不到 "74000" 而误判成"编造"。
    #    ⚠️ 注意：展开值**不在原文里**（原文是"74K"），所以不能用"子串"判，必须用"数值相等"判。
    _exp = []
    for m in re.finditer(r"([0-9]*\.?[0-9]+)\s*([KkMm])", t):
        try:
            _exp.append(float(m.group(1)) * (1000.0 if m.group(2).lower() == "k" else 1000000.0))
        except Exception:
            pass
    bad = []
    for v in _ai_numbers(d):
        _hit = False
        for c in ("%.8g" % v, (str(int(round(v))) if abs(v - round(v)) < 1e-9 else None)):
            if c and c in t:
                _hit = True
                break
        if not _hit:
            for e in _exp:                       # 74K → 74000 这类缩写
                if abs(float(v) - e) <= max(abs(e) * 1e-9, 1e-9):
                    _hit = True
                    break
        if not _hit:
            bad.append(v)
    return bad


def merge_parse_results(txt, ai, fast):
    """AI 优先：以 AI 为主，本地正则只做【交叉校验 + 补齐缺档】。返回 (info, 说明)。"""
    if not ai:
        if fast:
            return fast, "AI 无结果 → 降级用本地正则（仍走同一套校验与审批）"
        return None, "AI 无结果且本地正则也没读到"
    out = dict(ai)
    notes = []
    bad = verify_ai_numbers(txt, ai)
    if bad:
        out["_ai_unverified"] = bad
        notes.append("⚠️ 反幻觉校验未过：这些数字在原文里找不到 → 强制送审批：%s" % bad[:6])
    # 正则补齐：AI 漏读的止盈档
    ft = [float(x) for x in ((fast or {}).get("targets") or []) if isinstance(x, (int, float))]
    at = [float(x) for x in (out.get("targets") or []) if isinstance(x, (int, float))]
    if ft and len(ft) > len(at):
        _add = [t for t in ft if t not in at]
        if _add:
            out["targets"] = at + _add
            notes.append("正则补齐止盈档 +%s" % _add)
    # 正则补齐：AI 漏读的关键字段
    # ⚠️ 必须避免"注入冲突值"：实测踩到过 —— AI 已给出正确的 stop=4250，
    #    而正则的 stopRange 因为跨越逗号读到了止盈区间 [4350,4450]，一旦注入就污染了正确结果。
    #    规则：语义等价的字段（stop/stopRange、entry/entryRange/entryLegs）AI 已给就不注入。
    _SEM = {"stop": ("stop", "stopRange"), "stopRange": ("stop", "stopRange"),
            "entry": ("entry", "entryRange", "entryLegs"),
            "entryRange": ("entry", "entryRange", "entryLegs"),
            "entryLegs": ("entry", "entryRange", "entryLegs")}
    for k in ("stop", "entry", "entryLegs", "entryRange", "stopRange", "stopPct", "add_price"):
        if (out.get(k) in (None, [], "")) and (fast or {}).get(k) not in (None, [], ""):
            _grp = _SEM.get(k)
            if _grp and any(out.get(_g) not in (None, [], "") for _g in _grp):
                notes.append("跳过注入 %s（AI 已给出等价的 %s，避免冲突）"
                             % (k, [g for g in _grp if out.get(g) not in (None, [], "")]))
                continue
            out[k] = fast.get(k)
            notes.append("正则补 %s=%s" % (k, fast.get(k)))
    out["_ai"] = True
    if not out.get("direction"):
        out["direction"] = (fast or {}).get("direction")
        if out["direction"]:
            notes.append("正则补 direction=%s" % out["direction"])
    if not out.get("coin"):
        out["coin"] = (fast or {}).get("coin")
        if out["coin"]:
            notes.append("正则补 coin=%s" % out["coin"])
    return out, "；".join(notes)


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
    _cmp_mkt = None
    # 🆕 2026-09-17 联调实测：博主只写「CMP」没给价位 → p["entry"] 为 None →
    #    审批单显示「入场：未读到」，同时却写「已通过全部机器校验」，看起来自相矛盾。
    #    CMP 的语义本来就是"按市价入场"，所以借 p["mkt_at_ask"]（下单时取的市价）显示成
    #    「按市价（CMP）」，下面止损点数/亏损估算才有得算。
    if (not isinstance(entry, (int, float)) or not entry) and p.get("entry_is_cmp"):
        _m = p.get("mkt_at_ask")
        if isinstance(_m, (int, float)) and _m:
            entry, _cmp_mkt = _m, _m
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
    if coin and str(coin).upper() in MANUAL_COINS:
        out.append("⚠️ 你在币安有 **%s 的手工仓**：机器人在实盘下**不会碰它**（不会开真单、不挂保护、"
                   "不改它的止损）。这条信号只走纸面。" % str(coin).upper())
    if isinstance(entry, (int, float)) and entry:
        if _cmp_mkt:
            out.append("入场：**按市价（博主只写了 CMP，未给具体价位）** ≈ %s"
                       "（下单时以当时市价为准）" % _fmt_num(entry))
        else:
            out.append("入场：%s%s" % (_fmt_num(entry),
                                     ("（%s）" % p["entry_src"]) if p.get("entry_src") else ""))
    elif p.get("entry_is_cmp"):
        out.append("入场：**按市价（博主写的是 CMP）** → 下单时取当时市价")
    else:
        out.append("入场：**未读到**")
    if _lg:
        out.append("分批建仓：%d 个点位 %s（保证金等分）"
                   % (len(_lg), "、".join(_fmt_num(x) for x in _lg)))
    # 🆕 2026-09-17 清理死代码（用户要求"清"）：`add_price` 以前**解析了却从不显示**。
    #   它是有用信息（博主的加仓点位），现在接到审批里；没有就不显示这一行。
    _addp = p.get("add_price")
    if isinstance(_addp, (int, float)) and _addp:
        out.append("加仓点位：%s（博主给的加仓位，机器人**不会自动加仓**，只提示）" % _fmt_num(_addp))
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


def coins_in_order(txt):
    """按**在文字里出现的先后**返回币种（去重）。
    🆕 2026-09-17：多币种拆段原来用 `sorted(find_all_coins(...))[0]` = **字母序第一**，
    而不是文中最先提到的 —— 例如「ETH 做空 2465，BTC 站稳 76200 多」写在同一行时，
    字母序会挑中 BTC（B 在 E 前面），**哪怕 ETH 是先写的** → 方向/点位可能安到错的币上。
    """
    up = (txt or "").upper()
    hits = []
    for c in find_all_coins(txt):
        pos = None
        m = re.search(r"\$?" + re.escape(c) + r"(?![A-Za-z0-9])", up)
        if m:
            pos = m.start()
        else:                                # 中文/俗称写法：用别名表反查位置
            for al, cv in _NAME_MAP.items():
                if cv == c:
                    _m2 = re.search(re.escape(al.strip().upper()), up)
                    if _m2:
                        pos = _m2.start() + 0.5
                        break
        if pos is not None:
            hits.append((pos, c))
    seen, out = set(), []
    for _pos, c in sorted(hits):
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def split_by_coin(txt):
    """把一条多币种消息按【句子】切成"每币一段"。

    🆕 2026-09-17 修掉两个生产实测缺陷：
      ① **按换行切段会丢价位行**（用户 LSK 卡片事故）：卡片是"标题一行、价位各占一行"，
         旧写法按换行切、只保留"含币种的那一行" → `$0.4401 (limit)`、`Stop Loss:`、`TP1:` 全被丢，
         解析器只拿到 27 字符的标题行 → 开仓/止损/止盈全"未读到"。
         现在：换行**不再**当句子边界；含币种的行开新段，**不含币种但像价位/方向的行并进上一段**。
      ② 一行多币时取"字母序第一" → 改成**文中最先出现的那个**（见 coins_in_order）。
    """
    t = strip_sender_prefix(txt)
    _units = []
    for _ln in re.split(r"\n+", t):
        _parts = [p.strip() for p in re.split(r"[。；;！!？?]+", _ln) if p.strip()]
        _units.extend(_parts or [""])
    out, seen = [], set()
    _cont_re = re.compile(r"[0-9]|做多|做空|多单|空单|看多|看空|接多|接空|追多|追空|买|卖|"
                          r"止损|止盈|入场|进场|开仓|加仓|目标|条件单|限价|挂单|"
                          r"LONG|SHORT|Entry|CMP|TP|SL|stop|target|limit|profit", re.I)
    for seg in _units:
        if not seg:
            continue
        _cs = coins_in_order(seg)
        if _cs:
            c = _cs[0]
            if c in seen:
                continue
            seen.add(c)
            out.append((c, seg))
        elif out and _cont_re.search(seg):
            out[-1] = (out[-1][0], out[-1][1] + "\n" + seg)      # 续行并进上一段
    # 只认"至少带一个数字或方向词"的段（防假币种把单币消息拆成多币种）
    return [(c, s) for (c, s) in out if _cont_re.search(s)]


def _timing_line(p, prefix="⏱ 从信号发出到推送"):
    """把这一段耗时按现有分段打出来（用户 2026-09-16 要求：审批消息里也要有）。
    分段与"出单通知"里的 ⏱ 行**完全一致**：发现 / 抓图 / 解析 / 读图 / 等齐后续消息。"""
    now = time.time()
    _f = float(p.get("first_ts") or now)
    _t = {k: float(p.get(k) or 0) for k in ("t_found", "t_img", "t_parse", "t_chart")}
    _age = max(0.0, now - _f)
    _detect = max(0.0, (_t["t_found"] or _f) - _f) if _t["t_found"] else 0.0
    _img = max(0.0, _t["t_img"] - _t["t_found"]) if (_t["t_img"] and _t["t_found"]) else 0.0
    _parse = max(0.0, _t["t_parse"] - _t["t_img"]) if (_t["t_parse"] and _t["t_img"]) else 0.0
    _chart = max(0.0, _t["t_chart"] - _t["t_parse"]) if (_t["t_chart"] and _t["t_parse"]) else 0.0
    _wait = max(0.0, _age - _detect - _img - _parse - _chart)
    return ("%s 共 %.1fs（发现 %.1fs / 抓图 %.1fs / 解析 %.1fs / 读图 %.1fs / 等齐后续消息 %.1fs）"
            % (prefix, _age, _detect, _img, _parse, _chart, _wait))


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
           # 🆕 2026-09-16 用户要求：审批消息里也要带这段耗时（原来只在出单通知里有）
           _timing_line(p),
           "原文：%s" % (p["texts"][0][:180] if p.get("texts") else ""),
           "",
           "**回复「开」= 按上面这组参数开单；回复「不开」= 作废。**",
           "（%d 分钟内没回复自动作废）" % (ASK_TIMEOUT // 60)]))
    log("   ❓ 已挂起等用户确认：%s（%s）｜%s" % (coin, reason, _timing_line(p, "耗时")))


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
        if p.get("entry_is_cmp") and not _legs:
            signal_entry = None
            entry_note = "文本写的是 CMP/现价 → 入场价取**当前市价** %.8g（不用图上的标签）" % mkt
            # 🆕 2026-09-17：博主只写 CMP 没给价位时，审批单里原来显示「入场：未读到」，
            #    却又写着「已通过全部机器校验」—— 自相矛盾，看起来像解析失败。
            #    这里把"下单时的参考市价"记下来，**只用于审批单展示**（不改入场计划逻辑）。
            p["mkt_at_ask"] = mkt
            log("   ↳ CMP 语义：入场价取当前市价 %.8g" % mkt)
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
            # 2026-09-17 联调实测：这条路径原来也写「判为解析错误」，
            # 但实测那次其实是**市价已跌破信号止损（信号陈旧）** —— 交给共用归因函数区分。
            _side = _stop_side_reason(coin, dirc0, entry, p["stop"], mkt,
                                      bool(p.get("entry_is_cmp")),
                                      (p["texts"][0] if p.get("texts") else ""))
            if _side:
                notify("【信号·拒绝】%s %s\n%s\n原文：%s"
                       % (coin, "做多" if dirc0 == "LONG" else "做空", _side,
                          (p["texts"][0][:160] if p["texts"] else "")))
                log("   ⛔ 止损在错误一侧，拒绝出单：%s" % _side.replace("**", "")[:100])
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
        # ===== AI 优先：反幻觉校验没过的，一律送审批（绝不自动开）=====
        if _why is None and p.get("_ai_unverified"):
            _why = ("AI 解析出的这些数字在原文里**找不到**（疑似编造）：%s —— 必须你确认"
                    % (p.get("_ai_unverified") or [])[:6])
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
    # 🆕 2026-09-17 用户要求"收窄别名表"：原来有单字键「金」「银」「油」——
    #   它们会在任何含这个字的句子里命中（"资金""现金""石油""原油"…），把闲聊误判成信号。
    #   现在只保留**明确的多字写法**。
    "黄金": "XAU", "GOLD": "XAU", "XAUUSD": "XAU",
    "白银": "XAG", "SILVER": "XAG",
    "原油": "CL", "石油": "CL", "OIL": "CL", "WTI": "CL", "CRUDE": "CL",
    "天然气": "NATGAS", "GAS": "NATGAS",
    # 美股代币（币安有对应 USDT 永续）
    "闪迪": "SNDK", "SANDISK": "SNDK", "海力士": "SKHY", "SK海力士": "SKHY", "SKHYNIX": "SKHY", "HYNIX": "SKHY",
    "特斯拉": "TSLA", "TESLA": "TSLA", "英伟达": "NVDA", "NVIDIA": "NVDA", "苹果": "AAPL", "APPLE": "AAPL",
    "微软": "MSFT", "MICROSOFT": "MSFT", "谷歌": "GOOGL", "GOOGLE": "GOOGL", "亚马逊": "AMZN", "AMAZON": "AMZN",
    "奈飞": "NFLX", "NETFLIX": "NFLX", "超微": "AMD", "英特尔": "INTC", "INTEL": "INTC", "美光": "MU",
    # 🆕 2026-09-17 收窄：删掉键 `" coinbase"`（带前导空格，本身就是坏的写法）与泛词「策略」→MSTR
    "COINBASE": "COIN", "微策略": "MSTR", "帕兰提尔": "PLTR", "PLTR": "PLTR",
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
· 待你确认的信号会**落盘保存**，机器人意外重启也不会丢

— 手工仓护栏（你在币安自己开的仓）—
· 对账时若发现「交易所有仓、纸面无记录」→ 机器人把该币登记为**你的手工仓**并告警，
  之后**绝不对它发任何真单**（不开新仓、不平仓、不改止损、也不拿它的数量去挂保护）
· 解除手工仓 LSK —— 手工仓平掉后解除护栏（也可写：手工仓已平 LSK）"""


def load_runtime():
    global GROUPS, MARGIN, LEV, NOTIONAL, TEST_MODE, STRICT_LIMIT_GROUPS, MAX_OPEN
    global MAX_CONSEC_LOSS, DAILY_LOSS_LIMIT, MAX_TOTAL_MARGIN, SILENCE_ALERT_H, FETCH_MODE
    try:
        if os.path.exists(RUNTIME):
            cfg = json.load(open(RUNTIME, encoding="utf-8"))
            if cfg.get("groups"):
                GROUPS = [g for g in cfg["groups"] if g]
            # 取消息方式：api（官方 API，默认）| browser（爬网页，兜底可回滚）
            if cfg.get("fetch_mode") in ("api", "browser"):
                FETCH_MODE = cfg["fetch_mode"]
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
            # AI 优先解析开关：默认 True；设为 false 即回退到"本地正则优先"
            if "ai_first_parse" in cfg:
                AI_FIRST[0] = bool(cfg["ai_first_parse"])
            # 熔断线：总敞口上限的百分比（用户 2026-09-15 要求按金额熔断）
            if cfg.get("loss_limit_pct") is not None:
                LOSS_LIMIT_PCT[0] = float(cfg["loss_limit_pct"])
            # 真实下单层开关：跟随 runtime_config.json（热加载时也会走到这里）
            if _BEXEC_OK:
                bexec.LIVE[0] = bool(cfg.get("live_trading", False))
                bexec.LEV = LEV
            log("已载入运行配置：监控群=%s 保证金=%.0fU 杠杆=%d倍 测试模式=%s ｜ 严格限价群=%s ｜ 真实下单层=%s ｜ 开单需审批=%s ｜ AI优先解析=%s ｜ 失联告警阈值=%.1fh ｜ 暂停=%s"
                % ("、".join(GROUPS), MARGIN, LEV, TEST_MODE,
                   "、".join(STRICT_LIMIT_GROUPS) or "无", _be_mode(),
                   "是" if REQUIRE_APPROVAL[0] else "否",
                   "是" if AI_FIRST[0] else "否", SILENCE_ALERT_H,
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
               "require_approval": REQUIRE_APPROVAL[0], "ai_first_parse": AI_FIRST[0],
               "loss_limit_pct": LOSS_LIMIT_PCT[0]}
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


def _strip_nick(x):
    """去掉行首的发送者昵称（飞书对同一发送者的连续消息会省略时间戳，昵称就留在文本里了）。"""
    return re.sub(r"^[^\s]{2,16}\s+", "", x or "")


def _strip_time(x):
    """去掉行首的时间戳（如 "10:17 "）。"""
    return re.sub(r"^\d{1,2}:\d{2}\s*(AM|PM)?\s*", "", x or "", flags=re.I).strip()

_REPLY_WORD = (r"(开|开仓|开单|开吧|可以开|确认开|确认|下单|建仓|买入|买|执行|"
               r"不开|别开|不要|作废|全部开|全开|都开|全部不开|全不开|都不开|都不要|全部作废)")
# ⚠️ 2026-09-17 生产实测（用户报障"我回复开仓，机器人没有理我"）：
#   原来这张表只有「开|不开|作废|全部开|…」，而且判据是 re.fullmatch（整句必须一字不差）。
#   用户回的是「**开仓**」→ 既不 fullmatch、又不含币种 → _is_ask_reply 返回 False
#   → 消息掉到信号门槛 → 生产日志 `09:00:21 ↳ 闲聊/无关，跳过`，**一句话都不回**。
#   现在把日常说法都收进来（开仓/开单/建仓/下单/确认…），并配一条"没认出来也必须回话"
#   的兜底（_chitchat_hint），保证"你叫它、它必须应"。


def _is_ask_reply(t):
    """这条消息是不是"审批回复"？→ 返回 (bool, 用于处理的候选文本)。

    为什么把它抽成独立函数（2026-09-17）：原来这段判定**内联在 handle_command 里**，
    自检为了"图省事"把正则**又抄了一遍**去测，结果 `_handle_ask_reply` 的真实现从没被自检碰到 ——
    「不开被当开执行」的事故就是这么从自检眼皮底下溜过去的。抽出来之后，自检可以直接调它。
    """
    _ct_cands = []
    for _c in ((_strip_time(t) or t).strip(), _strip_nick((_strip_time(t) or t).strip())):
        _c2 = re.sub(r"^[\s，,。.、！!？?~～✓✅👍]+|[\s，,。.、！!？?~～]+$", "", _c or "")
        if _c2 and _c2 not in _ct_cands:
            _ct_cands.append(_c2)
    if not _ct_cands:
        _ct_cands = [(_strip_time(t) or t).strip()]
    for _ct in _ct_cands:
        if len(_ct) > 48:
            continue
        _co = _reply_coins(_ct)
        if not _co and ASKING:      # 兜底：直接匹配当前待确认里的币种（防止别名/非标准写法）
            _co = [c for c in ASKING
                   if re.search(r"(?<![A-Za-z0-9])" + re.escape(c) + r"(?![A-Za-z0-9])", _ct, re.I)]
        if bool(_co and re.search(r"开|买|作废|不要", _ct)) or bool(re.fullmatch(_REPLY_WORD, _ct)):
            return True, _ct
    return False, ""


def _is_self_app_row(g, sender_type):
    """这条消息是不是**我们自己发的通知**？（2026-09-17 自环事故的根治——这是第 4 次栽在自环上了）

    实测数据（生产真数据，详见 feishu_api.fetch_new 的注释）：
      · 机器人自己的通知：sender_type="app"（自定义机器人 webhook 发的）
      · 用户本人发的：sender_type="user"
      · **KOL 群里的博主信号也是 "app"**，而且 app_id 与我们自己的 webhook **完全相同**
        （同一个平台应用，只有 tenant_key 不同）⇒ 绝不能用"凡 app 发的就跳过"一刀切，
        那会把**真信号全部杀光**。

    判据取「群 + 发送者类型」这个组合：
      · CMD_GROUPS 是**我们自己的群**，里面只会有两种消息：你发的 / 机器人自己发的；
      · 博主信号只出现在 KOL 群，那些群不在 CMD_GROUPS 里 → 完全不受影响；
      · 拿不到 sender_type 时（浏览器兜底模式）返回 False = 保持原行为，绝不误杀。

    09-17 09:26 事故：机器人自己发的分币通知「· LSK 做多：开仓=未读到…」被它自己读回来，
    正文里有币种 LSK + 一个"开"字 → 被判成"用户回复：开 LSK" → 在用户**没有回复**的情况下
    执行了开单流程。本函数就是把这个入口关掉。
    """
    if not sender_type:
        return False
    if str(sender_type).lower() != "app":
        return False
    return g in CMD_GROUPS


_HINT_LAST = [0.0]


def _chitchat_hint(g, sender_type, txt):
    """指令群里、你发的、但既不是指令也不是信号的消息 → **必须回一句**，绝不静默。

    2026-09-17 实测：用户回「开仓」被判「闲聊/无关，跳过」吞掉，用户完全不知道机器人收没收到
    （用户原话："我回复开仓，机器人没有理我，这个bug必须修好"）。
    规矩（与设计原则"绝不静默丢弃"一致）：只要你在这个群里说了像指令的话，机器人必须回应。
    """
    if g not in CMD_GROUPS:
        return False
    if str(sender_type or "").lower() != "user":
        return False                       # 我们自己发的通知不回；别人发的也不回
    t = (txt or "").strip()
    if not t or len(t) > 40:
        return False
    if not re.search(r"开|关|仓|单|平|止|损|盈|确认|模式|持仓|状态|帮助|撤|停|继续|对账|手工仓|金额|杠杆|上限|监控", t):
        return False
    if time.time() - _HINT_LAST[0] < 20:
        log("   💬 指令群里没认出来的消息（%s）—— 20 秒内已提示过一次，不重复刷屏" % t[:20])
        return True
    _HINT_LAST[0] = time.time()
    notify("【指令·没看懂】「%s」我没认出来，所以**什么都没做**。\n"
           "可用：开 / 开仓 / 不开 / 只开 BTC / 全部不开 ｜ 持仓情况 ｜ 帮助" % t[:30])
    log("   💬 指令群里没认出来的消息 → 已回提示（绝不静默）")
    return True


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

    # 没点名币种：
    # ⚠️⚠️ 2026-09-17 【生产事故·方向性错误】用户回「不开」，机器人答「收到「开」」并**真的开了仓**。
    #    根因就在这里：`re.split` 把「不开」的否定词吃掉了 → head_list/tail_list 都是空 →
    #    下面那段"没点名币种 → 单条待确认时按「开」处理"**一律**把空列表当成"用户在说开"。
    #    也就是说：**否定语义在 split 时丢失了**，而空列表又被默认解释成肯定。
    #    修法：空列表时**先看这句是不是排除语义**（excl）；排除语义一律"作废"，绝不开仓。
    #    （宁可少开，也绝不把"不开"执行成"开" —— 前者是麻烦，后者是事故。）
    if not open_list and not close_list:
        if excl:
            n = len(pend)
            for c in list(ASKING):
                ASKING.pop(c, None)
            notify("【指令】收到「不开」→ 已作废 %d 个待确认信号（**未下单**）：%s\n"
                   "（想开其中的某几个就回：只开 XXX）" % (n, "、".join(pend)))
            log("   ❌ 用户回复是排除语义（未点名币种）→ 全部作废、不下单：%s" % pend)
            return True
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
           "进入实盘模式", "重新对账", "对账", "解除手工仓", "手工仓已平"]
    # 去掉可能的昵称/时间前缀后，指令必须在消息开头（防止转发内容被误当指令）
    # ⚠️ 2026-09-17：原来这里是把两个 lambda 定义在函数**内部**，导致抽出来的 _is_ask_reply
    #    看不到它们（自检当场 NameError）。现在统一用模块级的 _strip_nick / _strip_time。
    nick = _strip_nick
    tm = _strip_time
    # ===== 「开 / 不开 / 只开X和Y」确认（用户 2026-09-14 要求：把握不准必须问他）=====
    # 判定条件收紧，避免"开单记录""开始监控"这类正常文字被误当指令：
    #   ① 回复里点出了币种名 + 含开/买/作废等动词，或
    #   ② 整条就是「开/不开/全部开/全部不开/作废」这种极短词
    # ⚠️ 2026-09-16 实测事故（用户报「发送开单消息没有任何反应」）：
    #   日志 `发现新消息 | 用户963038 开` → 判成「闲聊/无关，跳过」→ XAU 待确认 30 分钟后超时作废。
    #   根因：飞书对**同一发送者的连续消息会省略时间戳**，而 strip_sender_prefix 的昵称规则
    #   要求"昵称后面必须跟时间"，于是昵称留在了文本里 → "整条就是『开』"这个判据失败。
    #   现在：**把"去掉开头昵称"的形态也算候选**，并允许首尾标点/emoji。
    # ⚠️ 2026-09-17：这里本来就有一份**重复的** _REPLY_WORD（函数内局部变量），
    #    而 _is_ask_reply() 读的是**模块级**那一份 —— 等于"改了这里，以为改了判定，其实毫无作用"。
    #    （与"自检抄一遍正则"是同一类坑。）现在删掉重复定义，判定只留模块级一份。
    _ok_reply, _ct_reply = _is_ask_reply(t)
    if _ok_reply:
        return _handle_ask_reply(_ct_reply)
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
        _fetch = ("官方 API（%s）" % ("已就绪" if API_READY[0] else "未就绪，正在用浏览器兜底")
                  if FETCH_MODE == "api" else "浏览器爬网页")
        if FETCH_MODE == "api" and _FAPI_OK:
            _ti = fapi.token_info()
            _fetch += " ｜ 令牌剩 %d 分钟 / 可续期 %d 小时%s" % (
                max(0, _ti.get("access_left_s", 0)) // 60, max(0, _ti.get("refresh_left_s", 0)) // 3600,
                "" if _ti.get("has_refresh") else "（⚠️ 无 refresh_token，到期需重新授权）")
        notify("【机器人状态】\n"
               "监控群：%s\n"
               "取消息：%s\n"
               "持仓：%d 笔 / 上限 %d 笔（%s）\n"
               "总敞口：%.0fU / 上限 %.0fU\n"
               "单笔：保证金 %.0fU × %d倍 = 名义 %.0fU\n"
               "模式：%s ｜ 测试模式：%s ｜ 暂停：%s\n"
               "风控：连亏 %s 笔（熔断线 %s）｜ 今日 %s 笔 / 净 %+.2fU（熔断线 -%.0fU）\n"
               "对账：%s"
               % ("、".join(GROUPS), _fetch, len(open_pos_ref), MAX_OPEN, "、".join(open_pos_ref) or "-",
                  _exp, _exposure_cap(), MARGIN, LEV, NOTIONAL,
                  _be_mode(), "开" if TEST_MODE else "关", "是" if PAUSED[0] else "否",
                  RISK.get("consec_loss", 0), MAX_CONSEC_LOSS,
                  RISK.get("day_trades", 0), RISK.get("day_pnl", 0.0), DAILY_LOSS_LIMIT, _rec))
    elif cmd.startswith("解除手工仓") or cmd.startswith("手工仓已平"):
        # 用户 2026-09-16：手工仓平掉后，用这条指令解除护栏
        _cs = [c for c in re.split(r"[\s,，、]+", cmd) if c and c not in ("解除手工仓", "手工仓已平")]
        if not _cs or "全部" in cmd:
            _cs = sorted(MANUAL_COINS)
        _done = []
        for c in _cs:
            c = resolve_coin(c)[0] or c.upper()
            if c in MANUAL_COINS:
                MANUAL_COINS.discard(c)
                _done.append(c)
        STATE_DIRTY[0] = True
        notify("【指令】手工仓护栏已解除：%s\n"
               "（如果交易所其实还有该币的仓，下次对账会重新登记。）"
               % ("、".join(_done) if _done else "没有匹配到手工仓（当前：%s）"
                  % ("、".join(sorted(MANUAL_COINS)) or "无")))
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
        # ⚠️ 2026-09-17：原来只判 `"关" in cmd` / `("开" in cmd and "确认" in cmd)`，
        #    于是「实盘模式 **不要开** 确认」这种带否定词的说法会被判成**开启实盘**。
        #    这是全项目最高危的开关（一开就真的向币安发单），改成：先看否定词，再要求明确的开+确认。
        _neg_live = bool(re.search(r"不|别|取消|勿", cmd))
        if ("关" in cmd) and not re.search(r"不关|别关", cmd):
            _set_live(False)
            notify("【指令】真实下单层已切回 **影子模式**（只记录下单计划，不发任何委托）")
        elif ("开" in cmd) and ("确认" in cmd) and not _neg_live:
            _set_live(True)
            notify("【指令】⚠️ 真实下单层已切到 **实盘**！\n"
                   "之后的信号会真的向币安发单（双向持仓 / %d 倍杠杆 / 止损走 Algo 接口）。\n"
                   "要停就发「实盘模式 关」。" % LEV)
        else:
            notify("【指令】实盘开关需要二次确认：\n"
                   "· 开启实盘：实盘模式 开 确认\n· 关闭实盘：实盘模式 关\n当前：%s%s"
                   % (_be_mode(), "" if _BEXEC_OK else "（⚠️ 下单层未加载：%s）" % _BEXEC_ERR))
    elif cmd.startswith("测试模式"):
        # ⚠️ 2026-09-17：原来 `on = ("开" in cmd) or ("on" in cmd.lower())` —— 同样会把
        #    「测试模式 不要开」判成"开"。改成：明确二选一，含糊就不动配置并问清楚。
        _m_off = re.search(r"关|off", cmd, re.I)
        _m_on = re.search(r"开|on", cmd, re.I)
        _neg = re.search(r"不|别|取消|勿", cmd)
        if _m_off and not _m_on:
            on = False
        elif _m_on and not _m_off:
            on = False if _neg else True
        else:
            notify("【指令】测试模式要说明白：发「测试模式 开」或「测试模式 关」")
            return True
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
    // blob: 开头的才是聊天里的【内容图】（头像是 https），所以即使还没加载完也能识别出"这条带图"
    const blobs = Array.from(row.querySelectorAll('img')).filter(i => String(i.src).indexOf('blob:') === 0).length;
    out.push({id: it.getAttribute('id'), nimg: row.querySelectorAll('img').length, loaded: imgs, nblob: blobs,
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
        for attempt in range(6):
            # 每 2 次重试把页面重新载入一遍：实测「标题不符」多半是页面/搜索还没准备好，
            # 刷新一次比原地重试更有效（2026-09-16 凌晨重启时 3 个群连续 4 次失败）
            if attempt and attempt % 2 == 0:
                try:
                    page.goto(MSG_URL, wait_until="domcontentloaded", timeout=60000)
                    time.sleep(6)
                except Exception:
                    pass
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
        # ⚠️ 打不开时必须留下**可诊断的现场**：页面当时到底显示了什么？
        #    （是群被改名了？还是飞书改版把标题挪出了 TITLE_JS 的判定区域？）
        try:
            _seen = page.evaluate("() => (document.body.innerText || '').split(String.fromCharCode(10))"
                                  ".map(s => s.trim()).filter(Boolean).slice(0, 6)")
            log("   [%s] 打不开：页面当时显示 %s" % (name, " ｜ ".join(_seen or [])[:200]))
        except Exception:
            pass
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


def _stop_side_reason(coin, dirc, entry, stop, mkt=None, entry_is_cmp=False, txt=None):
    """止损落在错误一侧时，**归因**到底是"信号陈旧"还是"解析错了"（2026-09-17 联调实测新增）。

    实测事故（真实端到端联调）：博主那条 UNI 信号是 `CMP 6.719 / 止损 6.39`，
    而联调时市价已跌到 **6.244 —— 已跌破信号的止损**。此时拒绝开仓是**对的**，
    但旧文案一律写「判为解析错误」→ **错误归因**：解析没错，是这条信号**过期了**。
    照旧文案去找解析的毛病，方向就错了（这次联调就是这么发现它的）。

    返回拒绝原因字符串；不该拒绝时返回 None。
    """
    if not (isinstance(stop, (int, float)) and stop
            and isinstance(entry, (int, float)) and entry):
        return None
    d = 1 if str(dirc or "LONG").upper() == "LONG" else -1
    wrong = (d == 1 and float(stop) >= float(entry)) or (d == -1 and float(stop) <= float(entry))
    if not wrong:
        return None
    _mkt = float(mkt) if isinstance(mkt, (int, float)) and mkt else None
    _entry_is_market = False
    if _mkt:
        try:
            _entry_is_market = abs(float(entry) - _mkt) / _mkt <= 0.005     # 0.5% 内视为"就是市价"
        except Exception:
            _entry_is_market = False
    _t = str(txt or "")
    _looks_close = bool(_CLOSE_ANNOUNCE.search(_t)) if _t else False
    if entry_is_cmp or _entry_is_market:
        return ("市价 %.8g **已%s**信号的止损 %.8g —— 这条信号已经**失效（陈旧）**："
                "按 CMP 语义要用当前市价入场，而市价已在止损的另一侧，开仓等于立刻认亏，**不下单**"
                % (_mkt or float(entry), "跌破" if d == 1 else "涨破", float(stop)))
    return ("止损价 %.8g **%s** 入场价 %.8g —— %s的止损必须在%s%s，**不下单**"
            % (float(stop), "不低于" if d == 1 else "不高于", float(entry),
               "做多" if d == 1 else "做空", "下方" if d == 1 else "上方",
               "；而且这条读起来像**平仓/止损通报**而不是开仓信号，八成是解析错了（判为解析错误）"
               if _looks_close else "（判为解析错误）"))


def validate_plan(coin, dirc, entry, stop, tps, mkt, texts=None, entry_is_cmp=False):
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
    #    2026-09-17：不再一律写「解析错误」—— 由 _stop_side_reason 区分
    #    「市价已越过止损 → 信号失效（陈旧）」与「止损落在入场价错误一侧 → 解析错误」。
    _side = _stop_side_reason(coin, dirc, entry, stop, mkt, entry_is_cmp, t0)
    if _side:
        return _side, tps
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
            # ===== 手工仓护栏恢复（用户 2026-09-16：交易所有、纸面没有的仓 = 用户手工仓）=====
            for _c in (sv.get("manual") or []):
                if _c:
                    MANUAL_COINS.add(str(_c).upper())
            # 🆕 2026-09-17：图上信号的"点位记忆"（识别"同一笔的进展通报"用）
            for _r in (sv.get("img_sig_seen") or []):
                if isinstance(_r, dict) and _r.get("entry"):
                    IMG_SIG_SEEN.append(_r)
            if IMG_SIG_SEEN:
                log("已恢复图上信号点位记忆 %d 条（用于识别持仓进展通报）" % len(IMG_SIG_SEEN))
            if MANUAL_COINS:
                log("已恢复手工仓护栏：%s（机器人不会对它们发任何真单）"
                    % "、".join(sorted(MANUAL_COINS)))
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
    # ===== 取消息层自检：官方 API 可用就**完全不启动浏览器** =====
    # （2026-09-16 用户授权官方 API；实测图片是原图、延迟大降、重启盲窗从 4.5 分钟变几秒）
    _api_ok = api_bootstrap() if FETCH_MODE == "api" else False
    if FETCH_MODE == "api" and not _api_ok:
        log("   [取消息] 回退：改用浏览器爬网页（等价于 fetch_mode=browser）")
    ctx, pages = None, {}
    if _api_ok:
        # 恢复 API 游标（毫秒）
        try:
            for _g, _v in (sv.get("last_api") or {}).items():
                API_CURSOR[_g] = int(_v)
            if API_CURSOR:
                log("已恢复 API 游标：%s" % "、".join(
                    "%s→%s" % (g, datetime.datetime.fromtimestamp(v / 1000, CST).strftime("%m-%d %H:%M:%S"))
                    for g, v in API_CURSOR.items()))
        except Exception:
            pass
        # 首次用 API：没有游标的群按「最近 10 分钟」起算，不回补更早历史（免得一上来灌一堆老信号）
        for _g in GROUPS:
            if not API_CURSOR.get(_g):
                API_CURSOR[_g] = int((time.time() - 600) * 1000)
                log("   ↳ [%s] 首次用 API：游标从 10 分钟前开始（不回补更早的历史）" % _g)
        log("==== 取消息模式：飞书官方 API（不需要浏览器，重启没有 4.5 分钟盲窗）====")
        log("==== 开始实时监控（%d 个群，API 模式）====" % len(GROUPS))
    # 打开页面后的统一处理：⚠️ 绝不把游标抬到"页面最新"，否则停机期间的消息会被静默吞掉
    # （只被浏览器模式用到；定义在 guard 之外，好让主循环里的"重开页面"分支也能调它）
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

    # ⚠️ 这里只有**浏览器模式**要启动 Chromium 并逐群开页；API 模式完全不碰浏览器。
    #    （2026-09-16 沙箱端到端验证抓到过一个真 bug：主循环曾被误缩进到这个 guard 里面，
    #      API 模式下会"启动完就退出" → pm2 无限重启。所以主循环必须在 guard 之外。）
    catchup_total = 0          # 两种模式都要有这个变量（后面汇总通报会用到）
    with sync_playwright() as p:
        if not _api_ok:
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
                _gk = clear_singleton_locks(BASE + "/fs_bot")
                if _gk:
                    log("   ↳ 已清理 profile 锁：%s" % _gk)
                ctx = launch_persistent(p, BASE + "/fs_bot")
            pages = {}
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
                    # ⚠️ 2026-09-16 实测缺陷：原来打不开也照样 pages[g]=pg（一个**没通过标题校验**的页面），
                    #    而主循环只在 pages[g] 为 None 或已关闭时才重开 → 这个群会**一直停在错误的页面上**：
                    #    既读不到该群消息（静默变瞎），又可能把别的会话的消息当成这个群的信号（串台）。
                    #    现在：关掉这个未经验证的页面、置 None（交给主循环持续重开），并**立即告警**。
                    log("[%s] 打开失败（未读到消息）→ 该群暂不监控，交给主循环持续重开" % g)
                    try:
                        pg.close()
                    except Exception:
                        pass
                    pages[g] = None
                    notify("【机器人告警】群「%s」这次开机没能打开（已重试 6 次：会话列表点击 + Ctrl+K 搜索）\n"
                           "这个群现在是**盲区**，我会在主循环里继续重开；期间它发的新信号可能收不到。" % g)
            log("==== 开始实时监控（%d 个页面）====" % len(pages))
        # ===== 以下两种模式都跑：落盘状态 → 预热行情 → 主循环 =====
        # ⚠️ 不要在这里写 {"open": []}，会把已恢复的持仓清空（曾经踩过这个坑）
        json.dump({"open": open_pos, "last": last_id, "seen": sorted(SEEN)[-800:], "risk": RISK,
                   "asking": _asking_dump(),
                   "paused": bool(PAUSED[0]),
                   "manual": _manual_dump(),      # 用户手工仓护栏（机器人不干涉）
                   "img_sig_seen": _img_sig_dump(),   # 图上信号点位记忆（识别持仓进展通报）
                   "last_api": dict(API_CURSOR),  # API 模式的消息游标（毫秒）
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
            # ===== 取消息模式：官方 API（不碰浏览器）/ 爬网页（兜底）=====
            _use_api = (FETCH_MODE == "api") and API_READY[0] and _FAPI_OK
            if _use_api:
                to_scan, changed, missing = list(GROUPS), [], []
                safety += 1
            else:
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
                if g not in to_scan and not _use_api:
                    continue
                if (not _use_api) and (page is None or page.is_closed()):
                    # ⚠️ 旧代码在 page 为 None 时直接 continue —— 那个群会永久停止监控，且日志里毫无提示。
                    #    现在改为主动重开；重开后保留原游标，停机期间的消息由回补闸门处理。
                    _reopen_fail[g] = _reopen_fail.get(g, 0) + 1
                    _nf = _reopen_fail[g]
                    if _nf <= 2 or _nf % 20 == 0:     # 日志降噪：事故时这两行刷了 2488 条
                        log("[%s] 页面不存在/已关闭，正在重新打开…（连续第 %d 次）" % (g, _nf))
                    try:
                        _pg, _rows = open_group_page(ctx, g)
                        if not _rows:
                            # 打开了页面但**没通过标题校验/没读到消息** → 不能当成成功
                            # （否则这个群会停在一个错误的页面上：读不到自己的消息，还可能串台）
                            try:
                                _pg.close()
                            except Exception:
                                pass
                            pages[g] = None
                            if _nf in (3, 10) or _nf % 50 == 0:
                                log("[%s] 重开后仍未通过标题校验（连续第 %d 次）" % (g, _nf))
                                notify("【机器人告警】群「%s」连续 %d 次没能打开（页面打开了但标题对不上）\n"
                                       "该群当前是**盲区**，我会继续重开；期间它发的信号可能收不到。"
                                       % (g, _nf))
                            continue
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
                    if _use_api:
                        # ===== 官方 API 取消息（不需要浏览器）=====
                        # 每个群最多每 2 秒轮询一次（飞书接口 1000 次/分钟，这样 4 个群 ≈ 120 次/分钟，
                        # 又足够快：信号从发出到推送 1~2 秒）
                        if time.time() - API_LAST.get(g, 0) < 2.0:
                            continue
                        API_LAST[g] = time.time()
                        _cid = API_CHAT_IDS.get(g)
                        if not _cid:
                            _cid = fapi.resolve_chat_ids([g]).get(g)
                            if _cid:
                                API_CHAT_IDS[g] = _cid
                        if not _cid:
                            if safety % 200 == 1:
                                log("[%s] ⚠️ 在飞书 API 里找不到这个群（名字要完全一致）" % g)
                            continue
                        rows, _err = fapi.fetch_new(_cid, API_CURSOR.get(g, 0), IMGDIR, skip_ids=SEEN)
                        if _err:
                            API_FAILS[0] += 1
                            log("[%s] ⚠️ API 取消息失败（连续第 %d 次）：%s" % (g, API_FAILS[0], _err))
                            if API_FAILS[0] in (3, 10) or API_FAILS[0] % 50 == 0:
                                # 令牌失效/刷新失败 → 附上"重新授权链接"，让他点一下就能恢复
                                _hint2 = ""
                                if ("令牌" in _err) or ("授权" in _err) or ("401" in _err):
                                    try:
                                        _hint2 = "\n\n" + fapi.reauth_hint()
                                    except Exception:
                                        _hint2 = ""
                                notify("⚠️【取消息告警】官方 API 连续 %d 次取不到消息：%s\n"
                                       "（也可以把 runtime_config.json 的 fetch_mode 改成 browser 先用浏览器兜底）%s"
                                       % (API_FAILS[0], _err, _hint2))
                            continue
                        API_FAILS[0] = 0
                        if rows:
                            API_CURSOR[g] = max(API_CURSOR.get(g, 0),
                                                max(int(r["t_sig"]) * 1000 for r in rows) + 1)
                            STATE_DIRTY[0] = True
                    else:
                        page.mouse.move(900, 400); page.mouse.wheel(0, 2600); time.sleep(0.3)
                        rows = page.evaluate(SCAN_JS)
                    if not rows:
                        finalize_pending(open_pos)          # 方案B：每个群扫完就检查一次出单
                        rawq_sweep(g)                       # B16：4 秒内没关联上文字的图 → 单独推送
                        continue
                    base = 0 if _use_api else last_id.get(g, 0)
                    cand = sorted([r for r in rows if r.get("id") and int(r["id"]) > base],
                                  key=lambda r: int(r["id"]))
                    # ===== 回补闸门（2026-09-13）替代旧的「发现 >15 条就静默全丢」=====
                    # ① 超过 CATCHUP_MAX_AGE 的老信号 -> 只通报不下单，避免拿过期点位追单
                    # ② 超过 CATCHUP_MAX_MSGS 的 -> 只处理最新那批，其余只通报
                    # 两条路径都推进游标 + 标记已处理，确保永不重复，但绝不静默丢弃
                    now_ts = time.time()
                    fresh, gated = [], []
                    for r in cand:
                        if now_ts - _row_t_sig(r) > CATCHUP_MAX_AGE:
                            gated.append(("超时效", r))
                        else:
                            fresh.append(r)
                    if len(fresh) > CATCHUP_MAX_MSGS:
                        gated.extend(("超数量上限", r) for r in fresh[:-CATCHUP_MAX_MSGS])
                        fresh = fresh[-CATCHUP_MAX_MSGS:]
                    for _why, r in gated:
                        _mid = r["id"]
                        mark_seen(_mid)
                        # ⚠️ API 模式的 id 与网页版 id 不同源，**不能互相污染游标**
                        if not _use_api:
                            last_id[g] = max(last_id.get(g, 0), int(_mid))
                        STATE_DIRTY[0] = True
                        missed_sig.append((g, _why, _row_t_sig(r), (r.get("text") or "")[:70]))
                    if gated:
                        log("[%s] 回补闸门拦下 %d 条（超时效 %d / 超上限 %d）：只通报不下单"
                            % (g, len(gated), sum(1 for w, _ in gated if w == "超时效"),
                               sum(1 for w, _ in gated if w == "超数量上限")))
                    new = fresh
                    for r in new:
                        mid = r["id"]
                        t_sig = _row_t_sig(r)
                        when = datetime.datetime.fromtimestamp(t_sig, CST).strftime("%m-%d %H:%M:%S")
                        txt = strip_sender_prefix(r["text"])   # 去掉行首的发送者名（"自定义机器人 BOT" 里的 BOT 是真实交易对，会误导币种识别）
                        LAST_MSG_TS[0] = time.time()           # 失联看门狗用：只要抓到任何一条消息就刷新
                        log("[%s] 发现新消息 | 发出=%s | %s" % (g, when, txt[:110]))
                        if int(mid) in SEEN:
                            log("   ↳ 该消息此前已处理过，跳过（防重复开单）")
                            if not _use_api:
                                last_id[g] = max(last_id.get(g, 0), int(mid))
                            continue
                        mark_seen(mid)
                        # 逐条推进游标：进程若中途挂掉，重启后只会重放（SEEN 挡住重复开单），不会丢单
                        if not _use_api:
                            last_id[g] = max(last_id.get(g, 0), int(mid))
                        STATE_DIRTY[0] = True
                        low = txt.lower()
                        # ⚠️ 2026-09-15 实测事故：用户发「全部平仓」后，机器人把自己的
                        #    【已平仓·纸面】通知**当成博主信号重新解析**，播报出 5 条假的
                        #    「【博主指令】xxx 动作：close_all」。根因就是这张表漏了「【已平仓」。
                        #    ⚠️ 2026-09-16 又犯一次：新加的「【手工仓护栏】…LSK…」没登记 →
                        #    机器人把自己的告警当成一条 LSK 信号。前缀表在模块级 SELF_MARKS，
                        #    并有一条自检（源码里 notify 用到的前缀必须都在表里）盯着这件事。
                        if any(_m in txt for _m in SELF_MARKS) or "通过webhook" in txt \
                                or "invited" in low or "test notification" in low \
                                or "（你的指令：" in txt or "本次盈亏：" in txt \
                                or "该档盈亏：" in txt or "累计：" in txt:
                            continue
                        # 🆕 2026-09-17 自环根治（00:34「不开」与 09:26「没回复却开单」两次事故）：
                        #   我们自己发的通知在飞书里是 sender_type="app"；CMD_GROUPS 是**我们自己的群**，
                        #   里面只会有"你发的"和"机器人发的"两种消息 → 指令群里 app 发的消息一律跳过：
                        #   既不当信号解析，也**绝不当成你的指令**。
                        #   ⚠️ 绝不能写成"凡 app 发的都跳过"：博主信号**也是 app 发的**（实测
                        #      黄金mansoor / UA-nurseneil2 的卡片全是 app，且 app_id 与我们相同），
                        #      那样会把真信号全杀光。所以只在 CMD_GROUPS 里生效，KOL 群行为不变。
                        if _is_self_app_row(g, r.get("sender_type")):
                            log("   ↳ 这是我们自己的通知（app 发送 + 指令群）→ 不当信号、也不当指令")
                            continue
                        # 指令优先：只有在指定指令群里、由你发的短消息才会被当成指令
                        if g in CMD_GROUPS:
                            try:
                                if handle_command(txt):
                                    continue
                            except Exception as _e:
                                log("   指令处理异常 " + str(_e)[:90])
                        # 门口关键词表在模块级 SIG_KW_GATE（便于自检直接校验，见 [8n]）
                        _has_kw = any(k in txt for k in SIG_KW_GATE)
                        # ⚠️ B16 修复（2026-09-15 实测）：原来是
                        #    has_img = loaded>0 or nimg>=2 —— 要求"图已经加载完"或"≥2 个图片元素"。
                        #    单张图**还没加载完**时 nimg=1 / loaded=0 → 被当成"没图、也没信号词" →
                        #    在关键词门槛就判「闲聊/无关，跳过」，**连抓图都不会尝试**。
                        #    21:57 那条 UNI 图消息就是这样丢的（v21/imgs 里根本没有这张文件）。
                        #    实测纯文本消息 nimg=0，所以 nimg>0 就是"这条消息带图元素"。
                        has_img = msg_has_image(r)
                        if not _has_kw and not has_img:
                            log("   ↳ 闲聊/无关，跳过")
                            # 🆕 2026-09-17：在**指令群**里、**你**发的、像指令却没认出来的消息，
                            #   必须回一句（用户报障"我回复开仓，机器人没有理我"就是栽在这里）。
                            _chitchat_hint(g, r.get("sender_type"), txt)
                            continue
                        t_found = time.time()
                        # 图：API 模式是**已经下载好的原图**（更清晰、更准）；浏览器模式才去抓 blob
                        imgs = []
                        if _use_api:
                            imgs = [p for p in (r.get("_imgs") or []) if p and os.path.exists(p)]
                            if r.get("nimg", 0) or imgs:
                                log("   媒体(API): 图元素=%d 已下载原图=%d %s"
                                    % (r.get("nimg", 0), len(imgs),
                                       "｜".join(os.path.basename(x) for x in imgs[:3])))
                        elif r.get("nimg", 0) > 0:
                            wait_ms = img_wait_ms(r, _has_kw)
                            data = page.evaluate(FETCH_IMG_JS, {"mid": mid, "waitMs": wait_ms})
                            for i, d in enumerate(data or []):
                                if isinstance(d, str) and d.startswith("data:image"):
                                    fn = IMGDIR + "/" + str(mid) + "_" + str(i) + ".png"
                                    try:
                                        open(fn, "wb").write(base64.b64decode(d.split(",", 1)[1])); imgs.append(fn)
                                    except Exception:
                                        pass
                            log("   媒体: 元素=%d 已加载=%d blob=%d 抓到图=%d%s"
                                % (r.get("nimg", 0), r.get("loaded", 0), r.get("nblob", 0), len(imgs),
                                   "" if imgs else "  ← 有图元素但没抓到内容图（未加载/非 blob）"))
                        t_img = time.time()
                        # ===== 🆕 结单/止损通报：**最先挡掉**（用户 2026-09-16 要求）=====
                        # 「这是触发了止损，博主说一声，并不是开单信号，无用，以后这样的消息直接忽略，不要推送。」
                        # 放在多币种拆分**之前**，任何分支都无法把它推成【博主指令】。
                        # 只含"止盈达成"的通报 → 按用户第 9 条回报**你自己的持仓**。
                        if _CLOSE_ANNOUNCE.search(txt):
                            _c0 = find_coin_in_text(txt)
                            _tp_only = bool(re.search(
                                r"(TP\s?\d?\s*(hit|nailed|done|reached|filled)|take[- ]?profit\s*(hit|reached)|"
                                r"止盈.{0,6}(达成|到了|命中|触发|已到))", txt, re.I)) and not bool(re.search(
                                r"(stop[ -]?loss|stopped\s+out|止损|平仓|closed|结单)", txt, re.I))
                            log("   ↳ 判定为【%s通报】→ %s：%s"
                                % ("止盈" if _tp_only else "结单/止损",
                                   "回报你的持仓" if _tp_only else "按你的要求直接忽略、不推送", txt[:90]))
                            if _tp_only and _c0 and _c0 in open_pos:
                                try:
                                    pos_report(_c0, open_pos[_c0])
                                except Exception as _e:
                                    log("   持仓汇报失败 " + str(_e)[:80])
                            continue
                        # ===== 方案C+D：本地正则先解析；需要 AI 时才调，且与读图并行 =====
                        # ===== 多币种消息：按币拆开、各自解析，然后逐个问用户（用户 2026-09-14 要求）=====
                        # 例：「原油Cl跌破97空，100.7止损，93止盈。 Sol突破102.5多，止损100，止盈107到110。」
                        # 不再整条当成"笼统总结"丢掉，而是拆成 CL / SOL 两条独立信号请你逐个确认。
                        _segs = split_by_coin(txt)
                        if len(_segs) >= 2:
                            log("   ↳ 多币种消息，按币拆开：%s" % [c for c, _ in _segs])
                            _names = []
                            # 🆕 2026-09-17 用户报障修复：多币种分支原来**完全不读图**
                            #   （"图白下载了"）。现在读一次；**只有图上币种与该段币种一致时**
                            #   才把图上点位并进去 —— 绝不把同一张图的止损/止盈挂到别的币上。
                            _mc_chart, _mc_meta = {}, {}
                            if imgs:
                                try:
                                    _mc_chart = read_chart_cached(imgs[0]) or {}
                                    _mc_meta = read_chart_meta(imgs[0]) or {}
                                except Exception as _e:
                                    log("   ⚠️ 多币种分支读图异常：%s" % str(_e)[:100])
                                if _mc_chart.get("ok"):
                                    log("   ↳ 多币种分支已读图：图上币种=%s ｜ 止损 %s 开仓 %s 止盈 %s"
                                        % (_mc_meta.get("coin") or "-", _mc_chart.get("sl"),
                                           _mc_chart.get("entry"), _mc_chart.get("tps")))
                                else:
                                    log("   ↳ 多币种分支：图已读但没读出可用点位（不静默丢）")
                            for _c, _seg in _segs:
                                _cf = fast_parse(_seg) or {}
                                if AI_FIRST[0]:
                                    # AI 优先模式下，多币种的每一段也走 AI 主解析 + 反幻觉校验
                                    _ci, _cn = merge_parse_results(_seg, parse_text(_seg), _cf)
                                    _ci = _ci or {}
                                    if _cn:
                                        log("   🤖 [AI优先·多币种] %s：%s" % (_c, _cn))
                                else:
                                    _ci = dict(_cf)
                                    if (not _ci.get("direction")) or fast_parse_suspect(_seg, _ci):
                                        _ai = parse_text(_seg) or {}
                                        _ci = {**(_ci or {}), **{k: v for k, v in _ai.items()
                                                                 if v not in (None, [], "")}}
                                _dir_ = (_ci.get("direction") or "").upper() or None
                                if not _dir_:
                                    log("   ↳ %s 段没解析出方向 → 只记录不询问" % _c)
                                    continue
                                PENDING.pop(_c, None)
                                _use_chart = (_mc_chart if (_mc_chart.get("ok")
                                                            and (_mc_meta.get("coin") or "").upper() == _c)
                                              else None)
                                if _mc_chart.get("ok") and not _use_chart:
                                    log("   ↳ 多币种分支：图上的币种(%s)与这一段(%s)不一致 → 只留图、不用图上的点位"
                                        % (_mc_meta.get("coin") or "-", _c))
                                _pp = merge_pending(_c, g, info=_ci, txt=_seg, chart=_use_chart,
                                                    imgs=(imgs if _use_chart else None),
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
                                                               texts=[_seg],
                                                               entry_is_cmp=bool(_pp.get("entry_is_cmp")))
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
                                notify("【信号·分币】· %s %s：开仓=%s 止损=%s 止盈=%s"
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
                        if not AI_FIRST[0]:
                            # —— 老行为（正则优先）：正则自称可疑才叫 AI ——
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
                        # ===== AI 优先：AI 与读图**并行**跑（不额外增加等待）=====
                        _ait, _aires = None, {}
                        if AI_FIRST[0]:
                            _ait = threading.Thread(target=lambda: _aires.update({"ai": parse_text(txt)}))
                            _ait.start()
                        elif info is None:
                            _ait = threading.Thread(target=lambda: _aires.update({"ai": parse_text(txt)}))
                            _ait.start()
                        if _ait:
                            _ait.join()
                            if AI_FIRST[0]:
                                info, _anote = merge_parse_results(txt, _aires.get("ai"), _fast)
                                if _anote:
                                    log("   🤖 [AI优先] %s" % _anote)
                            else:
                                info = _aires.get("ai") or info
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
                            _vf = chart.get("verify") or {}
                            _gg = chart.get("geo") or {}
                            log("   读图[%s]: 止损 %s 开仓 %s 止盈 %s ｜ 关键标签复核 %s 个（一致 %s / 单标签 %s / "
                                "批量 %s / 判为未读到 %s）%s"
                                % (chart.get("mode") or "?", chart["sl"], chart["entry"], chart["tps"],
                                   _vf.get("checked"), _vf.get("agreed"), _vf.get("single_only"),
                                   _vf.get("batch_only"), _vf.get("dropped"),
                                   (" ｜ 色框=%s" % _gg.get("boxes") if _gg.get("used") else "")))
                        raw_coin = info.get("coin")
                        coin, coin_ok = (resolve_coin(raw_coin) if raw_coin else (None, False))
                        if raw_coin and coin and not coin_ok:
                            log("   ⚠️ 币种 %s（原文写法 %s）不在币安 USDT-M 清单里" % (coin, raw_coin))
                            notify("【信号·不支持】%s\n币安 USDT-M 没有这个币种的合约（原文写法：%s）\n我们只交易 USDT 计价的合约。\n原文：%s"
                                   % (coin, raw_coin, txt[:160]))
                            continue
                        dirc = (info.get("direction") or "").upper() or None
                        # 只有图、文字里没有币种 -> 从图上读币种
                        _meta = {}
                        if coin is None and imgs:
                            _meta = read_chart_meta(imgs[-1]) or {}
                            mc, mc_ok = resolve_coin(_meta.get("coin"))
                            if _meta.get("is_chart") and mc and mc_ok:
                                coin = mc
                                dirc = dirc or ((_meta.get("direction") or "").upper() or None)
                                log("   图上读到币种: %s %s" % (coin, dirc))
                        # 这条消息的【文字】本身到底提供了什么？（B16：区分"文字信号"与"只有图"）
                        _text_info = any(info.get(k) for k in
                                         ("coin", "direction", "entry", "stop", "add_price", "targets",
                                          "entryRange", "entryLegs", "stopRange", "stopPct"))
                        t_chart = time.time()
                        stamps = {"found": t_found, "img": t_img, "parse": t_parse, "chart": t_chart}
                        # ===== ① 结单/止损通报：上面（拆币之前）已经挡掉，这里不再重复 =====
                        # ===== ② 博主管理指令（术语一律中文；用户 2026-09-16 要求）=====
                        _ACT_CN = {"close_all": "全部平仓", "trim": "减仓", "move_stop_to_cost": "止损移到开仓价",
                                   "other": "其它"}
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
                            # 用户要求：术语一律中文（原来推的是 close_all 这种英文）
                            notify("【博主指令】%s\n群：%s  时间：%s\n动作：%s\n原文：%s"
                                   % (coin or "?", g, when, _ACT_CN.get(_act, _act), txt[:200]))
                            continue
                        # （结单/止盈止损通报 已在上面第 ① 挡处理，这里不再重复判断）
                        if coin and dirc in ("LONG", "SHORT") and _text_info:
                            # 开单信号 -> 进待确认池，等同一条信号的后续消息（卡片/图）补齐
                            p = merge_pending(coin, g, info=info, chart=chart, imgs=imgs, t_sig=t_sig, txt=txt, stamps=stamps)
                            if dirc: p["dir"] = dirc
                            # B16：把【同群 4 秒内暂存的图】回填关联到这条信号（图常常是独立一条消息，
                            # 它自己没有币种，挂不进 PENDING[coin]，原来就在这里被丢掉）
                            rawq_bind(g, coin, p)
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
                            if coin in PENDING:
                                rawq_bind(g, coin, PENDING[coin])
                        elif imgs:
                            # ===== B16：带图但【没有可用文字信号】的消息 =====
                            # 用户要求：图片必须能被读到、不能丢。这里先按「群 + 时间窗」原样暂存，
                            # 不要求币种；4 秒内若有文字带来币种 → 回填关联；没等到 → 单独推送图的信息。
                            # 注意：只有图（币种/方向是图上读出来的）**不建可审批单** ——
                            # 用户明确：缺失的信息绝不瞎猜，只推送分析。
                            if coin and dirc in ("LONG", "SHORT") and not _text_info:
                                log("   📎 本条只有图（图上读到 %s %s），无文字信号 → 不建可审批单，转暂存"
                                    % (coin, dirc))
                            # 反向顺序也要覆盖：文字先到、图后到 → 直接并入那条还没结束的信号
                            _att = rawq_attach_pending(g, imgs, chart, meta=_meta, txt=txt, mid=mid)
                            if not _att:
                                _rec = rawq_add(g, {"kind": "img", "mid": mid, "t_sig": t_sig, "g": g,
                                                    "text": txt, "imgs": list(imgs), "chart": chart,
                                                    "meta": _meta, "when": when, "coin": coin, "dir": dirc})
                                log("   📎 [图] %d 张已暂存（本条没有可用文字：币种=%s 方向=%s）→ %.0f 秒内"
                                    "等同群文字回填，没等到就单独推送"
                                    % (len(imgs), coin or "-", dirc or "-", RAWQ_WAIT))
                        else:
                            # ⚠️ 2026-09-13：这里以前是【什么都不做、也不留一行日志】的静默丢弃。
                            #    实例：13:56 黄金mansoor 发「XAUUSD 👀 + 推文链接 + 图」，
                            #    图都抓到了，却既没下单、也没任何记录 —— 你完全不知道错过了什么。
                            log("   ↳ 没通过信号门槛（币种=%s 方向=%s 图=%d 类型=%s），未下单：%s"
                                % (coin or "-", dirc or "-", len(imgs), info.get("type") or "-", txt[:100]))
                            _looks_signal = bool(coin) and (
                                bool(imgs) or any(k in txt for k in ("止损", "止盈", "Entry", "SL", "TP")))
                            # B16 配套：**有文字、有价位、但没认出币种**的也算信号，不能静默丢弃
                            # （原则「绝不静默丢弃」；原来这种消息只留一行日志，你根本不知道错过了）
                            _no_coin_sig = (not coin) and (not imgs) and bool(re.search(r"[0-9]", txt)) and any(
                                k in txt for k in ("止损", "止盈", "Entry", "SL", "TP",
                                                   "做多", "做空", "long", "short"))
                            if (_looks_signal or _no_coin_sig) and (
                                    time.time() - _UNIDENT_NOTIFY.get(g, 0) > UNIDENT_NOTIFY_COOLDOWN):
                                _UNIDENT_NOTIFY[g] = time.time()
                                if _no_coin_sig:
                                    notify("【信号·未能识别】%s\n这条**文字**里有价位，但没能认出币种 "
                                           "→ **未下单**\n解析到：%s\n原文：%s"
                                           % (g,
                                              {k: v for k, v in (info or {}).items()
                                               if k in ("direction", "entry", "stop", "targets")},
                                              txt[:200]))
                                else:
                                    # 🆕 2026-09-17 用户要求：**没识别出方向 / 缺开仓价** 时也要推送，
                                    #   并把"读到了什么、缺了什么"一条条写清楚（绝不猜、不默认做多）。
                                    _tps0 = sorted(set((info or {}).get("targets") or []))
                                    _miss0 = []
                                    if not (info or {}).get("direction"):
                                        _miss0.append("方向")
                                    if (info or {}).get("entry") is None:
                                        _miss0.append("开仓价")
                                    if (info or {}).get("stop") is None:
                                        _miss0.append("止损")
                                    if not _tps0:
                                        _miss0.append("止盈")
                                    notify("【信号·未能识别】%s\n"
                                           "识别到币种 **%s**，但没读全 → **未下单**，等你确认\n"
                                           "读到：方向=%s ｜ 开仓=%s ｜ 止损=%s ｜ 止盈=%s\n"
                                           "缺的：%s%s\n"
                                           "（缺的一律写「未读到」，绝不替你猜）\n原文：%s"
                                           % (g, coin,
                                              (info or {}).get("direction") or "未读到",
                                              fmt_price((info or {}).get("entry")),
                                              fmt_price((info or {}).get("stop")),
                                              (" / ".join(fmt_price(x) for x in _tps0) if _tps0 else "未读到"),
                                              "、".join(_miss0) or "无",
                                              ("，图已抓到 %d 张（读了但没读出可用的方向/点位）" % len(imgs))
                                              if imgs else "，无图",
                                              txt[:200]))
                    # 方案B：本群处理完立刻检查一次出单（不再等整轮扫完 5 个群）
                    finalize_pending(open_pos)
                    rawq_sweep(g)                       # B16：4 秒内没关联上文字的图 → 单独推送
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
                # 🆕 2026-09-17：API 模式下**根本没有浏览器页面**（page=None），
                #   原来这里会去开页 → 每个群都报一次"开页失败"，还要发一条误导性通知。
                #   API 模式下新增群只需要游标（下一轮轮询自动开始取消息）。
                if FETCH_MODE == "api":
                    for _g in [x for x in GROUPS if x not in pages]:
                        pages[_g] = None
                        log("[热加载] API 模式新增监控群 %s（无需开页面，下一轮轮询自动开始取消息）" % _g)
                        _chg.append("新增 %s（API 模式，无需开页）" % _g)
                else:
                    for _g in [x for x in GROUPS if x not in pages]:          # 新增 -> 立刻开页面
                        log("[热加载] 新增监控群 %s，正在开页面（约 1 分钟）…" % _g)
                        try:
                            _pg, _rows = open_group_page(ctx, _g)
                            pages[_g] = _pg
                            adopt_page(_g, _rows)                             # 无游标则从页面最新起步
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
            # B16：图暂存区里 4 秒内没关联上文字的 → 单独推送（用户要求③）
            try:
                rawq_sweep()
            except Exception as e:
                log("图暂存区处理异常 " + str(e)[:100])
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
                       "manual": _manual_dump(),      # 手工仓护栏落盘（机器人不干涉用户手工仓）
                       "img_sig_seen": _img_sig_dump(),   # 图上信号点位记忆（识别持仓进展通报）
                       "last_api": dict(API_CURSOR),  # API 模式消息游标（毫秒）
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
        _before_size = os.path.getsize(_prod_log)
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
        # ⚠️ 判据修正（2026-09-16）：原来拿"行数不变"当隔离判据，但机器人进程本身每 10 秒写一条
        #    心跳，只要它活着行数必然涨 → 这个断言会**永远误报**（B1 自检要跑好几分钟，必中）。
        #    真判据：自检开始【之后新增】的那些行里，不许有本次自检写入的内容。
        with open(_prod_log, "rb") as _f:
            _f.seek(_before_size)
            _appended = _f.read().decode("utf-8", "replace")
        _leak = [l for l in _appended.splitlines()
                 if ("B1 自检" in l or "b1_selftest" in l or "自愈" in l and "B1" in l)]
        print("  生产 run.log 行数：自检前 %d → 自检后 %d（新增 %d 行）  %s"
              % (_before_lines, _after_lines, _after_lines - _before_lines,
                 "[ OK ] 新增行里没有自检痕迹" if not _leak else "[FAIL] 被写入了！"))
        print("      ↳ 新增行示例：%s" % ((_appended.splitlines() or ["-"])[-1][:70]))
        if _leak:
            _fail.append("隔离：生产 run.log 被写入（%d 行）" % len(_leak))
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

        # ---------- AI 优先：反幻觉 + 合并 ----------
        print("\n[6] AI 优先：反幻觉硬约束（AI 编的数字必须被抓住）")
        _msg = "BTC 做多 止损74000 止盈 79000 80000"
        _ai_ok = {"coin": "BTC", "direction": "LONG", "stop": 74000, "targets": [79000, 80000], "type": "open"}
        _ck("AI 数字都出自原文 → 无问题", verify_ai_numbers(_msg, _ai_ok), [])
        _ai_bad = dict(_ai_ok); _ai_bad["stop"] = 73500       # 原文里没有 73500
        _ck("AI 编了一个原文没有的止损 → 必须被抓住", verify_ai_numbers(_msg, _ai_bad), [73500.0])
        _ai_bad2 = dict(_ai_ok); _ai_bad2["entry"] = 77777.0
        _ck("AI 编了开仓价 → 必须被抓住", verify_ai_numbers(_msg, _ai_bad2), [77777.0])
        _ck("带 $ 和千分位也能对上",
             verify_ai_numbers("ENTRY: $77,760", {"entry": 77760.0}), [])
        _ck("元数据字段（direction/coin/type）不计入数字校验",
             verify_ai_numbers("BTC 做多", {"coin": "BTC", "direction": "LONG", "type": "open"}), [])

        print("\n[7] AI 优先：正则补齐 AI 漏档（AI 少读不能被静默吞掉）")
        _rg = fast_parse("BTC 做多 止损74000 第一止盈79000 第二止盈80000") or {}
        _ai_short = {"coin": "BTC", "direction": "LONG", "stop": 74000, "targets": [79000]}
        _mg, _note = merge_parse_results("BTC 做多 止损74000 第一止盈79000 第二止盈80000", _ai_short, _rg)
        _ck("AI 只读到 1 档 → 正则补齐到 2 档", len(_mg.get("targets") or []), 2)
        print("     ↳ 说明：%s" % _note)
        _mg2, _n2 = merge_parse_results("BTC 做多 止损74000", None, _rg)
        _ck("AI 无结果 → 降级用正则（不是丢单）", bool(_mg2), True)
        _mg3, _n3 = merge_parse_results("BTC 做多 止损74000 止盈73500", _ai_bad, _rg)
        _ck("反幻觉未过 → 打上 _ai_unverified 标记（后续强制审批）",
             bool(_mg3.get("_ai_unverified")), True)

        print("\n" + "-" * 72)
        if _fail:
            print("B5/B10/B3 自检：%d 项失败" % len(_fail))
            for _f in _fail:
                print("   ✗ %s" % _f)
            sys.exit(1)
        print("B5/B10/B3 自检：全部通过 ✅")
        sys.exit(0)

    if "--selftest-imgmerge" in sys.argv:
        # ===== B16 自检：图片消息不许丢（抓图门槛 / 群+时间窗暂存 / 回填关联 / 单独推送）=====
        # 用例全部取自【真实生产证据】：
        #   · 21:57:19 那条没有文字、只有图的消息（run.log：预览文本只有发送者名「用户963038」
        #     → 被判「闲聊/无关，跳过」；v21/imgs 里没有对应文件）
        #   · 19:03:10 那张真实落盘的 UNI 图（v21/imgs/7685715944936623383_0.png）
        #     + 21:57 那条真实 UNI 文本
        # 完全隔离在 /tmp：不碰生产配置/状态/日志/图片目录，不发飞书，不下单。
        import shutil, hashlib
        _T = "/tmp/imgmerge_selftest"
        _PROD = "/home/ubuntu/signal-bot"
        _prod_log = _PROD + "/v21/run.log"
        _prod_state = _PROD + "/v21/state.json"
        _prod_rt = _PROD + "/runtime_config.json"
        _prod_bot = _PROD + "/dryrun_bot2.py"
        _prod_imgdir = _PROD + "/v21/imgs"
        _before_lines = sum(1 for _ in open(_prod_log, encoding="utf-8", errors="replace"))
        _before_size = os.path.getsize(_prod_log)
        _mt = {p: os.path.getmtime(p) for p in (_prod_rt,)}
        # ⚠️ state.json 不能比 mtime：机器人本身每轮都在重写它（含心跳 ts），那不是"被测试改了"。
        #    改成比【内容里的持仓数】，这才是真的"没人动过生产状态"。
        _prod_open_before = len((json.load(open(_prod_state, encoding="utf-8")) or {}).get("open") or {})
        _md5 = hashlib.md5(open(_prod_bot, "rb").read()).hexdigest()
        _img_before = set(os.listdir(_prod_imgdir))
        shutil.rmtree(_T, ignore_errors=True)
        os.makedirs(_T, exist_ok=True)
        RUNTIME = _T + "/runtime_config.json"
        TRADES = _T + "/trades_dryrun.jsonl"
        STATE = _T + "/state.json"
        LOGF = _T + "/run.log"
        IMGDIR = _T + "/imgs"
        NOTIFY_CFG = _T + "/notify.json"      # 不存在 → 不发飞书
        RUN = _T + "/run"                     # 读图中间产物也不许落到生产 v21/
        os.makedirs(IMGDIR, exist_ok=True)
        os.makedirs(RUN, exist_ok=True)

        # 捕获通知文本（既验证内容，又保证不真的推飞书）
        _SENT = []
        _real_notify = notify

        def notify(text):                      # noqa: F811 —— 只在本次自检里替换
            _SENT.append(str(text))
            log("[通知] " + str(text).replace("**", "").replace("\n", " | ")[:200])

        print("=" * 72)
        print("B16 自检：图片消息不能再丢（门槛 / 暂存 / 回填关联 / 单独推送）")
        print("=" * 72)
        print("路径重定向证明（全部在 /tmp，生产零写入）：")
        for _k in ("RUNTIME", "TRADES", "STATE", "LOGF", "IMGDIR", "NOTIFY_CFG", "RUN"):
            print("  %-11s = %s" % (_k, eval(_k)))
        _fail = []

        def _chk(name, got, want):
            _ok = (got == want)
            print("  %s %-56s got=%s want=%s" % ("[ OK ]" if _ok else "[FAIL]", name, got, want))
            if not _ok:
                _fail.append(name)

        # ---------- ① 抓图门槛：未加载的单张图也必须算"有图" ----------
        print("\n[1] 抓图门槛（21:57 事故复刻：nimg=1 / loaded=0 被判成'没图'）")
        _row_2177 = {"id": "7685776121886936012", "nimg": 1, "loaded": 0, "nblob": 1,
                     "text": "用户963038"}
        _row_text = {"id": "7685776121886936011", "nimg": 0, "loaded": 0, "nblob": 0,
                     "text": "Going long on UNI here at CMP. TPs above, 4H close under 6.39 for stops."}
        _chk("未加载的单图消息 → 判为有图（旧代码 False）", msg_has_image(_row_2177), True)
        _chk("纯文本消息 → 判为无图（不会白白等图）", msg_has_image(_row_text), False)
        _chk("未加载的单图 → 抓图预算给足 6 秒", img_wait_ms(_row_2177, False), 6000)
        _chk("内容图已加载/带关键词 → 也给 6 秒（JS 拿到就提前返回，不真等满）",
             img_wait_ms({"nimg": 1, "nblob": 1, "loaded": 1}, True), 6000)
        _chk("无内容图、只有关键词 → 仍只等 1.2 秒（不拖慢出单）",
             img_wait_ms({"nimg": 1, "nblob": 0, "loaded": 0}, True), 1200)
        _chk("SCAN_JS 已上报 blob 内容图数量", "nblob" in SCAN_JS, True)

        # ---------- ② 真实图：读图必须读出三档止盈 ----------
        print("\n[2] 真实图读图（v21/imgs/7685715944936623383_0.png，UNI 那张真图）")
        _real = _prod_imgdir + "/7685715944936623383_0.png"
        _ch = {}
        if os.path.exists(_real):
            _ch = read_chart(_real) or {}
            print("     ↳ read_chart = sl=%s entry=%s tps=%s" % (_ch.get("sl"), _ch.get("entry"), _ch.get("tps")))
            _chk("止损读到 6.396", _ch.get("sl"), 6.396)
            _chk("三档止盈 = 7.180 / 8.216 / 9.289（原来只读到 9.289）",
                 [round(x, 3) for x in (_ch.get("tps") or [])], [7.18, 8.216, 9.289])
            _chk("价格轴刻度 9.302 未被当成止盈线", 9.302 in (_ch.get("tps") or []), False)
        else:
            print("     ⚠️ 生产图不存在，跳过（%s）" % _real)
            _ch = {"ok": True, "sl": 6.396, "entry": 6.513, "tps": [7.18, 8.216, 9.289],
                   "lines": [{"value": 7.18, "color": "white", "cov": 0.74},
                             {"value": 8.216, "color": "white", "cov": 0.79},
                             {"value": 9.289, "color": "green", "cov": 0.86}]}

        # ---------- ③ 图先到、文字后到 → 回填关联 ----------
        print("\n[3] 顺序A：图先到（无币种）→ 文字带来币种 → 回填关联")
        RAWQ.clear(); PENDING.clear(); ASKING.clear(); _SENT.clear()
        open_pos_ref.clear()
        _imgfile = IMGDIR + "/fake_uni_0.png"
        open(_imgfile, "wb").write(b"x")        # 占位文件（真实图只读，不动生产）
        _rec = rawq_add("机器人开单通知", {"kind": "img", "mid": "1", "imgs": [_imgfile],
                                          "chart": _ch, "meta": {"coin": "UNI", "direction": "LONG",
                                                                 "is_chart": True},
                                          "text": "用户963038", "when": "09-15 21:57:16"})
        _chk("图已进暂存区（无币种也能存下）", len(rawq_pending_imgs("机器人开单通知")), 1)
        _txt_uni = ("Going long on UNI here at CMP. TPs above, 4H close under 6.39 for stops. "
                    "Nice looking SR flip and way stronger than before.")
        _info_uni = dict(fast_parse(_txt_uni) or {})
        _info_uni["coin"] = "UNI"; _info_uni["direction"] = "LONG"; _info_uni["entry_is_cmp"] = True
        _p = merge_pending("UNI", "机器人开单通知", info=_info_uni, t_sig=0, txt=_txt_uni,
                           stamps={"found": time.time(), "img": 0.0, "parse": time.time(), "chart": 0.0})
        _p["dir"] = "LONG"
        _n = rawq_bind("机器人开单通知", "UNI", _p)
        _chk("回填关联到 UNI（1 张图）", _n, 1)
        _chk("UNI 待确认池已带图", _p.get("imgs"), [_imgfile])
        _chk("止损取自图上的红线 6.396（图优先）", _p.get("stop"), 6.396)
        _chk("三档止盈取自图上横线", [round(x, 3) for x in sorted(set(_p.get("tps") or []))],
             [7.18, 8.216, 9.289])
        _chk("文字写 CMP → 入场价按规则取当前市价（不受图上标签影响）",
             bool(_p.get("entry_is_cmp")), True)
        _chk("已关联的图不会再被单独推送", rawq_sweep("机器人开单通知"), 0)
        _chk("没有产生任何通知（正常信号走审批，不走图通报）", len(_SENT), 0)

        # ---------- ④ 4 秒内没关联上 → 单独推送，且不建可审批单 ----------
        print("\n[4] 顺序B：4 秒内没等到文字 → 单独推送图自己的信息（用户要求③）")
        RAWQ.clear(); PENDING.clear(); ASKING.clear(); _SENT.clear()
        rawq_add("黄金mansoor", {"kind": "img", "mid": "2", "imgs": [_imgfile], "chart": _ch,
                                 "meta": {"coin": "UNI", "direction": "LONG", "is_chart": True},
                                 "text": "Photo strip", "when": "09-15 20:21:43"})
        _chk("未到 4 秒不推送（不抢跑）", rawq_sweep("黄金mansoor", now=time.time()), 0)
        time.sleep(0.2)
        _n2 = rawq_sweep("黄金mansoor", now=time.time() + RAWQ_WAIT + 0.1)
        _chk("到 4 秒 → 推送 1 条", _n2, 1)
        _t2 = _SENT[-1] if _SENT else ""
        print("     ——实际推送内容——")
        for _l in _t2.splitlines():
            print("       %s" % _l)
        _chk("推送标题是「只识别到图」", "【信号·只识别到图】" in _t2, True)
        _chk("推送里带图上止损 6.396", "6.396" in _t2, True)
        _chk("推送里带三档止盈", all(x in _t2 for x in ("7.18", "8.216", "9.289")), True)
        _chk("明确写出「未读到的（绝不猜）」", "未读到的（绝不猜）" in _t2, True)
        _chk("明确说明不会下单", "不会下单" in _t2, True)
        _chk("没有建立可审批单（用户要求：只有图不建单）", (len(PENDING), len(ASKING)), (0, 0))
        _chk("同一条图不会重复推送", rawq_sweep("黄金mansoor", now=time.time() + 60), 0)

        # ---------- ⑤ 顺序C：文字先到、图后到 ----------
        print("\n[5] 顺序C：文字先到（4 秒窗口内）、图后到 → 并入同一条信号")
        RAWQ.clear(); PENDING.clear(); ASKING.clear(); _SENT.clear()
        _p3 = merge_pending("UNI", "机器人开单通知", info=_info_uni, t_sig=0, txt=_txt_uni,
                            stamps={"found": time.time(), "img": 0.0, "parse": time.time(),
                                    "chart": 0.0})
        _p3["dir"] = "LONG"
        _coin3 = rawq_attach_pending("机器人开单通知", [_imgfile], _ch,
                                     meta={"coin": "UNI", "direction": "LONG", "is_chart": True},
                                     txt="Photo strip", mid="3")
        _chk("图被并入 UNI 的待确认池", _coin3, "UNI")
        _chk("并入后带图", _p3.get("imgs"), [_imgfile])
        _chk("并入后止损用图上的 6.396", _p3.get("stop"), 6.396)
        _chk("并入后不会再多推一条「只有图」", rawq_sweep("机器人开单通知", now=time.time() + 60), 0)

        # ---------- ⑥ 保护：图上币种与文字币种不一致 ----------
        print("\n[6] 保护：图上币种与文字币种不一致 → 只留图，不用图上的线覆盖文字点位")
        RAWQ.clear(); PENDING.clear(); ASKING.clear(); _SENT.clear()
        rawq_add("暴富龙", {"kind": "img", "mid": "4", "imgs": [_imgfile], "chart": _ch,
                            "meta": {"coin": "BTC", "direction": "LONG", "is_chart": True},
                            "text": "", "when": "09-15 22:00:00"})
        _info_eth = {"coin": "ETH", "direction": "LONG", "stop": 2400.0, "targets": [2500.0]}
        _p4 = merge_pending("ETH", "暴富龙", info=_info_eth, t_sig=0, txt="ETH 做多 止损2400 止盈2500",
                            stamps={"found": time.time(), "img": 0.0, "parse": time.time(),
                                    "chart": 0.0})
        _p4["dir"] = "LONG"
        rawq_bind("暴富龙", "ETH", _p4)
        _chk("图留档了", bool(_p4.get("imgs")), True)
        _chk("但止损仍是文字的 2400（没被图上的 6.396 覆盖）", _p4.get("stop"), 2400.0)
        _chk("止盈仍是文字的 [2500]", _p4.get("tps"), [2500.0])

        # ---------- ⑦ B10 残留：Going long on X ----------
        print("\n[7] B10 残留：「Going long on UNI」的 on 不能再被当成币种 ON")
        _fp = fast_parse(_txt_uni) or {}
        _chk("coin = UNI（原来 = ON）", _fp.get("coin"), "UNI")
        _chk("方向仍是 LONG", _fp.get("direction"), "LONG")
        _chk("止损仍读到 6.39", _fp.get("stop"), 6.39)
        _chk("无介词写法「Going long LSK here at CMP」不受影响",
             (fast_parse("Going long LSK here at CMP. SL 0.1993") or {}).get("coin"), "LSK")
        _chk("「Selling BTC」这类模板不受影响",
             (fast_parse("Selling BTC here, SL 60000") or {}).get("coin"), "BTC")

        # ---------- ⑧ 合并窗口仍是 4 秒 ----------
        print("\n[8] 合并窗口没被拖慢（用户要求②）")
        _chk("PENDING_WAIT 仍是 4 秒", PENDING_WAIT, 4)
        _chk("图关联窗口 = 合并窗口", RAWQ_WAIT, PENDING_WAIT)

        # ---------- ⑨ 隔离复核 ----------
        # ---------- ⑧d 关键标签两次读数交叉校验（用户 2026-09-16 选定方案）----------
        print("\n[8d] 关键标签两次独立读数必须一致，不一致就按未读到处理")
        _chk("两次一致 → 可用", two_read_ok(6.396, 6.398), (True, 6.398, "两次一致"))
        _ok1, _v1, _w1 = two_read_ok(6.396, 4.0)
        _chk("两次差 37% → 不可用", _ok1, False)
        _chk("不可用时不给值（绝不猜）", _v1, None)
        print("     ↳ 说明：%s" % _w1)
        _ok2, _v2, _w2 = two_read_ok(6.396, None)
        _chk("只有批量读数 → 可用（不因单标签失败而丢数据）", (_ok2, _v2), (True, 6.396))
        _ok3, _v3, _w3 = two_read_ok(None, 7.18)
        _chk("只有单标签读数 → 可用（补回批量丢掉的标签）", (_ok3, _v3), (True, 7.18))
        _chk("两次都没读出 → 不可用", two_read_ok(None, None)[0], False)

        print("\n[8e] 单调性校验：价格必须随 y 增大而下降（价格轴几何性质）")
        _m_ok = [{"y": 100, "value": 9.289, "cov": 0.86},
                 {"y": 300, "value": 8.216, "cov": 0.79},
                 {"y": 500, "value": 7.18, "cov": 0.74}]
        _chk("正常图 → 剔除 0 个", drop_nonmonotonic(_m_ok), [])
        _m_bad = [{"y": 100, "value": 9.289, "cov": 0.86},
                  {"y": 300, "value": 6.4, "cov": 0.20},      # 这条读得可疑（cov 只有 0.2）
                  {"y": 500, "value": 7.18, "cov": 0.74}]
        _bad = drop_nonmonotonic(_m_bad)
        # 两条冲突时剔除**覆盖率更低**的那条（长线是 KOL 真画的线，更可信）
        _chk("违反单调（y 越大反而越贵）→ 剔除覆盖率更低的那条", [x["value"] for x in _bad], [6.4])

        # ---------- ⑧f 手工仓护栏（用户：我在币安手工开的仓，机器人不得有任何干涉）----------
        print("\n[8f] 手工仓护栏：交易所有、纸面没有的仓 = 用户手工仓，机器人绝不干涉")
        MANUAL_COINS.clear()
        MANUAL_COINS.add("LSK")
        _chk("manual_guard 认出手工仓", bool(manual_guard("LSK")), True)
        _chk("非手工仓不误拦", manual_guard("BTC"), None)
        _chk("manual_block 返回 True（会被拦）", manual_block("LSK", "开新仓"), True)
        _chk("拦下时发了告警", any("手工仓护栏" in s for s in _SENT), True)
        _chk("非手工仓不告警", manual_block("ETH", "开新仓"), False)
        _SENT.clear()
        # 止损数量：不能用交易所数量（含手工仓），只能退回纸面估算
        _tr = {"entry": 0.5, "remaining": 1.0, "dir": "LONG"}
        _q = _real_qty_or_estimate("LSK", _tr)
        _chk("手工仓的止损数量只用纸面估算（绝不用交易所数量）", _q, real_qty_estimate(0.5, 1.0))
        # 落盘 / 恢复
        _md = _manual_dump()
        _chk("手工仓名单可落盘", _md, ["LSK"])
        json.dump({"open": {}, "manual": _md}, open(STATE, "w", encoding="utf-8"), ensure_ascii=False)
        MANUAL_COINS.clear()
        for _c in (json.load(open(STATE, encoding="utf-8")).get("manual") or []):
            MANUAL_COINS.add(str(_c).upper())
        _chk("重启后手工仓护栏能恢复", sorted(MANUAL_COINS), ["LSK"])
        MANUAL_COINS.clear()

        # ---------- ⑧g 自环防护：机器人自己的通知前缀必须全部登记 ----------
        print("\n[8g] 自环防护：源码里 notify 用到的【前缀】必须都在 SELF_MARKS 里")
        _src = open(os.path.abspath(__file__), encoding="utf-8").read()
        _used = set()
        for _m in re.finditer(r"notify\(\s*(?:f)?\"([^\"]{0,240})", _src):
            for _mk in re.findall(r"【[^】]{0,14}】", _m.group(1)):
                _used.add(_mk)
        _missing = sorted(m for m in _used if not any(m.startswith(s[:6]) or s in m for s in SELF_MARKS))
        print("  源码里用到的通知前缀 %d 个：%s" % (len(_used), "、".join(sorted(_used))[:150]))
        _chk("没有漏登记的机器人通知前缀", _missing, [])
        # 🆕 2026-09-17：上面那条只扫【】前缀 —— 而 09:26 的事故恰恰是一条**完全没有【】前缀**的通知
        #   （有一条通知是 notify 配上「· 」开头的正文）被机器人自己读回去、当成了"你的回复"。
        #   所以再加一条：**每一条 notify 字面量的开头都必须是【**（允许前面有 1 个 emoji 标记）。
        #   ⚠️ 扫描前先剔掉**整行注释**：注释里举例写出来的 notify(…) 不是真代码，
        #      第一版就因为它误报了一条（自检自己给自己挖的坑，如实记在这）。
        _src_nc = "\n".join(ln for ln in _src.splitlines() if not ln.lstrip().startswith("#"))
        _bad_lead = []
        for _m in re.finditer(r"notify\(\s*(?:f)?\"([^\"]{0,240})", _src_nc):
            _lit = _m.group(1)
            if not _lit:
                continue
            if _lit.startswith("\n") or _lit.startswith("\\n"):
                continue                      # notify("\n".join(L))：前缀在列表首元素里，另有人眼可查
            if re.match(r"^[^【]{0,3}【", _lit):
                continue
            _bad_lead.append(_lit[:24])
        print("  没有【】前缀的 notify 字面量 %d 条：%s" % (len(_bad_lead), _bad_lead[:4]))
        _chk("所有 notify(...) 字面量都以【 开头（防裸前缀通知再被自己读回来）", _bad_lead, [])
        _chk("SELF_MARKS 含【手工仓护栏】（2026-09-16 实测漏过）", "【手工仓护栏】" in SELF_MARKS, True)
        _chk("SELF_MARKS 含【机器人告警】", "【机器人告警】" in SELF_MARKS, True)
        _chk("机器人自己的告警不会被当信号（实测那条原文）",
             any(_m in "ℹ️【手工仓护栏】检测到交易所有你的手工仓：LSK 机器人不会对它做任何事（不开新仓、不平仓、不改止损）"
                 for _m in SELF_MARKS), True)

        # ---------- ⑧h 几何读图（用户那张真图）----------
        print("\n[8h] 几何读图：色框语义（红框下边=止损、红绿交界=开仓、绿框内横线=止盈）")
        if os.path.exists(_real):
            _im = Image.open(_real).convert("RGB")
            _px2 = _im.load()
            _bx = find_fill_boxes(_px2, _im.width, _im.height)
            print("     ↳ 认出的色框：%s" % {k: list(v) for k, v in _bx.items()})
            _chk("认出了绿框", "green" in _bx, True)
            _chk("认出了红框", "red" in _bx, True)
            if "green" in _bx and "red" in _bx:
                _chk("绿框在红框上方（=做多）", _bx["green"][1] < _bx["red"][1], True)
                _chk("红框下边 ≈ y1598（实测）", abs(_bx["red"][3] - 1598) <= 4, True)
            _f = fit_axis_scale([(476, 9.289), (895, 8.216), (1294, 7.18)])
            _chk("用三条真实止盈线能拟合价格轴", bool(_f), True)
            if _f:
                _chk("轴换算 y=1598（红框下边）≈ 6.39（±1%）",
                     abs(axis_price(_f, 1598) - 6.39) / 6.39 < 0.01, True)
            _rc = read_chart(_real) or {}
            print("     ↳ read_chart：mode=%s 止损=%s 开仓=%s 止盈=%s"
                  % (_rc.get("mode"), _rc.get("sl"), _rc.get("entry"), _rc.get("tps")))
            _chk("走的是几何路径", _rc.get("mode"), "geometry")
            _chk("止损取到 6.396（红框下边）", _rc.get("sl"), 6.396)
            _chk("三档止盈 7.18/8.216/9.289", [round(x, 3) for x in (_rc.get("tps") or [])],
                 [7.18, 8.216, 9.289])

        # ---------- ⑧i 2026-09-16 下午用户报的三个问题 ----------
        print("\n[8i] 用户报的三个问题：回复识别 / 审批消息带耗时 / 止损通报不推送")
        _saved_asking = dict(ASKING)
        ASKING.clear()
        ASKING["XAU"] = {"p": {"entry": 4330.0, "stop": 4310.0, "tps": [4430.0], "group": "黄金mansoor",
                               "dir": "LONG", "texts": ["BUYING XAUUSD Entry : 4330.00 SL : 4310.00 TP : 4430.00"],
                               "legs": [], "first_ts": time.time() - 12, "t_found": time.time() - 10,
                               "t_img": time.time() - 9, "t_parse": time.time() - 8,
                               "t_chart": time.time() - 8, "deadline": 0},
                         "reason": "测试", "ask_ts": time.time(), "txt": ""}
        # ① 回复识别：飞书省略时间戳 → 昵称留在文本里（实测事故原文）
        # ⚠️⚠️ 2026-09-17 惨痛教训：这里原来写的是
        #     `_handle_ask_reply("开") if False else bool(re.fullmatch(<我又抄了一遍的正则>, ...))`
        #     —— 等于**用"重写一遍正则"冒充"测试真函数"**，真函数一次都没被这条自检碰到。
        #     后果实测到了：`_handle_ask_reply` 内部的「不开」分支缺失，把「不开」执行成了「开」、
        #     真的开了仓（00:34 UNI），而这条自检**一直是绿的**。
        #     所以：**自检必须调用真函数并断言副作用**，不能测自己抄的那份逻辑。
        _chk("回复路由：整条「不开」必须被 _is_ask_reply 认下",
             _is_ask_reply("用户963038 不开")[0], True)
        _chk("回复路由：整条「开」必须被认下", _is_ask_reply("用户963038 开")[0], True)
        ASKING["XAU"]["p"]["deadline"] = 0
        _r = handle_command("用户963038 开")
        _chk("handle_command 认下「用户963038 开」", _r, True)
        _chk("回复后 XAU 已进入可出单状态（approved=True）",
             bool(PENDING.get("XAU", {}).get("approved")), True)
        ASKING.clear(); PENDING.clear()
        # ② 审批消息必须带耗时
        _p9 = {"entry": 4330.0, "stop": 4310.0, "tps": [4430.0], "group": "黄金mansoor", "dir": "LONG",
               "texts": ["BUYING XAUUSD"], "legs": [], "first_ts": time.time() - 18.4,
               "t_found": time.time() - 9.4, "t_img": time.time() - 9.4, "t_parse": time.time() - 8.6,
               "t_chart": time.time() - 8.6, "deadline": 0}
        _tl = _timing_line(_p9)
        print("     ↳ %s" % _tl)
        for _kw in ("从信号发出到推送", "发现", "抓图", "解析", "读图", "等齐后续消息"):
            _chk("耗时行含 %r" % _kw, _kw in _tl, True)
        _SENT.clear()
        ask_user("XAU", dict(_p9), "按你的要求：所有订单在开之前都要经你审批")
        _chk("审批消息里确实带了耗时行", any("从信号发出到推送" in s for s in _SENT), True)
        ASKING.clear()
        # ③ 止损通报：直接忽略，不推送；术语中文
        _chk("止损通报文本被 _CLOSE_ANNOUNCE 命中",
             bool(_CLOSE_ANNOUNCE.search("Trade Closed — EIGEN/USDT LONG Embed Signal by Neil $0.1905 "
                                         "(-5.18% from entry) Stop loss hit at $0.1905")), True)
        _chk("止盈达成也命中（但它会去回报你的持仓）",
             bool(_CLOSE_ANNOUNCE.search("TP1 hit at 0.19 — 止盈达成")), True)
        _chk("管理指令术语中文化：close_all → 全部平仓",
             {"close_all": "全部平仓", "trim": "减仓",
              "move_stop_to_cost": "止损移到开仓价"}.get("close_all"), "全部平仓")
        ASKING.clear()
        ASKING.update(_saved_asking)

        # ---------- ⑧j 归因与展示（2026-09-17 真实端到端联调暴露的两个问题）----------
        print("\n[8j] 拒绝原因要区分「信号陈旧」与「解析错误」；CMP 无价位要显示「按市价」")
        # 实测原文：博主那条 UNI 信号 CMP 6.719 / 止损 6.39，联调时市价已跌到 6.244（跌破止损）。
        # 旧文案写「判为解析错误」→ 错误归因（会让人去查解析而不是查信号时效）。
        _stale = _stop_side_reason("UNI", "LONG", 6.244, 6.39, 6.244, True,
                                   "UNI 做多 CMP 6.719 止损 6.39 止盈 7.180 / 8.216 / 9.302")
        print("     ↳ 陈旧信号：%s" % _stale)
        _chk("市价已越过止损 → 判为「失效/陈旧」", bool(_stale) and ("失效" in _stale or "陈旧" in _stale), True)
        _chk("陈旧信号不再误写成「解析错误」", "解析错误" in (_stale or ""), False)
        _chk("陈旧信号里说清了是哪一侧越界（跌破）", "跌破" in (_stale or ""), True)
        # 真解析错误：入场价是博主给的价位、与市价差得远，止损落在错误一侧
        _perr = _stop_side_reason("DOGE", "LONG", 0.079, 0.09, 0.0790 * 1.6, False, "DOGE 在0.078到0.08接多")
        print("     ↳ 真解析错误：%s" % _perr)
        _chk("入场价与市价差得远 + 止损在错侧 → 判为「解析错误」",
             bool(_perr) and ("解析错误" in _perr), True)
        # 平仓通报被误读成开仓信号时，要额外点出来
        _perr2 = _stop_side_reason("DOGE", "LONG", 0.079, 0.0828, 0.079 * 1.6, False,
                                   "Trade Closed — DOGE/USDT LONG Stop loss hit at $0.08280")
        _chk("像是平仓通报 → 文案里点出来", "平仓" in (_perr2 or ""), True)
        _chk("止损方向正常时不拒绝", _stop_side_reason("BTC", "LONG", 100.0, 95.0, 100.0), None)
        # CMP 无价位：审批单不能再显示「入场：未读到」
        _pcmp = {"entry": None, "stop": 6.10, "tps": [6.60, 6.90, 7.20], "group": "机器人开单通知",
                 "dir": "LONG", "texts": ["【联调测试2】UNI 做多 CMP 止损 6.10 止盈 6.60 / 6.90 / 7.20"],
                 "legs": [], "entry_is_cmp": True, "mkt_at_ask": 6.244, "deadline": 0}
        _al = "\n".join(_approval_lines("UNI", _pcmp, 1))
        _aline = [x for x in _al.splitlines() if x.startswith("入场：")]
        print("     ↳ %s" % (_aline[0] if _aline else "(没有入场行)"))
        _chk("CMP 无价位 → 显示「按市价」，不再写「未读到」",
             bool(_aline) and ("按市价" in _aline[0]) and ("未读到" not in _aline[0]), True)
        _chk("CMP 无价位时也带上参考市价（止损点数才算得出来）", "6.244" in (_aline[0] if _aline else ""), True)
        _chk("CMP 有价位时仍正常显示价位",
             "6.719" in "\n".join(_approval_lines("UNI", dict(_pcmp, entry=6.719, entry_is_cmp=False), 1)), True)

        # ---------- ⑧k 回复方向安全（2026-09-17 生产事故：说「不开」却被开了仓）----------
        print("\n[8k] 回复方向安全：说「不开」绝不能开仓（复现 00:34 那次生产事故）")

        def _mk_pend(_c):
            return {"entry": 1.0, "stop": 0.9, "tps": [1.1, 1.2, 1.3], "group": "机器人开单通知",
                    "dir": "LONG", "texts": ["测试"], "legs": [], "deadline": 0}

        def _reset_asking(_coins):
            ASKING.clear(); PENDING.clear()
            for _c in _coins:
                ASKING[_c] = {"p": _mk_pend(_c), "reason": "测试", "ask_ts": time.time(), "txt": ""}

        # 注：不需要动 open_pos_ref —— 它是 dict，且当前 len(...) < MAX_OPEN，
        #     不会触发 _open_asking 里的"提高持仓上限"分支。（第一版我写了 del open_pos_ref[:]，
        #     报 unhashable type: 'slice'，是自检自己的错。）

        # ① 事故原样复现：只有 1 条待确认，用户回「不开」
        _reset_asking(["UNI"]); _SENT.clear()
        _handle_ask_reply("不开")
        _chk("单条待确认 + 回「不开」→ 待确认清空（不再挂着）", list(ASKING), [])
        _chk("单条待确认 + 回「不开」→ 绝不放行出单",
             bool(PENDING.get("UNI", {}).get("approved")), False)
        _chk("单条待确认 + 回「不开」→ 通知里不许出现「收到「开」」",
             any("收到「开」" in _s for _s in _SENT), False)
        _chk("单条待确认 + 回「不开」→ 通知里说明已作废",
             any("作废" in _s for _s in _SENT), True)

        # ② 对照：回「开」必须仍然能开（别把好的地方修反了）
        _reset_asking(["UNI"]); _SENT.clear()
        _handle_ask_reply("开")
        _chk("对照：回「开」→ 正常放行", bool(PENDING.get("UNI", {}).get("approved")), True)

        # ③ 多条 + 纯「不开」→ 全部作废（而不是"请指明币种"后继续挂着）
        _reset_asking(["UNI", "BTC"]); _handle_ask_reply("不开")
        _chk("多条 + 回「不开」→ 全部作废", list(ASKING), [])

        # ④「不开 BTC」→ 只作废 BTC，UNI 保持待确认且不许顺手开
        _reset_asking(["UNI", "BTC"]); _handle_ask_reply("不开 BTC")
        _chk("「不开 BTC」→ BTC 被作废", "BTC" in ASKING, False)
        _chk("「不开 BTC」→ UNI 仍待确认（不许顺手开）", "UNI" in ASKING, True)
        _chk("「不开 BTC」→ UNI 未被放行", bool(PENDING.get("UNI", {}).get("approved")), False)

        # ⑤「只开 BTC」—— "只开"的语义就是**其余一律作废**（不是"其余继续挂着"）
        _reset_asking(["UNI", "BTC"]); _handle_ask_reply("只开 BTC")
        _chk("「只开 BTC」→ BTC 放行", bool(PENDING.get("BTC", {}).get("approved")), True)
        _chk("「只开 BTC」→ 其余(UNI)被作废，不再挂着", "UNI" in ASKING, False)
        _chk("「只开 BTC」→ UNI 绝不被放行（只开=其余不开）",
             bool(PENDING.get("UNI", {}).get("approved")), False)

        # ⑥ 带昵称前缀（飞书省略时间戳时的实测形态）
        _reset_asking(["XAU"]); _handle_ask_reply("用户963038 不开")
        _chk("「用户963038 不开」→ 作废且不放行",
             (list(ASKING) == []) and (not PENDING.get("XAU", {}).get("approved")), True)

        # ⑦ 路由判定必须经真函数（原来自检抄了一遍正则，所以漏掉了 00:34 那次事故）
        _reset_asking(["UNI"])
        _chk("路由：整条「不开」被认下并原样交给处理器", _is_ask_reply("用户963038 不开"), (True, "不开"))
        _chk("路由：整条「开」被认下", _is_ask_reply("用户963038 开")[0], True)
        ASKING.clear(); PENDING.clear()

        # ---------- ⑧l 自环根治 + 「叫得动」（2026-09-17 两次生产事故）----------
        # 事故① 09:26 用户**没回复**，机器人却自动执行了开单流程：
        #   它自己发的分币通知「· LSK 做多：开仓=未读到…」被读回来，正文含币种 LSK + 一个"开"字
        #   → 判成"用户回复：开 LSK"。事故② 09:00 用户回「开仓」，机器人**一句话都不回**。
        print("\n[8l] 自环根治（sender 判定）与「叫得动」：复现 09:26 / 09:00 两次生产事故")

        # ① 09:26 事故复刻：机器人自己发的通知（app 发送 + 指令群）绝不进入指令/信号流程
        _self_text = "· LSK 做多：开仓=未读到 止损=未读到 止盈=未读到"
        _chk("事故复刻：那条通知（app 发送 + 指令群）→ 必须跳过",
             _is_self_app_row("机器人开单通知", "app"), True)
        _chk("同一句话若是你发的（user）→ 不能跳过（否则你会叫不动它）",
             _is_self_app_row("机器人开单通知", "user"), False)
        # ⚠️ 本轮最容易改错的地方：博主信号**也是 app 发的**，一刀切会把真信号全杀光
        _chk("KOL 群里的 app 消息（博主信号）→ 绝不跳过",
             _is_self_app_row("黄金mansoor", "app"), False)
        _chk("KOL 群里的 user 消息 → 不跳过", _is_self_app_row("黄金mansoor", "user"), False)
        _chk("浏览器兜底模式（无 sender_type）→ 保持原行为，不跳过",
             _is_self_app_row("机器人开单通知", None), False)
        _chk("那条通知现在也带【信号·分币】前缀（第二层防护）",
             any(_m in "【信号·分币】" + _self_text for _m in SELF_MARKS), True)
        # 对照留证：字面判定**仍然**会把它当回复 —— 所以"关掉入口"是必需的，不是可选的
        _reset_asking(["LSK"])
        _chk("（对照）它若真被送进路由仍会被认成回复 → 故入口必须关掉",
             _is_ask_reply(_self_text)[0], True)
        ASKING.clear(); PENDING.clear()

        # ② 09:00 事故复刻：你回「开仓」→ 必须认下、必须真的开
        _reset_asking(["LSK"]); _SENT.clear()
        _chk("路由：「开仓」被认下（旧代码是 False）", _is_ask_reply("开仓")[0], True)
        _handle_ask_reply("开仓")
        _chk("回「开仓」→ 待确认清空（不再挂着）", list(ASKING), [])
        _chk("回「开仓」→ 真的放行出单", bool(PENDING.get("LSK", {}).get("approved")), True)
        _chk("回「开仓」→ 通知里写明「收到『开』」", any("收到「开」" in _s for _s in _SENT), True)
        for _w in ("开单", "下单", "建仓", "确认", "开吧"):
            _reset_asking(["LSK"])
            _chk("日常说法「%s」也必须被认下" % _w, _is_ask_reply(_w)[0], True)
        ASKING.clear(); PENDING.clear()

        # ③ 扩词不许碰坏方向安全：所有"不开"类说法仍然一律不开
        for _w in ("不开", "别开", "不要", "作废"):
            _reset_asking(["UNI"]); _SENT.clear()
            _handle_ask_reply(_w)
            _chk("扩词后「%s」仍然绝不开仓（也不许顺手放行）" % _w,
                 (not PENDING.get("UNI", {}).get("approved")) and (not ASKING.get("UNI")), True)
        ASKING.clear(); PENDING.clear()

        # ④「没认出来也必须回话」（用户原话：我回复开仓，机器人没有理我）
        _HINT_LAST[0] = 0.0; _SENT.clear()
        _chk("不像指令的闲聊 → 不打扰", _chitchat_hint("机器人开单通知", "user", "那个什么来着"), False)
        _HINT_LAST[0] = 0.0; _SENT.clear()
        _chk("像指令却没认出来 → 必须回一句", _chitchat_hint("机器人开单通知", "user", "开仓呀"), True)
        _chk("回话里写清「什么都没做」并给出可用说法",
             any(("没看懂" in _s) and ("什么都没做" in _s) and ("帮助" in _s) for _s in _SENT), True)
        _chk("KOL 群里不乱回话", _chitchat_hint("黄金mansoor", "user", "开仓呀"), False)
        _chk("机器人自己的通知不回话", _chitchat_hint("机器人开单通知", "app", "开仓呀"), False)
        _chk("超长正文不乱回话（阈值 40 字）",
             _chitchat_hint("机器人开单通知", "user", "开" * 41), False)

        # ---------- ⑧m 不看颜色的色块读图 + 状态图不推送 + 手工仓名单自动核对 ----------
        print("\n[8m] 色块读图（不看颜色）/ 持仓状态图不推送 / 手工仓名单按交易所实时核对")
        # ① 两套配色、同一套判据（用户 2026-09-17：「不能只认红色和绿色」）
        _f_gold = {"a": -0.2394, "b": 4394.84}          # 黄金那张图的实测价格轴 y→价格
        _tag_gold = [{"value": 4258.0, "y": 571, "line_y": 571, "cov": 1.0, "color": "dark"},
                     {"value": 4239.0, "y": 651, "line_y": 651, "cov": 1.0, "color": "grey"},
                     {"value": 4380.0, "y": 62, "line_y": 62, "cov": 0.8, "color": "blue"}]
        _z_grey_blue = [{"rgb": (216, 224, 248), "y0": 62, "y1": 569, "h": 507, "x0": 0, "x1": 100, "w": 100},
                        {"rgb": (240, 240, 240), "y0": 574, "y1": 651, "h": 77, "x0": 0, "x1": 100, "w": 100}]
        _r1 = read_zones(_tag_gold, _z_grey_blue, _f_gold) or {}
        _chk("黄金那张（灰+蓝）→ 做多", _r1.get("dir"), "LONG")
        _chk("黄金那张 → 开仓=两区交界 4258", _r1.get("entry"), 4258.0)
        _chk("黄金那张 → 止损=小框远端 4239", _r1.get("sl"), 4239.0)
        _chk("黄金那张 → 止盈=大框远端 4380", _r1.get("tps"), [4380.0])
        _chk("黄金那张 → 颜色提示与大小关系一致（高置信）", _r1.get("conf"), "high")
        _f_lit = {"a": -0.001391, "b": 6.6939}          # ua 那张（LIT）的实测价格轴
        _tag_lit = [{"value": 4.3167, "y": 1709, "line_y": 1709, "cov": 1.0, "color": "white"},
                    {"value": 4.156, "y": 1821, "line_y": 1821, "cov": 1.0, "color": "red"},
                    {"value": 6.0137, "y": 489, "line_y": 489, "cov": 0.9, "color": "green"}]
        _z_red_green = [{"rgb": (24, 72, 40), "y0": 489, "y1": 1703, "h": 1214, "x0": 0, "x1": 100, "w": 100},
                        {"rgb": (88, 32, 40), "y0": 1716, "y1": 1821, "h": 105, "x0": 0, "x1": 100, "w": 100}]
        _r2 = read_zones(_tag_lit, _z_red_green, _f_lit) or {}
        _chk("ua 那张（红+绿）→ 同一套代码给出同样结论", (_r2.get("dir"), _r2.get("entry"), _r2.get("sl")),
             ("LONG", 4.3167, 4.156))
        _chk("ua 那张 → 止盈=大框远端 6.0137", _r2.get("tps"), [6.0137])
        # ② 颜色提示与"大小关系"矛盾时**不许瞎猜**（改成 conflict，交给审批时人工确认）
        # 造法：**小的（=止损空间，在下方）用"盈利色"绿、大的（=止盈空间，在上方）用"止损色"灰**
        _z_conflict = [{"rgb": (240, 240, 240), "y0": 62, "y1": 569, "h": 507, "x0": 0, "x1": 100, "w": 100},
                       {"rgb": (24, 72, 40), "y0": 574, "y1": 651, "h": 77, "x0": 0, "x1": 100, "w": 100}]
        _r3 = read_zones(_tag_gold, _z_conflict, _f_gold) or {}
        _chk("颜色提示反了（绿在上=盈利色却在止盈位）→ 标 conflict 不硬判", _r3.get("conf"), "conflict")
        # ③ 持仓状态图（同一笔的进展通报）→ 不推送
        IMG_SIG_SEEN.clear()
        _img_sig_remember({"ok": True, "entry": 4258.0, "sl": 4239.0, "tps": [4380.0]}, "XAU")
        _chk("同一笔的进展通报 → 判为重复（不推送）",
             _img_is_repeat({"ok": True, "entry": 4258.05, "sl": 4239.0, "tps": [4380.0]}), True)
        _chk("状态图把开仓读偏了、但止损/止盈对上 → 仍判重复（实测：4298 vs 4258）",
             _img_is_repeat({"ok": True, "entry": 4298.115, "sl": 4258.022, "tps": [4380.0]}), True)
        _chk("新的一笔（点位不同）→ 不算重复",
             _img_is_repeat({"ok": True, "entry": 4300.0, "sl": 4250.0, "tps": [4500.0]}), False)
        _chk("读不出点位的图 → 不算重复（交给账户截图判定）", _img_is_repeat({"ok": False}), False)
        IMG_SIG_SEEN.clear()
        # ④ 手工仓名单按交易所实时持仓核对（用户报障：LSK/XAU/XAUT 已平仓却还在名单里）
        MANUAL_COINS.clear()
        for _c in ("BTC", "LSK", "XAU", "XAUT"):
            MANUAL_COINS.add(_c)
        _removed = _manual_prune({"BTCUSDT", "DASHUSDT", "USELESSUSDT"})
        _chk("已平仓的从名单移除", _removed, ["LSK", "XAU", "XAUT"])
        _chk("名单只剩真实有仓的", sorted(MANUAL_COINS), ["BTC"])
        _chk("真实有仓的绝不被误删", "BTC" in MANUAL_COINS, True)
        MANUAL_COINS.clear()

        # ---------- ⑧n 用户 2026-09-17 定的规则：门口表扩容 / 拆段不丢价位行 / % 不当价格 / 别名收窄 ----------
        print("\n[8n] 门口关键词扩容、多币种拆段、百分比不当价格、别名表收窄")
        for _k in ("Stop", "Buy", "入场", "多单", "目标位", "take profit", "Limit", "止盈位"):
            _chk("门口关键词含 %s（原来在门口就被当闲聊丢掉）" % _k, _k in SIG_KW_GATE, True)
        _card = ("LSK/USDT — LONG (Leverage)\n\nSignal by Prestige | UnityEntry:\n$0.4401 (limit)\n"
                 "Stop Loss:\n$0.3842 (-12.70%)\nHard\nRisk:\n1R\nTake Profits:\n"
                 "TP1: $0.6690 (+52.00%)\n\nUnity Academy • Risk maximum 1-3% per trade")
        _cs = split_by_coin(_card)
        _chk("LSK 卡片 → 只拆出 1 段（不再被 Take Profits 拆成多币种）", len(_cs), 1)
        _chk("该段保住了价位行（0.4401 / Stop Loss / TP1）",
             bool(_cs) and all(x in _cs[0][1] for x in ("0.4401", "Stop Loss", "TP1")), True)
        _multi = split_by_coin("原油CL跌破97空，100.7止损，93止盈。 Sol突破102.5多，止损100，止盈107到110。")
        _chk("真·多币种仍然拆得开（2 段）", [c for c, _ in _multi], ["CL", "SOL"])
        _chk("同一行两个币 → 取文中最先出现的（ETH，而不是字母序第一的 BTC）",
             [c for c, _ in split_by_coin("ETH 做空 2465，BTC 站稳 76200 多")], ["ETH"])
        _r_pct = fast_parse("BTC 做多 入场100 止损3%") or {}
        _chk("「止损3%」不再被当成价格（stop 必须为 None）", _r_pct.get("stop"), None)
        _chk("「止损3%」被认成百分比止损 3%", _r_pct.get("stopPct"), 3.0)
        _chk("「带个3%止损」同样按百分比处理",
             (fast_parse("BTC 做多 入场100 带个3%止损") or {}).get("stopPct"), 3.0)
        _r_card = fast_parse(_card) or {}
        _chk("卡片止盈不含百分比垃圾（52.0 / 1.0）", _r_card.get("targets"), [0.669])
        _chk("卡片开仓价仍读到 0.4401", _r_card.get("entry"), 0.4401)
        _chk("卡片止损仍读到 0.3842", _r_card.get("stop"), 0.3842)
        for _bad in ("金", "银", "油", "策略", " coinbase"):
            _chk("别名表已删掉易误命中的键 %r" % _bad, _bad in _NAME_MAP, False)
        _chk("「资金费率」不再误判成 XAU", find_coin_in_text("资金费率很高"), None)
        _chk("「黄金」仍认 XAU", find_coin_in_text("黄金站上4258"), "XAU")

        # ---------- ⑧o B7：幂等键 / 503·限频 / 挂单后核对（用户要求"不重复下单"）----------
        print("\n[8o] B7 下单幂等：状态未知时**绝不盲目重试**（用幂等键先查再决定）")
        if not _BEXEC_OK:
            _chk("binance_exec 可导入（B7 前置）", _BEXEC_OK, True)
        else:
            import binance_exec as _bx
            _sleep0 = _bx.time.sleep
            _bx.time.sleep = lambda *_a: None            # 测试里不等退避
            _cids = [_bx.new_cid("mo") for _ in range(3)]
            _chk("幂等键唯一", len(set(_cids)), 3)
            _chk("幂等键合法（≤36 字符、只含字母数字）",
                 all(len(c) <= 36 and c.isalnum() for c in _cids), True)
            _calls = {"n": 0, "cids": []}

            def _fake_req(method, path, params=None, signed=True, test=False, timeout=20):
                _calls["n"] += 1
                if path.endswith("/order") and params and "newClientOrderId" in params:
                    _calls["cids"].append(params["newClientOrderId"])
                return {"ok": 1}
            _req0 = _bx._req
            _bx._req = _fake_req
            try:
                _r = _bx.post_idempotent("/fapi/v1/order", {"symbol": "BTCUSDT"}, tag="mo")
                _chk("正常下单：一次成功、带上幂等键", (_r.get("ok"), len(_calls["cids"])), (1, 1))
            except Exception as _e:
                _chk("正常下单：一次成功、带上幂等键", "异常 %s" % str(_e)[:60], "1 次成功")
            # ① 明确被拒（余额不足）→ 绝不重试
            _calls["n"] = 0

            def _reject(method, path, params=None, signed=True, test=False, timeout=20):
                _calls["n"] += 1
                raise _bx.BinanceError("HTTP 400: {'code': -2019, 'msg': 'Margin is insufficient.'}")
            _bx._req = _reject
            try:
                _bx.post_idempotent("/fapi/v1/order", {"symbol": "BTCUSDT"}, tag="mo")
                _chk("明确被拒 → 直接抛错", "没抛错", "抛错")
            except _bx.BinanceError as _e:
                _chk("明确被拒 → 直接抛错且**只尝试 1 次**（绝不重试）",
                     (_calls["n"], "2019" in str(_e) or "Margin" in str(_e)), (1, True))
            # ② 限频 -1003 → 退避后重试，最终成功
            _calls["n"] = 0

            def _rl(method, path, params=None, signed=True, test=False, timeout=20):
                _calls["n"] += 1
                if _calls["n"] == 1:
                    raise _bx.BinanceError("HTTP 429: {'code': -1003, 'msg': 'Too many requests.'}")
                return {"ok": 2}
            _bx._req = _rl
            try:
                _r = _bx.post_idempotent("/fapi/v1/order", {"symbol": "BTCUSDT"}, tag="mo")
                _chk("限频 -1003 → 退避重试后成功", (_r.get("ok"), _calls["n"]), (2, 2))
            except Exception as _e:
                _chk("限频 -1003 → 退避重试后成功", "异常 %s" % str(_e)[:60], "重试成功")
            # ③ 503（状态未知）→ 用幂等键查到"其实已经下进去了" → 直接采用，**不再下单**
            _calls["n"] = 0
            _sent = []

            def _amb_then_found(method, path, params=None, signed=True, test=False, timeout=20):
                if method == "GET":
                    return {"orderId": 999, "status": "NEW"}
                _calls["n"] += 1
                _sent.append(params.get("newClientOrderId"))
                raise _bx.BinanceError("HTTP 503: Service Unavailable")
            _bx._req = _amb_then_found
            try:
                _r = _bx.post_idempotent("/fapi/v1/order", {"symbol": "BTCUSDT"}, tag="mo")
                _chk("503 但幂等键查到了 → 采用查到的单、**只下过 1 次**（不重复下单）",
                     (_r.get("_recovered"), _calls["n"]), (True, 1))
            except Exception as _e:
                _chk("503 但幂等键查到了 → 采用查到的单", "异常 %s" % str(_e)[:60], "采用")
            # ④ 503 + 查不到 → 用**同一个幂等键**重试（这是安全的关键）
            _calls["n"] = 0
            _sent = []

            def _amb_then_ok(method, path, params=None, signed=True, test=False, timeout=20):
                if method == "GET":
                    raise _bx.BinanceError("HTTP 400: {'code': -2013, 'msg': 'Order does not exist.'}")
                _calls["n"] += 1
                _sent.append(params.get("newClientOrderId"))
                if _calls["n"] == 1:
                    raise _bx.BinanceError("HTTP 503: Service Unavailable")
                return {"ok": 3}
            _bx._req = _amb_then_ok
            try:
                _r = _bx.post_idempotent("/fapi/v1/order", {"symbol": "BTCUSDT"}, tag="mo")
                _chk("503 且查不到 → 重试成功，且两次用的是**同一个幂等键**",
                     (_r.get("ok"), len(set(_sent)), len(_sent)), (3, 1, 2))
            except Exception as _e:
                _chk("503 且查不到 → 重试成功且同键", "异常 %s" % str(_e)[:60], "重试成功")
            # ⑤ 一直未知 → 抛"不要再自动重试"（绝不无限重试）
            _calls["n"] = 0

            def _always_amb(method, path, params=None, signed=True, test=False, timeout=20):
                if method == "GET":
                    raise _bx.BinanceError("HTTP 400: {'code': -2013, 'msg': 'Order does not exist.'}")
                _calls["n"] += 1
                raise _bx.BinanceError("HTTP 503: Service Unavailable")
            _bx._req = _always_amb
            try:
                _bx.post_idempotent("/fapi/v1/order", {"symbol": "BTCUSDT"}, tag="mo", tries=2)
                _chk("一直状态未知 → 抛错让人工核对", "没抛错", "抛错")
            except _bx.BinanceError as _e:
                _chk("一直状态未知 → 抛错且提示「不要再自动重试」",
                     ("不要再自动重试" in str(_e), _calls["n"]), (True, 2))
            _bx._req = _req0
            # ⑥ 挂单后核对：止损/止盈缺一个都要能报出来
            _bx.algo_present = lambda *_a, **_k: None
            _bx.open_orders = lambda *_a, **_k: [{"price": "110.0", "orderId": 7}]
            try:
                _bv = _bx.bracket_verify("BTCUSDT", "LONG", stop=95.0, tps=[{"price": 110.0}, {"price": 120.0}])
                _chk("核对：止损缺失被列出", any("止损" in x for x in _bv["missing"]), True)
                _chk("核对：缺的那档止盈被列出", any("120" in x for x in _bv["missing"]), True)
                _chk("核对：已挂上的那档判为 OK", _bv["tps"][0]["ok"], True)
            except Exception as _e:
                _chk("bracket_verify 可运行", "异常 %s" % str(_e)[:60], "可运行")
            _bx.time.sleep = _sleep0

        # ⑧ 高危开关：否定词不许被当成"开"（实盘/测试模式原来用 `"开" in cmd` 判定）
        _chk("「实盘模式 不要开 确认」同时含 开+确认（所以必须靠否定词挡住）",
             ("开" in "实盘模式 不要开 确认") and ("确认" in "实盘模式 不要开 确认"), True)
        _chk("实盘开关的否定词判定生效",
             bool(re.search(r"不|别|取消|勿", "实盘模式 不要开 确认")), True)
        _chk("测试模式『不要开』被判为 off",
             bool(re.search(r"不|别|取消|勿", "测试模式 不要开")), True)

        # ---------- ⑨ 隔离复核 ----------
        print("\n[9] 隔离复核（生产零写入）")
        _after_lines = sum(1 for _ in open(_prod_log, encoding="utf-8", errors="replace"))
        # ⚠️ 不能拿"生产 run.log 行数不变"当隔离判据 —— 机器人自己每 10 秒就写一条心跳，
        #    行数必然会涨（第一次跑就这么误报了一次）。真判据是：
        #    **自检开始之后新增的那些行里，不许有任何一行来自本次自检**。
        with open(_prod_log, "rb") as _f:
            _f.seek(_before_size)
            _appended = _f.read().decode("utf-8", "replace")
        _leak = [l for l in _appended.splitlines()
                 if ("B16" in l or "📎" in l or "只识别到图" in l
                     or "imgmerge_selftest" in l or "已暂存" in l or "回填关联" in l)]
        _chk("自检没有往生产 run.log 写入任何内容（只看新增行）", _leak, [])
        print("      ↳ 自检期间生产日志新增 %d 行（全部是机器人自己的心跳，示例：%s）"
              % (_after_lines - _before_lines,
                 (_appended.splitlines() or ["-"])[-1][:60]))
        _prod_open_after = len((json.load(open(_prod_state, encoding="utf-8")) or {}).get("open") or {})
        _chk("生产 state.json 的持仓数未被改动（内容比对，不比 mtime —— 机器人每轮都重写它）",
             _prod_open_after, _prod_open_before)
        _chk("生产 runtime_config.json mtime 未变",
             os.path.getmtime(_prod_rt), _mt[_prod_rt])
        _chk("生产 dryrun_bot2.py 未被这次自检改动",
             hashlib.md5(open(_prod_bot, "rb").read()).hexdigest(), _md5)
        _new_imgs = set(os.listdir(_prod_imgdir)) - _img_before
        _chk("生产 v21/imgs 没有被写入", sorted(_new_imgs), [])
        print("  /tmp 产物：%s" % ", ".join(sorted(os.listdir(_T))))
        print("  本次自检里 notify() 被替换为捕获函数（不会推飞书），共捕获 %d 个函数" % 1)

        print("\n" + "-" * 72)
        if _fail:
            print("B16 自检：%d 项失败" % len(_fail))
            for _f in _fail:
                print("   ✗ %s" % _f)
            sys.exit(1)
        print("B16 自检：全部通过 ✅")
        sys.exit(0)

    if "--selftest-feishu" in sys.argv:
        # ===== 官方 API 取消息层自检 =====
        # ① 卡片/文本抽取用**真实抓下来的卡片 JSON** 当夹具（离线，不联网）
        # ② 有令牌就再跑一次线上只读自检（health + 拉一条群的新消息）
        print("=" * 72)
        print("飞书官方 API 取消息层自检")
        print("=" * 72)
        _fail = []
        import feishu_api as _fa

        def _ck(name, got, want):
            _ok = (got == want)
            print("  %s %-52s got=%s want=%s" % ("[ OK ]" if _ok else "[FAIL]", name, str(got)[:60],
                                                 str(want)[:60]))
            if not _ok:
                _fail.append(name)

        _chk = _ck

        print("\n[1] 卡片文字抽取（夹具=2026-09-16 真实卡片）")
        _card_baofu = json.dumps({"title": None, "elements": [[{"tag": "text", "text": "9.16视频\n比特币突破站稳76200，可以多，止损75000。"}]]}, ensure_ascii=False)
        _it = {"msg_type": "interactive", "body": {"content": _card_baofu}}
        _t = _fa.msg_text_of(_it)
        _chk("卡片里的文字抽出来（含换行）", "比特币突破站稳76200，可以多，止损75000。" in _t, True)
        _card_un = json.dumps({"elements": [[{"tag": "text", "text": "Trade Closed — EIGEN/USDT LONG"},
                                             {"tag": "text", "text": "Stop loss hit at $0.1905"}]]}, ensure_ascii=False)
        _t2 = _fa.msg_text_of({"msg_type": "interactive", "body": {"content": _card_un}})
        _chk("多段文字按顺序拼接", _t2.startswith("Trade Closed — EIGEN/USDT LONG"), True)
        _chk("止损通报仍会被 _CLOSE_ANNOUNCE 命中（会被忽略、不推送）",
             bool(_CLOSE_ANNOUNCE.search(_t2)), True)
        _card_img = json.dumps({"elements": [[{"tag": "img", "image_key": "img_v3_abc"}]]}, ensure_ascii=False)
        _ik = _fa.msg_images_of({"msg_type": "interactive", "body": {"content": _card_img}})
        _chk("卡片里的图片 key 抽出来", _ik, ["img_v3_abc"])
        _chk("纯文本消息", _fa.msg_text_of({"msg_type": "text", "body": {"content": json.dumps({"text": "开"})}}), "开")
        _chk("纯图片消息没有文字", _fa.msg_text_of({"msg_type": "image", "body": {"content": json.dumps({"image_key": "x"})}}), "")

        print("\n[2] 消息 id 稳定性（同一消息多次拉取必须同 id，否则会重复处理）")
        _a = _fa._stable_id(1789563467183, "om_x100b659e")
        _b = _fa._stable_id(1789563467183, "om_x100b659e")
        _c = _fa._stable_id(1789563467183, "om_x100b659f")
        _chk("同一消息 id 稳定", _a, _b)
        _chk("同一毫秒不同消息 id 不同", _a != _c, True)

        print("\n[3] 行结构与网页版一致（下游逻辑不用改）")
        _req = {"id", "t_sig", "text", "nimg", "loaded", "nblob", "_imgs"}
        _chk("fetch_new 产出的行含全部必需字段（代码检查）",
             all(k in open(os.path.abspath(__file__), encoding="utf-8").read() for k in ('"_imgs"', '"t_sig"')), True)
        _chk("必需字段集合", sorted(_req), sorted(["_imgs", "id", "loaded", "nblob", "nimg", "text", "t_sig"]))

        print("\n[4] 线上只读自检（有令牌才跑）")
        _ti = _fa.token_info()
        if not _ti.get("has_token"):
            print("     ⚠️ 没有令牌，跳过（需要先在服务器上完成一次授权）")
        else:
            _ok, _why = _fa.health()
            _ck("令牌可用 + 能列群", _ok, True)
            print("     ↳ %s" % _why)
            _chk("有 refresh_token（可自动续期）", bool(_ti.get("has_refresh")), True)
            _ids = _fa.resolve_chat_ids(GROUPS)
            _chk("四个监控群都能在 API 里找到", len([g for g in GROUPS if g in _ids]), len(GROUPS))
            if _ids:
                _g0 = list(_ids)[0]
                _rows, _err = _fa.fetch_new(_ids[_g0], int((time.time() - 86400) * 1000), "/tmp/feishu_selftest_imgs",
                                            max_msgs=5)
                _ck("能拉到消息（err 为空）", _err, None)
                print("     ↳ 群「%s」近 24 小时拉到 %d 条；示例：%s"
                      % (_g0, len(_rows), (_rows[-1]["text"][:60].replace("\n", " ") if _rows else "-")))

        print("\n[5] 令牌失效时必须给出「重新授权链接」（用户要求：刷新失败要提醒重新授权）")
        _m, _fbk = _fa.authorize_url("dshT")
        print("     ↳ 主链接：%s" % _m[:120])
        _chk("主链接是 accounts.feishu.cn 授权页", "accounts.feishu.cn/open-apis/authen/v1/authorize" in _m, True)
        _chk("带 offline_access（才能拿长期令牌）", "offline_access" in _m, True)
        _chk("带 redirect_uri", "redirect_uri=https%3A%2F%2Flocalhost%3A8765%2Fcallback" in _m, True)
        _chk("备用链接（不带 scope）", "scope" not in _fbk, True)
        _hint = _fa.reauth_hint()
        _chk("告警文案里含可点击链接与三步说明",
             ("http" in _hint) and ("2)" in _hint) and ("3)" in _hint), True)

        print("\n" + "-" * 72)
        if _fail:
            print("飞书 API 自检：%d 项失败" % len(_fail))
            for _f in _fail:
                print("   ✗ %s" % _f)
            sys.exit(1)
        print("飞书 API 自检：全部通过 ✅")
        sys.exit(0)

    main()
