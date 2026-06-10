import unittest
from decimal import Decimal
from unittest.mock import patch

from coin_rising_short import config, runtime
from coin_rising_short.indicators import _supertrend_directions, is_supertrend_short_signal


class TestSupertrendDirection(unittest.TestCase):
    def tearDown(self) -> None:
        runtime.QUALIFIED_WATCH.clear()

    def test_entry_on_flip_from_long_to_short(self) -> None:
        directions = [1] * 18 + [-1, -1]
        n = len(directions)
        runtime.QUALIFIED_WATCH["BTCUSDT"] = {"added_at": 0, "last_direction": 1}
        with patch.object(config, "SUPERTREND_ATR_PERIOD", 4), patch(
            "coin_rising_short.indicators._supertrend_directions",
            return_value=directions,
        ), patch(
            "coin_rising_short.indicators._get_closed_ohlc",
            return_value=(
                {
                    "highs": [Decimal("1")] * n,
                    "lows": [Decimal("1")] * n,
                    "closes": [Decimal("1")] * n,
                },
                "",
            ),
        ):
            ok, reason = is_supertrend_short_signal("BTCUSDT")
        self.assertTrue(ok)
        self.assertIn("supertrend_short_flip", reason)
        self.assertEqual(runtime.QUALIFIED_WATCH["BTCUSDT"]["last_direction"], -1)

    def test_no_entry_when_already_downtrend_on_watch(self) -> None:
        directions = [-1] * 20
        n = len(directions)
        runtime.QUALIFIED_WATCH["ETHUSDT"] = {"added_at": 0, "last_direction": None}
        with patch(
            "coin_rising_short.indicators._supertrend_directions",
            return_value=directions,
        ), patch(
            "coin_rising_short.indicators._get_closed_ohlc",
            return_value=(
                {
                    "highs": [Decimal("1")] * n,
                    "lows": [Decimal("1")] * n,
                    "closes": [Decimal("1")] * n,
                },
                "",
            ),
        ):
            ok, reason = is_supertrend_short_signal("ETHUSDT")
        self.assertFalse(ok)
        self.assertIn("1→-1 전환 대기", reason)
        self.assertEqual(runtime.QUALIFIED_WATCH["ETHUSDT"]["last_direction"], -1)

    def test_no_entry_while_stays_downtrend(self) -> None:
        directions = [-1] * 20
        n = len(directions)
        runtime.QUALIFIED_WATCH["XRPUSDT"] = {"added_at": 0, "last_direction": -1}
        with patch(
            "coin_rising_short.indicators._supertrend_directions",
            return_value=directions,
        ), patch(
            "coin_rising_short.indicators._get_closed_ohlc",
            return_value=(
                {
                    "highs": [Decimal("1")] * n,
                    "lows": [Decimal("1")] * n,
                    "closes": [Decimal("1")] * n,
                },
                "",
            ),
        ):
            ok, _ = is_supertrend_short_signal("XRPUSDT")
        self.assertFalse(ok)

    def test_no_signal_when_stays_bullish(self) -> None:
        directions = [1] * 20
        runtime.QUALIFIED_WATCH["ETHUSDT"] = {"added_at": 0, "last_direction": None}
        with patch(
            "coin_rising_short.indicators._supertrend_directions",
            return_value=directions,
        ), patch(
            "coin_rising_short.indicators._get_closed_ohlc",
            return_value=(
                {
                    "highs": [Decimal("1")] * 20,
                    "lows": [Decimal("1")] * 20,
                    "closes": [Decimal("1")] * 20,
                },
                "",
            ),
        ):
            ok, reason = is_supertrend_short_signal("ETHUSDT")
        self.assertFalse(ok)
        self.assertIn("flip 1→-1", reason)

    def test_supertrend_produces_valid_directions(self) -> None:
        n = 40
        highs, lows, closes = [], [], []
        for i in range(n):
            p = Decimal(str(100 + i))
            highs.append(p + Decimal("5"))
            lows.append(p - Decimal("5"))
            closes.append(p)
        closes[-1] = Decimal("70")
        lows[-1] = Decimal("65")
        highs[-1] = Decimal("75")

        directions = _supertrend_directions(
            highs, lows, closes, 10, Decimal("3")
        )
        self.assertTrue(all(d in (1, -1) for d in directions))
        self.assertGreater(len(directions), 15)


if __name__ == "__main__":
    unittest.main()
