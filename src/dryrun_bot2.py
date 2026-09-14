#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书跟单 · dryRun 机器人 v2（每群一个独立标签页，永不切换会话）
- 每个群一个 page，打开后一直停在该群 → 彻底避免"读错群"
- 消息时间 = message-id 高位（Unix 秒），与页面显示一致
- 只处理开单信号；闲聊直接跳过；博主管理指令单独处理
- 全链路计时：信号发出 → 发现 → 抓图 → 解析 → 读图 → 下单(纸面) → 推送
"""
import os, re, sys, json, time, base64, datetime, threading, hashlib, math
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
TEST_MODE = True       # 测试阶段：抓到的一切信号都要出单（不受持仓上限拦截）
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


def real_plan_open(coin, dirc, entry, stop, tps, margin=None):
    """开仓 → 交给真实下单层生成完整计划（市价/限价腿 + 各档止盈 + Algo 止损）"""
    if not _BEXEC_OK:
        return None
    try:
        return bexec.open_full_position(coin.upper() + "USDT", dirc, entry, stop,
                                        list(tps or []), margin=margin or MARGIN)
    except Exception as e:
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
              "\"stop\":数字或null,\"targets\":[数字],\"tp_on_chart\":bool,"
              "\"type\":\"open|manage|info\",\"manage_action\":\"close_all|trim|move_stop_to_cost|null\"}"
              " 规则：只用消息里真实出现的数字，绝不编造；止盈写在图上则 tp_on_chart=true 且 targets 为空。开仓价若给的是区间（如「在4360到4310区间多」「4310-4360 区间」），请填 entryRange=[小,大] 且 entry 留 null。")
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
    if "区间多" in txt or "做多" in txt or "多单" in txt or "看多" in txt:
        dirc = "LONG"
    elif "区间空" in txt or "做空" in txt or "空单" in txt or "看空" in txt:
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
    for pat in (r"close under\s*\$?([0-9]*\.?[0-9]+)", r"SL[^0-9]{0,14}\$?([0-9]*\.?[0-9]+)",
                r"stop[ -]?loss[^0-9]{0,14}\$?([0-9]*\.?[0-9]+)", r"止损[^0-9]{0,14}([0-9]*\.?[0-9]+)"):
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
    rng = None
    m = re.search(r"([0-9]*\.?[0-9]+)\s*(?:到|至|~|～|—|–)\s*([0-9]*\.?[0-9]+)", txt)
    if not m:
        m = re.search(r"([0-9]*\.?[0-9]+)\s*-\s*([0-9]*\.?[0-9]+)\s*(?:区间|之间)", txt)
    if m:
        try:
            a, b = float(m.group(1)), float(m.group(2))
            if a > 0 and b > 0 and a != b:
                rng = [min(a, b), max(a, b)]
        except Exception:
            pass
    if not (stop or add or entry or tps):
        return None
    return {"is_signal": True, "coin": coin, "direction": dirc, "entry": entry, "entryRange": rng,
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
        s = info.get("stop")
        if isinstance(s, (int, float)) and p["stop"] is None:
            p["stop"] = float(s); p["stop_src"] = "消息文字"
        for t in (info.get("targets") or []):
            try:
                tv = float(t)
                if tv not in p["tps"]: p["tps"].append(tv)
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
MAX_STOP_PCT = 0.40      # 止损距离开仓价超过 40% → 判为荒谬
MIN_STOP_PCT = 0.0005    # 止损距离小于 0.05% → 等于没设止损

# ===== 待人工确认（用户 2026-09-14：把握不准必须问我，回「开」才开）=====
ASKING = {}              # coin -> {p, reason, ask_ts, txt}
ASK_TIMEOUT = 1800       # 30 分钟没回复自动作废


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

def find_all_coins(txt):
    """找出文本里出现的【所有】币安 USDT-M 币种 —— 用来识别"一条消息混了多个币"的笼统总结。
    事故教训：暴富龙的「9.14视频总结」里 BTC/以太坊/Giggle 混在一起，fast_parse 把
    以太坊的区间、Giggle 的止盈都算到了 BTC 头上。"""
    hits = set()
    t = txt or ""
    for k, v in _NAME_MAP.items():
        if len(k) >= 2 and k in t:
            hits.add(v)
    for s in _SYMS:
        if len(s) >= 2 and not s.isdigit() and re.search(
                r"(?<![A-Za-z0-9])" + re.escape(s) + r"(?![A-Za-z0-9])", t, re.I):
            hits.add(s)
    return hits


def ask_user(coin, p, reason):
    """把握不准 → 挂起并询问用户。回「开」才开单，回「不开」作废。"""
    ASKING[coin] = {"p": p, "reason": reason, "ask_ts": time.time(),
                    "txt": (p["texts"][0][:300] if p.get("texts") else "")}
    d = 1 if (p.get("dir") or "LONG").upper() == "LONG" else -1
    tps = sorted(set(p.get("tps") or []))
    notify("\n".join([
        "【信号·待你确认】%s %s" % (coin, "做多 LONG" if d == 1 else "做空 SHORT"),
        "⚠️ 把握不准的原因：%s" % reason,
        "解析结果：开仓=%s ｜ 止损=%s ｜ 止盈=%s"
        % (p.get("entry") if p.get("entry") is not None else "未读到",
           p.get("stop") if p.get("stop") is not None else "未读到",
           tps if tps else "未读到"),
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
        # 开仓方式（用户规则：不追高/不追空，逆势有利直接市价）
        #   博主价与市价相差 ±2% 以内            -> 市价开满仓
        #   做多 且 市价高于博主价 2% 以上        -> 不追高：分两笔挂限价（博主价×1.01 一半 + 博主价 一半）
        #   做空 且 市价低于博主价 2% 以上        -> 不追空：分两笔挂限价（博主价×0.99 一半 + 博主价 一半）
        #   其余（做多时市价低于博主价 / 做空时市价高于博主价）-> 直接市价，止损不变
        entry, entry_mode, esrc = mkt, "市价", "市价成交"
        entry_orders = [{"kind": "市价", "price": mkt, "margin": MARGIN}]
        entry_note = ""
        if isinstance(signal_entry, (int, float)) and signal_entry and mkt:
            diff = (signal_entry - mkt) / mkt          # >0 表示市价在博主价下方
            if dirc0 == "LONG" and diff < -0.02:       # 市价高于博主价 2% 以上 -> 不追高
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
        if p["stop"] is None and not tps:
            notify("【信号·待确认】%s\n没读到止损和止盈（图上/卡片/文字都没读到），等你确认后我再挂单\n原文：%s"
                   % (coin, (p["texts"][0][:180] if p["texts"] else "")))
            PENDING.pop(coin, None); continue
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
        if over_cap and not TEST_MODE:
            notify("【信号·跳过】%s 同时持仓已满 %d 笔" % (coin, MAX_OPEN))
            PENDING.pop(coin, None); continue
        if coin in open_pos:
            _ex = open_pos[coin]
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

HELP_TEXT = """【机器人指令】在「开单记录」群直接发这些词（短消息即可）：
· 帮助 —— 看这份清单
· 状态 —— 运行状态 / 监控群 / 持仓数
· 持仓情况 —— 汇报全部持仓（也可写：持仓 BTC）
· 全部平仓 —— 立即平掉全部持仓
· 平仓 BTC —— 平掉某个币
· 减仓 BTC 50 —— 减掉 50%（默认一半）
· 修改止损 BTC 0.85 —— 改某笔止损
· 移保本 BTC —— 止损移到开仓价
· 暂停 / 继续 —— 暂停时不动作（仍记录）
· 修改监控群 开单记录,暴富龙,UA-nurseneil2
· 修改金额 300 / 修改杠杆 3
· 测试模式 开 / 测试模式 关 —— 是否忽略 5 笔上限
· 实盘模式 开 确认 / 实盘模式 关 —— 真实下单层开关（默认影子：只记录计划不发单）
· 挂单情况 —— 列出当前挂单 + 待确认信号 + 各持仓的止损止盈
· 待确认 —— 重发当前等你确认的信号
· 开 / 不开 —— 把握不准时机器人会问你，回「开」才开单、回「不开」作废"""

def load_runtime():
    global GROUPS, MARGIN, LEV, NOTIONAL, TEST_MODE
    try:
        if os.path.exists(RUNTIME):
            cfg = json.load(open(RUNTIME, encoding="utf-8"))
            if cfg.get("groups"):
                GROUPS = [g for g in cfg["groups"] if g]
            if cfg.get("margin"):
                MARGIN = float(cfg["margin"])
            if cfg.get("leverage"):
                LEV = int(cfg["leverage"])
            NOTIONAL = MARGIN * LEV
            if "test_mode" in cfg:
                TEST_MODE = bool(cfg["test_mode"])
            # 真实下单层开关：跟随 runtime_config.json（热加载时也会走到这里）
            if _BEXEC_OK:
                bexec.LIVE[0] = bool(cfg.get("live_trading", False))
                bexec.LEV = LEV
            log("已载入运行配置：监控群=%s 保证金=%.0fU 杠杆=%d倍 测试模式=%s ｜ 真实下单层=%s"
                % ("、".join(GROUPS), MARGIN, LEV, TEST_MODE, _be_mode()))
    except Exception as e:
        log("读取运行配置失败: " + str(e)[:80])

def save_runtime():
    try:
        _old = {}
        try:
            _old = json.load(open(RUNTIME, encoding="utf-8"))
        except Exception:
            pass
        out = {"groups": GROUPS, "margin": MARGIN, "leverage": LEV, "test_mode": TEST_MODE}
        # ⚠️ 必须保留 live_trading：否则任何一条指令（改金额/改杠杆/改监控群）都会把实盘开关悄悄抹掉
        if "live_trading" in _old:
            out["live_trading"] = _old["live_trading"]
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
    if _BEXEC_OK:      # 真实层同步：撤旧挂单，全平 or 按剩余量重挂止损
        try:
            _sym = coin.upper() + "USDT"
            bexec.cancel_all(_sym)
            if tr["remaining"] <= 0.001:
                real_plan_close(coin, tr["dir"], None)
            else:
                bexec.sync_sl(_sym, tr["dir"], tr.get("sl") or tr["entry"],
                              real_qty_estimate(tr["entry"], tr["remaining"]))
            log("   ↳ [真实下单层·%s] 已记录手工平/减仓的对应计划" % _be_mode())
        except Exception as _e:
            log("   ⚠️ 真实层手工平仓计划失败：%s" % str(_e)[:120])
    if tr["remaining"] <= 0.001:
        open_pos_ref.pop(coin, None)
        notify("【已平仓·纸面】%s %s（%s）@%.8g\n本次盈亏：%+.1fU · 累计：%+.1fU"
               % (coin, tr["dir"], why, px, pnl, tr["realized"]))
    else:
        notify("【已减仓·纸面】%s %s（%s）@%.8g\n本次平掉 %.0f%% · 盈亏 %+.1fU · 剩余 %.0f%%"
               % (coin, tr["dir"], why, px, part * 100, pnl, tr["remaining"] * 100))

def _handle_ask(verb, coin_hint=""):
    """处理用户的「开 / 不开」回复。返回 True 表示这是一条指令。"""
    # 找出要处理的待确认信号：指定币种优先，否则取最近挂起的那一个
    coin = None
    if coin_hint:
        for c in ASKING:
            if c.upper() == coin_hint or c.upper().startswith(coin_hint):
                coin = c
                break
        if coin is None:
            notify("【指令】没有 %s 的待确认信号。当前待确认：%s"
                   % (coin_hint, "、".join(ASKING) or "无"))
            return True
    elif ASKING:
        coin = max(ASKING, key=lambda c: ASKING[c].get("ask_ts", 0))
    else:
        notify("【指令】当前没有待确认的信号")
        return True

    item = ASKING.pop(coin)
    p = item["p"]
    if verb in ("不开", "作废"):
        notify("【指令】已作废 %s 的待确认信号（未下单）" % coin)
        log("   ❌ 用户选择不开：%s" % coin)
        return True
    # 用户说「开」→ 把信号放回待确认池，走正常出单流程
    p["deadline"] = 0                 # 立刻处理，不再等合并窗口
    PENDING[coin] = p
    notify("【指令】收到「开」→ %s 立刻按解析结果出单（保证金 %.0fU × %d倍）" % (coin, MARGIN, LEV))
    log("   ✅ 用户确认开单：%s，放回待确认池立即出单" % coin)
    return True


def handle_command(txt):
    """返回 True 表示这条消息是指令（已处理，不再走信号流程）"""
    global GROUPS, MARGIN, LEV, NOTIONAL, TEST_MODE
    t = re.sub(r"\s+", " ", (txt or "")).strip()
    if len(t) > 60:
        return False
    KEY = ["帮助", "状态", "持仓情况", "持仓", "全部平仓", "确认全部平仓", "平仓", "减仓",
           "修改止损", "移保本", "暂停", "继续", "修改监控群", "修改金额", "修改杠杆", "测试模式",
           "实盘模式", "挂单情况", "挂单", "待确认"]
    # 去掉可能的昵称/时间前缀后，指令必须在消息开头（防止转发内容被误当指令）
    nick = lambda x: re.sub(r"^[^\s]{2,16}\s+", "", x)
    tm = lambda x: re.sub(r"^\d{1,2}:\d{2}\s*(AM|PM)?\s*", "", x, flags=re.I).strip()
    # ===== 「开 / 不开」确认（用户 2026-09-14 要求：把握不准必须问他）=====
    # 用**严格全匹配**且要求极短，避免"开单记录"这类正常文字被误当指令。
    _ct = (tm(t) or t).strip()
    if len(_ct) <= 16:
        _m = re.fullmatch(r"(不开|作废|开单|开)[\s:：]*([A-Za-z0-9]{2,12})?", _ct)
        if _m:
            return _handle_ask(_m.group(1), (_m.group(2) or "").upper())
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
        notify("【机器人状态】\n监控群：%s\n持仓：%d 笔（%s）\n单笔：保证金 %.0fU × %d倍 = 名义 %.0fU\n测试模式：%s\n暂停：%s"
               % ("、".join(GROUPS), len(open_pos_ref), "、".join(open_pos_ref) or "-",
                  MARGIN, LEV, NOTIONAL, "开" if TEST_MODE else "关", "是" if PAUSED[0] else "否"))
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
        notify("【指令】已暂停：仍会抓取和记录，但不会开单/平仓。回复「继续」恢复")
    elif cmd.startswith("继续"):
        PAUSED[0] = False
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
        except Exception:
            pass
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(user_data_dir=BASE + "/fs_bot", headless=False,
                                                  args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                                                        "--disable-software-rasterizer", "--renderer-process-limit=2",
                                                        "--js-flags=--max-old-space-size=320",
                                                        "--disable-features=Translate,BackForwardCache"])
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
        json.dump({"open": open_pos, "last": last_id, "seen": sorted(SEEN)[-800:],
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
            missed_sig = []          # 本轮被闸门拦下的消息（只通报，不下单）
            for g in GROUPS:
                page = pages.get(g)
                if g not in to_scan:
                    continue
                if page is None or page.is_closed():
                    # ⚠️ 旧代码在 page 为 None 时直接 continue —— 那个群会永久停止监控，且日志里毫无提示。
                    #    现在改为主动重开；重开后保留原游标，停机期间的消息由回补闸门处理。
                    log("[%s] 页面不存在/已关闭，正在重新打开…" % g)
                    try:
                        _pg, _rows = open_group_page(ctx, g)
                        pages[g] = _pg
                        adopt_page(g, _rows)
                    except Exception as _e:
                        pages[g] = None
                        log("[%s] 重开失败（下一轮会继续重试）：%s" % (g, str(_e)[:80]))
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
                        txt = r["text"]
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
                    if "crash" in msg.lower() or "closed" in msg.lower():
                        try:
                            pages[g].close()
                        except Exception:
                            pass
                        log("[%s] 页面崩溃，正在重建…" % g)
                        try:
                            pages[g] = open_group_page(ctx, g)[0]
                        except Exception:
                            pages[g] = None
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
                        if _BEXEC_OK:      # 真实层：仓位已了结 → 撤掉剩余挂单
                            try:
                                bexec.cancel_all(coin.upper() + "USDT")
                                log("   ↳ [真实下单层·%s] 已记录撤单计划（仓位已了结）" % _be_mode())
                            except Exception as _e:
                                log("   ⚠️ 真实层撤单计划失败：%s" % str(_e)[:120])
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
                        # 2R 兜底档只平 1/3（由 tp_part 显式指定）；常规档位仍按 1/档数 平分
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
                        if _BEXEC_OK:      # 真实层：限价止盈成交 → Algo 止损数量必须跟着改（撤单重挂）
                            try:
                                bexec.sync_sl(coin.upper() + "USDT", tr["dir"],
                                              tr.get("sl") or tr["entry"],
                                              real_qty_estimate(tr["entry"], remaining))
                                log("   ↳ [真实下单层·%s] 已记录止损同步计划：%s，数量按剩余 %.0f%% 重算"
                                    % (_be_mode(), "移到开仓价" if hit_i == 0 else "价格不变", remaining * 100))
                            except Exception as _e:
                                log("   ⚠️ 真实层止损同步计划失败：%s" % str(_e)[:120])
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
            json.dump({"open": open_pos, "last": last_id, "seen": sorted(SEEN)[-800:],
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
    main()
