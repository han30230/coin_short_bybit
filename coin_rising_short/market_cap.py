import logging
import time
from decimal import Decimal
from typing import Any, Optional

import requests

from coin_rising_short import config

logger = logging.getLogger(__name__)

CMC_QUOTES_URL = "https://pro-api.coinmarketcap.com/v2/cryptocurrency/quotes/latest"

# Binance 선물 티커 → CMC 조회 심볼 예외 매핑 (필요 시 확장)
SYMBOL_OVERRIDES: dict[str, str] = {}

_mcap_cache: dict[str, tuple[Decimal, float]] = {}
_logged_no_cmc_key = False


def log_mcap_filter_status_once() -> None:
    """시가총액 필터 상태 1회 안내."""
    global _logged_no_cmc_key
    if _logged_no_cmc_key:
        return
    _logged_no_cmc_key = True
    if config.MCAP_FILTER_ENABLED:
        logger.info(
            "시가총액 필터 ON (min_usd=%s, 조회 실패 시 %s)",
            config.MIN_MARKET_CAP_USD,
            "후보 유지" if config.MCAP_FAIL_OPEN else "후보 제외",
            extra={"event": "mcap_filter_enabled"},
        )
    else:
        logger.info(
            "시가총액 필터 OFF (USE_MCAP_FILTER=false 또는 CMC 키 없음)",
            extra={"event": "mcap_filter_disabled"},
        )


def clear_mcap_cache() -> None:
    """테스트 등에서 캐시 초기화용."""
    _mcap_cache.clear()


def normalize_binance_symbol(symbol: str) -> str:
    s = str(symbol).upper().strip()
    if s in SYMBOL_OVERRIDES:
        return SYMBOL_OVERRIDES[s]
    if s.endswith("USDT"):
        base = s[:-4]
    else:
        base = s
    if base.startswith("1000") and len(base) > 4:
        return base[4:]
    return base


def _pick_best_market_cap_usd(entries: Any) -> Optional[Decimal]:
    items: list[dict[str, Any]] = []
    if isinstance(entries, list):
        items = [x for x in entries if isinstance(x, dict)]
    elif isinstance(entries, dict):
        items = [entries]
    if not items:
        return None
    best: Optional[Decimal] = None
    for item in items:
        try:
            q = item.get("quote") or {}
            usd = q.get("USD") or {}
            cap = usd.get("market_cap")
            if cap is None:
                continue
            d = Decimal(str(cap))
            if best is None or d > best:
                best = d
        except Exception:
            continue
    return best


def _fetch_market_cap_usd_from_cmc(cmc_symbol: str) -> Optional[Decimal]:
    headers = {
        "X-CMC_PRO_API_KEY": config.CMC_API_KEY,
        "Accept": "application/json",
    }
    params = {"symbol": cmc_symbol, "convert": "USD"}
    try:
        resp = requests.get(CMC_QUOTES_URL, headers=headers, params=params, timeout=15)
    except requests.RequestException as exc:
        logger.warning(
            "시가총액 API 요청 실패: symbol=%s err=%s",
            cmc_symbol,
            exc,
            extra={"event": "mcap_request_error", "cmc_symbol": cmc_symbol},
        )
        return None

    try:
        body = resp.json()
    except Exception as exc:
        logger.warning(
            "시가총액 JSON 파싱 실패: symbol=%s status=%s err=%s",
            cmc_symbol,
            resp.status_code,
            exc,
            extra={"event": "mcap_json_error", "cmc_symbol": cmc_symbol},
        )
        return None

    if resp.status_code >= 400:
        err = body.get("status") if isinstance(body, dict) else None
        logger.warning(
            "시가총액 API 오류 응답: symbol=%s http=%s status=%s",
            cmc_symbol,
            resp.status_code,
            err,
            extra={"event": "mcap_api_http_error", "cmc_symbol": cmc_symbol},
        )
        return None

    if not isinstance(body, dict):
        return None

    st = body.get("status")
    if isinstance(st, dict) and st.get("error_code") not in (None, 0):
        logger.warning(
            "시가총액 API status 오류: symbol=%s %s",
            cmc_symbol,
            st,
            extra={"event": "mcap_api_status_error", "cmc_symbol": cmc_symbol},
        )
        return None

    data = body.get("data")
    if not isinstance(data, dict):
        logger.warning(
            "시가총액 응답 data 형식 오류: symbol=%s",
            cmc_symbol,
            extra={"event": "mcap_data_bad_type", "cmc_symbol": cmc_symbol},
        )
        return None

    key = cmc_symbol.upper()
    entries = data.get(key)
    if entries is None:
        for k, v in data.items():
            if str(k).upper() == key:
                entries = v
                break
    cap = _pick_best_market_cap_usd(entries)
    if cap is None:
        logger.warning(
            "시가총액 필드 없음 또는 빈 목록: symbol=%s",
            cmc_symbol,
            extra={"event": "mcap_missing_cap", "cmc_symbol": cmc_symbol},
        )
    return cap


def get_market_cap_usd(symbol: str) -> Optional[Decimal]:
    """
    Binance 선물 심볼 기준 USD 시가총액.
    CMC_API_KEY가 없으면(MCAP_FILTER_ENABLED False) 호출하지 말 것. 실수 호출 시 None.
    실패 시 None.
    """
    if not config.MCAP_FILTER_ENABLED:
        return None

    cmc_sym = normalize_binance_symbol(symbol)
    now = time.time()
    cached = _mcap_cache.get(cmc_sym)
    if cached is not None and now - cached[1] < config.MCAP_CACHE_TTL_SEC:
        cap, ts = cached
        logger.info(
            "시가총액 캐시 사용: futures_symbol=%s cmc_symbol=%s cap_usd=%s age_sec=%.1f",
            symbol,
            cmc_sym,
            cap,
            now - ts,
            extra={
                "event": "mcap_cache_hit",
                "symbol": symbol,
                "cmc_symbol": cmc_sym,
                "market_cap_usd": str(cap),
            },
        )
        return cap

    logger.info(
        "시가총액 API 조회 시작: futures_symbol=%s cmc_symbol=%s",
        symbol,
        cmc_sym,
        extra={"event": "mcap_lookup_start", "symbol": symbol, "cmc_symbol": cmc_sym},
    )

    try:
        cap = _fetch_market_cap_usd_from_cmc(cmc_sym)
    except Exception as exc:
        logger.warning(
            "시가총액 조회 예외: futures_symbol=%s err=%s",
            symbol,
            exc,
            extra={"event": "mcap_lookup_exception", "symbol": symbol},
        )
        return None

    if cap is None:
        return None

    _mcap_cache[cmc_sym] = (cap, now)
    logger.info(
        "시가총액 조회 성공: futures_symbol=%s cmc_symbol=%s cap_usd=%s",
        symbol,
        cmc_sym,
        cap,
        extra={
            "event": "mcap_lookup_success",
            "symbol": symbol,
            "cmc_symbol": cmc_sym,
            "market_cap_usd": str(cap),
        },
    )
    return cap
