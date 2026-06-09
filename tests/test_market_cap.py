import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from coin_rising_short import config, monitor
from coin_rising_short.market_cap import (
    clear_mcap_cache,
    get_market_cap_usd,
    normalize_binance_symbol,
)


class TestNormalizeBinanceSymbol(unittest.TestCase):
    def test_btc_usdt(self) -> None:
        self.assertEqual(normalize_binance_symbol("BTCUSDT"), "BTC")

    def test_1000_pepe_usdt(self) -> None:
        self.assertEqual(normalize_binance_symbol("1000PEPEUSDT"), "PEPE")

    def test_eth(self) -> None:
        self.assertEqual(normalize_binance_symbol("ETHUSDT"), "ETH")


class TestMcapCache(unittest.TestCase):
    def setUp(self) -> None:
        clear_mcap_cache()

    def tearDown(self) -> None:
        clear_mcap_cache()

    @patch("coin_rising_short.market_cap._fetch_market_cap_usd_from_cmc")
    @patch("coin_rising_short.market_cap.time.time", side_effect=[1000.0, 1005.0])
    def test_cache_avoids_second_api_call_within_ttl(self, mock_time: MagicMock, mock_fetch: MagicMock) -> None:
        mock_fetch.return_value = Decimal("250000000")
        with patch.object(config, "MCAP_FILTER_ENABLED", True), patch.object(config, "CMC_API_KEY", "fake-key"):
            a = get_market_cap_usd("BTCUSDT")
            b = get_market_cap_usd("BTCUSDT")
        self.assertEqual(a, Decimal("250000000"))
        self.assertEqual(b, Decimal("250000000"))
        self.assertEqual(mock_fetch.call_count, 1)


class TestMonitorQualifiedMcap(unittest.TestCase):
    def setUp(self) -> None:
        clear_mcap_cache()

    def tearDown(self) -> None:
        clear_mcap_cache()

    def _ticker_row(self) -> dict:
        return {
            "symbol": "BTCUSDT",
            "price24hPcnt": "0.25",
            "turnover24h": "5000000",
            "lastPrice": "50000",
        }

    @patch("coin_rising_short.monitor.symbols.TRADING_SYMBOLS", {"BTCUSDT": {}})
    @patch("coin_rising_short.monitor.client.get_linear_tickers")
    def test_mcap_below_min_excluded_from_qualified(
        self, mock_tickers: MagicMock,
    ) -> None:
        mock_tickers.return_value = [self._ticker_row()]
        funding = {"BTCUSDT": Decimal("0")}

        with patch.object(config, "MCAP_FILTER_ENABLED", True), patch.object(
            config, "FILTER_MCAP_FDV", False
        ), patch.object(config, "MIN_MARKET_CAP_USD", Decimal("1000000")), patch(
            "coin_rising_short.monitor.market_cap.get_market_cap_usd", return_value=Decimal("500000")
        ):
            qualified, _top = monitor.get_futures_gainers_and_top_movers(funding)

        self.assertEqual(qualified, [])

    @patch("coin_rising_short.monitor.symbols.TRADING_SYMBOLS", {"BTCUSDT": {}})
    @patch("coin_rising_short.monitor.client.get_linear_tickers")
    def test_no_cmc_key_behaves_like_before_no_mcap_call(
        self, mock_tickers: MagicMock,
    ) -> None:
        mock_tickers.return_value = [self._ticker_row()]
        funding = {"BTCUSDT": Decimal("0")}

        with patch.object(config, "MCAP_FILTER_ENABLED", False), patch(
            "coin_rising_short.monitor.market_cap.get_market_cap_usd"
        ) as mock_mcap:
            qualified, _top = monitor.get_futures_gainers_and_top_movers(funding)

        mock_mcap.assert_not_called()
        self.assertEqual(len(qualified), 1)
        self.assertEqual(qualified[0]["symbol"], "BTCUSDT")
        self.assertNotIn("market_cap_usd", qualified[0])

    @patch("coin_rising_short.monitor.symbols.TRADING_SYMBOLS", {"BTCUSDT": {}})
    @patch("coin_rising_short.monitor.client.get_linear_tickers")
    def test_mcap_meets_min_includes_field(
        self, mock_tickers: MagicMock,
    ) -> None:
        mock_tickers.return_value = [self._ticker_row()]
        funding = {"BTCUSDT": Decimal("0")}
        cap = Decimal("150000000")

        with patch.object(config, "MCAP_FILTER_ENABLED", True), patch.object(
            config, "FILTER_MCAP_FDV", False
        ), patch.object(config, "MIN_MARKET_CAP_USD", Decimal("1000000")), patch(
            "coin_rising_short.monitor.market_cap.get_market_cap_usd", return_value=cap
        ):
            qualified, _top = monitor.get_futures_gainers_and_top_movers(funding)

        self.assertEqual(len(qualified), 1)
        self.assertEqual(qualified[0].get("market_cap_usd"), cap)

    @patch("coin_rising_short.monitor.symbols.TRADING_SYMBOLS", {"BTCUSDT": {}})
    @patch("coin_rising_short.monitor.client.get_linear_tickers")
    def test_mcap_fetch_failed_still_qualified_when_fail_open(
        self, mock_tickers: MagicMock,
    ) -> None:
        mock_tickers.return_value = [self._ticker_row()]
        funding = {"BTCUSDT": Decimal("0")}

        with patch.object(config, "MCAP_FILTER_ENABLED", True), patch.object(
            config, "MCAP_FAIL_OPEN", True
        ), patch.object(config, "FILTER_MCAP_FDV", False), patch(
            "coin_rising_short.monitor.market_cap.get_market_cap_usd", return_value=None
        ):
            qualified, _top = monitor.get_futures_gainers_and_top_movers(funding)

        self.assertEqual(len(qualified), 1)
        self.assertNotIn("market_cap_usd", qualified[0])


if __name__ == "__main__":
    unittest.main()
