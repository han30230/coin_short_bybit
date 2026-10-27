"""Fail-closed short-entry filter using this exchange contract's complete daily lows."""

import json
import logging
import os
import queue
import tempfile
import threading
from decimal import Decimal
from pathlib import Path

from coin_rising_short import client, config, symbols

logger = logging.getLogger(__name__)
DAY = 86_400_000
SOURCE = 'bybit_linear'
_service = None
_blocked = {}
_last_log = {}


class HistoricalATL:
    def __init__(self, fetch_page, path, *, clock=None, start_worker=True):
        self.fetch_page = fetch_page
        self.path = Path(path)
        self.clock = clock or client.effective_timestamp_ms
        self.lock = threading.Lock()
        self.jobs = queue.Queue(maxsize=100)
        self.pending = set()
        self.retry_after = {}
        self.records = {}
        try:
            payload = json.loads(self.path.read_text())
            if payload.get('version') == 1 and payload.get('source') == SOURCE and payload.get('env') == config.ENV:
                if isinstance(payload.get('records'), dict):
                    self.records = payload['records']
        except Exception:
            pass
        if start_worker:
            threading.Thread(target=self._run, name='full-history-atl', daemon=True).start()

    def _valid_record(self, record, onboard, now):
        try:
            low = Decimal(record['atl'])
            day = now // DAY * DAY
            return (record['verified'] is True and record['onboard_ms'] == onboard
                    and record['first_day'] == onboard // DAY * DAY
                    and record['first_day'] <= record['through_day'] <= day
                    and record['through_day'] % DAY == 0
                    and 0 <= record['updated_ms'] <= now
                    and low.is_finite() and low > 0)
        except Exception:
            return False

    def check(self, symbol, onboard_ms, current_price):
        now = self.clock()
        try:
            onboard = int(onboard_ms)
            price = Decimal(str(current_price))
            multiplier = Decimal(str(config.ATL_ENTRY_MULTIPLIER))
            if onboard <= 0 or onboard > now or not price.is_finite() or price <= 0:
                return False
            if not multiplier.is_finite() or multiplier <= 1:
                return False
        except Exception:
            return False
        with self.lock:
            record = self.records.get(symbol)
            if self._valid_record(record, onboard, now):
                if record['through_day'] == now // DAY * DAY and now - record['updated_ms'] <= 60_000:
                    # A new low since the last API refresh must also be blocked.
                    low = min(Decimal(record['atl']), price)
                    return price > low * multiplier
            key = (symbol, onboard)
            if key not in self.pending and now >= self.retry_after.get(key, 0):
                try:
                    self.jobs.put_nowait(key)
                    self.pending.add(key)
                except queue.Full:
                    pass
        return False

    def _refresh(self, symbol, onboard):
        now = self.clock()
        first = onboard // DAY * DAY
        today = now // DAY * DAY
        with self.lock:
            old = self.records.get(symbol)
        if self._valid_record(old, onboard, now):
            cursor = old['through_day']  # Repeat the previously unfinished day.
            low = Decimal(old['atl'])
        else:
            cursor = first
            low = None
        while cursor <= today:
            last_day = min(cursor + 999 * DAY, today)
            end = min(last_day + DAY - 1, now)
            rows = self.fetch_page(symbol, cursor, end)
            if not isinstance(rows, list):
                raise ValueError('invalid candle response')
            expected = set(range(cursor, last_day + 1, DAY))
            seen = set()
            for row in rows:
                if not isinstance(row, (list, tuple)) or len(row) < 5:
                    raise ValueError('invalid candle row')
                stamp = int(row[0])
                if stamp not in expected or stamp in seen:
                    raise ValueError('out-of-range or duplicate candle')
                o, h, l, c = (Decimal(str(value)) for value in row[1:5])
                if not all(value.is_finite() and value > 0 for value in (o, h, l, c)):
                    raise ValueError('invalid candle price')
                if not (l <= min(o, c) <= max(o, c) <= h):
                    raise ValueError('inconsistent candle OHLC')
                low = l if low is None else min(low, l)
                seen.add(stamp)
            if seen != expected:
                raise ValueError('history missing one or more daily candles')
            cursor = last_day + DAY
        record = dict(verified=True, onboard_ms=onboard, first_day=first,
                      through_day=today, updated_ms=now, atl=str(low))
        with self.lock:
            self.records[symbol] = record
            payload = dict(version=1, source=SOURCE, env=config.ENV, records=dict(self.records))
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', dir=self.path.parent, encoding='utf-8', delete=False) as handle:
                temporary = handle.name
                json.dump(payload, handle)
            os.replace(temporary, self.path)
        except Exception:
            logger.warning('ATL cache save failed; verified value remains in memory', extra={'symbol': symbol})
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        logger.info('Full-history ATL verified: %s atl=%s from=%s through=%s', symbol, low, first, today,
                    extra={'event': 'atl_history_verified', 'symbol': symbol, 'atl': str(low)})

    def process_one(self):
        try:
            symbol, onboard = self.jobs.get_nowait()
        except queue.Empty:
            return False
        key = (symbol, onboard)
        try:
            self._refresh(symbol, onboard)
        except Exception as exc:
            with self.lock:
                self.retry_after[key] = self.clock() + 60_000
            logger.warning('ATL verification failed; short entry blocked: %s (%s)', symbol, type(exc).__name__,
                           extra={'event': 'atl_history_unavailable', 'symbol': symbol})
        finally:
            with self.lock:
                self.pending.discard(key)
            self.jobs.task_done()
        return True

    def _run(self):
        while True:
            if not self.process_one():
                # Only this daemon worker waits; the trading loop never waits for history.
                threading.Event().wait(0.1)


def _fetch_page(symbol, start, end):
    result = client.public_get('/v5/market/kline', {
        'category': 'linear', 'symbol': symbol, 'interval': 'D',
        'start': start, 'end': end, 'limit': 1000,
    })
    if not isinstance(result, dict) or result.get('symbol') != symbol:
        raise ValueError('invalid historical kline response')
    return result.get('list')


def allow_short(symbol, current_price):
    global _service
    if not config.ATL_FILTER_ENABLED:
        _blocked[symbol] = False
        return True
    try:
        if _service is None:
            _service = HistoricalATL(_fetch_page, config.ATL_CACHE_FILE)
        onboard = symbols.TRADING_SYMBOLS.get(symbol, {}).get('onboardDate')
        allowed = _service.check(symbol, onboard, current_price)
    except Exception:
        allowed = False
    _blocked[symbol] = not allowed
    if not allowed:
        now = client.effective_timestamp_ms()
        if now - _last_log.get(symbol, 0) >= 60_000:
            logger.info('ATL short-entry block: %s price=%s multiplier=%s (near ATL or history not verified)',
                        symbol, current_price, config.ATL_ENTRY_MULTIPLIER,
                        extra={'event': 'atl_entry_blocked', 'symbol': symbol})
            _last_log[symbol] = now
    return allowed


def was_blocked(symbol):
    return _blocked.get(symbol, False)
