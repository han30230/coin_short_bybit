"""실행 시점에 설정되는 값 (예: Hedge 모드)."""

IS_HEDGE = False

# 심볼별 일시 스킵(거래소 오픈 금지/점검 등)
# key: symbol, value: unix epoch seconds until which to skip
SKIP_UNTIL: dict[str, int] = {}

# 급등+지표 등 1차 진입 조건을 통과한 종목 (SuperTrend 숏 신호 대기)
# symbol -> {"added_at": float, "last_direction": int | None}
QUALIFIED_WATCH: dict[str, dict] = {}
