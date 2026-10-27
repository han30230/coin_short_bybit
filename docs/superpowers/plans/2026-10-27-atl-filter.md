# Full-history ATL short-entry filter implementation plan

Goal: Only the Binance and Bybit short-only bots block new/additional shorts when price <= ATL * 1.3.
Source: each exchange's current futures contract listing history, not a rolling lookback.

- [x] Write failing tests for >1 page history, earliest low, equality, gaps, corrupt data,
  missing listing dates, stale/API failures, disk cache, day rollover, nonblocking queries,
  actual short-order guard and ST flip preservation.
- [x] Implement bounded-page full-history validation in a daemon worker; fail closed until verified.
- [x] Cache verified coverage and overlapping incremental updates, isolate data per exchange/environment.
- [x] Integrate only at short-entry submission; preserve exits and blocked ST signals.
- [x] Document default-enabled settings and limitations.
- [x] Run full suites, inspect diffs and obtain independent review before publishing with a branch head lease.

No orders are placed by the tests. The ST long/short repository is outside this change.
