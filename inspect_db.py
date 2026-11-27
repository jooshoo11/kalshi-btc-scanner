import sqlite3
conn = sqlite3.connect('multi_coin_paper.db')
cursor = conn.cursor()
trades = cursor.execute('SELECT id, timestamp, coin, side, entry_price, contracts, pnl_dollars, status, exit_reason FROM trades ORDER BY id ASC').fetchall()

print(f'Total trades in table: {len(trades)}')
open_pos = cursor.execute('SELECT * FROM trades WHERE status != "CLOSED"').fetchall()
print('Open positions:', open_pos)

initial_equity = 100.0
total_pnl = sum(t[6] for t in trades if t[6] is not None)
print(f'Total Net PnL across all trades: ${total_pnl:.2f}, Equity: ${initial_equity + total_pnl:.2f}')

btc_trades = [t for t in trades if t[2] == 'BTC']
eth_trades = [t for t in trades if t[2] == 'ETH']

def stats(lst):
    closed = [t for t in lst if t[6] is not None]
    wins = [t for t in closed if t[6] > 0]
    losses = [t for t in closed if t[6] <= 0]
    pnl = sum(t[6] for t in closed)
    win_rate = (len(wins) / len(closed) * 100) if closed else 0
    return len(closed), len(wins), len(losses), win_rate, pnl

print('BTC Stats:', stats(btc_trades))
print('ETH Stats:', stats(eth_trades))
conn.close()
