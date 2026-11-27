import sqlite3

conn = sqlite3.connect('multi_coin_paper.db')
cursor = conn.cursor()
trades = cursor.execute('SELECT coin, status, pnl_dollars, pnl_pct, entry_price, contracts, side, ticker, timestamp, exit_timestamp, exit_reason FROM trades').fetchall()
print(f'Total trades in DB: {len(trades)}')
by_coin = {}
total_pnl = 0.0
total_wins = 0
total_losses = 0
closed_count = 0
open_positions = []

for coin, status, pnl, roi, price, contracts, side, ticker, ts, ets, ereason in trades:
    if coin not in by_coin:
        by_coin[coin] = {'total': 0, 'closed': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0}
    by_coin[coin]['total'] += 1
    
    if status == 'CLOSED':
        closed_count += 1
        by_coin[coin]['closed'] += 1
        if pnl is not None:
            total_pnl += pnl
            by_coin[coin]['pnl'] += pnl
            if pnl > 0:
                total_wins += 1
                by_coin[coin]['wins'] += 1
            elif pnl < 0:
                total_losses += 1
                by_coin[coin]['losses'] += 1
    else:
        open_positions.append((coin, status, price, contracts, side, ticker))

win_rate = (total_wins / closed_count * 100) if closed_count > 0 else 0
print(f'Closed trades: {closed_count} (Wins: {total_wins}, Losses: {total_losses}, Win Rate: {win_rate:.1f}%)')
print(f'Net PnL: ${total_pnl:.2f}')
print(f'Baseline Equity: $100.00 + Net PnL = ${100.00 + total_pnl:.2f}')

print('\nPer-coin breakdown:')
for c, d in by_coin.items():
    wr = (d['wins'] / d['closed'] * 100) if d['closed'] > 0 else 0
    print(f'  {c}: Total={d["total"]}, Closed={d["closed"]}, Wins={d["wins"]}, Losses={d["losses"]}, WinRate={wr:.1f}%, PnL=${d["pnl"]:.2f}')

print(f'\nOpen positions ({len(open_positions)}):')
for op in open_positions:
    print(op)

print('\nRecent 5 closed trades:')
recent_closed = [t for t in trades if t[1] == 'CLOSED']
recent_closed.sort(key=lambda x: x[9] or 0, reverse=True)
for rc in recent_closed[:5]:
    print(f'  Coin: {rc[0]}, Ticker: {rc[7]}, Side: {rc[5]}, Price: {rc[4]}¢, Contracts: {rc[5]}, PnL: ${rc[2]:.2f} ({rc[3]}%), Reason: {rc[10]}')

conn.close()
