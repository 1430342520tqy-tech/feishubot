# -*- coding: utf-8 -*-
"""美股代币：休市 vs 盘中 成交额 + 盘口稳定性
目标：验证「休市时盘口薄到不能下单」这个说法是否成立于我们的下单规模(900U)。
"""
import json, time, urllib.request

BASE = "https://fapi.binance.com"
SYMS = ["SNDKUSDT", "SKHYUSDT", "SKHYNIXUSDT", "NVDAUSDT", "TSLAUSDT", "AAPLUSDT",
        "COINUSDT", "MSTRUSDT", "MUUSDT", "INTCUSDT", "CRCLUSDT", "HOODUSDT",
        "XAUUSDT", "CLUSDT", "SPYUSDT", "QQQUSDT", "MRVLUSDT", "WDCUSDT",
        "SOXLUSDT", "CRWVUSDT", "NOKUSDT", "EWYUSDT", "ARMUSDT", "SMCIUSDT",
        "BTCUSDT"]
NOTIONAL = 900.0  # 我们的单笔名义规模：300U保证金 x 3倍


def get(path, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{BASE}{path}" + (f"?{q}" if q else "")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e
            time.sleep(0.7)
    raise last


def main():
    now = int(time.time())
    et = time.gmtime(now - 4 * 3600)
    print(f"采样时刻 UTC {time.strftime('%Y-%m-%d %H:%M', time.gmtime(now))} / "
          f"ET {time.strftime('%Y-%m-%d %H:%M', et)}")
    print(f"我们的单笔名义规模 = {NOTIONAL:.0f} USDT")
    print()

    print("=== A. 成交额/分钟（最近1500根1mK线，区分美股盘中13:30-20:00 UTC）===")
    print(f"{'symbol':<13}{'盘中U/分':>13}{'盘外U/分':>13}{'盘外最低':>12}"
          f"{'900U占盘外':>12}{'盘中零成交':>11}{'盘外零成交':>11}")
    vol_rows = []
    for s in SYMS:
        try:
            kl = get("/fapi/v1/klines", symbol=s, interval="1m", limit=1500)
        except Exception as e:
            print(f"{s:<13} klines失败: {e}")
            continue
        vin, voff = [], []
        for k in kl:
            ts = k[0] // 1000
            g = time.gmtime(ts)
            mins = g.tm_hour * 60 + g.tm_min
            qv = float(k[7])  # quote asset volume
            (vin if (13 * 60 + 30) <= mins < 20 * 60 else voff).append(qv)
        ai = sum(vin) / len(vin) if vin else 0
        ao = sum(voff) / len(voff) if voff else 0
        mn = min(voff) if voff else 0
        share = (NOTIONAL / mn * 100) if mn > 0 else -1
        zi = sum(1 for x in vin if x == 0)
        zo = sum(1 for x in voff if x == 0)
        vol_rows.append((s, ai, ao, mn, share, zi, zo))
        print(f"{s:<13}{ai:>12,.0f}{ao:>12,.0f}{mn:>11,.0f}"
              f"{(f'{share:.2f}%' if share >= 0 else 'n/a'):>12}"
              f"{zi:>11}{zo:>11}")
        time.sleep(0.25)

    print()
    print("=== B. 盘口稳定性：连续 4 次采样（间隔 4 秒，当前休市）===")
    print(f"{'symbol':<13}{'spread%':>26}{'±0.5%深度U':>34}")
    for s in SYMS:
        cells_s, cells_d = [], []
        for i in range(4):
            try:
                ob = get("/fapi/v1/depth", symbol=s, limit=100)
                bid = float(ob["bids"][0][0]); ask = float(ob["asks"][0][0])
                sp = (ask - bid) / ((ask + bid) / 2) * 100
                mid = (ask + bid) / 2
                lo, hi = mid * 0.995, mid * 1.005
                d = sum(float(p) * float(q) for p, q in ob["bids"] if float(p) >= lo)
                d += sum(float(p) * float(q) for p, q in ob["asks"] if float(p) <= hi)
                cells_s.append(f"{sp:.5f}")
                cells_d.append(d)
            except Exception:
                cells_s.append("ERR"); cells_d.append(0)
            if i < 3:
                time.sleep(4)
        ds = "/".join(f"{x/1000:.0f}k" for x in cells_d)
        print(f"{s:<13}{'/'.join(cells_s):>26}{ds:>34}")

    print()
    print("=== C. 结论口径 ===")
    print("1) '900U占盘外' = 我们一笔单 / 休市时段最小单分钟成交额 → 越小越安全")
    print("2) '盘外零成交' = 休市1500分钟里完全没有成交的分钟数 → 0 表示盘口一直有成交")
    print("3) B 段 4 次采样 spread/深度稳定 = 不是瞬时偶然")


if __name__ == "__main__":
    main()
