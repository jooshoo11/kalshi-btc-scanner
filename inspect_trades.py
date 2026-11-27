import sqlite3

conn = sqlite3.connect("multi_coin_paper.db")
cursor = conn.cursor()
all_trades = cursor.execute("SELECT id, coin, ticker, side, entry_price, contracts, pnl_dollars, pnl_pct, status, exit_reason, timestamp, exit_timestamp FROM trades ORDER BY id DESC").fetchall()

print("--- ALL TRADES ---")
for t in all_trades:
    print(t)

conn.close()
