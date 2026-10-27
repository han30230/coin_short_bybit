import importlib
import importlib.util
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

DAY = 86400000
BASE = 1640995200000


def candle(day, low='100'):
    return [day, '100', '200', low, '100']


class TestHistoricalATL(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('coin_rising_short.atl'), 'Full-history ATL filter missing')
        self.mod = importlib.import_module('coin_rising_short.atl')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'atl.json'
        self.now = BASE + 2100 * DAY + 12345
        self.calls = []
        self.history = {BASE + i * DAY: candle(BASE + i * DAY) for i in range(2101)}
        self.history[BASE][3] = '10'
        def fetch(symbol, start, end):
            self.calls.append((start, end))
            return [row for ts, row in sorted(self.history.items(), reverse=True) if start <= ts <= end]
        self.fetch = fetch

    def service(self):
        return self.mod.HistoricalATL(self.fetch, self.path, clock=lambda: self.now, start_worker=False)

    def ready(self, service):
        self.assertFalse(service.check('TESTUSDT', BASE + 3600000, Decimal('14')))
        self.assertTrue(service.process_one())

    def test_scans_every_day_beyond_one_api_page_and_preserves_oldest_low(self):
        s = self.service()
        self.ready(s)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[0][0], BASE)
        self.assertTrue(s.check('TESTUSDT', BASE + 3600000, Decimal('13.0001')))
        self.assertFalse(s.check('TESTUSDT', BASE + 3600000, Decimal('13')))
        self.assertFalse(s.check('TESTUSDT', BASE + 3600000, Decimal('12.99')))

    def test_missing_first_day_or_middle_day_fails_closed(self):
        for missing in [BASE, BASE + 1250 * DAY]:
            with self.subTest(missing=missing):
                row = self.history.pop(missing)
                s = self.service()
                self.ready(s)
                self.assertFalse(s.check('TESTUSDT', BASE + 3600000, Decimal('1000')))
                self.history[missing] = row

    def test_empty_truncated_duplicate_and_invalid_low_fail_closed(self):
        bad_pages = [[], [candle(BASE)], [candle(BASE)] * 1000,
                     [candle(BASE, 'NaN')], [candle(BASE, '0')]]
        for rows in bad_pages:
            with self.subTest(rows=len(rows)):
                self.fetch = lambda *_: rows
                s = self.service()
                self.ready(s)
                self.assertFalse(s.check('TESTUSDT', BASE + 3600000, Decimal('1000')))

    def test_unknown_or_future_listing_does_not_query_or_allow(self):
        s = self.service()
        for start in [0, -1, None, self.now + DAY]:
            self.assertFalse(s.check('TESTUSDT', start, Decimal('1000')))
        self.assertFalse(s.process_one())
        self.assertEqual(self.calls, [])

    def test_stale_and_api_failure_never_reuse_old_verified_value(self):
        s = self.service()
        self.ready(s)
        self.now += 61000
        self.assertFalse(s.check('TESTUSDT', BASE + 3600000, Decimal('14')))
        s.fetch_page = lambda *_: (_ for _ in ()).throw(RuntimeError('API unavailable'))
        self.assertTrue(s.process_one())
        self.assertFalse(s.check('TESTUSDT', BASE + 3600000, Decimal('14')))

    def test_cache_survives_restart_and_refresh_overlaps_previous_live_day(self):
        s = self.service()
        self.ready(s)
        count = len(self.calls)
        restored = self.service()
        self.assertTrue(restored.check('TESTUSDT', BASE + 3600000, Decimal('14')))
        self.assertEqual(len(self.calls), count)
        self.now += DAY
        self.history[BASE + 2101 * DAY] = candle(BASE + 2101 * DAY, '8')
        self.assertFalse(restored.check('TESTUSDT', BASE + 3600000, Decimal('14')))
        self.assertTrue(restored.process_one())
        self.assertEqual(self.calls[-1][0], BASE + 2100 * DAY)
        self.assertTrue(restored.check('TESTUSDT', BASE + 3600000, Decimal('10.41')))
        self.assertFalse(restored.check('TESTUSDT', BASE + 3600000, Decimal('10.4')))

    def test_changed_listing_identity_requires_full_rescan(self):
        s = self.service()
        self.ready(s)
        self.assertFalse(s.check('TESTUSDT', BASE + DAY, Decimal('14')))
        self.assertTrue(s.process_one())
        self.assertEqual(self.calls[-3][0], BASE + DAY)

    def test_corrupt_cache_does_not_allow_without_refresh(self):
        self.path.write_text('{broken')
        self.assertFalse(self.service().check('TESTUSDT', BASE, Decimal('14')))

    def test_current_new_all_time_low_is_blocked_even_before_daily_refresh(self):
        s = self.service()
        self.ready(s)
        self.assertFalse(s.check('TESTUSDT', BASE + 3600000, Decimal('9')))

    def test_fetch_does_not_happen_on_trading_thread(self):
        s = self.service()
        self.assertFalse(s.check('TESTUSDT', BASE, Decimal('14')))
        self.assertEqual(self.calls, [])
        s.check('TESTUSDT', BASE, Decimal('14'))
        self.assertTrue(s.process_one())
        self.assertFalse(s.process_one())

    def test_order_guard_covers_new_and_additional_short_but_not_close(self):
        from coin_rising_short import orders, client, filters
        price_patches = []
        if hasattr(client, 'get_ticker_price'):
            price_patches.append(patch.object(client, 'get_ticker_price', return_value=Decimal('13')))
        else:
            price_patches.extend([patch.object(client, '_http_get'), patch.object(client, 'parse_json_response', return_value={'price': '13'})])
        from contextlib import ExitStack
        with ExitStack() as stack:
            for item in price_patches:
                stack.enter_context(item)
            stack.enter_context(patch.object(orders, 'ensure_leverage'))
            stack.enter_context(patch.object(self.mod, 'allow_short', return_value=False))
            send = stack.enter_context(patch.object(orders, 'place_limit_order'))
            stack.enter_context(patch.object(filters, 'get_price_step_and_qty_step', return_value=(Decimal('.01'), Decimal('.01'), Decimal('.01'), Decimal('0'))))
            self.assertIsNone(orders.place_short_order('TESTUSDT'))
            self.assertIsNone(orders.place_short_order('TESTUSDT', Decimal('100')))
            send.assert_not_called()

    def test_blocked_entry_does_not_consume_supertrend_flip(self):
        from coin_rising_short import monitor, runtime, config
        runtime.QUALIFIED_WATCH['TESTUSDT'] = {'last_direction': 1}
        self.addCleanup(runtime.QUALIFIED_WATCH.pop, 'TESTUSDT', None)
        def signal(symbol):
            runtime.QUALIFIED_WATCH[symbol]['last_direction'] = -1
            return True, 'flip'
        with patch.object(config, 'USE_SUPERTREND_ENTRY', True), patch.object(monitor.positions, 'at_position_capacity', return_value=False), patch.object(monitor.indicators, 'is_supertrend_short_signal', side_effect=signal), patch.object(monitor.orders, 'place_short_order', return_value=None), patch.object(self.mod, 'was_blocked', return_value=True), patch.object(monitor.state, 'save_qualified_watch'):
            if hasattr(monitor, '_collect_open_sell_symbols'):
                with patch.object(monitor, '_collect_open_sell_symbols', return_value=set()):
                    monitor._try_initial_short_entry('TESTUSDT', {})
            else:
                monitor._try_initial_short_entry('TESTUSDT', {})
        self.assertEqual(runtime.QUALIFIED_WATCH['TESTUSDT']['last_direction'], 1)


    def test_close_is_not_subject_to_atl_gate(self):
        from coin_rising_short import orders, client, filters
        from contextlib import ExitStack
        from unittest.mock import Mock
        with ExitStack() as stack:
            gate = stack.enter_context(patch.object(self.mod, 'allow_short', return_value=False))
            stack.enter_context(patch.object(filters, 'get_price_step_and_qty_step', return_value=(Decimal('.01'), Decimal('.01'), Decimal('.01'), Decimal('0'))))
            if hasattr(client, 'place_market_order_raw'):
                stack.enter_context(patch.object(client, 'place_market_order_raw', return_value=('456', None)))
                expected = '456'
            else:
                response = Mock(status_code=200, text='json')
                response.json.return_value = {'orderId': 456}
                stack.enter_context(patch.object(client, 'signed_request', return_value=response))
                expected = 456
            self.assertEqual(orders.close_short_position('TESTUSDT', Decimal('2')), expected)
            gate.assert_not_called()

    def test_public_adapter_requests_explicit_full_window(self):
        from coin_rising_short import client
        from unittest.mock import Mock
        rows = [candle(BASE)]
        if self.mod.SOURCE == 'binance_usdm':
            response = Mock(status_code=200)
            with patch.object(client, '_http_get', return_value=response) as get, patch.object(client, 'parse_json_response', return_value=rows):
                self.assertEqual(self.mod._fetch_page('TESTUSDT', BASE, BASE + DAY - 1), rows)
            params = get.call_args.kwargs['params']
            self.assertEqual((params['startTime'], params['endTime'], params['limit'], params['interval']), (BASE, BASE + DAY - 1, 1000, '1d'))
        else:
            with patch.object(client, 'public_get', return_value={'symbol': 'TESTUSDT', 'list': rows}) as get:
                self.assertEqual(self.mod._fetch_page('TESTUSDT', BASE, BASE + DAY - 1), rows)
            params = get.call_args.args[1]
            self.assertEqual((params['start'], params['end'], params['limit'], params['interval']), (BASE, BASE + DAY - 1, 1000, 'D'))

    def test_disabled_filter_does_not_start_history_worker(self):
        from coin_rising_short import config
        with patch.object(config, 'ATL_FILTER_ENABLED', False), patch.object(self.mod, '_service', None):
            self.assertTrue(self.mod.allow_short('TESTUSDT', Decimal('1')))
            self.assertIsNone(self.mod._service)


if __name__ == '__main__':
    unittest.main()
