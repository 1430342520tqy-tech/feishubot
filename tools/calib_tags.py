"""Calibrate: find the colour-tag rectangles at the right edge of a TradingView chart."""
import sys, os
from collections import Counter, defaultdict
from PIL import Image

def classify(r, g, b):
    mx, mn = max(r, g, b), min(r, g, b)
    sat = mx - mn
    if sat < 40:
        if mx > 200: return "white"
        if mx > 120: return "grey"
        return None
    if r > 150 and g > 90 and b < 110 and r >= g: return "orange"
    if r > 150 and g < 100 and b < 110: return "red"
    if g > 110 and r < 130 and b < 170: return "green"
    if b > 150 and r < 130: return "blue"
    return None

def main(path, frac=0.15):
    im = Image.open(path).convert("RGB")
    w, h = im.size
    x0 = int(w * (1 - frac))
    band = im.crop((x0, 0, w, h))
    bw, bh = band.size
    px = band.load()
    # column-wise: a tag is a wide horizontal run of one colour
    rows = defaultdict(list)
    for y in range(bh):
        run = None
        for x in range(bw):
            c = classify(*px[x, y])
            if c in ("orange", "red", "green", "white", "grey", "blue"):
                if run is None or run[0] != c:
                    if run and run[1] > 8:
                        rows[y].append((run[0], run[1], run[2]))
                    run = (c, 1, x)
                else:
                    run = (c, run[1] + 1, run[2])
            else:
                if run and run[1] > 8:
                    rows[y].append((run[0], run[1], run[2]))
                run = None
        if run and run[1] > 8:
            rows[y].append((run[0], run[1], run[2]))
    # collect wide runs (tag bodies)
    hits = []
    for y, runs in rows.items():
        for (c, ln, xs) in runs:
            if ln >= 25:
                hits.append((y, c, ln, xs))
    print("image %s  size=%dx%d  wide-colour-runs=%d" % (os.path.basename(path), w, h, len(hits)))
    # group consecutive rows with same colour into rectangles
    groups = []
    for y, c, ln, xs in sorted(hits):
        placed = False
        for g in groups:
            if g["c"] == c and y - g["y2"] <= 2 and abs(g["x1"] - xs) < 60:
                g["y2"] = y; g["n"] += 1
                g["x1"] = min(g["x1"], xs); g["x2"] = max(g["x2"], xs + ln)
                placed = True; break
        if not placed:
            groups.append({"c": c, "y1": y, "y2": y, "x1": xs, "x2": xs + ln, "n": 1})
    groups = [g for g in groups if g["n"] >= 8]
    print("candidate tags: %d" % len(groups))
    for g in groups:
        cx = (g["x1"] + g["x2"]) // 2 + x0
        cy = (g["y1"] + g["y2"]) // 2
        rgb = px[min(bw - 1, g["x2"] - g["x1"] - 3 if g["x2"] - g["x1"] > 6 else 1), cy]
        print("   %-7s y=%4d..%4d (h=%2d) x=%4d..%4d  sampleRGB=%s" %
              (g["c"], g["y1"], g["y2"], g["y2"] - g["y1"] + 1, g["x1"] + x0, g["x2"] + x0, rgb))

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "chart.png")
