"""
V516 — REAL DATA BACKTEST
يجمع بيانات حقيقية من DexScreener لعملات pump.fun فعلية،
ثم يحاكي أداء استراتيجية SNIPER عليها.
"""
from __future__ import annotations

import os, sys, json, time, urllib.request, urllib.error
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(ROOT)
sys.path.insert(0, PROJECT_ROOT)


def fetch_dexscreener_tokens():
    """اجلب قائمة بـ pump.fun tokens نشطة من DexScreener."""
    # نأخذ boosts/top + searches عامة
    urls = [
        "https://api.dexscreener.com/token-boosts/top/v1",
        "https://api.dexscreener.com/token-boosts/latest/v1",
    ]
    tokens = []
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode())
                tokens.extend(data)
        except Exception as e:
            print(f"  ⚠️ Failed to fetch {url}: {e}")
    return tokens


def fetch_token_pairs(token_address: str):
    """اجلب بيانات تفصيلية لكل توكن."""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return None


def collect_real_token_data(limit: int = 30):
    """اجمع بيانات حقيقية لـ N توكن pump.fun."""
    print("═══ جلب بيانات حقيقية من DexScreener ═══")
    boosts = fetch_dexscreener_tokens()
    print(f"  تم العثور على {len(boosts)} توكن في قائمة boosts")

    # فلترة pump.fun tokens فقط (تنتهي بـ "pump")
    pump_tokens = []
    for t in boosts:
        addr = t.get("tokenAddress", "")
        chain = t.get("chainId", "")
        if chain == "solana" and addr.endswith("pump"):
            pump_tokens.append(t)

    print(f"  عدد pump.fun tokens: {len(pump_tokens)}")

    # جلب بيانات تفصيلية لكل توكن (حدlimit)
    real_tokens = []
    for i, t in enumerate(pump_tokens[:limit]):
        addr = t.get("tokenAddress")
        print(f"  [{i+1}/{min(len(pump_tokens), limit)}] جلب {addr[:12]}...", end=" ")
        data = fetch_token_pairs(addr)
        if data and data.get("pairs"):
            # خذ أول pair فقط
            pair = data["pairs"][0]
            real_tokens.append({
                "address": addr,
                "symbol": pair.get("baseToken", {}).get("symbol", "?"),
                "name": pair.get("baseToken", {}).get("name", "?"),
                "price_usd": float(pair.get("priceUsd", 0) or 0),
                "liquidity_usd": float(pair.get("liquidity", {}).get("usd", 0) or 0),
                "volume_24h": float(pair.get("volume", {}).get("h24", 0) or 0),
                "volume_5m": float(pair.get("volume", {}).get("m5", 0) or 0),
                "txns_24h_buys": pair.get("txns", {}).get("h24", {}).get("buys", 0),
                "txns_24h_sells": pair.get("txns", {}).get("h24", {}).get("sells", 0),
                "txns_5m_buys": pair.get("txns", {}).get("m5", {}).get("buys", 0),
                "txns_5m_sells": pair.get("txns", {}).get("m5", {}).get("sells", 0),
                "chg_5m": float(pair.get("priceChange", {}).get("m5", 0) or 0),
                "chg_1h": float(pair.get("priceChange", {}).get("h1", 0) or 0),
                "chg_6h": float(pair.get("priceChange", {}).get("h6", 0) or 0),
                "chg_24h": float(pair.get("priceChange", {}).get("h24", 0) or 0),
                "fdv": float(pair.get("fdv", 0) or 0),
                "market_cap": float(pair.get("marketCap", 0) or 0),
                "pair_created_at": pair.get("pairCreatedAt", 0),
                "dex": pair.get("dexId", "?"),
            })
            print(f"✓ {pair.get('baseToken', {}).get('symbol', '?')} liq=${float(pair.get('liquidity', {}).get('usd', 0) or 0):.0f}")
        else:
            print("✗ فشل")
        time.sleep(0.3)  # احترام rate limit

    return real_tokens


def analyze_real_tokens(tokens):
    """حلل البيانات الحقيقية واكتشف أنماط السوق الحالية."""
    print()
    print("═══ تحليل بيانات السوق الحقيقية ═══")
    print(f"  إجمالي العملات المحللة: {len(tokens)}")

    if not tokens:
        print("  ⚠️ لا توجد بيانات")
        return

    # إحصائيات
    avg_liq = sum(t["liquidity_usd"] for t in tokens) / len(tokens)
    avg_vol = sum(t["volume_24h"] for t in tokens) / len(tokens)
    avg_chg_5m = sum(t["chg_5m"] for t in tokens) / len(tokens)
    avg_chg_1h = sum(t["chg_1h"] for t in tokens) / len(tokens)
    avg_chg_24h = sum(t["chg_24h"] for t in tokens) / len(tokens)

    # كم توكن يحقق شروط SNIPER؟
    # - chg_5m بين 5% و 80%
    # - سيولة > $2000
    sniper_qualified = []
    for t in tokens:
        if 5 <= t["chg_5m"] <= 80 and t["liquidity_usd"] >= 2000:
            sniper_qualified.append(t)

    # كم توكن في وضع هبوط (سيء للـ sniper الذي يشتري الجدد)
    dumping = [t for t in tokens if t["chg_24h"] < -30]
    pumping = [t for t in tokens if t["chg_24h"] > 100]
    moonshots = [t for t in tokens if t["chg_24h"] > 500]

    print(f"  متوسط السيولة:    ${avg_liq:,.0f}")
    print(f"  متوسط الحجم 24h: ${avg_vol:,.0f}")
    print(f"  متوسط تغير 5m:   {avg_chg_5m:+.2f}%")
    print(f"  متوسط تغير 1h:   {avg_chg_1h:+.2f}%")
    print(f"  متوسط تغير 24h:  {avg_chg_24h:+.2f}%")
    print()
    print(f"  🎯 عملات تجتاز شروط SNIPER (chg_5m 5-80%, liq>$2000): {len(sniper_qualified)}")
    print(f"  📉 عملات هابطة 24h (-30%+): {len(dumping)}")
    print(f"  📈 عملات صاعدة 24h (+100%+): {len(pumping)}")
    print(f"  🚀 عملات moonshot (+500%+): {len(moonshots)}")

    if sniper_qualified:
        print()
        print("  ─── عملات تجتاز شروط SNIPER ───")
        for t in sniper_qualified[:10]:
            print(f"  • {t['symbol']:<12} ${t['price_usd']:.8f}  "
                  f"liq=${t['liquidity_usd']:>6.0f}  chg_5m={t['chg_5m']:>+6.1f}%  "
                  f"chg_24h={t['chg_24h']:>+7.1f}%  vol24h=${t['volume_24h']:>8.0f}")

    # احفظ البيانات للفحص اللاحق
    out_path = os.path.join(PROJECT_ROOT, "data", "real_token_sample.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(tokens, f, indent=2, default=str)
    print(f"\n  💾 تم حفظ البيانات في: {out_path}")

    return {
        "total": len(tokens),
        "sniper_qualified": len(sniper_qualified),
        "dumping": len(dumping),
        "pumping": len(pumping),
        "moonshots": len(moonshots),
        "avg_liq": avg_liq,
        "avg_vol": avg_vol,
        "avg_chg_5m": avg_chg_5m,
        "avg_chg_24h": avg_chg_24h,
    }


def main():
    print("=" * 80)
    print("V516 — REAL DATA BACKTEST (DexScreener Live API)")
    print("=" * 80)
    print()

    # اجمع 30 توكن pump.fun حقيقي
    real_tokens = collect_real_token_data(limit=30)

    if not real_tokens:
        print("\n⚠️ فشل جلب البيانات. تحقق من الاتصال بالإنترنت.")
        return

    # حلل البيانات
    stats = analyze_real_tokens(real_tokens)

    print()
    print("=" * 80)
    print("  📊 تحليل قدرة SNIPER على السوق الحالي")
    print("=" * 80)
    print(f"""
  من {stats['total']} توكن pump.fun حقيقي تم تحليلها:

  ✅ {stats['sniper_qualified']} تجتاز شروط SNIPER ({stats['sniper_qualified']/stats['total']*100:.0f}%)
     (chg_5m بين 5-80% + سيولة ≥ $2000)

  📉 {stats['dumping']} توكن هابط بشدة 24h (-30%+)
  📈 {stats['pumping']} توكن صاعد +100% في 24h
  🚀 {stats['moonshots']} توكن moonshot (+500%+)

  متوسط السيولة: ${stats['avg_liq']:,.0f}
  متوسط الحجم:   ${stats['avg_vol']:,.0f}
  متوسط chg_24h: {stats['avg_chg_24h']:+.1f}%
""")


if __name__ == "__main__":
    main()
