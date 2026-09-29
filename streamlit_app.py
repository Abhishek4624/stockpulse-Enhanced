"""
StockPulse - Streamlit version (for share.streamlit.io)
Uses the data + analysis code in app.py, so keep both files together.
"""
import html
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from werkzeug.security import check_password_hash, generate_password_hash

import app as core  # data, indicators, verdict and mood-index code

st.set_page_config(page_title="StockPulse", page_icon="📈", layout="wide")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,800&family=Figtree:wght@400;600&display=swap');
html, body, [class*="css"] { font-family: 'Figtree', sans-serif; }
h1, h2, h3 { font-family: 'Bricolage Grotesque', sans-serif !important; letter-spacing: -0.02em; }
.verdict { border-radius: 18px; padding: 22px 26px; color: #fff; }
.verdict .lbl { opacity: .85; font-size: .95rem; }
.verdict .act { font-family: 'Bricolage Grotesque', sans-serif; font-weight: 800; font-size: 2.3rem; line-height: 1.05; margin: 4px 0 8px; }
.verdict ul { margin: 12px 0 0; padding-left: 18px; font-size: .95rem; }
.zone-name { font-family: 'Bricolage Grotesque', sans-serif; font-weight: 800; font-size: 2rem; }
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------- helpers
def fmt(v, ty):
    if v is None:
        return "—"
    return {"x": f"{v:,.2f}x", "n": f"{v:,.2f}", "inr": f"₹{v:,.2f}", "cr": f"₹{v / 1e7:,.0f} Cr",
            "pc": f"{v:.1f}%", "vol": f"{v:,.0f}"}[ty]


def mmi_color(s):
    return "#e5484d" if s < 30 else "#f59f3a" if s < 50 else "#5fbf45" if s < 70 else "#12a672"


# ---------------------------------------------------------------- accounts (SQLite)
def dbc():
    c = sqlite3.connect(core.DB)
    c.row_factory = sqlite3.Row
    return c


def do_register(username, email, pw):
    username, email = username.strip(), email.strip().lower()
    if not re.fullmatch(r"[A-Za-z0-9_.]{3,24}", username):
        return "Username must be 3–24 characters: letters, numbers, _ or ."
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return "Enter a valid email address."
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    try:
        with dbc() as c:
            cur = c.execute("INSERT INTO users(username,email,pw_hash) VALUES(?,?,?)",
                            (username, email, generate_password_hash(pw)))
            uid = cur.lastrowid
    except sqlite3.IntegrityError:
        return "That username or email is already registered."
    st.session_state.user = {"id": uid, "name": username}
    return None


def do_login(ident, pw):
    ident = ident.strip()
    with dbc() as c:
        row = c.execute("SELECT * FROM users WHERE username=? OR email=?", (ident, ident.lower())).fetchone()
    if not row or not check_password_hash(row["pw_hash"], pw):
        return "Wrong username/email or password."
    st.session_state.user = {"id": row["id"], "name": row["username"]}
    return None


def wl_list(uid):
    with dbc() as c:
        return [r["symbol"] for r in c.execute(
            "SELECT symbol FROM watchlist WHERE user_id=? ORDER BY added DESC", (uid,))]


def wl_add(uid, sym):
    with dbc() as c:
        c.execute("INSERT OR IGNORE INTO watchlist(user_id,symbol) VALUES(?,?)", (uid, sym))


def wl_del(uid, sym):
    with dbc() as c:
        c.execute("DELETE FROM watchlist WHERE user_id=? AND symbol=?", (uid, sym))


def open_stock(sym):
    st.session_state.sym = sym
    st.session_state.page = "Stock analysis"


# ---------------------------------------------------------------- sidebar: login + watchlist
st.session_state.setdefault("page", "Market mood")
st.session_state.setdefault("sym", None)

with st.sidebar:
    st.markdown("## 📈 StockPulse")
    user = st.session_state.get("user")
    if user:
        st.write(f"Signed in as **{user['name']}**")
        if st.button("Log out"):
            del st.session_state["user"]
            st.rerun()
        st.markdown("#### My watchlist")
        wl = wl_list(user["id"])
        if not wl:
            st.caption("Empty. Open a stock and press “Add to watchlist”.")
        for s in wl:
            c1, c2 = st.columns([4, 1])
            c1.button(s, key=f"open_{s}", on_click=open_stock, args=(s,), use_container_width=True)
            c2.button("✕", key=f"rm_{s}", on_click=wl_del, args=(user["id"], s))
    else:
        mode = st.radio("Account", ["Log in", "Create account"], horizontal=True, label_visibility="collapsed")
        with st.form("auth_form"):
            if mode == "Log in":
                a = st.text_input("Username or email")
                p = st.text_input("Password", type="password")
                go_ = st.form_submit_button("Log in", use_container_width=True)
                if go_:
                    err = do_login(a, p)
                    if err:
                        st.error(err)
                    else:
                        st.rerun()
            else:
                u = st.text_input("Username")
                e = st.text_input("Email")
                p = st.text_input("Password (8+ characters)", type="password")
                go_ = st.form_submit_button("Create account", use_container_width=True)
                if go_:
                    err = do_register(u, e, p)
                    if err:
                        st.error(err)
                    else:
                        st.rerun()
        st.caption("Log in to keep a watchlist of your favourite stocks.")


# ---------------------------------------------------------------- top: index strip
@st.fragment(run_every=15)
def index_strip():
    items = [("NIFTY 50", "^NSEI"), ("SENSEX", "^BSESN"), ("BANK NIFTY", "^NSEBANK"),
             ("NIFTY IT", "^CNXIT"), ("INDIA VIX", "^INDIAVIX")]

    def one(it):
        try:
            return core.quote_for(it[1])
        except Exception:  # noqa: BLE001
            return None

    with ThreadPoolExecutor(5) as ex:
        qs = list(ex.map(one, items))
    cols = st.columns(len(items))
    for col, (name, _), q in zip(cols, items, qs):
        if q:
            col.metric(name, f"{q['price']:,.2f}", f"{q['change_pct']:+.2f}%",
                       delta_color="inverse" if name == "INDIA VIX" else "normal")
        else:
            col.metric(name, "—")
    st.caption(("🟢 Market open" if core.market_open() else "⚪ Market closed") +
               " · prices come from Yahoo Finance and may be delayed up to 15 minutes")


index_strip()

page = st.radio("View", ["Market mood", "Stock analysis", "Stock screener"], horizontal=True, key="page",
                label_visibility="collapsed")
st.divider()


# ---------------------------------------------------------------- page 1: market mood
def mood_page():
    st.title("Market Mood Index")
    st.write("A 0 to 100 score for how fearful or greedy Indian investors are right now.")
    with st.spinner("Reading the market. The first load can take about 20 seconds…"):
        try:
            d = core.compute_mmi()
        except Exception as e:  # noqa: BLE001
            st.error(f"Couldn't compute the mood index: {e}")
            return
    c1, c2 = st.columns([1, 1], gap="large")
    with c1:
        fig = go.Figure(go.Indicator(
            mode="gauge+number", value=round(d["score"]),
            gauge={"axis": {"range": [0, 100]}, "bar": {"color": "#121a4a", "thickness": 0.25},
                   "steps": [{"range": [0, 30], "color": "#e5484d"}, {"range": [30, 50], "color": "#f59f3a"},
                             {"range": [50, 70], "color": "#8bd46e"}, {"range": [70, 100], "color": "#12a672"}]}))
        fig.update_layout(height=300, margin=dict(l=20, r=20, t=30, b=0))
        st.plotly_chart(fig, use_container_width=True)
        st.markdown(f"<div class='zone-name' style='color:{mmi_color(d['score'])}'>{html.escape(d['zone'])}</div>",
                    unsafe_allow_html=True)
        st.write(d["advice"])
        st.caption("Updated " + d["updated"])
    with c2:
        st.markdown("#### What is moving the score")
        for comp in d["components"]:
            st.markdown(f"**{comp['name']}**: {round(comp['score'])}/100")
            st.progress(int(comp["score"]) / 100, text=comp["detail"])
    st.divider()
    try:
        mv = core.movers()
        g1, g2 = st.columns(2)
        for col, title, key in ((g1, "Top gainers (Nifty 50)", "gainers"), (g2, "Top losers (Nifty 50)", "losers")):
            col.markdown(f"#### {title}")
            df = pd.DataFrame(mv[key]).rename(columns={"symbol": "Stock", "price": "Price", "change_pct": "Change %"})
            col.dataframe(df, hide_index=True, use_container_width=True,
                          column_config={"Price": st.column_config.NumberColumn(format="₹%.2f"),
                                         "Change %": st.column_config.NumberColumn(format="%+.2f%%")})
    except Exception as e:  # noqa: BLE001
        st.caption(f"Movers unavailable: {e}")


# ---------------------------------------------------------------- page 2: stock analysis
@st.cache_data(ttl=45, show_spinner=False)
def load_stock(sym):
    quote = core.live_quote(sym)
    fund = core.fundamentals(sym)
    core.daily(sym)
    px = quote["price"]
    sigs, summary, levels, tech = core.technical_analysis(sym, px)
    fview, fnotes = core.fundamental_view(fund)
    verdict = core.make_verdict(px, summary, levels, tech, fview, fnotes)
    name = fund.get("name") or next((n for s, n in core.universe() if s == sym), sym)
    return {"symbol": sym, "name": name, "quote": quote, "fund": fund, "fview": fview, "fnotes": fnotes,
            "signals": sigs, "summary": summary, "levels": levels, "tech": tech, "verdict": verdict}


@st.fragment(run_every=10)
def live_header(sym, name, sector):
    try:
        q = core.live_quote(sym)
    except Exception as e:  # noqa: BLE001
        st.error(str(e))
        return
    c1, c2 = st.columns([2, 1])
    c1.markdown(f"## {name}")
    c1.caption(f"NSE: {sym}" + (f" · {sector}" if sector else "") +
               (" · 🟢 Live" if core.market_open() else " · Market closed, showing last close"))
    c2.metric("Price", f"₹{q['price']:,.2f}", f"{q['change']:+.2f} ({q['change_pct']:+.2f}%)")


def zone_figure(v, px):
    lo = min(v["stop_loss"], px) * 0.995
    hi = max(v["target"], px) * 1.005
    fig = go.Figure()
    fig.add_shape(type="rect", x0=v["zone_low"], x1=v["zone_high"], y0=0.25, y1=0.75,
                  fillcolor="#5cd0a0", line_width=0, opacity=0.8)
    for x, label, col in ((v["stop_loss"], "Stop", "#dc3348"), (px, "Now", "#121a4a"), (v["target"], "Target", "#0f9d68")):
        fig.add_shape(type="line", x0=x, x1=x, y0=0.05, y1=0.95, line=dict(color=col, width=3))
        fig.add_annotation(x=x, y=1.05, text=f"{label}<br>₹{x:,.0f}", showarrow=False, font=dict(color=col, size=12))
    fig.update_xaxes(range=[lo, hi], showgrid=False, tickprefix="₹")
    fig.update_yaxes(visible=False, range=[0, 1.4])
    fig.update_layout(height=190, margin=dict(l=10, r=10, t=10, b=30), plot_bgcolor="white")
    return fig


def price_chart(sym, rng, show_bb):
    period, interval, tail = core.RANGES[rng]
    intra = period is not None
    df = core.intraday(sym, period, interval) if intra else core.daily(sym)
    d = core.indicators(df)
    if tail:
        d = d.tail(tail)
    x = d.index
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, row_heights=[0.62, 0.14, 0.24], vertical_spacing=0.025)
    fig.add_trace(go.Candlestick(x=x, open=d["Open"], high=d["High"], low=d["Low"], close=d["Close"], name="Price",
                                 increasing_line_color="#0f9d68", decreasing_line_color="#dc3348"), row=1, col=1)
    for col, name, color in (("sma20", "SMA 20", "#ff9d1f"), ("sma50", "SMA 50", "#2b3796"), ("sma200", "SMA 200", "#8a4bd6")):
        fig.add_trace(go.Scatter(x=x, y=d[col], name=name, line=dict(color=color, width=1.6)), row=1, col=1)
    if show_bb:
        for col, name in (("bb_up", "Bollinger upper"), ("bb_lo", "Bollinger lower")):
            fig.add_trace(go.Scatter(x=x, y=d[col], name=name, line=dict(color="#9aa3d6", width=1, dash="dot")), row=1, col=1)
    vcol = ["rgba(15,157,104,.45)" if c >= o else "rgba(220,51,72,.45)" for o, c in zip(d["Open"], d["Close"])]
    fig.add_trace(go.Bar(x=x, y=d["Volume"], marker_color=vcol, name="Volume", showlegend=False), row=2, col=1)
    fig.add_trace(go.Scatter(x=x, y=d["rsi"], name="RSI (14)", line=dict(color="#2b3796", width=1.8)), row=3, col=1)
    fig.add_hline(y=70, line=dict(color="#dc3348", dash="dash", width=1), row=3, col=1)
    fig.add_hline(y=30, line=dict(color="#0f9d68", dash="dash", width=1), row=3, col=1)
    breaks = [dict(bounds=["sat", "mon"])]
    if intra:
        breaks.append(dict(bounds=[15.5, 9.25], pattern="hour"))
    fig.update_xaxes(rangebreaks=breaks, rangeslider_visible=False)
    fig.update_yaxes(title_text="Price (₹)", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)
    fig.update_yaxes(title_text="RSI", range=[0, 100], row=3, col=1)
    fig.update_layout(height=680, template="plotly_white", margin=dict(l=10, r=10, t=10, b=10),
                      legend=dict(orientation="h", y=1.03), hovermode="x unified")
    return fig


def stock_page():
    opts = [f"{s} — {n}" for s, n in core.universe()]

    def on_pick():
        v = st.session_state.get("pick")
        if v:
            st.session_state.sym = v.split(" — ")[0]

    def on_type():
        v = core.norm(st.session_state.get("typed", ""))
        if v:
            st.session_state.sym = v

    c1, c2 = st.columns([3, 2])
    c1.selectbox("Search any NSE stock", opts, index=None, key="pick", on_change=on_pick,
                 placeholder="Type a company name or symbol, e.g. Reliance")
    c2.text_input("Or type an NSE symbol", key="typed", on_change=on_type, placeholder="e.g. TCS, M&M, IRCTC")

    sym = core.norm(st.session_state.get("sym") or "")
    if not sym:
        st.info("Search for a stock above to see its live price, technicals, fundamentals and buy verdict.")
        return
    try:
        with st.spinner(f"Loading {sym}…"):
            d = load_stock(sym)
    except Exception as e:  # noqa: BLE001
        st.error(f"We couldn't load {sym}: {e}")
        st.caption("Check the NSE symbol (for example RELIANCE, TCS or M&M).")
        return

    q, f, v, t = d["quote"], d["fund"], d["verdict"], d["tech"]
    live_header(sym, d["name"], f.get("sector"))

    user = st.session_state.get("user")
    if user:
        on = sym in wl_list(user["id"])
        if st.button("★ Remove from watchlist" if on else "☆ Add to watchlist"):
            (wl_del if on else wl_add)(user["id"], sym)
            st.rerun()
    else:
        st.caption("Log in from the sidebar to add this stock to a watchlist.")

    # ----- verdict
    bg = {"buy": "linear-gradient(135deg,#0b7a52,#12a672)", "sell": "linear-gradient(135deg,#a51d31,#dc3348)",
          "neutral": "linear-gradient(135deg,#1c2670,#3341a8)"}[v["tone"]]
    lis = "".join(f"<li>{html.escape(r)}</li>" for r in v["reasons"])
    vc1, vc2 = st.columns([1, 1.15], gap="large")
    with vc1:
        st.markdown(f"<div class='verdict' style='background:{bg}'><div class='lbl'>Verdict at ₹{q['price']:,.2f}</div>"
                    f"<div class='act'>{html.escape(v['action'])}</div><div>{html.escape(v['headline'])}</div>"
                    f"<ul>{lis}</ul></div>", unsafe_allow_html=True)
    with vc2:
        st.markdown("#### Price zone")
        m1, m2, m3 = st.columns(3)
        m1.metric("Buy zone", f"₹{v['zone_low']:,.0f} – ₹{v['zone_high']:,.0f}")
        m2.metric("Stop-loss", f"₹{v['stop_loss']:,.2f}")
        m3.metric("Target", f"₹{v['target']:,.2f}")
        st.plotly_chart(zone_figure(v, q["price"]), use_container_width=True)
        rr = f" Risk to reward from here is about 1 : {v['risk_reward']:.1f}." if v["risk_reward"] else ""
        st.caption(("The price is inside or close to the value zone." if v["in_zone"]
                    else "The price is above the value zone.") + rr)

    # ----- chart
    st.markdown("### Chart")
    cc1, cc2 = st.columns([3, 1])
    rng = cc1.radio("Range", list(core.RANGES.keys()), index=3, horizontal=True, label_visibility="collapsed")
    bb = cc2.checkbox("Bollinger Bands")
    try:
        st.plotly_chart(price_chart(sym, rng, bb), use_container_width=True)
    except Exception as e:  # noqa: BLE001
        st.warning(str(e))

    # ----- fundamentals
    st.markdown(f"### Fundamentals: {d['fview']}")
    F = [("P/E ratio", f["pe"], "x", "Price you pay for each ₹1 of yearly profit."),
         ("Forward P/E", f["forward_pe"], "x", "P/E using expected profits for the next year."),
         ("P/B ratio", f["pb"], "x", "Price compared with book value per share."),
         ("EPS (TTM)", f["eps"], "inr", "Profit per share over the last 12 months."),
         ("EBITDA", f["ebitda"], "cr", "Operating profit before interest, tax, depreciation and amortisation."),
         ("EV / EBITDA", f["ev_ebitda"], "x", "What the whole business costs versus its operating profit."),
         ("Market cap", f["market_cap"], "cr", "Total value of all shares."),
         ("Revenue", f["revenue"], "cr", "Sales over the last 12 months."),
         ("Net profit", f["net_income"], "cr", "Profit after all costs, last 12 months."),
         ("ROE", f["roe"], "pc", "Profit earned on shareholders' money."),
         ("ROA", f["roa"], "pc", "Return on assets."),
         ("Net margin", f["profit_margin"], "pc", "Share of sales that becomes profit."),
         ("Operating margin", f["operating_margin"], "pc", "Share of sales left after operating costs."),
         ("Revenue growth", f["revenue_growth"], "pc", "Latest yearly change in sales."),
         ("Earnings growth", f["earnings_growth"], "pc", "Latest yearly change in profit."),
         ("Debt / Equity", f["debt_to_equity"], "n", "Borrowings versus shareholders' money. Lower is safer."),
         ("Dividend yield", f["dividend_yield"], "pc", "Yearly dividend as a % of the price."),
         ("Beta", f["beta"], "n", "How much the stock swings versus the market. 1 = moves with it."),
         ("52-week high", t["high52"], "inr", "Highest price in the last year."),
         ("52-week low", t["low52"], "inr", "Lowest price in the last year.")]
    for i in range(0, len(F), 5):
        cols = st.columns(5)
        for col, (lab, val, ty, tip) in zip(cols, F[i:i + 5]):
            col.metric(lab, fmt(val, ty), help=tip)
    for n in d["fnotes"]:
        st.markdown(("🟢 " if n["tone"] == "buy" else "🔴 ") + n["text"])

    # ----- technicals
    st.markdown("### Technical indicators")
    sm = d["summary"]
    k1, k2, k3 = st.columns(3)
    k1.metric("Buy signals", sm["buy"])
    k2.metric("Neutral", sm["neutral"])
    k3.metric("Sell signals", sm["sell"])
    icon = {"buy": "🟢 Buy", "sell": "🔴 Sell", "neutral": "⚪ Neutral"}
    tdf = pd.DataFrame([{"Group": s["group"], "Indicator": s["name"], "Value": s["value"],
                         "Signal": icon[s["signal"]], "What it means": s["note"]} for s in d["signals"]])
    st.dataframe(tdf, hide_index=True, use_container_width=True,
                 column_config={"Value": st.column_config.NumberColumn(format="%.2f")})
    st.markdown("#### Support and resistance")
    L = d["levels"]
    for col, (lab, val) in zip(st.columns(5), (("S2", L["s2"]), ("S1", L["s1"]), ("Pivot", L["pivot"]),
                                                ("R1", L["r1"]), ("R2", L["r2"]))):
        col.metric(lab, f"₹{val:,.1f}")


# ---------------------------------------------------------------- page 3: stock screener
PRESETS = {
    "Buy signals right now": ("Stocks where the trend is up and the price is near its buy zone.",
                              {"actions": ["BUY"]}),
    "Oversold in an uptrend": ("RSI below 35 but still above the 200-day average: a dip inside a long-term uptrend.",
                               {"rsi_max": 35, "above_sma200": True}),
    "Strong uptrend": ("Above the 50 and 200-day averages, MACD bullish and a strong trend (ADX 25+).",
                       {"above_sma50": True, "above_sma200": True, "macd_bull": True, "adx_min": 25}),
    "Breakout near 52-week high": ("Within 3% of the yearly high, above the 50-day average, with higher volume.",
                                   {"max_from_high": 3, "vol_ratio_min": 1.2, "above_sma50": True}),
    "Golden cross": ("50-day average is above the 200-day average and the price is above the 50-day.",
                     {"golden": True, "above_sma50": True}),
    "Quality at a fair price": ("P/E under 25, ROE above 15%, debt/equity under 1, and above the 200-day average.",
                                {"pe_max": 25, "roe_min": 15, "de_max": 1.0, "above_sma200": True}),
    "Custom filters": ("Set your own filters below.", None),
}
ACTIONS = ["BUY", "WAIT FOR A DIP", "HOLD / WATCH", "AVOID"]


def custom_criteria():
    c = {}
    a, b = st.columns(2)
    lo, hi = a.slider("RSI range", 0, 100, (0, 100), help="Below 30 is oversold, above 70 is overbought.")
    c["rsi_min"], c["rsi_max"] = lo, hi
    c["min_change"] = b.slider("Minimum change today (%)", -10.0, 10.0, -10.0, 0.5)
    k1, k2, k3, k4 = st.columns(4)
    c["above_sma50"] = k1.checkbox("Above 50-day average")
    c["above_sma200"] = k2.checkbox("Above 200-day average")
    c["macd_bull"] = k3.checkbox("MACD bullish")
    c["golden"] = k4.checkbox("Golden cross (50 > 200)")
    d1, d2, d3 = st.columns(3)
    mh = d1.slider("Within % of 52-week high", 0, 100, 100, help="100 means no limit.")
    c["max_from_high"] = mh if mh < 100 else 1000
    c["vol_ratio_min"] = d2.slider("Volume vs 20-day average (min ×)", 0.0, 5.0, 0.0, 0.1)
    c["adx_min"] = d3.slider("Minimum trend strength (ADX)", 0, 50, 0)
    c["actions"] = st.multiselect("Verdict is one of", ACTIONS, default=ACTIONS)
    with st.expander("Fundamental filters (optional, a bit slower)"):
        st.caption("Checked only for stocks that already passed the filters above.")
        f1, f2, f3 = st.columns(3)
        if f1.checkbox("Max P/E"):
            c["pe_max"] = f1.number_input("P/E at most", 1.0, 500.0, 25.0)
        if f2.checkbox("Min ROE"):
            c["roe_min"] = f2.number_input("ROE at least (%)", -50.0, 100.0, 15.0)
        if f3.checkbox("Max debt/equity"):
            c["de_max"] = f3.number_input("Debt/Equity at most", 0.0, 20.0, 1.0)
        g1, g2 = st.columns(2)
        if g1.checkbox("Min market cap"):
            c["mcap_min_cr"] = g1.number_input("Market cap at least (₹ Cr)", 0.0, 2000000.0, 5000.0, 500.0)
        if g2.checkbox("Min revenue growth"):
            c["rev_growth_min"] = g2.number_input("Revenue growth at least (%)", -50.0, 200.0, 10.0)
    return c


def run_scan(symbols):
    chunks = [tuple(symbols[i:i + 100]) for i in range(0, len(symbols), 100)]
    rows, bar = [], st.progress(0, text="Starting scan…")
    for i, ch in enumerate(chunks):
        try:
            rows += core.scan_chunk(ch)
        except Exception as e:  # noqa: BLE001
            st.warning(f"One batch failed and was skipped: {e}")
        bar.progress((i + 1) / len(chunks), text=f"Scanned {min((i + 1) * 100, len(symbols))} of {len(symbols)} stocks")
    bar.empty()
    return rows


def screener_page():
    st.title("Stock Screener")
    st.write("Scan many stocks at once and keep only the ones that match your rules.")

    uni = st.radio("Which stocks should I scan?", ["Nifty 50 (fast)", "All NSE stocks (slow)", "My own list"],
                   horizontal=True)
    symbols = []
    if uni.startswith("Nifty"):
        symbols = list(core.NIFTY50)
    elif uni.startswith("All"):
        allsyms = [s for s, _ in core.universe()]
        n = st.slider("How many stocks to scan", 50, max(len(allsyms), 51), min(300, len(allsyms)), 50,
                      help="Scanning is done in batches of 100. Larger scans can take several minutes.")
        symbols = allsyms[:n]
        st.caption(f"{len(allsyms)} NSE stocks are available; scanning the first {len(symbols)} in alphabetical order.")
    else:
        txt = st.text_area("Type NSE symbols separated by commas", "RELIANCE, TCS, INFY, HDFCBANK, ITC")
        symbols = list(dict.fromkeys(core.norm(x) for x in re.split(r"[,\s]+", txt) if core.norm(x)))[:300]

    preset = st.selectbox("What are you looking for?", list(PRESETS.keys()))
    desc, crit = PRESETS[preset]
    st.caption(desc)
    if crit is None:
        crit = custom_criteria()

    if st.button("🔍 Run screener", type="primary", disabled=not symbols):
        with st.spinner("Scanning… the first run downloads a year of prices for every stock."):
            rows = run_scan(symbols)
        passed = core.apply_filters(rows, crit)
        needs_f = any(crit.get(k) is not None for k in ("pe_max", "roe_min", "de_max", "mcap_min_cr", "rev_growth_min"))
        truncated = False
        if needs_f and passed:
            with st.spinner("Checking fundamentals…"):
                passed, truncated = core.fundamental_filter(passed, crit)
        st.session_state.screen = {"rows": passed, "scanned": len(rows), "asked": len(symbols),
                                   "truncated": truncated}

    res = st.session_state.get("screen")
    if not res:
        st.info("Pick what you are looking for and press **Run screener**.")
        return
    st.markdown(f"### {len(res['rows'])} matches out of {res['scanned']} stocks scanned")
    if res["asked"] > res["scanned"]:
        st.caption(f"{res['asked'] - res['scanned']} stocks had no usable price data and were skipped.")
    if res["truncated"]:
        st.caption("Many stocks matched, so fundamentals were checked only for the 80 with the best signal score.")
    if not res["rows"]:
        st.warning("No stock matches right now. Try loosening a filter.")
        return

    sort_opts = {"Signal score (best first)": ("score", False), "RSI (lowest first)": ("rsi", True),
                 "Change today (highest first)": ("change_pct", False),
                 "Closest to 52-week high": ("from_high", True), "Volume spike (highest first)": ("vol_ratio", False),
                 "3-month return (highest first)": ("ret_3m", False)}
    sort_by = st.selectbox("Sort by", list(sort_opts.keys()))
    key, asc = sort_opts[sort_by]
    df = pd.DataFrame(res["rows"]).sort_values(key, ascending=asc, na_position="last")
    show = pd.DataFrame({"Stock": df["symbol"], "Price": df["price"], "Today %": df["change_pct"],
                         "Verdict": df["verdict"], "Signal score": df["score"], "RSI": df["rsi"],
                         "Below 52W high %": df["from_high"], "Volume ×": df["vol_ratio"],
                         "1M %": df["ret_1m"], "3M %": df["ret_3m"]})
    for col, lab in (("pe", "P/E"), ("roe", "ROE %"), ("de", "Debt/Equity"), ("mcap_cr", "Mkt cap ₹ Cr")):
        if col in df.columns:
            show[lab] = df[col]
    fm = lambda f: st.column_config.NumberColumn(format=f)  # noqa: E731
    st.dataframe(show, hide_index=True, use_container_width=True, column_config={
        "Price": fm("₹%.2f"), "Today %": fm("%+.2f%%"), "Signal score": fm("%.0f"), "RSI": fm("%.0f"),
        "Below 52W high %": fm("%.1f%%"), "Volume ×": fm("%.1fx"), "1M %": fm("%+.1f%%"), "3M %": fm("%+.1f%%"),
        "P/E": fm("%.1f"), "ROE %": fm("%.1f"), "Debt/Equity": fm("%.2f"), "Mkt cap ₹ Cr": fm("%.0f")})
    st.caption("Signal score runs from -100 (all indicators bearish) to +100 (all bullish). "
               "During market hours today's volume is still building, so Volume × can look low.")

    c1, c2, c3 = st.columns([2, 1, 1])
    pick = c1.selectbox("Open a stock from the results", list(show["Stock"]), label_visibility="collapsed")
    c2.button("Analyse this stock →", on_click=open_stock, args=(pick,), use_container_width=True)
    c3.download_button("Download CSV", show.to_csv(index=False).encode("utf-8"), "stockpulse_screener.csv",
                       "text/csv", use_container_width=True)


if page == "Market mood":
    mood_page()
elif page == "Stock analysis":
    stock_page()
else:
    screener_page()

st.divider()
st.caption("Signals are rule-based on past prices and are for learning, not investment advice. "
           "Please talk to a SEBI-registered adviser before you invest.")
