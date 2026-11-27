import sqlite3

conn = sqlite3.connect('multi_coin_paper.db')
cursor = conn.cursor()
cursor.execute('SELECT id, timestamp, coin, ticker, side, entry_price, contracts, pnl_dollars, pnl_pct, status, exit_reason FROM trades ORDER BY timestamp DESC;')
rows = cursor.fetchall()

total_pnl = 0.0
wins = 0
losses = 0

btc_trades = 0
btc_pnl = 0.0
btc_wins = 0
btc_losses = 0

eth_trades = 0
eth_pnl = 0.0
eth_wins = 0
eth_losses = 0

for r in rows:
    pnl = r[7] if r[7] is not None else 0.0
    total_pnl += pnl
    coin = r[2]
    is_win = pnl > 0 or 'WIN' in str(r[10]) or 'TAKE_PROFIT' in str(r[10])
    
    if coin == 'BTC':
        btc_trades += 1
        btc_pnl += pnl
        if is_win: btc_wins += 1
        else: btc_losses += 1
    elif coin == 'ETH':
        eth_trades += 1
        eth_pnl += pnl
        if is_win: eth_wins += 1
        else: eth_losses += 1
        
    if is_win: wins += 1
    else: losses += 1

print(f"Total Trades: {len(rows)}")
print(f"Total Net PnL: ${total_pnl:.2f}")
print(f"Starting Baseline (Trial 3): $100.00")
print(f"Current Equity: ${100.0 + total_pnl:.2f}")
print(f"Win Rate: {wins}/{wins+losses} ({wins/(wins+losses)*100:.1f}%)")
print("--- Per-Coin Breakdown ---")
if btc_trades > 0:
    print(f"BTC: {btc_trades} trades | PnL: ${btc_pnl:.2f} | Wins: {btc_wins}, Losses: {btc_losses} | Win Rate: {btc_wins/btc_trades*100:.1f}%")
else:
    print("BTC: 0 trades")

if eth_trades > 0:
    print(f"ETH: {eth_trades} trades | PnL: ${eth_pnl:.2f} | Wins: {eth_wins}, Losses: {eth_losses} | Win Rate: {eth_wins/eth_trades*100:.1f}%")
else:
    print("ETH: 0 trades")

print("--- Recent Trades (Last 5) ---")
for r in rows[:5]:
    print(f"ID: {r[0]} | {r[2]} | {r[3]} | {r[4]} @ {r[5]} | PnL: ${r[7]:.2f} ({r[8]}%) | Exit: {r[10]}")

conn.close()
