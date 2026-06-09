import logging
import time
from typing import Dict

from coin_rising_short import client, config, upbit

logger = logging.getLogger(__name__)


def _is_old_enough_futures_symbol(launch_ms: int) -> bool:
    if launch_ms <= 0:
        return False
    now_ms = int(time.time() * 1000)
    min_age_ms = config.MIN_FUTURES_LISTING_AGE_DAYS * 24 * 60 * 60 * 1000
    return now_ms - launch_ms >= min_age_ms


def _linear_to_binance_shape(row: dict) -> dict:
    """filters.parse_filters 호환용 Binance exchangeInfo 심볼 형태."""
    symbol = row["symbol"]
    base = row.get("baseCoin") or symbol.replace("USDT", "")
    price_filter = row.get("priceFilter") or {}
    lot_filter = row.get("lotSizeFilter") or {}
    launch_ms = int(row.get("launchTime") or 0)
    return {
        "symbol": symbol,
        "baseAsset": base,
        "quoteAsset": row.get("quoteCoin", "USDT"),
        "status": "TRADING",
        "contractType": "PERPETUAL",
        "closeOnly": False,
        "orderTypes": ["LIMIT"],
        "onboardDate": launch_ms,
        "filters": [
            {
                "filterType": "PRICE_FILTER",
                "tickSize": str(price_filter.get("tickSize", "0.01")),
            },
            {
                "filterType": "LOT_SIZE",
                "stepSize": str(lot_filter.get("qtyStep", "0.001")),
                "minQty": str(lot_filter.get("minOrderQty", "0.001")),
            },
            {
                "filterType": "MIN_NOTIONAL",
                "notional": str(lot_filter.get("minNotionalValue", "0")),
            },
        ],
    }


def get_trading_symbols() -> Dict[str, dict]:
    """Bybit Linear USDT Perp 거래 가능 심볼 (선택적 유니버스 필터)."""
    logger.info("심볼 정보 로딩 중...")

    fut_rows = client.fetch_instruments_paginated(config.CATEGORY_LINEAR)
    upbit_assets = None
    if config.FILTER_UPBIT_LISTED:
        upbit_assets = upbit.get_upbit_base_assets()
        logger.info("업비트 상장 필터 적용: ON")
    else:
        logger.info("업비트 상장 필터 적용: OFF")

    raw_futures: list[dict] = []
    for row in fut_rows:
        if row.get("status") != "Trading":
            continue
        if row.get("quoteCoin") != "USDT":
            continue
        base = str(row.get("baseCoin", "")).upper()
        if upbit_assets is not None and base not in upbit_assets:
            continue
        raw_futures.append(row)

    futures_symbols: Dict[str, dict] = {}
    for row in raw_futures:
        if config.FILTER_FUTURES_LISTING_AGE:
            launch_ms = int(row.get("launchTime") or 0)
            if not _is_old_enough_futures_symbol(launch_ms):
                continue
        shaped = _linear_to_binance_shape(row)
        futures_symbols[shaped["symbol"]] = shaped

    if config.FILTER_FUTURES_LISTING_AGE:
        logger.info(
            "선물 상장 %s일 이상 필터 적용: %s개 -> %s개",
            config.MIN_FUTURES_LISTING_AGE_DAYS,
            len(raw_futures),
            len(futures_symbols),
        )
    else:
        logger.info("선물 상장 기간 필터 적용: OFF (%s개)", len(futures_symbols))

    if not config.FILTER_SPOT_COEXIST:
        logger.info("스팟+선물 공존 필터 적용: OFF, 선물 심볼 %s개", len(futures_symbols))
        return futures_symbols

    spot_rows = client.fetch_instruments_paginated(config.CATEGORY_SPOT)
    spot_symbols = {
        s["symbol"]
        for s in spot_rows
        if s.get("status") == "Trading" and s.get("quoteCoin") == "USDT"
    }

    both = {k: v for k, v in futures_symbols.items() if k in spot_symbols}
    logger.info("스팟+선물 공존 필터 적용: ON, %s개 -> %s개", len(futures_symbols), len(both))
    return both


TRADING_SYMBOLS: Dict[str, dict] = {}


def init_trading_symbols(max_retries: int = 3) -> None:
    global TRADING_SYMBOLS
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            TRADING_SYMBOLS = get_trading_symbols()
            if not TRADING_SYMBOLS:
                raise RuntimeError("로딩된 거래 심볼이 없습니다.")
            return
        except Exception as exc:
            last_error = exc
            if attempt < max_retries:
                wait_sec = min(2**attempt, 8)
                logger.warning(
                    "심볼 로딩 실패 (%s/%s): %s. %ss 후 재시도",
                    attempt,
                    max_retries,
                    exc,
                    wait_sec,
                )
                time.sleep(wait_sec)
            else:
                logger.exception("심볼 로딩 최종 실패")
    raise RuntimeError(f"심볼 초기화 실패: {last_error}")
