import os, json, base64, re, requests
from PIL import Image
KEY = os.environ.get("DEEPSEEK_API_KEY", "")
API = "https://api.deepseek.com/chat/completions"
TMP = "/home/ubuntu/signal-bot/v31"; os.makedirs(TMP, exist_ok=True)
IMGDIR = "/home/ubuntu/signal-bot/v19/ua_imgs"

def cls(r, g, b):
    mx, mn = max(r, g, b), min(r, g, b)
    if mx - mn < 40: return "white" if mx > 200 else ("grey" if mx > 110 else None)
    if r > 150 and g > 90 and b < 110 and r >= g: return "orange"
    if r > 150 and g < 100 and b < 110: return "red"
    if g > 110 and r < 130 and b < 170: return "green"
    return None

def coverage(px, w, h, y):
    xa, xb = int(w * 0.06), int(w * 0.72)
    best = 0.0
    for yy in range(max(0, y - 14), min(h, y + 15)):
        cnt = tot = 0
        for x in range(xa, xb, 2):
            r, g, b = px[x, yy]
            tot += 1
            if abs(r - 2) + abs(g - 24) + abs(b - 21) > 30: cnt += 1
        if tot and cnt / tot > best: best = cnt / tot
    return round(best, 3)

def read(path):
    im = Image.open(path).convert("RGB"); w, h = im.size
    px = im.load()
    # --- 1) 在右侧找标签色块 ---
    x0 = int(w * 0.76); x1 = int(w * 0.985)
    band = im.crop((x0, 0, x1, h)); bw, bh = band.size
    bpx = band.load()
    grid = [[cls(*bpx[x, y]) for x in range(bw)] for y in range(bh)]
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
    # --- 2) 逐个 OCR + 计算横线覆盖率 ---
    tags = []
    for t in sorted(merged, key=lambda t: t["y1"]):
        box = (max(0, x0 + t["x1"] - 5), max(0, t["y1"] - 5), min(w, x0 + t["x2"] + 6), min(h, t["y2"] + 6))
        crop = im.crop(box); sc = 8
        while crop.width * sc > 1500 or crop.height * sc > 1500:
            sc -= 1
            if sc < 2: break
        crop = crop.resize((crop.width * sc, crop.height * sc), Image.LANCZOS)
        p = TMP + "/t_%d.png" % t["y1"]; crop.save(p)
        b64 = base64.b64encode(open(p, "rb").read()).decode()
        body = {"model": "deepseek-v4-flash-vision-exp", "temperature": 0,
                "messages": [{"role": "system", "content": "Read the price number printed in this label. STRICT JSON only."},
                             {"role": "user", "content": [{"type": "text", "text": "Return {\"text\":\"<digits exactly as printed>\",\"value\":<number>}"},
                              {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}]}
        try:
            r = requests.post(API, headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"}, json=body, timeout=180)
            m = re.search(r"\{[\s\S]*\}", r.json()["choices"][0]["message"]["content"])
            v = json.loads(m.group(0))
            txt = str(v.get("text", "")).replace(",", "").replace("$", "").strip()
            try: fv = float(txt)
            except Exception: fv = v.get("value") if isinstance(v.get("value"), (int, float)) else None
            if fv is None: continue
            yc = (t["y1"] + t["y2"]) // 2
            tags.append({"y": yc, "color": t["c"], "value": fv, "text": txt, "cov": coverage(px, w, h, yc)})
        except Exception: pass
    # --- 3) 按你的规则解析：红块=止损；开仓价=止损上方第一个标签；止盈=开仓价上方且是实线的横线 ---
    reds = [t for t in tags if t["color"] == "red"]
    if not reds: return {"ok": False, "why": "没有红色止损标签", "tags": tags}
    sl = min(reds, key=lambda t: t["value"])["value"]
    above = sorted([t for t in tags if t["value"] > sl * 1.0005], key=lambda t: t["value"])
    if not above: return {"ok": False, "why": "止损上方没有标签", "tags": tags}
    entry = above[0]["value"]
    tp = []
    for t in above:
        v = t["value"]
        if v <= entry * 1.0005: continue
        if t["cov"] < 0.85: continue                      # 不是实线 -> 不是止盈线
        if any(abs(v - s) / v < 0.003 for s in tp): continue
        tp.append(v)
    return {"ok": True, "sl": sl, "entry": entry, "tps_all": tp, "tps_used": tp[:3], "tags": tags}

CASES = [("VELVET", "7681008357750099146_0.png", 0.0886, [0.1052, 0.1246, 0.1571]),
         ("ZEC",    "7681659532879105241_0.png", 964.09, [990.21, 1023.24, 1067.10]),
         ("EIGEN",  "7680773389685935326_0.png", 0.1905, [0.2146, 0.2397, 0.266])]
tot = ok = 0
for name, fn, tsl, ttp in CASES:
    p = os.path.join(IMGDIR, fn)
    if not os.path.exists(p): print(name, "缺文件", flush=True); continue
    r = read(p)
    print("=" * 78, flush=True)
    print("%s  %s" % (name, fn), flush=True)
    if not r["ok"]:
        print("   ✗", r["why"], flush=True); continue
    print("   标签(值/覆盖率): %s" % [(t["text"], t["cov"]) for t in r["tags"]], flush=True)
    print("   → 止损 %s | 开仓 %s | 止盈 %s（图上实线共%d档，取前3）" % (r["sl"], r["entry"], r["tps_used"], len(r["tps_all"])), flush=True)
    print("   真值 止损 %s | 止盈 %s" % (tsl, ttp), flush=True)
    hit = sum(1 for t in ttp if any(abs(t - x) / t < 0.002 for x in r["tps_used"]))
    slok = abs(r["sl"] - tsl) / tsl < 0.002
    tot += 1
    if slok and hit == len(ttp): ok += 1
    print("   %s" % ("✅ 完全一致" if (slok and hit == len(ttp)) else "⚠️ 止损%s 止盈%d/%d" % ("一致" if slok else "不一致", hit, len(ttp))), flush=True)
print("=" * 78, flush=True)
print("回归结果: %d/%d 张图完全一致" % (ok, tot), flush=True)
