import sqlite3
conn = sqlite3.connect('multi_coin_paper.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()
trades = cur.execute('SELECT * FROM trades').fetchall()
coins = set(t['coin'] for t in trades)
for c in coins:
    ct = [t for t in trades if t['coin'] == c]
    wins = [t for t in ct if t['pnl_dollars'] > 0]
    pnl = sum(t['pnl_dollars'] for t in ct)
    print(f"Coin {c}: Count={len(ct)}, Wins={len(wins)}, Losses={len(ct)-len(wins)}, WinRate={len(wins)/len(ct)*100:.1f}%, PnL=${pnl:.2f}")
total_pnl = sum(t['pnl_dollars'] for t in trades)
wins_all = [t for t in trades if t['pnl_dollars'] > 0]
print(f"Overall: Count={len(trades)}, Wins={len(wins_all)}, Losses={len(trades)-len(wins_all)}, WinRate={len(wins_all)/len(trades)*100:.1f}%, NetPnL=${total_pnl:.2f}, Equity=${100.0 + total_pnl:.2f}")
