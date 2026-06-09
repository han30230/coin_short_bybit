import logging
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from coin_rising_short import client, config, indicators, market_cap, market_data, orders, positions, runtime, state, symbols, sync, trade_journal

logger = logging.getLogger(__name__)


def _get_funding_rate_map() -> Dict[str, Decimal]:
    tickers = client.get_linear_tickers()
    out: Dict[str, Decimal] = {}
    for row in tickers:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        if not isinstance(symbol, str):
            continue
        try:
            out[symbol] = Decimal(str(row.get("fundingRate", "0")))
        except Exception:
            continue
    return out


def get_futures_gainers_and_top_movers(
    funding_rate_map: Dict[str, Decimal],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    market_cap.log_mcap_filter_status_once()

    data = client.get_linear_tickers()
    if not isinstance(data, list):
        raise RuntimeError(f"24hr ticker 응답 형식 오류: {type(data)}")

    qualified: List[Dict[str, Any]] = []
    all_movers: List[Dict[str, Any]] = []
    for t in data:
        symbol = t.get("symbol")
        if symbol not in symbols.TRADING_SYMBOLS:
            continue
        try:
            # Bybit price24hPcnt: 소수(0.1678 = 16.78%)
            change_pct = Decimal(str(t.get("price24hPcnt", "0"))) * Decimal("100")
            turnover_24h = Decimal(str(t.get("turnover24h", "0")))
            last_price = Decimal(str(t.get("lastPrice", "0")))

            row = {
                "symbol": symbol,
                "change_pct": change_pct,
                "last_price": last_price,
                "turnover_24h": turnover_24h,
                "funding_rate": funding_rate_map.get(symbol, Decimal("0")),
            }
            all_movers.append(row)
            passed_basic = (
                change_pct >= config.GAINER_THRESHOLD_PCT
                and turnover_24h >= config.MIN_VOLUME_USDT
            )
            if not passed_basic:
                continue

            if config.MCAP_FILTER_ENABLED:
                mcap_usd = market_cap.get_market_cap_usd(symbol)
                if mcap_usd is None:
                    if config.MCAP_FAIL_OPEN:
                        logger.info(
                            "시가총액 조회 실패 — 후보 유지: symbol=%s",
                            symbol,
                            extra={"event": "mcap_fetch_failed_pass", "symbol": symbol},
                        )
                    else:
                        logger.warning(
                            "시가총액 조회 실패로 진입 후보 제외: symbol=%s",
                            symbol,
                            extra={"event": "mcap_skip_fetch_failed", "symbol": symbol},
                        )
                        continue
                elif mcap_usd < config.MIN_MARKET_CAP_USD:
                    logger.info(
                        "최소 시가총액 미달로 진입 후보 제외: symbol=%s cap_usd=%s min_usd=%s",
                        symbol,
                        mcap_usd,
                        config.MIN_MARKET_CAP_USD,
                        extra={
                            "event": "mcap_skip_below_min",
                            "symbol": symbol,
                            "market_cap_usd": str(mcap_usd),
                            "min_market_cap_usd": str(config.MIN_MARKET_CAP_USD),
                        },
                    )
                    continue
                else:
                    row["market_cap_usd"] = mcap_usd

            qualified.append(row)
        except Exception:
            continue

    qualified.sort(key=lambda x: x["change_pct"], reverse=True)

    if config.FILTER_MCAP_FDV:
        qualified = market_data.filter_by_mcap_fdv(qualified)

    all_movers.sort(key=lambda x: x["change_pct"], reverse=True)
    return qualified, all_movers[:3]


def _get_filled_position(st: Dict[str, Any]) -> Tuple[Decimal, Decimal, str]:
    return positions.get_filled_from_state(st)


def _close_short_with_retry(symbol: str, qty: Decimal) -> Optional[int]:
    for attempt in range(1, config.EXIT_CLOSE_MAX_RETRIES + 1):
        close_oid = orders.close_short_position(symbol, qty)
        if close_oid is not None:
            return close_oid
        logger.warning(
            "청산 주문 실패 재시도: %s (%s/%s)",
            symbol,
            attempt,
            config.EXIT_CLOSE_MAX_RETRIES,
            extra={"event": "exit_close_retry", "symbol": symbol, "attempt": attempt},
        )
        time.sleep(0.5)
    logger.error(
        "청산 주문 최종 실패(다음 루프 재시도): %s qty=%s",
        symbol,
        qty,
        extra={"event": "exit_close_failed", "symbol": symbol},
    )
    return None


def _finalize_exit_filled(symbol: str, st: Dict[str, Any], exit_oid: int, detail: dict) -> None:
    exit_price = Decimal(str(detail.get("avgPrice", "0")))
    exit_qty = Decimal(str(detail.get("executedQty", "0")))
    avg_entry, _, direction = _get_filled_position(st)
    if exit_price > 0 and exit_qty > 0:
        trade_journal.log_exit_filled(
            symbol=symbol,
            direction=direction,
            entry_order_id="MULTI",
            tp_order_id=int(exit_oid),
            entry_price=avg_entry if avg_entry > 0 else exit_price,
            exit_price=exit_price,
            qty=exit_qty,
            entry_time_ms=None,
            exit_time_ms=detail.get("updateTime") if isinstance(detail.get("updateTime"), int) else None,
            note="SuperTrend 롱 신호 청산",
        )
    st["tp_exit_logged"] = True
    st["exit_pending"] = False
    st["exit_order_id"] = None
    for entry in st.get("entries", []):
        if entry.get("filled"):
            entry["closed"] = True
            entry["filled"] = False


def _process_short_exit(symbol: str, st: Dict[str, Any], exchange_shorts: Dict[str, positions.ExternalShort]) -> bool:
    """
    청산 주문 추적·재시도. True면 심볼 상태 제거 대상.
    """
    exit_oid = st.get("exit_order_id")
    if exit_oid:
        try:
            detail = orders.get_order_detail(symbol, int(exit_oid))
        except Exception as exc:
            logger.warning("청산 주문 조회 실패: %s orderId=%s %s", symbol, exit_oid, exc)
            detail = None
        status = detail.get("status") if detail else None
        if status == "FILLED" and detail:
            _finalize_exit_filled(symbol, st, int(exit_oid), detail)
            logger.info(
                "ST 롱 청산 완료: %s exitOrderId=%s",
                symbol,
                exit_oid,
                extra={"event": "st_long_exit_filled", "symbol": symbol, "order_id": int(exit_oid)},
            )
            return True
        if status == "NEW":
            return False
        if status == "PARTIALLY_FILLED":
            orders.cancel_order(symbol, int(exit_oid))
            st["exit_order_id"] = None
        else:
            logger.warning(
                "청산 주문 비정상 상태(%s), 재주문: %s orderId=%s",
                status,
                symbol,
                exit_oid,
                extra={"event": "exit_order_reset", "symbol": symbol, "status": status},
            )
            st["exit_order_id"] = None

    avg_entry, local_qty, _ = _get_filled_position(st)
    ex = exchange_shorts.get(symbol)
    ex_qty = ex["size"] if ex else Decimal("0")

    if ex_qty <= 0:
        if local_qty > 0:
            positions.clear_symbol_state(symbol, st, note="청산 완료(거래소 포지션 없음)")
        return True

    if local_qty <= 0:
        return False

    close_qty = local_qty if local_qty <= ex_qty else ex_qty

    logger.info(
        "숏 청산 시도: %s qty=%s (exit_pending=%s)",
        symbol,
        close_qty,
        st.get("exit_pending"),
        extra={"event": "short_exit_attempt", "symbol": symbol, "qty": str(close_qty)},
    )
    close_oid = _close_short_with_retry(symbol, close_qty)
    if close_oid is not None:
        st["exit_order_id"] = close_oid
        st["exit_retry_count"] = 0
    else:
        st["exit_retry_count"] = int(st.get("exit_retry_count", 0)) + 1
    return False


def _refresh_symbol_take_profit(symbol: str, st: Dict[str, Any]) -> bool:
    avg_entry, total_qty, direction = _get_filled_position(st)
    if total_qty <= 0:
        return False

    need_replace = False
    existing_tp_oid = st.get("tp_order_id")
    target_avg = str(avg_entry)
    target_qty = str(total_qty)

    if existing_tp_oid:
        old_avg = str(st.get("tp_entry_price", ""))
        old_qty = str(st.get("tp_qty", ""))
        tp_status = orders.get_order_status(symbol, int(existing_tp_oid))
        if tp_status == "FILLED":
            return False
        if tp_status in ("NEW", "PARTIALLY_FILLED") and old_avg == target_avg and old_qty == target_qty:
            return False
        need_replace = True

    if need_replace and existing_tp_oid:
        if not orders.cancel_order(symbol, int(existing_tp_oid)):
            return False

    tp_oid = None
    for attempt in range(3):
        tp_oid = orders.place_take_profit_order(symbol, direction, avg_entry, total_qty)
        if tp_oid is not None:
            break
        logger.warning(
            "TP 재생성 실패 재시도: %s (%s/3)",
            symbol,
            attempt + 1,
            extra={"event": "symbol_tp_refresh_retry", "symbol": symbol},
        )
        time.sleep(0.5)
    if tp_oid is None:
        logger.error(
            "TP 재생성 최종 실패: %s (무보호 구간 가능)",
            symbol,
            extra={"event": "symbol_tp_refresh_failed", "symbol": symbol},
        )
        return False
    st["tp_order_id"] = tp_oid
    st["tp_entry_price"] = avg_entry
    st["tp_qty"] = total_qty
    st["tp_exit_logged"] = False
    logger.info(
        "심볼 TP 갱신: %s avg=%s qty=%s tpOrderId=%s",
        symbol,
        avg_entry,
        total_qty,
        tp_oid,
        extra={"event": "symbol_tp_refreshed", "symbol": symbol, "order_id": tp_oid},
    )
    return True


def check_filled_and_refresh_tp() -> None:
    dirty = False
    remove_symbols: List[str] = []
    for symbol, st in state.position_state.items():
        entries = st.get("entries", [])
        symbol_dirty = False
        for entry in entries:
            if entry.get("filled") or entry.get("closed"):
                continue

            order_id = entry["order_id"]
            direction = entry["direction"]
            entry_price = entry["entry_price"]
            qty = entry["qty"]

            status = orders.get_order_status(symbol, order_id)
            if status is None:
                continue
            if status == "NOT_FOUND":
                logger.warning(
                    "주문 미존재(-2013)로 엔트리 종료 처리: %s orderId=%s",
                    symbol,
                    order_id,
                    extra={"event": "entry_order_not_found", "symbol": symbol, "order_id": order_id},
                )
                entry["filled"] = False
                entry["closed"] = True
                dirty = True
                symbol_dirty = True
                continue

            if status == "FILLED":
                logger.info(
                    "진입 체결 확인: %s %s (orderId=%s)",
                    symbol,
                    direction,
                    order_id,
                    extra={
                        "event": "entry_filled",
                        "symbol": symbol,
                        "direction": direction,
                        "order_id": order_id,
                    },
                )
                detail = orders.get_order_detail(symbol, order_id)
                filled_time_ms = None
                if detail and isinstance(detail, dict):
                    ap = Decimal(str(detail.get("avgPrice", "0")))
                    ex = Decimal(str(detail.get("executedQty", "0")))
                    if ap > 0:
                        entry["entry_price"] = ap
                        entry_price = ap
                    if ex > 0:
                        entry["qty"] = ex
                        qty = ex
                    t = detail.get("updateTime")
                    if isinstance(t, int):
                        filled_time_ms = t

                if not entry.get("entry_logged"):
                    trade_journal.log_entry_filled(
                        symbol=symbol,
                        direction=direction,
                        order_id=order_id,
                        entry_price=entry_price,
                        qty=qty,
                        filled_time_ms=filled_time_ms,
                    )
                    entry["entry_logged"] = True
                entry["filled"] = True
                entry["closed"] = False
                if config.USE_SUPERTREND_EXIT and st.get("st_last_direction") is None:
                    st["st_last_direction"] = -1
                dirty = True
                symbol_dirty = True
            elif status == "PARTIALLY_FILLED":
                d = orders.get_order_detail(symbol, order_id)
                if d:
                    ex = Decimal(str(d.get("executedQty", "0")))
                    ap = Decimal(str(d.get("avgPrice", "0")))
                    if ex > 0:
                        entry["qty"] = ex
                    if ap > 0:
                        entry["entry_price"] = ap
                    dirty = True
                    symbol_dirty = True
            elif status in ("CANCELED", "REJECTED", "EXPIRED"):
                logger.warning(
                    "진입 주문 종료 상태(%s): %s (orderId=%s) -> TP 생성 스킵",
                    status,
                    symbol,
                    order_id,
                    extra={
                        "event": "entry_closed_without_tp",
                        "symbol": symbol,
                        "status": status,
                        "order_id": order_id,
                    },
                )
                entry["filled"] = False
                entry["closed"] = True
                dirty = True
                symbol_dirty = True
        if symbol_dirty:
            if config.USE_FIXED_TP and not config.USE_SUPERTREND_EXIT:
                if _refresh_symbol_take_profit(symbol, st):
                    dirty = True
            elif config.USE_SUPERTREND_EXIT and st.get("tp_order_id"):
                if orders.cancel_order(symbol, int(st["tp_order_id"])):
                    st["tp_order_id"] = None
                    dirty = True
        # 모든 엔트리가 종료됐고 TP도 없다면, 심볼 상태를 지워서 다음 사이클 진입 가능하게 함.
        entries = st.get("entries", [])
        if entries and all(bool(e.get("closed")) for e in entries) and not st.get("tp_order_id"):
            remove_symbols.append(symbol)
            logger.info(
                "종료된 심볼 상태 정리(수동청산/취소 등): %s",
                symbol,
                extra={"event": "symbol_state_cleared_manual", "symbol": symbol},
            )
            dirty = True
    for symbol in remove_symbols:
        state.position_state.pop(symbol, None)
    if dirty:
        state.save_position_state()


def check_supertrend_exit_and_log(
    exchange_shorts: Optional[Dict[str, positions.ExternalShort]] = None,
) -> None:
    """4h SuperTrend 롱(1) 신호 시 숏 청산 (실패 시 exit_pending으로 루프마다 재시도)."""
    if not config.USE_SUPERTREND_EXIT:
        return

    if exchange_shorts is None:
        exchange_shorts = positions.fetch_exchange_shorts()

    dirty = False
    remove_symbols: List[str] = []
    for symbol, st in list(state.position_state.items()):
        if st.get("external"):
            continue

        st.setdefault("exit_pending", False)
        st.setdefault("exit_order_id", None)
        st.setdefault("exit_retry_count", 0)

        avg_entry, total_qty, _ = _get_filled_position(st)
        ex = exchange_shorts.get(symbol)
        ex_qty = ex["size"] if ex else Decimal("0")

        if st.get("exit_pending") or st.get("exit_order_id"):
            if _process_short_exit(symbol, st, exchange_shorts):
                remove_symbols.append(symbol)
                dirty = True
            else:
                dirty = True
            continue

        if total_qty <= 0 and ex_qty <= 0:
            continue

        if st.get("tp_order_id"):
            if orders.cancel_order(symbol, int(st["tp_order_id"])):
                st["tp_order_id"] = None
                dirty = True

        last_seen = st.get("st_last_direction")
        ok, reason, curr_d = indicators.evaluate_supertrend_signal(symbol, 1, last_seen)
        if curr_d is not None and not st.get("exit_pending"):
            st["st_last_direction"] = curr_d
        if not ok:
            continue

        logger.info(
            "SuperTrend 롱 신호 청산: %s (%s) qty=%s",
            symbol,
            reason,
            ex_qty if ex_qty > 0 else total_qty,
            extra={"event": "st_long_exit_signal", "symbol": symbol},
        )
        st["exit_pending"] = True
        if curr_d is not None:
            st["st_last_direction"] = curr_d
        if _process_short_exit(symbol, st, exchange_shorts):
            remove_symbols.append(symbol)
        dirty = True

    for symbol in remove_symbols:
        state.position_state.pop(symbol, None)
        logger.info(
            "심볼 상태 정리 완료(신규 사이클 허용): %s",
            symbol,
            extra={"event": "symbol_state_cleared", "symbol": symbol},
        )
    if dirty:
        state.save_position_state()


def check_tp_filled_and_log() -> None:
    dirty = False
    remove_symbols: List[str] = []
    for symbol, st in state.position_state.items():
        tp_oid = st.get("tp_order_id")
        if not tp_oid or st.get("tp_exit_logged"):
            continue
        tp_detail = orders.get_order_detail(symbol, int(tp_oid))
        if not tp_detail or tp_detail.get("status") != "FILLED":
            continue
        exit_price = Decimal(str(tp_detail.get("avgPrice", "0")))
        exit_qty = Decimal(str(tp_detail.get("executedQty", "0")))
        if exit_price <= 0 or exit_qty <= 0:
            continue

        avg_entry = Decimal(str(st.get("tp_entry_price", "0")))
        direction = "SHORT"
        if st.get("entries"):
            direction = str(st["entries"][0].get("direction", "SHORT"))

        trade_journal.log_exit_filled(
            symbol=symbol,
            direction=direction,
            entry_order_id="MULTI",
            tp_order_id=int(tp_oid),
            entry_price=avg_entry if avg_entry > 0 else exit_price,
            exit_price=exit_price,
            qty=exit_qty,
            entry_time_ms=None,
            exit_time_ms=tp_detail.get("updateTime") if isinstance(tp_detail.get("updateTime"), int) else None,
            note="평균진입가 기준 TP 청산",
        )
        st["tp_exit_logged"] = True
        # TP 체결 시 현재 사이클 엔트리를 종료 처리해 다음 사이클 진입 가능하게 함.
        for entry in st.get("entries", []):
            if entry.get("filled"):
                entry["closed"] = True
                entry["filled"] = False
        remove_symbols.append(symbol)
        dirty = True
        logger.info(
            "TP 체결 기록 완료: %s tpOrderId=%s",
            symbol,
            tp_oid,
            extra={"event": "tp_filled_logged", "symbol": symbol, "order_id": int(tp_oid)},
        )
    for symbol in remove_symbols:
        state.position_state.pop(symbol, None)
        logger.info(
            "심볼 상태 정리 완료(신규 사이클 허용): %s",
            symbol,
            extra={"event": "symbol_state_cleared", "symbol": symbol},
        )
    if dirty:
        state.save_position_state()


def _sync_qualified_watch(gainers: List[Dict[str, Any]]) -> None:
    """급등 후보 상위 N을 ST 감시에 추가 (해제 없음, 진입 시에만 제거)."""
    now = time.time()
    top_n = config.QUALIFIED_WATCH_TOP_N

    for symbol in list(runtime.QUALIFIED_WATCH.keys()):
        if symbol in state.position_state:
            runtime.QUALIFIED_WATCH.pop(symbol, None)
            logger.info(
                "SuperTrend 감시 제거(포지션 보유): %s",
                symbol,
                extra={"event": "supertrend_watch_removed_position", "symbol": symbol},
            )

    for g in gainers[:top_n]:
        if len(runtime.QUALIFIED_WATCH) >= top_n:
            break
        symbol = g["symbol"]
        until = runtime.SKIP_UNTIL.get(symbol, 0)
        if until and int(now) < until:
            continue
        if symbol in state.position_state:
            continue
        if symbol in runtime.QUALIFIED_WATCH:
            continue
        runtime.QUALIFIED_WATCH[symbol] = {"added_at": now, "last_direction": None}
        logger.info(
            "SuperTrend 숏 신호 대기 등록: %s (change=%.2f%%)",
            symbol,
            g["change_pct"],
            extra={
                "event": "supertrend_watch_added",
                "symbol": symbol,
                "change_pct": str(g["change_pct"]),
            },
        )

    if config.USE_SUPERTREND_ENTRY:
        state.save_qualified_watch()


def _process_watch_entries(
    exchange_shorts: Optional[Dict[str, positions.ExternalShort]],
) -> None:
    """감시 목록 전체에 대해 ST 숏 진입 시도 (순위 이탈해도 유지)."""
    for symbol in list(runtime.QUALIFIED_WATCH.keys()):
        if symbol in state.position_state:
            continue
        until = runtime.SKIP_UNTIL.get(symbol, 0)
        if until and int(time.time()) < until:
            continue
        _try_initial_short_entry(symbol, exchange_shorts)


def _process_reentries(gainers: List[Dict[str, Any]]) -> None:
    if not config.USE_REENTRY:
        return
    top_n = config.QUALIFIED_WATCH_TOP_N
    for g in gainers[:top_n]:
        symbol = g["symbol"]
        if symbol not in state.position_state:
            continue
        until = runtime.SKIP_UNTIL.get(symbol, 0)
        if until and int(time.time()) < until:
            continue
        current_price = g["last_price"]
        st = state.position_state[symbol]
        reentry_count = int(st.get("reentry_count", 0))
        if reentry_count >= config.REENTRY_MAX_COUNT:
            continue
        base_reentry_price = Decimal(str(st.get("last_reentry_price", st["entry_price"])))
        target_price = base_reentry_price * (Decimal("1") + config.REENTRY_RISE_PCT / Decimal("100"))
        if current_price < target_price:
            continue
        logger.warning(
            "%s 직전 재진입가 대비 +%s%% 이상! 추가 숏 재진입 시도... (%s/%s)",
            symbol,
            config.REENTRY_RISE_PCT,
            reentry_count + 1,
            config.REENTRY_MAX_COUNT,
        )
        if config.USE_REENTRY_INDICATOR_FILTER:
            ok, reason = indicators.allow_reentry_short(symbol)
            if not ok:
                logger.info(
                    "지표 필터로 재진입 보류: %s reason=%s",
                    symbol,
                    reason,
                    extra={"event": "reentry_indicator_skipped", "symbol": symbol, "reason": reason},
                )
                continue
        short_entry = orders.place_short_order(symbol)
        if short_entry:
            se_price, se_qty, se_id = short_entry
            st.setdefault("entries", []).append(
                {
                    "direction": "SHORT",
                    "entry_price": se_price,
                    "qty": se_qty,
                    "order_id": se_id,
                    "filled": False,
                }
            )
            st["reentry_count"] = reentry_count + 1
            st["last_reentry_price"] = se_price
            logger.info(
                "%s 재진입 숏 기록: price=%s, orderId=%s, qty=%s",
                symbol,
                se_price,
                se_id,
                se_qty,
                extra={
                    "event": "reentry_recorded",
                    "symbol": symbol,
                    "entry_price": str(se_price),
                    "order_id": se_id,
                    "qty": str(se_qty),
                },
            )
            state.save_position_state()


def _record_initial_short_entry(symbol: str, entry: Tuple[Decimal, Decimal, int]) -> None:
    entry_price, qty, order_id = entry
    state.position_state[symbol] = {
        "entry_price": entry_price,
        "reentry_count": 0,
        "last_reentry_price": entry_price,
        "tp_order_id": None,
        "tp_entry_price": Decimal("0"),
        "tp_qty": Decimal("0"),
        "tp_exit_logged": False,
        "st_last_direction": None,
        "exit_order_id": None,
        "exit_pending": False,
        "exit_retry_count": 0,
        "entries": [
            {
                "direction": "SHORT",
                "entry_price": entry_price,
                "qty": qty,
                "order_id": order_id,
                "filled": False,
            }
        ],
    }
    runtime.QUALIFIED_WATCH.pop(symbol, None)
    logger.info(
        "%s 첫 진입 기록: entry_price=%s, orderId=%s, qty=%s",
        symbol,
        entry_price,
        order_id,
        qty,
        extra={
            "event": "entry_recorded",
            "symbol": symbol,
            "entry_price": str(entry_price),
            "order_id": order_id,
            "qty": str(qty),
        },
    )
    state.save_position_state()
    state.save_qualified_watch()


def _try_initial_short_entry(
    symbol: str,
    exchange_shorts: Optional[Dict[str, positions.ExternalShort]] = None,
) -> None:
    if positions.at_position_capacity(exchange_shorts):
        logger.info(
            "동시 포지션 한도(%s) 도달 — 신규 진입 스킵: %s",
            config.MAX_CONCURRENT_POSITIONS,
            symbol,
            extra={
                "event": "max_positions_reached",
                "symbol": symbol,
                "max": config.MAX_CONCURRENT_POSITIONS,
            },
        )
        return

    if config.USE_SUPERTREND_ENTRY:
        ok, reason = indicators.is_supertrend_short_signal(symbol)
        if not ok:
            if symbol in runtime.QUALIFIED_WATCH:
                logger.info(
                    "SuperTrend 숏 신호 대기: %s (%s)",
                    symbol,
                    reason,
                    extra={
                        "event": "supertrend_entry_waiting",
                        "symbol": symbol,
                        "reason": reason,
                    },
                )
            return
        logger.info(
            "SuperTrend 숏 신호 확인, 진입 시도: %s (%s)",
            symbol,
            reason,
            extra={"event": "supertrend_short_signal", "symbol": symbol},
        )

    entry = orders.place_short_order(symbol)
    if entry is not None:
        _record_initial_short_entry(symbol, entry)


def monitor_loop() -> None:
    st_mode = "ON" if config.USE_SUPERTREND_ENTRY else "OFF"
    re_mode = "ON" if config.USE_REENTRY else "OFF"
    logger.info(
        "Bybit 선물 급등 감시 (+%s%%, ST감시=%s개, SuperTrend 진입=%s, 재진입=%s, 최대포지션=%s)...",
        config.GAINER_THRESHOLD_PCT,
        config.QUALIFIED_WATCH_TOP_N,
        st_mode,
        re_mode,
        config.MAX_CONCURRENT_POSITIONS,
    )
    while True:
        try:
            exchange_shorts = positions.fetch_exchange_shorts()
            funding_rate_map = _get_funding_rate_map()
            gainers, top3 = get_futures_gainers_and_top_movers(funding_rate_map)
            now_str = time.strftime("%H:%M:%S")

            logger.info("%s [%s] 감시 중 %s", "-" * 20, now_str, "-" * 20)
            if not gainers:
                logger.info(
                    "조건에 맞는 종목 없음 -> 전체 상승률 TOP 3 표시",
                    extra={"event": "no_qualified_symbols_fallback"},
                )
                for i, g in enumerate(top3, start=1):
                    symbol = g["symbol"]
                    until = runtime.SKIP_UNTIL.get(symbol, 0)
                    if until and int(time.time()) < until:
                        continue
                    current_price = g["last_price"]
                    change_pct = g["change_pct"]
                    turnover_24h = g.get("turnover_24h", Decimal("0"))
                    funding_rate = g.get("funding_rate", Decimal("0"))
                    logger.info(
                        "TOP%s. %s | price: %.4f | change: %.2f%% | volume(24h): %s | funding: %s",
                        i,
                        symbol,
                        current_price,
                        change_pct,
                        turnover_24h,
                        funding_rate,
                        extra={
                            "event": "top_movers_fallback",
                            "symbol": symbol,
                            "rank": i,
                            "last_price": str(current_price),
                            "change_pct": str(change_pct),
                            "turnover_24h": str(turnover_24h),
                            "funding_rate": str(funding_rate),
                        },
                    )
            else:
                _sync_qualified_watch(gainers)

                top_n = config.QUALIFIED_WATCH_TOP_N
                for i, g in enumerate(gainers[:top_n], start=1):
                    symbol = g["symbol"]
                    until = runtime.SKIP_UNTIL.get(symbol, 0)
                    if until and int(time.time()) < until:
                        continue
                    current_price = g["last_price"]
                    change_pct = g["change_pct"]
                    funding_rate = g.get("funding_rate", Decimal("0"))
                    watch_tag = " [ST감시]" if symbol in runtime.QUALIFIED_WATCH else ""

                    logger.info(
                        "%s. %s%s | price: %.4f | change: %.2f%% | funding: %s",
                        i,
                        symbol,
                        watch_tag,
                        current_price,
                        change_pct,
                        funding_rate,
                        extra={
                            "event": "gainer_ranked",
                            "symbol": symbol,
                            "rank": i,
                            "last_price": str(current_price),
                            "change_pct": str(change_pct),
                            "funding_rate": str(funding_rate),
                            "supertrend_watch": symbol in runtime.QUALIFIED_WATCH,
                        },
                    )

                if config.USE_SUPERTREND_ENTRY:
                    _process_watch_entries(exchange_shorts)
                else:
                    for g in gainers[:top_n]:
                        symbol = g["symbol"]
                        if symbol in state.position_state:
                            continue
                        until = runtime.SKIP_UNTIL.get(symbol, 0)
                        if until and int(time.time()) < until:
                            continue
                        _try_initial_short_entry(symbol, exchange_shorts)

                _process_reentries(gainers)

            if runtime.QUALIFIED_WATCH and config.USE_SUPERTREND_ENTRY:
                if not gainers:
                    _process_watch_entries(exchange_shorts)

            check_filled_and_refresh_tp()
            sync.reconcile_positions_with_exchange(exchange_shorts)
            if config.USE_SUPERTREND_EXIT:
                check_supertrend_exit_and_log(exchange_shorts)
            else:
                check_tp_filled_and_log()

        except KeyboardInterrupt:
            logger.info("사용자 중단 (Ctrl+C). 종료.")
            break
        except Exception as e:
            logger.exception("루프 오류: %s", e)
        time.sleep(config.POLL_INTERVAL_SEC)
