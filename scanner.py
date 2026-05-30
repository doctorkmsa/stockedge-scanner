import os, re, time, requests
import pandas as pd
import yfinance as yf
from datetime import datetime

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

MIN_PRICE = 2
MIN_VOLUME = 500_000
MIN_REL_VOLUME = 1.8
MIN_MARKET_CAP = 1000_000
MIN_YEARLY_GROWTH = 0

CHUNK_SIZE = 100

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": msg[:3900]})

def get_us_tickers():
    urls = [
        "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
        "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
    ]
    symbols = []

    for url in urls:
        df = pd.read_csv(url, sep="|")
        sym_col = "Symbol" if "Symbol" in df.columns else "ACT Symbol"

        if "Test Issue" in df.columns:
            df = df[df["Test Issue"] == "N"]
        if "ETF" in df.columns:
            df = df[df["ETF"] == "N"]

        for s in df[sym_col].dropna():
            s = str(s).strip()
            if re.match(r"^[A-Z]{1,5}$", s):
                symbols.append(s)

    return sorted(list(set(symbols)))

def compute(df):
    if df is None or len(df) < 320:
        return None

    df = df.dropna()

    high, low, close, volume = df["High"], df["Low"], df["Close"], df["Volume"]

    price = close.iloc[-1]
    today_volume = volume.iloc[-1]
    avg_volume = volume.tail(30).mean()
    rel_volume = today_volume / avg_volume if avg_volume > 0 else 0
    yearly_growth = (close.iloc[-1] / close.iloc[-252] - 1) * 100

    if price <= MIN_PRICE:
        return None
    if today_volume <= MIN_VOLUME:
        return None
    if rel_volume <= MIN_REL_VOLUME:
        return None
    if yearly_growth <= MIN_YEARLY_GROWTH:
        return None

    # Swing Anchored VWAP trend
    highest = high.rolling(50).max()
    lowest = low.rolling(50).min()

    swing_list = []
    cur = None
    for i in range(len(df)):
        if high.iloc[i] == highest.iloc[i]:
            cur = True
        if low.iloc[i] == lowest.iloc[i]:
            cur = False
        swing_list.append(cur)

    swing = pd.Series(swing_list, index=df.index)

    swing_buy_today = swing.iloc[-1] == True and swing.iloc[-2] == False
    swing_sell_today = swing.iloc[-1] == False and swing.iloc[-2] == True

    # Future Trend Channel
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    atr200 = tr.rolling(200).mean()
    atr = atr200.rolling(100).max()

    sma = close.rolling(100).mean()
    upper = sma + atr
    lower = sma - atr

    future_list = []
    cur = None
    for i in range(len(df)):
        if i > 0:
            if close.iloc[i-1] <= upper.iloc[i-1] and close.iloc[i] > upper.iloc[i]:
                cur = True
            if close.iloc[i-1] >= lower.iloc[i-1] and close.iloc[i] < lower.iloc[i]:
                cur = False
        future_list.append(cur)

    future = pd.Series(future_list, index=df.index)

    future_buy_today = future.iloc[-1] == True and future.iloc[-2] == False
    future_sell_today = future.iloc[-1] == False and future.iloc[-2] == True

    today_buy = (swing_buy_today and future.iloc[-1] == True) or (future_buy_today and swing.iloc[-1] == True)
    today_sell = (swing_sell_today and future.iloc[-1] == False) or (future_sell_today and swing.iloc[-1] == False)

    valid_buy = swing.iloc[-1] == True and future.iloc[-1] == True
    valid_sell = swing.iloc[-1] == False and future.iloc[-1] == False

    if not (today_buy or today_sell or valid_buy or valid_sell):
        return None

    try:
        mc = yf.Ticker(df.name).fast_info.get("market_cap", None)
    except Exception:
        mc = None

    if mc is not None and mc < MIN_MARKET_CAP:
        return None

    return {
        "price": round(float(price), 2),
        "volume": int(today_volume),
        "rvol": round(float(rel_volume), 2),
        "growth": round(float(yearly_growth), 1),
        "today_buy": today_buy,
        "today_sell": today_sell,
        "valid_buy": valid_buy,
        "valid_sell": valid_sell
    }

def scan():
    send_telegram("StockEdge Daily Scanner started ✅")

    tickers = get_us_tickers()

    today_buy, today_sell, valid_buy, valid_sell = [], [], [], []

    for i in range(0, len(tickers), CHUNK_SIZE):
        batch = tickers[i:i+CHUNK_SIZE]
        print(f"Scanning {i+1} to {i+len(batch)} / {len(tickers)}")

        try:
            data = yf.download(
                batch,
                period="2y",
                interval="1d",
                group_by="ticker",
                auto_adjust=False,
                threads=True,
                progress=False
            )

            for ticker in batch:
                try:
                    if ticker not in data.columns.get_level_values(0):
                        continue

                    df = data[ticker]
                    df.name = ticker

                    r = compute(df)
                    if r is None:
                        continue

                    line = f"{ticker} | ${r['price']} | RVOL {r['rvol']} | Vol {r['volume']:,} | 1Y {r['growth']}%"

                    if r["today_buy"]:
                        today_buy.append(line)
                    elif r["today_sell"]:
                        today_sell.append(line)
                    elif r["valid_buy"]:
                        valid_buy.append(line)
                    elif r["valid_sell"]:
                        valid_sell.append(line)

                except Exception:
                    continue

        except Exception as e:
            print("Batch error:", e)

        time.sleep(1)

    date = datetime.now().strftime("%Y-%m-%d")

    msg = f"""
StockEdge Daily Market Scan
Date: {date}

Filters:
Price > $2
Volume > 500k
Relative Volume > 1.8
Market Cap > 100k
Yearly Growth > 0%

TODAY BUY:
{chr(10).join(today_buy[:40]) if today_buy else "None"}

TODAY SELL:
{chr(10).join(today_sell[:40]) if today_sell else "None"}

VALID BUY:
{chr(10).join(valid_buy[:40]) if valid_buy else "None"}

VALID SELL:
{chr(10).join(valid_sell[:40]) if valid_sell else "None"}
"""

    send_telegram(msg)
    print(msg)

scan()
