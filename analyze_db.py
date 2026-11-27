import sqlite3
conn = sqlite3.connect('multi_coin_paper.db')
cur = conn.cursor()
rows = cur.execute('SELECT coin, status, pnl_dollars, pnl_pct FROM trades').fetchall()
print(f"Total trades: {len(rows)}")
wins = [r for r in rows if r[2] is not None and r[2] > 0]
losses = [r for r in rows if r[2] is not None and r[2] <= 0]
print(f"Wins: {len(wins)}, Losses: {len(losses)}, Win Rate: {len(wins)/len(rows)*100:.1f}%")
total_pnl = sum(r[2] for r in rows if r[2] is not None)
print(f"Net PnL: ${total_pnl:.2f}")
equity = 100.0 + total_pnl
print(f"Equity: ${equity:.2f}")

btc = [r for r in rows if r[0] == 'BTC']
eth = [r for r in rows if r[0] == 'ETH']
btc_pnl = sum(r[2] for r in btc if r[2] is not None)
eth_pnl = sum(r[2] for r in eth if r[2] is not None)
btc_wins = len([r for r in btc if r[2] is not None and r[2] > 0])
eth_wins = len([r for r in eth if r[2] is not None and r[2] > 0])
print(f"BTC: {len(btc)} trades, {btc_wins} wins, PnL: ${btc_pnl:.2f}")
print(f"ETH: {len(eth)} trades, {eth_wins} wins, PnL: ${eth_pnl:.2f}")

open_pos = cur.execute('SELECT ticker, coin, side, entry_price, timestamp FROM trades WHERE status = "OPEN"').fetchall()
print(f"Open positions: {len(open_pos)}")
for op in open_pos:
    print(op)

recent = cur.execute('SELECT coin, ticker, status, pnl_dollars, timestamp FROM trades ORDER BY timestamp DESC LIMIT 5').fetchall()
print("Recent trades:")
for r in recent:
    print(r)
