import logging
import time
from decimal import Decimal
from typing import List, Optional, Tuple

from coin_rising_short import client, config

logger = logging.getLogger(__name__)

_RSI_PERIOD = 14

_kline_cache: dict[str, tuple[float, List[Decimal]]] = {}


def _mean(values: List[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return sum(values) / Decimal(len(values))


def _wilder_rsi_series(closes: List[Decimal]) -> List[Optional[Decimal]]:
    n = len(closes)
    out: List[Optional[Decimal]] = [None] * n
    if n < _RSI_PERIOD + 1:
        return out

    gains: List[Decimal] = []
    losses: List[Decimal] = []
    for i in range(1, n):
        ch = closes[i] - closes[i - 1]
        gains.append(ch if ch > 0 else Decimal("0"))
        losses.append(-ch if ch < 0 else Decimal("0"))

    period = Decimal(str(_RSI_PERIOD))
    avg_gain = sum(gains[0:_RSI_PERIOD]) / period
    avg_loss = sum(losses[0:_RSI_PERIOD]) / period

    def rsi_from_avgs(ag: Decimal, al: Decimal) -> Decimal:
        if al == 0:
            return Decimal("100") if ag > 0 else Decimal("50")
        rs = ag / al
        return Decimal("100") - (Decimal("100") / (Decimal("1") + rs))

    out[_RSI_PERIOD] = rsi_from_avgs(avg_gain, avg_loss)
    pm1 = period - Decimal("1")

    for j in range(_RSI_PERIOD, n - 1):
        g = gains[j]
        l_ = losses[j]
        avg_gain = (avg_gain * pm1 + g) / period
        avg_loss = (avg_loss * pm1 + l_) / period
        out[j + 1] = rsi_from_avgs(avg_gain, avg_loss)

    return out


def _ma20_gap_pct_series(closes: List[Decimal]) -> List[Optional[Decimal]]:
    n = len(closes)
    out: List[Optional[Decimal]] = [None] * n
    for i in range(19, n):
        window = closes[i - 19 : i + 1]
        ma20 = sum(window) / Decimal("20")
        if ma20 <= 0:
            out[i] = None
        else:
            out[i] = (closes[i] - ma20) / ma20 * Decimal("100")
    return out


def _ma5_slope_turns_down(closes: List[Decimal]) -> bool:
    """최신 닫힌 봉 기준: slope_prev > 0 and slope_now < 0."""
    L = len(closes) - 1
    if L < 14:
        return False
    ma5_now = _mean(closes[L - 4 : L + 1])
    ma5_prev = _mean(closes[L - 9 : L - 4])
    ma5_prev2 = _mean(closes[L - 14 : L - 9])
    slope_prev = ma5_prev - ma5_prev2
    slope_now = ma5_now - ma5_prev
    return slope_prev > 0 and slope_now < 0


def _get_closed_closes(symbol: str) -> Tuple[Optional[List[Decimal]], str]:
    now = time.time()
    cached = _kline_cache.get(symbol)
    if cached is not None and now - cached[0] < config.INDICATOR_CACHE_TTL_SEC:
        return cached[1], ""

    try:
        data = client.get_klines_binance_shape(symbol)
    except Exception as exc:
        msg = f"kline API/파싱 실패: {exc}"
        logger.warning("%s symbol=%s", msg, symbol, extra={"event": "indicator_kline_failed", "symbol": symbol})
        return None, msg

    if not isinstance(data, list):
        msg = "kline 응답이 list가 아님"
        logger.warning("%s symbol=%s type=%s", msg, symbol, type(data), extra={"event": "indicator_kline_bad_type", "symbol": symbol})
        return None, msg

    rows = data[:-1]
    closes: List[Decimal] = []
    try:
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 5:
                continue
            closes.append(Decimal(str(row[4])))
    except Exception as exc:
        msg = f"지표 계산 실패: close 변환 오류: {exc}"
        logger.warning("%s symbol=%s", msg, symbol, extra={"event": "indicator_close_parse_failed", "symbol": symbol})
        return None, msg

    if not closes:
        msg = "닫힌 캔들이 없음"
        logger.warning("%s symbol=%s", msg, symbol, extra={"event": "indicator_no_closed_candles", "symbol": symbol})
        return None, msg

    _kline_cache[symbol] = (now, closes)
    return closes, ""


def allow_initial_short(symbol: str) -> Tuple[bool, str]:
    try:
        closes, err = _get_closed_closes(symbol)
        if closes is None:
            return False, err or "지표 계산 실패: kline 없음"

        if len(closes) < 20:
            msg = f"캔들 개수 부족: {len(closes)} < 20"
            logger.warning("%s symbol=%s", msg, symbol, extra={"event": "indicator_insufficient_bars", "symbol": symbol})
            return False, msg

        rsi_s = _wilder_rsi_series(closes)
        gap_s = _ma20_gap_pct_series(closes)
        L = len(closes) - 1
        rsi = rsi_s[L]
        gap = gap_s[L]
        if rsi is None or gap is None:
            msg = "RSI 또는 MA20 이격률 계산 불가"
            logger.warning("%s symbol=%s", msg, symbol, extra={"event": "indicator_series_none", "symbol": symbol})
            return False, msg

        if rsi < config.ENTRY_RSI_THRESHOLD:
            return False, f"RSI 미달: {rsi} < {config.ENTRY_RSI_THRESHOLD}"
        if gap < config.ENTRY_MA20_GAP_PCT:
            return False, f"MA20 이격률 미달: {gap}% < {config.ENTRY_MA20_GAP_PCT}%"

        return True, "ok"
    except Exception as exc:
        msg = f"지표 계산 실패: {exc}"
        logger.warning("%s symbol=%s", msg, symbol, extra={"event": "indicator_initial_exception", "symbol": symbol})
        return False, msg


def allow_reentry_short(symbol: str) -> Tuple[bool, str]:
    try:
        closes, err = _get_closed_closes(symbol)
        if closes is None:
            return False, err or "지표 계산 실패: kline 없음"

        n_bars = config.REENTRY_RECENT_OVER_BARS
        min_len = 19 + n_bars
        if len(closes) < min_len:
            msg = f"캔들 개수 부족: {len(closes)} < {min_len}"
            logger.warning("%s symbol=%s", msg, symbol, extra={"event": "indicator_insufficient_bars_reentry", "symbol": symbol})
            return False, msg

        rsi_s = _wilder_rsi_series(closes)
        gap_s = _ma20_gap_pct_series(closes)
        L = len(closes) - 1
        start = L - (n_bars - 1)
        overheated = False
        for idx in range(start, L + 1):
            r = rsi_s[idx]
            g = gap_s[idx]
            if r is None or g is None:
                continue
            if r >= config.REENTRY_RSI_THRESHOLD and g >= config.REENTRY_MA20_GAP_PCT:
                overheated = True
                break

        if not overheated:
            return False, "최근 과열 구간 없음(RSI/MA20 이격)"

        if not _ma5_slope_turns_down(closes):
            return False, "MA5 기울기 하락 전환 없음"

        return True, "ok"
    except Exception as exc:
        msg = f"지표 계산 실패: {exc}"
        logger.warning("%s symbol=%s", msg, symbol, extra={"event": "indicator_reentry_exception", "symbol": symbol})
        return False, msg
