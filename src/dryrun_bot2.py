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
DS_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DS_API = "https://api.deepseek.com/chat/completions"
CST = datetime.timezone(datetime.timedelta(hours=8))

GROUPS = ["开单记录", "暴富龙", "UA-nurseneil2", "医生DrProfit2群", "颜驰2群"]
POLL_SEC = 5
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
    tags = []
    for t in sorted(merged, key=lambda t: t["y1"]):
        box = (max(0, x0 + t["x1"] - 5), max(0, t["y1"] - 5), min(w, x0 + t["x2"] + 6), min(h, t["y2"] + 6))
        crop = im.crop(box); sc = 8
        while crop.width * sc > 1500 or crop.height * sc > 1500:
            sc -= 1
            if sc < 2: break
        crop = crop.resize((crop.width * sc, crop.height * sc), Image.LANCZOS)
        p = RUN + "/tmp_tag.png"; crop.save(p)
        try:
            v = _vision(p)
        except Exception:
            continue
        txt = str(v.get("text", "")).replace(",", "").replace("$", "").strip()
        try:
            fv = float(txt)
        except Exception:
            fv = v.get("value") if isinstance(v.get("value"), (int, float)) else None
        if fv is None: continue
        yc = (t["y1"] + t["y2"]) // 2
        tags.append({"y": yc, "color": t["c"], "value": fv, "text": txt,
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
def fmt_plan(coin, direction, entry, sl, tps, src, when, note="", add=None, timing=None):
    d = 1 if (direction or "LONG").upper() == "LONG" else -1
    L = []
    L.append("【已开单·纸面】%s/USDT 永续 · %s" % (coin, "做多 LONG" if d == 1 else "做空 SHORT"))
    L.append("来源：%s   信号时间：%s" % (src, when))
    L.append("金额：保证金 %.0fU × %d倍 = 名义 %.0fU" % (MARGIN, LEV, NOTIONAL))
    if isinstance(entry, (int, float)):
        if add:
            L.append("入场：头仓 1/3 市价 %.8g ＋ 加仓 2/3 挂 %.8g" % (entry, add))
        else:
            L.append("入场：市价 %.8g" % entry)
    if isinstance(sl, (int, float)) and isinstance(entry, (int, float)) and entry:
        L.append("止损：%.8g → %.2f%%（不含杠杆）· 约 %+.1fU" % (sl, (sl - entry) / entry * 100 * d, (sl - entry) * d / entry * NOTIONAL))
    else:
        L.append("止损：需人工确认")
    if tps:
        for i, t in enumerate(tps[:TP_TIERS], 1):
            if isinstance(entry, (int, float)) and entry:
                L.append("止盈%d：%.8g → %+.2f%%（不含杠杆）· 平1/3 · 约 %+.1fU" % (
                    i, t, (t - entry) / entry * 100 * d, (t - entry) * d / entry * NOTIONAL / len(tps[:TP_TIERS])))
        L.append("规则：TP1 成交后止损移到开仓价（保本）")
    else:
        L.append("止盈：图上未读到，暂不挂（等你确认）")
    if note: L.append("备注：" + note)
    if timing: L.append(timing)
    return "\n".join(L)

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

FETCH_IMG_JS = """async (mid) => {
  const it = document.querySelector('.js-message-item[id="' + mid + '"]');
  if (!it) return [];
  const t0 = Date.now();
  while (Date.now() - t0 < 6000) {
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
                    page.mouse.move(900, 400); page.mouse.wheel(0, 2600); time.sleep(0.8)
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
                        if not any(k in txt for k in SIG_KW):
                            log("   ↳ 闲聊/无关，跳过")
                            continue
                        t_found = time.time()
                        imgs = []
                        if r.get("loaded", 0) > 0:
                            data = page.evaluate(FETCH_IMG_JS, mid)
                            for i, d in enumerate(data or []):
                                if isinstance(d, str) and d.startswith("data:image"):
                                    fn = IMGDIR + "/" + mid + "_" + str(i) + ".png"
                                    try:
                                        open(fn, "wb").write(base64.b64decode(d.split(",", 1)[1])); imgs.append(fn)
                                    except Exception:
                                        pass
                        t_img = time.time()
                        info = parse_text(txt)
                        t_parse = time.time()
                        coin = (info.get("coin") or "").upper() or None
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
                        t_chart = time.time()
                        if info.get("type") == "manage" or (info.get("manage_action") and info.get("manage_action") != "other"):
                            notify("【博主指令】%s\n群：%s  时间：%s\n动作：%s\n原文：%s" % (coin or "?", g, when, info.get("manage_action"), txt[:200]))
                            continue
                        dirc = (info.get("direction") or "").upper() or None
                        has_hint = bool(info.get("entry_is_cmp")) or isinstance(info.get("entry"), (int, float)) or (chart and chart.get("entry"))
                        if not (coin and dirc in ("LONG", "SHORT") and (info.get("is_signal") or chart) and (has_hint or (chart and chart.get("sl")))):
                            log("   ↳ 不是开单信号（币种=%s 方向=%s 图=%s），跳过" % (coin, dirc, bool(chart)))
                            continue
                        entry = None
                        if isinstance(info.get("entry"), (int, float)): entry = float(info["entry"])
                        elif chart and chart.get("entry"): entry = chart["entry"]
                        else: entry = price_of(coin)
                        add = info.get("add_price") if isinstance(info.get("add_price"), (int, float)) else None
                        sl = info.get("stop") if isinstance(info.get("stop"), (int, float)) else (chart.get("sl") if chart else None)
                        tps = [float(x) for x in (info.get("targets") or [])][:TP_TIERS]
                        if not tps and chart: tps = chart.get("tps") or []
                        t_order = time.time()
                        if entry is None:
                            notify("【信号·待确认】%s 无法确定入场价\n原文：%s" % (coin, txt[:200])); continue
                        if sl is None and not tps:
                            notify("【信号·待确认】%s %s 只读到入场 %s，止损/止盈没读准\n原文：%s" % (coin, dirc, entry, txt[:200])); continue
                        if len(open_pos) >= MAX_OPEN and coin not in open_pos:
                            notify("【信号·跳过】%s 同时持仓已满 %d 笔" % (coin, MAX_OPEN)); continue
                        tm = {"detect": t_found - t_sig, "img": t_img - t_found, "parse": t_parse - t_img,
                              "chart": t_chart - t_parse, "order": t_order - t_chart, "push": 0.0}
                        tm["total"] = time.time() - t_sig
                        tr = {"coin": coin, "dir": dirc, "entry": entry, "add": add, "sl": sl, "tps": tps,
                              "t_open": when, "group": g, "text": txt[:300], "status": "OPEN", "chart_imgs": imgs, "timer": tm}
                        open_pos[coin] = tr
                        timing = "⏱ 信号→推送 共 %.1fs（发现 %.1fs / 抓图 %.1fs / 解析 %.1fs / 读图 %.1fs / 下单 %.1fs）" % (
                            time.time() - t_sig, tm["detect"], tm["img"], tm["parse"], tm["chart"], tm["order"])
                        t_push0 = time.time()
                        notify(fmt_plan(coin, dirc, entry, sl, tps, g, when,
                                        note=("图上读到止损/止盈" if chart else "文字解析"), add=add, timing=timing))
                        t_push = time.time()
                        tm["push"] = t_push - t_push0; tm["total"] = t_push - t_sig
                        with open(TRADES, "a", encoding="utf-8") as f:
                            f.write(json.dumps(tr, ensure_ascii=False) + "\n")
                        log("⏱ [%s] %s 发出=%s | 发现=%.1fs | 抓图=%.1fs | 解析=%.1fs | 读图=%.1fs | 下单=%.1fs | 推送=%.1fs | 总=%.1fs" % (
                            g, coin, when, tm["detect"], tm["img"], tm["parse"], tm["chart"], tm["order"], tm["push"], tm["total"]))
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
            json.dump({"open": list(open_pos.keys()), "last": last_id, "ts": datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")},
                      open(STATE, "w"), ensure_ascii=False, indent=1)
            if hb % 10 == 0:
                log("心跳：运行中 | 持仓 %d 笔（%s）" % (len(open_pos), ",".join(open_pos) or "-"))
            time.sleep(POLL_SEC)

main()
