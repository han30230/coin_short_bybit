"""진입점 — 프로젝트 루트에서 `python Bybit_SH_1.py` 실행."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)

# config import 전에 계정별 state/logs·API 키 고정 (다른 폴더·다른 계정과 분리)
if not os.getenv("BOT_API_KEY"):
    os.environ["BOT_API_KEY"] = (
        os.getenv("BYBIT_API_KEY_SH") or os.getenv("BYBIT_API_KEY") or ""
    )
    os.environ["BOT_API_SECRET"] = (
        os.getenv("BYBIT_SECRET_SH") or os.getenv("BYBIT_SECRET") or ""
    )

if not os.getenv("POSITION_STATE_FILE"):
    state_dir = ROOT / "state" / "sh"
    log_dir = ROOT / "logs" / "sh"
    state_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("BOT_ACCOUNT", "sh")
    os.environ["POSITION_STATE_FILE"] = str(state_dir / "position_state.json")
    os.environ["SUPERTREND_WATCH_STATE_FILE"] = str(state_dir / "supertrend_watch.json")
    os.environ["TRADE_JOURNAL_FILE"] = str(log_dir / "trade_journal.csv")
    os.environ["BOT_LOG_FILE"] = str(log_dir / "bot.log")

from coin_rising_short.main import run

if __name__ == "__main__":
    run()
