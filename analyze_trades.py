import sqlite3

conn = sqlite3.connect('multi_coin_paper.db')
cursor = conn.cursor()

cursor.execute("SELECT coin, status, pnl_dollars, pnl_pct, contracts, entry_price, timestamp, exit_timestamp, exit_reason, ticker FROM trades;")
rows = cursor.fetchall()
conn.close()

total_trades = len(rows)
closed_trades = [r for r in rows if r[1] == 'CLOSED']
open_trades = [r for r in rows if r[1] == 'OPEN']

print(f"Total trades in DB: {total_trades}")
print(f"Open trades: {len(open_trades)}")
for ot in open_trades:
    print('  OPEN:', ot)

print(f"Closed trades: {len(closed_trades)}")
wins = [r for r in closed_trades if (r[2] is not None and r[2] > 0)]
losses = [r for r in closed_trades if (r[2] is not None and r[2] <= 0)]

print(f"Wins: {len(wins)}, Losses: {len(losses)}")
win_rate = (len(wins) / len(closed_trades) * 100) if closed_trades else 0
print(f"Win Rate: {win_rate:.2f}%")

net_pnl = sum([r[2] for r in closed_trades if r[2] is not None])
print(f"Net PnL: ${net_pnl:.2f}")

equity = 100.00 + net_pnl
print(f"Equity: ${equity:.2f}")

by_coin = {}
for r in closed_trades:
    coin = r[0]
    if coin not in by_coin:
        by_coin[coin] = {'count': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0}
    by_coin[coin]['count'] += 1
    if r[2] is not None and r[2] > 0:
        by_coin[coin]['wins'] += 1
    else:
        by_coin[coin]['losses'] += 1
    if r[2] is not None:
        by_coin[coin]['pnl'] += r[2]

for coin, stats in by_coin.items():
    wr = (stats['wins'] / stats['count'] * 100) if stats['count'] > 0 else 0
    print(f"Coin {coin}: {stats['count']} trades, Wins: {stats['wins']}, Losses: {stats['losses']}, Win Rate: {wr:.2f}%, PnL: ${stats['pnl']:.2f}")

print("\nLast 5 closed trades:")
for r in closed_trades[-5:]:
    print(r)
