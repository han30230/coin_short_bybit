import json
import logging
import os
from decimal import Decimal
from typing import Any, Dict

from coin_rising_short import config, runtime

position_state: Dict[str, Dict[str, Any]] = {}
logger = logging.getLogger(__name__)


def _sanitize_for_json(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(x) for x in obj]
    return obj


def _convert_loaded_state(obj: Any) -> Any:
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            if k in ("entry_price", "qty") and isinstance(v, (str, int, float)):
                out[k] = Decimal(str(v))
            else:
                out[k] = _convert_loaded_state(v)
        return out
    if isinstance(obj, list):
        return [_convert_loaded_state(x) for x in obj]
    return obj


def load_position_state() -> None:
    global position_state
    if not os.path.isfile(config.POSITION_STATE_PATH):
        position_state = {}
        return
    try:
        with open(config.POSITION_STATE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            position_state = {}
            return
        position_state = _convert_loaded_state(raw)
    except Exception as e:
        logger.warning("상태 파일 로드 실패, 빈 상태로 시작: %s", e)
        position_state = {}


def save_position_state() -> None:
    try:
        path = config.POSITION_STATE_PATH
        tmp = path + ".tmp"
        data = _sanitize_for_json(position_state)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning("상태 파일 저장 실패: %s", e)


def load_qualified_watch() -> None:
    """재시작 후 ST 감시 목록·last_direction 복원."""
    path = config.SUPERTREND_WATCH_STATE_PATH
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return
        restored = 0
        for symbol, entry in raw.items():
            if symbol in position_state:
                continue
            if not isinstance(entry, dict):
                continue
            last_direction = entry.get("last_direction")
            if last_direction is not None:
                try:
                    last_direction = int(last_direction)
                except (TypeError, ValueError):
                    last_direction = None
            runtime.QUALIFIED_WATCH[symbol] = {
                "added_at": float(entry.get("added_at", 0)),
                "last_direction": last_direction,
            }
            restored += 1
        if restored:
            logger.info(
                "SuperTrend 감시 목록 복원: %s개 (last_direction 유지, %s)",
                restored,
                path,
                extra={"event": "supertrend_watch_restored", "count": restored},
            )
    except Exception as e:
        logger.warning("ST 감시 파일 로드 실패: %s", e)


def save_qualified_watch() -> None:
    if not config.USE_SUPERTREND_ENTRY:
        return
    try:
        path = config.SUPERTREND_WATCH_STATE_PATH
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(runtime.QUALIFIED_WATCH, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning("ST 감시 파일 저장 실패: %s", e)
