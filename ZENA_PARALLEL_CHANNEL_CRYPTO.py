# -*- coding: utf-8 -*-
"""ZENA Parallel Channel Scanner. Signal/watchlist only; no auto-trading."""
from __future__ import annotations
import os, sys, time, json, math, re, csv, argparse, logging, subprocess, importlib
from pathlib import Path
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

APP_KIND = "CRYPTO"   # STOCK or CRYPTO
VERSION = "ZENA PARALLEL CHANNEL CRYPTO V1.4 — MIN LIKUIDITAS 100 JUTA, TELEGRAM ONLY"
ROOT = Path(__file__).resolve().parent

# Existing Telegram destination is retained; environment variables override it.
# Do not share this file publicly because it contains the bot credential.
TELEGRAM_BOT_TOKEN = os.getenv("ZENA_TELEGRAM_BOT_TOKEN", "8979917342:AAETt_7wWXO_TzzYPVTLftW5-0W8MBlto2I").strip()
TELEGRAM_CHAT_ID = os.getenv("ZENA_TELEGRAM_CHAT_ID", "8049352232").strip()

# Parallel-channel work-order defaults; tune only after backtesting / paper trading.
TOP_N = max(1, int(os.getenv("ZENA_CHANNEL_TOP_N", "5")))
LOOKBACK = max(80, min(250, int(os.getenv("ZENA_CHANNEL_LOOKBACK", "180"))))
PIVOT_LEFT = max(2, int(os.getenv("ZENA_CHANNEL_PIVOT_LEFT", "3")))
PIVOT_RIGHT = max(2, int(os.getenv("ZENA_CHANNEL_PIVOT_RIGHT", "3")))
MIN_RR = max(1.0, float(os.getenv("ZENA_CHANNEL_MIN_RR", "1.5")))
BREAKOUT_ATR_BUFFER = max(0.1, float(os.getenv("ZENA_CHANNEL_BREAKOUT_ATR", "0.35")))
BREAKOUT_VOLUME_RATIO = max(1.0, float(os.getenv("ZENA_CHANNEL_BREAKOUT_VOLUME", "1.5")))
BOUNCE_VOLUME_RATIO = max(1.0, float(os.getenv("ZENA_CHANNEL_BOUNCE_VOLUME", "1.2")))
MIN_CHANNEL_QUALITY = max(0, min(100, int(os.getenv("ZENA_CHANNEL_MIN_QUALITY", "55"))))
MAX_WORKERS = max(2, min(12, int(os.getenv("ZENA_CHANNEL_WORKERS", "6"))))
MAX_SYMBOLS = max(0, int(os.getenv("ZENA_CHANNEL_MAX_SYMBOLS", "0"))) # default ALL; 0 = no symbol cap
HTTP_TIMEOUT = max(5, int(os.getenv("ZENA_HTTP_TIMEOUT", "12")))

if APP_KIND == "STOCK":
    SETUP_TF = {"day": "15m", "swing": "1d"}
    CONTEXT_TF = {"day": "1h", "swing": "1wk"}
    MIN_TURNOVER = {"day": float(os.getenv("ZENA_IDX_MIN_TURNOVER_DAY", "1000000000")),
                    "swing": float(os.getenv("ZENA_IDX_MIN_TURNOVER_SWING", "100000000"))}
    UNIVERSE_CSV = ROOT / "idx_universe.csv"
    OUTPUT_CSV = ROOT / "zena_parallel_channel_saham.csv"
    OUTPUT_TXT = ROOT / "zena_parallel_channel_saham.txt"
    LOG_FILE = ROOT / "zena_parallel_channel_saham.log"
    IDXC_HOME = "https://www.idx.co.id/"
    IDXC_PROFILES = "https://www.idx.co.id/primary/ListedCompany/GetCompanyProfiles"
    FALLBACK_UNIVERSE = """BBCA BMRI BBRI BBNI BRIS BSSR ANTM TLKM PGAS ASII AMMN MDKA INCO TINS BUMI ADRO AADI PTBA ITMG UNTR ICBP INDF KLBF GOTO BREN DSSA CUAN PANI BRPT CPIN JPFA SMGR TOWR EXCL ISAT EMTK SIDO ACES MAPI ERAA HEAL MIKA JSMR CTRA BSDE PWON UNVR MEDC AKRA PGEO RAJA ESSA HRUM MBMA NCKL SRTG AVIA MYOR AMRT GGRM HMSP BBTN BTPS BNGA PNBN ARTO MTEL BUKA BRMS ELSA TAPG LSIP DSNG SSMS WIFI DATA KAEF INAF ADMR ADRO HRTA CLEO COIN EMTK ENRG FIRE GJTL GOOD INKP INTP JPFA KINO MAIN MCAS MLPL NISP OASA OMED PPRO PTRO PTPP PWON RALS SCMA SKBM SMCB SMRA SSIA TKIM TKIM TPIA TRIM WTON ZINC ACST AGII AKPI ANTM ASRI AUTO BIRD BJBR BJTM BMTR BNLI BRPT BSDE BTPS CASS CENT CMRY CPIN CTRA DMAS DOID ELSA ERAA ESSA EXCL FASW FREN GEMS GMFI HMSP HRUM IMAS IMPC INCO INDF INKP INTP ISAT ITMG JPFA JPFA JPFA JSMR KIJA KLBF LPPF LSIP MAPA MAPI MBMA MDKA MEDC MIDI MIKA MINA MKPI MLBI MNCN MPPA MTEL MTLA MYOR NCKL NELY NICL NISP NIKL NRCA OASA PANI PBRX PGAS PGEO PGJO PJAA PNBN PNLF PNIN PPRE PTBA PTIS PTMP PTPP PTRO PTSN PUDP PWON RAJA RALS RANC SCMA SIDO SILO SIMP SMBR SMDR SMGR SMRA SMSM SRTG SSIA SSMS TAPG TBIG TBLA TCPI TINS TKIM TLKM TMAS TOBA TOTL TOWR TPIA TRGU TSPC ULTJ UNTR UNVR VICI WIKA WIFI WINS WOOD ZONE""".split()
    DEFAULT_QUOTE_CURRENCY = "IDR"
else:
    SETUP_TF = {"day": "15m", "swing": "4h"}
    CONTEXT_TF = {"day": "1h", "swing": "1d"}
    OUTPUT_CSV = ROOT / "zena_parallel_channel_crypto.csv"
    OUTPUT_TXT = ROOT / "zena_parallel_channel_crypto.txt"
    LOG_FILE = ROOT / "zena_parallel_channel_crypto.log"
    CRYPTO_EXCHANGE = os.getenv("ZENA_CRYPTO_EXCHANGE", "indodax").strip().lower()
    DEFAULT_QUOTE_CURRENCY = "IDR" if CRYPTO_EXCHANGE == "indodax" else "USDT"
    MIN_QUOTE_VOLUME = {"day": float(os.getenv("ZENA_CRYPTO_MIN_QV_DAY", "100000000" if DEFAULT_QUOTE_CURRENCY == "IDR" else "3000000")),
                        "swing": float(os.getenv("ZENA_CRYPTO_MIN_QV_SWING", "100000000" if DEFAULT_QUOTE_CURRENCY == "IDR" else "750000"))}
    STABLECOINS = {"USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDE", "USDD", "PYUSD", "USDP", "IDRT", "USD1", "USTC", "UST"}
    LEVERAGED_MARKERS = ("3L", "3S", "5L", "5S", "BULL", "BEAR", "UP", "DOWN")

# Optional dependency installation so a normal Windows Python can run the file.
def ensure_package(import_name, package_name=None):
    try:
        importlib.import_module(import_name)
    except ImportError:
        pkg = package_name or import_name
        # Quiet mode: keep installation output off the console.
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", pkg],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

ensure_package("numpy")
ensure_package("pandas")
ensure_package("requests")
if APP_KIND == "CRYPTO":
    ensure_package("ccxt")

import numpy as np
import pandas as pd
import requests
if APP_KIND == "CRYPTO":
    import ccxt

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s",
                    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")])
log = logging.getLogger("ZENA_CHANNEL")

INTERVAL_SECONDS = {"1m":60, "5m":300, "15m":900, "30m":1800, "1h":3600, "4h":14400,
                    "1d":86400, "1wk":604800, "1w":604800}


def finite(value, default=np.nan):
    try:
        x = float(value)
        return x if np.isfinite(x) else default
    except Exception:
        return default


def fmt_num(x, digits=2):
    v = finite(x)
    return "N/A" if not np.isfinite(v) else f"{v:,.{digits}f}"


def fmt_price(x, quote="IDR"):
    v = finite(x)
    if not np.isfinite(v) or v <= 0:
        return "N/A"
    if quote == "IDR":
        return "Rp " + f"{v:,.0f}".replace(",", ".")
    return f"{v:,.6f}".rstrip("0").rstrip(".") + f" {quote}"


def fmt_pct(x):
    v = finite(x)
    return "N/A" if not np.isfinite(v) else f"{v:+.1f}%"


def api_json(url, params=None, headers=None):
    h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36",
         "Accept": "application/json,text/plain,*/*"}
    if headers:
        h.update(headers)
    r = requests.get(url, params=params, headers=h, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def interval_seconds(interval):
    return INTERVAL_SECONDS.get(interval, 86400)


def clean_ohlcv(df):
    if df is None or df.empty:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    x = df.copy()
    x["time"] = pd.to_datetime(x["time"], utc=True, errors="coerce")
    for c in ("open", "high", "low", "close", "volume"):
        x[c] = pd.to_numeric(x[c], errors="coerce")
    x = x.replace([np.inf, -np.inf], np.nan).dropna(subset=["time", "open", "high", "low", "close"])
    x = x[(x["close"] > 0) & (x["high"] >= x["low"])].sort_values("time").drop_duplicates("time")
    if "volume" not in x:
        x["volume"] = np.nan
    return x.reset_index(drop=True)


def drop_unclosed(df, interval, stock=False):
    """Use completed candles only. Stock daily/weekly current periods are excluded conservatively."""
    x = clean_ohlcv(df)
    if x.empty:
        return x
    now = pd.Timestamp.now(tz="UTC")
    if stock and interval == "1d":
        local_today = pd.Timestamp.now(tz="Asia/Jakarta").date()
        local_dates = x["time"].dt.tz_convert("Asia/Jakarta").dt.date
        x = x.loc[local_dates < local_today]
    elif stock and interval in ("1wk", "1w"):
        local_now = pd.Timestamp.now(tz="Asia/Jakarta")
        monday = (local_now - pd.Timedelta(days=local_now.weekday())).date()
        local_dates = x["time"].dt.tz_convert("Asia/Jakarta").dt.date
        x = x.loc[local_dates < monday]
    else:
        cutoff = now - pd.Timedelta(seconds=interval_seconds(interval))
        x = x.loc[x["time"] <= cutoff]
    return x.reset_index(drop=True)


def fetch_yahoo(symbol, interval, days=500):
    """Yahoo chart API for IDX stocks and IHSG; applies split/dividend adjustment when provided."""
    end = int(time.time())
    # Yahoo limits intraday history; keep within supported window.
    if interval in ("1m", "5m"):
        days = min(days, 7 if interval == "1m" else 59)
    elif interval in ("15m", "30m", "1h"):
        days = min(days, 59)
    start = end - int(days * 86400)
    encoded = requests.utils.quote(symbol, safe="^.-")
    last_error = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            payload = api_json(f"https://{host}/v8/finance/chart/{encoded}",
                               {"period1": start, "period2": end, "interval": interval,
                                "events": "div,splits"})
            results = payload.get("chart", {}).get("result") or []
            if not results:
                continue
            r = results[0]
            ts = r.get("timestamp") or []
            if not ts:
                continue
            quote = ((r.get("indicators") or {}).get("quote") or [{}])[0]
            adj = (((r.get("indicators") or {}).get("adjclose") or [{}])[0]).get("adjclose") or []
            n = len(ts)
            def aligned(k):
                values = quote.get(k)
                return values if isinstance(values, list) and len(values) == n else [np.nan] * n
            x = pd.DataFrame({"time": pd.to_datetime(ts, unit="s", utc=True), "open": aligned("open"),
                              "high": aligned("high"), "low": aligned("low"), "close": aligned("close"),
                              "volume": aligned("volume")})
            # Normalize historic prices for split/dividend adjustments if Yahoo supplies adjclose.
            # Latest candle remains on its current price scale; raw volume is retained.
            if interval in ("1d", "1wk", "1w") and len(adj) == len(x):
                raw_close = pd.to_numeric(x["close"], errors="coerce")
                adj_close = pd.to_numeric(pd.Series(adj), errors="coerce")
                factor = (adj_close / raw_close.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).fillna(1.0)
                for c in ("open", "high", "low", "close"):
                    x[c] = pd.to_numeric(x[c], errors="coerce") * factor.values
            return drop_unclosed(x, interval, stock=(APP_KIND == "STOCK"))
        except Exception as e:
            last_error = e
    log.debug("Yahoo data unavailable %s %s (%s)", symbol, interval, type(last_error).__name__ if last_error else "empty")
    return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])


def discover_idx_universe():
    """Prefer idx_universe.csv, then public IDX company profiles, then a clearly limited fallback."""
    if UNIVERSE_CSV.exists():
        try:
            tab = pd.read_csv(UNIVERSE_CSV)
            col = next((c for c in tab.columns if c.lower() in ("symbol", "ticker", "code", "kode", "kodeemiten")), tab.columns[0])
            out = sorted({str(v).strip().upper().replace(".JK", "") for v in tab[col].dropna()
                          if re.fullmatch(r"[A-Z0-9]{4,5}", str(v).strip().upper().replace(".JK", ""))})
            if len(out) >= 10:
                log.info("Universe dari %s: %s ticker", UNIVERSE_CSV.name, len(out))
                return out, "idx_universe.csv"
        except Exception as e:
            log.warning("CSV universe gagal dibaca: %s", type(e).__name__)
    try:
        # Bootstrap website first; if direct requests get blocked, use the CSV/fallback path.
        sess = requests.Session()
        sess.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36",
                             "Accept": "application/json, text/plain, */*", "Referer": "https://www.idx.co.id/id/perusahaan-tercatat/profil-perusahaan/"})
        sess.get(IDXC_HOME, timeout=min(HTTP_TIMEOUT, 10))
        rr = sess.get(IDXC_PROFILES, params={"lang": "id", "start": 0, "length": 9999, "code": ""}, timeout=min(HTTP_TIMEOUT, 20))
        rr.raise_for_status()
        payload = rr.json()
        def find_rows(obj, depth=0):
            if depth > 6:
                return []
            if isinstance(obj, list):
                if obj and isinstance(obj[0], dict):
                    return obj
                return []
            if isinstance(obj, dict):
                for key in ("data", "Data", "rows", "Rows", "items", "Items", "result", "Result", "replies"):
                    if key in obj:
                        found = find_rows(obj[key], depth + 1)
                        if found:
                            return found
            return []
        rows = find_rows(payload)
        symbols = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = ""
            for key in ("KodeEmiten", "StockCode", "SecurityCode", "Code", "code"):
                if key in row and row[key]:
                    sym = str(row[key]).strip().upper(); break
            if re.fullmatch(r"[A-Z0-9]{4,5}", sym):
                symbols.append(sym)
        symbols = sorted(set(symbols))
        if len(symbols) >= 100:
            log.info("Universe resmi profil BEI berhasil: %s ticker", len(symbols))
            return symbols, "profil emiten BEI"
    except Exception as e:
        log.warning("Daftar resmi BEI tidak tersedia (%s).", type(e).__name__)
    fallback = list(dict.fromkeys(FALLBACK_UNIVERSE))
    log.warning("Memakai universe cadangan terbatas (%s ticker), bukan jaminan seluruh BEI.", len(fallback))
    return fallback, "fallback terbatas"


def setup_crypto_exchange(exchange_id):
    ex_class = getattr(ccxt, exchange_id, None)
    if ex_class is None:
        raise ValueError(f"Exchange CCXT tidak dikenal: {exchange_id}")
    ex = ex_class({"enableRateLimit": True, "timeout": HTTP_TIMEOUT * 1000, "options": {"defaultType": "spot"}})
    ex.load_markets()
    return ex


def fetch_crypto_ohlcv(ex, symbol, timeframe, limit=250):
    try:
        raw = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not raw:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
        x = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        x["time"] = pd.to_datetime(x["timestamp"], unit="ms", utc=True)
        return drop_unclosed(x[["time", "open", "high", "low", "close", "volume"]], timeframe, stock=False)
    except Exception as e:
        log.debug("Crypto OHLCV gagal %s %s (%s)", symbol, timeframe, type(e).__name__)
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])


def discover_crypto_universe(ex, exchange_id, max_symbols):
    markets = ex.markets
    tickers = {}
    try:
        tickers = ex.fetch_tickers()
    except Exception as e:
        log.warning("fetch_tickers gagal; universe market mungkin terbatas: %s", type(e).__name__)
    quote = "IDR" if exchange_id == "indodax" else "USDT"
    rows = []
    for sym, market in markets.items():
        if not market.get("active", True):
            continue
        if market.get("spot") is not True and market.get("type") != "spot":
            continue
        if market.get("quote") != quote or market.get("base") in STABLECOINS:
            continue
        base = str(market.get("base", "")).upper()
        if not base or base.endswith(LEVERAGED_MARKERS):
            continue
        t = tickers.get(sym, {}) if isinstance(tickers, dict) else {}
        last = finite(t.get("last"))
        qv = finite(t.get("quoteVolume"))
        if not np.isfinite(qv) and np.isfinite(last) and np.isfinite(finite(t.get("baseVolume"))):
            qv = last * finite(t.get("baseVolume"))
        rows.append({"symbol": sym, "base": base, "quote_volume": qv, "last": last})
    rows.sort(key=lambda r: finite(r.get("quote_volume"), 0), reverse=True)
    # Universal channel scan only: no user portfolio symbols are forced into the universe.
    selected = rows[:max_symbols] if max_symbols > 0 else list(rows)
    return selected, quote, len(rows), bool(tickers), rows


def ema(s, n):
    return s.ewm(span=n, adjust=False, min_periods=max(2, n // 2)).mean()


def atr_series(df, n=14):
    pc = df["close"].shift(1)
    tr = pd.concat([(df["high"] - df["low"]), (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False, min_periods=n).mean()


def rsi_series(close, n=14):
    diff = close.diff()
    gain = diff.clip(lower=0).ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    loss = -diff.clip(upper=0).ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def add_indicators(df):
    x = clean_ohlcv(df)
    if x.empty:
        return x
    x["atr"] = atr_series(x)
    x["rsi"] = rsi_series(x["close"])
    x["ema12"] = ema(x["close"], 12); x["ema26"] = ema(x["close"], 26)
    x["macd"] = x["ema12"] - x["ema26"]
    x["macd_signal"] = ema(x["macd"], 9)
    x["macd_hist"] = x["macd"] - x["macd_signal"]
    rng = (x["high"] - x["low"]).replace(0, np.nan)
    mult = (((x["close"] - x["low"]) - (x["high"] - x["close"])) / rng).replace([np.inf, -np.inf], np.nan).fillna(0)
    mfv = mult * x["volume"]
    x["cmf20"] = mfv.rolling(20, min_periods=10).sum() / x["volume"].rolling(20, min_periods=10).sum().replace(0, np.nan)
    x["obv"] = (np.sign(x["close"].diff()).fillna(0) * x["volume"].fillna(0)).cumsum()
    x["vol_med20"] = x["volume"].rolling(20, min_periods=10).median()
    x["rel_volume"] = x["volume"] / x["vol_med20"].replace(0, np.nan)
    return x


def confirmed_pivots(series, left=PIVOT_LEFT, right=PIVOT_RIGHT, kind="high"):
    arr = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)
    ids = []
    for i in range(left, len(arr) - right):
        window = arr[i-left:i+right+1]
        if not np.isfinite(arr[i]) or np.any(~np.isfinite(window)):
            continue
        center = arr[i]
        # Strict-right comparison reduces duplicate pivots on flat plateaus.
        if kind == "high":
            cond = center >= np.max(window[:left+1]) and center > np.max(window[left+1:])
        else:
            cond = center <= np.min(window[:left+1]) and center < np.min(window[left+1:])
        if cond:
            ids.append(i)
    return ids


def independent_touch_count(indices, min_gap):
    kept = []
    for i in sorted(indices):
        if not kept or i - kept[-1] >= min_gap:
            kept.append(i)
    return len(kept), kept


def line_fit(indices, values):
    if len(indices) < 2:
        return None
    try:
        slope, intercept = np.polyfit(np.asarray(indices, dtype=float), np.asarray(values, dtype=float), 1)
        if not np.isfinite(slope) or not np.isfinite(intercept):
            return None
        return float(slope), float(intercept)
    except Exception:
        return None


def build_channel_candidate(df, fit_side, high_pivots, low_pivots):
    n = len(df)
    high_vals = df["high"].to_numpy(dtype=float)
    low_vals = df["low"].to_numpy(dtype=float)
    close_vals = df["close"].to_numpy(dtype=float)
    atrs = df["atr"].to_numpy(dtype=float)
    x_all = np.arange(n, dtype=float)
    if fit_side == "upper":
        fit = line_fit(high_pivots, high_vals[high_pivots])
        if fit is None: return None
        slope, intercept = fit
        upper = slope*x_all + intercept
        distances = [upper[i] - low_vals[i] for i in low_pivots if upper[i] > low_vals[i]]
    else:
        fit = line_fit(low_pivots, low_vals[low_pivots])
        if fit is None: return None
        slope, intercept = fit
        lower_base = slope*x_all + intercept
        distances = [high_vals[i] - lower_base[i] for i in high_pivots if high_vals[i] > lower_base[i]]
    if len(distances) < 2:
        return None
    width = float(np.nanmedian(distances))
    if not np.isfinite(width) or width <= 0:
        return None
    if fit_side == "upper":
        upper_line = upper
        lower_line = upper_line - width
    else:
        lower_line = lower_base
        upper_line = lower_line + width
    atr_safe = np.where(np.isfinite(atrs) & (atrs > 0), atrs, np.nanmedian(atrs[np.isfinite(atrs) & (atrs > 0)]) if np.any(np.isfinite(atrs) & (atrs > 0)) else np.nan)
    if not np.isfinite(atr_safe[-1]):
        return None
    top_touch_idx = [i for i in high_pivots if abs(high_vals[i] - upper_line[i]) <= 0.70 * atr_safe[i]]
    bot_touch_idx = [i for i in low_pivots if abs(low_vals[i] - lower_line[i]) <= 0.70 * atr_safe[i]]
    top_count, top_kept = independent_touch_count(top_touch_idx, max(4, n // 50))
    bot_count, bot_kept = independent_touch_count(bot_touch_idx, max(4, n // 50))
    inside = (close_vals >= lower_line - 0.35*atr_safe) & (close_vals <= upper_line + 0.35*atr_safe)
    inside_ratio = float(np.nanmean(inside))
    residuals = []
    for i in high_pivots:
        residuals.append(abs(high_vals[i]-upper_line[i]) / atr_safe[i])
    for i in low_pivots:
        residuals.append(abs(low_vals[i]-lower_line[i]) / atr_safe[i])
    residual = float(np.nanmean(residuals)) if residuals else 99.0
    atr_med = float(np.nanmedian(atr_safe[-min(60, n):]))
    width_atr = width / atr_med if atr_med > 0 else np.nan
    mean_price = float(np.nanmedian(close_vals[-min(60, n):]))
    total_slope_pct = (slope * n / mean_price) if mean_price > 0 else 999
    # Touch score intentionally prefers a young/usable channel over an over-tested one.
    # Research hypothesis: 2 confirmed contacts per side establish the channel; a live 3rd
    # lower-boundary test is the preferred bounce setup. 4+ is not auto-invalid, but is late.
    def touch_score(count):
        if count < 2:
            return count / 2.0
        if count == 2:
            return 0.82
        return max(0.35, 1.0 - 0.16 * (count - 3))
    touch_component = 0.45*touch_score(top_count) + 0.55*touch_score(bot_count)
    fit_component = max(0.0, 1.0 - min(residual, 2.5)/2.5)
    width_component = 1.0 if 2.0 <= width_atr <= 12.0 else max(0.0, 1.0 - min(abs(width_atr-5.0)/12.0, 1.0))
    slope_component = max(0.0, 1.0 - max(0.0, abs(total_slope_pct)-0.25)/0.60)
    quality = 35*inside_ratio + 25*touch_component + 20*fit_component + 10*width_component + 10*slope_component
    if total_slope_pct > 0.02: direction = "NAIK"
    elif total_slope_pct < -0.02: direction = "TURUN"
    else: direction = "DATAR"
    # Upward-only. Two independent touches on both upper/lower edges establish the channel.
    valid = (direction == "NAIK" and top_count >= 2 and bot_count >= 2 and top_count + bot_count >= 4 and
             inside_ratio >= 0.70 and np.isfinite(width_atr) and 1.5 <= width_atr <= 18.0 and
             0.02 < total_slope_pct <= 0.70 and quality >= MIN_CHANNEL_QUALITY)
    last_i = n - 1
    return {"upper": float(upper_line[-1]), "lower": float(lower_line[-1]),
            "mid": float((upper_line[-1]+lower_line[-1])/2), "width": float(width),
            "slope": float(slope), "direction": direction, "quality": float(quality),
            "touch_top": int(top_count), "touch_bottom": int(bot_count),
            "touches": int(top_count+bot_count), "inside_pct": 100*inside_ratio,
            "fit_error_atr": residual, "width_atr": width_atr,
            "touch_stage": ("EARLY: MENUNGGU TES BAWAH KE-3" if bot_count == 2 and top_count <= 3 else
                            "TES BAWAH KE-3 SUDAH TERKONFIRMASI" if bot_count == 3 and top_count <= 3 else
                            "MATANG: 4+ SENTUHAN" if max(top_count, bot_count) >= 4 else "KANAL TERBENTUK"),
            "valid": bool(valid), "upper_series": upper_line, "lower_series": lower_line,
            "inside_mask": inside}


def detect_parallel_channel(raw, lookback=LOOKBACK):
    df = add_indicators(raw)
    if len(df) < max(65, PIVOT_LEFT + PIVOT_RIGHT + 40):
        return None, df
    df = df.tail(lookback).reset_index(drop=True)
    highs = confirmed_pivots(df["high"], kind="high")
    lows = confirmed_pivots(df["low"], kind="low")
    if len(highs) < 2 or len(lows) < 2:
        return None, df
    candidates = [build_channel_candidate(df, side, highs, lows) for side in ("upper", "lower")]
    # Hard filter: descending and flat channels never qualify for this scanner.
    candidates = [c for c in candidates if c is not None and c.get("direction") == "NAIK" and c.get("slope", 0) > 0 and c.get("valid")]
    if not candidates:
        return None, df
    def candidate_priority(c):
        # Prefer a young, well-shaped channel where the 3rd lower test is still ahead.
        early_bonus = 3.0 if c.get("touch_top", 0) in (2,3) and c.get("touch_bottom", 0) == 2 else 0.0
        overtested_penalty = 2.5 if max(c.get("touch_top",0), c.get("touch_bottom",0)) >= 4 else 0.0
        return c["quality"] + early_bonus - overtested_penalty
    best = max(candidates, key=candidate_priority)
    best["valid"] = bool(best["valid"])
    best["pos_pct"] = 100*(float(df.close.iloc[-1])-best["lower"])/best["width"]
    best["atr"] = finite(df.atr.iloc[-1])
    best["pivot_highs"] = len(highs); best["pivot_lows"] = len(lows)
    return best, df


def return_pct(df, bars=20):
    if df is None or len(df) <= bars:
        return np.nan
    old = finite(df.close.iloc[-bars-1]); new = finite(df.close.iloc[-1])
    return (new/old-1)*100 if old > 0 and np.isfinite(old) and np.isfinite(new) else np.nan


def context_trend(df):
    if df is None or len(df) < 25:
        return {"label": "N/A", "score": 8, "close": np.nan, "sma20": np.nan, "sma50": np.nan}
    c = pd.to_numeric(df.close, errors="coerce").dropna()
    s20 = c.rolling(20, min_periods=15).mean().iloc[-1]
    s50 = c.rolling(50, min_periods=30).mean().iloc[-1] if len(c) >= 30 else np.nan
    p = finite(c.iloc[-1])
    if np.isfinite(s20) and np.isfinite(s50) and p > s20 > s50:
        return {"label": "BULLISH", "score": 20, "close": p, "sma20": s20, "sma50": s50}
    if np.isfinite(s20) and np.isfinite(s50) and p < s20 < s50:
        return {"label": "BEARISH", "score": 3, "close": p, "sma20": s20, "sma50": s50}
    if np.isfinite(s20) and p > s20:
        return {"label": "MULAI KUAT", "score": 14, "close": p, "sma20": s20, "sma50": s50}
    if np.isfinite(s20) and p < s20:
        return {"label": "MULAI LEMAH", "score": 6, "close": p, "sma20": s20, "sma50": s50}
    return {"label": "NETRAL", "score": 8, "close": p, "sma20": s20, "sma50": s50}


def detect_setup(df, channel):
    """Classify only from completed candles. Labels are alerts, not orders."""
    last = df.iloc[-1]; prev = df.iloc[-2]
    upper = channel["upper_series"]; lower = channel["lower_series"]
    atr = max(finite(last.atr, 0), finite(channel["atr"], 0), finite(last.close)*0.002)
    buf = BREAKOUT_ATR_BUFFER * atr
    pos = channel["pos_pct"]
    close = finite(last.close); prev_close = finite(prev.close)
    upper_now = float(upper[-1]); lower_now = float(lower[-1])
    upper_prev = float(upper[-2]); lower_prev = float(lower[-2])
    vol_ratio = finite(last.rel_volume)
    if not np.isfinite(vol_ratio): vol_ratio = 0.0
    # A prior breakout that closes back into the channel is a failed breakout risk.
    prev3 = df.tail(4).iloc[:-1]
    had_up_break = any(finite(row.close) > float(upper[i]) + BREAKOUT_ATR_BUFFER*max(finite(row.atr, atr), 0)
                       for i, (_, row) in enumerate(df.tail(4).iloc[:-1].iterrows(), start=len(df)-4)) if len(df) >= 4 else False
    if close < lower_now - buf and (prev_close < lower_prev - 0.25*atr or vol_ratio >= BREAKOUT_VOLUME_RATIO):
        return "CHANNEL BROKEN — AVOID NEW LONG", "Penutupan di bawah batas bawah dengan konfirmasi; tunggu struktur baru/reclaim.", "breakdown"
    if close < lower_now and finite(last.low) < lower_now - buf:
        return "FALSE BREAKDOWN RISK", "Wick menembus bawah tetapi penutupan belum mengonfirmasi breakdown.", "false_break"
    if had_up_break and close <= upper_now:
        return "FAILED BREAKOUT / FALSE BREAK RISK", "Harga kembali ke dalam kanal setelah penembusan; setup breakout dibatalkan.", "failed_breakout"
    if close > upper_now + buf:
        prev_above = prev_close > upper_prev + 0.25*max(finite(prev.atr, atr), 0)
        if prev_above and vol_ratio >= BREAKOUT_VOLUME_RATIO:
            return "BREAKOUT CONFIRMED", "Dua penutupan bertahan di atas kanal dan volume relatif mendukung; tetap cek RR/resistance.", "breakout"
        return "BREAKOUT WATCH", "Harga menutup di atas batas atas; tunggu candle berikutnya bertahan atau retest.", "breakout_watch"
    last_low = finite(last.low, close)
    low_pos = 100*(last_low-lower_now)/max(channel["width"], 1e-12)
    lower_touch_now = (low_pos <= 25 and low_pos >= -15 and last_low >= lower_now - 0.60*atr)
    top_count = int(channel.get("touch_top", 0))
    bot_count = int(channel.get("touch_bottom", 0))
    bullish_trigger = close > finite(prev.high) and close > finite(last.open)
    near_lower_zone = pos <= 25 or lower_touch_now
    if near_lower_zone:
        # If 3 lower contacts were already confirmed, another lower retest is the 4th+:
        # do not market it as a fresh, early bounce entry.
        if bot_count >= 3 or top_count >= 4:
            channel["touch_stage"] = "MATANG: TES BAWAH KE-4+ / PRIORITAS RENDAH"
            return "MATURE CHANNEL — NO FRESH BOUNCE", "Kanal naik sudah diuji berulang; pantulan baru tidak diprioritaskan. Tunggu breakout valid atau struktur baru.", "mature"
        # Two historical lower contacts + today's near-lower-zone test = potential 3rd touch.
        if bot_count == 2 and 2 <= top_count <= 3 and lower_touch_now:
            channel["touch_stage"] = "POTENSI TES BAWAH KE-3"
            if bullish_trigger and vol_ratio >= BOUNCE_VOLUME_RATIO:
                return "LOWER-CHANNEL BOUNCE CONFIRMED — TOUCH #3", "Tes bawah ke-3 bertahan; candle bullish menembus high sebelumnya dan volume mendukung. Hipotesis perlu diuji historis.", "bounce"
            return "LOWER-CHANNEL BOUNCE WATCH — TOUCH #3", "Potensi tes bawah ke-3 pada kanal naik; tunggu pemicu bullish dan volume, jangan entry hanya karena sentuh garis.", "bounce_watch"
        channel["touch_stage"] = "KANAL TERBENTUK — MENUNGGU TES BAWAH KE-3"
        return "CHANNEL FORMED — WAIT LOWER TOUCH #3", "Kanal naik terbentuk, tetapi belum terdeteksi tes bawah ke-3 yang dapat diperdagangkan.", "wait"
    if pos >= 75 and close < finite(prev.close) and close < upper_now:
        return "UPPER-CHANNEL REJECTION", "Harga ditolak dari zona atas; peringatan pullback, bukan sinyal beli.", "rejection"
    return "VALID CHANNEL — WAIT", "Kanal lolos filter, tetapi belum ada pemicu entry terkonfirmasi.", "wait"


def levels_for_setup(df, channel, setup_type):
    last = df.iloc[-1]
    close = finite(last.close); atr = max(finite(last.atr, close*0.02), close*0.001)
    upper = channel["upper"]; lower = channel["lower"]; mid = channel["mid"]; width = channel["width"]
    entry = stop = tp1 = tp2 = rr = np.nan
    if setup_type in ("bounce", "bounce_watch"):
        entry = max(close, finite(last.high) + 0.05*atr)
        recent_low = finite(df.low.tail(5).min(), lower)
        stop = min(recent_low, lower - 0.25*atr)
        tp1 = mid; tp2 = upper
    elif setup_type in ("breakout", "breakout_watch"):
        entry = max(close, upper + BREAKOUT_ATR_BUFFER*atr)
        stop = upper - 0.50*atr
        tp1 = entry + 0.50*width; tp2 = entry + width
    else:
        # No entry/SL/TP is invented for WAIT, rejection, failed-breakout or breakdown states.
        return {"entry": np.nan, "stop": np.nan, "tp1": np.nan, "tp2": np.nan, "rr": np.nan}
    if np.isfinite(entry) and np.isfinite(stop) and entry > stop and np.isfinite(tp1):
        rr = (tp1-entry)/(entry-stop)
    return {"entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2, "rr": rr}


def score_channel(df, channel, setup_type, context, rel_strength, rr):
    quality_points = min(25, channel["quality"]*0.25)
    stage_text = str(channel.get("touch_stage", ""))
    if "TES BAWAH KE-3" in stage_text and setup_type in ("bounce", "bounce_watch"):
        quality_points = min(25, quality_points + 4)
    elif "MATANG" in stage_text:
        quality_points = max(0, quality_points - 4)
    trend_points = finite(context.get("score"), 8)
    last = df.iloc[-1]
    vol_ratio = finite(last.rel_volume, 0)
    cmf = finite(last.cmf20, 0)
    obv_rising = len(df) >= 21 and finite(df.obv.iloc[-1]) > finite(df.obv.iloc[-21])
    if setup_type in ("bounce", "breakout"):
        trigger_points = 15
    elif setup_type in ("bounce_watch", "breakout_watch"):
        trigger_points = 8
    elif setup_type in ("rejection", "failed_breakout", "breakdown", "false_break"):
        trigger_points = 0
    else:
        trigger_points = 3
    flow_points = 4
    if vol_ratio >= 1.5: flow_points += 5
    elif vol_ratio >= 1.2: flow_points += 3
    elif vol_ratio >= 0.8: flow_points += 1
    if cmf > 0.05: flow_points += 3
    elif cmf < -0.05: flow_points -= 1
    if obv_rising: flow_points += 3
    flow_points = max(0, min(15, flow_points))
    rs = finite(rel_strength)
    if not np.isfinite(rs): rs_points = 5
    elif rs >= 10: rs_points = 10
    elif rs >= 5: rs_points = 8
    elif rs >= 0: rs_points = 6
    elif rs >= -5: rs_points = 3
    else: rs_points = 0
    rr_value = finite(rr)
    if np.isfinite(rr_value) and rr_value >= 2.5: rr_points = 15
    elif np.isfinite(rr_value) and rr_value >= 2.0: rr_points = 12
    elif np.isfinite(rr_value) and rr_value >= 1.5: rr_points = 9
    elif np.isfinite(rr_value) and rr_value >= 1.0: rr_points = 4
    else: rr_points = 0
    return quality_points + trend_points + trigger_points + flow_points + rs_points + rr_points, {
        "channel_points": quality_points, "trend_points": trend_points,
        "trigger_points": trigger_points, "flow_points": flow_points, "rs_points": rs_points, "rr_points": rr_points,
        "obv_rising": obv_rising, "cmf20": cmf, "rsi": finite(last.rsi),
        "macd_hist": finite(last.macd_hist), "rel_volume": vol_ratio,
    }


def analyze_channel(symbol, setup_raw, context_df, liquidity, liquidity_ok, rel_strength, quote, source, name=""):
    result = {"symbol": symbol, "name": name, "market": APP_KIND, "source": source,
              "timeframe": "", "timestamp": "N/A",
              "price": np.nan, "status": "DATA GAP", "setup_type": "data_gap", "score": 0,
              "channel_quality": np.nan, "channel_direction": "N/A", "position_pct": np.nan,
              "touch_top": 0, "touch_bottom": 0, "inside_pct": np.nan, "touch_stage": "N/A",
              "volume_ratio": np.nan, "rsi": np.nan, "macd_hist": np.nan, "cmf20": np.nan,
              "higher_tf_trend": "N/A", "relative_strength": rel_strength,
              "liquidity": liquidity, "liquidity_ok": liquidity_ok,
              "upper": np.nan, "mid": np.nan, "lower": np.nan,
              "entry_trigger": np.nan, "stop_loss": np.nan, "tp1": np.nan, "tp2": np.nan,
              "rr": np.nan, "reason": "", "data_status": "OK"}
    if setup_raw is None or setup_raw.empty:
        result.update({"status": "DATA GAP / NO OHLCV", "reason": "Riwayat candle setup tidak tersedia.", "data_status": "N/A"})
        return result
    channel, df = detect_parallel_channel(setup_raw)
    result["timeframe"] = SETUP_TF["day"] if APP_KIND == "STOCK" else SETUP_TF["day"]
    result["timestamp"] = pd.Timestamp(df.time.iloc[-1]).strftime("%Y-%m-%d %H:%M UTC") if not df.empty else "N/A"
    if not df.empty:
        last = df.iloc[-1]
        result["price"] = finite(last.close)
        result["volume_ratio"] = finite(last.rel_volume)
        result["rsi"] = finite(last.rsi)
        result["macd_hist"] = finite(last.macd_hist)
        result["cmf20"] = finite(last.cmf20)
    context = context_trend(context_df)
    result["higher_tf_trend"] = context["label"]
    if channel is None:
        result.update({"status": "NO VALID CHANNEL", "reason": "Pivot, sentuhan, data atau struktur kanal belum memenuhi syarat.", "data_status": "PARTIAL"})
        if not liquidity_ok:
            result["status"] = "LOW LIQUIDITY / DATA GAP"
        return result
    result.update({"channel_quality": channel["quality"], "channel_direction": channel["direction"],
                   "position_pct": channel["pos_pct"], "touch_top": channel["touch_top"],
                   "touch_bottom": channel["touch_bottom"], "inside_pct": channel["inside_pct"],
                   "touch_stage": channel.get("touch_stage", "KANAL TERBENTUK"),
                   "upper": channel["upper"], "mid": channel["mid"], "lower": channel["lower"]})
    if not channel["valid"] or channel["quality"] < MIN_CHANNEL_QUALITY:
        result.update({"status": "NO VALID CHANNEL", "reason": "Kualitas/sentuhan kanal di bawah ambang.", "data_status": "PARTIAL"})
        if not liquidity_ok:
            result["status"] = "LOW LIQUIDITY / DATA GAP"
        return result
    status, reason, setup_type = detect_setup(df, channel)
    # detect_setup refines the stage using the current completed candle (e.g., today's potential 3rd touch).
    result["touch_stage"] = channel.get("touch_stage", result.get("touch_stage", "N/A"))
    levels = levels_for_setup(df, channel, setup_type)
    score, components = score_channel(df, channel, setup_type, context, rel_strength, levels["rr"])
    # Risk gates downgrade signals; a high score never bypasses them.
    rr = finite(levels["rr"])
    if status in ("LOWER-CHANNEL BOUNCE CONFIRMED — TOUCH #3", "BREAKOUT CONFIRMED", "LOWER-CHANNEL BOUNCE WATCH — TOUCH #3", "BREAKOUT WATCH") and (not np.isfinite(rr) or rr < MIN_RR):
        status = "WATCH — RR TIDAK CUKUP"
        reason += " RR ke target pertama belum memenuhi batas minimum."
    if np.isfinite(components["rsi"]) and components["rsi"] >= 78 and status in ("BREAKOUT CONFIRMED", "BREAKOUT WATCH"):
        status = "DO NOT CHASE — RSI PANAS"
        reason += " RSI tinggi; hindari mengejar harga."
    current = finite(df.close.iloc[-1])
    atr_now = max(finite(df.atr.iloc[-1], current*0.002), current*0.001)
    if setup_type in ("breakout", "breakout_watch") and current-channel["upper"] > 1.5*atr_now:
        status = "DO NOT CHASE — TERLALU JAUH"
        reason += " Harga sudah terlalu jauh dari batas atas kanal."
    if not liquidity_ok:
        status = "LOW LIQUIDITY / DATA GAP"
        reason = "Likuiditas/coverage tidak memenuhi hard gate; tidak masuk rekomendasi." 
    result.update({"status": status, "setup_type": setup_type, "score": round(float(max(0, min(100, score))), 1),
                   "volume_ratio": components["rel_volume"], "rsi": components["rsi"],
                   "macd_hist": components["macd_hist"], "cmf20": components["cmf20"],
                   "obv_rising": components["obv_rising"], "higher_tf_trend": context["label"],
                   "entry_trigger": levels["entry"], "stop_loss": levels["stop"], "tp1": levels["tp1"],
                   "tp2": levels["tp2"], "rr": rr, "reason": reason,
                   "data_status": "OK" if np.isfinite(current) and len(df) >= 80 else "PARTIAL"})
    return result


def telegram_send(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("Telegram disabled/missing credentials; report saved locally only.")
        return False
    # Plain text by design: avoids Markdown parse failures in Telegram.
    chunks, current = [], ""
    for line in str(text).splitlines() or [str(text)]:
        if len(line) > 3300:
            if current: chunks.append(current); current = ""
            for i in range(0, len(line), 3200): chunks.append(line[i:i+3200])
            continue
        proposed = line if not current else current + "\n" + line
        if len(proposed) > 3300:
            chunks.append(current); current = line
        else:
            current = proposed
    if current: chunks.append(current)
    all_ok = True
    for chunk in chunks:
        try:
            resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                 json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "disable_web_page_preview": True},
                                 timeout=20)
            data = resp.json() if resp.ok else {}
            if not resp.ok or not data.get("ok"):
                log.error("Telegram send failed: HTTP %s / %s", resp.status_code, data.get("description", "unknown"))
                all_ok = False
            time.sleep(0.8)
        except Exception as e:
            log.error("Telegram error: %s", type(e).__name__); all_ok = False
    return all_ok


def setup_priority(r):
    s = str(r.get("status", ""))
    priorities = {
        "BREAKOUT CONFIRMED": 9,
        "LOWER-CHANNEL BOUNCE CONFIRMED — TOUCH #3": 10,
        "LOWER-CHANNEL BOUNCE WATCH — TOUCH #3": 8,
        "BREAKOUT WATCH": 7,
        "CHANNEL FORMED — WAIT LOWER TOUCH #3": 5,
        "VALID CHANNEL — WAIT": 4,
        "WATCH — RR TIDAK CUKUP": 3,
        "MATURE CHANNEL — NO FRESH BOUNCE": 0,
        "UPPER-CHANNEL REJECTION": 0,
        "FAILED BREAKOUT / FALSE BREAK RISK": 0,
    }
    if r.get("channel_direction") != "NAIK":
        return -1
    stage = str(r.get("touch_stage", ""))
    if "MATANG" in stage and s not in ("BREAKOUT CONFIRMED", "BREAKOUT WATCH"):
        return 1
    return priorities.get(s, 0)


def export_results(results, mode, source_info, quote):
    df = pd.DataFrame(results)
    if not df.empty:
        df["ranking_priority"] = df.apply(lambda row: setup_priority(row.to_dict()), axis=1)
        df = df.sort_values(["ranking_priority", "score", "channel_quality"], ascending=False, na_position="last")
        df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
        lines = [f"{VERSION}", f"Mode: {mode.upper()} | Waktu scan: {datetime.now().astimezone().isoformat(timespec='seconds')}",
                 f"Universe: {source_info}", "Semua hasil yang tercatat, termasuk DATA GAP, tersimpan di CSV.", "",
                 "RANK | SYMBOL | STATUS | SCORE | QUALITY | TF | HARGA | RR | ALASAN"]
        for i, row in enumerate(df.to_dict("records"), 1):
            lines.append(f"{i}. {row.get('symbol')} | {row.get('status')} | {fmt_num(row.get('score'),1)} | "
                         f"{fmt_num(row.get('channel_quality'),1)} | {row.get('timeframe')} | "
                         f"{fmt_price(row.get('price'), quote)} | {fmt_num(row.get('rr'),2)} | {row.get('reason','')}")
        OUTPUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    else:
        OUTPUT_CSV.write_text("", encoding="utf-8-sig")
        OUTPUT_TXT.write_text(f"{VERSION}\nTidak ada hasil.\n", encoding="utf-8-sig")
    return df


def report_messages(results, mode, source_info, universe_count, scanned_count, benchmark_text, quote):
    """Telegram only lists ascending-channel signals; no personal holdings/portfolio comparison."""
    excluded_statuses = {
        "NO VALID CHANNEL", "LOW LIQUIDITY / DATA GAP", "DATA GAP / NO OHLCV", "DATA GAP / NO DAILY DATA",
        "NOT IN INTRADAY SCAN — BELOW LIQUIDITY/RANK LIMIT", "NOT SELECTED: BELOW UNIVERSE CAP",
        "NOT SELECTED: BELOW QUOTE VOLUME RANK", "MATURE CHANNEL — NO FRESH BOUNCE",
        "CHANNEL BROKEN — AVOID NEW LONG", "FAILED BREAKOUT / FALSE BREAK RISK",
        "FALSE BREAKDOWN RISK", "UPPER-CHANNEL REJECTION"
    }
    channels = [r for r in results
                if r.get("channel_direction") == "NAIK"
                and finite(r.get("channel_quality"), -1) >= MIN_CHANNEL_QUALITY
                and r.get("status") not in excluded_statuses
                and setup_priority(r) > 0]
    channels.sort(key=lambda r: (setup_priority(r), finite(r.get("score"), 0),
                                 finite(r.get("channel_quality"), 0)), reverse=True)
    top = channels[:TOP_N]
    # Watchlist is diagnostic only: valid rising channels near a setup, not buy signals.
    watch_excluded = excluded_statuses | {"LOWER-CHANNEL BOUNCE CONFIRMED — TOUCH #3", "BREAKOUT CONFIRMED"}
    watch_candidates = [r for r in results
                        if r.get("channel_direction") == "NAIK"
                        and finite(r.get("channel_quality"), -1) >= MIN_CHANNEL_QUALITY
                        and r.get("status") not in watch_excluded]
    watch_candidates.sort(key=lambda r: (finite(r.get("channel_quality"), 0),
                                         finite(r.get("score"), 0)), reverse=True)
    # Stage diagnostics show where the funnel is empty without pretending the candidates are actionable.
    status_counts = {}
    for row in results:
        st = str(row.get("status", "UNKNOWN"))
        status_counts[st] = status_counts.get(st, 0) + 1
    msg1 = [f"📈 {VERSION}", "MODE: " + mode.upper() + " | HANYA KANAL PARALEL NAIK",
            f"Setup TF: {SETUP_TF[mode]} | Konteks TF: {CONTEXT_TF[mode]}",
            f"Waktu scan: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}",
            f"Sumber: {source_info}", f"Universe {universe_count} | dianalisis {scanned_count} | sinyal terkonfirmasi/layak {len(channels)} | watch valid {len(watch_candidates)}",
            benchmark_text,
            "Fokus pantulan: kanal terbentuk (2 sentuhan tiap sisi), cari potensi TES BAWAH KE-3.",
            "Tes ke-4+ tidak diprioritaskan untuk pantulan baru; tidak otomatis berarti kanal pasti gagal.",
            "Candle belum tutup dikeluarkan. Skor bukan probabilitas profit dan tidak ada auto-trading.",
            f"Semua hasil lokal: {OUTPUT_CSV.name} dan {OUTPUT_TXT.name}"]
    msg2 = [f"🏆 TOP {min(TOP_N, len(top))} SINYAL KANAL NAIK — {APP_KIND}",
            "Peringkat mengutamakan tes bawah ke-3 terkonfirmasi, lalu breakout naik terkonfirmasi."]
    qtxt = "IDR" if quote == "IDR" else quote
    if top:
        for i, r in enumerate(top, 1):
            msg2.append(f"\n{i}) {r.get('symbol')} — {r.get('status')}")
            msg2.append(f"Harga {fmt_price(r.get('price'), qtxt)} | Skor {fmt_num(r.get('score'),1)}/100 | Kualitas {fmt_num(r.get('channel_quality'),1)}/100")
            msg2.append(f"Sentuhan atas/bawah: {r.get('touch_top',0)}/{r.get('touch_bottom',0)} | Tahap: {r.get('touch_stage','N/A')}")
            msg2.append(f"Posisi kanal {fmt_num(r.get('position_pct'),1)}% | Volume {fmt_num(r.get('volume_ratio'),2)}x | Tren TF besar {r.get('higher_tf_trend','N/A')}")
            msg2.append(f"RSI {fmt_num(r.get('rsi'),1)} | CMF20 {fmt_num(r.get('cmf20'),2)} | RR {fmt_num(r.get('rr'),2)}")
            msg2.append(f"Entry {fmt_price(r.get('entry_trigger'),qtxt)} | SL {fmt_price(r.get('stop_loss'),qtxt)} | TP1 {fmt_price(r.get('tp1'),qtxt)} | TP2 {fmt_price(r.get('tp2'),qtxt)}")
            msg2.append(f"Alasan/invalidasi: {r.get('reason','')}")
    else:
        msg2.append("BELUM ADA SINYAL TERKONFIRMASI. WATCHLIST DI BAWAH BUKAN SINYAL BELI:")
    if not top and watch_candidates:
        for i, r in enumerate(watch_candidates[:TOP_N], 1):
            msg2.append(f"\n👀 WATCH {i}) {r.get('symbol')} — {r.get('status','WATCH')}")
            msg2.append(f"Harga {fmt_price(r.get('price'), qtxt)} | Kualitas kanal {fmt_num(r.get('channel_quality'),1)}/100 | Skor {fmt_num(r.get('score'),1)}/100")
            msg2.append(f"Sentuhan atas/bawah {r.get('touch_top',0)}/{r.get('touch_bottom',0)} | Posisi {fmt_num(r.get('position_pct'),1)}% | Tahap {r.get('touch_stage','N/A')}")
            msg2.append(f"Volume {fmt_num(r.get('volume_ratio'),2)}x | RR {fmt_num(r.get('rr'),2)} | Catatan: {r.get('reason','Tunggu konfirmasi candle, volume, dan RR.')}")
    elif not top:
        msg2.append("Tidak ada kandidat watch yang memenuhi kualitas minimum. Ini bisa berarti filter tidak lolos atau data kurang; cek CSV/TXT untuk diagnosis.")
    msg2.append("\nDIAGNOSTIK FILTER (jumlah status):")
    for st, count in sorted(status_counts.items(), key=lambda kv: kv[1], reverse=True)[:8]:
        msg2.append(f"• {st}: {count}")
    msg2.append("\nAturan: kanal turun/datar tidak ditampilkan. Tes bawah ke-3 hanya watch/confirm jika close, pemicu bullish, volume dan RR mendukung. Bukan order otomatis.")
    return "\n".join(msg1), "\n".join(msg2)


def resample_context(df, rule):
    if df is None or df.empty:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    x = clean_ohlcv(df).set_index("time").sort_index()
    try:
        y = x.resample(rule).agg({"open":"first", "high":"max", "low":"min", "close":"last", "volume":"sum"}).dropna(subset=["close"]).reset_index()
        if rule.startswith("W") and len(y) > 1:
            # Drop the current partial week; no use of an unfinished HTF candle.
            now_local = pd.Timestamp.now(tz="Asia/Jakarta")
            monday = (now_local - pd.Timedelta(days=now_local.weekday())).date()
            y = y.loc[y["time"].dt.tz_convert("Asia/Jakarta").dt.date < monday]
        return clean_ohlcv(y)
    except Exception:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])


def run_crypto(args):
    exchange_id = args.exchange or CRYPTO_EXCHANGE
    ex = setup_crypto_exchange(exchange_id)
    max_symbols = max(0, args.max_symbols if args.max_symbols is not None else MAX_SYMBOLS)
    selected_rows, quote, total_pairs, tickers_available, all_market_rows = discover_crypto_universe(ex, exchange_id, max_symbols)
    setup_tf = SETUP_TF[args.mode]; context_tf = CONTEXT_TF[args.mode]
    log.info("Crypto exchange=%s quote=%s candidates=%s selected=%s mode=%s", exchange_id, quote, total_pairs, len(selected_rows), args.mode)
    # The same exchange and pair are used throughout; do not mix candles from different venues.
    benchmark_symbol = f"BTC/{quote}"
    if benchmark_symbol not in ex.markets:
        benchmark_df = pd.DataFrame()
    else:
        benchmark_df = fetch_crypto_ohlcv(ex, benchmark_symbol, setup_tf, 220)
    results = []
    min_qv = MIN_QUOTE_VOLUME[args.mode]
    all_markets = []
    for row in selected_rows:
        symbol = row["symbol"]
        qv = finite(row.get("quote_volume"))
        last = finite(row.get("last"))
        liquidity_ok = np.isfinite(qv) and qv >= min_qv
        if not liquidity_ok:
            results.append({"symbol": symbol, "name": "", "market": "CRYPTO", "source": exchange_id, "timeframe": setup_tf, "timestamp": "N/A", "price": last, "status": "LOW LIQUIDITY / DATA GAP", "setup_type": "low_liquidity", "score": 0, "channel_quality": np.nan, "channel_direction": "N/A", "position_pct": np.nan, "touch_top": 0, "touch_bottom": 0, "inside_pct": np.nan, "volume_ratio": np.nan, "rsi": np.nan, "macd_hist": np.nan, "cmf20": np.nan, "higher_tf_trend": "N/A", "relative_strength": np.nan, "liquidity": qv, "liquidity_ok": False, "upper": np.nan, "mid": np.nan, "lower": np.nan, "entry_trigger": np.nan, "stop_loss": np.nan, "tp1": np.nan, "tp2": np.nan, "rr": np.nan, "reason": f"Quote volume {fmt_num(qv,0)} di bawah batas {fmt_num(min_qv,0)} {quote} atau tidak tersedia.", "data_status": "PARTIAL"})
            continue
        setup_df = fetch_crypto_ohlcv(ex, symbol, setup_tf, 220 if args.mode == "swing" else 250)
        context_df = fetch_crypto_ohlcv(ex, symbol, context_tf, 250)
        ret = return_pct(setup_df, 20)
        bret = return_pct(benchmark_df, 20)
        rel = ret-bret if np.isfinite(ret) and np.isfinite(bret) else np.nan
        price = finite(setup_df.close.iloc[-1]) if not setup_df.empty else last
        result = analyze_channel(symbol, setup_df, context_df, qv, liquidity_ok, rel, quote, exchange_id)
        result["timeframe"] = setup_tf
        if not np.isfinite(finite(result.get("price"))) and np.isfinite(price): result["price"] = price
        result["relative_strength"] = rel
        result["benchmark_return"] = bret
        result["quote_volume_24h"] = qv
        if not liquidity_ok:
            result["status"] = "LOW LIQUIDITY / DATA GAP"
            result["reason"] = f"Quote volume 24 jam tidak memenuhi batas {fmt_num(min_qv,0)} {quote}."
        results.append(result)
        if len(results) % 20 == 0:
            log.info("Analisis crypto %s/%s", len(results), len(selected_rows))
    scanned_crypto_syms = {r.get("symbol") for r in results}
    for row in all_market_rows:
        symbol = row["symbol"]
        if symbol in scanned_crypto_syms:
            continue
        results.append({"symbol": symbol, "name": "", "market": "CRYPTO", "source": exchange_id, "timeframe": setup_tf, "timestamp": "N/A", "price": row.get("last"), "status": "NOT SELECTED: BELOW QUOTE VOLUME RANK", "setup_type": "not_scanned", "score": 0, "channel_quality": np.nan, "channel_direction": "N/A", "position_pct": np.nan, "touch_top": 0, "touch_bottom": 0, "inside_pct": np.nan, "volume_ratio": np.nan, "rsi": np.nan, "macd_hist": np.nan, "cmf20": np.nan, "higher_tf_trend": "N/A", "relative_strength": np.nan, "liquidity": row.get("quote_volume"), "liquidity_ok": False, "upper": np.nan, "mid": np.nan, "lower": np.nan, "entry_trigger": np.nan, "stop_loss": np.nan, "tp1": np.nan, "tp2": np.nan, "rr": np.nan, "reason": "Di luar batas jumlah pasangan yang dianalisis berdasarkan volume; bukan hasil setup.", "data_status": "PARTIAL"})
    benchmark_ret = return_pct(benchmark_df, 20)
    benchmark_price = finite(benchmark_df.close.iloc[-1]) if not benchmark_df.empty else np.nan
    benchmark_text = f"Benchmark {benchmark_symbol}: {fmt_price(benchmark_price, quote)} | return {fmt_pct(benchmark_ret)} | venue tunggal={exchange_id}"
    export_results(results, args.mode, f"{exchange_id} {quote} markets; ticker coverage={'OK' if tickers_available else 'PARTIAL'}", quote)
    msg1, msg2 = report_messages(results, args.mode, f"{exchange_id} {quote}; ticker coverage={'OK' if tickers_available else 'PARTIAL'}", total_pairs, len(selected_rows), benchmark_text, quote)
    # Report is sent to Telegram only; full results remain saved in CSV/TXT.
    if not args.no_telegram:
        telegram_send(msg1); telegram_send(msg2)
    return 0


def main():
    parser = argparse.ArgumentParser(description=f"{VERSION}: deteksi parallel channel, bounce, breakout, risiko dan Top 5")
    parser.add_argument("--mode", choices=["day", "swing"], default="swing", help="day atau swing")
    parser.add_argument("--max-symbols", type=int, default=None, help="batas symbol; 0=semua yang ditemukan (default dari env/konfigurasi)")
    parser.add_argument("--no-telegram", action="store_true", help="simpan laporan lokal tanpa kirim Telegram")
    parser.add_argument("--test-telegram", action="store_true", help="kirim pesan tes singkat ke Telegram")
    if APP_KIND == "CRYPTO":
        parser.add_argument("--exchange", default=None, help="CCXT exchange id; default dari ZENA_CRYPTO_EXCHANGE (indodax)")
    args = parser.parse_args()
    if args.test_telegram:
        ok = telegram_send(f"✅ {VERSION} — test Telegram berhasil. Waktu {datetime.now().astimezone().isoformat(timespec='seconds')}")
        if not ok: raise SystemExit(2)
        return 0
    try:
        return run_stock(args) if APP_KIND == "STOCK" else run_crypto(args)
    except KeyboardInterrupt:
        log.warning("Dihentikan pengguna.")
        return 130
    except Exception as e:
        log.exception("Screening gagal: %s", type(e).__name__)
        if not args.no_telegram:
            telegram_send(f"⚠️ {VERSION}: screening gagal ({type(e).__name__}). Periksa file log {LOG_FILE.name}.")
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
