"""Dip-buy cbBTC bot for make.fun's 24-hour BTC race.

The idea is simple: Bitcoin flushes hard inside the day and then recovers,
so the bot waits for a sharp flush, buys it at full size, and sells the
recovery. It makes at most two dip buys a day and otherwise sits in cash.

All decisions use integer arithmetic on bar closes. No clock, no network, no
randomness, nothing read from the environment, so two runs on the same bars
answer byte for byte the same.

Rules (all thresholds in basis points, 1 bp = 0.01%):
- Buy the dip: the close is at least 120 bps below the trailing 60-bar
  high, fell at least 50 bps over the last 15 bars (or is already 150 bps
  deep), and the last 5 bars stopped falling (down less than 5 bps). A
  second dip buy needs a deeper flush, 150 bps below the high.
- Sell the recovery: take profit at +200 bps from the entry close, hard
  stop at -150 bps, trailing stop once +100 bps up (sell 100 bps below the
  peak close), or 8 hours after entry, whichever comes first.
- Fallback: if the bot never traded by bar 1080, one trend entry on a fresh
  60-bar high after a 60-bar run of at least +40 bps, so a flat window still
  leaves it with a trade. No new position after bar 1380.

Speaks agent-fun/bars-replay/v1 through the kit's Python shim.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# How far back the trailing high looks, in bars.
HIGH_BARS = 60
# First dip buy: close this far below the trailing high.
FIRST_DIP_BPS = 120
# Second dip buy: only on a deeper flush than the first.
SECOND_DIP_BPS = 150
# A fast flush: this far down over the last 15 bars.
FLUSH_BARS = 15
FLUSH_BPS = 50
# Settled: the last 5 bars down less than this.
SETTLE_BARS = 5
SETTLE_BPS = 5
# Exits, from the entry close.
TAKE_BPS = 200
STOP_BPS = 150
# Trailing stop: arms this far up, sells this far below the peak close.
TRAIL_ARM_BPS = 100
TRAIL_GIVE_BPS = 100
# Time cap, in bars (8 hours).
TIME_CAP_BARS = 480
# Fallback window: first trend entry no earlier than this bar, none after this one.
FALLBACK_BAR = 1080
LAST_ENTRY_BAR = 1380
# Fallback needs a 60-bar run of at least this.
FALLBACK_RUN_BPS = 40
# Max slippage the harness may move a fill from the latest close.
SLIPPAGE_BPS = 150


def _drop_bps(high, close):
    """How far below `high` the `close` sits, in basis points."""
    return (high - close) * 10_000 // high


def _move_bps(earlier, later):
    """The move from `earlier` to `later`, in basis points (negative when down)."""
    return (later - earlier) * 10_000 // earlier


class DipTrader:
    """The strategy's memory: closes seen, the open position, pending orders."""

    def __init__(self):
        self.closes = []
        self.entry_close = None
        self.entry_seq = None
        self.peak_close = None
        self.pending = None
        self.dip_buys = 0
        self.fallback_done = False

    def holding(self, state):
        return SYMBOL in state.positions

    def note_close(self, close):
        self.closes.append(close)

    def settle_pending(self, state):
        """A buy sent on an earlier batch has filled or been rejected by now."""
        if self.pending is None or state.seq <= self.pending[1]:
            return
        close, _seq, kind = self.pending
        self.pending = None
        if self.holding(state):
            self.entry_close = close
            self.entry_seq = _seq
            self.peak_close = close
            if kind == "dip":
                self.dip_buys += 1

    def dip_signal(self, close):
        """True when the flush is deep and fast enough and has stopped falling."""
        if len(self.closes) < HIGH_BARS + 1:
            return False
        high = max(self.closes[-HIGH_BARS:])
        needed = FIRST_DIP_BPS if self.dip_buys == 0 else SECOND_DIP_BPS
        drop = _drop_bps(high, close)
        if drop < needed:
            return False
        flush = _move_bps(self.closes[-FLUSH_BARS - 1], close)
        if flush < -FLUSH_BPS and drop < SECOND_DIP_BPS:
            return False
        settled = _move_bps(self.closes[-SETTLE_BARS - 1], close)
        return settled > -SETTLE_BPS

    def fallback_signal(self, close):
        """True on a fresh 60-bar high after a 60-bar run, once only."""
        if self.fallback_done or self.dip_buys > 0:
            return False
        if len(self.closes) < HIGH_BARS + 1:
            return False
        if close <= max(self.closes[-HIGH_BARS - 1:-1]):
            return False
        run = _move_bps(self.closes[-HIGH_BARS - 1], close)
        return run >= FALLBACK_RUN_BPS

    def exit_now(self, state, close):
        """Which exit, if any, the current close triggers."""
        if self.entry_close is None:
            return None
        if close > self.peak_close:
            self.peak_close = close
        if _move_bps(self.entry_close, close) >= TAKE_BPS:
            return "take-profit"
        if _move_bps(close, self.entry_close) >= STOP_BPS:
            return "hard-stop"
        if self.peak_close * 10_000 >= self.entry_close * (10_000 + TRAIL_ARM_BPS):
            if close * 10_000 <= self.peak_close * (10_000 - TRAIL_GIVE_BPS):
                return "trailing-stop"
        if state.seq - self.entry_seq >= TIME_CAP_BARS:
            return "time-cap"
        return None

    def decide(self, state, batch):
        close = state.close(SYMBOL)
        if close is None or state.warming_up:
            return []
        self.note_close(close)
        self.settle_pending(state)
        if self.holding(state):
            if self.exit_now(state, close) is not None:
                held = state.positions[SYMBOL]
                self.entry_close = None
                self.entry_seq = None
                self.peak_close = None
                return [{"side": "sell", "symbol": SYMBOL, "qty": held.qty,
                         "max_slippage_bps": SLIPPAGE_BPS}]
            return []
        if state.seq > LAST_ENTRY_BAR:
            return []
        kind = None
        if self.dip_buys < 2 and self.dip_signal(close):
            kind = "dip"
        elif state.seq >= FALLBACK_BAR and self.fallback_signal(close):
            kind = "fallback"
        if kind is None:
            return []
        self.pending = (close, state.seq, kind)
        if kind == "fallback":
            self.fallback_done = True
        return [{"side": "buy", "symbol": SYMBOL,
                 "notional_quote_units": state.cash,
                 "max_slippage_bps": SLIPPAGE_BPS}]


_TRADER = DipTrader()


def on_batch(state, batch):
    return _TRADER.decide(state, batch)


makefun_bars.run(on_batch)
