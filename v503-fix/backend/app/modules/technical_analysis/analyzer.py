"""
Advanced technical analysis engine.

Computes the FULL set of indicators specified in the project brief:
- Trend: EMA(9,20,50,100,200), SMA(20,50,200), ADX
- Momentum: RSI, MACD, Stochastic RSI
- Volatility: Bollinger Bands, ATR
- Volume: OBV, Volume Profile, VWAP
- Levels: Support/Resistance, Fibonacci
- Patterns: Candlestick, Price Action (Breakout / Fake Breakout / Reversal)

All math is implemented from scratch (numpy/pandas) so the system has
ZERO hidden third-party dependencies and full transparency.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# =========================================================================
# Indicator functions (pure, no side effects)
# =========================================================================
def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger(close: pd.Series, period: int = 20, std_mult: float = 2.0) -> Tuple[pd.Series, pd.Series, pd.Series]:
    middle = sma(close, period)
    std = close.rolling(window=period, min_periods=period).std()
    upper = middle + std_mult * std
    lower = middle - std_mult * std
    return upper, middle, lower


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    # +DM / -DM
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    plus_dm = pd.Series(plus_dm, index=high.index)
    minus_dm = pd.Series(minus_dm, index=high.index)
    tr = atr(high, low, close, period=1)
    atr_ = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def stochastic_rsi(close: pd.Series, rsi_period: int = 14, stoch_period: int = 14) -> Tuple[pd.Series, pd.Series]:
    r = rsi(close, rsi_period)
    r_min = r.rolling(window=stoch_period, min_periods=stoch_period).min()
    r_max = r.rolling(window=stoch_period, min_periods=stoch_period).max()
    stoch = (r - r_min) / (r_max - r_min).replace(0, np.nan) * 100
    stoch = stoch.fillna(50)
    k = stoch.rolling(window=3, min_periods=1).mean()
    d = k.rolling(window=3, min_periods=1).mean()
    return k, d


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = close.diff().fillna(0).apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    return (direction * volume).cumsum()


def vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, window: int = 20) -> pd.Series:
    typical_price = (high + low + close) / 3
    return (typical_price * volume).rolling(window=window, min_periods=1).sum() / \
           volume.rolling(window=window, min_periods=1).sum().replace(0, np.nan)


def volume_profile(close: pd.Series, volume: pd.Series, bins: int = 20) -> Dict[str, float]:
    """Return POC (point of control) and value-area high/low."""
    if len(close) < bins:
        return {"poc": float(close.iloc[-1]), "vah": float(close.iloc[-1]), "val": float(close.iloc[-1])}
    hist, edges = np.histogram(close, bins=bins, weights=volume)
    poc_idx = int(np.argmax(hist))
    poc = float((edges[poc_idx] + edges[poc_idx + 1]) / 2)
    total = hist.sum()
    # Find value area (70% of volume around POC)
    cum = 0
    lo, hi = poc_idx, poc_idx
    while cum < 0.70 * total and (lo > 0 or hi < bins - 1):
        if lo > 0 and (hi >= bins - 1 or hist[lo - 1] >= hist[hi + 1]):
            lo -= 1
            cum += hist[lo]
        elif hi < bins - 1:
            hi += 1
            cum += hist[hi]
    return {
        "poc": poc,
        "vah": float(edges[hi + 1]),
        "val": float(edges[lo]),
    }


def support_resistance(close: pd.Series, lookback: int = 50) -> Dict[str, float]:
    """Find nearest support and resistance from swing highs/lows."""
    window = close.iloc[-lookback:] if len(close) >= lookback else close
    # Local maxima / minima using simple comparison
    highs = []
    lows = []
    arr = window.values
    for i in range(1, len(arr) - 1):
        if arr[i] > arr[i - 1] and arr[i] > arr[i + 1]:
            highs.append(arr[i])
        if arr[i] < arr[i - 1] and arr[i] < arr[i + 1]:
            lows.append(arr[i])

    last_price = float(close.iloc[-1])
    resistances = [h for h in highs if h > last_price]
    supports = [l for l in lows if l < last_price]

    return {
        "nearest_resistance": min(resistances) if resistances else last_price * 1.10,
        "nearest_support": max(supports) if supports else last_price * 0.90,
        "all_resistances": sorted(resistances)[:5],
        "all_supports": sorted(supports, reverse=True)[:5],
    }


def fibonacci_levels(close: pd.Series, lookback: int = 100) -> Dict[str, float]:
    """Compute Fib retracement levels from the recent swing."""
    window = close.iloc[-lookback:] if len(close) >= lookback else close
    high = float(window.max())
    low = float(window.min())
    diff = high - low
    return {
        "0.0": high,
        "0.236": high - 0.236 * diff,
        "0.382": high - 0.382 * diff,
        "0.5": high - 0.5 * diff,
        "0.618": high - 0.618 * diff,
        "0.786": high - 0.786 * diff,
        "1.0": low,
    }


# =========================================================================
# Candlestick & pattern detection
# =========================================================================
def detect_candlestick_patterns(df: pd.DataFrame) -> List[str]:
    """Detect common candlestick patterns in the last few candles."""
    patterns = []
    if len(df) < 3:
        return patterns

    o = df["open"].iloc[-1]
    h = df["high"].iloc[-1]
    l = df["low"].iloc[-1]
    c = df["close"].iloc[-1]
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    range_ = max(h - l, 1e-9)

    prev_c = df["close"].iloc[-2]
    prev_o = df["open"].iloc[-2]

    # Hammer / Hanging man
    if lower_wick > 2 * body and upper_wick < 0.1 * range_:
        patterns.append("hammer" if c > prev_c else "hanging_man")
    # Shooting star
    if upper_wick > 2 * body and lower_wick < 0.1 * range_:
        patterns.append("shooting_star")
    # Doji
    if body < 0.05 * range_:
        patterns.append("doji")
    # Bullish engulfing
    if prev_c < prev_o and c > o and c > prev_o and o < prev_c:
        patterns.append("bullish_engulfing")
    # Bearish engulfing
    if prev_c > prev_o and c < o and c < prev_o and o > prev_c:
        patterns.append("bearish_engulfing")

    return patterns


def detect_price_action(df: pd.DataFrame, lookback: int = 20) -> Dict[str, bool | str]:
    """Detect breakout / fake breakout / trend reversal."""
    result = {
        "breakout_up": False,
        "breakout_down": False,
        "fake_breakout_up": False,
        "fake_breakout_down": False,
        "trend_reversal": False,
        "trend": "neutral",
        "trend_strength": 0.0,
        "momentum_weakening": False,
    }
    if len(df) < lookback:
        return result

    window = df.iloc[-lookback:]
    close = df["close"]
    last = float(close.iloc[-1])
    recent_high = float(window["high"].iloc[:-1].max())
    recent_low = float(window["low"].iloc[:-1].min())

    # Breakout
    if last > recent_high:
        result["breakout_up"] = True
        # Check if it's a fake: closed back below within 3 candles
        recent_3 = df["close"].iloc[-4:-1]
        if (recent_3 > recent_high).any() and last < recent_high:
            result["fake_breakout_up"] = True
    elif last < recent_low:
        result["breakout_down"] = True
        recent_3 = df["close"].iloc[-4:-1]
        if (recent_3 < recent_low).any() and last > recent_low:
            result["fake_breakout_down"] = True

    # Trend via EMA alignment
    ema9 = ema(close, 9).iloc[-1]
    ema20 = ema(close, 20).iloc[-1]
    ema50 = ema(close, 50).iloc[-1] if len(close) >= 50 else ema20

    if ema9 > ema20 > ema50:
        result["trend"] = "bullish"
        result["trend_strength"] = min((ema9 - ema50) / max(ema50, 1e-9), 1.0)
    elif ema9 < ema20 < ema50:
        result["trend"] = "bearish"
        result["trend_strength"] = min((ema50 - ema9) / max(ema50, 1e-9), 1.0)

    # Momentum weakening (RSI divergence proxy)
    if len(close) >= 30:
        r = rsi(close, 14).iloc[-1]
        r_prev = rsi(close, 14).iloc[-15]
        if result["trend"] == "bullish" and r < r_prev - 5:
            result["momentum_weakening"] = True
        if result["trend"] == "bearish" and r > r_prev + 5:
            result["momentum_weakening"] = True

    # Trend reversal
    if result["trend"] == "bullish" and last < ema20:
        result["trend_reversal"] = True
    if result["trend"] == "bearish" and last > ema20:
        result["trend_reversal"] = True

    return result


def detect_accumulation_distribution(df: pd.DataFrame, lookback: int = 30) -> Dict[str, str]:
    """Detect accumulation / distribution zones."""
    if len(df) < lookback:
        return {"phase": "unknown"}

    window = df.iloc[-lookback:]
    price_range = (window["high"].max() - window["low"].min()) / max(window["close"].mean(), 1e-9)
    vol_mean = window["volume"].mean()
    last_vol = df["volume"].iloc[-1]

    # Tight price range + rising volume = accumulation
    if price_range < 0.10 and last_vol > vol_mean * 1.2:
        return {"phase": "accumulation", "range_pct": price_range}
    # Wide range + high volume on red candles = distribution
    if price_range > 0.20 and df["close"].iloc[-1] < df["open"].iloc[-1] and last_vol > vol_mean * 1.3:
        return {"phase": "distribution", "range_pct": price_range}
    return {"phase": "neutral", "range_pct": price_range}


# =========================================================================
# Master result structure
# =========================================================================
@dataclass
class TechnicalAnalysisResult:
    # Indicator values (latest)
    rsi: float = 50.0
    macd_line: float = 0.0
    macd_signal: float = 0.0
    macd_hist: float = 0.0
    ema_9: float = 0.0
    ema_20: float = 0.0
    ema_50: float = 0.0
    ema_100: float = 0.0
    ema_200: float = 0.0
    sma_20: float = 0.0
    sma_50: float = 0.0
    sma_200: float = 0.0
    bb_upper: float = 0.0
    bb_middle: float = 0.0
    bb_lower: float = 0.0
    vwap: float = 0.0
    atr: float = 0.0
    adx: float = 0.0
    stoch_rsi_k: float = 50.0
    stoch_rsi_d: float = 50.0
    obv: float = 0.0
    volume_profile_poc: float = 0.0
    volume_profile_vah: float = 0.0
    volume_profile_val: float = 0.0

    # Levels
    nearest_support: float = 0.0
    nearest_resistance: float = 0.0
    fib_levels: Dict[str, float] = field(default_factory=dict)

    # Patterns & action
    candlestick_patterns: List[str] = field(default_factory=list)
    price_action: Dict = field(default_factory=dict)
    accumulation_phase: str = "neutral"
    volatility_pct: float = 0.0

    # Composite score (0-100, higher = more bullish)
    technical_score: float = 50.0
    score_reasons: List[str] = field(default_factory=list)
    data_sufficient: bool = False

    def to_dict(self) -> Dict:
        return asdict(self)


# =========================================================================
# Main analyzer
# =========================================================================
class TechnicalAnalyzer:
    """Computes the full indicator suite and a composite technical score."""

    MIN_CANDLES = 30
    IDEAL_CANDLES = 200

    def analyze(self, df: pd.DataFrame) -> TechnicalAnalysisResult:
        """Compute the full indicator set on a candle DataFrame."""
        result = TechnicalAnalysisResult()

        if df is None or len(df) < self.MIN_CANDLES:
            result.data_sufficient = False
            result.score_reasons.append("عدد الشموع غير كافٍ للتحليل الفني")
            return result

        result.data_sufficient = True
        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)
        open_ = df["open"].astype(float)

        # --- Indicators ---
        result.rsi = float(rsi(close, 14).iloc[-1])
        macd_l, macd_s, macd_h = macd(close)
        result.macd_line = float(macd_l.iloc[-1])
        result.macd_signal = float(macd_s.iloc[-1])
        result.macd_hist = float(macd_h.iloc[-1])

        result.ema_9 = float(ema(close, 9).iloc[-1])
        result.ema_20 = float(ema(close, 20).iloc[-1])
        result.ema_50 = float(ema(close, 50).iloc[-1]) if len(close) >= 50 else result.ema_20
        result.ema_100 = float(ema(close, 100).iloc[-1]) if len(close) >= 100 else result.ema_50
        result.ema_200 = float(ema(close, 200).iloc[-1]) if len(close) >= 200 else result.ema_100

        result.sma_20 = float(sma(close, 20).iloc[-1])
        result.sma_50 = float(sma(close, 50).iloc[-1]) if len(close) >= 50 else result.sma_20
        result.sma_200 = float(sma(close, 200).iloc[-1]) if len(close) >= 200 else result.sma_50

        bb_u, bb_m, bb_l = bollinger(close)
        result.bb_upper = float(bb_u.iloc[-1])
        result.bb_middle = float(bb_m.iloc[-1])
        result.bb_lower = float(bb_l.iloc[-1])

        result.vwap = float(vwap(high, low, close, volume).iloc[-1])
        result.atr = float(atr(high, low, close).iloc[-1])
        result.adx = float(adx(high, low, close).iloc[-1]) if len(close) >= 28 else 0.0

        k, d = stochastic_rsi(close)
        result.stoch_rsi_k = float(k.iloc[-1])
        result.stoch_rsi_d = float(d.iloc[-1])

        result.obv = float(obv(close, volume).iloc[-1])

        vp = volume_profile(close, volume)
        result.volume_profile_poc = vp["poc"]
        result.volume_profile_vah = vp["vah"]
        result.volume_profile_val = vp["val"]

        sr = support_resistance(close)
        result.nearest_support = sr["nearest_support"]
        result.nearest_resistance = sr["nearest_resistance"]

        result.fib_levels = fibonacci_levels(close)

        # --- Patterns ---
        result.candlestick_patterns = detect_candlestick_patterns(df)
        result.price_action = detect_price_action(df)
        result.accumulation_phase = detect_accumulation_distribution(df).get("phase", "neutral")

        last_price = float(close.iloc[-1])
        result.volatility_pct = float(result.atr / max(last_price, 1e-9) * 100)

        # --- Composite score ---
        self._compute_score(result, close)
        return result

    def _compute_score(self, result: TechnicalAnalysisResult, close: pd.Series) -> None:
        """Compute a composite technical score 0-100."""
        score = 50.0
        reasons: List[str] = []
        last_price = float(close.iloc[-1])

        # RSI (oversold bullish / overbought bearish)
        if result.rsi < 30:
            score += 12
            reasons.append(f"RSI منخفض ({result.rsi:.1f}) - تشبع بيعي")
        elif result.rsi > 70:
            score -= 8
            reasons.append(f"RSI مرتفع ({result.rsi:.1f}) - تشبع شرائي")
        elif 45 <= result.rsi <= 60:
            score += 4
            reasons.append("RSI في منطقة محايدة صحية")

        # MACD
        if result.macd_hist > 0 and result.macd_line > result.macd_signal:
            score += 10
            reasons.append("MACD إيجابي - تقاطع صاعد")
        elif result.macd_hist < 0 and result.macd_line < result.macd_signal:
            score -= 10
            reasons.append("MACD سلبي - تقاطع هابط")

        # EMA alignment
        if result.ema_9 > result.ema_20 > result.ema_50:
            score += 12
            reasons.append("ترتيب EMA صاعد قوي (9>20>50)")
        elif result.ema_9 < result.ema_20 < result.ema_50:
            score -= 12
            reasons.append("ترتيب EMA هابط (9<20<50)")

        # Price vs EMA50
        if last_price > result.ema_50:
            score += 5
        else:
            score -= 5

        # Price vs EMA200 (if available)
        if result.ema_200 > 0:
            if last_price > result.ema_200:
                score += 8
                reasons.append("السعر فوق EMA200 - اتجاه صاعد طويل الأمد")
            else:
                score -= 6
                reasons.append("السعر تحت EMA200 - ضغط بيعي طويل الأمد")

        # Bollinger position
        bb_pos = (last_price - result.bb_lower) / max(result.bb_upper - result.bb_lower, 1e-9)
        if bb_pos < 0.1:
            score += 6
            reasons.append("السعر قرب الحد السفلي لبولنجر - ارتداد محتمل")
        elif bb_pos > 0.9:
            score -= 4
            reasons.append("السعر قرب الحد العلوي لبولنجر - تصحيح محتمل")

        # VWAP
        if last_price > result.vwap:
            score += 4
            reasons.append("السعر فوق VWAP - إيجابي")
        else:
            score -= 4

        # ADX strength
        if result.adx > 25:
            if result.price_action.get("trend") == "bullish":
                score += 8
                reasons.append(f"ADX قوي ({result.adx:.1f}) مع اتجاه صاعد")
            elif result.price_action.get("trend") == "bearish":
                score -= 6
                reasons.append(f"ADX قوي ({result.adx:.1f}) مع اتجاه هابط")
        else:
            reasons.append(f"ADX ضعيف ({result.adx:.1f}) - سوق متذبذب")

        # Stochastic RSI
        if result.stoch_rsi_k < 20 and result.stoch_rsi_k > result.stoch_rsi_d:
            score += 6
            reasons.append("Stochastic RSI في تشبع بيعي مع تقاطع صاعد")
        elif result.stoch_rsi_k > 80 and result.stoch_rsi_k < result.stoch_rsi_d:
            score -= 4

        # Patterns
        bull_patterns = {"hammer", "bullish_engulfing"}
        bear_patterns = {"shooting_star", "hanging_man", "bearish_engulfing"}
        for p in result.candlestick_patterns:
            if p in bull_patterns:
                score += 5
                reasons.append(f"نمط شموع صاعد: {p}")
            elif p in bear_patterns:
                score -= 5
                reasons.append(f"نمط شموع هابط: {p}")

        # Price action
        pa = result.price_action
        if pa.get("breakout_up") and not pa.get("fake_breakout_up"):
            score += 10
            reasons.append("اختراق صحي للأعلى")
        if pa.get("fake_breakout_up"):
            score -= 12
            reasons.append("اختراق وهمي للأعلى - فخ صعودي")
        if pa.get("breakout_down") and not pa.get("fake_breakout_down"):
            score -= 8
            reasons.append("اختراق للأسفل")
        if pa.get("trend_reversal"):
            score += 6 if pa.get("trend") == "bullish" else -6
            reasons.append("انعكاس محتمل للاتجاه")
        if pa.get("momentum_weakening"):
            score -= 5
            reasons.append("ضعف في الزخم")

        # Accumulation / Distribution
        if result.accumulation_phase == "accumulation":
            score += 8
            reasons.append("مرحلة تجميع - استعداد لصعود")
        elif result.accumulation_phase == "distribution":
            score -= 8
            reasons.append("مرحلة تصريف - استعداد لهبوط")

        # Volume profile POC
        if last_price > result.volume_profile_poc:
            score += 3

        result.technical_score = max(0, min(100, score))
        result.score_reasons = reasons
