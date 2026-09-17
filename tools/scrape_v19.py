import os, re, json, time, base64, datetime, hashlib, requests
from PIL import Image
os.environ.setdefault("DISPLAY", ":99")
from playwright.sync_api import sync_playwright

BASE = os.environ.get("SIGNAL_BOT_BASE", "/home/ubuntu/signal-bot")
OUT = BASE + "/v19"
IMGDIR = OUT + "/ua_imgs"
os.makedirs(IMGDIR, exist_ok=True)
CST = datetime.timezone(datetime.timedelta(hours=8))
CUTOFF = int(datetime.datetime(2026, 9, 1, 0, 0, tzinfo=CST).timestamp())
KEY = os.environ.get("DEEPSEEK_API_KEY", "")
API = "https://api.deepseek.com/chat/completions"
NAME = "UA-nurseneil2"

FINDROW = r"""
(name) => {
  const rows = Array.from(document.querySelectorAll('[class*="a11y_feed_card_main"]'));
  for (const el of rows) {
    if ((el.innerText || '').indexOf(name) >= 0) {
      el.scrollIntoView({block: 'center'});
      const r = el.getBoundingClientRect();
      return {x: r.x + r.width/2, y: r.y + r.height/2};
    }
  }
  return null;
}
"""
NFCARDS = r"""() => document.querySelectorAll('[class*="a11y_feed_card_main"]').length"""
SCAN = r"""
() => {
  const lists = Array.from(document.querySelectorAll('.list_items')).filter(l => l.querySelectorAll('.messageItem-wrapper').length > 2);
  if (!lists.length) return {rows: []};
  const list = lists[lists.length - 1];
  const out = [];
  for (const row of list.children) {
    const item = row.querySelector('.js-message-item');
    if (!item) continue;
    const imgs = Array.from(row.querySelectorAll('img'));
    out.push({id: item.getAttribute('id'), nimg: imgs.length,
              nloaded: imgs.filter(i => i.naturalWidth >= 150).length,
              text: (row.innerText || '').replace(/\n+/g, ' ').slice(0, 80)});
  }
  return {rows: out};
}
"""
WAITANY = r"""
async (mid) => {
  const item = document.querySelector('.js-message-item[id="' + mid + '"]');
  if (!item) return 'NOITEM';
  const t0 = Date.now();
  while (Date.now() - t0 < 6000) {
    const l = Array.from(item.querySelectorAll('img')).filter(i => i.naturalWidth >= 150);
    if (l.length) return 'LOADED';
    await new Promise(r => setTimeout(r, 250));
  }
  return 'STILL_SMALL';
}
"""
GETIMGS = r"""
async (mid) => {
  const item = document.querySelector('.js-message-item[id="' + mid + '"]');
  if (!item) return [];
  const out = [];
  for (const im of Array.from(item.querySelectorAll('img')).filter(i => i.naturalWidth >= 150)) {
    if (String(im.src).indexOf('blob:') !== 0) continue;
    try {
      const r = await fetch(im.src);
      const b = await r.blob();
      const d = await new Promise(res => { const fr = new FileReader(); fr.onload = () => res(fr.result); fr.readAsDataURL(b); });
      out.push(d);
    } catch (e) {}
  }
  return out;
}
"""
def dt_of(i): return datetime.datetime.fromtimestamp(int(int(i) >> 32), CST)

def scrape():
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(user_data_dir=BASE + "/fs_bot", headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.feishu.cn/messenger", wait_until="domcontentloaded", timeout=90000)
        time.sleep(12)
        for i in range(24):
            if page.evaluate(NFCARDS): break
            page.mouse.move(380, 400); page.mouse.wheel(0, 400); time.sleep(1.5)
        hit = None
        for i in range(30):
            hit = page.evaluate(FINDROW, NAME)
            if hit: break
            page.mouse.move(380, 400); page.mouse.wheel(0, 900); time.sleep(1.2)
        if not hit: print("OPEN FAILED", flush=True); ctx.close(); return []
        page.mouse.click(hit["x"], hit["y"])
        for k in range(12):
            time.sleep(2.5)
            if page.evaluate(SCAN)["rows"]: break
        store = {}
        page.mouse.move(900, 400)
        for rnd in range(30):
            sc = page.evaluate(SCAN)
            for r in sc.get("rows", []):
                st = store.setdefault(r["id"], {"id": r["id"], "dt": dt_of(r["id"]).strftime("%Y-%m-%d %H:%M:%S"),
                                                "text": r.get("text", ""), "imgs": [], "tries": 0})
                if r.get("nimg", 0) > 0 and not st["imgs"] and st["tries"] < 3:
                    st["tries"] += 1
                    status = page.evaluate(WAITANY, r["id"])
                    data = page.evaluate(GETIMGS, r["id"])
                    for i, d in enumerate(data or []):
                        if isinstance(d, str) and d.startswith("data:image"):
                            fn = r["id"] + "_" + str(i) + ".png"
                            pth = os.path.join(IMGDIR, fn)
                            open(pth, "wb").write(base64.b64decode(d.split(",", 1)[1]))
                            st["imgs"].append(fn)
            oldest = min((int(k) for k in store), default=None)
            nimg = sum(1 for v in store.values() if v["imgs"])
            print("round %d rows %d stored %d imgs_msgs %d oldest %s" % (rnd, len(sc.get("rows", [])), len(store), nimg,
                  dt_of(oldest).strftime("%m-%d %H:%M") if oldest else "-"), flush=True)
            if oldest and (oldest >> 32) <= CUTOFF: print("cutoff reached", flush=True); break
            page.mouse.wheel(0, -1400)
            time.sleep(2.0)
        ctx.close()
    rows = [store[k] for k in sorted(store, key=lambda x: int(x))]
    with open(OUT + "/ua_media.jsonl", "w", encoding="utf-8") as f:
        for r in rows: f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("SAVED messages=%d with-images=%d" % (len(rows), sum(1 for r in rows if r["imgs"])), flush=True)
    return rows

rows = scrape()

# ---- vision: read charts with color-tag transcription, cropped + upscaled ----
SYS = ("You transcribe price TAGS printed at the right edge of TradingView charts. A tag is a small filled rectangle"
       " containing a price number. Tag colours: YELLOW/ORANGE = take-profit target; RED = stop loss;"
       " GREEN or WHITE/GREY = entry price. STRICT JSON only; never invent a number.")
P = ("This strip is the right-hand part of a chart. For every price tag you can see, report its colour and the exact"
     " number inside it. Return {\"tags\":[{\"color\":\"yellow|orange|red|green|white|grey|other\",\"value\":number}],\"is_chart\":bool}")

def strip(path, frac=0.24, scale=4):
    im = Image.open(path).convert("RGB"); w, h = im.size
    im2 = im.crop((int(w * (1 - frac)), 0, w, h)).resize((int(w * frac * scale), h * scale), Image.LANCZOS)
    o = path.replace(".png", "_tag.png"); im2.save(o); return o

def ask(img, prompt, sx):
    b64 = base64.b64encode(open(img, "rb").read()).decode()
    body = {"model": "deepseek-v4-flash-vision-exp", "temperature": 0, "messages": [
        {"role": "system", "content": sx},
        {"role": "user", "content": [{"type": "text", "text": prompt},
         {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}]}
    try:
        r = requests.post(API, headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"}, json=body, timeout=300)
        j = r.json()
    except Exception as e:
        return {"error": str(e)[:60]}
    if "choices" not in j: return {"error": "api"}
    m = re.search(r"\{[\s\S]*\}", j["choices"][0]["message"]["content"])
    try: return json.loads(m.group(0))
    except Exception: return {}

TRUTH = {"VELVET": "你给的真值 TP 0.1052/0.1246/0.1571/0.25  SL 0.0886",
         "ZEC": "你给的真值 TP 990/1023.24/1067.1  SL 964.09",
         "INIT": "你给的真值 TP 0.07065/0.07856/0.08  SL 0.0607",
         "DOGE": "你给的真值 TP 0.09965/0.11476  SL 0.0828"}
targets = []
for r in rows:
    for c in TRUTH:
        if c in (r["text"] or "").upper() and r["imgs"] and r["dt"] >= "2026-09-01":
            targets.append((c, r))
seen = set(); n = 0
print("=" * 78, flush=True)
for c, r in targets:
    for im in r["imgs"]:
        p = os.path.join(IMGDIR, im)
        if not os.path.exists(p): continue
        h = hashlib.sha1(open(p, "rb").read()).hexdigest()[:8]
        if h in seen: continue
        seen.add(h)
        v = ask(strip(p), P, SYS)
        tags = v.get("tags") or []
        tps = sorted([t["value"] for t in tags if str(t.get("color")) in ("yellow", "orange")], reverse=True)
        sl = [t["value"] for t in tags if str(t.get("color")) == "red"]
        print("%-7s %s %s chart=%s" % (c, r["dt"], im, v.get("is_chart")), flush=True)
        print("       读图 → 止损 %s | 止盈 %s" % (sl, tps), flush=True)
        print("       %s" % TRUTH[c], flush=True)
        n += 1
        if n >= 8: break
    if n >= 8: break
print("charts read:", n, flush=True)
