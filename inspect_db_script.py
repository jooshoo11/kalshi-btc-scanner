import sqlite3
conn = sqlite3.connect("multi_coin_paper.db")
cursor = conn.cursor()
cursor.execute("SELECT coin, status, pnl_dollars, pnl_pct FROM trades;")
trades = cursor.fetchall()
print("Total trades:", len(trades))
by_coin = {}
wins = 0
total = len(trades)
total_pnl = 0.0
for coin, status, pnl, roi in trades:
    if coin not in by_coin:
        by_coin[coin] = {"total": 0, "wins": 0, "pnl": 0.0}
    by_coin[coin]["total"] += 1
    if pnl and pnl > 0:
        by_coin[coin]["wins"] += 1
        wins += 1
    if pnl:
        total_pnl += pnl
    by_coin[coin]["pnl"] += (pnl or 0.0)

print(f"Overall Win Rate: {wins}/{total} ({wins/total*100:.1f}%)" if total > 0 else "No trades")
print(f"Total Net PnL: ${total_pnl:.2f}")
for coin, d in by_coin.items():
    wr = d["wins"]/d["total"]*100 if d["total"] > 0 else 0
    print(f"  {coin}: {d['total']} trades, Win Rate: {wr:.1f}% ({d['wins']}/{d['total']}), PnL: ${d['pnl']:.2f}")

cursor.execute("SELECT ticker, side, entry_price, contracts, timestamp, status, pnl_dollars FROM trades WHERE status != 'CLOSED';")
open_pos = cursor.fetchall()
print("Open positions count:", len(open_pos))
for op in open_pos:
    print(op)

cursor.execute("SELECT ticker, side, entry_price, contracts, timestamp, status, pnl_dollars, exit_reason FROM trades WHERE status = 'CLOSED' ORDER BY timestamp DESC LIMIT 5;")
recent = cursor.fetchall()
print("Recent closed trades:")
for r in recent:
    print(r)
