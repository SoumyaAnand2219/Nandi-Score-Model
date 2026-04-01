import streamlit as st
import pandas as pd
import numpy as np
import datetime as dt
import pyotp
from SmartApi.smartConnect import SmartConnect
import time
import io

# ================= CONFIG =================
st.set_page_config(page_title="🟡 NANDI Advanced Scanner", layout="wide")
st.title("🟡 NANDI White Candle Advanced Score Scanner")

# ================= ANGEL LOGIN =================
api_key = "g5o6vfTl"
client_id = "R59803990"
password = "1234"
totp_secret = "5W4MC6MMLANC3UYOAW2QDUIFEU"

@st.cache_resource
def angel_login():
    obj = SmartConnect(api_key=api_key)
    totp = pyotp.TOTP(totp_secret).now()
    obj.generateSession(client_id, password, totp)
    return obj

try:
    obj = angel_login()
    st.success("Login Successful")
except:
    st.error("Login Failed")
    st.stop()

# ================= STOCK LIST =================
from Stock_tokens import stock_list

# ================= HELPER FUNCTIONS =================
def fetch_data(token, interval, from_date, to_date):
    params = {
        "exchange": "NSE",
        "symboltoken": str(token),
        "interval": interval,
        "fromdate": from_date.strftime("%Y-%m-%d 09:15"),
        "todate": to_date.strftime("%Y-%m-%d 15:30"),
    }

    response = obj.getCandleData(params)
    if not response or response["status"] != True:
        return None

    df = pd.DataFrame(response["data"],
                      columns=["timestamp","open","high","low","close","volume"])

    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
    df[["open","high","low","close","volume"]] = df[["open","high","low","close","volume"]].astype(float)
    return df


def compute_cmo(close, length=9):
    diff = close.diff()
    up = diff.clip(lower=0)
    down = (-diff).clip(lower=0)
    sum_up = up.rolling(length).sum()
    sum_down = down.rolling(length).sum()
    return 100 * (sum_up - sum_down) / (sum_up + sum_down)


def detect_pine_logic(df):
    df = df.copy()

    len_ = 20
    mult = 2.0
    cmoLen = 9
    emaLen = 50
    atrLen = 14
    slopeLookback = 5
    nearHighLen = 20
    maxWidthATR = 2.5
    maxAdverseATR = 1.0
    idealBreakMin = 3
    idealBreakMax = 8
    bodyATRThresh = 0.8
    volMultiplier = 1.5

    close = df["close"]
    high = df["high"]
    low = df["low"]
    open_ = df["open"]
    volume = df["volume"]

    # === ATR (True Range based) ===
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    tr = np.maximum.reduce([tr1, tr2, tr3])
    atr = pd.Series(tr).rolling(atrLen).mean()

    # === EMA ===
    ema50 = close.ewm(span=emaLen, adjust=False).mean()

    # === CMO ===
    diff = close.diff()
    up = diff.clip(lower=0)
    down = (-diff).clip(lower=0)
    sum_up = up.rolling(cmoLen).sum()
    sum_down = down.rolling(cmoLen).sum()
    cmo = 100 * (sum_up - sum_down) / (sum_up + sum_down)

    # === White Candle Logic ===
    sma = close.rolling(len_).mean()
    dev = mult * (close.sub(sma).abs().rolling(len_).std())
    kri = close - sma
    absKRI = kri.abs()
    absKRIprev = absKRI.shift(1)
    devPrev = dev.shift(1)
    changePerc = (close / close.shift(1) - 1) * 100

    condition1 = (absKRI > dev) & (absKRIprev <= devPrev) & (changePerc >= 0)
    condition2 = (cmo > 0) & (cmo.shift(1) < 0)
    whiteCandle = condition1 & condition2

    volSma = volume.rolling(20).mean()
    volSpike = volume > volSma * volMultiplier

    scores = np.zeros(len(df))
    setup_active = False
    white_high = white_low = white_close = white_atr = None
    white_index = None
    structure_score = 0

    for i in range(len(df)):
        if whiteCandle.iloc[i]:
            setup_active = True
            white_high = high.iloc[i]
            white_low = low.iloc[i]
            white_close = close.iloc[i]
            white_atr = atr.iloc[i]
            white_index = i

            s1 = 1 if close.iloc[i] > ema50.iloc[i] else 0
            s2 = 1 if i >= slopeLookback and ema50.iloc[i] > ema50.iloc[i - slopeLookback] else 0
            priorHH = high.iloc[max(0, i-nearHighLen):i].max()
            s3 = 1 if not np.isnan(priorHH) and close.iloc[i] >= priorHH * 0.95 else 0
            structure_score = s1 + s2 + s3

        if setup_active and white_index is not None:
            bars_since = i - white_index
            if bars_since > 12:
                setup_active = False
                continue

            hh = high.iloc[white_index:i+1].max()
            ll = low.iloc[white_index:i+1].min()

            cons_width_atr = (hh - ll) / white_atr if white_atr else np.nan
            adverse_atr = max(0, white_close - ll) / white_atr if white_atr else np.nan

            closes_below_mid = sum(close.iloc[j] < (white_high + white_low)/2 for j in range(white_index, i+1))
            closes_below_support = sum(close.iloc[j] < white_low for j in range(white_index, i+1))

            c1 = 1 if closes_below_support == 0 else 0
            c2 = 1 if not np.isnan(cons_width_atr) and cons_width_atr <= maxWidthATR else 0
            c3 = 1 if closes_below_mid <= int(max(1, bars_since) * 0.35) else 0
            c4 = 1 if not np.isnan(adverse_atr) and adverse_atr <= maxAdverseATR else 0

            compression_score = c1 + c2 + c3 + c4

            breakout_up = close.iloc[i] > white_high and volSpike.iloc[i]
            bodyATR = abs(close.iloc[i] - open_.iloc[i]) / atr.iloc[i] if atr.iloc[i] > 0 else 0
            close_near_high = ((close.iloc[i] - low.iloc[i]) / (high.iloc[i] - low.iloc[i])) >= 0.7 if high.iloc[i] != low.iloc[i] else False

            b1 = 1 if breakout_up else 0
            b2 = 1 if breakout_up and idealBreakMin <= bars_since <= idealBreakMax else 0
            b3 = 1 if breakout_up and bodyATR >= bodyATRThresh and close_near_high else 0

            breakout_score = b1 + b2 + b3

            dist_from_ema_atr = abs(close.iloc[i] - ema50.iloc[i]) / atr.iloc[i] if atr.iloc[i] > 0 else 0
            stretch_penalty = 0.5 if dist_from_ema_atr > 3.5 else 0

            raw_score = structure_score + compression_score + breakout_score - stretch_penalty
            scores[i] = max(0, min(raw_score, 10))

            if close.iloc[i] < white_low:
                setup_active = False

    df["Score"] = scores
    return df

# ================= TABS =================
tab1, tab2 = st.tabs(["📦 Batch Scanner", "🔍 Single Stock Analyzer"])

# ================= TAB 1: BATCH SCANNER =================
with tab1:
    st.subheader("Batch Scanner")

    items = list(stock_list.items())
    batch_size = 100
    batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]

    batch_no = st.selectbox("Select Batch", list(range(1, len(batches) + 1)), key="batch_no")
    selected_batch = batches[batch_no - 1]

    col1, col2, col3 = st.columns(3)
    with col1:
        interval = st.selectbox("Interval", ["ONE_DAY", "ONE_HOUR"], key="batch_interval")
    with col2:
        from_date = st.date_input("From Date", dt.date.today() - dt.timedelta(days=120), key="batch_from")
    with col3:
        to_date = st.date_input("To Date", dt.date.today(), key="batch_to")

    threshold = st.slider("Minimum Score Threshold", 0.0, 10.0, 6.0, 0.5, key="batch_threshold")
    scan_button = st.button("Run Batch Scan", key="batch_scan")

    if scan_button:
        results = []
        progress = st.progress(0)

        for i, (symbol, token) in enumerate(selected_batch):
            df = fetch_data(token, interval, from_date, to_date)
            if df is not None:
                df = detect_pine_logic(df)
                df["date"] = df["timestamp"].dt.date
                qualified = df[(df["date"] >= from_date) & (df["date"] <= to_date) & (df["Score"] >= threshold)]

                if not qualified.empty:
                    last = qualified.sort_values("timestamp").iloc[-1]
                    results.append({
                        "Symbol": symbol,
                        "Date": last["timestamp"],
                        "Close": last["close"],
                        "Score": round(last["Score"], 2)
                    })

            progress.progress((i + 1) / len(selected_batch))
            time.sleep(0.2)

        if results:
            result_df = pd.DataFrame(results).sort_values("Score", ascending=False)
            st.dataframe(result_df, use_container_width=True)

            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                result_df.to_excel(writer, index=False)

            st.download_button("Download Excel", data=buffer.getvalue(),
                               file_name=f"NANDI_BATCH_{batch_no}.xlsx")
        else:
            st.warning("No stocks qualified based on threshold.")

# ================= TAB 2: SINGLE STOCK ANALYZER =================
with tab2:
    st.subheader("Single Stock Analyzer")

    stock_names = list(stock_list.keys())
    selected_stock = st.selectbox("Select Stock", stock_names, key="single_stock")

    col1, col2 = st.columns(2)
    with col1:
        from_date_single = st.date_input("From Date", dt.date.today() - dt.timedelta(days=120), key="single_from")
    with col2:
        to_date_single = st.date_input("To Date", dt.date.today(), key="single_to")

    interval_single = st.selectbox("Interval", ["ONE_DAY", "ONE_HOUR"], key="single_interval")
    run_single = st.button("Analyze Stock", key="single_run")

    if run_single:
        token = stock_list[selected_stock]
        df = fetch_data(token, interval_single, from_date_single, to_date_single)

        if df is not None:
            df = detect_pine_logic(df)
            df["date"] = df["timestamp"].dt.date
            df = df[(df["date"] >= from_date_single) & (df["date"] <= to_date_single)]

            if not df.empty:
                valid_scores = df[df["Score"] > 0]
                latest_row = valid_scores.sort_values("timestamp").iloc[-1] if not valid_scores.empty else df.iloc[-1]

                st.metric("Latest Score", round(latest_row["Score"], 2))
                st.metric("Close", round(latest_row["close"], 2))
                st.write("Last Candle Date:", latest_row["timestamp"])

                st.dataframe(df.tail(10), use_container_width=True)
            else:
                st.warning("No data available in selected date range.")
        else:
            st.error("Failed to fetch data from API.")    
