"""Dip-buy mean-reversion bot for the BTC 24h race (agent-fun/bars-replay/v1).

Bitcoin flushes hard intraday and then recovers, so the bot waits for a sharp
flush, buys it once at full size, and sells the recovery. The hello message
carries the universe and the capital; each batch carries one bar and the fills
of the previous reply's orders, and the bot answers every batch with its orders.

A round trip costs about 26 bps in fees, slippage and impact, so the bot only
trades when the setup is worth more than that: at most two dip buys a day, no
chasing, no re-entry on the same dip. If no dip ever comes, one late trend entry
keeps the bot from sitting out the whole window.

Rules, all in basis points (1 bp = 0.01%):
  buy  - the close sits at least 1.20% below the trailing 60-bar high, fell at
         least 0.50% over the last 15 bars (a flush, not a slow drift), and the
         last 5 bars stopped falling (down less than 0.05%). A second buy needs
         a deeper flush, at least 1.50% below the high.
  sell - take profit at +2.00% from entry; hard stop at -1.50%; a trailing stop
         arms once the trade is +1.00% up and sells 1.00% below the peak close;
         a time cap sells 480 bars (8 hours) after entry.
  fall - if the bot never traded by bar 1080, buy a fresh 60-bar high after a
         quiet drift up (60-bar return at least +0.40%), so it still trades.

Everything is integer arithmetic on the bar closes, no clock, no network, no
randomness, so two runs on the same bars answer byte for byte the same.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# Entry: how far the close must sit below the trailing 60-bar high.
# 1.20%, not 1.00%: the Nov 17 race final showed a -1.0% dip that kept
# falling and cost -$17.61, while the real flush was -3.2% deep. Requiring
# a deeper flush skips the noise dips; the same set is profitable on all
# three windows we can test (new dev +$17.84, old dev +$12.20, old race
# +$17.84), where the 1.00% version lost money on the old race window.
DIP_BPS = 120
# A second dip buy must be deeper than the first.
DEEP_BPS = 150
# The flush must be fast: at least this much down over the last 15 bars,
# unless the dip is already deep.
FAST_BPS = 50
# Stabilisation: the last 5 bars must not still be falling fast.
STAB_BPS = 5
# Exits, measured from the entry fill price.
TAKE_BPS = 200
STOP_BPS = 150
TRAIL_ARM_BPS = 100
TRAIL_GIVE_BPS = 100
# Bars held before the time cap sells (8 hours).
TIME_CAP = 480
# Fallback trend entry, only if the bot never traded.
FALLBACK_BAR = 1080
FALLBACK_RUN_BPS = 40
# No new position in the last hour of the window: a buy that late cannot
# mature before the final flatten, so it would only pay the round trip.
NO_BUY_AFTER_BAR = 1380
# At most this many dip buys per window.
MAX_DIP_TRADES = 2
# Wide enough to survive a fast bar, tight enough to refuse a bad fill.
MAX_SLIPPAGE_BPS = 150


def _bps(now, ref):
    """(now / ref - 1) in basis points, integer math, negative when now < ref."""
    return (now - ref) * 10_000 // ref


class DipBot:
    def __init__(self):
        self.closes = []
        self.entry = None       # fill price of the open position, or None
        self.entry_bar = None   # seq of the batch the buy fill arrived on
        self.peak = None        # highest close since entry
        self.trades = 0         # filled buys so far

    def on_batch(self, state, batch):
        if state.warming_up:
            return []
        for fill in batch["fills"]:
            if fill["status"] != "filled":
                continue
            if fill["side"] == "buy":
                self.trades += 1
                self.entry = int(fill["price"])
                self.entry_bar = state.seq
                self.peak = self.entry
            elif fill["side"] == "sell":
                self.entry = None
                self.entry_bar = None
                self.peak = None
        bar = state.latest.get(SYMBOL)
        if bar is None:
            return []
        close = int(bar["c"])
        self.closes.append(close)
        holding = state.positions.get(SYMBOL)
        if holding is None:
            self.entry = None
            self.entry_bar = None
            self.peak = None
            return self._maybe_buy(state)
        if self.entry is None:
            # Position open but no fill seen (should not happen): anchor on the
            # close so the exit rules still have a reference.
            self.entry = close
            self.entry_bar = state.seq
            self.peak = close
        if close > self.peak:
            self.peak = close
        return self._maybe_sell(state, close, holding)

    def _maybe_buy(self, state):
        closes = self.closes
        if len(closes) < 61 or state.cash < 1_000_000:
            return []
        close = closes[-1]
        high = max(closes[-61:-1])
        dip = _bps(close, high)
        scored_bar = state.seq - state.warmup_bars
        if scored_bar > NO_BUY_AFTER_BAR:
            return []
        if self.trades < MAX_DIP_TRADES:
            need = DIP_BPS if self.trades == 0 else DEEP_BPS
            fast = _bps(close, closes[-16]) <= -FAST_BPS
            settled = _bps(close, closes[-6]) >= -STAB_BPS
            if dip <= -need and (fast or dip <= -DEEP_BPS) and settled:
                return [{"side": "buy", "symbol": SYMBOL,
                         "notional_quote_units": state.cash,
                         "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        if self.trades == 0 and scored_bar >= FALLBACK_BAR:
            run = _bps(close, closes[-61])
            if close >= high and run >= FALLBACK_RUN_BPS:
                return [{"side": "buy", "symbol": SYMBOL,
                         "notional_quote_units": state.cash,
                         "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        return []

    def _maybe_sell(self, state, close, holding):
        gain = _bps(close, self.entry)
        if gain >= TAKE_BPS or gain <= -STOP_BPS:
            return self._sell_all(holding)
        if _bps(self.peak, self.entry) >= TRAIL_ARM_BPS and _bps(close, self.peak) <= -TRAIL_GIVE_BPS:
            return self._sell_all(holding)
        if state.seq - self.entry_bar >= TIME_CAP:
            return self._sell_all(holding)
        return []

    def _sell_all(self, holding):
        return [{"side": "sell", "symbol": SYMBOL,
                 "qty": holding.qty,
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]


def main():
    bot = DipBot()
    makefun_bars.run(bot.on_batch)


if __name__ == "__main__":
    main()
