#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书跟单 · dryRun 机器人 v2（每群一个独立标签页，永不切换会话）
- 每个群一个 page，打开后一直停在该群 → 彻底避免"读错群"
- 消息时间 = message-id 高位（Unix 秒），与页面显示一致
- 只处理开单信号；闲聊直接跳过；博主管理指令单独处理
- 全链路计时：信号发出 → 发现 → 抓图 → 解析 → 读图 → 下单(纸面) → 推送
"""
import os, re, json, time, base64, datetime
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

GROUPS = ["开单记录", "暴富龙", "UA-nurseneil2", "医生DrProfit2群", "颜驰2群"]
POLL_SEC = 2
MARGIN = 300.0
LEV = 3
NOTIONAL = MARGIN * LEV
MAX_OPEN = 3
TP_TIERS = 3
MSG_URL = "https://www.feishu.cn/messenger"
SEARCH_TERM = {"颜驰2群": "颜驰"}

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
              "\"entry\":数字或null,\"entry_is_cmp\":bool,\"add_price\":数字或null,"
              "\"stop\":数字或null,\"targets\":[数字],\"tp_on_chart\":bool,"
              "\"type\":\"open|manage|info\",\"manage_action\":\"close_all|trim|move_stop_to_cost|null\"}"
              " 规则：只用消息里真实出现的数字，绝不编造；止盈写在图上则 tp_on_chart=true 且 targets 为空。")
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
             signal_entry=None, signal_src=None, crossed=False, entry_mode="市价"):
    d = 1 if (direction or "LONG").upper() == "LONG" else -1
    lev = LEV
    L = []
    L.append("【已开单·纸面】%s/USDT 永续 · %s" % (coin, "做多 LONG" if d == 1 else "做空 SHORT"))
    L.append("")
    L.append("来源：%s   信号时间：%s" % (src, when))
    L.append("金额：保证金 %.0fU %d倍 = 名义 %.0fU" % (MARGIN, lev, NOTIONAL))
    L.append("开仓：")
    if isinstance(entry, (int, float)):
        if entry_mode == "限价挂单":
            L.append("你的开仓价：限价挂单 %.8g（还没成交，等价格到位）" % entry)
        elif entry_mode == "市价":
            L.append("你的开仓价：市价成交 %.8g" % entry)
        else:
            L.append("你的开仓价：%.8g" % entry)
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
    m = re.search(r"(?:Going|Market|Longing|Buying)\s+(long|short)\s+\$?([A-Za-z0-9]{2,12})", txt, re.I)
    if m:
        dirc = "LONG" if m.group(1).lower() == "long" else "SHORT"
        coin = m.group(2).upper()
    else:
        m = re.search(r"([A-Za-z0-9]{2,12})\s*(?:/USDT)?\s*[—\-–]\s*(LONG|SHORT)\b", txt, re.I)
        if not m:
            return None
        coin, dirc = m.group(1).upper(), m.group(2).upper()
    if _SYMS and coin not in _SYMS:
        return None                       # 币种不在币安合约清单里 → 交给 AI 判断
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
    tps = []
    for x in re.findall(r"TP\s?\d?\s*[:：]?\s*\$?([0-9]*\.?[0-9]+)", txt, re.I):
        try: tps.append(float(x))
        except Exception: pass
    if not (stop or add or entry or tps):
        return None
    return {"is_signal": True, "coin": coin, "direction": dirc, "entry": entry,
            "entry_is_cmp": bool(re.search(r"\bCMP\b|市价|现价", txt, re.I)),
            "add_price": add, "stop": stop, "targets": tps,
            "tp_on_chart": bool(re.search(r"TPs?\s+above|止盈在?上方|止盈位在上方", txt, re.I)),
            "type": "open", "manage_action": None, "_fast": True}

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

def finalize_pending(open_pos):
    """到点或信息齐全 -> 出单；信息不足 -> 只发提醒，绝不猜价"""
    now = time.time()
    for coin in list(PENDING):
        p = PENDING[coin]
        tps = sorted(set(p["tps"]))[:TP_TIERS]
        if not pending_complete(p) and now < p["deadline"]:
            continue                                  # 继续等同一条信号的后续消息（最多 8 秒）
        signal_entry = p["entry"]                     # 博主信号里的开仓价（图上/卡片读到）
        age = now - p["first_ts"]
        dirc0 = (p.get("dir") or "LONG").upper()
        mkt = price_of(coin)
        if mkt is None:
            notify("【信号·待确认】%s\n拿不到币安实时价，无法开仓\n原文：%s"
                   % (coin, (p["texts"][0][:180] if p["texts"] else "")))
            PENDING.pop(coin, None); continue
        # 开仓方式（用户规则）：
        #   博主价与市价相差 ±2% 以内 -> 市价开仓
        #   做多且博主价高于市价 2% 以上 -> 在博主开仓价挂限价单
        #   做空且博主价低于市价 2% 以上 -> 在博主开仓价挂限价单
        #   其余（价格已朝不利方向跑过头）-> 不自动开，发提醒等确认
        entry, entry_mode, esrc = mkt, "市价", "市价成交"
        if isinstance(signal_entry, (int, float)) and signal_entry and mkt:
            diff = (signal_entry - mkt) / mkt
            if abs(diff) <= 0.02:
                entry, entry_mode = mkt, "市价"
            elif dirc0 == "LONG" and diff > 0.02:
                entry, entry_mode, esrc = signal_entry, "限价挂单", "限价挂单(待成交)"
            elif dirc0 == "SHORT" and diff < -0.02:
                entry, entry_mode, esrc = signal_entry, "限价挂单", "限价挂单(待成交)"
            else:
                notify("【信号·需你确认】%s %s\n博主开仓价 %.8g 与现价 %.8g 相差 %.2f%%（超过 2%%，且方向不利：%s）\n"
                       "按规则我不自动开仓。你回一句「市价追」或「挂博主价等回调」，我再下。\n原文：%s"
                       % (coin, dirc0, signal_entry, mkt, diff * 100, "价格已涨过头" if dirc0 == "LONG" else "价格已跌过头",
                          (p["texts"][0][:160] if p["texts"] else "")))
                PENDING.pop(coin, None); continue
        if p["stop"] is None and not tps:
            notify("【信号·待确认】%s\n没读到止损和止盈（图上/卡片/文字都没读到），等你确认后我再挂单\n原文：%s"
                   % (coin, (p["texts"][0][:180] if p["texts"] else "")))
            PENDING.pop(coin, None); continue
        if len(open_pos) >= MAX_OPEN and coin not in open_pos:
            notify("【信号·跳过】%s 同时持仓已满 %d 笔" % (coin, MAX_OPEN))
            PENDING.pop(coin, None); continue
        dirc = dirc0
        tm = {"detect": p["t_found"] - p["first_ts"], "img": max(0.0, p["t_img"] - p["t_found"]),
              "parse": max(0.0, p["t_parse"] - p["t_img"]), "chart": max(0.0, p["t_chart"] - p["t_parse"]),
              "order": 0.0, "push": 0.0, "wait": age, "messages": len(p["texts"])}
        t_order = time.time()
        tr = {"coin": coin, "dir": dirc, "entry": entry, "signal_entry": signal_entry, "add": p["add"],
              "sl": p["stop"], "tps": tps, "entry_src": esrc, "stop_src": p.get("stop_src"),
              "t_open": datetime.datetime.fromtimestamp(p["first_ts"], CST).strftime("%m-%d %H:%M:%S"),
              "group": p["group"], "text": (p["texts"][0] if p["texts"] else "")[:300], "status": "OPEN",
              "chart_imgs": p["imgs"], "timer": tm}
        tm["order"] = time.time() - t_order
        open_pos[coin] = tr
        timing = ("⏱ 从信号发出到推送 共 %.1fs（其中：发现 %.1fs / 抓图 %.1fs / 解析 %.1fs / 读图 %.1fs / 等同一条信号后续消息 %.1fs）"
                  % (age, tm["detect"], tm["img"], tm["parse"], tm["chart"], tm["wait"]))
        t_push0 = time.time()
        d0 = 1 if dirc == "LONG" else -1
        crossed = any(((t - entry) * d0 <= 0) for t in tps)   # 止盈价是否已被现价越过（真实下单必须处理）
        notify(fmt_plan(coin, dirc, entry, p["stop"], tps, p["group"], tr["t_open"],
                        note=("止损来自%s，共合并 %d 条消息（文案/卡片/图）" % (p.get("stop_src") or "-", len(p["texts"]) + (1 if p["imgs"] else 0))),
                        add=p["add"], timing=timing, signal_entry=signal_entry,
                        signal_src=p.get("entry_src"), crossed=crossed, entry_mode=entry_mode))
        tm["push"] = time.time() - t_push0
        with open(TRADES, "a", encoding="utf-8") as f:
            f.write(json.dumps(tr, ensure_ascii=False) + "\n")
        log("⏱ [%s] %s 出单 | 总=%.1fs | 发现=%.1fs 抓图=%.1fs 解析=%.1fs 读图=%.1fs 等待=%.1fs 推送=%.1fs | 市价开仓=%s 信号价=%s 止损=%s 止盈=%s"
            % (p["group"], coin, age, tm["detect"], tm["img"], tm["parse"], tm["chart"], tm["wait"], tm["push"],
               entry, signal_entry, p["stop"], tps))
        PENDING.pop(coin, None)

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
    last_id, open_pos, trades = {}, {}, []
    if os.path.exists(STATE):
        try:
            sv = json.load(open(STATE, encoding="utf-8"))
            for k, v in (sv.get("last") or {}).items():
                last_id[k] = int(v)
            _op = sv.get("open")
            if isinstance(_op, dict):
                open_pos.update(_op)
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
        for g in GROUPS:
            pg, rows = open_group_page(ctx, g)
            pages[g] = pg
            ids = [int(r["id"]) for r in rows if r.get("id")]
            if ids:
                last_id[g] = max(last_id.get(g, 0), max(ids))
                log("[%s] 已打开并定位 最新 %s  id=%s | 末条=%s" % (
                    g, datetime.datetime.fromtimestamp(max(ids) >> 32, CST).strftime("%m-%d %H:%M:%S"),
                    max(ids), (rows[-1].get("text") or "")[:36]))
            else:
                log("[%s] 打开失败（未读到消息）" % g)
        log("==== 开始实时监控（%d 个页面）====" % len(pages))
        json.dump({"open": [], "last": last_id, "ts": datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")},
                  open(STATE, "w"), ensure_ascii=False, indent=1)
        try:
            price_of("BTC")
            log("币安行情已预热（减少下单阶段耗时）")
        except Exception:
            pass
        notify("【跟单机器人】dryRun 已启动（纸面模式，只抓开单信号，不会下单）")
        hb = 0
        while True:
            for g in GROUPS:
                page = pages.get(g)
                if page is None:
                    continue
                if page.is_closed():
                    log("[%s] 页面已关闭，重新打开" % g)
                    try:
                        pages[g] = open_group_page(ctx, g)[0]
                    except Exception:
                        pages[g] = None
                    continue
                try:
                    page.mouse.move(900, 400); page.mouse.wheel(0, 2600); time.sleep(0.35)
                    rows = page.evaluate(SCAN_JS)
                    if not rows: continue
                    base = last_id.get(g, 0)
                    new = [r for r in rows if r.get("id") and int(r["id"]) > base]
                    if len(new) > 15:
                        log("[%s] 忽略 %d 条回放" % (g, len(new))); new = []
                    if not new: continue
                    last_id[g] = max(int(r["id"]) for r in new)
                    for r in sorted(new, key=lambda r: int(r["id"])):
                        mid = r["id"]
                        t_sig = int(mid) >> 32
                        when = datetime.datetime.fromtimestamp(t_sig, CST).strftime("%m-%d %H:%M:%S")
                        txt = r["text"]
                        log("[%s] 发现新消息 | 发出=%s | %s" % (g, when, txt[:110]))
                        low = txt.lower()
                        if "通过webhook" in txt or "【跟单机器人】" in txt or "invited" in low or "test notification" in low:
                            continue
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
                        info = fast_parse(txt)
                        if info is None:
                            info = parse_text(txt)
                        else:
                            log("   ⚡ 快速解析命中（本地正则，0 AI 调用）")
                        t_parse = time.time()
                        coin = (info.get("coin") or "").upper() or None
                        dirc = (info.get("direction") or "").upper() or None
                        chart = None
                        for f in imgs:
                            try:
                                c = read_chart(f)
                                if c.get("ok"):
                                    chart = c
                                    log("   读图: 止损 %s 开仓 %s 止盈 %s" % (c["sl"], c["entry"], c["tps"]))
                                    break
                            except Exception as e:
                                log("   读图失败: " + str(e)[:90])
                        # 只有图、文字里没有币种 -> 从图上读币种
                        if coin is None and imgs:
                            meta = read_chart_meta(imgs[-1])
                            mc = (meta.get("coin") or "").upper()
                            if meta.get("is_chart") and mc and (not _SYMS or mc in _SYMS):
                                coin = mc
                                dirc = dirc or ((meta.get("direction") or "").upper() or "LONG")
                                log("   图上读到币种: %s %s" % (coin, dirc))
                        t_chart = time.time()
                        stamps = {"found": t_found, "img": t_img, "parse": t_parse, "chart": t_chart}
                        # 博主管理指令：立即处理
                        if info.get("type") == "manage" or (info.get("manage_action") and info.get("manage_action") != "other"):
                            notify("【博主指令】%s\n群：%s  时间：%s\n动作：%s\n原文：%s" % (coin or "?", g, when, info.get("manage_action"), txt[:200]))
                            continue
                        if coin and dirc in ("LONG", "SHORT"):
                            # 开单信号 -> 进待确认池，等同一条信号的后续消息（卡片/图）补齐
                            p = merge_pending(coin, g, info=info, chart=chart, imgs=imgs, t_sig=t_sig, txt=txt, stamps=stamps)
                            if dirc: p["dir"] = dirc
                            log("   待确认池 %s：开仓=%s(%s) 加仓=%s 止损=%s 止盈=%s 图=%d 已合并%d条消息" % (
                                coin, p["entry"], p.get("entry_src") or "-", p["add"], p["stop"],
                                sorted(set(p["tps"])), len(p["imgs"]), len(p["texts"])))
                        elif coin and coin in PENDING and (chart or imgs or any(isinstance(info.get(k), (int, float)) for k in ("entry", "stop", "add_price"))):
                            # 后续消息（卡片/带图）补进同一条信号
                            merge_pending(coin, g, info=info, chart=chart, imgs=imgs, t_sig=t_sig, txt=txt, stamps=stamps)
                            log("   并入 %s 的待确认池（补充信息，图=%d）" % (coin, len(imgs)))
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
            # 待确认池：信息齐全就出单，到点还没齐也只发提醒（绝不猜价）
            try:
                finalize_pending(open_pos)
            except Exception as e:
                log("待确认池处理异常 " + str(e)[:100])
            # 纸面持仓监控
            try:
                for coin, tr in list(open_pos.items()):
                    px = price_of(coin)
                    if px is None: continue
                    d = 1 if tr["dir"] == "LONG" else -1
                    hit = None
                    if tr.get("sl") and ((px - tr["sl"]) * d <= 0): hit = ("止损", tr["sl"])
                    elif tr.get("tps") and ((px - tr["tps"][0]) * d >= 0): hit = ("止盈1", tr["tps"][0])
                    if hit:
                        pnl = (hit[1] - tr["entry"]) * d / tr["entry"] * NOTIONAL
                        tr.update({"status": "CLOSED", "exit": hit[1], "exit_why": hit[0], "pnl": pnl})
                        with open(TRADES, "a", encoding="utf-8") as f:
                            f.write(json.dumps(tr, ensure_ascii=False) + "\n")
                        notify("【已结单·纸面】%s %s\n结果：%s @%.8g\n盈亏：%+.1fU（保证金 %.0fU）" % (coin, tr["dir"], hit[0], hit[1], pnl, MARGIN))
                        open_pos.pop(coin, None)
            except Exception as e:
                log("持仓监控异常 " + str(e)[:100])
            hb += 1
            json.dump({"open": open_pos, "last": last_id, "ts": datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")},
                      open(STATE, "w"), ensure_ascii=False, indent=1)
            if hb % 10 == 0:
                log("心跳：运行中 | 持仓 %d 笔（%s）" % (len(open_pos), ",".join(open_pos) or "-"))
            time.sleep(POLL_SEC)

main()
