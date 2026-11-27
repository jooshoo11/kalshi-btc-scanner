import sqlite3

conn = sqlite3.connect("multi_coin_paper.db")
cursor = conn.cursor()
trades = cursor.execute("SELECT coin, side, entry_price, contracts, pnl_dollars, pnl_pct, status, exit_reason FROM trades").fetchall()
conn.close()

total_trades = len(trades)
closed_trades = [t for t in trades if t[6] == "CLOSED"]
total_pnl = sum(t[4] for t in closed_trades if t[4] is not None)
equity = 100.0 + total_pnl

wins = [t for t in closed_trades if t[4] is not None and t[4] > 0]
win_rate = (len(wins) / len(closed_trades)) * 100 if closed_trades else 0

print(f"Total Trades: {total_trades}")
print(f"Closed Trades: {len(closed_trades)}")
print(f"Total Net PnL: ${total_pnl:.2f}")
print(f"Account Equity ($100 baseline): ${equity:.2f}")
print(f"Overall Win Rate: {win_rate:.1f}% ({len(wins)}/{len(closed_trades)})")

by_coin = {}
for t in closed_trades:
    coin = t[0]
    if coin not in by_coin:
        by_coin[coin] = {"count": 0, "wins": 0, "pnl": 0.0}
    by_coin[coin]["count"] += 1
    by_coin[coin]["pnl"] += t[4] if t[4] else 0.0
    if t[4] and t[4] > 0:
        by_coin[coin]["wins"] += 1

print("\nPer-Coin Breakdown:")
for coin, data in by_coin.items():
    c = data["count"]
    w = data["wins"]
    p = data["pnl"]
    wr = (w / c) * 100 if c > 0 else 0
    print(f"  {coin}: Count={c}, WinRate={wr:.1f}%, PnL=${p:.2f}")
