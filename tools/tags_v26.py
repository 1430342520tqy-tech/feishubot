import sys, os, json, base64, re, requests
from PIL import Image
KEY = os.environ.get("DEEPSEEK_API_KEY", "")
API = "https://api.deepseek.com/chat/completions"
TMP = "/home/ubuntu/signal-bot/v26"; os.makedirs(TMP, exist_ok=True)

def cls(r, g, b):
    mx, mn = max(r, g, b), min(r, g, b)
    if mx - mn < 40:
        return "white" if mx > 200 else ("grey" if mx > 110 else None)
    if r > 150 and g > 90 and b < 110 and r >= g: return "orange"
    if r > 150 and g < 100 and b < 110: return "red"
    if g > 110 and r < 130 and b < 170: return "green"
    return None

def raw_rects(path, x_lo=0.76, x_hi=0.985):
    im = Image.open(path).convert("RGB")
    w, h = im.size
    x0 = int(w * x_lo); x1 = int(w * x_hi)
    band = im.crop((x0, 0, x1, h)); bw, bh = band.size
    px = band.load()
    grid = [[cls(*px[x, y]) for x in range(bw)] for y in range(bh)]
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
    return im, x0, bw, bh, grid, groups

def merge_tags(groups, max_gap=32, min_w=55):
    out = []
    for g in sorted(groups, key=lambda g: g["y1"]):
        for t in out:
            if t["c"] == g["c"] and 0 <= g["y1"] - t["y2"] <= max_gap and not (g["x2"] < t["x1"] - 10 or g["x1"] > t["x2"] + 10):
                t["y2"] = g["y2"]; t["x1"] = min(t["x1"], g["x1"]); t["x2"] = max(t["x2"], g["x2"]); break
        else:
            out.append(dict(g))
    return [t for t in out if t["x2"] - t["x1"] + 1 >= min_w]

def ocr(im, x0, tag):
    box = (max(0, x0 + tag["x1"] - 4), max(0, tag["y1"] - 4), min(im.width, x0 + tag["x2"] + 5), min(im.height, tag["y2"] + 5))
    crop = im.crop(box)
    s = max(2, min(6, 700 // max(1, crop.height)))
    crop = crop.resize((crop.width * s, crop.height * s), Image.LANCZOS)
    p = TMP + "/t_%d.png" % tag["y1"]; crop.save(p)
    b64 = base64.b64encode(open(p, "rb").read()).decode()
    body = {"model": "deepseek-v4-flash-vision-exp", "temperature": 0,
            "messages": [{"role": "system", "content": "Transcribe the single price number printed in this image crop. STRICT JSON only."},
                         {"role": "user", "content": [{"type": "text", "text": "Return {\"text\":\"exact characters you see\",\"value\":number}"},
                          {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}]}
    try:
        r = requests.post(API, headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"}, json=body, timeout=180)
        j = r.json()
        m = re.search(r"\{[\s\S]*\}", j["choices"][0]["message"]["content"])
        return json.loads(m.group(0))
    except Exception as e:
        return {"error": str(e)[:50]}

f = sys.argv[1] if len(sys.argv) > 1 else None
im = None if f is None else None
if f:
    im, x0, bw, bh, grid, groups = raw_rects(f)
tags = merge_tags(groups)
print("%s  %s  raw_groups=%d merged_tags=%d" % (os.path.basename(f), im.size, len(groups), len(tags)), flush=True)
res = []
for t in sorted(tags, key=lambda t: t["y1"]):
    v = ocr(im, x0, t)
    res.append({"y1": t["y1"], "y2": t["y2"], "color": t["c"], "text": v.get("text"), "value": v.get("value")})
    print("   y=%4d-%4d %-6s -> %s" % (t["y1"], t["y2"], t["c"], json.dumps(v, ensure_ascii=False)), flush=True)
json.dump(res, open(TMP + "/" + os.path.basename(f) + ".json", "w"), ensure_ascii=False, indent=1)
