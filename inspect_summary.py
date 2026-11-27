import sqlite3
conn = sqlite3.connect('multi_coin_paper.db')
c = conn.cursor()
c.execute('SELECT id, timestamp, coin, ticker, side, entry_price, contracts, pnl_dollars, fee_dollars, pnl_pct, status, exit_reason FROM trades ORDER BY id DESC;')
trades = c.fetchall()
print(f"Total trades: {len(trades)}")
wins = sum(1 for t in trades if (t[7] or 0) > 0)
print(f"Wins: {wins}, Win rate: {wins/len(trades)*100:.1f}%" if trades else "No trades")
total_pnl = sum(t[7] for t in trades if t[7] is not None)
total_fees = sum(t[8] for t in trades if t[8] is not None)
print(f"Total PnL: ${total_pnl:.2f}, Total Fees: ${total_fees:.2f}, Net: ${total_pnl - total_fees:.2f}")
print("Current Equity (assuming $100 baseline):", 100.0 + total_pnl - total_fees)

by_coin = {}
for t in trades:
    coin = t[2]
    if coin not in by_coin:
        by_coin[coin] = {'count': 0, 'wins': 0, 'pnl': 0.0, 'fees': 0.0}
    by_coin[coin]['count'] += 1
    if (t[7] or 0) > 0:
        by_coin[coin]['wins'] += 1
    by_coin[coin]['pnl'] += (t[7] or 0.0)
    by_coin[coin]['fees'] += (t[8] or 0.0)

print("\nPer Coin:")
for coin, stats in by_coin.items():
    wr = (stats['wins'] / stats['count']) * 100 if stats['count'] > 0 else 0
    print(f"  {coin}: Count={stats['count']}, Wins={stats['wins']}, WinRate={wr:.1f}%, PnL=${stats['pnl']:.2f}, Net=${stats['pnl']-stats['fees']:.2f}")

print("\nLast 5 Closed Trades:")
for t in trades[:5]:
    print(f"  ID {t[0]} | {t[2]} | {t[3]} | Side: {t[4]} | Entry: {t[5]} | Contracts: {t[6]} | PnL: ${t[7]:.2f} ({t[9]:.1f}%) | Exit reason: {t[11]}")

conn.close()
