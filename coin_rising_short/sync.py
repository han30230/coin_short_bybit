import logging
from decimal import Decimal
from typing import Dict, List

from coin_rising_short import client, config, orders, positions, runtime, state

logger = logging.getLogger(__name__)


def _sync_order_state() -> bool:
    """저장된 주문 ID와 거래소 open/filled 상태 맞춤."""
    if not state.position_state:
        return False

    try:
        orders_list = client.get_open_orders()
    except Exception as exc:
        logger.warning("openOrders 조회 실패: %s", exc)
        orders_list = []

    open_map = {(o["symbol"], int(o["orderId"])): o for o in orders_list}
    remove_symbols: List[str] = []
    dirty = False

    for symbol, st in list(state.position_state.items()):
        st.setdefault("reentry_count", 0)
        st.setdefault("last_reentry_price", st.get("entry_price", Decimal("0")))
        st.setdefault("tp_order_id", None)
        st.setdefault("tp_entry_price", Decimal("0"))
        st.setdefault("tp_qty", Decimal("0"))
        st.setdefault("tp_exit_logged", False)
        st.setdefault("exit_order_id", None)
        st.setdefault("exit_retry_count", 0)
        st.setdefault("st_last_direction", None)

        for entry in st.get("entries", []):
            entry.setdefault("filled", False)
            entry.setdefault("closed", False)
            entry.setdefault("entry_logged", False)
            oid = entry.get("order_id")
            if not oid or int(oid) == 0:
                continue
            oid = int(oid)
            key = (symbol, oid)
            if key in open_map:
                o = open_map[key]
                st_ord = o.get("status", "")
                if st_ord == "PARTIALLY_FILLED":
                    ex = Decimal(str(o.get("executedQty", "0")))
                    if ex > 0:
                        entry["qty"] = ex
                        dirty = True
                entry["filled"] = False
                entry["closed"] = False
                continue

            detail = orders.get_order_detail(symbol, oid)
            if not detail:
                logger.warning("주문 상세 없음(스킵): %s orderId=%s", symbol, oid)
                continue
            st_detail = detail.get("status")
            if st_detail == "FILLED":
                ap = Decimal(str(detail.get("avgPrice", "0")))
                eq = Decimal(str(detail.get("executedQty", "0")))
                if ap > 0:
                    entry["entry_price"] = ap
                if eq > 0:
                    entry["qty"] = eq
                entry["filled"] = True
                entry["closed"] = False
                if config.USE_SUPERTREND_EXIT and st.get("st_last_direction") is None:
                    st["st_last_direction"] = -1
                dirty = True
            elif st_detail in ("CANCELED", "REJECTED", "EXPIRED", "NEW"):
                entry["filled"] = False
                entry["closed"] = True
                dirty = True
            elif st_detail == "PARTIALLY_FILLED":
                ex = Decimal(str(detail.get("executedQty", "0")))
                if ex > 0:
                    entry["qty"] = ex
                    dirty = True

        tp_oid = st.get("tp_order_id")
        if tp_oid:
            tp_detail = orders.get_order_detail(symbol, int(tp_oid))
            if tp_detail:
                tp_status = tp_detail.get("status")
                if tp_status == "FILLED":
                    st["tp_exit_logged"] = True
                    for entry in st.get("entries", []):
                        if entry.get("filled"):
                            entry["filled"] = False
                            entry["closed"] = True
                    remove_symbols.append(symbol)
                    dirty = True
                elif tp_status in ("CANCELED", "REJECTED", "EXPIRED"):
                    st["tp_order_id"] = None
                    dirty = True

        exit_oid = st.get("exit_order_id")
        if exit_oid:
            ex_detail = orders.get_order_detail(symbol, int(exit_oid))
            ex_status = ex_detail.get("status") if ex_detail else None
            if ex_status == "FILLED":
                st["tp_exit_logged"] = True
                for entry in st.get("entries", []):
                    if entry.get("filled"):
                        entry["filled"] = False
                        entry["closed"] = True
                remove_symbols.append(symbol)
                dirty = True
            elif ex_status in ("CANCELED", "REJECTED", "EXPIRED") or ex_detail is None:
                st["exit_order_id"] = None
                dirty = True

    for symbol in remove_symbols:
        state.position_state.pop(symbol, None)
        dirty = True
        logger.info(
            "동기화 중 종료 심볼 상태 정리: %s",
            symbol,
            extra={"event": "sync_symbol_state_cleared", "symbol": symbol},
        )

    return dirty


def reconcile_positions_with_exchange(
    exchange_shorts: Dict[str, positions.ExternalShort] | None = None,
) -> bool:
    """
    거래소 실포지션과 로컬 JSON 대조.
    - 수동 청산: 로컬 filled 있으나 거래소 size=0 → 상태 정리
    - 수동 진입: 거래소 숏 있으나 로컬 없음/미체결 → 추적 등록
    - 수량 불일치: 거래소 size 기준으로 로컬 수정
    """
    if exchange_shorts is None:
        exchange_shorts = positions.fetch_exchange_shorts()
    remove_symbols: List[str] = []
    dirty = False

    for symbol in list(state.position_state.keys()):
        st = state.position_state[symbol]
        _, local_qty, _ = positions.get_filled_from_state(st)
        ex = exchange_shorts.get(symbol)
        ex_qty = ex["size"] if ex else Decimal("0")

        if local_qty > 0 and ex_qty <= 0:
            positions.clear_symbol_state(
                symbol,
                st,
                note="수동 청산 감지(거래소 동기화)",
            )
            remove_symbols.append(symbol)
            dirty = True
            logger.info(
                "수동 청산 반영: %s (로컬 qty=%s)",
                symbol,
                local_qty,
                extra={"event": "manual_close_detected", "symbol": symbol},
            )
            continue

        if ex and ex_qty > 0:
            if local_qty <= 0:
                pending = any(
                    not e.get("closed") and not e.get("filled")
                    for e in st.get("entries", [])
                )
                if pending:
                    for entry in st.get("entries", []):
                        if not entry.get("closed") and not entry.get("filled"):
                            entry["closed"] = True
                            entry["filled"] = False
                    dirty = True
                positions.adopt_external_short(symbol, ex)
                dirty = True
            elif positions.sync_state_qty_from_exchange(symbol, st, ex):
                dirty = True

        entries = st.get("entries", [])
        if entries and all(bool(e.get("closed")) for e in entries) and not st.get("tp_order_id"):
            if symbol not in remove_symbols:
                remove_symbols.append(symbol)
                dirty = True

    for symbol, ex in exchange_shorts.items():
        if symbol in state.position_state:
            continue
        positions.adopt_external_short(symbol, ex)
        runtime.QUALIFIED_WATCH.pop(symbol, None)
        dirty = True

    for symbol in remove_symbols:
        state.position_state.pop(symbol, None)
        runtime.QUALIFIED_WATCH.pop(symbol, None)

    if dirty:
        state.save_position_state()
        if config.USE_SUPERTREND_ENTRY:
            state.save_qualified_watch()
    return dirty


def sync_state_with_exchange() -> None:
    state.load_position_state()
    logger.info("거래소와 상태 동기화 중...", extra={"event": "sync_started"})

    dirty = _sync_order_state()
    if reconcile_positions_with_exchange():
        dirty = True

    if dirty:
        state.save_position_state()

    exchange_shorts = positions.fetch_exchange_shorts()
    open_count = len(exchange_shorts) if exchange_shorts else positions.count_open_short_positions()
    logger.info(
        "동기화 완료: 추적 심볼 %s개, 거래소 숏 %s개 (한도 %s) -> %s",
        len(state.position_state),
        open_count,
        config.MAX_CONCURRENT_POSITIONS,
        config.POSITION_STATE_PATH,
        extra={
            "event": "sync_completed",
            "tracked_symbols": len(state.position_state),
            "exchange_shorts": open_count,
            "max_positions": config.MAX_CONCURRENT_POSITIONS,
        },
    )
