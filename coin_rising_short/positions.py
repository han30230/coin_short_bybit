"""거래소 포지션 조회·로컬 상태 헬퍼."""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from coin_rising_short import client, config, state, trade_journal

logger = logging.getLogger(__name__)

ExternalShort = Dict[str, Any]


def get_filled_from_state(st: Dict[str, Any]) -> Tuple[Decimal, Decimal, str]:
    total_qty = Decimal("0")
    weighted_sum = Decimal("0")
    direction = "SHORT"
    for entry in st.get("entries", []):
        if not entry.get("filled"):
            continue
        qty = Decimal(str(entry.get("qty", "0")))
        price = Decimal(str(entry.get("entry_price", "0")))
        if qty <= 0 or price <= 0:
            continue
        total_qty += qty
        weighted_sum += price * qty
        direction = str(entry.get("direction", "SHORT"))
    if total_qty <= 0:
        return Decimal("0"), Decimal("0"), direction
    return weighted_sum / total_qty, total_qty, direction


def fetch_exchange_shorts() -> Dict[str, ExternalShort]:
    """Bybit linear 숏 포지션 (size>0, side=Sell)."""
    out: Dict[str, ExternalShort] = {}
    try:
        rows = client.get_position_risk()
    except Exception as exc:
        logger.warning("포지션 조회 실패: %s", exc, extra={"event": "position_fetch_failed"})
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        if not isinstance(symbol, str):
            continue
        try:
            size = Decimal(str(row.get("size", "0")))
        except Exception:
            continue
        if size <= 0:
            continue
        side = str(row.get("side", "")).lower()
        if side not in ("sell", "short"):
            continue
        try:
            avg = Decimal(str(row.get("avgPrice") or row.get("entryPrice") or "0"))
        except Exception:
            avg = Decimal("0")
        out[symbol] = {"size": size, "avg_price": avg, "side": "SHORT"}
    return out


def count_open_short_positions(exchange_shorts: Optional[Dict[str, ExternalShort]] = None) -> int:
    """봇이 추적 중인 체결 숏만 집계 (수동 포지션 제외)."""
    _ = exchange_shorts
    count = 0
    for st in state.position_state.values():
        if st.get("external"):
            continue
        _, qty, _ = get_filled_from_state(st)
        if qty > 0:
            count += 1
    return count


def at_position_capacity(exchange_shorts: Optional[Dict[str, ExternalShort]] = None) -> bool:
    return count_open_short_positions(exchange_shorts) >= config.MAX_CONCURRENT_POSITIONS


def clear_symbol_state(
    symbol: str,
    st: Dict[str, Any],
    *,
    note: str,
    log_exit: bool = True,
) -> None:
    if log_exit and not st.get("tp_exit_logged"):
        avg_entry, qty, direction = get_filled_from_state(st)
        if qty > 0:
            try:
                exit_price = client.get_ticker_price(symbol)
            except Exception:
                exit_price = avg_entry if avg_entry > 0 else Decimal("0")
            if exit_price > 0:
                trade_journal.log_exit_filled(
                    symbol=symbol,
                    direction=direction,
                    entry_order_id="MULTI",
                    tp_order_id=st.get("exit_order_id") or "",
                    entry_price=avg_entry if avg_entry > 0 else exit_price,
                    exit_price=exit_price,
                    qty=qty,
                    entry_time_ms=None,
                    exit_time_ms=None,
                    note=note,
                )
    for entry in st.get("entries", []):
        if entry.get("filled"):
            entry["closed"] = True
            entry["filled"] = False
    st["tp_exit_logged"] = True


def adopt_external_short(symbol: str, ex: ExternalShort) -> None:
    """거래소에만 있는 숏(수동 진입 등)을 추적 상태로 등록."""
    size = ex["size"]
    avg = ex.get("avg_price") or Decimal("0")
    if avg <= 0:
        try:
            avg = client.get_ticker_price(symbol)
        except Exception:
            avg = Decimal("0")

    state.position_state[symbol] = {
        "entry_price": avg,
        "reentry_count": 0,
        "last_reentry_price": avg,
        "tp_order_id": None,
        "tp_entry_price": Decimal("0"),
        "tp_qty": Decimal("0"),
        "tp_exit_logged": False,
        "st_last_direction": -1,
        "exit_order_id": None,
        "exit_retry_count": 0,
        "external": True,
        "entries": [
            {
                "direction": "SHORT",
                "entry_price": avg,
                "qty": size,
                "order_id": 0,
                "filled": True,
                "closed": False,
                "entry_logged": True,
                "external": True,
            }
        ],
    }
    logger.info(
        "거래소 숏 포지션 추적 등록(수동/외부): %s qty=%s avg=%s",
        symbol,
        size,
        avg,
        extra={"event": "external_short_adopted", "symbol": symbol, "qty": str(size)},
    )


def sync_state_qty_from_exchange(symbol: str, st: Dict[str, Any], ex: ExternalShort) -> bool:
    """봇 체결 수량을 거래소 숏 size에 맞춤 (부분 수동 청산). 수동 추가분은 반영하지 않음."""
    if st.get("external"):
        return False
    ex_qty = ex["size"]
    ex_avg = ex.get("avg_price") or Decimal("0")
    _, local_qty, _ = get_filled_from_state(st)
    if local_qty <= 0 or ex_qty <= 0:
        return False
    if ex_qty > local_qty:
        return False
    if abs(local_qty - ex_qty) <= Decimal("0.0000001"):
        return False

    filled_entries = [e for e in st.get("entries", []) if e.get("filled") and not e.get("closed")]
    if not filled_entries:
        return False

    primary = filled_entries[0]
    primary["qty"] = ex_qty
    if ex_avg > 0:
        primary["entry_price"] = ex_avg
        st["entry_price"] = ex_avg
    for extra in filled_entries[1:]:
        extra["filled"] = False
        extra["closed"] = True
    logger.info(
        "거래소 기준 수량 동기화: %s local=%s exchange=%s",
        symbol,
        local_qty,
        ex_qty,
        extra={"event": "position_qty_synced", "symbol": symbol},
    )
    return True
