#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZENA SWING MOMENTUM SCANNER

Khusus swing trading beberapa hari sampai beberapa bulan.
Tidak melakukan order otomatis.
Data market: Indodax via CCXT.
Alert: Telegram Bot API.

Install (script juga mencoba install otomatis):
    pip install ccxt pandas numpy requests

Jalankan sekali:
    python zena_swing_momentum_scanner.py --once

Continuous:
    python zena_swing_momentum_scanner.py

Telegram:
    Menggunakan konfigurasi Telegram yang sudah ada pada versi ZENA sebelumnya.

PENTING:
Token Telegram memakai konfigurasi versi sebelumnya sesuai permintaan pengguna.
"""

import os
import sys
import time
import json
import math
import argparse
import subprocess
import importlib
from pathlib import Path
from datetime import datetime, timezone

REQUIRED = {"ccxt":"ccxt", "pandas":"pandas", "numpy":"numpy", "requests":"requests"}
for mod, pkg in REQUIRED.items():
    try:
        importlib.import_module(mod)
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", pkg])

import ccxt
import pandas as pd
import numpy as np
import requests

# ============================================================
# CONFIG
# ============================================================
EXCHANGE_ID = "indodax"
TELEGRAM_BOT_TOKEN = "8979917342:AAETt_7wWXO_TzzYPVTLftW5-0W8MBlto2I".strip()
TELEGRAM_CHAT_ID = os.getenv("ZENA_TELEGRAM_CHAT_ID", "8049352232").strip()

MAX_MARKETS = 1000
MIN_24H_QUOTE_VOLUME_IDR = 50_000_000
MIN_REPORT_SCORE = 55
MIN_ALERT_SCORE = 70
TOP_N = 5
SCAN_INTERVAL_MINUTES = 30
ALERT_COOLDOWN_HOURS = 6

TF_1D, TF_3D, TF_1W = "1d", "3d", "1w"
LIMIT_1D, LIMIT_3D, LIMIT_1W = 260, 160, 120
STATE_FILE = Path("zena_early_swing_state.json")
LOG_FILE = Path("zena_early_swing_scanner.log")

MIN_RR = 1.8
PREFERRED_RR = 2.0
MAX_STRUCTURAL_STOP_PCT = 0.20
SL_ATR_BUFFER = 0.50
FOMO_RSI = 68
EXTREME_RSI = 75
EARLY_RSI_MAX = 64
EARLY_DIST_MAX = 0.10


def log(msg):
    s = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {msg}"
    print(s, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(s + "\n")
    except Exception:
        pass


def sf(x, default=np.nan):
    try:
        return float(x)
    except Exception:
        return default


def clamp(x, lo=0, hi=100):
    return max(lo, min(hi, x))


def idr(x):
    return "N/A" if not np.isfinite(sf(x)) else "Rp{:,.0f}".format(float(x)).replace(",", ".")


def fx(x, n=1):
    return "N/A" if not np.isfinite(sf(x)) else f"{float(x):.{n}f}"


def pct(x):
    return "N/A" if not np.isfinite(sf(x)) else f"{float(x)*100:+.1f}%"


def html_escape(x):
    """Escape teks dinamis untuk Telegram HTML."""
    import html
    return html.escape(str(x), quote=False)


def coin_link(symbol):
    """Nama coin bold + biru (Telegram HTML link)."""
    base = symbol.replace("/IDR", "").upper()
    slug = symbol.replace("/", "").lower()
    return f'<b><a href="https://indodax.com/market/{slug}">{html_escape(base)}</a></b>'


def split_telegram_html(text, limit=3500):
    """Pecah pesan HTML Telegram di batas baris agar di bawah limit 4096 karakter.

    Pesan dibuat lebih pendek dari batas Telegram untuk memberi ruang aman.
    Tiap baris pesan scanner dirancang memiliki tag HTML yang seimbang.
    """
    text = str(text or "")
    if len(text) <= limit:
        return [text]
    chunks = []
    current = ""
    for line in text.splitlines():
        candidate = line if not current else current + "\n" + line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        # Baris tunggal yang sangat panjang: potong dengan aman sebagai fallback.
        while len(line) > limit:
            cut = line.rfind(" ", 0, limit)
            if cut < limit // 2:
                cut = limit
            chunks.append(line[:cut])
            line = line[cut:].lstrip()
        current = line
    if current:
        chunks.append(current)
    total = len(chunks)
    return [f"<b>Bagian {i}/{total}</b>\n{chunk}" if total > 1 else chunk
            for i, chunk in enumerate(chunks, 1)]


def req_telegram(text):
    """Kirim pesan Telegram HTML; otomatis pecah pesan yang terlalu panjang."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("[TELEGRAM] Token/chat ID belum tersedia pada konfigurasi")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = split_telegram_html(text, limit=3500)
    for i, chunk in enumerate(chunks, 1):
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            r = requests.post(url, json=payload, timeout=20)
            if r.ok:
                try:
                    data = r.json()
                    if data.get("ok") is True:
                        log(f"[TELEGRAM] sent OK ({i}/{len(chunks)})")
                        continue
                except Exception:
                    pass
                log("[TELEGRAM] HTTP OK tetapi respons Telegram tidak valid")
            else:
                log(f"[TELEGRAM] ERROR {r.status_code}: {r.text[:500]}")
        except requests.RequestException as e:
            log(f"[TELEGRAM] NETWORK ERROR: {e}")
        log(f"[TELEGRAM] Pengiriman gagal pada bagian {i}/{len(chunks)}")
        return False
    return True


def telegram_scan_summary(results, btc):
    """Ringkasan Telegram singkat dan fokus pada kandidat tahap awal."""
    lines = [
        "🌱 ZENA EARLY SWING / BEFORE PUMP",
        "━━━━━━━━━━━━━━━━━━━━",
        f"BTC Macro: {html_escape(btc['regime'])} (INFO SAJA)",
        f"BTC RSI 1D/1W: {fx(btc['rsi1d'])}/{fx(btc['rsi1w'])} | MACD: {html_escape(btc['macd_mom'])}",
        "Score EARLY = akumulasi + momentum awal + posisi harga. BTC tidak dihitung.",
    ]
    if not results:
        lines.extend(["", "Tidak ada coin yang berhasil dianalisis pada scan ini."])
        return "\n".join(lines)

    def v(x):
        return "N/A" if not np.isfinite(sf(x)) else f"{sf(x):+.2f}"

    lines.extend(["", "🏆 TOP 5 KANDIDAT AWAL"])
    for i, r in enumerate(results[:5], 1):
        cm = r.get("cmf_multi", {})
        rr = max([x for x in [r["rr1"], r["rr2"] if np.isfinite(r["rr2"]) else np.nan] if np.isfinite(x)] or [np.nan])
        lines.extend([
            "",
            f"<b>#{i} {coin_link(r['symbol'])} — {r['score']}/100 ({r['grade']})</b>",
            f"{html_escape(r['status'])} | Harga {idr(r['price'])} | SMA20 {pct(r.get('dist_sma20', np.nan))}",
            f"Skor: Smart {r['smart_score']}/35 | Momentum {r['momentum_score']}/30 | Struktur {r['structure_score']}/20 | Risk {r['risk_score']}/15",
            f"RSI 1D/3D/1W: {fx(r['rsi1d'])}/{fx(r['rsi3d'])}/{fx(r['rsi1w'])} | Vol {fx(r['vol_ratio'],2)}x",
            f"<b>Flow CMF: {html_escape(r.get('cmf_flow','FLOW BELUM JELAS'))}</b>",
            f"CMF 180D/90D: {v(cm.get(180))}/{v(cm.get(90))} | 60D/30D/20D: {v(cm.get(60))}/{v(cm.get(30))}/{v(cm.get(20))}",
            f"CMF 14D/10D/7D: {v(cm.get(14))}/{v(cm.get(10))}/{v(cm.get(7))} | 3D/1D: {v(cm.get(3))}/{v(cm.get(1))}",
            f"<b>Smart-money: {html_escape(r['smart_beh'])} | OBV {'mendukung' if r.get('obv_bull') else 'belum mendukung'} | BB Squeeze: {'YA' if r['bb_squeeze'] else 'TIDAK'}</b>",
            f"Entry {idr(r['entry'])} | BO {idr(r['breakout_entry'])} | SL {idr(r['sl'])}",
            f"TP1 {idr(r['tp1'])} | TP2 {idr(r['tp2'])} | RR {'1:'+format(rr,'.2f') if np.isfinite(rr) else 'N/A'}",
            f"<b>Alasan: {html_escape('; '.join(r['reasons'][:2]) if r['reasons'] else 'Belum ada konfirmasi dominan')}</b>",
            f"<b>Warning: {html_escape(r['warns'][0] if r['warns'] else 'Tidak ada warning utama')}</b>",
        ])
    # Multi-horizon recommendations are ranked separately from BEFORE-PUMP score.
    swing = sorted(results, key=lambda x: x.get("swing_score", 0), reverse=True)
    cycle = sorted([r for r in results if not r.get("cycle_excluded", False)],
                   key=lambda x: x.get("cycle_score", 0), reverse=True)
    lines.extend(["", "━━━━━━━━━━━━━━━━━━━━", "🗓️ <b>SWING TRADE — BEBERAPA HARI / MINGGU</b>"])
    if not swing:
        lines.append("Belum ada kandidat swing yang bisa dinilai.")
    else:
        for i, r in enumerate(swing[:3], 1):
            lines.append(
                f"<b>#{i} {coin_link(r['symbol'])} — {r.get('swing_score', 0)}/100</b> | "
                f"{html_escape(r.get('swing_label', 'PANTAU'))} | {html_escape(r.get('swing_reason', 'Konfirmasi belum lengkap'))}"
            )
    lines.extend(["", "🏦 <b>CALON INVESTMENT CYCLE — JANGKA PANJANG</b>"])
    if not cycle:
        lines.append("Belum ada kandidat cycle yang bisa dinilai.")
    else:
        for i, r in enumerate(cycle[:3], 1):
            lines.append(
                f"<b>#{i} {coin_link(r['symbol'])} — {r.get('cycle_score', 0)}/100</b> | "
                f"{html_escape(r.get('cycle_label', 'BELUM KUAT'))} | {html_escape(r.get('cycle_reason', 'Trend panjang belum dominan'))}"
            )
    lines.extend([
        "", "━━━━━━━━━━━━━━━━━━━━",
        "Skor swing dan cycle adalah penilaian terpisah, bukan target harga atau jaminan profit.",
        "Prioritas EARLY BUY ZONE > BEFORE BREAKOUT > WATCH EARLY. BTC hanya informasi; tidak menambah skor koin.",
        "BEFORE PUMP bukan jaminan harga akan pump. Screening saja, bukan order otomatis.",
    ])
    return "\n".join(lines)


def ema(s, n):
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def sma(s, n):
    return s.rolling(n, min_periods=n).mean()


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0)
    dn = -d.clip(upper=0)
    ag = up.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    al = dn.ewm(alpha=1/n, adjust=False, min_periods=n).mean().replace(0, np.nan)
    return (100 - 100/(1 + ag/al)).replace([np.inf, -np.inf], np.nan)


def macd(s):
    m = ema(s, 12) - ema(s, 26)
    sig = ema(m, 9)
    return m, sig, m-sig


def tsi(s, long=25, short=13, signal=7):
    d = s.diff()
    m = d.ewm(span=short, adjust=False, min_periods=short).mean().ewm(span=long, adjust=False, min_periods=long).mean()
    a = d.abs().ewm(span=short, adjust=False, min_periods=short).mean().ewm(span=long, adjust=False, min_periods=long).mean()
    t = 100*m/a.replace(0, np.nan)
    return t, ema(t, signal)


def stoch_rsi(s, n=14, k=3, d=3):
    r = rsi(s, n)
    lo = r.rolling(n, min_periods=n).min()
    hi = r.rolling(n, min_periods=n).max()
    st = 100*(r-lo)/(hi-lo).replace(0, np.nan)
    K = st.rolling(k, min_periods=k).mean()
    D = K.rolling(d, min_periods=d).mean()
    return st, K, D


def adx(df, n=14):
    h, l, c = df.high, df.low, df.close
    up = h.diff(); dn = -l.diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    pc = c.shift(1)
    tr = pd.concat([h-l, (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
    atrv = tr.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    pdi = 100*plus.ewm(alpha=1/n, adjust=False, min_periods=n).mean()/atrv
    mdi = 100*minus.ewm(alpha=1/n, adjust=False, min_periods=n).mean()/atrv
    dx = 100*(pdi-mdi).abs()/(pdi+mdi).replace(0, np.nan)
    return dx.ewm(alpha=1/n, adjust=False, min_periods=n).mean()


def atr(df, n=14):
    pc = df.close.shift(1)
    tr = pd.concat([df.high-df.low, (df.high-pc).abs(), (df.low-pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False, min_periods=n).mean()


def cmf(df, n=20):
    den = (df.high-df.low)
    mfm = (((df.close-df.low)-(df.high-df.close))/den.where(den != 0)).where(den != 0, 0.0)
    mfv = mfm*df.volume
    vol = pd.to_numeric(df.volume, errors="coerce").where(pd.to_numeric(df.volume, errors="coerce") >= 0)
    mfv = mfv.where(vol.notna())
    return mfv.rolling(n, min_periods=n).sum()/vol.rolling(n, min_periods=n).sum().replace(0, np.nan)


CMF_PERIODS = (180, 90, 60, 30, 20, 14, 10, 7, 3, 1)


def cmf_multi(df, periods=CMF_PERIODS):
    """CMF multi-periode dari candle harian OHLCV; data tak cukup/invalid -> NaN."""
    out = {int(n): np.nan for n in periods}
    if df is None or df.empty:
        return out
    required = {"high", "low", "close", "volume"}
    if not required.issubset(df.columns):
        return out
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    volume = pd.to_numeric(df["volume"], errors="coerce")
    den = high - low
    mfm = (((close - low) - (high - close)) / den.where(den != 0)).where(den != 0, 0.0)
    mfm = mfm.replace([np.inf, -np.inf], np.nan)
    # Harga/volume invalid tidak boleh menyumbang ke perhitungan; rentang nol memberi MFM=0.
    valid = high.notna() & low.notna() & close.notna() & volume.notna() & (volume >= 0) & (high >= low)
    mfv = (mfm.where(valid) * volume.where(valid)).replace([np.inf, -np.inf], np.nan)
    volume = volume.where(valid)
    for n in periods:
        n = int(n)
        if n < 1 or len(df) < n:
            continue
        vol_sum = volume.rolling(n, min_periods=n).sum().iloc[-1]
        mfv_sum = mfv.rolling(n, min_periods=n).sum().iloc[-1]
        if pd.notna(vol_sum) and pd.notna(mfv_sum) and vol_sum > 0:
            value = sf(mfv_sum / vol_sum)
            if np.isfinite(value):
                out[n] = float(max(-1.0, min(1.0, value)))
    return out


def cmf_classification(values, df, obv_bull, vol_ratio, dist_sma20, support):
    """Satu interpretasi pola CMF; bukan bukti niat pelaku pasar atau sinyal beli otomatis."""
    v = {n: sf(values.get(n)) for n in CMF_PERIODS}
    long_vals = [v[n] for n in (180, 90) if np.isfinite(v[n])]
    mid_vals = [v[n] for n in (60, 30, 20) if np.isfinite(v[n])]
    short_vals = [v[n] for n in (14, 10, 7) if np.isfinite(v[n])]
    ultra_vals = [v[n] for n in (3, 1) if np.isfinite(v[n])]
    if len(long_vals) < 1 or len(mid_vals) < 1 or len(short_vals) < 1 or len(ultra_vals) < 1:
        return "FLOW BELUM JELAS", "Data CMF multi-periode belum mencukupi."
    long_pos = sum(x > 0 for x in long_vals) >= max(1, len(long_vals))
    long_neg = sum(x < 0 for x in long_vals) >= max(1, len(long_vals))
    mid_pos = sum(x > 0 for x in mid_vals) >= max(1, (len(mid_vals)+1)//2)
    mid_neg = sum(x < 0 for x in mid_vals) >= max(1, (len(mid_vals)+1)//2)
    short_improving = np.mean(short_vals + ultra_vals) > np.mean(long_vals + mid_vals)
    short_neg = sum(x < 0 for x in short_vals + ultra_vals) >= max(2, len(short_vals + ultra_vals)//2)
    price = sf(df.close.iloc[-1]) if df is not None and len(df) else np.nan
    prev_low = sf(df.low.iloc[-2]) if df is not None and len(df) >= 2 else np.nan
    prev_close = sf(df.close.iloc[-2]) if df is not None and len(df) >= 2 else np.nan
    # Shakeout hanya kandidat jika support sempat tersapu lalu direbut kembali.
    swept_reclaimed = (np.isfinite(price) and np.isfinite(prev_low) and np.isfinite(support)
                       and prev_low < support and price >= support and np.isfinite(prev_close) and prev_close < support)
    volume_confirms = np.isfinite(sf(vol_ratio)) and sf(vol_ratio) >= 1.0
    near_base = np.isfinite(sf(dist_sma20)) and sf(dist_sma20) <= 0.10
    if swept_reclaimed and short_improving and long_pos and volume_confirms and obv_bull:
        return "POTENSI SHAKEOUT — BUTUH KONFIRMASI", "Support sempat tersapu lalu direbut; tunggu konfirmasi lanjutan."
    if long_neg and mid_neg and short_neg:
        return "POTENSI DISTRIBUSI", "Flow lintas periode melemah; periksa support, OBV, dan volume."
    if long_pos and mid_pos and short_improving and obv_bull and near_base:
        return "POTENSI AKUMULASI", "Flow relatif konsisten dan OBV mendukung saat harga dekat basis."
    if long_neg and short_improving:
        return "POTENSI PEMULIHAN AWAL — BELUM TENTU AKUMULASI", "Flow pendek membaik, tetapi flow panjang masih negatif."
    if long_pos and short_neg:
        return "JANGKA PANJANG POSITIF, FLOW PENDEK MELEMAH", "Jangan buru-buru menyebut akumulasi/shakeout; tunggu harga dan volume mengonfirmasi."
    if (not obv_bull and short_neg) or (mid_neg and short_neg):
        return "KENAIKAN BELUM TERKONFIRMASI — WASPADAI DISTRIBUSI", "CMF/OBV belum mendukung; hindari mengejar candle bullish."
    return "FLOW BELUM JELAS", "Sinyal antarperiode belum cukup konsisten untuk klasifikasi yang lebih tegas."


def cmf_fmt(x):
    return f"{sf(x):+.3f}" if np.isfinite(sf(x)) else "N/A"


def bbands(s, n=20, mult=2):
    mid = sma(s, n); std = s.rolling(n, min_periods=n).std(ddof=0)
    up, lo = mid+mult*std, mid-mult*std
    return up, mid, lo, (up-lo)/mid.replace(0, np.nan)


def enrich(df):
    df = df.copy().sort_index()
    df["sma20"] = sma(df.close, 20); df["sma50"] = sma(df.close, 50)
    df["sma100"] = sma(df.close, 100); df["sma200"] = sma(df.close, 200)
    df["rsi"] = rsi(df.close)
    df["macd"], df["macd_sig"], df["macd_hist"] = macd(df.close)
    df["tsi"], df["tsi_sig"] = tsi(df.close)
    df["stoch"], df["stoch_k"], df["stoch_d"] = stoch_rsi(df.close)
    df["adx"] = adx(df)
    df["cmf"] = cmf(df)
    df["obv"] = (np.sign(df.close.diff()).fillna(0)*df.volume).cumsum()
    df["obv_ema20"] = ema(df.obv, 20)
    df["atr"] = atr(df)
    df["bb_up"], df["bb_mid"], df["bb_lo"], df["bb_width"] = bbands(df.close)
    df["vol_ma20"] = sma(df.volume, 20)
    df["vol_ratio"] = df.volume/df.vol_ma20.replace(0, np.nan)
    return df


def cross_up(a, b):
    return len(a) >= 2 and len(b) >= 2 and bool(a.iloc[-2] <= b.iloc[-2] and a.iloc[-1] > b.iloc[-1])


def rising(s, n=4):
    return len(s) >= n+1 and bool(s.iloc[-1] > s.iloc[-1-n])


def slope_pos(s, n=5):
    if len(s) < n: return False
    y = s.iloc[-n:].astype(float).values
    return bool(np.polyfit(np.arange(n), y, 1)[0] > 0)


def bb_squeeze(df):
    w = df.bb_width.dropna()
    if len(w) < 40: return False
    x = w.iloc[-40:]
    return bool((x <= x.iloc[-1]).mean() <= 0.20)


def candle_patterns(df):
    if len(df) < 3:
        return {"hammer":False,"bullish_engulfing":False,"morning_star":False}
    a,b,c = df.iloc[-1],df.iloc[-2],df.iloc[-3]
    body = abs(a.close-a.open); upper = a.high-max(a.open,a.close); lower=min(a.open,a.close)-a.low
    hammer = body>0 and lower >= 2*body and upper <= body
    engulf = b.close<b.open and a.close>a.open and a.open<=b.close and a.close>=b.open
    rb = max(b.high-b.low,1e-12); bodyb=abs(b.close-b.open)
    morning = c.close<c.open and bodyb <= 0.35*rb and a.close>a.open and a.close >= (c.open+c.close)/2
    return {"hammer":bool(hammer),"bullish_engulfing":bool(engulf),"morning_star":bool(morning)}


def macd_early(df):
    h=df.macd_hist; m=df.macd; s=df.macd_sig
    if len(h)<4: return False
    fading = h.iloc[-4]<0 and h.iloc[-3]<0 and h.iloc[-2]>=h.iloc[-3] and h.iloc[-1]>=h.iloc[-2]
    green = h.iloc[-2]<=0 and h.iloc[-1]>0
    return bool(fading or green or cross_up(m,s))


def tsi_bull(df):
    if len(df)<3: return False
    return bool(cross_up(df.tsi,df.tsi_sig) or (df.tsi.iloc[-2]<0<=df.tsi.iloc[-1]))


def stoch_bull(df):
    return bool(cross_up(df.stoch_k,df.stoch_d) or (slope_pos(df.stoch_k,3) and slope_pos(df.stoch_d,3)))


def rsi_rebound(df):
    r=df.rsi.dropna()
    if len(r)<12: return False
    q=r.iloc[-12:]
    lo=q.min(); idx=int(np.argmin(q.values)); cur=q.iloc[-1]
    return bool(lo<=35 and idx < len(q)-2 and cur > lo+5 and cur < 68)


def fetch_df(ex, symbol, tf, limit):
    try:
        raw=ex.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
        if not raw or len(raw)<80: return None
        df=pd.DataFrame(raw,columns=["timestamp","open","high","low","close","volume"])
        df.index=pd.to_datetime(df.timestamp,unit="ms",utc=True)
        for c in ["open","high","low","close","volume"]: df[c]=pd.to_numeric(df[c],errors="coerce")
        return df.dropna(subset=["open","high","low","close","volume"])
    except Exception as e:
        log(f"[OHLCV] {symbol} {tf}: {e}")
        return None


def btc_regime(d,w):
    """BTC macro/regime sebagai INFO SAJA, bukan komponen score coin."""
    x=d.iloc[-1]
    y=w.iloc[-1]
    s=0
    why=[]

    checks = [
        (x.close>x.sma20,2,"1D harga di atas SMA20"),
        (x.close>x.sma50,3,"1D harga di atas SMA50"),
        (x.close>x.sma100,2,"1D harga di atas SMA100"),
        (x.close>x.sma200,2,"1D harga di atas SMA200"),
        (x.rsi>=50,1,"1D RSI >=50"),
        (x.macd_hist>0,2,"1D MACD histogram positif"),
        (x.cmf>0,1,"1D CMF positif"),
        (y.close>y.sma20,1,"1W harga di atas SMA20"),
        (y.rsi>=50,1,"1W RSI >=50"),
    ]
    for cond, pts, txt in checks:
        if pd.notna(cond) and cond:
            s += pts
            why.append(txt)

    if s>=12:
        reg="RISK-ON / BULLISH"
    elif s>=9:
        reg="BULLISH CAUTIOUS"
    elif s>=6:
        reg="NEUTRAL / TRANSITION"
    elif s>=4:
        reg="RISK-OFF MILD"
    else:
        reg="RISK-OFF"

    def rel(a, b):
        if pd.isna(a) or pd.isna(b):
            return "N/A"
        return "DI ATAS" if a > b else "DI BAWAH"

    # x adalah baris terakhir (scalar), jadi gunakan Series dari dataframe
    # untuk membandingkan histogram MACD dengan candle sebelumnya.
    macd_hist_prev = d["macd_hist"].iloc[-2] if len(d) >= 2 else np.nan
    macd_hist_now = x.macd_hist
    if pd.notna(macd_hist_now) and pd.notna(macd_hist_prev):
        macd_mom = "menguat" if macd_hist_now > macd_hist_prev else "melemah"
    else:
        macd_mom = "N/A"

    if pd.notna(x.cmf):
        cmf_state = "positif" if x.cmf > 0 else "negatif"
    else:
        cmf_state = "N/A"

    if reg == "RISK-ON / BULLISH":
        implication = "Lingkungan pasar mendukung momentum; altcoin biasanya lebih kondusif."
    elif reg == "BULLISH CAUTIOUS":
        implication = "Pasar cenderung mendukung, tetapi tetap perlu seleksi setup."
    elif reg == "NEUTRAL / TRANSITION":
        implication = "Pasar belum tegas; fokus pada coin dengan setup paling kuat."
    elif reg == "RISK-OFF MILD":
        implication = "Tekanan pasar meningkat; kurangi agresivitas dan hindari chase."
    else:
        implication = "Pasar defensif; peluang tetap bisa ada, tetapi risiko lebih tinggi."

    return {
        "score": s,  # indikator kekuatan regime saja, BUKAN score coin
        "regime": reg,
        "why": why,
        "price": float(x.close),
        "rsi1d": sf(x.rsi),
        "rsi1w": sf(y.rsi),
        "cmf1d": sf(x.cmf),
        "macd_hist": sf(x.macd_hist),
        "macd_mom": macd_mom,
        "sma20_1d": sf(x.sma20),
        "sma50_1d": sf(x.sma50),
        "sma100_1d": sf(x.sma100),
        "sma200_1d": sf(x.sma200),
        "sma20_rel": rel(x.close, x.sma20),
        "sma50_rel": rel(x.close, x.sma50),
        "sma200_rel": rel(x.close, x.sma200),
        "week_sma20_rel": rel(y.close, y.sma20),
        "cmf_state": cmf_state,
        "implication": implication,
    }


def smart_proxy(df):
    p=float(df.close.iloc[-1]); out={}
    for days in [30,60,180]:
        sub=df.tail(min(days,len(df))); tv=(sub.high+sub.low+sub.close)/3
        den=sub.volume.sum(); cost=float((tv*sub.volume).sum()/den) if den>0 else float(sub.close.mean())
        out[str(days)]= {"cost":cost,"premium":p/cost-1 if cost>0 else np.nan}
    c=float(df.cmf.iloc[-1]) if pd.notna(df.cmf.iloc[-1]) else np.nan
    cp=float(df.cmf.iloc[-6]) if len(df)>=6 and pd.notna(df.cmf.iloc[-6]) else np.nan
    obv_bull=bool(pd.notna(df.obv_ema20.iloc[-1]) and df.obv.iloc[-1]>df.obv_ema20.iloc[-1] and slope_pos(df.obv,5))
    dist=bool(np.isfinite(c) and np.isfinite(cp) and c<0 and c<cp and out["30"]["premium"]>0.12 and out["60"]["premium"]>0.18)
    accum=bool(np.isfinite(c) and c>0 and out["30"]["premium"]<=0.08 and out["60"]["premium"]<=0.12 and obv_bull)
    reacc=bool(np.isfinite(c) and c>0 and 0.08<out["30"]["premium"]<=0.20 and obv_bull)
    beh="DISTRIBUSI / EXIT RISK" if dist else "AKUMULASI / RE-ACCUMULATION" if accum else "MARKUP + RE-ACCUMULATION" if reacc else "NETRAL / CAMPURAN"
    return out,beh


def levels(df):
    r=df.tail(21); sup=float(r.low.iloc[:-1].min()) if len(r)>2 else float(r.low.min()); res=float(r.high.iloc[:-1].max()) if len(r)>2 else float(r.high.max())
    av=float(df.atr.iloc[-1]) if pd.notna(df.atr.iloc[-1]) else max(float(df.close.iloc[-1])*0.03,1)
    sl=sup-SL_ATR_BUFFER*av
    rng=max(res-sup,float(df.close.iloc[-1])*0.05); tp2=res+0.75*rng
    return sup,res,av,sl,tp2


def analyze(symbol, d, t, w, btc):
    """EARLY SWING / BEFORE PUMP scoring.

    Fokus: akumulasi, momentum awal, kompresi, dan entry yang masih dekat basis.
    BTC Macro hanya informasi dan TIDAK mempengaruhi score coin.
    """
    e=d.iloc[-1]; tt=t.iloc[-1]; ww=w.iloc[-1]
    smart_score=0; momentum=0; structure=0; risk=0
    reasons=[]; warns=[]; tags=[]
    close=float(e.close)
    rsi1=sf(e.rsi); rsi3=sf(tt.rsi); rsiw=sf(ww.rsi)
    vr=sf(e.vol_ratio)
    dist20=(close/sf(e.sma20)-1) if pd.notna(e.sma20) and sf(e.sma20)>0 else np.nan
    dist50=(close/sf(e.sma50)-1) if pd.notna(e.sma50) and sf(e.sma50)>0 else np.nan

    # Levels dihitung di awal supaya penilaian posisi/resistance konsisten.
    sup,res,av,sl,tp2=levels(d)
    entry=max(sup, close-0.75*av)
    brk=res+0.15*av
    rr1=(res-entry)/(entry-sl) if entry>sl else np.nan
    rr2=(tp2-brk)/(brk-sl) if brk>sl else np.nan
    best_rr=max([x for x in [rr1,rr2] if np.isfinite(x)] or [np.nan])
    stop_pct=(close-sl)/close if close>0 else np.nan
    range_to_res=(res-close)/close if close>0 else np.nan

    cp=candle_patterns(d)
    squeeze=bb_squeeze(d)
    upper=bool(pd.notna(e.bb_up) and close>=e.bb_up)
    early_macd=macd_early(d); early_macd3=macd_early(t)
    tsi1=tsi_bull(d); tsi3=tsi_bull(t)
    stoch1=stoch_bull(d); stoch3=stoch_bull(t)

    # ========================================================
    # 1) SMART MONEY / ACCUMULATION — max 35
    # ========================================================
    sm,beh=smart_proxy(d)
    cmf_values=cmf_multi(d)
    cmf1=sf(cmf_values.get(1)); cmf3=sf(cmf_values.get(3)); cmf10=sf(cmf_values.get(10))

    obv_bull=bool(pd.notna(e.obv_ema20) and e.obv>e.obv_ema20 and slope_pos(d.obv,5))
    cmf_flow, cmf_flow_note = cmf_classification(cmf_values, d, obv_bull, vr, dist20, sup)
    # Satu skor gabungan Smart Money: perilaku historis + pola CMF + OBV, tanpa poin per periode.
    if cmf_flow == "POTENSI AKUMULASI":
        smart_score += 12; reasons.append("Pola CMF lintas periode mendukung potensi akumulasi"); tags.append("AKUMULASI")
    elif cmf_flow.startswith("POTENSI SHAKEOUT"):
        smart_score += 7; reasons.append("Kemungkinan shakeout; perlu konfirmasi support"); tags.append("POTENSI SHAKEOUT")
    elif cmf_flow.startswith("POTENSI PEMULIHAN AWAL"):
        smart_score += 5; reasons.append("Flow pendek membaik, pemulihan belum terkonfirmasi")
    elif cmf_flow.startswith("POTENSI DISTRIBUSI") or cmf_flow.startswith("KENAIKAN BELUM TERKONFIRMASI"):
        smart_score -= 8; warns.append("Pola CMF/OBV menunjukkan risiko distribusi atau konfirmasi lemah")
    elif cmf_flow.startswith("JANGKA PANJANG POSITIF"):
        smart_score += 2; warns.append("Flow pendek melemah meski jangka panjang positif")
    else:
        # Pertahankan konteks proxy lama untuk kondisi campuran, tetapi jangan beri bonus CMF berulang.
        if 'MARKUP' in beh:
            smart_score += 4; tags.append('EARLY MARKUP')
        elif 'DISTRIBUSI' in beh:
            smart_score -= 4; warns.append('Smart-money proxy menunjukkan distribusi')
        else:
            smart_score += 2; reasons.append('Smart-money proxy belum memberi arah dominan')
        warns.append("Pola CMF belum jelas atau data tidak cukup")

    if obv_bull:
        smart_score+=7; reasons.append('OBV naik dan di atas EMA20')

    if np.isfinite(vr):
        if 1.10<=vr<=1.80:
            smart_score+=5; reasons.append('Volume mulai berkembang, belum ekstrem')
        elif 0.85<=vr<1.10:
            smart_score+=3
        elif vr>2.20:
            smart_score-=3; warns.append('Volume terlalu meledak; risiko sudah telat/climax')

    if np.isfinite(cmf1) and cmf1<0 and np.isfinite(rsi1) and rsi1>60:
        warns.append('RSI kuat tetapi aliran dana masih negatif')
    smart_score=int(clamp(smart_score,0,35))

    # ========================================================
    # 2) EARLY MOMENTUM — max 30
    # ========================================================
    if np.isfinite(rsi1):
        if 40<=rsi1<48 and slope_pos(d.rsi,4):
            momentum+=5; reasons.append('RSI 1D recovery dari zona rendah')
        elif 48<=rsi1<=58 and slope_pos(d.rsi,4):
            momentum+=8; reasons.append('RSI 1D mulai naik dari zona awal')
        elif 58<rsi1<=EARLY_RSI_MAX and slope_pos(d.rsi,4):
            momentum+=5; reasons.append('RSI 1D masih sehat dan menanjak')
        elif rsi1>EARLY_RSI_MAX:
            momentum-=5; warns.append('RSI 1D sudah tinggi untuk entry awal')

    if np.isfinite(rsi3):
        if 42<=rsi3<=65 and rising(t.rsi,4):
            momentum+=5; reasons.append('RSI 3D ikut menguat')
        elif rsi3>68:
            momentum-=3; warns.append('RSI 3D mulai panas')

    if np.isfinite(rsiw):
        if 42<=rsiw<=65 and slope_pos(w.rsi,4):
            momentum+=4; reasons.append('RSI 1W masih sehat')
        elif rsiw>70:
            momentum-=3; warns.append('RSI 1W sudah tinggi')

    if early_macd:
        momentum+=5; reasons.append('MACD 1D masuk fase bullish awal')
    if early_macd3:
        momentum+=3; reasons.append('MACD 3D mulai bullish')
    if tsi1: momentum+=2
    if tsi3: momentum+=1
    if stoch1 and (not np.isfinite(rsi1) or rsi1<=EARLY_RSI_MAX):
        momentum+=2; reasons.append('Stoch RSI 1D berbalik naik')
    if stoch3 and (not np.isfinite(rsi3) or rsi3<=68): momentum+=1
    if cp['bullish_engulfing'] or cp['morning_star']:
        momentum+=2; tags.append('REVERSAL CANDLE')
    elif cp['hammer']:
        momentum+=1; tags.append('HAMMER')
    if squeeze:
        momentum+=2; tags.append('BB SQUEEZE'); reasons.append('Volatilitas sedang terkompresi')
    if upper:
        momentum-=4; warns.append('Sudah menyentuh Upper BB — bukan area terbaik untuk mengejar')
    momentum=int(clamp(momentum,0,30))

    # ========================================================
    # 3) MARKET STRUCTURE / PRICE POSITION — max 20
    # ========================================================
    if pd.notna(e.close) and pd.notna(e.sma20) and close>=e.sma20: structure+=2
    elif np.isfinite(dist20) and -0.06<=dist20<0: structure+=3; reasons.append('Harga masih sedikit di bawah SMA20, dekat basis')
    if pd.notna(e.sma20) and pd.notna(e.sma50) and e.sma20>=e.sma50: structure+=3
    if pd.notna(tt.sma20) and pd.notna(tt.sma50) and tt.sma20>=tt.sma50: structure+=2
    if pd.notna(ww.sma20) and pd.notna(ww.sma50) and ww.sma20>=ww.sma50: structure+=2
    if np.isfinite(dist20):
        if -0.03<=dist20<=0.05:
            structure+=5; reasons.append('Harga sangat dekat basis SMA20')
        elif 0.05<dist20<=EARLY_DIST_MAX:
            structure+=3
        elif dist20>0.15:
            structure-=5; warns.append('Harga terlalu jauh dari basis SMA20')
        elif dist20>EARLY_DIST_MAX:
            structure-=2; warns.append('Harga mulai menjauh dari basis')
    if np.isfinite(dist50) and -0.05<=dist50<=0.08: structure+=2
    if np.isfinite(range_to_res):
        if 0<=range_to_res<=0.08:
            structure+=3; reasons.append('Harga dekat resistance pemicu breakout')
        elif range_to_res>0.25:
            structure+=1
    adxv=sf(e.adx)
    if np.isfinite(adxv) and 18<=adxv<=35:
        structure+=2
    elif np.isfinite(adxv) and adxv>55:
        warns.append('ADX sangat tinggi — tren cenderung sudah matang')
    structure=int(clamp(structure,0,20))

    # ========================================================
    # 4) RISK / ENTRY QUALITY — max 15
    # ========================================================
    if np.isfinite(best_rr):
        if best_rr>=2.5: risk+=6
        elif best_rr>=2.0: risk+=5
        elif best_rr>=1.8: risk+=4
        elif best_rr>=1.3: risk+=2
        else: warns.append('Risk/reward belum menarik')
    if np.isfinite(stop_pct):
        if stop_pct<=0.10: risk+=4
        elif stop_pct<=0.15: risk+=3
        elif stop_pct<=0.20: risk+=1
        else: warns.append('Stop struktural terlalu lebar')
    if np.isfinite(dist20) and dist20<=EARLY_DIST_MAX: risk+=2
    if squeeze: risk+=2
    if np.isfinite(vr) and 0.90<=vr<=1.80: risk+=1
    risk=int(clamp(risk,0,15))

    score=int(clamp(smart_score+momentum+structure+risk))
    grade='A+' if score>=85 else 'A' if score>=75 else 'B' if score>=65 else 'C' if score>=55 else 'D'

    # Separate scoring models for short swing and longer investment-cycle candidates.
    # These are intentionally independent of the BEFORE-PUMP score and are not buy orders.
    swing_score = 0
    if np.isfinite(rsi1):
        if 45 <= rsi1 <= 65: swing_score += 12
        elif 38 <= rsi1 < 45 or 65 < rsi1 <= 70: swing_score += 7
        elif rsi1 > 75: swing_score -= 8
    if np.isfinite(rsi3):
        if 45 <= rsi3 <= 68: swing_score += 10
        elif 38 <= rsi3 < 45: swing_score += 5
    if early_macd: swing_score += 10
    if early_macd3: swing_score += 8
    if tsi1: swing_score += 5
    if tsi3: swing_score += 5
    if stoch1: swing_score += 4
    if obv_bull: swing_score += 10
    if np.isfinite(vr):
        if 1.0 <= vr <= 2.0: swing_score += 8
        elif 0.7 <= vr < 1.0: swing_score += 3
        elif vr > 2.5: swing_score -= 5
    if np.isfinite(dist20):
        if -0.05 <= dist20 <= 0.08: swing_score += 8
        elif dist20 > 0.15: swing_score -= 8
    if np.isfinite(best_rr):
        if best_rr >= 2.0: swing_score += 10
        elif best_rr >= 1.5: swing_score += 5
        else: swing_score -= 8
    if cmf_flow == "POTENSI AKUMULASI": swing_score += 10
    elif cmf_flow.startswith("POTENSI DISTRIBUSI") or cmf_flow.startswith("KENAIKAN BELUM TERKONFIRMASI"): swing_score -= 10
    if np.isfinite(stop_pct) and stop_pct > MAX_STRUCTURAL_STOP_PCT: swing_score -= 15
    swing_score = int(clamp(swing_score, 0, 100))
    if swing_score >= 72 and (early_macd or early_macd3) and np.isfinite(best_rr) and best_rr >= MIN_RR:
        swing_label = "SETUP SWING KUAT"
        swing_reason = "momentum dan RR mendukung; tunggu pemicu harga"
    elif swing_score >= 58:
        swing_label = "PANTAU SWING"
        swing_reason = "ada sinyal awal, konfirmasi belum lengkap"
    else:
        swing_label = "BELUM SIAP"
        swing_reason = "momentum/flow/risiko belum cukup searah"

    cmf180 = sf(cmf_values.get(180)); cmf90 = sf(cmf_values.get(90)); cmf60 = sf(cmf_values.get(60))
    cycle_score = 0
    weekly_close = sf(ww.close); weekly_sma20 = sf(ww.sma20); weekly_sma50 = sf(ww.sma50)
    weekly_trend = bool(np.isfinite(weekly_close) and np.isfinite(weekly_sma20) and weekly_close > weekly_sma20)
    weekly_stack = bool(np.isfinite(weekly_sma20) and np.isfinite(weekly_sma50) and weekly_sma20 > weekly_sma50)
    if weekly_trend: cycle_score += 20
    if weekly_stack: cycle_score += 20
    if np.isfinite(rsiw):
        if 45 <= rsiw <= 68: cycle_score += 10
        elif 38 <= rsiw < 45: cycle_score += 5
    long_cmf = [x for x in (cmf180, cmf90, cmf60) if np.isfinite(x)]
    if len(long_cmf) >= 2:
        pos_count = sum(x > 0.05 for x in long_cmf)
        neg_count = sum(x < -0.05 for x in long_cmf)
        cycle_score += 20 if pos_count >= 2 else 10 if pos_count == 1 and neg_count == 0 else 0
        if neg_count >= 2: cycle_score -= 10
    elif len(long_cmf) == 1:
        cycle_score += 5 if long_cmf[0] > 0.05 else 0
    if obv_bull: cycle_score += 10
    if np.isfinite(dist50) and dist50 >= -0.05: cycle_score += 5
    if np.isfinite(vr) and vr >= 0.8: cycle_score += 5
    if np.isfinite(rsiw) and rsiw > 75: cycle_score -= 10
    cycle_score = int(clamp(cycle_score, 0, 100))
    base_name = symbol.split('/')[0].upper()
    cycle_excluded = base_name in {"XAUT", "PAXG", "USDT", "USDC", "DAI", "TUSD", "BUSD", "FDUSD", "USDE"}
    if cycle_excluded:
        cycle_label = "TIDAK DIPERINGKATKAN UNTUK ALTCOIN CYCLE"
        cycle_reason = "aset berpatokan emas/stablecoin; beda karakter dari altcoin cycle"
    elif cycle_score >= 70 and weekly_trend and weekly_stack and len(long_cmf) >= 2 and sum(x > 0 for x in long_cmf) >= 2:
        cycle_label = "CALON CYCLE KUAT"
        cycle_reason = "trend mingguan dan flow jangka panjang searah"
    elif cycle_score >= 52:
        cycle_label = "AKUMULASI / PANTAU CYCLE"
        cycle_reason = "sebagian kondisi jangka panjang mendukung, belum lengkap"
    else:
        cycle_label = "BELUM TERKONFIRMASI"
        cycle_reason = "trend mingguan atau flow panjang belum cukup kuat"

    early_core=(score>=MIN_REPORT_SCORE and ('AKUMULASI' in beh or 'RE-ACCUMULATION' in beh) and np.isfinite(rsi1) and rsi1<=EARLY_RSI_MAX and np.isfinite(dist20) and -0.08<=dist20<=EARLY_DIST_MAX and not upper)
    early_setup=(score>=MIN_REPORT_SCORE and (early_macd or squeeze or stoch1 or tsi1) and np.isfinite(rsi1) and rsi1<=68 and np.isfinite(dist20) and -0.08<=dist20<=0.12 and not upper)

    if np.isfinite(rsi1) and rsi1>=EXTREME_RSI:
        status='DO NOT CHASE'
    elif np.isfinite(stop_pct) and stop_pct>MAX_STRUCTURAL_STOP_PCT:
        status='NO TRADE'
    elif early_core and np.isfinite(best_rr) and best_rr>=PREFERRED_RR:
        status='EARLY BUY ZONE'
    elif early_core or early_setup:
        status='BEFORE BREAKOUT'
    elif np.isfinite(rsi1) and rsi1>FOMO_RSI:
        status='DO NOT CHASE'
    elif score>=MIN_REPORT_SCORE:
        status='WATCH EARLY'
    elif score>=50:
        status='WATCH'
    else:
        status='NO TRADE'

    if np.isfinite(rsi1) and rsi1>70: warns.append('Sudah terlalu panas untuk strategi BEFORE PUMP')
    if np.isfinite(dist20) and dist20>0.12: warns.append('Sudah terlalu jauh dari basis')

    return {
        'symbol':symbol,'score':score,'grade':grade,'status':status,'price':close,
        'support':sup,'resistance':res,'entry':entry,'breakout_entry':brk,'sl':sl,'tp1':res,'tp2':tp2,
        'rr1':rr1,'rr2':rr2,'stop_pct':stop_pct,'rsi1d':rsi1,'rsi3d':rsi3,'rsi1w':rsiw,
        'adx':sf(e.adx),'vol_ratio':vr,'cmf':sf(e.cmf),'cmf_multi':cmf_values,'cmf_flow':cmf_flow,'cmf_flow_note':cmf_flow_note,
        'obv_bull':obv_bull,'bb_squeeze':squeeze,'upper_break':upper,'smart':sm,'smart_beh':beh,
        'smart_score':smart_score,'momentum_score':momentum,'structure_score':structure,'risk_score':risk,
        'dist_sma20':dist20,'dist_sma50':dist50,'range_to_res':range_to_res,
        'swing_score':swing_score,'swing_label':swing_label,'swing_reason':swing_reason,
        'cycle_score':cycle_score,'cycle_label':cycle_label,'cycle_reason':cycle_reason,'cycle_excluded':cycle_excluded,
        'tags':tags[:6],'reasons':reasons[:6],'warns':warns[:6],
    }


def exchange():
    return getattr(ccxt,EXCHANGE_ID)({"enableRateLimit":True,"timeout":30000})


def liquid_symbols(ex,markets):
    try: tickers=ex.fetch_tickers()
    except Exception as e:
        log(f"[MARKETS] {e}"); tickers={}
    arr=[]
    for sym,m in markets.items():
        if not (m.get("spot") and m.get("quote")=="IDR" and m.get("active") is not False): continue
        base=str(m.get("base","")).upper()
        if base in {"USDT","USDC","DAI","TUSD"} or any(x in base for x in ["3L","3S","5L","5S","BULL","BEAR"]): continue
        t=tickers.get(sym,{}); last=sf(t.get("last")); bv=sf(t.get("baseVolume")); qv=sf(t.get("quoteVolume"))
        if not np.isfinite(qv) and np.isfinite(last) and np.isfinite(bv): qv=last*bv
        if np.isfinite(qv) and qv>=MIN_24H_QUOTE_VOLUME_IDR and sym!="BTC/IDR": arr.append((sym,qv))
    arr.sort(key=lambda x:x[1],reverse=True)
    return [x[0] for x in arr[:MAX_MARKETS]]


def message(r, btc):
    sm = r["smart"]
    reasons = "\n".join("• " + html_escape(x) for x in r["reasons"]) or "• Tidak ada konfirmasi dominan."
    warns = "\n".join("• " + html_escape(x) for x in r["warns"]) or "• Tidak ada warning utama."
    rr = max([x for x in [r["rr1"], r["rr2"] if np.isfinite(r["rr2"]) else np.nan] if np.isfinite(x)] or [np.nan])
    return f"""━━━━━━━━━━━━━━━━━━━━
🌱 ZENA EARLY SWING ALERT
━━━━━━━━━━━━━━━━━━━━
COIN: {coin_link(r['symbol'])}
EARLY SCORE: {r['score']}/100 ({r['grade']})
STATUS: {html_escape(r['status'])}

🧩 SCORE BREAKDOWN
Smart Money: {r['smart_score']}/35
Early Momentum: {r['momentum_score']}/30
Posisi/Struktur: {r['structure_score']}/20
Risk/Entry: {r['risk_score']}/15

🌐 BTC MACRO — INFO SAJA
Regime: {html_escape(btc['regime'])}
BTC: {idr(btc['price'])}
RSI 1D/1W: {fx(btc['rsi1d'])} / {fx(btc['rsi1w'])}
Trend 1D: SMA20 {btc.get('sma20_rel','N/A')} | SMA50 {btc.get('sma50_rel','N/A')} | SMA200 {btc.get('sma200_rel','N/A')}
MACD 1D: {btc.get('macd_mom','N/A')}
Makna: {html_escape(btc['implication'])}
⚠️ BTC Macro TIDAK dihitung ke score coin.

PRICE: {idr(r['price'])}
DIST SMA20: {pct(r.get('dist_sma20', np.nan))} | KE RESISTANCE: {pct(r.get('range_to_res', np.nan))}
SETUP: {html_escape(', '.join(r['tags']) if r['tags'] else 'EARLY BASE')}

📊 MULTI-TF
1D RSI {fx(r['rsi1d'])} | 3D RSI {fx(r['rsi3d'])} | 1W RSI {fx(r['rsi1w'])}
1D ADX {fx(r['adx'])} | Volume {fx(r['vol_ratio'],2)}x VMA20
Flow: {html_escape(r.get('cmf_flow', 'FLOW BELUM JELAS'))}
Makna flow: {html_escape(r.get('cmf_flow_note', 'Data belum cukup'))}
CMF 180D/90D: {cmf_fmt(r['cmf_multi'].get(180))} / {cmf_fmt(r['cmf_multi'].get(90))}
CMF 60D/30D/20D: {cmf_fmt(r['cmf_multi'].get(60))} / {cmf_fmt(r['cmf_multi'].get(30))} / {cmf_fmt(r['cmf_multi'].get(20))}
CMF 14D/10D/7D: {cmf_fmt(r['cmf_multi'].get(14))} / {cmf_fmt(r['cmf_multi'].get(10))} / {cmf_fmt(r['cmf_multi'].get(7))}
CMF 3D/1D: {cmf_fmt(r['cmf_multi'].get(3))} / {cmf_fmt(r['cmf_multi'].get(1))}
OBV {'mendukung' if r['obv_bull'] else 'belum mendukung'}
BB Squeeze {'YES' if r['bb_squeeze'] else 'NO'} | Upper Break {'YES' if r['upper_break'] else 'NO'}

🐋 SMART-MONEY PROXY
180D {idr(sm['180']['cost'])} ({pct(sm['180']['premium'])})
60D  {idr(sm['60']['cost'])} ({pct(sm['60']['premium'])})
30D  {idr(sm['30']['cost'])} ({pct(sm['30']['premium'])})
Behavior: {html_escape(r['smart_beh'])}
Proxy historis VWAP/volume — BUKAN cost basis wallet whale.

🎯 LEVEL
Support: {idr(r['support'])}
Resistance: {idr(r['resistance'])}
Pullback Entry: {idr(r['entry'])}
Breakout Entry: {idr(r['breakout_entry'])}
SL: {idr(r['sl'])}
TP1: {idr(r['tp1'])}
TP2: {idr(r['tp2'])}
RR terbaik: {'1:'+format(rr,'.2f') if np.isfinite(rr) else 'N/A'}

✅ ALASAN
{reasons}

⚠️ WARNING
{warns}

HORIZON SWING: {r.get('swing_score',0)}/100 — {html_escape(r.get('swing_label','PANTAU'))}
HORIZON INVEST CYCLE: {r.get('cycle_score',0)}/100 — {html_escape(r.get('cycle_label','BELUM TERKONFIRMASI'))}
Catatan swing: {html_escape(r.get('swing_reason',''))}
Catatan cycle: {html_escape(r.get('cycle_reason',''))}
Horizon: swing beberapa hari–minggu; cycle beberapa bulan hingga beberapa tahun jika trend mingguan dan flow panjang mendukung.
Invalidation: close di bawah support struktural / momentum gagal. Bukan order otomatis.
━━━━━━━━━━━━━━━━━━━━"""


def load_state():
    if not STATE_FILE.exists(): return {}
    try: return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception: return {}


def save_state(s):
    try: STATE_FILE.write_text(json.dumps(s,indent=2,ensure_ascii=False),encoding="utf-8")
    except Exception as e: log(f"[STATE] {e}")


def should_alert(state,r):
    if r["score"]<MIN_ALERT_SCORE or r["status"] not in {"EARLY BUY ZONE","BEFORE BREAKOUT","WATCH EARLY"}: return False
    old=state.get(r["symbol"])
    if not old: return True
    if time.time()-float(old.get("time",0)) < ALERT_COOLDOWN_HOURS*3600: return False
    return r["status"]!=old.get("status") or r["score"]>=int(old.get("score",0))+5


def scan_once(ex):
    markets=ex.load_markets()
    b1=fetch_df(ex,"BTC/IDR",TF_1D,LIMIT_1D); bw=fetch_df(ex,"BTC/IDR",TF_1W,LIMIT_1W)
    if b1 is None or bw is None: raise RuntimeError("BTC data unavailable")
    b1=enrich(b1); bw=enrich(bw); btc=btc_regime(b1,bw)
    log(f"[BTC] {btc['regime']} score={btc['score']}/15 price={idr(btc['price'])}")
    syms=liquid_symbols(ex,markets); log(f"[MARKETS] {len(syms)} liquid IDR markets")
    results=[]
    for i,sym in enumerate(syms,1):
        log(f"[{i}/{len(syms)}] {sym}")
        d=fetch_df(ex,sym,TF_1D,LIMIT_1D); t=fetch_df(ex,sym,TF_3D,LIMIT_3D); w=fetch_df(ex,sym,TF_1W,LIMIT_1W)
        if any(x is None for x in [d,t,w]): continue
        try:
            r=analyze(sym,enrich(d),enrich(t),enrich(w),btc); results.append(r)
            log(f"[RESULT] {sym} early_score={r['score']} status={r['status']} RSI={r['rsi1d']:.1f} distSMA20={r['dist_sma20']*100:+.1f}% smart={r['smart_beh']}")
        except Exception as e: log(f"[ANALYZE] {sym}: {e}")
    results.sort(key=lambda x:x["score"],reverse=True)
    print("\nTOP EARLY SWING / BEFORE PUMP")
    for r in results[:10]:
        print(f"{r['symbol']:12} | {r['score']:3} | {r['status']:18} | Smart {r['smart_score']:2}/35 | Mom {r['momentum_score']:2}/30 | Pos {r['structure_score']:2}/20 | Risk {r['risk_score']:2}/15 | RSI {r['rsi1d']:.1f} | Dist {r['dist_sma20']*100:+.1f}%")

    # SELALU kirim ringkasan hasil scan ke Telegram.
    # Ini tidak dibatasi score/cooldown, sehingga Telegram tidak lagi kosong
    # hanya karena tidak ada coin yang memenuhi syarat alert.
    summary_ok = req_telegram(telegram_scan_summary(results, btc))
    if not summary_ok:
        log("[TELEGRAM] Ringkasan scan GAGAL dikirim")

    # Alert EARLY SWING memakai filter score/status + cooldown.
    state=load_state(); sent=0
    for r in results[:TOP_N]:
        if should_alert(state,r) and req_telegram(message(r,btc)):
            state[r["symbol"]]={"time":time.time(),"score":r["score"],"status":r["status"]}
            save_state(state)
            sent+=1
            log(f"[ALERT] sent {r['symbol']}")

    snapshot={"timestamp_utc":datetime.now(timezone.utc).isoformat(),"btc":btc,"top":[{"symbol":r["symbol"],"score":r["score"],"status":r["status"],"price":r["price"],"smart":r["smart_beh"]} for r in results[:10]]}
    Path("zena_last_scan.json").write_text(json.dumps(snapshot,indent=2,ensure_ascii=False),encoding="utf-8")
    log(f"[DONE] {len(results)} results | {sent} alerts")
    return results


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--once",action="store_true"); args=ap.parse_args()
    # Tes koneksi Telegram sebelum mulai scan agar error token/chat ID
    # terlihat jelas di terminal.
    test_ok = req_telegram("🟢 ZENA aktif. Memulai scan EARLY SWING / BEFORE PUMP...")
    if not test_ok:
        log("[TELEGRAM] Tes awal gagal. Scanner tetap berjalan; cek token/chat ID.")
    ex=exchange()
    if args.once:
        scan_once(ex); return
    while True:
        started=time.time()
        try: scan_once(ex)
        except KeyboardInterrupt: log("Stopped by user"); break
        except Exception as e: log(f"[LOOP ERROR] {e}")
        wait=max(60,SCAN_INTERVAL_MINUTES*60-int(time.time()-started)); log(f"Next scan in ~{wait//60} minutes"); time.sleep(wait)


if __name__=="__main__": main()
