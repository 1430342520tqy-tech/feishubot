# -*- coding: utf-8 -*-
"""美股代币永续：休市 vs 开盘 波动/跳空实测
关键问题：休市时段能不能成交？开盘会不会跳空把止损打穿？
"""
import json, time, urllib.request

BASE = "https://fapi.binance.com"

STOCKS = ["AAPL","TSLA","NVDA","MSFT","GOOGL","AMZN","META","COIN","MSTR","HOOD",
          "PLTR","AMD","NFLX","SNDK","SKHY","SKHYNIX","MU","INTC","BABA","PDD",
          "AVGO","QCOM","SMCI","ARM","CRCL","GME","SPY","QQQ","SQQQ","TQQQ",
          "MRVL","SOXL","EWY","NOK","CRWV","WDC","XAU","XAG","CL","NATGAS"]

def get(path, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{BASE}{path}" + (f"?{q}" if q else "")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            err = e
            time.sleep(0.6)
    raise err

def main():
    info = get("/fapi/v1/exchangeInfo")
    syms = {s["symbol"] for s in info["symbols"]
            if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}

    found = []
    for b in STOCKS:
        for cand in (f"{b}USDT", f"{b}USDT.P"):
            if cand in syms:
                found.append(cand)
                break
    print("== 币安USDT-M 上的美股/商品代币 ==")
    print(json.dumps(found, ensure_ascii=False))
    print()

    # 当前时间 -> 美东
    now = int(time.time())
    # 2026-09 夏令时 EDT = UTC-4
    et_h = (time.gmtime(now).tm_hour - 4) % 24
    et_wd = time.gmtime(now).tm_wday  # 0=Mon
    print(f"now_utc={time.strftime('%Y-%m-%d %H:%M', time.gmtime(now))} "
          f"ET≈{et_wd}/{'%02d' % et_h}:{'%02d' % time.gmtime(now).tm_min} "
          f"(周中13:30-20:00 UTC = 美股盘中)")
    print()

    # 1m K线，取最近 5 天，统计：
    #  a) 美股盘中(13:30-20:00 UTC) 1分钟平均振幅
    #  b) 盘外 1分钟平均振幅
    #  c) 开盘首分钟(13:30 UTC) 相对前一分钟收盘的跳空
    rows = []
    for sym in found:
        try:
            kl = get("/fapi/v1/klines", symbol=sym, interval="1m", limit=1500)
        except Exception as e:
            print(f"{sym}: klines失败 {e}")
            continue
        in_rng, off_rng, gaps_in = [], [], []
        prev_close = None
        for k in kl:
            ts = k[0] // 1000
            h, m = time.gmtime(ts).tm_hour, time.gmtime(ts).tm_min
            o, hh, ll, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
            vol = float(k[5])
            if c <= 0 or o <= 0:
                continue
            rng = (hh - ll) / c * 100
            mins = h * 60 + m
            is_market = (13 * 60 + 30) <= mins < (20 * 60)
            if vol > 0:
                (in_rng if is_market else off_rng).append(rng)
            # 开盘跳空：13:30 那根的开盘 vs 上一根收盘
            if mins == 13 * 60 + 30 and prev_close:
                gaps_in.append(abs(o - prev_close) / prev_close * 100)
            prev_close = c if vol > 0 else prev_close
        rows.append({
            "sym": sym,
            "n_in": len(in_rng),
            "n_off": len(off_rng),
            "avg_in": sum(in_rng) / len(in_rng) if in_rng else 0,
            "avg_off": sum(off_rng) / len(off_rng) if off_rng else 0,
            "max_off": max(off_rng) if off_rng else 0,
            "gap_n": len(gaps_in),
            "gap_max": max(gaps_in) if gaps_in else 0,
            "gap_avg": sum(gaps_in) / len(gaps_in) if gaps_in else 0,
        })
        time.sleep(0.25)

    print(f"{'symbol':<12}{'盘中/分':>9}{'盘外/分':>9}{'盘外最大':>10}"
          f"{'开盘跳空均':>12}{'开盘跳空最大':>13}")
    for r in sorted(rows, key=lambda x: -x["avg_in"]):
        print(f"{r['sym']:<12}{r['avg_in']:>8.4f}%{r['avg_off']:>8.4f}%"
              f"{r['max_off']:>9.3f}%{r['gap_avg']:>11.4f}%{r['gap_max']:>12.4f}%")

    print()
    print("说明：'开盘跳空' = 13:30 UTC 那根1分钟K线的开盘价 vs 前一根收盘价的偏离；")
    print("      反映休市到开盘瞬间价格是否会直接跳过我们的止损/止盈挂单位。")

if __name__ == "__main__":
    main()
