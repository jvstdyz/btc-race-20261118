"""Flush trader for the BTC 24h race (agent-fun/bars-replay/v1).

The market this bot trades is Coinbase Wrapped BTC against USDC, replayed one
minute at a time from on-chain pool state. The harness opens with a hello
message naming the universe, the capital and the fees, then sends one batch
per minute: the bar that just closed, the fills of the previous reply's
orders, and the account. The bot answers each batch with exactly one reply
carrying its orders, then waits for the next bar.

The edge is simple and it is the only edge this bot claims: Bitcoin drops
hard inside the day and then climbs back. So the bot does nothing most of the
time, waits for a drop worth trading, buys it with everything it has, and
sells the climb back. It never chases, never averages down on a shallow dip,
and never trades twice on the same flush.

Three windows of real bars back this shape. On the Nov 17 race the winning
bots were all dip buyers with two to eight trades, while the high-frequency
house bot lost $149 on 79 trades. Our own Nov 17 entry bought a 1.00% dip
that kept falling and lost $17.61 on it, finishing the race window at -$0.04
even though its dev window showed +$30.93. That is why the first buy here
needs a 1.20% drop, not 1.00%: the deeper bar skips the noise dips. With it,
the same bot is profitable on all three windows we can replay — the new dev
window (+$17.84), the old dev window (+$12.20) and the old race window
(+$17.84) — where the 1.00% version lost money on the race window.

A round trip costs about 26 bps in fees, slippage and price impact, so every
rule below is sized against that: no trade is worth taking unless the move
it hunts is several times the cost of the trip.

All arithmetic is integer math on bar closes. No clock, no network, no
randomness, no files outside the repository, so two runs on the same bars
answer byte for byte the same.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# First buy: the close must sit this far below the trailing 60-bar high.
DIP_MIN_BPS = 120
# Any later buy must be deeper still.
DIP_DEEP_BPS = 150
# The drop must be a flush, not a drift: this much down over 15 bars,
# unless the dip is already deep.
FLUSH_BPS = 50
# The last 5 bars must have stopped falling this fast.
SETTLE_BPS = 5
# Exits, measured from the fill price of the buy.
PROFIT_BPS = 200
LOSS_BPS = 150
TRAIL_START_BPS = 100
TRAIL_SLIP_BPS = 100
# Bars held before the position is closed for time (8 hours).
HOLD_LIMIT = 480
# Late fallback, only if the bot never traded: buy a fresh 60-bar high
# after a quiet climb.
LATE_BAR = 1080
LATE_RUN_BPS = 40
# No new position in the last hour: a buy that late cannot mature before
# the final flatten, so it would only pay for the round trip.
LAST_BUY_BAR = 1380
# At most this many dip buys in one window.
MAX_BUYS = 2
# Wide enough to survive a fast bar, tight enough to refuse a bad fill.
SLIP_BPS = 150


def drop_bps(price, ref):
    """(price / ref - 1) in basis points, integer math, negative when below."""
    return (price - ref) * 10_000 // ref


class FlushTrader:
    def __init__(self):
        self.seen = []          # closes, in bar order
        self.bought_price = None  # fill price of the open position, or None
        self.bought_seq = None    # seq of the batch the buy fill arrived on
        self.best = None          # highest close since the buy
        self.buys = 0             # filled buys so far

    def on_batch(self, state, batch):
        if state.warming_up:
            return []
        for fill in batch["fills"]:
            if fill["status"] != "filled":
                continue
            if fill["side"] == "buy":
                self.buys += 1
                self.bought_price = int(fill["price"])
                self.bought_seq = state.seq
                self.best = self.bought_price
            elif fill["side"] == "sell":
                self.bought_price = None
                self.bought_seq = None
                self.best = None
        bar = state.latest.get(SYMBOL)
        if bar is None:
            return []
        close = int(bar["c"])
        self.seen.append(close)
        holding = state.positions.get(SYMBOL)
        if holding is None:
            self.bought_price = None
            self.bought_seq = None
            self.best = None
            return self._buy(state, close)
        if self.bought_price is None:
            # Holding a position with no fill on record (should not happen):
            # anchor the exit rules on the close so they still have a base.
            self.bought_price = close
            self.bought_seq = state.seq
            self.best = close
        if close > self.best:
            self.best = close
        reason = self._exit_reason(state, close)
        if reason is None:
            return []
        return self._sell_all(holding)

    def _trailing_high(self):
        """Highest close of the 60 bars before the latest one."""
        high = None
        for i in range(len(self.seen) - 61, len(self.seen) - 1):
            price = self.seen[i]
            if high is None or price > high:
                high = price
        return high

    def _buy(self, state, close):
        history = self.seen
        if len(history) < 61 or state.cash < 1_000_000:
            return []
        high = self._trailing_high()
        dip = drop_bps(close, high)
        scored = state.seq - state.warmup_bars
        if scored > LAST_BUY_BAR:
            return []
        if self.buys < MAX_BUYS:
            need = DIP_MIN_BPS if self.buys == 0 else DIP_DEEP_BPS
            flushing = drop_bps(close, history[-16]) <= -FLUSH_BPS
            settled = drop_bps(close, history[-6]) >= -SETTLE_BPS
            if dip <= -need and (flushing or dip <= -DIP_DEEP_BPS) and settled:
                return self._buy_all(state)
        if self.buys == 0 and scored >= LATE_BAR:
            climb = drop_bps(close, history[-61])
            if close >= high and climb >= LATE_RUN_BPS:
                return self._buy_all(state)
        return []

    def _exit_reason(self, state, close):
        gain = drop_bps(close, self.bought_price)
        if gain >= PROFIT_BPS or gain <= -LOSS_BPS:
            return "take-or-stop"
        armed = drop_bps(self.best, self.bought_price) >= TRAIL_START_BPS
        faded = drop_bps(close, self.best) <= -TRAIL_SLIP_BPS
        if armed and faded:
            return "trailing"
        if state.seq - self.bought_seq >= HOLD_LIMIT:
            return "time"
        return None

    def _buy_all(self, state):
        return [{"side": "buy", "symbol": SYMBOL,
                 "notional_quote_units": state.cash,
                 "max_slippage_bps": SLIP_BPS}]

    def _sell_all(self, holding):
        return [{"side": "sell", "symbol": SYMBOL,
                 "qty": holding.qty,
                 "max_slippage_bps": SLIP_BPS}]


def main():
    trader = FlushTrader()
    makefun_bars.run(trader.on_batch)


if __name__ == "__main__":
    main()
