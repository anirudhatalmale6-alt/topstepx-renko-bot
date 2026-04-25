"""
Renko Multi-TF Alignment Strategy - Backtest

Pulls historical 1-min NQ data from TopstepX and simulates
the exact same strategy as the live bot.
"""

import asyncio
import os
import json
from datetime import datetime, time as dtime, timedelta
from collections import defaultdict, OrderedDict

import pytz

ET = pytz.timezone("America/New_York")
SESSION_START = dtime(11, 0, 0)
SESSION_END = dtime(16, 0)
POINT_VALUE = 20.0
BRICK_SIZE = 0.25
TF_MINUTES = [1, 3, 5, 15]


class RenkoEngine:
    def __init__(self, brick_size: float, label: str = ""):
        self.brick_size = brick_size
        self.label = label
        self.last_close = None
        self.direction = 0
        self.brick_count = 0

    def initialize(self, price: float):
        self.last_close = round(price / self.brick_size) * self.brick_size

    def feed_close(self, close_price: float) -> list:
        if self.last_close is None:
            self.initialize(close_price)
            return []
        new_bricks = []
        while True:
            if close_price >= self.last_close + self.brick_size:
                new_open = self.last_close
                new_close = self.last_close + self.brick_size
                new_bricks.append((new_open, new_close, 1))
                self.last_close = new_close
                self.direction = 1
                self.brick_count += 1
            elif close_price <= self.last_close - self.brick_size:
                new_open = self.last_close
                new_close = self.last_close - self.brick_size
                new_bricks.append((new_open, new_close, -1))
                self.last_close = new_close
                self.direction = -1
                self.brick_count += 1
            else:
                break
        return new_bricks

    def clone(self):
        e = RenkoEngine(self.brick_size, self.label)
        e.last_close = self.last_close
        e.direction = self.direction
        e.brick_count = self.brick_count
        return e


def in_session(dt_et):
    t = dt_et.time()
    return SESSION_START <= t < SESSION_END


async def fetch_data(days=60):
    """Fetch historical 1-min NQ data from TopstepX."""
    from project_x_py import TradingSuite

    print(f"Connecting to TopstepX...")
    suite = await TradingSuite.create(
        instruments="NQ",
        timeframes=["1min"],
        initial_days=days,
    )
    ctx = suite["NQ"]

    # Try fetching in chunks to get more data
    all_rows = []
    chunk_sizes = [50000, 30000, 20000, 12000]
    for chunk in chunk_sizes:
        print(f"Trying to fetch {chunk} bars...")
        try:
            data = await ctx.data.get_data("1min", bars=chunk)
            if data is not None and len(data) > 0:
                all_rows = list(data.iter_rows(named=True))
                print(f"Got {len(all_rows)} bars")
                break
        except Exception as e:
            print(f"  Failed with {chunk} bars: {e}")
            continue

    if not all_rows:
        print("No data returned!")

    await suite.disconnect()
    return all_rows


def run_backtest(rows, allowed_days=None, timeframes=None):
    """Simulate the Renko multi-TF alignment strategy on historical data.
    allowed_days: list of weekday numbers to trade (0=Mon, 1=Tue, ..., 4=Fri). None = all days.
    timeframes: list of timeframe minutes to use. None = TF_MINUTES default.
    """
    tf_list = timeframes or TF_MINUTES

    # Initialize engines
    engines = {}
    for tf in tf_list:
        engines[tf] = RenkoEngine(BRICK_SIZE, f"{tf}min")

    # State
    position = 0  # 1=long, -1=short, 0=flat
    entry_price = 0.0
    entry_time = "N/A"
    bar_count = 0
    trades = []
    daily_pnl = defaultdict(float)
    current_date = None
    session_active = False

    # Warm up with first 200 bars
    warmup = min(200, len(rows) // 2)
    for i in range(warmup):
        close = float(rows[i]["close"])
        for tf in tf_list:
            if tf == 1 or (i + 1) % tf == 0:
                engines[tf].feed_close(close)

    bar_count = warmup

    # Run strategy
    for i in range(warmup, len(rows)):
        row = rows[i]
        close = float(row["close"])
        ts = row.get("timestamp") or row.get("time")

        # Parse timestamp
        if isinstance(ts, str):
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except:
                continue
        elif isinstance(ts, datetime):
            dt = ts
        else:
            continue

        if dt.tzinfo is None:
            dt = pytz.utc.localize(dt)
        dt_et = dt.astimezone(ET)
        date_str = dt_et.strftime("%Y-%m-%d")

        # Day of week filter
        if allowed_days is not None and dt_et.weekday() not in allowed_days:
            # Still feed data to keep engines warm but don't trade
            bar_count += 1
            for tf in tf_list:
                if tf == 1 or bar_count % tf == 0:
                    engines[tf].feed_close(close)
            continue

        # Session boundaries
        was_in_session = session_active
        session_active = in_session(dt_et)

        # Session end - flatten
        if was_in_session and not session_active:
            if position != 0:
                direction = "LONG" if position == 1 else "SHORT"
                trade_pnl = (close - entry_price) * position * POINT_VALUE
                trades.append({
                    "date": current_date,
                    "direction": direction,
                    "entry": entry_price,
                    "exit": close,
                    "pnl": trade_pnl,
                    "entry_time": entry_time if entry_price else "N/A",
                    "exit_time": dt_et.strftime("%H:%M"),
                    "reason": "SESSION_END",
                })
                daily_pnl[current_date] += trade_pnl
                position = 0
                entry_price = 0.0

        if not session_active:
            # Still feed data to keep engines warm
            bar_count += 1
            for tf in tf_list:
                if tf == 1 or bar_count % tf == 0:
                    engines[tf].feed_close(close)
            continue

        current_date = date_str

        # Feed Renko engines
        bar_count += 1
        new_renko = False

        for tf in tf_list:
            if tf == 1 or bar_count % tf == 0:
                bricks = engines[tf].feed_close(close)
                if bricks:
                    new_renko = True

        if not new_renko:
            continue

        # Get alignment
        directions = {tf: engines[tf].direction for tf in tf_list}
        all_up = all(d == 1 for d in directions.values())
        all_down = all(d == -1 for d in directions.values())

        # Check misalignment
        misaligned = False
        if position == 1:
            misaligned = any(d == -1 for d in directions.values())
        elif position == -1:
            misaligned = any(d == 1 for d in directions.values())

        # Exit on misalignment
        if position != 0 and misaligned:
            direction = "LONG" if position == 1 else "SHORT"
            trade_pnl = (close - entry_price) * position * POINT_VALUE
            trades.append({
                "date": current_date,
                "direction": direction,
                "entry": entry_price,
                "exit": close,
                "pnl": trade_pnl,
                "entry_time": entry_time,
                "exit_time": dt_et.strftime("%H:%M"),
                "reason": "MISALIGNED",
            })
            daily_pnl[current_date] += trade_pnl
            position = 0
            entry_price = 0.0

        # Entry on alignment
        if all_up and position <= 0:
            if position == -1:
                # Already handled above
                pass
            if position == 0:
                position = 1
                entry_price = close
                entry_time = dt_et.strftime("%H:%M")

        elif all_down and position >= 0:
            if position == 1:
                pass
            if position == 0:
                position = -1
                entry_price = close
                entry_time = dt_et.strftime("%H:%M")

    return trades, dict(daily_pnl)


def generate_report(trades, daily_pnl):
    """Generate backtest report data."""
    if not trades:
        return "No trades found in backtest period."

    # Overall stats
    total_trades = len(trades)
    winners = [t for t in trades if t["pnl"] > 0]
    losers = [t for t in trades if t["pnl"] < 0]
    scratches = [t for t in trades if t["pnl"] == 0]

    total_pnl = sum(t["pnl"] for t in trades)
    win_rate = len(winners) / total_trades * 100 if total_trades > 0 else 0
    avg_win = sum(t["pnl"] for t in winners) / len(winners) if winners else 0
    avg_loss = sum(t["pnl"] for t in losers) / len(losers) if losers else 0
    largest_win = max((t["pnl"] for t in trades), default=0)
    largest_loss = min((t["pnl"] for t in trades), default=0)

    # Profit factor
    gross_profit = sum(t["pnl"] for t in winners)
    gross_loss = abs(sum(t["pnl"] for t in losers))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Daily stats
    sorted_dates = sorted(daily_pnl.keys())
    daily_values = [daily_pnl[d] for d in sorted_dates]
    winning_days = [v for v in daily_values if v > 0]
    losing_days = [v for v in daily_values if v < 0]

    avg_daily = total_pnl / len(sorted_dates) if sorted_dates else 0
    avg_winning_day = sum(winning_days) / len(winning_days) if winning_days else 0
    avg_losing_day = sum(losing_days) / len(losing_days) if losing_days else 0
    best_day = max(daily_values) if daily_values else 0
    worst_day = min(daily_values) if daily_values else 0

    # Weekly stats
    weekly_pnl = defaultdict(float)
    for date_str, pnl in daily_pnl.items():
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        week_start = dt - timedelta(days=dt.weekday())
        weekly_pnl[week_start.strftime("%Y-%m-%d")] += pnl

    sorted_weeks = sorted(weekly_pnl.keys())
    weekly_values = [weekly_pnl[w] for w in sorted_weeks]
    avg_weekly = sum(weekly_values) / len(weekly_values) if weekly_values else 0

    # Max drawdown
    cumulative = 0
    peak = 0
    max_dd = 0
    for t in trades:
        cumulative += t["pnl"]
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    # Consecutive wins/losses
    max_consec_wins = 0
    max_consec_losses = 0
    curr_wins = 0
    curr_losses = 0
    for t in trades:
        if t["pnl"] > 0:
            curr_wins += 1
            curr_losses = 0
            max_consec_wins = max(max_consec_wins, curr_wins)
        elif t["pnl"] < 0:
            curr_losses += 1
            curr_wins = 0
            max_consec_losses = max(max_consec_losses, curr_losses)
        else:
            curr_wins = 0
            curr_losses = 0

    # Avg trades per day
    avg_trades_per_day = total_trades / len(sorted_dates) if sorted_dates else 0

    # Hourly analysis (time-of-day breakdown)
    hourly_stats = {}
    for hour in range(9, 17):  # 9 AM to 4 PM
        hour_trades = [t for t in trades if t.get("entry_time") and int(t["entry_time"].split(":")[0]) == hour]
        if hour_trades:
            h_winners = [t for t in hour_trades if t["pnl"] > 0]
            h_losers = [t for t in hour_trades if t["pnl"] < 0]
            h_total_pnl = sum(t["pnl"] for t in hour_trades)
            h_avg_pnl = h_total_pnl / len(hour_trades)
            h_win_rate = len(h_winners) / len(hour_trades) * 100
            h_avg_win = sum(t["pnl"] for t in h_winners) / len(h_winners) if h_winners else 0
            h_avg_loss = sum(t["pnl"] for t in h_losers) / len(h_losers) if h_losers else 0
            hourly_stats[f"{hour:02d}:00"] = {
                "trades": len(hour_trades),
                "winners": len(h_winners),
                "losers": len(h_losers),
                "total_pnl": h_total_pnl,
                "avg_pnl": h_avg_pnl,
                "win_rate": h_win_rate,
                "avg_win": h_avg_win,
                "avg_loss": h_avg_loss,
            }

    # 15-minute window analysis (quarter-hour breakdown)
    quarter_stats = {}
    for hour in range(9, 17):
        for q in range(4):  # 0=:00-:14, 1=:15-:29, 2=:30-:44, 3=:45-:59
            min_start = q * 15
            min_end = min_start + 14
            label = f"{hour:02d}:{min_start:02d}"
            q_trades = []
            for t in trades:
                et = t.get("entry_time")
                if not et:
                    continue
                parts = et.split(":")
                t_hour = int(parts[0])
                t_min = int(parts[1])
                if t_hour == hour and min_start <= t_min <= min_end:
                    q_trades.append(t)
            if q_trades:
                q_winners = [t for t in q_trades if t["pnl"] > 0]
                q_losers = [t for t in q_trades if t["pnl"] < 0]
                q_total_pnl = sum(t["pnl"] for t in q_trades)
                quarter_stats[label] = {
                    "trades": len(q_trades),
                    "winners": len(q_winners),
                    "losers": len(q_losers),
                    "total_pnl": q_total_pnl,
                    "avg_pnl": q_total_pnl / len(q_trades),
                    "win_rate": len(q_winners) / len(q_trades) * 100,
                    "avg_win": sum(t["pnl"] for t in q_winners) / len(q_winners) if q_winners else 0,
                    "avg_loss": sum(t["pnl"] for t in q_losers) / len(q_losers) if q_losers else 0,
                }

    report = {
        "period": f"{sorted_dates[0]} to {sorted_dates[-1]}" if sorted_dates else "N/A",
        "trading_days": len(sorted_dates),
        "total_trades": total_trades,
        "winners": len(winners),
        "losers": len(losers),
        "scratches": len(scratches),
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "largest_win": largest_win,
        "largest_loss": largest_loss,
        "avg_daily_pnl": avg_daily,
        "avg_winning_day": avg_winning_day,
        "avg_losing_day": avg_losing_day,
        "best_day": best_day,
        "worst_day": worst_day,
        "winning_days": len(winning_days),
        "losing_days": len(losing_days),
        "day_win_rate": len(winning_days) / len(sorted_dates) * 100 if sorted_dates else 0,
        "avg_weekly_pnl": avg_weekly,
        "max_drawdown": max_dd,
        "max_consec_wins": max_consec_wins,
        "max_consec_losses": max_consec_losses,
        "avg_trades_per_day": avg_trades_per_day,
        "daily_pnl": {d: daily_pnl[d] for d in sorted_dates},
        "weekly_pnl": {w: weekly_pnl[w] for w in sorted_weeks},
        "trades": trades,
        "hourly_stats": hourly_stats,
        "quarter_stats": quarter_stats,
    }

    return report


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", default="", help="Comma-separated days to trade: mon,tue,wed,thu,fri")
    parser.add_argument("--timeframes", default="", help="Comma-separated timeframe minutes: 1,3,5,15,60")
    args = parser.parse_args()

    allowed_days = None
    if args.days:
        day_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4}
        allowed_days = [day_map[d.strip().lower()] for d in args.days.split(",") if d.strip().lower() in day_map]
        day_names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri"}
        print(f"Filtering to: {', '.join(day_names[d] for d in allowed_days)}")

    timeframes = None
    if args.timeframes:
        timeframes = [int(t.strip()) for t in args.timeframes.split(",")]
        print(f"Timeframes: {', '.join(f'{t}min' for t in timeframes)}")

    rows = await fetch_data(days=30)
    if not rows:
        print("No data to backtest")
        return

    print(f"\nRunning backtest...")
    trades, daily_pnl = run_backtest(rows, allowed_days=allowed_days, timeframes=timeframes)
    print(f"Backtest complete: {len(trades)} trades over {len(daily_pnl)} days\n")

    report = generate_report(trades, daily_pnl)

    # Save raw data
    with open("/var/lib/freelancer/projects/40344180/backtest_results.json", "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"Results saved to backtest_results.json")

    # Print summary
    if isinstance(report, dict):
        print(f"\n{'='*60}")
        print(f"  RENKO MULTI-TF ALIGNMENT STRATEGY - BACKTEST REPORT")
        print(f"{'='*60}")
        print(f"  Period:           {report['period']}")
        print(f"  Trading Days:     {report['trading_days']}")
        print(f"  Total Trades:     {report['total_trades']}")
        print(f"  Win Rate:         {report['win_rate']:.1f}%")
        print(f"  Total P&L:        ${report['total_pnl']:,.2f}")
        print(f"  Profit Factor:    {report['profit_factor']:.2f}")
        print(f"  Avg Win:          ${report['avg_win']:,.2f}")
        print(f"  Avg Loss:         ${report['avg_loss']:,.2f}")
        print(f"  Largest Win:      ${report['largest_win']:,.2f}")
        print(f"  Largest Loss:     ${report['largest_loss']:,.2f}")
        print(f"  Max Drawdown:     ${report['max_drawdown']:,.2f}")
        print(f"  Avg Daily P&L:    ${report['avg_daily_pnl']:,.2f}")
        print(f"  Best Day:         ${report['best_day']:,.2f}")
        print(f"  Worst Day:        ${report['worst_day']:,.2f}")
        print(f"  Day Win Rate:     {report['day_win_rate']:.1f}%")
        print(f"  Avg Trades/Day:   {report['avg_trades_per_day']:.1f}")
        print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
