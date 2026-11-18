# Dip-buy cbBTC bot — make.fun BTC 24h race (2026-11-18)

A bot that trades Coinbase Wrapped BTC (cbBTC) against USDC, minute by minute,
on 1-minute on-chain bars from its Orca Whirlpool. It waits for a sharp
intraday flush, buys it once at full size, and sells the recovery.

## Strategy

- **Buy the dip**: the close sits at least 1.20% below the trailing 60-bar
  high, fell at least 0.50% over the last 15 bars, and the last 5 bars stopped
  falling. A second buy needs a deeper flush (1.50%). At most 2 dip buys.
- **Sell the recovery**: take profit at +2.00% from entry, hard stop at
  −1.50%, trailing stop once +1.00% up (sell 1.00% below the peak close),
  8-hour time cap.
- **Fallback**: if the bot never traded by bar 1080, one trend entry on a
  fresh 60-bar high, so it still makes at least one trade. No new position
  after bar 1380.

Everything is integer arithmetic on bar closes — no clock, no network, no
randomness — so two runs on the same bars answer byte for byte the same.

## Local verification (replay.py, exact re-implementation of the kit's replay)

| Window | Net PnL | Trades | Max drawdown | 2x fees |
|---|---|---|---|---|
| New dev window (this race) | +$17.84 | 2 | 1.50% | +$17.02 |
| Old dev window (Nov 17 race) | +$12.20 | 2 | 0.62% | +$11.39 |
| Old race window (Nov 17 final) | +$17.84 | 2 | 1.50% | +$17.02 |

The 1.20% dip threshold is the change from our Nov 17 entry: the Nov 17 race
final showed a −1.0% dip that kept falling and cost −$17.61, while the real
flush was −3.2% deep. Requiring a deeper flush skips the noise dips; the
1.00% version lost money on the old race window (−$0.04), this version is
profitable on all three windows we can test.

## Files

- `main.py` — the bot (Python 3.13, standard library only)
- `vendor/makefun_bars.py` — the kit's Python SDK shim, byte-identical

## License

MIT — see `LICENSE`.
