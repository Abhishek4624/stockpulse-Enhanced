"""
StockPulse - Indian stock market dashboard
Flask backend  ·  data via yfinance (NSE tickers, SYMBOL.NS)  ·  SQLite for logins

Run:  pip install -r requirements.txt   then   python app.py
"""
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from flask import Flask, g, jsonify, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)
DB = os.path.join(DATA, "stockpulse.db")
IST = ZoneInfo("Asia/Kolkata")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

app = Flask(__name__)


def _secret():
    p = os.path.join(DATA, "secret.key")
    if not os.path.exists(p):
        with open(p, "w") as fh:
            fh.write(secrets.token_hex(32))
    with open(p) as fh:
        return fh.read().strip()


app.secret_key = os.environ.get("SECRET_KEY") or _secret()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
_cache, _lock = {}, threading.Lock()


def cached(ttl):
    """Tiny in-memory TTL cache so we don't hammer the data source."""
    def deco(fn):
        @wraps(fn)
        def wrap(*a, **k):
            key = (fn.__name__, a, tuple(sorted(k.items())))
            now = time.time()
            with _lock:
                hit = _cache.get(key)
                if hit and now - hit[0] < ttl:
                    return hit[1]
            val = fn(*a, **k)
            with _lock:
                _cache[key] = (now, val)
            return val
        return wrap
    return deco


def clean(o):
    """Make numpy / NaN values JSON safe."""
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (np.bool_, bool)):
        return bool(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return None if math.isnan(f) or math.isinf(f) else f
    return o


def ok(data, status=200):
    return jsonify(clean(data)), status


def fail(msg, status=400):
    return jsonify({"error": msg}), status


def fnum(x):
    try:
        f = float(x)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def norm(sym):
    return re.sub(r"[^A-Z0-9&\-]", "", (sym or "").upper())


def clip(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def scale(x, lo, hi):
    return clip((x - lo) / (hi - lo) * 100.0)


def market_open():
    n = datetime.now(IST)
    return n.weekday() < 5 and (9, 15) <= (n.hour, n.minute) <= (15, 30)


# --------------------------------------------------------------------------
# Stock universe (every NSE-listed equity) + search
# --------------------------------------------------------------------------
NSE_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"

NIFTY50 = ["RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "BHARTIARTL", "ITC", "SBIN", "LT",
           "HINDUNILVR", "KOTAKBANK", "AXISBANK", "BAJFINANCE", "MARUTI", "SUNPHARMA", "ASIANPAINT",
           "HCLTECH", "M&M", "TITAN", "ULTRACEMCO", "NTPC", "POWERGRID", "ONGC", "TATASTEEL",
           "ADANIPORTS", "ADANIENT", "JSWSTEEL", "COALINDIA", "WIPRO", "BAJAJFINSV", "NESTLEIND",
           "TECHM", "INDUSINDBK", "HINDALCO", "GRASIM", "CIPLA", "DRREDDY", "EICHERMOT", "BPCL",
           "APOLLOHOSP", "BRITANNIA", "HEROMOTOCO", "SBILIFE", "HDFCLIFE", "SHRIRAMFIN", "TRENT",
           "BAJAJ-AUTO", "ETERNAL", "JIOFIN", "TATACONSUM"]

_universe = None


def universe():
    """List of (SYMBOL, COMPANY NAME) for all NSE equities. Refreshed weekly."""
    global _universe
    with _lock:
        if _universe is not None:
            return _universe
    path = os.path.join(DATA, "equity_list.csv")
    stale = (not os.path.exists(path)) or time.time() - os.path.getmtime(path) > 7 * 86400
    if stale:
        for url in (NSE_LIST_URL, NSE_LIST_URL.replace("nsearchives", "archives")):
            try:
                r = requests.get(url, headers={"User-Agent": UA, "Accept": "text/csv,*/*", "Referer": "https://www.nseindia.com/"}, timeout=20)
                r.raise_for_status()
                with open(path, "wb") as fh:
                    fh.write(r.content)
                break
            except Exception as e:  # noqa: BLE001
                print("[universe] could not download NSE list from", url, "-", e)
    rows = []
    if os.path.exists(path):
        try:
            df = pd.read_csv(path)
            df.columns = [c.strip() for c in df.columns]
            for _, r in df.iterrows():
                rows.append((str(r["SYMBOL"]).strip(), str(r["NAME OF COMPANY"]).strip()))
        except Exception as e:  # noqa: BLE001
            print("[universe] bad csv:", e)
    if not rows:
        rows = [(s, s) for s in NIFTY50]
    with _lock:
        _universe = rows
    return rows


def search_stocks(q, limit=12):
    q = (q or "").strip().upper()
    if not q:
        return []
    res = []
    for sym, name in universe():
        nu = name.upper()
        if sym == q:
            rank = 0
        elif sym.startswith(q):
            rank = 1
        elif nu.startswith(q):
            rank = 2
        elif q in sym:
            rank = 3
        elif q in nu:
            rank = 4
        else:
            continue
        res.append((rank, len(sym), sym, name))
    res.sort()
    return [{"symbol": s, "name": n} for _, _, s, n in res[:limit]]


# --------------------------------------------------------------------------
# Market data (yfinance)
# --------------------------------------------------------------------------
def _fi(fi, *keys):
    for k in keys:
        try:
            v = fi[k]
        except Exception:  # noqa: BLE001
            continue
        f = fnum(v)
        if f is not None:
            return f
    return None


@cached(4)
def quote_for(ticker):
    """Latest price + day stats for any Yahoo ticker (e.g. RELIANCE.NS, ^NSEI)."""
    t = yf.Ticker(ticker)
    price = prev = None
    fi = None
    try:
        fi = t.fast_info
        price = _fi(fi, "last_price", "lastPrice")
        prev = _fi(fi, "previous_close", "previousClose")
    except Exception:  # noqa: BLE001
        pass
    if price is None or prev is None:
        h = t.history(period="5d", interval="1d")
        h = h.dropna(subset=["Close"])
        if h.empty:
            raise ValueError("No price data for " + ticker)
        if price is None:
            price = float(h["Close"].iloc[-1])
        if prev is None:
            prev = float(h["Close"].iloc[-2]) if len(h) > 1 else price
    change = price - prev
    g_ = (lambda *k: _fi(fi, *k)) if fi is not None else (lambda *k: None)
    return {
        "ticker": ticker,
        "price": price,
        "prev_close": prev,
        "change": change,
        "change_pct": (change / prev * 100.0) if prev else 0.0,
        "open": g_("open"),
        "day_high": g_("day_high", "dayHigh"),
        "day_low": g_("day_low", "dayLow"),
        "volume": g_("last_volume", "lastVolume"),
        "year_high": g_("year_high", "yearHigh"),
        "year_low": g_("year_low", "yearLow"),
        "market_cap": g_("market_cap", "marketCap"),
        "ts": time.time(),
    }


def live_quote(sym):
    q = quote_for(sym + ".NS")
    q["symbol"] = sym
    return q


@cached(120)
def daily(sym):
    h = yf.Ticker(sym + ".NS").history(period="5y", interval="1d", auto_adjust=True)
    h = h.dropna(subset=["Close"])
    if h.empty:
        raise ValueError(f"No data found for '{sym}'. Is it a valid NSE symbol?")
    if h.index.tz is not None:
        h.index = h.index.tz_localize(None)
    return h[["Open", "High", "Low", "Close", "Volume"]]


@cached(30)
def intraday(sym, period, interval):
    h = yf.Ticker(sym + ".NS").history(period=period, interval=interval, auto_adjust=True)
    h = h.dropna(subset=["Close"])
    if h.empty:
        raise ValueError("No intraday data yet (market may be closed or symbol is illiquid).")
    return h[["Open", "High", "Low", "Close", "Volume"]]


@cached(900)
def fundamentals(sym):
    try:
        info = yf.Ticker(sym + ".NS").info or {}
    except Exception as e:  # noqa: BLE001
        print("[fundamentals]", sym, e)
        info = {}

    def pct(k):
        v = fnum(info.get(k))
        return None if v is None else v * 100.0

    dte = fnum(info.get("debtToEquity"))
    dy = fnum(info.get("trailingAnnualDividendYield"))
    return {
        "name": info.get("longName") or info.get("shortName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "pe": fnum(info.get("trailingPE")),
        "forward_pe": fnum(info.get("forwardPE")),
        "pb": fnum(info.get("priceToBook")),
        "eps": fnum(info.get("trailingEps")),
        "book_value": fnum(info.get("bookValue")),
        "ebitda": fnum(info.get("ebitda")),
        "ev_ebitda": fnum(info.get("enterpriseToEbitda")),
        "enterprise_value": fnum(info.get("enterpriseValue")),
        "market_cap": fnum(info.get("marketCap")),
        "revenue": fnum(info.get("totalRevenue")),
        "net_income": fnum(info.get("netIncomeToCommon")),
        "free_cash_flow": fnum(info.get("freeCashflow")),
        "roe": pct("returnOnEquity"),
        "roa": pct("returnOnAssets"),
        "profit_margin": pct("profitMargins"),
        "operating_margin": pct("operatingMargins"),
        "revenue_growth": pct("revenueGrowth"),
        "earnings_growth": pct("earningsGrowth"),
        "debt_to_equity": None if dte is None else dte / 100.0,
        "current_ratio": fnum(info.get("currentRatio")),
        "dividend_yield": None if dy is None else dy * 100.0,
        "beta": fnum(info.get("beta")),
        "year_high": fnum(info.get("fiftyTwoWeekHigh")),
        "year_low": fnum(info.get("fiftyTwoWeekLow")),
        "promoter_holding": pct("heldPercentInsiders"),
        "institution_holding": pct("heldPercentInstitutions"),
    }


def fundamental_view(f):
    """Quick health check from the fundamentals. Returns (label, notes)."""
    pos, neg, notes, seen = 0, 0, [], 0

    def chk(cond_pos, cond_neg, good, bad):
        nonlocal pos, neg
        if cond_pos:
            pos += 1
            notes.append({"tone": "buy", "text": good})
        elif cond_neg:
            neg += 1
            notes.append({"tone": "sell", "text": bad})

    pe, roe, dte = f.get("pe"), f.get("roe"), f.get("debt_to_equity")
    pm, rg = f.get("profit_margin"), f.get("revenue_growth")
    seen = sum(x is not None for x in (pe, roe, dte, pm, rg))
    if seen == 0:
        return "Unknown", []
    if pe is not None:
        chk(0 < pe < 25, pe > 60, f"P/E of {pe:.1f} is reasonable", f"P/E of {pe:.1f} is expensive")
    if roe is not None:
        chk(roe >= 15, roe < 8, f"ROE {roe:.1f}% shows good capital efficiency", f"ROE {roe:.1f}% is weak")
    if dte is not None:
        chk(dte < 0.5, dte > 1.5, f"Low debt (D/E {dte:.2f})", f"High debt (D/E {dte:.2f})")
    if pm is not None:
        chk(pm >= 10, pm < 0, f"Healthy profit margin ({pm:.1f}%)", "The company is loss-making")
    if rg is not None:
        chk(rg >= 10, rg < 0, f"Revenue growing {rg:.1f}%", f"Revenue shrinking ({rg:.1f}%)")
    d = pos - neg
    return ("Strong" if d >= 2 else "Weak" if d <= -2 else "Average"), notes


# --------------------------------------------------------------------------
# Technical indicators
# --------------------------------------------------------------------------
def indicators(df):
    d = df.copy()
    c, h, l, v = d["Close"], d["High"], d["Low"], d["Volume"]
    for n in (20, 50, 200):
        d[f"sma{n}"] = c.rolling(n).mean()
    d["ema20"] = c.ewm(span=20, adjust=False).mean()
    d["ema50"] = c.ewm(span=50, adjust=False).mean()

    delta = c.diff()
    ag = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    al = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    d["rsi"] = 100 - 100 / (1 + ag / al)

    e12, e26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    d["macd"] = e12 - e26
    d["macd_sig"] = d["macd"].ewm(span=9, adjust=False).mean()
    d["macd_hist"] = d["macd"] - d["macd_sig"]

    mid, sd = d["sma20"], c.rolling(20).std()
    d["bb_up"], d["bb_lo"] = mid + 2 * sd, mid - 2 * sd

    ll, hh = l.rolling(14).min(), h.rolling(14).max()
    d["stoch_k"] = 100 * (c - ll) / (hh - ll).replace(0, np.nan)
    d["stoch_d"] = d["stoch_k"].rolling(3).mean()

    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()

    up, dn = h.diff(), -l.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=d.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=d.index)
    atr_s = d["atr"].replace(0, np.nan)
    d["pdi"] = 100 * pdm.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean() / atr_s
    d["mdi"] = 100 * mdm.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean() / atr_s
    dx = 100 * (d["pdi"] - d["mdi"]).abs() / (d["pdi"] + d["mdi"]).replace(0, np.nan)
    d["adx"] = dx.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()

    d["vol_avg20"] = v.rolling(20).mean()
    return d


def technical_analysis(sym, price, df=None):
    d = indicators(daily(sym) if df is None else df)
    if len(d) < 40:
        raise ValueError("Not enough price history to run technical analysis.")
    r, p = d.iloc[-1], d.iloc[-2]
    px = price if price else float(r["Close"])

    sigs = []

    def add(group, name, value, s, note):
        sigs.append({"group": group, "name": name, "value": fnum(value), "signal": s, "note": note})

    # --- moving averages
    for n, col in ((20, "sma20"), (50, "sma50"), (200, "sma200")):
        v = fnum(r[col])
        if v is not None:
            up = px > v
            add("Moving averages", f"SMA {n}", v, "buy" if up else "sell",
                f"Price is {'above' if up else 'below'} the {n}-day average")
    for n, col in ((20, "ema20"), (50, "ema50")):
        v = fnum(r[col])
        if v is not None:
            up = px > v
            add("Moving averages", f"EMA {n}", v, "buy" if up else "sell",
                f"Price is {'above' if up else 'below'} the {n}-day exponential average")
    s50, s200 = fnum(r["sma50"]), fnum(r["sma200"])
    if s50 is not None and s200 is not None:
        gold = s50 > s200
        add("Moving averages", "50/200 cross", s50 - s200, "buy" if gold else "sell",
            "Golden cross: 50-day above 200-day" if gold else "Death cross: 50-day below 200-day")

    # --- oscillators
    rsi = fnum(r["rsi"])
    if rsi is not None:
        if rsi < 30:
            add("Oscillators", "RSI (14)", rsi, "buy", "Oversold, selling may be exhausted")
        elif rsi > 70:
            add("Oscillators", "RSI (14)", rsi, "sell", "Overbought, the rally looks stretched")
        else:
            add("Oscillators", "RSI (14)", rsi, "neutral",
                "Momentum leaning up" if rsi >= 50 else "Momentum leaning down")
    if fnum(r["macd"]) is not None and fnum(r["macd_sig"]) is not None:
        bull = r["macd"] > r["macd_sig"]
        rising = r["macd_hist"] > p["macd_hist"]
        add("Oscillators", "MACD (12,26,9)", r["macd"], "buy" if bull else "sell",
            ("MACD above signal line" if bull else "MACD below signal line")
            + (", momentum building" if rising else ", momentum fading"))
    k = fnum(r["stoch_k"])
    if k is not None:
        s = "buy" if k < 20 else "sell" if k > 80 else "neutral"
        add("Oscillators", "Stochastic %K", k, s,
            "Oversold zone" if k < 20 else "Overbought zone" if k > 80 else "Mid range")
    bb_lo, bb_up = fnum(r["bb_lo"]), fnum(r["bb_up"])
    if bb_lo is not None and bb_up is not None:
        if px < bb_lo:
            add("Oscillators", "Bollinger Bands", bb_lo, "buy", "Price under the lower band, stretched to the downside")
        elif px > bb_up:
            add("Oscillators", "Bollinger Bands", bb_up, "sell", "Price over the upper band, stretched to the upside")
        else:
            add("Oscillators", "Bollinger Bands", fnum(r["sma20"]), "neutral", "Price is inside the bands")

    # --- trend strength & volume
    adx, pdi, mdi = fnum(r["adx"]), fnum(r["pdi"]), fnum(r["mdi"])
    if adx is not None and pdi is not None and mdi is not None:
        if adx >= 20:
            add("Trend & volume", "ADX (14)", adx, "buy" if pdi > mdi else "sell",
                f"{'Strong' if adx >= 25 else 'Emerging'} {'uptrend' if pdi > mdi else 'downtrend'}")
        else:
            add("Trend & volume", "ADX (14)", adx, "neutral", "No clear trend, market is range-bound")
    va = fnum(r["vol_avg20"])
    if va and fnum(r["Volume"]) is not None:
        ratio = float(r["Volume"]) / va
        chg = float(r["Close"]) - float(p["Close"])
        if ratio > 1.5 and chg > 0:
            add("Trend & volume", "Volume vs 20-day avg", ratio, "buy", "Heavy buying volume")
        elif ratio > 1.5 and chg < 0:
            add("Trend & volume", "Volume vs 20-day avg", ratio, "sell", "Heavy selling volume")
        else:
            add("Trend & volume", "Volume vs 20-day avg", ratio, "neutral", "Volume is normal")

    buys = sum(s["signal"] == "buy" for s in sigs)
    sells = sum(s["signal"] == "sell" for s in sigs)
    neut = len(sigs) - buys - sells
    score = (buys - sells) / len(sigs) if sigs else 0.0

    # --- levels
    piv = d.iloc[-1] if not market_open() else d.iloc[-2]
    H, L, C = float(piv["High"]), float(piv["Low"]), float(piv["Close"])
    P = (H + L + C) / 3
    levels = {"pivot": P, "r1": 2 * P - L, "r2": P + (H - L), "s1": 2 * P - H, "s2": P - (H - L)}
    low20 = float(d["Low"].iloc[-20:].min())
    high52 = float(d["High"].iloc[-252:].max())
    low52 = float(d["Low"].iloc[-252:].min())

    tech = {
        "rsi": rsi, "macd": fnum(r["macd"]), "macd_signal": fnum(r["macd_sig"]),
        "adx": adx, "atr": fnum(r["atr"]), "sma20": fnum(r["sma20"]), "sma50": s50, "sma200": s200,
        "ema20": fnum(r["ema20"]), "bb_upper": bb_up, "bb_lower": bb_lo,
        "stoch_k": k, "low20": low20, "high52": high52, "low52": low52,
        "from_high52_pct": (px / high52 - 1) * 100, "from_low52_pct": (px / low52 - 1) * 100,
    }
    summary = {"buy": buys, "sell": sells, "neutral": neut, "score": score}
    return sigs, summary, levels, tech


def make_verdict(px, summary, levels, tech, fview, fnotes):
    score, rsi = summary["score"], tech["rsi"]
    atr = tech["atr"] or px * 0.02
    floor_ = min(x for x in (tech["low20"], tech["bb_lower"]) if x is not None)
    ema20 = tech["ema20"]
    zone_hi = floor_ + 1.5 * atr
    if ema20 is not None and ema20 > floor_:
        zone_hi = min(zone_hi, ema20)
    zone_lo = floor_
    stop = zone_lo - atr
    target = max(levels["r1"], px + 2 * atr) if levels["r1"] > px else px + 2 * atr
    in_zone = px <= zone_hi * 1.03
    risk, reward = px - stop, target - px
    rr = (reward / risk) if risk > 0 else None

    reasons = [f"{summary['buy']} of {summary['buy'] + summary['sell'] + summary['neutral']} indicators are bullish, "
               f"{summary['sell']} bearish."]
    if rsi is not None:
        reasons.append(f"RSI is {rsi:.0f}: " + ("oversold." if rsi < 30 else "overbought." if rsi > 70 else "in the normal range."))
    if tech["sma200"]:
        reasons.append("Trading above the 200-day average (long-term uptrend)." if px > tech["sma200"]
                       else "Trading below the 200-day average (long-term trend is down).")
    reasons.append(f"Price {'is inside' if in_zone else 'is above'} the value zone "
                   f"₹{zone_lo:,.2f} to ₹{zone_hi:,.2f}.")
    if fview != "Unknown":
        reasons.append(f"Fundamentals look {fview.lower()}.")

    if score >= 0.3:
        if in_zone:
            action, tone, head = "BUY", "buy", "Good entry: trend is up and price is near support"
        else:
            action, tone, head = "WAIT FOR A DIP", "neutral", "Trend is up, but price has run ahead of its value zone"
    elif score <= -0.3:
        action, tone, head = "AVOID", "sell", "Trend is weak, better to stay out for now"
        if rsi is not None and rsi < 30:
            reasons.append("Oversold, so a short bounce is possible, but the trend hasn't turned.")
    else:
        action, tone, head = "HOLD / WATCH", "neutral", "Signals are mixed, wait for a clearer move"

    if action == "BUY" and fview == "Weak":
        action, tone, head = "CAUTIOUS BUY", "neutral", "Chart looks good, but the fundamentals are weak"
    if action == "BUY" and rsi is not None and rsi > 70:
        action, tone, head = "WAIT FOR A DIP", "neutral", "Overbought right now, a pullback would be a better entry"

    return {
        "action": action, "tone": tone, "headline": head, "reasons": reasons,
        "zone_low": zone_lo, "zone_high": zone_hi, "in_zone": in_zone,
        "stop_loss": stop, "target": target, "risk_reward": rr, "score": score,
    }


# --------------------------------------------------------------------------
# Stock screener
# --------------------------------------------------------------------------
def _scan_row(sym, df):
    """One row of screening numbers for a stock, from its daily prices."""
    if len(df) < 60:
        return None
    c = df["Close"]
    px, prev = float(c.iloc[-1]), float(c.iloc[-2])
    sigs, summary, levels, tech = technical_analysis(sym, px, df)
    verdict = make_verdict(px, summary, levels, tech, "Unknown", [])
    r = indicators(df).iloc[-1]
    s50, s200 = fnum(r["sma50"]), fnum(r["sma200"])
    macd, msig = fnum(r["macd"]), fnum(r["macd_sig"])
    va = fnum(r["vol_avg20"])
    return {
        "symbol": sym, "price": px, "change_pct": (px / prev - 1) * 100,
        "rsi": tech["rsi"], "adx": tech["adx"],
        "above_sma50": s50 is not None and px > s50,
        "above_sma200": s200 is not None and px > s200,
        "golden": s50 is not None and s200 is not None and s50 > s200,
        "macd_bull": macd is not None and msig is not None and macd > msig,
        "from_high": -tech["from_high52_pct"], "from_low": tech["from_low52_pct"],
        "vol_ratio": (float(r["Volume"]) / va) if va else None,
        "ret_1m": (px / float(c.iloc[-22]) - 1) * 100 if len(c) > 22 else None,
        "ret_3m": (px / float(c.iloc[-64]) - 1) * 100 if len(c) > 64 else None,
        "score": summary["score"] * 100, "buys": summary["buy"], "sells": summary["sell"],
        "verdict": verdict["action"],
    }


@cached(900)
def scan_chunk(symbols):
    """Download ~100 stocks in one go and return their screening rows."""
    tickers = [s + ".NS" for s in symbols]
    raw = yf.download(tickers, period="1y", interval="1d", auto_adjust=True,
                      progress=False, threads=True, group_by="ticker")
    rows = []
    for s, t in zip(symbols, tickers):
        try:
            df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
            row = _scan_row(s, df)
            if row:
                rows.append(row)
        except Exception:  # noqa: BLE001
            continue
    return rows


def apply_filters(rows, c):
    """Keep the rows that pass every technical filter in criteria dict c."""
    out = []
    for r in rows:
        rsi = r["rsi"]
        if (c.get("rsi_min", 0) > 0 or c.get("rsi_max", 100) < 100):
            if rsi is None or not (c.get("rsi_min", 0) <= rsi <= c.get("rsi_max", 100)):
                continue
        if c.get("above_sma50") and not r["above_sma50"]:
            continue
        if c.get("above_sma200") and not r["above_sma200"]:
            continue
        if c.get("macd_bull") and not r["macd_bull"]:
            continue
        if c.get("golden") and not r["golden"]:
            continue
        if r["change_pct"] < c.get("min_change", -100):
            continue
        if r["from_high"] > c.get("max_from_high", 1000):
            continue
        if c.get("vol_ratio_min", 0) > 0 and (r["vol_ratio"] is None or r["vol_ratio"] < c["vol_ratio_min"]):
            continue
        if c.get("adx_min", 0) > 0 and (r["adx"] is None or r["adx"] < c["adx_min"]):
            continue
        if c.get("actions") and r["verdict"] not in c["actions"]:
            continue
        out.append(r)
    return out


def fundamental_filter(rows, c, cap=80):
    """Optional second step: look up fundamentals only for stocks that passed."""
    keys = ("pe_max", "roe_min", "de_max", "mcap_min_cr", "rev_growth_min")
    if not any(c.get(k) is not None for k in keys):
        return rows, False
    rows = sorted(rows, key=lambda r: -r["score"])
    truncated = len(rows) > cap
    rows = rows[:cap]
    with ThreadPoolExecutor(8) as ex:
        funds = list(ex.map(lambda r: fundamentals(r["symbol"]), rows))
    keep = []
    for r, f in zip(rows, funds):
        r = dict(r)
        mc = f.get("market_cap")
        r.update(pe=f.get("pe"), roe=f.get("roe"), de=f.get("debt_to_equity"),
                 mcap_cr=(mc / 1e7) if mc else None, rev_growth=f.get("revenue_growth"))
        if c.get("pe_max") is not None and (r["pe"] is None or r["pe"] <= 0 or r["pe"] > c["pe_max"]):
            continue
        if c.get("roe_min") is not None and (r["roe"] is None or r["roe"] < c["roe_min"]):
            continue
        if c.get("de_max") is not None and (r["de"] is None or r["de"] > c["de_max"]):
            continue
        if c.get("mcap_min_cr") is not None and (r["mcap_cr"] is None or r["mcap_cr"] < c["mcap_min_cr"]):
            continue
        if c.get("rev_growth_min") is not None and (r["rev_growth"] is None or r["rev_growth"] < c["rev_growth_min"]):
            continue
        keep.append(r)
    return keep, truncated


# --------------------------------------------------------------------------
# Market Mood Index (built from Nifty 50 + India VIX)
# --------------------------------------------------------------------------
@cached(300)
def nifty50_closes():
    tickers = [s + ".NS" for s in NIFTY50]
    df = yf.download(tickers, period="1y", interval="1d", auto_adjust=True,
                     progress=False, threads=True)["Close"]
    return df.dropna(axis=1, how="all")


def mmi_zone(score):
    if score < 30:
        return ("Extreme Fear", "Investors are panicking. Quality stocks often get cheap here, but enter in stages instead of all at once.")
    if score < 50:
        return ("Fear", "Sentiment is cautious. Good businesses may be available at fair prices.")
    if score < 70:
        return ("Greed", "Sentiment is positive. Keep buying selectively and avoid chasing rallies.")
    return ("Extreme Greed", "The market is euphoric. Prices may be stretched, so be choosy and consider booking some profits.")


@cached(300)
def compute_mmi():
    closes = nifty50_closes()
    nifty = yf.Ticker("^NSEI").history(period="1y", interval="1d")["Close"].dropna()
    comps = []

    def add(key, name, weight, sc, detail):
        if sc is not None and not math.isnan(sc):
            comps.append({"key": key, "name": name, "weight": weight, "score": float(sc), "detail": detail})

    try:
        vix = quote_for("^INDIAVIX")["price"]
        add("vix", "Volatility", 25, scale(30 - vix, 0, 20), f"India VIX at {vix:.1f}. Lower means calmer.")
    except Exception as e:  # noqa: BLE001
        print("[mmi] vix", e)
    try:
        ema = nifty.ewm(span=125, adjust=False).mean().iloc[-1]
        dev = (nifty.iloc[-1] / ema - 1) * 100
        add("mom", "Momentum", 15, scale(dev, -6, 6), f"Nifty is {dev:+.1f}% versus its 125-day average.")
        sma200 = nifty.rolling(200).mean().iloc[-1]
        d2 = (nifty.iloc[-1] / sma200 - 1) * 100
        add("trend", "Long-term trend", 10, scale(d2, -10, 10), f"Nifty is {d2:+.1f}% versus its 200-day average.")
        r5 = (nifty.iloc[-1] / nifty.iloc[-6] - 1) * 100
        add("week", "Last 5 days", 10, scale(r5, -3, 3), f"Nifty moved {r5:+.1f}% over five sessions.")
    except Exception as e:  # noqa: BLE001
        print("[mmi] nifty", e)
    try:
        n = closes.shape[1]
        above = (closes.iloc[-1] > closes.rolling(50).mean().iloc[-1]).sum()
        add("above50", "Stocks above 50-day avg", 15, above / n * 100, f"{above} of {n} Nifty 50 stocks are above their 50-day average.")
        pos = ((closes.iloc[-1] - closes.min()) / (closes.max() - closes.min())).mean() * 100
        add("range", "Position in 52-week range", 15, pos, f"Stocks sit at {pos:.0f}% of their yearly range on average.")
        adv = (closes.iloc[-1] > closes.iloc[-2]).sum()
        add("breadth", "Advancers today", 10, adv / n * 100, f"{adv} of {n} stocks are up on the day.")
    except Exception as e:  # noqa: BLE001
        print("[mmi] breadth", e)

    if not comps:
        raise ValueError("Could not fetch enough market data to compute the index.")
    tw = sum(c["weight"] for c in comps)
    score = sum(c["score"] * c["weight"] for c in comps) / tw
    zone, advice = mmi_zone(score)
    return {"score": score, "zone": zone, "advice": advice, "components": comps,
            "updated": datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")}


@cached(300)
def movers():
    closes = nifty50_closes()
    chg = ((closes.iloc[-1] / closes.iloc[-2] - 1) * 100).dropna().sort_values()
    def rows(s):
        return [{"symbol": k.replace(".NS", ""), "change_pct": float(v), "price": float(closes[k].iloc[-1])}
                for k, v in s.items()]
    return {"gainers": rows(chg.tail(5)[::-1]), "losers": rows(chg.head(5))}


# --------------------------------------------------------------------------
# Database + auth
# --------------------------------------------------------------------------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    with sqlite3.connect(DB) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT UNIQUE NOT NULL COLLATE NOCASE,
              email TEXT UNIQUE NOT NULL COLLATE NOCASE,
              pw_hash TEXT NOT NULL,
              created TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS watchlist(
              user_id INTEGER NOT NULL, symbol TEXT NOT NULL,
              added TEXT DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(user_id, symbol));
        """)


def login_required(fn):
    @wraps(fn)
    def wrap(*a, **k):
        if not session.get("uid"):
            return fail("Log in to use your watchlist.", 401)
        return fn(*a, **k)
    return wrap


@app.get("/api/me")
def me():
    return ok({"user": session.get("username")})


@app.post("/api/register")
def register():
    d = request.get_json(silent=True) or {}
    username = (d.get("username") or "").strip()
    email = (d.get("email") or "").strip().lower()
    pw = d.get("password") or ""
    if not re.fullmatch(r"[A-Za-z0-9_.]{3,24}", username):
        return fail("Username must be 3–24 characters: letters, numbers, _ or .")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return fail("Enter a valid email address.")
    if len(pw) < 8:
        return fail("Password must be at least 8 characters.")
    try:
        cur = db().execute("INSERT INTO users(username,email,pw_hash) VALUES(?,?,?)",
                           (username, email, generate_password_hash(pw)))
        db().commit()
    except sqlite3.IntegrityError:
        return fail("That username or email is already registered.", 409)
    session.clear()
    session["uid"], session["username"] = cur.lastrowid, username
    return ok({"user": username})


@app.post("/api/login")
def login():
    d = request.get_json(silent=True) or {}
    ident = (d.get("identifier") or "").strip()
    pw = d.get("password") or ""
    row = db().execute("SELECT * FROM users WHERE username=? OR email=?", (ident, ident.lower())).fetchone()
    if not row or not check_password_hash(row["pw_hash"], pw):
        return fail("Wrong username/email or password.", 401)
    session.clear()
    session["uid"], session["username"] = row["id"], row["username"]
    return ok({"user": row["username"]})


@app.post("/api/logout")
def logout():
    session.clear()
    return ok({"ok": True})


@app.get("/api/watchlist")
@login_required
def wl_get():
    rows = db().execute("SELECT symbol FROM watchlist WHERE user_id=? ORDER BY added DESC", (session["uid"],)).fetchall()
    return ok({"symbols": [r["symbol"] for r in rows]})


@app.post("/api/watchlist/<sym>")
@login_required
def wl_add(sym):
    db().execute("INSERT OR IGNORE INTO watchlist(user_id,symbol) VALUES(?,?)", (session["uid"], norm(sym)))
    db().commit()
    return ok({"ok": True})


@app.delete("/api/watchlist/<sym>")
@login_required
def wl_del(sym):
    db().execute("DELETE FROM watchlist WHERE user_id=? AND symbol=?", (session["uid"], norm(sym)))
    db().commit()
    return ok({"ok": True})


# --------------------------------------------------------------------------
# Market API routes
# --------------------------------------------------------------------------
@app.get("/")
def home():
    return render_template("index.html")


@app.get("/api/search")
def api_search():
    return ok({"results": search_stocks(request.args.get("q", ""))})


@app.get("/api/indices")
def api_indices():
    items = [("NIFTY 50", "^NSEI"), ("SENSEX", "^BSESN"), ("BANK NIFTY", "^NSEBANK"),
             ("NIFTY IT", "^CNXIT"), ("INDIA VIX", "^INDIAVIX")]

    def one(it):
        try:
            q = quote_for(it[1])
            return {"name": it[0], "price": q["price"], "change": q["change"], "change_pct": q["change_pct"]}
        except Exception:  # noqa: BLE001
            return {"name": it[0], "price": None, "change": None, "change_pct": None}

    with ThreadPoolExecutor(5) as ex:
        rows = list(ex.map(one, items))
    return ok({"indices": rows, "market_open": market_open(),
               "time": datetime.now(IST).strftime("%d %b, %I:%M:%S %p IST")})


@app.get("/api/mmi")
def api_mmi():
    try:
        return ok(compute_mmi())
    except Exception as e:  # noqa: BLE001
        return fail(str(e), 502)


@app.get("/api/movers")
def api_movers():
    try:
        return ok(movers())
    except Exception as e:  # noqa: BLE001
        return fail(str(e), 502)


@app.get("/api/quote/<sym>")
def api_quote(sym):
    try:
        return ok(live_quote(norm(sym)))
    except Exception as e:  # noqa: BLE001
        return fail(str(e), 404)


@app.get("/api/quotes")
def api_quotes():
    syms = [norm(s) for s in request.args.get("symbols", "").split(",") if norm(s)][:30]

    def one(s):
        try:
            q = live_quote(s)
            return {"symbol": s, "price": q["price"], "change_pct": q["change_pct"]}
        except Exception:  # noqa: BLE001
            return {"symbol": s, "price": None, "change_pct": None}

    with ThreadPoolExecutor(8) as ex:
        return ok({"quotes": list(ex.map(one, syms))})


@app.get("/api/stock/<sym>")
def api_stock(sym):
    sym = norm(sym)
    if not sym:
        return fail("Enter a stock symbol.")
    try:
        with ThreadPoolExecutor(3) as ex:
            fq = ex.submit(live_quote, sym)
            ff = ex.submit(fundamentals, sym)
            fd = ex.submit(daily, sym)
            quote, fund, _ = fq.result(), ff.result(), fd.result()
        px = quote["price"]
        sigs, summary, levels, tech = technical_analysis(sym, px)
        fview, fnotes = fundamental_view(fund)
        verdict = make_verdict(px, summary, levels, tech, fview, fnotes)
        name = fund.get("name") or next((n for s, n in universe() if s == sym), sym)
        return ok({"symbol": sym, "name": name, "quote": quote, "fundamentals": fund,
                   "fundamental_view": fview, "fundamental_notes": fnotes,
                   "signals": sigs, "summary": summary, "levels": levels, "tech": tech,
                   "verdict": verdict, "market_open": market_open()})
    except Exception as e:  # noqa: BLE001
        print("[stock]", sym, repr(e))
        return fail(f"Couldn't load {sym}: {e}", 404)


RANGES = {"1D": ("1d", "5m", None), "5D": ("5d", "15m", None), "1M": (None, None, 22),
          "6M": (None, None, 126), "1Y": (None, None, 252), "5Y": (None, None, None)}


@app.get("/api/chart/<sym>")
def api_chart(sym):
    sym = norm(sym)
    rng = request.args.get("range", "6M")
    if rng not in RANGES:
        return fail("Unknown range.")
    period, interval, tail = RANGES[rng]
    try:
        is_intra = period is not None
        df = intraday(sym, period, interval) if is_intra else daily(sym)
        d = indicators(df)
        if tail:
            d = d.tail(tail)
        if is_intra:
            times = [int(ts.timestamp()) + 19800 for ts in d.index]  # show IST on the axis
        else:
            times = [ts.strftime("%Y-%m-%d") for ts in d.index]

        def line(col):
            return [{"time": t, "value": fnum(v)} for t, v in zip(times, d[col])]

        candles, vol = [], []
        for t, o, h, l, c, v in zip(times, d["Open"], d["High"], d["Low"], d["Close"], d["Volume"]):
            candles.append({"time": t, "open": o, "high": h, "low": l, "close": c})
            vol.append({"time": t, "value": v, "up": bool(c >= o)})
        return ok({"candles": candles, "volume": vol, "intraday": is_intra,
                   "sma20": line("sma20"), "sma50": line("sma50"), "sma200": line("sma200"),
                   "bb_up": line("bb_up"), "bb_lo": line("bb_lo"), "rsi": line("rsi")})
    except Exception as e:  # noqa: BLE001
        return fail(str(e), 404)


init_db()
threading.Thread(target=universe, daemon=True).start()  # warm the stock list

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  StockPulse running on http://127.0.0.1:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
