import os, json, time, base64, datetime
os.environ.setdefault("DISPLAY", ":99")
from playwright.sync_api import sync_playwright

BASE = os.environ.get("SIGNAL_BOT_BASE", "/home/ubuntu/signal-bot")
PROFILE = BASE + "/fs_bot"
OUTDIR = BASE + "/v16"
IMGDIR = OUTDIR + "/ua_imgs"
CST = datetime.timezone(datetime.timedelta(hours=8))
CUTOFF = int(datetime.datetime(2026, 8, 30, 0, 0, tzinfo=CST).timestamp())
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

# broad inventory: every img, canvas, background-image inside each message row
SCAN = r"""
() => {
  const lists = Array.from(document.querySelectorAll('.list_items')).filter(l => l.querySelectorAll('.messageItem-wrapper').length > 2);
  if (!lists.length) { return {rows: []}; }
  const list = lists[lists.length - 1];
  const out = [];
  for (const row of list.children) {
    const item = row.querySelector('.js-message-item');
    if (!item) { continue; }
    const media = [];
    for (const im of row.querySelectorAll('img')) {
      const r = im.getBoundingClientRect();
      media.push({k: 'img', cls: String(im.className || '').slice(0, 46), nw: im.naturalWidth, w: Math.round(r.width), h: Math.round(r.height), blob: String(im.src || '').indexOf('blob:') === 0});
    }
    for (const cv of row.querySelectorAll('canvas')) {
      const r = cv.getBoundingClientRect();
      media.push({k: 'canvas', cls: String(cv.className || '').slice(0, 46), nw: cv.width, w: Math.round(r.width), h: Math.round(r.height)});
    }
    const big = media.filter(m => (m.nw || 0) >= 150 || m.w >= 150);
    out.push({id: item.getAttribute('id'), text: (row.innerText || '').replace(/\n+/g, ' ').slice(0, 90), nmedia: media.length, nbig: big.length, big: big.slice(0, 4), all: media.slice(0, 6)});
  }
  return {rows: out};
}
"""

WAITIMG = r"""
async (mid) => {
  const item = document.querySelector('.js-message-item[id="' + mid + '"]');
  if (!item) return 'NOITEM';
  const t0 = Date.now();
  while (Date.now() - t0 < 5000) {
    const l = Array.from(item.querySelectorAll('img')).filter(i => i.naturalWidth >= 150);
    if (l.length && l.every(i => i.complete)) return 'READY';
    await new Promise(r => setTimeout(r, 250));
  }
  return 'TIMEOUT';
}
"""

GETIMGS = r"""
async (mid) => {
  const item = document.querySelector('.js-message-item[id="' + mid + '"]');
  if (!item) return null;
  const imgs = Array.from(item.querySelectorAll('img')).filter(i => i.naturalWidth >= 150);
  const out = [];
  for (const im of imgs) {
    if (String(im.src).indexOf('blob:') === 0) {
      try {
        const r = await fetch(im.src);
        const b = await r.blob();
        const d = await new Promise(res => { const fr = new FileReader(); fr.onload = () => res(fr.result); fr.readAsDataURL(b); });
        out.push({kind: 'blob', data: d}); continue;
      } catch (e) { out.push({kind: 'bloberr'}); continue; }
    }
    out.push({kind: 'other', src: String(im.src).slice(0, 120)});
  }
  return out;
}
"""

def dt_of(i):
    return datetime.datetime.fromtimestamp(int(int(i) >> 32), CST)

def main():
    os.makedirs(IMGDIR, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(user_data_dir=PROFILE, headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
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
        print("row hit:", bool(hit), flush=True)
        if not hit: ctx.close(); return
        page.mouse.click(hit["x"], hit["y"])
        store = {}
        imgs_saved = 0
        page.mouse.move(900, 400)
        for k in range(12):
            time.sleep(2.5)
            if page.evaluate(SCAN)["rows"]: break
        for rnd in range(40):
            sc = page.evaluate(SCAN)
            rows = sc.get("rows", [])
            for r in rows:
                prev = store.get(r["id"])
                if prev is None:
                    r["dt"] = dt_of(r["id"]).strftime("%Y-%m-%d %H:%M:%S")
                    r["imgs"] = []
                    store[r["id"]] = r
                if r.get("nbig", 0) > 0 and not (prev or {}).get("imgs"):
                    page.evaluate(WAITIMG, r["id"])
                    data = page.evaluate(GETIMGS, r["id"])
                    saved = 0
                    for i, d in enumerate(data or []):
                        if d.get("kind") == "blob" and str(d.get("data", "")).startswith("data:image"):
                            try:
                                fn = r["id"] + "_" + str(i) + ".png"
                                open(os.path.join(IMGDIR, fn), "wb").write(base64.b64decode(d["data"].split(",", 1)[1]))
                                store[r["id"]].setdefault("imgs", []).append(fn); saved += 1; imgs_saved += 1
                            except Exception: pass
                        elif d.get("kind") == "other":
                            store[r["id"]].setdefault("othersrc", []).append(d.get("src"))
                    st = store[r["id"]]
                    st["media"] = r.get("big")
                    print("  MEDIA %s %s nbig=%d saved=%d %s" % (r["id"], st["dt"], r.get("nbig", 0), saved,
                          json.dumps(r.get("big", []), ensure_ascii=False)[:200]), flush=True)
            oldest = min((int(k) for k in store), default=None)
            print("round %d rows %d stored %d oldest %s imgs %d" % (rnd, len(rows), len(store),
                  dt_of(oldest).strftime("%m-%d %H:%M") if oldest else "-", imgs_saved), flush=True)
            if oldest and (oldest >> 32) <= CUTOFF:
                print("reached cutoff", flush=True); break
            page.mouse.wheel(0, -1500)
            time.sleep(2.0)
        with open(OUTDIR + "/ua_media.jsonl", "w", encoding="utf-8") as f:
            for k in sorted(store, key=lambda x: int(x)):
                f.write(json.dumps(store[k], ensure_ascii=False) + "\n")
        withimg = [v for v in store.values() if v.get("imgs")]
        print("SAVED messages=%d images=%d msgs-with-image=%d" % (len(store), imgs_saved, len(withimg)), flush=True)
        for v in withimg:
            print("   IMG %s %s %s" % (v["dt"], v["id"], v["text"][:60]), flush=True)
        ctx.close()

main()
