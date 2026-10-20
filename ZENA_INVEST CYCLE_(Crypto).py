import os, time, math, logging, argparse, getpass, re
from io import StringIO
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import numpy as np
import pandas as pd

# ============================================================
# ZENA V3.6.3 — BTC/ETH BENCHMARK + TELEGRAM READABILITY + VERIFIED ON-CHAIN FLOW
# ============================================================
# ZENA WORK ORDER (urutan kerja wajib):
# 01 DATA INTEGRITY   -> apakah history + OHLCV + market data cukup?
# 02 UNIVERSE GATE     -> apakah coin layak dipertimbangkan untuk investasi?
# 03 LIQUIDITY         -> apakah volume cukup untuk DCA / exit realistis?
# 04 TOKENOMICS        -> FDV, circulating/total supply, unlock dilution risk.
# 05 4Y CYCLE          -> VWAP 4Y/3Y/2Y/1Y/180D/90D/60D/30D + cycle zone.
# 06 MTF MOMENTUM      -> 1W (macro) + 3D (confirmation) + 1D (entry).
#                         RSI14 + MACD(12,26,9) + TSI(25,13) + Stoch RSI.
# 07 RELATIVE STRENGTH -> performance coin vs BTC 30D/90D/180D/365D.
# 08 FLOW              -> CMF 180D/90D/60D/30D/20D/14D/10D/7D/3D/1D + OBV + volume behavior.
# 09 MACRO / ROTATION  -> informasi konteks saja; tidak masuk skor investasi.
# 10 RISK GATE         -> Event risk + hard filters + data gaps.
# 11 MARKET CAP        -> current tier, 4Y/3Y/2Y/1Y cap-migration proxy + skenario 5x/10x.
# 12 ON-CHAIN CHECK    -> public explorer data; transfer-flow proxy, not proof of buying intent.
# 13 RANK + VERDICT    -> CORE / SATELLITE / WATCHLIST / AVOID.
#
# PRINSIP:
# - Score tinggi tidak otomatis = BUY. Hard gate tetap menang.
# - Data yang hilang TIDAK dianggap sehat; diberi status DATA GAP / WATCHLIST.
# - Coin muda tidak dipaksa menjadi "4Y proven": dikategorikan SATELLITE.
# - VWAP panjang untuk konteks modal/cycle; VWAP 30D untuk timing.
# - Triple timeframe: 1W 50%, 3D 30%, 1D 20%.
# - BOT HANYA MENGIRIM SINYAL, TIDAK MELAKUKAN AUTO-TRADING.
# ============================================================

# Telegram configuration: mempertahankan konfigurasi Telegram lama agar file bisa langsung dijalankan.
# Jangan bagikan file ini karena token bot tertanam di dalamnya.
TELEGRAM_BOT_TOKEN = os.getenv("ZENA_TELEGRAM_BOT_TOKEN", "8979917342:AAETt_7wWXO_TzzYPVTLftW5-0W8MBlto2I").strip()
TELEGRAM_CHAT_ID = os.getenv("ZENA_TELEGRAM_CHAT_ID", "8049352232").strip()
TELEGRAM_SEND_DELAY = max(0.8, float(os.getenv("ZENA_TELEGRAM_DELAY", "1.1")))

# Universe dinamis dari semua market IDR Indodax. Portfolio diketahui dari percakapan:
# 8 simbol eksplisit; dua holding lain yang belum tersedia tetap bisa ditambahkan di sini.
BTC_ETH_EXCLUDE = {"BTC", "ETH"}
USER_HOLDINGS = {"SOL", "SUI", "RENDER", "FET", "AIOZ", "ONDO", "NEAR", "MON"}
STABLECOINS = {"USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDE", "USDD", "USDP", "PYUSD", "USTC", "UST", "IDRT", "USDS", "USD1"}
# Token beragunan emas tidak dihitung sebagai kandidat pemulihan kripto 5x-10x.
GOLD_BACKED_ASSETS = {"PAXG", "XAUT"}
MAX_ALL_COIN_SCAN = int(os.getenv("ZENA_MAX_ALL_COIN_SCAN", "450"))
# Diisi ulang dari tickers Indodax saat mulai scan; dipakai untuk lookup data pasar.
TARGET_COINS = ["BTC", "ETH", "SOL", "SUI", "RENDER", "FET", "AIOZ", "ONDO", "NEAR", "MON"]
CG_IDS = {
    "BTC":"bitcoin","ETH":"ethereum","SOL":"solana","SUI":"sui",
    "RENDER":"render-token","FET":"artificial-superintelligence-alliance",
    "AIOZ":"aioz-network","ONDO":"ondo-finance","NEAR":"near","MON":"monad"
}

# Isi manual HANYA dari sumber resmi. Critical/High dapat memblok BUY.
EVENT_RISK_OVERRIDES = {
    # "FET": {"level":"HIGH", "reason":"Verified security/bridge incident"},
}
# Persentase suplai yang diperkirakan unlock 365 hari ke depan.
# Isi manual bila punya data token-unlock yang terverifikasi.
UNLOCK_365D = {
    # "SUI": 12.5,
    # "ONDO": 20.0,
}

# ------------------------- INVESTMENT SCREEN CONFIG -------------------------
# Hard gates are screening defaults, not guarantees of safety or future return.
MIN_HISTORY_DAYS = 270          # Enough history to build a robust 1W MTF series.
CORE_HISTORY_DAYS = 730
FULL_CYCLE_HISTORY_DAYS = 1460
# Perintah kerja: batas WAJIB likuiditas di atas Rp100 juta/hari untuk kedua horizon.
MIN_MEDIAN_30D_QUOTE_IDR = 100_000_000
MIN_MEDIAN_90D_QUOTE_IDR = 100_000_000
MIN_CIRC_TOTAL_RATIO = 0.45
MAX_FDV_MCAP_MULTIPLE = 3.0
MAX_UNLOCK_365D_HARD = 20.0
MIN_MARKET_CAP_IDR = 1_000_000_000

# Optional manually verified historical peak market-cap references.
# Leave empty when you do not have a trustworthy source.
REFERENCE_PEAK_MKTCAP_IDR = {
    # Isi hanya dengan market cap historis yang benar-benar terverifikasi.
    # "SUI": 170_000_000_000_000,
    # "ONDO": 35_000_000_000_000,
}

# Snapshot MARKET CAP historis aktual (IDR) yang dapat diverifikasi.
# Key yang didukung: "4Y", "3Y", "2Y", "1Y", "180D", "90D", "30D".
# Jangan mengisi estimasi sebagai data aktual. Jika kosong, ZENA memakai
# proxy harga historis x circulating supply SAAT INI dan menandainya sebagai proxy.
REFERENCE_MKTCAP_HISTORY_IDR = {
    # Contoh format saja — bukan angka rekomendasi:
    # "SOL": {"4Y": 123_000_000_000, "2Y": 456_000_000_000, "1Y": 789_000_000_000},
}

# Kelas market cap indikatif dalam IDR (ambang pendekatan; FX dan definisi industri bervariasi).
# Micro < ~US$50m; Small ~US$50m–300m; Mid ~US$300m–2b; Large ~US$2b–10b; Mega >= ~US$10b.
MCAP_TIER_THRESHOLDS_IDR = [
    (800_000_000_000, "MICRO"),
    (4_800_000_000_000, "SMALL"),
    (32_000_000_000_000, "MID"),
    (160_000_000_000_000, "LARGE"),
    (float("inf"), "MEGA"),
]

H = {
    "User-Agent": "ZENA-V3/3.5",
    "Accept": "application/json,text/plain,*/*",
    "Connection": "keep-alive",
}

# Optional keys. Scanner tetap berjalan tanpa key; data makro yang tidak tersedia
# ditampilkan sebagai N/A, bukan angka nol yang seolah-olah merupakan data aktual.
# FRED_API_KEY mengaktifkan Web Service FRED; CSV publik tetap dicoba tanpa key.
CG_DEMO_API_KEY = os.getenv("CG_DEMO_API_KEY", "").strip()
FRED_API_KEY = os.getenv("FRED_API_KEY", "").strip()
# Optional: set FRED_API_KEY in Windows to use the official FRED Web Service; public CSV fallback is also attempted.

# Timeout lebih pendek agar satu provider bermasalah tidak membuat scanner
# tertahan sangat lama. Bisa dioverride dengan environment variable.
TIMEOUT = max(5, int(os.getenv("ZENA_HTTP_TIMEOUT", "10")))

S = requests.Session()
S.headers.update(H)

# Session ringan tanpa retry untuk fallback yang sifatnya opsional.
# Tujuannya mencegah satu provider down membuat scanner menunggu terlalu lama.
DIRECT = requests.Session()
DIRECT.headers.update(H)

# Retry hanya untuk error jaringan / 429 / 5xx. Jangan retry 4xx seperti 404
# karena itu biasanya masalah endpoint, bukan masalah sementara.
_retry = Retry(
    total=1,
    connect=1,
    read=1,
    status=1,
    backoff_factor=0.5,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET", "POST"]),
    raise_on_status=False,
)
S.mount("https://", HTTPAdapter(max_retries=_retry))

if CG_DEMO_API_KEY:
    S.headers.update({"x-cg-demo-api-key": CG_DEMO_API_KEY})

# Binance menyediakan host market-data publik alternatif.
# data-api.binance.vision ditujukan untuk endpoint tanpa autentikasi.
BINANCE_BASES = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("ZENA")

URL = {
    "indo_ticker":"https://indodax.com/api/tickers",
    "indo_hist":"https://indodax.com/tradingview/history_v2",
    "cg_global":"https://api.coingecko.com/api/v3/global",
    "cg_markets":"https://api.coingecko.com/api/v3/coins/markets",
    "cg_coin":"https://api.coingecko.com/api/v3/coins/{id}",
    "cp_global":"https://api.coinpaprika.com/v1/global",
    "fng":"https://api.alternative.me/fng/?limit=1&format=json",
    # Primary official DefiLlama stablecoin chart route; fallback route tried below.
    "stable":"https://stablecoins.llama.fi/stablecoincharts/all",
    "dex":"https://api.llama.fi/overview/dexs",
    "fred":"https://fred.stlouisfed.org/graph/fredgraph.csv",
    "fred_api":"https://api.stlouisfed.org/fred/series/observations",
    "yahoo":"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
    "dex_search":"https://api.dexscreener.com/latest/dex/search",
}

# FREE-FIRST ON-CHAIN CHECK. No paid key is required. Data coverage is intentionally
# conservative: only public Blockscout instances are queried; other chains show N/A.
# IMPORTANT: address holder flows are NOT proof that wallets bought tokens or intend to hold.
ONCHAIN_ENABLED = os.getenv("ZENA_ONCHAIN_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
ONCHAIN_MAX_TRANSFER_PAGES = max(1, min(5, int(os.getenv("ZENA_ONCHAIN_MAX_PAGES", "3"))))
ONCHAIN_HTTP_TIMEOUT = max(4, min(12, int(os.getenv("ZENA_ONCHAIN_TIMEOUT", "7"))))
ONCHAIN_TOP_HOLDER_COHORT = 10
ONCHAIN_TOKEN_CACHE = {}

# Public EVM explorer -> Blockscout API v2 endpoints for supported contract chains.
# Coverage is partial by design; Solana and native non-EVM assets are not guessed.
ONCHAIN_BLOCKSCOUT = {
    "ethereum": "https://eth.blockscout.com/api/v2",
    "base": "https://base.blockscout.com/api/v2",
    "arbitrum": "https://arbitrum.blockscout.com/api/v2",
    "optimism": "https://optimism.blockscout.com/api/v2",
    "polygon": "https://polygon.blockscout.com/api/v2",
    "gnosis": "https://gnosis.blockscout.com/api/v2",
    "avalanche": "https://avalanche.blockscout.com/api/v2",
    "celo": "https://celo.blockscout.com/api/v2",
    "linea": "https://explorer.linea.build/api/v2",
    "scroll": "https://scrollscan.com/api/v2",
}

# Contract addresses below are pinned from public token/project references. For symbols
# not in this list, a cautious fallback may resolve the exact CoinGecko coin ID from the
# already-loaded market row, retrieve its platform contract map, and require the public
# explorer token symbol to match before using holder-flow data. This is not proof of
# project ownership; unsupported/native chains remain N/A and ticker-only guessing is banned.
ONCHAIN_VERIFIED_CONTRACTS = {
    "PEPE": {"chain": "ethereum", "contract": "0x6982508145454Ce325dDbE47a25d4ec3d2311933", "token_name": "Pepe", "source_url": "https://www.coingecko.com/en/coins/pepe", "scope": "Ethereum ERC-20 only"},
    "POL":  {"chain": "ethereum", "contract": "0x455e53CBB86018Ac2B8092FdCD39d8444aFFC3F6", "token_name": "Polygon Ecosystem Token", "source_url": "https://support.polygon.technology/support/solutions/articles/82000906989-information-about-pol-contract-and-migration", "scope": "Ethereum ERC-20 only; excludes native Polygon PoS balances"},
    "PAXG": {"chain": "ethereum", "contract": "0x45804880De22913dAFE09f4980848ECE6EcbAf78", "token_name": "PAX Gold", "source_url": "https://docs.paxos.com/guides/stablecoin/paxg", "scope": "Ethereum ERC-20 only"},
    "XAUT": {"chain": "ethereum", "contract": "0x68749665FF8D2d112Fa859AA293F07A622782F38", "token_name": "Tether Gold", "source_url": "https://tether.to/en/supported-protocols/", "scope": "Ethereum ERC-20 only; excludes other chain representations"},
    "AIOZ": {"chain": "ethereum", "contract": "0x626E8036dEB333b408Be468F951bdB42433cBF18", "token_name": "AIOZ Network", "source_url": "https://aioz.network/blog/ready-for-lift-off-the-aioz-network-starts-in-one-hour", "scope": "Ethereum ERC-20 only; native AIOZ chain is not included"},
    "ONDO": {"chain": "ethereum", "contract": "0xfAbA6f8e4a5E8Ab82F62fe7C39859FA577269be3", "token_name": "Ondo", "source_url": "https://www.coingecko.com/en/coins/ondo", "scope": "Ethereum ERC-20 only"},
    "FET":  {"chain": "ethereum", "contract": "0xaea46A60368A7bD060eec7DF8CBa43b7EF41Ad85", "token_name": "Fetch", "source_url": "https://network.fetch.ai/docs/introduction/native-and-erc-fet", "scope": "Ethereum ERC-20 only; native Fetch.ai network is not included"},
}

# Address labels are provider metadata only; unmatched/unlabelled wallets remain unknown.
ONCHAIN_EXCLUDED_LABELS = re.compile(
    r"\b(binance|coinbase|okx|bybit|kraken|kucoin|gate\.io|mexc|bitget|crypto\.com|"
    r"uniswap|pancakeswap|swap|liquidity pool|\bpool\b|bridge|burn|dead address|"
    r"staking contract|treasury wallet)\b", re.IGNORECASE
)


def get_json(url, params=None, quiet_401=False, quiet=False):
    try:
        r=S.get(url, params=params, timeout=TIMEOUT)
        if r.status_code == 401 and quiet_401:
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        if not quiet and not (quiet_401 and "401 Client Error" in str(e)):
            log.warning("GET failed %s: %s", url, e)
        return None

def get_json_from_bases(path, params=None):
    """Try public Binance market-data hosts in order."""
    last_error = None
    for base in BINANCE_BASES:
        url = base.rstrip("/") + path
        try:
            r = DIRECT.get(url, params=params, timeout=min(TIMEOUT, 8))
            r.raise_for_status()
            return r.json(), base
        except Exception as e:
            last_error = e
            continue
    log.warning("Binance unavailable on all public endpoints: %s", last_error)
    return None, None

def parse_mixed_datetime(series):
    """Parse ISO strings and Unix timestamps, even when both appear in one column."""
    s = series.copy()
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns, UTC]")
    nums = pd.to_numeric(s, errors="coerce")
    num_mask = nums.notna()
    if num_mask.any():
        med = nums[num_mask].abs().median()
        unit = "ms" if med > 10**11 else "s"
        out.loc[num_mask] = pd.to_datetime(nums.loc[num_mask], unit=unit, errors="coerce", utc=True)
    text_mask = ~num_mask
    if text_mask.any():
        out.loc[text_mask] = pd.to_datetime(s.loc[text_mask], format="ISO8601", errors="coerce", utc=True)
    return out

def get_csv(url, params=None, quiet=False):
    try:
        r=DIRECT.get(url, params=params, timeout=min(TIMEOUT, 8))
        r.raise_for_status()
        return pd.read_csv(StringIO(r.text))
    except Exception as e:
        if not quiet:
            log.warning("CSV failed %s: %s", url, e)
        return None

def f(x, default=0.0):
    try:
        if x is None: return default
        y=float(x); return default if not np.isfinite(y) else y
    except Exception: return default

def clamp(x,a=0,b=100): return max(a,min(b,x))
def pc(new,old): return None if old in (None,0) else (new/old-1)*100

def idr(x):
    """Format IDR without rounding sub-1-IDR token prices/VWAP to Rp 0."""
    value = f(x, 0.0)
    if value <= 0:
        return "-"
    if value < 0.0001:
        body = f"{value:,.8f}".rstrip("0").rstrip(".")
    elif value < 0.01:
        body = f"{value:,.6f}".rstrip("0").rstrip(".")
    elif value < 1:
        body = f"{value:,.5f}".rstrip("0").rstrip(".")
    elif value < 10:
        body = f"{value:,.3f}".rstrip("0").rstrip(".")
    elif value < 1000:
        body = f"{value:,.2f}".rstrip("0").rstrip(".")
    else:
        body = f"{value:,.0f}"
    return f"Rp {body}"

def idr_compact(x):
    """Display large IDR market caps in a compact Indonesian number format."""
    value = f(x, 0.0)
    if value <= 0:
        return "N/A"
    if value >= 1e15:
        return f"Rp {value / 1e15:.2f} kuadriliun"
    if value >= 1e12:
        return f"Rp {value / 1e12:.2f} triliun"
    if value >= 1e9:
        return f"Rp {value / 1e9:.2f} miliar"
    if value >= 1e6:
        return f"Rp {value / 1e6:.2f} juta"
    return idr(value)

def usd(x):
    if x<=0: return "-"
    if x>=1e12: return f"${x/1e12:.2f}T"
    if x>=1e9: return f"${x/1e9:.2f}B"
    if x>=1e6: return f"${x/1e6:.2f}M"
    return f"${x:,.0f}"

def _telegram_configured():
    bad = {"", "PASTE_NEW_BOT_TOKEN_HERE", "PASTE_CHAT_ID_HERE"}
    return TELEGRAM_BOT_TOKEN not in bad and TELEGRAM_CHAT_ID not in bad

def configure_telegram_interactive():
    """Ask for Telegram credentials once when they are missing and stdin is interactive."""
    global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    if _telegram_configured():
        return True
    try:
        if not getattr(__import__("sys"), "stdin").isatty():
            return False
        print("\n=== ZENA TELEGRAM SETUP ===")
        print("Telegram belum dikonfigurasi. Masukkan token bot yang ingin digunakan (token lama juga bisa jika masih aktif).")
        token = getpass.getpass("Token bot Telegram (input disembunyikan): ").strip()
        chat_id = input("Chat ID Telegram: ").strip()
        if token:
            TELEGRAM_BOT_TOKEN = token
        if chat_id:
            TELEGRAM_CHAT_ID = chat_id
        return _telegram_configured()
    except (EOFError, KeyboardInterrupt):
        print("\nTelegram setup dibatalkan.")
        return False

def telegram_api(method, payload=None, timeout=None):
    """Call Telegram Bot API without ever exposing the bot token in logs."""
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "PASTE_NEW_BOT_TOKEN_HERE":
        return None, "BOT_TOKEN belum diisi."
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        r = S.post(url, json=payload or {}, timeout=timeout or TIMEOUT)
        try:
            data = r.json()
        except Exception:
            data = {}
        if r.ok and data.get("ok"):
            return data, None
        desc = data.get("description") or r.text or f"HTTP {r.status_code}"
        return data, desc
    except requests.RequestException as e:
        # Do NOT include the request URL here because it contains the bot token.
        return None, f"NETWORK ERROR: {type(e).__name__}: koneksi ke Telegram gagal."


def telegram_check(send_test=False):
    """Validate bot token and chat; optionally send a real test message."""
    if not _telegram_configured():
        return False, "Credential Telegram belum diisi."

    me, err = telegram_api("getMe")
    if err:
        return False, f"Token/Bot gagal divalidasi: {err}"

    bot_user = me.get("result",{}).get("username", "unknown")
    chat, chat_err = telegram_api("getChat", {"chat_id": TELEGRAM_CHAT_ID})
    if chat_err:
        # getChat is useful, but sendMessage is the authoritative test.
        # When --test-telegram is requested, try the actual send even if getChat
        # is unavailable, so we get the real Telegram error for this chat.
        if send_test:
            test_text = f"✅ ZENA Telegram test — @{bot_user}\nChat ID: {TELEGRAM_CHAT_ID}"
            sent, send_err = telegram_api("sendMessage", {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": test_text,
                "disable_web_page_preview": True,
            })
            if send_err:
                return False, f"Token valid (@{bot_user}), tetapi sendMessage gagal: {send_err}"
            return True, f"Telegram test berhasil: @{bot_user} → chat {TELEGRAM_CHAT_ID}"
        return False, f"Token valid (@{bot_user}), tetapi Chat ID gagal divalidasi: {chat_err}"

    chat_title = chat.get("result",{}).get("title") or chat.get("result",{}).get("first_name") or TELEGRAM_CHAT_ID
    if send_test:
        test_text = f"✅ ZENA Telegram test OK — @{bot_user}\nChat: {chat_title}"
        sent, err = telegram_api("sendMessage", {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": test_text,
            "disable_web_page_preview": True,
        })
        if err:
            return False, f"Token valid (@{bot_user}), chat valid, tetapi sendMessage gagal: {err}"
    return True, f"Telegram siap: @{bot_user} → {chat_title}"


def tg(msg, label="laporan"):
    # Telegram tidak mendukung warna font arbitrer; gunakan bold + penanda emoji 🟡/🔵, tanpa blok latar.
    """Kirim seluruh pesan ke Telegram dalam beberapa bagian, tanpa memotong laporan."""
    if not _telegram_configured():
        log.error("Telegram tidak dikirim: credential belum diisi.")
        print("\n--- TELEGRAM PREVIEW / NOT SENT ---\n" + str(msg) + "\n--- END ---\n")
        return False
    raw = str(msg)
    # Pecah di batas baris agar tidak terpotong batas 4096 karakter Telegram.
    chunks, current = [], ""
    for line in raw.splitlines() or [raw]:
        if len(line) > 3400:
            if current:
                chunks.append(current); current = ""
            for i in range(0, len(line), 3300): chunks.append(line[i:i+3300])
            continue
        candidate = line if not current else current + "\n" + line
        if len(candidate) > 3400:
            chunks.append(current); current = line
        else:
            current = candidate
    if current: chunks.append(current)
    ok_all = True
    for idx, chunk in enumerate(chunks, 1):
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": chunk,
                   "parse_mode": "Markdown", "disable_web_page_preview": True}
        _, err = telegram_api("sendMessage", payload)
        if err:
            low = err.lower()
            # Fallback plain text untuk masalah Markdown.
            if any(k in low for k in ("parse entities", "can't parse", "entities", "markdown")):
                _, err = telegram_api("sendMessage", {"chat_id": TELEGRAM_CHAT_ID,
                    "text": chunk, "disable_web_page_preview": True})
        if err:
            time.sleep(1.2)
            _, err = telegram_api("sendMessage", {"chat_id": TELEGRAM_CHAT_ID,
                "text": chunk, "disable_web_page_preview": True})
        if err:
            log.error("Telegram gagal untuk %s (bagian %s/%s): %s", label, idx, len(chunks), err)
            ok_all = False
        elif len(chunks) > 1:
            log.info("Telegram terkirim %s bagian %s/%s.", label, idx, len(chunks))
        if idx < len(chunks): time.sleep(TELEGRAM_SEND_DELAY)
    return ok_all

# ------------------------- PRICE / CANDLE -------------------------
def indodax_tickers():
    d=get_json(URL["indo_ticker"]); return d.get("tickers",{}) if isinstance(d,dict) else {}

# History depth is sized for the longest requested rolling VWAP (4Y ~ 1460D)
# plus warm-up room for higher-timeframe RSI/MACD/TSI calculations.
HISTORY_DAYS = 1800

def indodax_hist(sym, days=HISTORY_DAYS):
    """Fetch daily OHLCV from Indodax's public TradingView history endpoint.

    Important: Indodax expects `tf`, not `resolution`, and the documented
    response is a list of candle objects (Time/Open/High/Low/Close/Volume).
    The old script used the TradingView-style `resolution` + dict response,
    which causes HTTP 400 / empty history.
    """
    now=int(time.time()); start=now-days*86400
    code=f"{sym.upper()}IDR"
    params={"symbol":code,"tf":"1D","from":start,"to":now}
    d=get_json(URL["indo_hist"],params,quiet=True)

    if isinstance(d,list):
        rows=d
    elif isinstance(d,dict):
        # Backward compatibility with a TradingView-like response if Indodax
        # ever returns that schema again.
        if d.get("s") not in (None,"ok"):
            return pd.DataFrame()
        rows=[]
        ts=d.get("t",[])
        oo=d.get("o",[]); hh=d.get("h",[]); ll=d.get("l",[])
        cc=d.get("c",[]); vv=d.get("v",[])
        for i in range(min(len(ts),len(oo),len(hh),len(ll),len(cc),len(vv))):
            rows.append({"Time":ts[i],"Open":oo[i],"High":hh[i],"Low":ll[i],"Close":cc[i],"Volume":vv[i]})
    else:
        rows=[]

    # Preserve even short histories: newer listings must still be screened and
    # reported as DATA GAP / insufficient history rather than disappearing.
    if not rows:
        return pd.DataFrame()

    try:
        x=pd.DataFrame(rows)
        rename={
            "Time":"time","time":"time",
            "Open":"open","open":"open",
            "High":"high","high":"high",
            "Low":"low","low":"low",
            "Close":"close","close":"close",
            "Volume":"volume","volume":"volume",
        }
        x=x.rename(columns={k:v for k,v in rename.items() if k in x.columns})
        needed=["time","open","high","low","close","volume"]
        if not all(c in x.columns for c in needed):
            return pd.DataFrame()
        for c in needed[1:]:
            x[c]=pd.to_numeric(x[c],errors="coerce")
        # Indodax documents Unix timestamps in seconds.
        x["time"]=pd.to_datetime(pd.to_numeric(x["time"],errors="coerce"),unit="s",utc=True,errors="coerce")
        return x.dropna(subset=["time","close"]).sort_values("time").reset_index(drop=True)
    except Exception as e:
        log.warning("Indodax history parse failed for %s: %s",sym,e)
        return pd.DataFrame()

def binance_hist(sym, usdt_idr, days=HISTORY_DAYS):
    if sym in {"AIOZ","MON"}: return pd.DataFrame()

    # Binance public klines are limited to 1000 rows per request. Paginate
    # backwards so 4Y rolling VWAPs can still be calculated on the fallback.
    target=max(30, int(days)); rows=[]; end_ms=None
    while len(rows) < target:
        params={"symbol":f"{sym}USDT","interval":"1d","limit":1000}
        if end_ms is not None:
            params["endTime"]=end_ms
        d, _ = get_json_from_bases("/api/v3/klines", params)
        if not isinstance(d,list) or not d:
            break
        rows=d+rows
        first_open=int(d[0][0]); next_end=first_open-1
        if end_ms is not None and next_end >= end_ms:
            break
        end_ms=next_end
        if len(d)<1000:
            break
    if len(rows)<20: return pd.DataFrame()
    rows=rows[-target:]
    cols=["time","open","high","low","close","volume","ct","qav","tr","tbv","tqv","i"]
    x=pd.DataFrame(rows,columns=cols)
    x["time"]=pd.to_datetime(x["time"],unit="ms",utc=True)
    for c in ["open","high","low","close","volume"]:
        x[c]=pd.to_numeric(x[c],errors="coerce")
    for c in ["open","high","low","close"]:
        x[c]*=usdt_idr
    return x[["time","open","high","low","close","volume"]].dropna().drop_duplicates("time").sort_values("time").reset_index(drop=True)
def binance_price(symbol):
    """Fetch cross-pair prices from a real cross-pair market, never an IDR ticker.

    ETH/BTC is a BTC-denominated exchange rate. Try Binance public market data,
    then CoinGecko. Missing/invalid quotes return None (not 0.0).
    """
    target = str(symbol).upper().replace("_", "").replace("/", "")
    if not target:
        return None

    data, _ = get_json_from_bases("/api/v3/ticker/price", {"symbol": target})
    if isinstance(data, dict):
        value = f(data.get("price"), None)
        if value is not None and np.isfinite(value) and value > 0:
            # Reject implausible values for ETH/BTC instead of scoring bad data.
            if target != "ETHBTC" or 0.001 <= value <= 0.2:
                return float(value)

    if target == "ETHBTC":
        d = get_json(
            "https://api.coingecko.com/api/v3/simple/price",
            {"ids": "ethereum", "vs_currencies": "btc"},
            quiet_401=True, quiet=True
        )
        try:
            value = f(d["ethereum"]["btc"], None)
            if value is not None and np.isfinite(value) and 0.001 <= value <= 0.2:
                return float(value)
        except (TypeError, KeyError):
            pass
    return None

def binance_ethbtc_metrics():
    """Fetch genuine ETHBTC cross-market quote and 30D trend from Binance public market data."""
    kline, base = get_json_from_bases("/api/v3/klines", {"symbol":"ETHBTC", "interval":"1d", "limit":40})
    if isinstance(kline, list) and len(kline) >= 2:
        try:
            closes = [float(row[4]) for row in kline if len(row) > 4 and float(row[4]) > 0]
            latest = closes[-1] if closes else None
            prior = closes[-31] if len(closes) >= 31 else None
            # pc() already returns a percentage (e.g. -5.0 means -5%).
            # Do NOT multiply by 100 again; that caused values like -505.65%.
            change30 = pc(latest, prior) if latest is not None and prior not in (None, 0) else None
            # A simple price return cannot be below -100%; reject malformed data.
            if change30 is not None and (not np.isfinite(change30) or change30 < -100 or change30 > 1000):
                change30 = None
            return {"price":latest, "change30":change30, "source":base or "Binance", "date":pd.to_datetime(int(kline[-1][0]), unit="ms", utc=True).strftime("%Y-%m-%d")}
        except (TypeError, ValueError, IndexError):
            pass
    # Ticker endpoint provides a current spot only; trend is deliberately left N/A.
    ticker, base = get_json_from_bases("/api/v3/ticker/price", {"symbol":"ETHBTC"})
    try:
        price = float(ticker.get("price")) if isinstance(ticker, dict) else None
        if price is not None and price > 0:
            return {"price":price, "change30":None, "source":base or "Binance", "date":pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")}
    except (TypeError, ValueError):
        pass
    return {"price":None, "change30":None, "source":"N/A", "date":None}


def stablecoin_history():
    """Try current DefiLlama routes; return validated payload and source URL, never cached stale data."""
    urls = [
        URL["stable"],
        "https://stablecoins.llama.fi/stablecoins/stablecoincharts/all",
    ]
    for url in urls:
        data = get_json(url, quiet=True)
        rows = data if isinstance(data, list) else (data.get("data") if isinstance(data, dict) else None)
        if isinstance(rows, list) and rows:
            if any(isinstance(r, dict) and ("date" in r or "timestamp" in r) for r in rows):
                return rows, url
    log.warning("Stablecoin data tidak tersedia dari endpoint DefiLlama yang dicoba; nilai diset N/A.")
    return [], "N/A"


def history(sym,usdt_idr=None):
    """Use Indodax IDR candles consistently for every listed IDR market.

    Do not silently substitute Binance candles: unsupported Binance symbols and
    TLS/proxy failures should not interrupt the scan or mix market data sources.
    If Indodax has no usable history, the caller still screens the coin using
    its live Indodax ticker and records a DATA GAP result.
    """
    x=indodax_hist(sym)
    if x is None or x.empty:
        return pd.DataFrame(columns=["time","open","high","low","close","volume"]), "Indodax IDR (riwayat tidak tersedia)"
    return x,"Indodax IDR"


def indodax_ticker_entry(tickers, sym):
    """Resolve a ticker entry despite Indodax key spelling variations."""
    if not isinstance(tickers, dict):
        return {}
    candidates=(f"{sym.lower()}_idr", f"{sym.lower()}idr", sym.lower())
    for key in candidates:
        value=tickers.get(key)
        if isinstance(value,dict):
            return value
    target=sym.upper()
    for key,value in tickers.items():
        if not isinstance(value,dict):
            continue
        normalized=str(key).lower().replace("_", "")
        if normalized == f"{target.lower()}idr":
            return value
    return {}

# ------------------------- INDICATORS -------------------------
def ema(s,n): return s.ewm(span=n,adjust=False).mean()
def sma(s,n): return s.rolling(n).mean()
def RSI(s,n=14):
    d=s.diff(); g=d.clip(lower=0); l=-d.clip(upper=0)
    ag=g.ewm(alpha=1/n,adjust=False).mean(); al=l.ewm(alpha=1/n,adjust=False).mean()
    return (100-100/(1+ag/al.replace(0,np.nan))).fillna(50)
def MACD(s):
    m=ema(s,12)-ema(s,26); sig=ema(m,9); return m,sig,m-sig
def CMF(x,n=20):
    rng=(x.high-x.low).replace(0,np.nan)
    mult=((x.close-x.low)-(x.high-x.close))/rng
    return (mult.fillna(0)*x.volume).rolling(n).sum()/x.volume.rolling(n).sum().replace(0,np.nan)


# ADDITIVE WORK ORDER: CMF multi-periode berbasis candle harian.
# Ini hanya menambah informasi flow; bobot skor utama ZENA tetap dipertahankan.
CMF_PERIODS = {"180D":180, "90D":90, "60D":60, "30D":30, "20D":20,
               "14D":14, "10D":10, "7D":7, "3D":3, "1D":1}


def cmf_multi_metrics(x):
    """Hitung CMF semua horizon dari OHLCV harian; data kurang -> None/N/A."""
    values = {}
    if x is None or x.empty:
        return {k: None for k in CMF_PERIODS}
    clean = x.copy()
    for col in ("high", "low", "close", "volume"):
        clean[col] = pd.to_numeric(clean[col], errors="coerce")
    clean = clean.replace([np.inf, -np.inf], np.nan).dropna(subset=["high", "low", "close", "volume"])
    clean = clean[(clean["volume"] >= 0) & (clean["high"] >= clean["low"])]
    for label, period in CMF_PERIODS.items():
        if len(clean) < period:
            values[label] = None
            continue
        try:
            val = CMF(clean, period).iloc[-1]
            values[label] = float(val) if pd.notna(val) and np.isfinite(val) else None
        except (ValueError, TypeError, IndexError, ZeroDivisionError):
            values[label] = None
    return values


def cmf_pattern(x, cmfs, tech_data):
    """Interpretasi hati-hati atas pola CMF multi-horizon; bukan bukti aktivitas bandar."""
    long_vals = [cmfs.get(k) for k in ("180D", "90D") if cmfs.get(k) is not None]
    mid_vals = [cmfs.get(k) for k in ("60D", "30D", "20D") if cmfs.get(k) is not None]
    short_vals = [cmfs.get(k) for k in ("14D", "10D", "7D") if cmfs.get(k) is not None]
    very_short = [cmfs.get(k) for k in ("3D", "1D") if cmfs.get(k) is not None]
    groups = {"long": (sum(long_vals)/len(long_vals) if long_vals else None),
              "medium": (sum(mid_vals)/len(mid_vals) if mid_vals else None),
              "short": (sum(short_vals)/len(short_vals) if short_vals else None),
              "very_short": (sum(very_short)/len(very_short) if very_short else None)}
    available = sum(v is not None for v in cmfs.values())
    obv = tech_data.get("obv_slope", 0)
    price, sma20 = tech_data.get("p", 0), tech_data.get("s20", 0)
    near_base = bool(price > 0 and sma20 > 0 and price <= sma20 * 1.10)
    long_v, mid_v, short_v = groups["long"], groups["medium"], groups["short"]
    vshort_v = groups["very_short"]
    if available < 4 or long_v is None or mid_v is None or short_v is None:
        label = "FLOW BELUM JELAS"
        note = "Data CMF belum cukup untuk membaca pola lintas waktu."
    elif long_v <= -0.05 and mid_v <= -0.03 and short_v < 0 and (obv < 0 or (price and sma20 and price < sma20)):
        label = "POTENSI DISTRIBUSI"
        note = "Flow beberapa horizon melemah; waspadai tekanan jual dan struktur harga."
    elif long_v > 0 and mid_v >= 0 and short_v > 0 and (vshort_v is None or vshort_v >= -0.02) and obv >= 0 and near_base:
        label = "POTENSI AKUMULASI"
        note = "Flow lintas waktu cenderung mendukung, dengan harga relatif dekat basis. Belum merupakan sinyal beli otomatis."
    elif long_v > 0 and short_v < -0.05 and vshort_v is not None and vshort_v > short_v and price > 0 and sma20 > 0 and price >= sma20 * 0.97:
        label = "POTENSI SHAKEOUT — BUTUH KONFIRMASI"
        note = "Flow panjang masih positif, flow pendek melemah lalu membaik; tunggu konfirmasi support dan volume."
    elif long_v < 0 and short_v is not None and vshort_v is not None and vshort_v > short_v:
        label = "POTENSI PEMULIHAN AWAL — BELUM TENTU AKUMULASI"
        note = "Flow pendek membaik saat flow panjang masih negatif; tunggu konfirmasi tambahan."
    elif long_v > 0 and short_v < 0:
        label = "JANGKA PANJANG POSITIF, FLOW PENDEK MELEMAH"
        note = "Jangan langsung menyimpulkan akumulasi/shakeout; pantau support, OBV, dan volume."
    elif price > 0 and sma20 > 0 and price > sma20 and mid_v < 0 and short_v < 0 and obv < 0:
        label = "KENAIKAN BELUM TERKONFIRMASI — WASPADAI DISTRIBUSI"
        note = "Harga naik tanpa dukungan flow/OBV yang cukup; hindari mengejar kenaikan."
    else:
        label = "FLOW BELUM JELAS"
        note = "Sinyal antarkala waktu belum cukup selaras untuk klasifikasi yang lebih tegas."
    fmt = lambda z: f"{z:+.3f}" if z is not None else "N/A"
    # Kesimpulan lintas SEMUA horizon CMF yang tersedia, bukan CMF20 saja.
    all_values = [v for v in cmfs.values() if v is not None]
    pos_count = sum(v > 0.05 for v in all_values)
    neg_count = sum(v < -0.05 for v in all_values)
    neutral_count = len(all_values) - pos_count - neg_count
    long_positive = long_v is not None and long_v > 0.05
    short_negative = (short_v is not None and short_v < -0.05) or (vshort_v is not None and vshort_v < -0.05)
    short_improving = (short_v is not None and vshort_v is not None and vshort_v > short_v)
    if long_positive and short_negative and short_improving:
        conclusion = "POTENSI SHAKEOUT — flow panjang positif, flow pendek melemah lalu membaik; wajib konfirmasi support."
    elif pos_count >= max(1, int(len(all_values) * 0.65)) and long_positive:
        conclusion = "AKUMULASI LEBIH DOMINAN — mayoritas horizon CMF positif. Bukan sinyal beli otomatis."
    elif neg_count >= max(1, int(len(all_values) * 0.60)):
        conclusion = "DISTRIBUSI LEBIH DOMINAN — mayoritas horizon CMF negatif; waspadai tekanan jual."
    elif long_positive and short_negative:
        conclusion = "FLOW CAMPURAN — jangka panjang positif, pendek negatif; belum cukup memastikan shakeout."
    else:
        conclusion = "BELUM KONKLUSIF — CMF antarhorizon campuran/netral atau data belum cukup."
    summary = (f"Panjang {fmt(groups['long'])} | Menengah {fmt(groups['medium'])} | "
               f"Pendek {fmt(groups['short'])} | Sangat pendek {fmt(groups['very_short'])} | "
               f"CMF +{pos_count}/-{neg_count}/netral {neutral_count}")
    return {"label": label, "note": note, "groups": groups, "summary": summary,
            "available": available, "conclusion": conclusion,
            "positive_count": pos_count, "negative_count": neg_count, "neutral_count": neutral_count}


def coin_status_labels(result):
    """Label keputusan yang tidak melonggarkan hard gate, likuiditas, atau kualitas data."""
    cyc = result.get("cycle", {})
    gate = result.get("gate", {})
    liq = result.get("liquidity", {})
    labels = []
    liquidity_pass = (liq.get("median30", 0) > MIN_MEDIAN_30D_QUOTE_IDR and
                      liq.get("median90", 0) > MIN_MEDIAN_90D_QUOTE_IDR)
    data_ok = not gate.get("hard_fail") and not gate.get("data_gap") and liquidity_pass
    # Filter awal cycle hanya diberikan jika skor cycle + VWAP jangka panjang selaras,
    # dan semua hard gate/kualitas data tetap lolos.
    if (data_ok and cyc.get("score", 0) >= 11 and cyc.get("long_align", 0) >= 3):
        labels.append("CALON CYCLE KUAT")
    low_nominal_price = 0 < result.get("price", 0) <= 1000
    flow_label = str(result.get("flow_pattern", {}).get("label", "FLOW BELUM JELAS")).upper()
    weak_flow = any(term in flow_label for term in (
        "POTENSI DISTRIBUSI", "FLOW PENDEK MELEMAH", "KENAIKAN BELUM TERKONFIRMASI"))
    weak_cycle = cyc.get("score", 0) < 9 or cyc.get("long_align", 0) < 2
    poor_cycle_investment = bool(not data_ok or result.get("score", 0) < 55 or weak_cycle or weak_flow)
    # Harga murah nominal bukan bukti tunggal; harus ada kelemahan investasi siklus.
    if low_nominal_price and poor_cycle_investment:
        labels.append("KOIN MURAHAN")
    if not data_ok or result.get("score", 0) < 55:
        labels.append("BELUM SIAP")
    if not labels:
        labels.append("PANTAU")
    if result.get("ver", "").startswith(("🟢 DCA", "🟡 DCA MIKRO", "🟡 CALON DCA")):
        labels.insert(0, "CALON DCA PRA-REVERSAL")
    return labels

def OBV(x): return (np.sign(x.close.diff()).fillna(0)*x.volume).cumsum()
def TSI(s,long=25,short=13):
    m=s.diff(); a=m.abs(); return ema(ema(m,long),short)/ema(ema(a,long),short).replace(0,np.nan)
def StochRSI(s, rsi_n=14, stoch_n=14, k_n=3, d_n=3):
    r=RSI(s,rsi_n); lo=r.rolling(stoch_n).min(); hi=r.rolling(stoch_n).max()
    raw=(r-lo)/(hi-lo).replace(0,np.nan)*100; k=sma(raw,k_n); d=sma(k,d_n)
    return raw,k,d

def resample_ohlcv(x, rule):
    if x.empty: return pd.DataFrame()
    y=x.set_index("time").sort_index()
    out=y.resample(rule, label="right", closed="right").agg({
        "open":"first","high":"max","low":"min","close":"last","volume":"sum"
    }).dropna(subset=["open","high","low","close"]).reset_index()
    return out

def timeframe_indicators(x):
    """RSI14 + MACD(12,26,9) + TSI(25,13) + StochRSI on 1D/3D/1W."""
    if x.empty: return {}
    frames={"1D":x,"3D":resample_ohlcv(x,"3D"),"1W":resample_ohlcv(x,"W-SUN")}
    out={}
    for tf,df in frames.items():
        if len(df)<35: continue
        c=df["close"]; macd,macds,mh=MACD(c); _,sk,sd=StochRSI(c)
        out[tf]={"rsi":f(RSI(c).iloc[-1],50),"macd":f(macd.iloc[-1]),"macds":f(macds.iloc[-1]),
                 "mh":f(mh.iloc[-1]),"tsi":f(TSI(c).iloc[-1]),"stoch_k":f(sk.iloc[-1],50),
                 "stoch_d":f(sd.iloc[-1],50),"price":f(df.iloc[-1].close),"bars":len(df)}
    return out

def tech(x):
    if x.empty or len(x)<30:return {}
    x=x.copy(); c=x.close
    x["s20"]=sma(c,20); x["s50"]=sma(c,50); x["s100"]=sma(c,100); x["s200"]=sma(c,200)
    x["vs9"]=sma(x.volume,9); x["rsi"]=RSI(c); x["macd"],x["macds"],x["mh"]=MACD(c)
    x["cmf"]=CMF(x); x["obv"]=OBV(x); x["tsi"]=TSI(c)
    z=x.iloc[-1]; mtf=timeframe_indicators(x)
    return {"p":f(z.close),"s20":f(z.s20),"s50":f(z.s50),"s100":f(z.s100),"s200":f(z.s200),
            "vs9":f(z.vs9),"vol":f(z.volume),"rsi":f(z.rsi,50),"macd":f(z.macd),"macds":f(z.macds),
            "mh":f(z.mh),"cmf":f(z.cmf),"obv_slope":f(x.obv.iloc[-1]-x.obv.iloc[max(0,len(x)-21)]),
            "tsi":f(z.tsi),"mtf":mtf}

# Rolling VWAP horizons: cycle context + medium-term + tactical entry.
VWAP_WINDOWS={"4Y":1460,"3Y":1095,"2Y":730,"1Y":365,"180D":180,"90D":90,"60D":60,"30D":30}

def vwap(x,n):
    if x.empty or len(x)<n:return 0
    y=x.tail(n); typ=(y.high+y.low+y.close)/3; vs=y.volume.sum()
    return f((typ*y.volume).sum()/vs) if vs>0 else 0

def vwap_set(x): return {label:vwap(x,days) for label,days in VWAP_WINDOWS.items()}

def quote_volume_series(x):
    if x.empty: return pd.Series(dtype=float)
    return (x["close"]*x["volume"]).replace([np.inf,-np.inf],np.nan).fillna(0.0)

def liquidity_metrics(x):
    if x.empty:
        return {"median30":0.0,"median90":0.0,"trend30_90":None,"score":0,"status":"NO DATA","active30":0}
    q=quote_volume_series(x); q30=q.tail(30); q90=q.tail(90)
    med30=f(q30.median(),0.0); med90=f(q90.median(),0.0)
    trend=pc(med30,med90) if med90>0 else None
    active30=int((q30>0).sum())
    s=0
    if med30>=2_000_000_000: s=10
    elif med30>=1_000_000_000: s=8
    elif med30>=MIN_MEDIAN_30D_QUOTE_IDR: s=6
    elif med30>=100_000_000: s=3
    status=("🟢 LIQUID" if med30>=1_000_000_000 else
            "🟡 ADEQUATE" if med30>MIN_MEDIAN_30D_QUOTE_IDR else
            "🟠 THIN" if med30>=100_000_000 else "🔴 VERY THIN")
    if trend is not None:
        if trend>20: s=min(10,s+1)
        elif trend<-30: s=max(0,s-1)
    return {"median30":med30,"median90":med90,"trend30_90":trend,
            "score":clamp(s,0,10),"status":status,"active30":active30}

def period_return(x, days):
    if x.empty or len(x)<2: return None
    target=x["time"].iloc[-1]-pd.Timedelta(days=days)
    prior=x[x["time"]<=target]
    if prior.empty: return None
    old=f(prior.iloc[-1]["close"],0); new=f(x.iloc[-1]["close"],0)
    return pc(new,old) if old>0 else None

def relative_strength_metrics(coin, btc):
    out={}
    for d in (30,90,180,365):
        cr=period_return(coin,d); br=period_return(btc,d) if not btc.empty else None
        out[d]={"coin":cr,"btc":br,"rs":(cr-br) if cr is not None and br is not None else None}
    valid=[v["rs"] for v in out.values() if v["rs"] is not None]
    avg=sum(valid)/len(valid) if valid else None
    s=0
    if avg is not None:
        if avg>=30: s=5
        elif avg>=15: s=4
        elif avg>=0: s=3
        elif avg>=-15: s=2
        elif avg>=-30: s=1
    return {"periods":out,"avg_rs":avg,"score":s}

def drawdown_metrics(x):
    if x.empty: return {"ath365":0.0,"dd365":None,"dd90":None}
    p=f(x.iloc[-1]["close"],0); h365=f(x.tail(365)["high"].max(),0); h90=f(x.tail(90)["high"].max(),0)
    return {"ath365":h365,"dd365":(p/h365-1)*100 if h365>0 else None,
            "dd90":(p/h90-1)*100 if h90>0 else None}

def cycle_metrics(x, vws):
    p=f(x.iloc[-1]["close"],0) if not x.empty else 0
    ratios={}
    for label in VWAP_WINDOWS:
        v=f(vws.get(label,0),0)
        ratios[label]=(p/v-1)*100 if p>0 and v>0 else None
    if ratios.get("4Y") is not None:
        zone=("🟢 DEEP DISCOUNT vs 4Y VWAP" if ratios["4Y"]<=-15 else
              "🟡 BELOW 4Y VWAP / ACCUMULATION" if ratios["4Y"]<=0 else
              "🟠 MID-CYCLE / EXPANSION" if ratios["4Y"]<=25 else
              "🔴 EXTENDED vs 4Y VWAP")
    elif ratios.get("1Y") is not None:
        zone=("🟢 DEEP DISCOUNT vs 1Y VWAP" if ratios["1Y"]<=-15 else
              "🟡 BELOW 1Y VWAP / ACCUMULATION" if ratios["1Y"]<=0 else
              "🟠 MID-CYCLE / EXPANSION" if ratios["1Y"]<=25 else
              "🔴 EXTENDED vs 1Y VWAP")
    else:
        zone="⚪ INSUFFICIENT VWAP HISTORY"
    long_vals=[ratios[k] for k in ("4Y","3Y","2Y","1Y") if ratios.get(k) is not None]
    medium_vals=[ratios[k] for k in ("180D","90D","60D","30D") if ratios.get(k) is not None]
    long_align=sum(v>0 for v in long_vals); med_align=sum(v>0 for v in medium_vals)
    score=5
    if long_vals: score += 3 if long_align>=3 else 2 if long_align>=2 else 1 if long_align>=1 else 0
    if ratios.get("1Y") is not None: score += 2 if -5<=ratios["1Y"]<=15 else 1 if ratios["1Y"]<25 else 0
    if ratios.get("180D") is not None and ratios["180D"]>0: score += 1
    if ratios.get("30D") is not None and ratios["30D"]>12: score -= 2
    return {"ratios":ratios,"zone":zone,"long_align":long_align,"medium_align":med_align,"score":clamp(score,0,15)}

def tokenomics_quality(d):
    m,fdv,c,t=[f(d.get(k),0.0) for k in ("mcap","fdv","circ","total")]
    issues=[]
    if m<=0: issues.append("MCap missing")
    if fdv<=0: issues.append("FDV missing")
    if c<=0: issues.append("Circulating supply missing")
    if t<=0: issues.append("Total supply missing")
    return {"complete":not issues,"issues":issues,
            "circ_total":(c/t if t>0 else None),"fdv_mcap":(fdv/m if m>0 and fdv>0 else None)}


def quality_proxy_score(sym, d, tokq, liq, histdays, ev):
    """Quality proxy from available evidence; market-cap rank is NOT full fundamental research."""
    rank = f(d.get("rank"), 9999) if isinstance(d, dict) else 9999
    rank_score = 100 if rank <= 20 else 85 if rank <= 50 else 70 if rank <= 100 else 55 if rank <= 200 else 35 if rank <= 500 else 15
    token_score_100 = 100.0 * f(tokq.get("circ_total"), 0) if tokq.get("complete") else 0.0
    if tokq.get("complete"):
        # Include FDV/cap and supply distribution quality; don't use missing data as healthy.
        fdv_mcap = f(tokq.get("fdv_mcap"), 999)
        token_score_100 = 50.0 * clamp((f(tokq.get("circ_total"), 0) - 0.35) / 0.65, 0, 1) + 50.0 * clamp((3.0 - fdv_mcap) / 2.0, 0, 1)
    liquidity_score_100 = clamp(f(liq.get("score"), 0) * 10, 0, 100)
    history_score = 100 if histdays >= FULL_CYCLE_HISTORY_DAYS else 85 if histdays >= CORE_HISTORY_DAYS else 65 if histdays >= 365 else 45 if histdays >= MIN_HISTORY_DAYS else 0
    score = 0.35 * rank_score + 0.30 * token_score_100 + 0.25 * liquidity_score_100 + 0.10 * history_score
    if ev and ev[0] == "HIGH": score -= 25
    if ev and ev[0] == "CRITICAL": score = 0
    # Data incompleteness should visibly lower confidence as well as trigger the existing gate.
    if not tokq.get("complete"): score = min(score, 54)
    return round(clamp(score, 0, 100), 1), {
        "market_rank_proxy": rank_score, "tokenomics": round(token_score_100, 1),
        "liquidity": round(liquidity_score_100, 1), "history": history_score,
        "disclaimer": "Proxy kualitas; developer/adopsi/utilitas belum diverifikasi penuh oleh data bulk."
    }


def accumulation_metrics(t, cmfs, cycle, drawdown, hist):
    """Pre-reversal accumulation proxy from price discount, CMF and OBV—not whale wallet data."""
    earned = 0.0
    capacity = 0.0
    reasons = []
    dd = drawdown.get("dd365") if isinstance(drawdown, dict) else None
    if dd is not None:
        capacity += 20
        pts = 20 if dd <= -70 else 17 if dd <= -55 else 14 if dd <= -40 else 9 if dd <= -25 else 5 if dd <= -15 else 0
        earned += pts
        reasons.append(f"drawdown 365D {dd:.1f}% ({pts:.0f}/20)")

    ratios = cycle.get("ratios", {}) if isinstance(cycle, dict) else {}
    for label, weight in (("4Y", 15), ("2Y", 6), ("1Y", 4)):
        val = ratios.get(label)
        if val is None:
            continue
        capacity += weight
        if val <= -30: pts = weight
        elif val <= -15: pts = weight * 0.8
        elif val <= 0: pts = weight * 0.6
        elif val <= 10: pts = weight * 0.3
        else: pts = 0
        earned += pts
        reasons.append(f"harga vs VWAP {label} {val:+.1f}%")

    def avg_valid(keys):
        vals = [cmfs.get(k) for k in keys if cmfs.get(k) is not None and np.isfinite(cmfs.get(k))]
        return (sum(vals) / len(vals), len(vals)) if vals else (None, 0)

    long_flow, long_n = avg_valid(("180D", "90D"))
    if long_flow is not None:
        capacity += 20
        pts = 20 if long_flow >= .08 else 16 if long_flow >= .03 else 11 if long_flow >= 0 else 5 if long_flow >= -.03 else 0
        earned += pts
        reasons.append(f"CMF panjang {long_flow:+.3f} ({pts:.0f}/20)")

    mid_flow, mid_n = avg_valid(("60D", "30D"))
    if mid_flow is not None:
        capacity += 15
        pts = 15 if mid_flow >= .08 else 12 if mid_flow >= .03 else 8 if mid_flow >= 0 else 3 if mid_flow >= -.03 else 0
        earned += pts
        reasons.append(f"CMF menengah {mid_flow:+.3f} ({pts:.0f}/15)")

    short_flow, short_n = avg_valid(("20D", "14D", "7D"))
    if short_flow is not None:
        capacity += 10
        pts = 10 if short_flow >= .08 else 7 if short_flow >= .02 else 5 if short_flow >= -.02 else 2 if short_flow >= -.06 else 0
        earned += pts
        reasons.append(f"CMF pendek {short_flow:+.3f} ({pts:.0f}/10)")

    obv = t.get("obv_slope") if isinstance(t, dict) else None
    if obv is not None and np.isfinite(obv):
        capacity += 10
        pts = 10 if obv > 0 else 0
        earned += pts
        reasons.append(f"OBV {'naik' if obv > 0 else 'datar/turun'}")

    vol, vavg, cmf20 = (t.get("vol"), t.get("vs9"), t.get("cmf")) if isinstance(t, dict) else (None, None, None)
    if vol is not None and vavg is not None and vavg > 0 and cmf20 is not None:
        capacity += 5
        ratio = vol / vavg
        pts = 5 if ratio >= 1.15 and cmf20 >= -0.02 else 2 if ratio >= 0.9 and cmf20 >= 0 else 0
        earned += pts
        reasons.append(f"volume/SMA9 {ratio:.2f}x")

    price = f(t.get("p"), 0) if isinstance(t, dict) else 0
    sma20v = f(t.get("s20"), 0) if isinstance(t, dict) else 0
    if price > 0 and sma20v > 0:
        capacity += 5
        pts = 5 if price <= sma20v * 1.10 else 0
        earned += pts
        reasons.append("harga dekat basis SMA20" if pts else "harga jauh di atas SMA20")

    # Component weights total 110; normalize coverage to 0-100 without changing the score denominator.
    coverage = round(min(100.0, capacity / 110.0 * 100.0), 1)
    score = round((earned / capacity) * 100, 1) if capacity > 0 else None
    # Lower confidence when too many components are absent; never invent whale evidence.
    effective = round(score * (0.65 + 0.35 * coverage / 100), 1) if score is not None else None
    if effective is None:
        label = "DATA FLOW TIDAK CUKUP"
    elif effective >= 65 and long_flow is not None and long_flow > 0:
        label = "POTENSI AKUMULASI PASAR"
    elif effective >= 50:
        label = "AKUMULASI CAMPURAN / AWAL"
    else:
        label = "AKUMULASI BELUM TERKONFIRMASI"
    return {
        "score": score, "effective_score": effective, "coverage": coverage,
        "label": label, "reasons": reasons[:8],
        "long_cmf": long_flow, "medium_cmf": mid_flow, "short_cmf": short_flow,
        "whale_status": "N/A — build ini belum memakai data wallet/on-chain whale yang terverifikasi",
        "disclaimer": "Akumulasi Score memakai proksi CMF/OBV/volume/discount; bukan bukti pembelian whale."
    }


def dca_zone_metrics(price, cycle, vw30, drawdown, accumulation):
    """Price zones for staged DCA; indicative levels are not a forecast of bottom."""
    ratios = cycle.get("ratios", {}) if isinstance(cycle, dict) else {}
    r4, r2, r1, r30 = (ratios.get("4Y"), ratios.get("2Y"), ratios.get("1Y"), ratios.get("30D"))
    dd = drawdown.get("dd365") if isinstance(drawdown, dict) else None
    if vw30 and price > vw30 * 1.10:
        zone = "TUNGGU RETEST TIKTIKAL — >10% di atas VWAP 30D"
    elif r4 is not None and r4 <= -15 and (r2 is None or r2 <= 0):
        zone = "DISKON STRUKTURAL — di bawah VWAP siklus"
    elif r1 is not None and r1 <= 0:
        zone = "DISKON MENENGAH — di bawah VWAP 1Y"
    elif r30 is not None and r30 <= 0:
        zone = "ZONA RETRACE TAKTIS — di bawah VWAP 30D"
    elif dd is not None and dd <= -30:
        zone = "ZONA DRAWDOWN — verifikasi flow dan fundamental"
    else:
        zone = "BELUM DISKON JELAS — DCA konservatif / tunggu harga lebih baik"
    ladder = {"now": round(price, 8) if price > 0 else None,
              "minus8": round(price * 0.92, 8) if price > 0 else None,
              "minus15": round(price * 0.85, 8) if price > 0 else None}
    return {"zone": zone, "ladder": ladder,
            "invalidation": "Tinjau ulang bila risiko proyek/keamanan memburuk, tokenomics berubah drastis, likuiditas gagal, atau ada event CRITICAL; harga turun saja bukan pembatal tesis otomatis."}

def mcap_tier_idr(mcap_idr):
    """Kelas market cap indikatif; ambang dalam IDR, bukan standar universal."""
    try:
        value = float(mcap_idr)
    except (TypeError, ValueError):
        return "N/A"
    if not np.isfinite(value) or value <= 0:
        return "N/A"
    for threshold, label in MCAP_TIER_THRESHOLDS_IDR:
        if value < threshold:
            return label
    return "MEGA"


def market_cap_migration_metrics(sym, d, hist):
    """Market-cap sizing + migration view.

    Bulk market data supplies current market cap but not full historical market cap.
    For historical periods, use verified manual cap snapshots when configured;
    otherwise use historical close * current circulating supply as a price-normalized
    cap proxy. The proxy is NOT actual historical market cap because past circulating
    supply can differ; output and score metadata make this explicit.
    """
    d = d if isinstance(d, dict) else {}
    current_mcap = f(d.get("mcap"), 0.0)
    circ = f(d.get("circ"), 0.0)
    current_tier = mcap_tier_idr(current_mcap)
    lookbacks = {"4Y": 1460, "3Y": 1095, "2Y": 730, "1Y": 365, "180D": 180, "90D": 90, "30D": 30}
    manual = REFERENCE_MKTCAP_HISTORY_IDR.get(str(sym).upper(), {}) if isinstance(REFERENCE_MKTCAP_HISTORY_IDR, dict) else {}
    snapshots = {}
    x = hist.copy() if isinstance(hist, pd.DataFrame) else pd.DataFrame()
    if not x.empty and "time" in x.columns and "close" in x.columns:
        try:
            times = pd.to_datetime(x["time"], utc=True, errors="coerce")
            x = x.assign(_time=times, _close=pd.to_numeric(x["close"], errors="coerce"))
            x = x.dropna(subset=["_time", "_close"]).sort_values("_time")
        except Exception:
            x = pd.DataFrame()
    else:
        x = pd.DataFrame()

    anchor = x["_time"].iloc[-1] if not x.empty else None
    for label, days in lookbacks.items():
        manual_cap = f(manual.get(label), 0.0) if isinstance(manual, dict) else 0.0
        if manual_cap > 0:
            snapshots[label] = {"mcap": manual_cap, "tier": mcap_tier_idr(manual_cap),
                                "source": "snapshot market cap historis aktual (konfigurasi manual terverifikasi)",
                                "actual": True}
            continue
        if anchor is None or circ <= 0 or x.empty:
            continue
        target = anchor - pd.Timedelta(days=days)
        prior = x.loc[x["_time"] <= target]
        if prior.empty:
            continue
        row = prior.iloc[-1]
        age_error = abs((target - row["_time"]).total_seconds()) / 86400.0
        if age_error > 10:
            continue
        historical_proxy = f(row["_close"], 0.0) * circ
        if historical_proxy > 0:
            snapshots[label] = {"mcap": historical_proxy, "tier": mcap_tier_idr(historical_proxy),
                                "source": "proxy: harga historis Indodax x suplai beredar saat ini",
                                "actual": False}

    # Primary migration comparison uses the furthest available historical observation.
    tier_rank = {"MICRO": 0, "SMALL": 1, "MID": 2, "LARGE": 3, "MEGA": 4}
    ordered_periods = ["4Y", "3Y", "2Y", "1Y", "180D", "90D", "30D"]
    oldest_label = next((lab for lab in ordered_periods if lab in snapshots), None)
    migration_delta = None
    migration_basis = "N/A"
    migration_label = "RIWAYAT MCap TIDAK CUKUP"
    adjustment = 0.0
    if oldest_label and current_tier in tier_rank and snapshots[oldest_label]["tier"] in tier_rank:
        old_tier = snapshots[oldest_label]["tier"]
        migration_delta = tier_rank[current_tier] - tier_rank[old_tier]
        migration_basis = "market cap snapshot aktual" if snapshots[oldest_label].get("actual") else "proxy suplai saat ini"
        if migration_delta >= 2:
            adjustment += 8.0
        elif migration_delta == 1:
            adjustment += 4.0
        elif migration_delta == -1:
            adjustment -= 5.0
        elif migration_delta <= -2:
            adjustment -= 10.0
        migration_label = f"{old_tier} → {current_tier} ({'naik' if migration_delta > 0 else 'turun' if migration_delta < 0 else 'tetap'} kelas; {migration_basis})"

    # Recent migration proxy is a smaller signal; it must not overpower long-term quality.
    one_y = snapshots.get("1Y")
    if one_y and current_tier in tier_rank and one_y["tier"] in tier_rank:
        delta_1y = tier_rank[current_tier] - tier_rank[one_y["tier"]]
        adjustment += 3.0 if delta_1y > 0 else -4.0 if delta_1y < 0 else 0.0
    else:
        delta_1y = None

    # Market-cap headroom score balances upside space against micro-cap survivorship risk.
    if current_mcap <= 0:
        base_score = None
    elif current_tier == "MICRO":
        base_score = 52.0
    elif current_tier == "SMALL":
        base_score = 86.0
    elif current_tier == "MID":
        base_score = 74.0
    elif current_tier == "LARGE":
        base_score = 52.0
    else:
        base_score = 30.0
    mcap_5x = current_mcap * 5 if current_mcap > 0 else None
    mcap_10x = current_mcap * 10 if current_mcap > 0 else None
    # A future cap > ~US$100b is possible but a very demanding hurdle; mild penalty only.
    demanding_10x = bool(mcap_10x is not None and mcap_10x > 1_600_000_000_000_000)
    if demanding_10x:
        adjustment -= 5.0
    score = round(clamp(base_score + adjustment, 0, 100), 1) if base_score is not None else None

    return {
        "current_mcap": current_mcap if current_mcap > 0 else None,
        "current_tier": current_tier,
        "historical": snapshots,
        "oldest_period": oldest_label,
        "oldest_tier": snapshots[oldest_label]["tier"] if oldest_label else None,
        "migration_delta_tiers": migration_delta,
        "migration_basis": migration_basis,
        "migration_label": migration_label,
        "migration_score": score,
        "migration_score_base": base_score,
        "migration_adjustment": round(adjustment, 1),
        "tier_4Y": snapshots.get("4Y", {}).get("tier"),
        "tier_3Y": snapshots.get("3Y", {}).get("tier"),
        "tier_2Y": snapshots.get("2Y", {}).get("tier"),
        "tier_1Y": snapshots.get("1Y", {}).get("tier"),
        "tier_180D": snapshots.get("180D", {}).get("tier"),
        "tier_90D": snapshots.get("90D", {}).get("tier"),
        "tier_30D": snapshots.get("30D", {}).get("tier"),
        "mcap_5x": mcap_5x,
        "mcap_10x": mcap_10x,
        "tier_5x": mcap_tier_idr(mcap_5x) if mcap_5x else "N/A",
        "tier_10x": mcap_tier_idr(mcap_10x) if mcap_10x else "N/A",
        "demanding_10x_hurdle": demanding_10x,
        "coverage_count": len(snapshots),
        "actual_snapshot_count": sum(1 for value in snapshots.values() if value.get("actual")),
        "proxy_snapshot_count": sum(1 for value in snapshots.values() if not value.get("actual")),
        "disclaimer": "Kelas market cap hanya indikatif. Proxy historis memakai suplai beredar saat ini, bukan suplai historis; gunakan snapshot aktual terverifikasi untuk klaim migrasi kapitalisasi yang sesungguhnya."
    }


def recovery_potential_metrics(price, d, quality_score, accumulation, rel, gate, liq, event, cycle=None, hist=None, sym=None):
    """Score recovery viability; an old ATH is context, never a buy thesis.

    Inputs available in this batch screener do not verify users, revenue,
    developers, security or wallet-level whales. Therefore this remains a
    quant shortlist, and long-term relative underperformance is explicitly
    penalised instead of rewarding a very deep drawdown by itself.
    """
    d = d if isinstance(d, dict) else {}
    cycle = cycle if isinstance(cycle, dict) else {}
    ath = f(d.get("ath"), 0.0)
    mcap = f(d.get("mcap"), 0.0)
    circ = f(d.get("circ"), 0.0)
    total_supply = f(d.get("total"), 0.0)
    price = f(price, 0.0)
    ath_multiple = ath / price if ath > 0 and price > 0 else None
    ath_drawdown = (price / ath - 1.0) * 100.0 if ath > 0 and price > 0 else None

    # Price decline is deliberately capped and then penalised when extreme:
    # a 95%+ drawdown can be evidence of a value trap, not a bigger bargain.
    if ath_drawdown is None:
        ath_discount_score = None
    elif ath_drawdown > -25:
        ath_discount_score = 20
    elif ath_drawdown > -40:
        ath_discount_score = 35
    elif ath_drawdown > -55:
        ath_discount_score = 50
    elif ath_drawdown > -70:
        ath_discount_score = 68
    elif ath_drawdown > -80:
        ath_discount_score = 60
    elif ath_drawdown > -90:
        ath_discount_score = 45
    elif ath_drawdown > -95:
        ath_discount_score = 25
    elif ath_drawdown > -98:
        ath_discount_score = 10
    else:
        ath_discount_score = 0

    # Discount versus long VWAP adds context not tied to a single historical peak.
    ratios = cycle.get("ratios", {})
    vwap_scores = []
    for key in ("4Y", "2Y", "1Y"):
        value = ratios.get(key)
        if value is None:
            continue
        if value <= -25: sc = 90
        elif value <= -10: sc = 75
        elif value <= 5: sc = 60
        elif value <= 20: sc = 35
        else: sc = 15
        vwap_scores.append(sc)
    vwap_discount_score = sum(vwap_scores) / len(vwap_scores) if vwap_scores else None
    valid_discounts = []
    if ath_discount_score is not None: valid_discounts.append((ath_discount_score, 0.50))
    if vwap_discount_score is not None: valid_discounts.append((vwap_discount_score, 0.50))
    discount_score = (sum(v * w for v, w in valid_discounts) / sum(w for _, w in valid_discounts)) if valid_discounts else None

    periods = rel.get("periods", {}) if isinstance(rel, dict) else {}
    rs_values = {d: periods.get(d, {}).get("rs") for d in (30, 90, 180, 365)}

    def rs_band(value):
        if value is None:
            return None
        if value >= 20: return 100.0
        if value >= 5: return 82.0
        if value >= -10: return 62.0
        if value >= -25: return 42.0
        if value >= -45: return 22.0
        return 5.0

    # Resilience rewards holding up against BTC over 90/180/365D; it does not
    # require an absolute price reversal, but persistent underperformance counts.
    rs_weights = {30: 0.10, 90: 0.20, 180: 0.30, 365: 0.40}
    rs_scored = [(rs_band(v), rs_weights[k]) for k, v in rs_values.items() if v is not None]
    strength_score = (sum(v * w for v, w in rs_scored) / sum(w for _, w in rs_scored)) if rs_scored else None

    # Improvement proxy: compare the recent 30D relative return with the average
    # of the preceding two months (approximated from nested 90D and 30D returns).
    rs30, rs90 = rs_values[30], rs_values[90]
    recent_improvement = None
    improvement_score = None
    if rs30 is not None and rs90 is not None:
        preceding_60_monthly = (rs90 - rs30) / 2.0
        recent_improvement = rs30 - preceding_60_monthly
        if recent_improvement >= 20: improvement_score = 100.0
        elif recent_improvement >= 5: improvement_score = 80.0
        elif recent_improvement >= 0: improvement_score = 60.0
        elif recent_improvement >= -8: improvement_score = 40.0
        elif recent_improvement >= -20: improvement_score = 20.0
        else: improvement_score = 5.0
    if strength_score is not None and improvement_score is not None:
        resilience_score = 0.75 * strength_score + 0.25 * improvement_score
    elif strength_score is not None:
        resilience_score = strength_score
    elif improvement_score is not None:
        resilience_score = improvement_score
    else:
        resilience_score = None

    rs180, rs365 = rs_values[180], rs_values[365]
    persistent_underperformer = (rs180 is not None and rs365 is not None and
                                 rs180 <= -25 and rs365 <= -40 and
                                 (improvement_score is None or improvement_score < 80))

    # ATH age penalty: a peak from a prior cycle is a stale anchor, not fair value.
    ath_age_days = None
    ath_age_penalty = 0.0
    try:
        ath_date = pd.to_datetime(d.get("ath_date"), utc=True, errors="coerce")
        if pd.notna(ath_date):
            ath_age_days = max(0, int((pd.Timestamp.now(tz="UTC") - ath_date).days))
            ath_age_penalty = (10.0 if ath_age_days > 1460 else 8.0 if ath_age_days > 1095
                               else 5.0 if ath_age_days > 730 else 2.0 if ath_age_days > 365 else 0.0)
    except Exception:
        pass

    # Simple dilution pressure proxy based on current circulation; verified unlock
    # schedules are not part of CoinGecko's basic bulk endpoint, so absent values
    # remain unknown rather than being assumed safe.
    circ_total = (circ / total_supply) if circ > 0 and total_supply > 0 else None
    dilution_penalty = 0.0
    if circ_total is None:
        dilution_penalty += 6.0
    elif circ_total < 0.60:
        dilution_penalty += 8.0
    elif circ_total < 0.75:
        dilution_penalty += 5.0
    elif circ_total < 0.85:
        dilution_penalty += 2.0
    fdv_mcap = (f(d.get("fdv"), 0) / mcap) if mcap > 0 and f(d.get("fdv"), 0) > 0 else None
    if fdv_mcap is not None and fdv_mcap > 2.0:
        dilution_penalty += 4.0

    acc = f(accumulation.get("effective_score"), 0.0) if isinstance(accumulation, dict) else 0.0
    qscore = f(quality_score, 0.0)
    if discount_score is None or resilience_score is None:
        base_before_mcap = 0.35 * qscore + 0.30 * acc + 0.25 * f(resilience_score, 0) + 0.10 * f(discount_score, 0)
        coverage_penalty = (8.0 if discount_score is None else 0.0) + (10.0 if resilience_score is None else 0.0)
    else:
        base_before_mcap = 0.35 * qscore + 0.30 * acc + 0.25 * resilience_score + 0.10 * discount_score
        coverage_penalty = 0.0

    # Add market-cap headroom/migration as a controlled 12% dimension. The other
    # dimensions are normalized to 88%; missing cap data does not get a fake score.
    mcap_migration = market_cap_migration_metrics(sym or d.get("symbol", ""), d, hist)
    mcap_score = mcap_migration.get("migration_score")
    base = (0.88 * base_before_mcap + 0.12 * mcap_score) if mcap_score is not None else base_before_mcap
    persistence_penalty = 12.0 if persistent_underperformer else 0.0
    total_penalty = min(35.0, coverage_penalty + ath_age_penalty + dilution_penalty + persistence_penalty)
    recovery = clamp(base - total_penalty, 0, 100)

    # Market-cap scenarios assume circulating supply and valuation basis unchanged.
    # They are hurdle illustrations, not targets or forecasts.
    mcap_5x = mcap * 5 if mcap > 0 else None
    mcap_10x = mcap * 10 if mcap > 0 else None
    ath_mcap_equiv = ath * circ if ath > 0 and circ > 0 else None
    ath_burden = ath_mcap_equiv / mcap if ath_mcap_equiv is not None and mcap > 0 else ath_multiple

    notes = []
    if ath_multiple is None:
        notes.append("ATH tidak tersedia; jarak ke puncak lama tidak dinilai")
    else:
        notes.append(f"ATH lama {ath_multiple:.1f}x dari harga kini; hanya konteks, bukan target")
    if ath_age_days is not None and ath_age_days > 1095:
        notes.append(f"ATH sudah {ath_age_days} hari lalu; puncak siklus lama diberi penalti")
    if persistent_underperformer:
        notes.append("Kinerja vs BTC lemah pada 180D dan 365D tanpa perbaikan relatif yang cukup; risiko value trap meningkat")
    if circ_total is not None and circ_total < 0.75:
        notes.append(f"Dilusi: suplai beredar baru {circ_total*100:.1f}% dari total")
    if mcap_5x is not None:
        notes.append(f"Kapitalisasi pasar skenario harga 5x ≈ {idr(mcap_5x)} bila suplai beredar tetap")
    if mcap_10x is not None:
        notes.append(f"Kapitalisasi pasar skenario harga 10x ≈ {idr(mcap_10x)} bila suplai beredar tetap")

    liq_pass = (f(liq.get("median30"), 0) > MIN_MEDIAN_30D_QUOTE_IDR and
                f(liq.get("median90"), 0) > MIN_MEDIAN_90D_QUOTE_IDR)
    hard_fail = bool(gate.get("hard_fail")) if isinstance(gate, dict) else True
    data_gap = bool(gate.get("data_gap")) if isinstance(gate, dict) else True
    ev_level = str(event[0]).upper() if event else "NONE"
    if ev_level == "CRITICAL" or hard_fail:
        label = "GUGUR — HARD RISK GATE"
    elif ev_level == "HIGH":
        label = "PAUSE — RISIKO EVENT TINGGI"
    elif data_gap or not liq_pass:
        label = "RISET AWAL — DATA/LIKUIDITAS BELUM LOLOS"
    elif persistent_underperformer:
        label = ("QUALITY PROXY TINGGI, TAPI UNDERPERFORMER LAMA — WATCH ONLY"
                 if qscore >= 70 else "DISKON BESAR, RELATIVE STRENGTH LEMAH — BUKAN PRIORITAS")
    elif qscore >= 62 and acc >= 50 and resilience_score is not None and resilience_score >= 45 and recovery >= 60:
        label = "KANDIDAT RECOVERY ASIMETRIS — RISET FUNDAMENTAL"
    elif qscore >= 58 and acc >= 42 and recovery >= 52:
        label = "WATCHLIST DCA PRA-CYCLE — VERIFIKASI"
    else:
        label = "BELUM CUKUP KUAT — PANTAU"

    # Priority flag keeps severe, persistent underperformers out of the priority
    # top 10 unless relative performance starts improving. They remain reportable.
    priority_eligible = bool(
        not hard_fail and not data_gap and liq_pass and ev_level not in {"HIGH", "CRITICAL"}
        and qscore >= 52 and acc >= 42 and resilience_score is not None and resilience_score >= 30
        and recovery >= 50 and not persistent_underperformer
    )
    return {
        "score": round(recovery, 1), "raw_score": round(base, 1),
        "ath": ath, "ath_multiple": round(ath_multiple, 2) if ath_multiple is not None else None,
        "ath_drawdown_pct": round(ath_drawdown, 1) if ath_drawdown is not None else None,
        "ath_age_days": ath_age_days, "ath_age_penalty": ath_age_penalty,
        "ath_discount_score": ath_discount_score,
        "vwap_discount_score": round(vwap_discount_score, 1) if vwap_discount_score is not None else None,
        "discount_score": round(discount_score, 1) if discount_score is not None else None,
        "turnaround_score": round(resilience_score, 1) if resilience_score is not None else None,
        "strength_score": round(strength_score, 1) if strength_score is not None else None,
        "improvement_score": round(improvement_score, 1) if improvement_score is not None else None,
        "recent_improvement_rs": round(recent_improvement, 1) if recent_improvement is not None else None,
        "rs30": round(rs30, 1) if rs30 is not None else None,
        "rs90": round(rs90, 1) if rs90 is not None else None,
        "rs180": round(rs180, 1) if rs180 is not None else None,
        "rs365": round(rs365, 1) if rs365 is not None else None,
        "persistent_underperformer": persistent_underperformer,
        "dilution_penalty": round(dilution_penalty, 1),
        "circ_total_ratio": round(circ_total, 4) if circ_total is not None else None,
        "fdv_mcap": round(fdv_mcap, 2) if fdv_mcap is not None else None,
        "mcap_current": mcap if mcap > 0 else None,
        "mcap_5x": mcap_5x, "mcap_10x": mcap_10x,
        "market_cap_migration": mcap_migration,
        "market_cap_migration_weight_pct": 12.0 if mcap_score is not None else 0.0,
        "base_score_before_mcap": round(base_before_mcap, 1),
        "ath_mcap_equiv_current_supply": ath_mcap_equiv,
        "ath_marketcap_burden_multiple": round(ath_burden, 2) if ath_burden is not None else None,
        "label": label, "priority_eligible": priority_eligible,
        "liquidity_pass": liq_pass,
        "fundamental_status": "BELUM DIVERIFIKASI OTOMATIS — usage, revenue, developer, keamanan, kompetisi, unlock, dan whale on-chain perlu validasi sumber proyek/independen",
        "notes": notes,
        "disclaimer": "Recovery Score bukan prediksi 5x/10x. ATH lama tidak masuk sebagai target; skenario market cap hanya ilustrasi dengan suplai dianggap tetap."
    }


def universe_gate(sym, hist, d, liq, tokq, ev):
    reasons=[]; hard_fail=False; data_gap=False; histdays=len(hist)
    if histdays<MIN_HISTORY_DAYS:
        hard_fail=True; reasons.append(f"History hanya {histdays}D < {MIN_HISTORY_DAYS}D")
    elif histdays<FULL_CYCLE_HISTORY_DAYS:
        reasons.append("Belum memiliki full 4Y history; confidence cycle terbatas")
    mcap=f(d.get("mcap"),0) if d else 0
    if mcap<=0:
        data_gap=True; reasons.append("Market-cap data tidak tersedia")
    elif mcap<MIN_MARKET_CAP_IDR:
        reasons.append("Market cap sangat kecil; risk premium tinggi")
    if liq.get("median30",0)<=MIN_MEDIAN_30D_QUOTE_IDR or liq.get("median90",0)<=MIN_MEDIAN_90D_QUOTE_IDR:
        hard_fail=True
        reasons.append(
            f"Turnover liquidity wajib > Rp100 juta/hari; belum lolos: 30D Rp{liq.get('median30',0):,.0f}/day, "
            f"90D Rp{liq.get('median90',0):,.0f}/day"
        )
    if not tokq["complete"]:
        data_gap=True; reasons.extend(tokq["issues"])
    else:
        if tokq["circ_total"]<MIN_CIRC_TOTAL_RATIO:
            hard_fail=True; reasons.append(f"Circ/Total {tokq['circ_total']*100:.1f}% terlalu rendah")
        if tokq["fdv_mcap"]>MAX_FDV_MCAP_MULTIPLE:
            hard_fail=True; reasons.append(f"FDV/MCap {tokq['fdv_mcap']:.2f}x terlalu tinggi")
        unlock=UNLOCK_365D.get(sym,None)
        if unlock is not None and unlock>MAX_UNLOCK_365D_HARD:
            hard_fail=True; reasons.append(f"Unlock 365D {unlock:.1f}% > {MAX_UNLOCK_365D_HARD:.1f}%")
    if ev[0]=="CRITICAL":
        hard_fail=True; reasons.append("CRITICAL event override")
    elif ev[0]=="HIGH":
        reasons.append("HIGH event risk: BUY diblokir sementara")
    status="FAIL" if hard_fail else "DATA GAP" if data_gap else "PASS"
    age_class=("CORE HISTORY" if histdays>=CORE_HISTORY_DAYS else
               "EMERGING / SATELLITE" if histdays>=MIN_HISTORY_DAYS else "NEW / INSUFFICIENT")
    return {"status":status,"hard_fail":hard_fail,"data_gap":data_gap,
            "reasons":reasons[:8],"age_class":age_class}

def investment_role(gate, score, histdays):
    if gate["hard_fail"]: return "🔴 AVOID / REJECT"
    if gate["data_gap"]: return "🟡 WATCHLIST — DATA GAP"
    if score>=80 and histdays>=CORE_HISTORY_DAYS: return "🟢 CORE INVESTMENT"
    if score>=70: return "🟢 SATELLITE / HIGH-CONVICTION DCA"
    if score>=60: return "🟡 SATELLITE / WAIT FOR BETTER SETUP"
    return "🟠 WATCHLIST / NO AGGRESSIVE BUY"

# ------------------------- MACRO -------------------------
def _normalise_fred_frame(frame, series):
    """Normalise FRED CSV/JSON columns to time + close.

    FRED CSV headers normally look like observation_date,DGS10 (or DFF), not
    DATE,VALUE. Missing values are commonly represented by a dot.
    """
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["time", "close"])
    x = frame.copy()
    by_lower = {str(c).strip().lower(): c for c in x.columns}
    date_col = next((by_lower[k] for k in ("date", "observation_date", "time") if k in by_lower), None)
    value_col = next((by_lower[k] for k in (str(series).lower(), "value", "close") if k in by_lower), None)
    if date_col is None:
        return pd.DataFrame(columns=["time", "close"])
    if value_col is None:
        others = [c for c in x.columns if c != date_col]
        if not others:
            return pd.DataFrame(columns=["time", "close"])
        value_col = others[0]
    out = pd.DataFrame({
        "time": pd.to_datetime(x[date_col], errors="coerce", utc=True),
        "close": pd.to_numeric(x[value_col].astype("string").replace(".", pd.NA), errors="coerce"),
    })
    out = out.replace([np.inf, -np.inf], np.nan).dropna(subset=["time", "close"])
    return out.sort_values("time").reset_index(drop=True)


def _fresh_series_info(frame, max_age_days=10, min_value=None, max_value=None):
    """Latest observation + timestamp; value is None if stale/out of range."""
    empty = {"value": None, "date": None, "age_days": None, "fresh": False}
    if frame is None or frame.empty or not {"time", "close"}.issubset(frame.columns):
        return empty.copy()
    x = frame.copy()
    x["time"] = pd.to_datetime(x["time"], errors="coerce", utc=True)
    x["close"] = pd.to_numeric(x["close"], errors="coerce")
    x = x.replace([np.inf, -np.inf], np.nan).dropna(subset=["time", "close"]).sort_values("time")
    if x.empty:
        return empty.copy()
    row = x.iloc[-1]
    now = pd.Timestamp.now(tz="UTC")
    obs = row["time"]
    age = (now - obs).total_seconds() / 86400.0
    raw = float(row["close"])
    in_range = ((min_value is None or raw >= min_value) and
                (max_value is None or raw <= max_value))
    fresh = bool(0 <= age <= max_age_days and in_range)
    return {"value": raw if fresh else None,
            "date": obs.strftime("%Y-%m-%d"),
            "age_days": round(age, 1),
            "fresh": fresh}


def _period_change(frame, days=30):
    """Percentage change over calendar days, using the latest observation on/before target."""
    if frame is None or frame.empty or not {"time", "close"}.issubset(frame.columns):
        return None
    x = frame.copy()
    x["time"] = pd.to_datetime(x["time"], errors="coerce", utc=True)
    x["close"] = pd.to_numeric(x["close"], errors="coerce")
    x = x.replace([np.inf, -np.inf], np.nan).dropna(subset=["time", "close"]).sort_values("time")
    if len(x) < 2:
        return None
    latest = x.iloc[-1]
    target = latest["time"] - pd.Timedelta(days=days)
    prior_rows = x[x["time"] <= target]
    if prior_rows.empty:
        return None
    prior = prior_rows.iloc[-1]
    # Avoid labelling a much older comparison as a 30D change after a data gap.
    if (target - prior["time"]).total_seconds() > 7 * 86400:
        return None
    return pc(float(latest["close"]), float(prior["close"]))


def _meta_line(label, meta):
    if not isinstance(meta, dict) or not meta.get("date"):
        return f"{label}: N/A (tanggal observasi tidak tersedia)"
    age = meta.get("age_days")
    age_text = f", umur {age:.0f} hari" if age is not None else ""
    quality = "SEGAR" if meta.get("fresh") else "STALE/N/A"
    source = meta.get("source")
    source_text = f" — {source}" if source else ""
    return f"{label}: {meta['date']}{age_text} [{quality}]{source_text}"


def _fresh_series_value(frame, max_age_days=10, min_value=None, max_value=None):
    """Compatibility wrapper: latest numeric value only if fresh and in range."""
    return _fresh_series_info(frame, max_age_days, min_value, max_value).get("value")


def _series_delta(frame, steps=20):
    """Absolute delta versus N observations earlier; None if history is short."""
    if frame is None or frame.empty or len(frame) <= steps:
        return None
    x = frame.copy()
    x["close"] = pd.to_numeric(x["close"], errors="coerce")
    x = x.replace([np.inf, -np.inf], np.nan).dropna(subset=["close"])
    if len(x) <= steps:
        return None
    latest, previous = float(x.iloc[-1]["close"]), float(x.iloc[-steps-1]["close"])
    if not np.isfinite(latest) or not np.isfinite(previous):
        return None
    return latest - previous


def fred(series,days=450):
    """Fetch a FRED series; use API if configured, then multiple public CSV forms.

    The parser accepts FRED's observation_date/SERIES headers and dot-missing values.
    Never fabricate observations: if every source fails, return an empty frame.
    """
    st=(pd.Timestamp.now(tz="UTC")-pd.Timedelta(days=days)).strftime("%Y-%m-%d")
    end=pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    empty=pd.DataFrame(columns=["time","close"])

    # Prefer official FRED Web Service when an API key is configured.
    if FRED_API_KEY:
        d=get_json(
            URL["fred_api"],
            {"series_id":series,"observation_start":st,"observation_end":end,"file_type":"json","api_key":FRED_API_KEY},
            quiet=True
        )
        if isinstance(d,dict) and isinstance(d.get("observations"),list):
            rows=[{"observation_date":row.get("date"), series:row.get("value")}
                  for row in d["observations"] if isinstance(row,dict)]
            parsed=_normalise_fred_frame(pd.DataFrame(rows),series)
            if not parsed.empty:
                return parsed

    # Public FRED graph CSV does not require an API key. Try bounded and full
    # downloads because some responses/proxies mishandle the cosd parameter.
    attempts=[
        {"id":series,"cosd":st,"coed":end},
        {"id":series,"cosd":st},
        {"id":series},
    ]
    for params in attempts:
        x=get_csv(URL["fred"],params,quiet=True)
        parsed=_normalise_fred_frame(x,series)
        if not parsed.empty:
            parsed=parsed[parsed["time"] >= pd.Timestamp(st,tz="UTC")].copy()
            if not parsed.empty:
                return parsed.reset_index(drop=True)
    log.warning("FRED %s unavailable or unparseable; report will show N/A (no fabricated data).",series)
    return empty


def yahoo(sym,days=450):
    p2=int(time.time()); p1=p2-days*86400
    d=get_json(URL["yahoo"].format(sym=sym),{"period1":p1,"period2":p2,"interval":"1d"},quiet=True)
    try:
        r=d["chart"]["result"][0]
        return pd.DataFrame({
            "time":pd.to_datetime(r["timestamp"],unit="s",utc=True),
            "close":pd.to_numeric(r["indicators"]["quote"][0]["close"],errors="coerce")
        }).replace([np.inf,-np.inf],np.nan).dropna().sort_values("time").reset_index(drop=True)
    except (TypeError, KeyError, IndexError):
        return pd.DataFrame(columns=["time", "close"])


def macro():
    """Gather live macro/rotation data with source timestamps and explicit freshness."""
    cg_bulk_markets()

    g=get_json(URL["cg_global"], quiet_401=True) or {}
    gd=g.get("data",{}) if isinstance(g,dict) else {}
    cp=get_json(URL["cp_global"]) or {}
    total_raw = gd.get("total_market_cap",{}).get("usd") if gd else cp.get("market_cap_usd")
    dom_raw = gd.get("market_cap_percentage",{}).get("btc") if gd else cp.get("bitcoin_dominance_percentage")
    total=f(total_raw,None) if total_raw is not None else None
    btcdom=f(dom_raw,None) if dom_raw is not None else None

    # Market breadth: require enough real observations; a missing API is N/A, not zero.
    top=get_json(URL["cg_markets"],{"vs_currency":"usd","order":"market_cap_desc","per_page":10,"page":1,"sparkline":"false"}, quiet_401=True) or []
    if not top:
        cp_top=get_json("https://api.coinpaprika.com/v1/tickers",{"start":1,"limit":10,"quotes":"USD"}) or []
        top=[{"market_cap":f(q.get("quotes",{}).get("USD",{}).get("market_cap"),None)} for q in cp_top if isinstance(q,dict)]
    caps=[f(q.get("market_cap"),None) for q in top if isinstance(q,dict)]
    caps=[v for v in caps if v is not None and v>0]
    top10=sum(caps) if caps else None
    others=max(total-top10,0) if total is not None and top10 is not None else None
    od=(others/total*100) if total and others is not None else None

    m50=get_json(URL["cg_markets"],{"vs_currency":"usd","order":"market_cap_desc","per_page":50,"page":1,"sparkline":"false","price_change_percentage":"7d,30d,90d"}, quiet_401=True) or []
    btcrow=next((q for q in m50 if isinstance(q,dict) and q.get("id")=="bitcoin"),{})
    btc30=f(btcrow.get("price_change_percentage_30d_in_currency"),None) if btcrow.get("price_change_percentage_30d_in_currency") is not None else None
    btc90=f(btcrow.get("price_change_percentage_90d_in_currency"),None) if btcrow.get("price_change_percentage_90d_in_currency") is not None else None
    btc_usd=f(btcrow.get("current_price"),None) if btcrow.get("current_price") is not None else None
    adv=cnt=0
    btc90_reference=btc90
    if btc90_reference is not None:
        for q in m50:
            if not isinstance(q,dict) or q.get("id") in {"bitcoin","tether","usd-coin","binance-usd","dai","true-usd","first-digital-usd","ethena-usde"}:
                continue
            change=f(q.get("price_change_percentage_90d_in_currency"),None) if q.get("price_change_percentage_90d_in_currency") is not None else None
            if change is not None:
                cnt+=1
                adv += change > btc90_reference
    # Do not call a thin partial sample a valid altseason index.
    alt=(adv/cnt*100) if cnt>=30 else None

    fg=get_json(URL["fng"]) or {}; fv=fl=None
    try:
        fv=f(fg["data"][0]["value"],None)
        fl=fg["data"][0].get("value_classification","UNKNOWN")
    except (KeyError, IndexError, TypeError):
        pass

    # Stablecoin endpoint fallback + schema/date/freshness validation.
    stable_rows, stable_source = stablecoin_history()
    stable=stable30=stable_date=stable_age_days=None
    try:
        x=pd.DataFrame(stable_rows)
        if not x.empty and "date" in x.columns and "totalCirculatingUSD" in x.columns:
            x["date"]=parse_mixed_datetime(x["date"])
            def stable_value(v):
                if isinstance(v,dict):
                    return f(v.get("peggedUSD"),None)
                return f(v,None)
            x["stable_value"]=x["totalCirculatingUSD"].apply(stable_value)
            x=x.replace([np.inf,-np.inf],np.nan).dropna(subset=["date","stable_value"]).sort_values("date")
            if len(x):
                last=x.iloc[-1]
                age=(pd.Timestamp.now(tz="UTC")-last["date"]).total_seconds()/86400
                stable_age_days=round(age,1)
                stable_date=last["date"].strftime("%Y-%m-%d")
                if 0 <= age <= 10:
                    stable=float(last["stable_value"])
                    prior=x[x["date"] <= last["date"]-pd.Timedelta(days=30)]
                    if not prior.empty and float(prior.iloc[-1]["stable_value"])>0:
                        if (last["date"]-prior.iloc[-1]["date"]).days >= 23 and (last["date"]-prior.iloc[-1]["date"]).days <= 37:
                            stable30=pc(stable,float(prior.iloc[-1]["stable_value"]))
                else:
                    log.warning("Stablecoin chart stale (%s; %.1f hari); hasil diset N/A.",stable_date,age)
    except Exception as e:
        log.warning("Stablecoin history parse failed: %s", type(e).__name__)
        stable=stable30=None

    dx=get_json(URL["dex"],{"excludeTotalDataChart":"true","excludeTotalDataChartBreakdown":"true"}) or {}
    eth=binance_ethbtc_metrics()
    dxy=yahoo("DX-Y.NYB"); gold=yahoo("GC=F")
    y10_fred=fred("DGS10"); dff=fred("DFF"); m2=fred("M2SL")
    y10_yahoo=yahoo("^TNX")

    dxy_info=_fresh_series_info(dxy,max_age_days=10,min_value=1,max_value=300)
    dxyv=dxy_info["value"]
    dxy30=_period_change(dxy,30) if dxyv is not None else None
    gold_info=_fresh_series_info(gold,max_age_days=10,min_value=100,max_value=100000)
    goldv=gold_info["value"]
    gold30=_period_change(gold,30) if goldv is not None else None

    y10_fred_info=_fresh_series_info(y10_fred,max_age_days=10,min_value=0,max_value=20)
    y10_yahoo_info=_fresh_series_info(y10_yahoo,max_age_days=10,min_value=0,max_value=20)
    y10_fred_v=y10_fred_info["value"]
    y10_yahoo_v=y10_yahoo_info["value"]
    if y10_fred_v is not None and y10_yahoo_v is not None and abs(y10_fred_v-y10_yahoo_v)>1.0:
        y10v=y10_yahoo_v; y10_source="Yahoo Finance ^TNX (fallback; mismatch with FRED)"; y10_series=y10_yahoo; y10_info=y10_yahoo_info
        log.warning("US10Y source mismatch: FRED %.3f%% vs Yahoo ^TNX %.3f%%; using Yahoo proxy.",y10_fred_v,y10_yahoo_v)
    elif y10_fred_v is not None:
        y10v=y10_fred_v; y10_source="FRED DGS10"; y10_series=y10_fred; y10_info=y10_fred_info
    elif y10_yahoo_v is not None:
        y10v=y10_yahoo_v; y10_source="Yahoo Finance ^TNX fallback"; y10_series=y10_yahoo; y10_info=y10_yahoo_info
    else:
        y10v=None; y10_source="N/A"; y10_series=pd.DataFrame(); y10_info={"date":None,"age_days":None,"fresh":False,"value":None}
    y10d=_series_delta(y10_series,steps=20) if y10v is not None else None

    dff_info=_fresh_series_info(dff,max_age_days=10,min_value=0,max_value=20)
    dffv=dff_info["value"]
    dffd=_series_delta(dff,steps=20) if dffv is not None else None
    m2_info=_fresh_series_info(m2,max_age_days=100,min_value=0,max_value=1000000)
    m2v=m2_info["value"]
    # M2 is monthly. Compare to the immediately previous observation only when
    # the dates are plausibly one month apart; never mislabel a long-gap change as monthly.
    m2_gap_days=None
    if m2v is not None and len(m2)>1:
        m2_gap_days=(pd.to_datetime(m2.iloc[-1].time,utc=True)-pd.to_datetime(m2.iloc[-2].time,utc=True)).total_seconds()/86400
    m230=(pc(float(m2.iloc[-1].close),float(m2.iloc[-2].close))
          if m2v is not None and len(m2)>1 and m2_gap_days is not None and 20<=m2_gap_days<=50
          and f(m2.iloc[-2].close,None) not in (None,0) else None)
    m2_usd=(m2v*1e9) if m2v is not None else None

    dxy_source="Yahoo Finance DX-Y.NYB"
    data_meta={
        "dxy":{"source":dxy_source,"date":dxy_info.get("date"),"age_days":dxy_info.get("age_days"),"fresh":dxy_info.get("fresh")},
        "us10y":{"source":y10_source,"date":y10_info.get("date"),"age_days":y10_info.get("age_days"),"fresh":y10_info.get("fresh")},
        "dff":{"source":"FRED DFF CSV/API","date":dff_info.get("date"),"age_days":dff_info.get("age_days"),"fresh":dff_info.get("fresh")},
        "m2":{"source":"FRED M2SL CSV/API","date":m2_info.get("date"),"age_days":m2_info.get("age_days"),"fresh":m2_info.get("fresh")},
        "stable":{"source":stable_source,"date":stable_date,"age_days":stable_age_days,"fresh":stable is not None},
        "ethbtc":{"source":eth.get("source","N/A"),"date":eth.get("date"),"fresh":eth.get("price") is not None},
    }
    return {
        "total":total,"btcdom":btcdom,"btcdom30":None,"btcdom_source":"CoinGecko global / CoinPaprika fallback",
        "others":others,"othersd":od,"alt":alt,"alt_sample":cnt,"fg":fv,"fl":fl,
        "stable":stable,"stable30":stable30,"stable_source":stable_source,"stable_date":stable_date,
        "dex24":f(dx.get("total24h"),None) if dx.get("total24h") is not None else None,
        "dex7":f(dx.get("total7d"),None) if dx.get("total7d") is not None else None,
        "ethbtc":eth.get("price"),"ethbtc30":eth.get("change30"),"ethbtc_source":eth.get("source","N/A"),
        "dxy":dxyv,"dxy30":dxy30,"gold":goldv,"gold30":gold30,
        "y10":y10v,"y10d":y10d,"dff":dffv,"dffd":dffd,
        "m2":m2_usd,"m2_observation":m2v,"m230":m230,"btcusd":btc_usd,"btc30":btc30,"btc90":btc90,
        "fred_key":bool(FRED_API_KEY),"us10y_source":y10_source,"fund_source":CG_MARKET_SOURCE,
        "data_meta":data_meta,
    }

def macro_data_quality(m):
    """Quality of the four requested macro series' usable 30D/rolling changes."""
    names=("dxy30","y10d","dffd","m230")
    available=sum(m.get(k) is not None and np.isfinite(f(m.get(k),float("nan"))) for k in names)
    total=len(names)
    status="VALID" if available==total else "CAUTION" if available>=2 else "INVALID DATA"
    return status,available,total


def rotation_data_quality(m):
    names=("alt","btcdom30","ethbtc30","stable30")
    available=sum(m.get(k) is not None and np.isfinite(f(m.get(k),float("nan"))) for k in names)
    total=len(names)
    status="VALID" if available==total else "CAUTION" if available>=2 else "INVALID DATA"
    return status,available,total


def macro_score(m):
    """Normalize only observed component scores; missing data is not a bearish zero."""
    scores=[]; notes=[]
    v=m.get("dxy30")
    if v is not None:
        score=5 if v < -2 else 4 if v < 0 else 3 if v <= 2 else 1
        scores.append(score); notes.append("DXY melemah kuat" if v < -2 else "DXY melemah" if v < 0 else "DXY relatif stabil" if v <= 2 else "DXY menguat (headwind)")
    else: notes.append("DXY 30D N/A; tidak diberi nilai bearish/bullish")
    v=m.get("y10d")
    if v is not None:
        score=5 if v < -0.15 else 3 if v <= 0.15 else 1
        scores.append(score); notes.append("US10Y turun" if v < -0.15 else "US10Y relatif stabil" if v <= 0.15 else "US10Y naik (headwind)")
    else: notes.append("US10Y perubahan N/A; komponen tidak dinilai")
    v=m.get("dffd")
    if v is not None:
        score=5 if v < -0.25 else 3 if abs(v) <= 0.10 else 1
        scores.append(score); notes.append("Fed DFF turun" if v < -0.25 else "Fed DFF relatif stabil" if abs(v) <= 0.10 else "Fed DFF naik (headwind)")
    else: notes.append("Fed DFF perubahan N/A; komponen tidak dinilai")
    v=m.get("m230")
    if v is not None:
        score=5 if v > 1 else 4 if v > 0 else 2 if v >= -1 else 1
        scores.append(score); notes.append("US M2 tumbuh" if v > 0 else "US M2 menurun")
    else: notes.append("US M2 perubahan bulanan N/A; komponen tidak dinilai")
    if not scores:
        return None,"DATA INCOMPLETE",notes
    # Too little evidence means no aggregate score; do not publish a misleading partial score.
    quality,available,total=macro_data_quality(m)
    notes.append(f"Kelengkapan Macro Score: {available}/{total} komponen — {quality}; data hilang tidak dianggap bearish")
    if available < 2:
        return None,"DATA INCOMPLETE",notes
    # Each available input is scored 0–5, then normalized to 0–25 across observed inputs.
    s=round((sum(scores)/len(scores))*5,1)
    regime="STRONG RISK-ON" if s>=19 else "BULLISH" if s>=15 else "NEUTRAL / TRANSITION" if s>=10 else "CAUTIOUS" if s>=6 else "RISK-OFF"
    return s,regime,notes


def rotation_score(m):
    """Score observed rotation components only; missing BTC.D/alt data is not treated as zero."""
    points=[]; capacity=0; component_count=0; notes=[]
    v=m.get("alt")
    if v is not None:
        component_count+=1; capacity+=5; points.append(5 if v>=75 else 3 if v>=60 else 1 if v>=50 else 0)
        notes.append(f"Altseason breadth {v:.0f}/100 (sampel {m.get('alt_sample',0)})")
    else: notes.append("Altseason Index N/A/coverage kurang; rotasi belum lengkap")
    v=m.get("btcdom30")
    if v is not None:
        component_count+=1; capacity+=3; points.append(3 if v < -1 else 2 if v < -0.25 else 0)
        notes.append(f"BTC.D perubahan 30D {v:+.2f} poin persentase")
    else: notes.append("Perubahan BTC Dominance 30D N/A; tidak diasumsikan netral/positif")
    v=m.get("ethbtc30")
    if v is not None:
        component_count+=1; capacity+=3; points.append(3 if v>5 else 2 if v>0 else 1 if v>=-3 else 0)
        notes.append(f"ETH/BTC 30D {v:+.2f}%")
    else: notes.append("Tren ETH/BTC 30D N/A; komponen rotasi tidak dinilai")
    v=m.get("stable30")
    if v is not None:
        component_count+=1; capacity+=2; points.append(2 if v>2 else 1 if v>0 else 0)
        notes.append("Stablecoin liquidity bertambah" if v>0 else "Stablecoin market cap tidak bertambah")
    else: notes.append("Stablecoin change 30D N/A/stale; komponen tidak dinilai")
    if capacity<=0 or component_count < 2:
        notes.append(f"Rotation Score N/A: hanya {component_count}/4 komponen tersedia")
        return None,notes
    raw=sum(points)
    return round(raw/capacity*15,1),notes

# ------------------------- FUNDAMENTAL / TOKENOMICS -------------------------
# CoinGecko individual /coins/{id} calls are intentionally avoided by default.
# The old design made one request per coin and could trigger HTTP 429; FET also
# returned 404 on the old hard-coded ID in some runs. The current CoinGecko
# /coins/markets endpoint supports symbol lookup, so we fetch the whole target
# set in one request and cache the result.
CG_MARKET_CACHE = {}
CG_MARKET_CACHE_LOADED = False
CG_MARKET_SOURCE = "N/A"

def _cg_market_row(q):
    if not isinstance(q,dict):
        return {}
    rank_raw=q.get("market_cap_rank")
    if rank_raw is None:
        rank_raw=q.get("market_cap_rank_with_rehypothecated")
    return {
        "name": q.get("name"),
        "id": q.get("id"),
        "symbol": str(q.get("symbol","")).upper(),
        "rank": f(rank_raw,9999),
        "mcap": f(q.get("market_cap")),
        "fdv": f(q.get("fully_diluted_valuation")),
        "circ": f(q.get("circulating_supply")),
        "total": f(q.get("total_supply")),
        "max": f(q.get("max_supply")),
        "ath": f(q.get("ath")),
        "ath_date": q.get("ath_date"),
        # These fields are not exposed by /coins/markets. Keep them absent
        # instead of inventing developer/community data.
        "dev": None,
        "comm": None,
        "commits": None,
    }

def cg_bulk_markets():
    """Ambil metadata pasar CoinGecko secara batch; tidak bergantung pada daftar coin statis."""
    global CG_MARKET_CACHE_LOADED, CG_MARKET_SOURCE
    if CG_MARKET_CACHE_LOADED:
        return CG_MARKET_CACHE
    rows = []
    # Dua halaman @250 cukup untuk sebagian besar universe likuid; hindari query symbols raksasa.
    for page in (1, 2):
        d = get_json(URL["cg_markets"], {
            "vs_currency": "idr", "order": "market_cap_desc", "per_page": 250,
            "page": page, "sparkline": "false"
        }, quiet_401=True, quiet=True)
        if isinstance(d, list): rows.extend(d)
        if not isinstance(d, list) or len(d) < 250: break
    for q in rows:
        row = _cg_market_row(q)
        sym = row.get("symbol", "")
        if sym and sym not in CG_MARKET_CACHE:
            CG_MARKET_CACHE[sym] = row
    CG_MARKET_CACHE_LOADED = True
    CG_MARKET_SOURCE = "CoinGecko /coins/markets batch" if CG_MARKET_CACHE else "N/A"
    if not CG_MARKET_CACHE:
        log.info("CoinGecko bulk market data unavailable; fundamental/tokenomics fields may be N/A.")
    return CG_MARKET_CACHE

def cg_coin(sym):
    return cg_bulk_markets().get(sym,{})


def _onchain_get(url, params=None):
    """One low-timeout public request; optional on-chain failure must never stop screening."""
    try:
        response = DIRECT.get(url, params=params, timeout=ONCHAIN_HTTP_TIMEOUT)
        if response.status_code == 429:
            return None
        response.raise_for_status()
        obj = response.json()
        return obj if isinstance(obj, (dict, list)) else None
    except Exception:
        return None


def _normalise_name(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _pick_verified_contract(sym, name=None):
    """Resolve a source-backed EVM contract; never choose a contract from ticker alone.

    Priority: manually pinned project/reference entry, then exact CoinGecko coin ID from
    the existing market row. Dynamic candidates are only used if the coin name matches
    the market row and the public explorer later returns a matching token symbol.
    """
    sym_u = str(sym or "").upper().strip()
    cache_key = (sym_u, str(name or "").lower())
    if cache_key in ONCHAIN_TOKEN_CACHE:
        return ONCHAIN_TOKEN_CACHE[cache_key]

    rec = ONCHAIN_VERIFIED_CONTRACTS.get(sym_u)
    source_kind = "Pinned public project/reference entry"
    if not rec:
        market = cg_coin(sym_u) or {}
        coin_id = str(market.get("id") or "").strip()
        market_name = str(market.get("name") or name or "").strip()
        requested_name = str(name or market_name).strip()
        # CoinGecko bulk markets are symbol-keyed; reject collisions/name mismatch.
        if (not coin_id or not market_name or not requested_name
                or _normalise_name(market_name) != _normalise_name(requested_name)
                or not re.fullmatch(r"[a-z0-9-]+", coin_id.lower())):
            ONCHAIN_TOKEN_CACHE[cache_key] = None
            return None
        detail = _onchain_get(
            f"https://api.coingecko.com/api/v3/coins/{coin_id.lower()}",
            params={"localization":"false", "tickers":"false", "market_data":"false",
                    "community_data":"false", "developer_data":"false", "sparkline":"false"}
        )
        if not isinstance(detail, dict) or _normalise_name(detail.get("name")) != _normalise_name(market_name):
            ONCHAIN_TOKEN_CACHE[cache_key] = None
            return None
        platforms = detail.get("platforms") if isinstance(detail.get("platforms"), dict) else {}
        platform_chain_map = {
            "ethereum":"ethereum", "base":"base", "arbitrum-one":"arbitrum",
            "optimistic-ethereum":"optimism", "polygon-pos":"polygon", "xdai":"gnosis",
            "avalanche":"avalanche", "celo":"celo", "linea":"linea", "scroll":"scroll",
        }
        rec = None
        for platform, chain_name in platform_chain_map.items():
            address = str(platforms.get(platform) or "").strip()
            if address and re.fullmatch(r"0x[a-fA-F0-9]{40}", address) and chain_name in ONCHAIN_BLOCKSCOUT:
                rec = {"chain":chain_name, "contract":address, "token_name":detail.get("name") or market_name,
                       "source_url":f"https://www.coingecko.com/en/coins/{coin_id}",
                       "scope":f"{chain_name} contract from CoinGecko platform map; explorer symbol cross-check required"}
                source_kind = "CoinGecko platform map + public explorer cross-check"
                break
        if not rec:
            ONCHAIN_TOKEN_CACHE[cache_key] = None
            return None

    chain = str(rec.get("chain", "")).lower()
    explorer = ONCHAIN_BLOCKSCOUT.get(chain)
    contract = str(rec.get("contract", "")).strip()
    if not explorer or not re.fullmatch(r"0x[a-fA-F0-9]{40}", contract):
        ONCHAIN_TOKEN_CACHE[cache_key] = None
        return None
    result = {
        "chain": chain,
        "contract": contract,
        "token_name": rec.get("token_name", sym_u),
        "explorer": explorer,
        "identity": f"{source_kind}; token symbol must match explorer; scope limited to this chain/contract",
        "contract_source_url": rec.get("source_url"),
        "scope": rec.get("scope", f"{chain} contract only"),
    }
    ONCHAIN_TOKEN_CACHE[cache_key] = result
    return result


def _address_fields(value):
    if isinstance(value, dict):
        address = (value.get("hash") or value.get("address_hash") or value.get("address")
                   or value.get("contract_address_hash") or "")
        label_parts = [value.get(k) for k in ("name", "ens_domain_name", "label", "public_tag", "metadata")]
        public_tags = value.get("watchlist_names") or value.get("public_tags") or []
        if isinstance(public_tags, list):
            label_parts.extend(public_tags)
        label = " ".join(str(v) for v in label_parts if v)
        is_contract = bool(value.get("is_contract", False) or value.get("is_contract_creation", False))
        return str(address).lower(), label, is_contract
    return str(value or "").lower(), "", False


def _is_excluded_onchain_address(label, is_contract=False):
    if is_contract:
        return True
    return bool(ONCHAIN_EXCLUDED_LABELS.search(str(label or "")))


def _holder_items(payload):
    if isinstance(payload, dict):
        return payload.get("items", []) if isinstance(payload.get("items", []), list) else []
    return payload if isinstance(payload, list) else []


def _transfer_amount(item, decimals):
    """Return token-unit amount from Blockscout v2 transfer shapes."""
    total = item.get("total") or item.get("amount") or {}
    if isinstance(total, dict):
        formatted = total.get("formatted")
        if formatted not in (None, ""):
            try: return abs(float(formatted))
            except (TypeError, ValueError): pass
        raw = total.get("value")
        dec = total.get("decimals", decimals)
    else:
        raw = total
        dec = decimals
    try:
        return abs(float(raw)) / (10 ** int(dec or 0))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _transfer_timestamp(item):
    raw = item.get("timestamp") or (item.get("block") or {}).get("timestamp") or item.get("timestamp_utc")
    try:
        ts = pd.to_datetime(raw, utc=True, errors="coerce")
        return None if pd.isna(ts) else ts
    except Exception:
        return None


def free_onchain_whale_check(sym, market_data=None):
    """Public EVM holder-flow check. It does not identify real-world owners or prove a purchase."""
    base = {
        "status": "N/A", "grade": "N/A", "summary": "Data on-chain gratis belum cukup untuk diverifikasi.",
        "chain": None, "contract": None, "explorer_url": None, "source": "Pinned token references + public Blockscout",
        "covered_30d": False, "events_30d": 0, "cohort_size": 0, "net_tokens": None,
        "net_supply_pct": None, "in_events": 0, "out_events": 0,
        "disclaimer": "Arus bersih ke alamat di kelompok holder atas bukan bukti pembelian, identitas whale, atau niat menahan aset; analisis hanya mencakup kontrak/chain yang ditampilkan."
    }
    if not ONCHAIN_ENABLED:
        base.update(status="NONAKTIF", summary="Pemeriksaan on-chain dimatikan via ZENA_ONCHAIN_ENABLED=0.")
        return base
    market_data = market_data if isinstance(market_data, dict) else {}
    name = market_data.get("name") or sym
    discovered = _pick_verified_contract(sym, name)
    if not discovered:
        base.update(status="N/A — kontrak belum dapat diverifikasi silang", summary="Tidak ditemukan kontrak EVM yang cocok dari daftar rujukan atau peta platform CoinGecko; ZENA tidak memilih kontrak hanya dari ticker. Token native/Solana/Sui dan chain yang belum didukung tetap N/A.")
        return base

    chain = discovered["chain"]
    contract = discovered["contract"]
    explorer = discovered["explorer"].rstrip("/")
    base.update(chain=chain, contract=contract, explorer_url=f"{explorer}/tokens/{contract}",
                contract_source_url=discovered.get("contract_source_url"),
                identity_note=discovered.get("identity"),
                scope=discovered.get("scope", f"{chain} contract only"))
    token = _onchain_get(f"{explorer}/tokens/{contract}")
    if not isinstance(token, dict):
        base.update(status="N/A — explorer tidak tersedia", summary="Explorer publik tidak memberi data token saat diperiksa.")
        return base
    observed_symbol = str(token.get("symbol", "")).upper().strip()
    if not observed_symbol:
        base.update(status="N/A — simbol explorer tidak tersedia", summary="Explorer tidak menampilkan simbol token; kontrak belum dapat diverifikasi silang sehingga data holder-flow tidak dipakai.")
        return base
    if observed_symbol != str(sym).upper():
        base.update(status="N/A — symbol mismatch", summary=f"Simbol kontrak di explorer ({observed_symbol}) tidak cocok dengan simbol market ({str(sym).upper()}); data tidak dipakai.")
        return base
    # Dynamic addresses are accepted only after the public explorer's token symbol matches.
    decimals = int(f(token.get("decimals"), 18))
    if decimals < 0 or decimals > 36: decimals = 18

    holders_payload = _onchain_get(f"{explorer}/tokens/{contract}/holders", params={"items_count": 50})
    holders = _holder_items(holders_payload)
    if not holders:
        base.update(status="N/A — holder tidak tersedia", summary="Explorer tidak memberi daftar holder yang dapat dipakai.")
        return base
    cohort = []
    for holder in holders:
        addr_obj = holder.get("address") or holder.get("holder") or holder.get("address_hash") or ""
        address, label, is_contract = _address_fields(addr_obj)
        # Some API responses place the address and flags alongside the holder object.
        if not label:
            label = " ".join(str(holder.get(k) or "") for k in ("name", "label", "public_tag"))
        if holder.get("is_contract"):
            is_contract = True
        if not address.startswith("0x") or _is_excluded_onchain_address(label, is_contract):
            continue
        if address not in {x[0] for x in cohort}:
            cohort.append((address, label))
        if len(cohort) >= ONCHAIN_TOP_HOLDER_COHORT:
            break
    if len(cohort) < 5:
        base.update(status="N/A — holder EOA kurang", summary="Holder besar non-contract berlabel/terbaca kurang dari 5; tidak cukup untuk proksi.", cohort_size=len(cohort))
        return base
    cohort_set = {x[0] for x in cohort}

    net_tokens = 0.0
    in_events = 0
    out_events = 0
    events_30d = 0
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=30)
    covered = True
    transfer_api_failed = False
    transfer_pages = 0
    cohort_set = {x[0] for x in cohort}
    unique_transfers = {}

    # Blockscout ERC-20 transfer history is queried via each holder's token-transfer
    # endpoint with a token filter. The old /tokens/{contract}/transfers route is
    # unsuitable for ERC-20 history and often returns no usable pages.
    for holder_addr in cohort_set:
        params = {"token": contract, "type": "ERC-20", "items_count": 50}
        wallet_covered = False
        for page_no in range(ONCHAIN_MAX_TRANSFER_PAGES):
            payload = _onchain_get(f"{explorer}/addresses/{holder_addr}/token-transfers", params=params)
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                transfer_api_failed = True
                wallet_covered = False
                break
            items = payload.get("items", [])
            transfer_pages += 1
            if not items:
                wallet_covered = True
                break

            oldest_ts = None
            for item in items:
                if not isinstance(item, dict):
                    continue
                ts = _transfer_timestamp(item)
                if ts is None:
                    continue
                oldest_ts = ts if oldest_ts is None or ts < oldest_ts else oldest_ts
                if ts < cutoff:
                    continue
                tx_hash = str(item.get("transaction_hash") or item.get("tx_hash") or item.get("hash") or "")
                log_index = str(item.get("log_index") or item.get("logIndex") or item.get("index") or "")
                from_addr, _, _ = _address_fields(item.get("from") or item.get("from_address") or "")
                to_addr, _, _ = _address_fields(item.get("to") or item.get("to_address") or "")
                amount = _transfer_amount(item, decimals)
                if amount <= 0:
                    continue
                # Deduplicate the same transfer returned for both sender and receiver.
                key = (tx_hash, log_index, from_addr, to_addr, f"{amount:.18g}")
                unique_transfers[key] = (ts, from_addr, to_addr, amount)

            if oldest_ts is not None and oldest_ts < cutoff:
                wallet_covered = True
                break
            next_page = payload.get("next_page_params")
            if not next_page:
                wallet_covered = True
                break
            if page_no + 1 >= ONCHAIN_MAX_TRANSFER_PAGES:
                wallet_covered = False
                break
            if not isinstance(next_page, dict):
                wallet_covered = False
                break
            params.update({k: v for k, v in next_page.items() if v is not None})
            time.sleep(0.08)
        if not wallet_covered:
            covered = False

    for ts, from_addr, to_addr, amount in unique_transfers.values():
        if ts < cutoff:
            continue
        events_30d += 1
        if to_addr in cohort_set:
            net_tokens += amount
            in_events += 1
        if from_addr in cohort_set:
            net_tokens -= amount
            out_events += 1

    total_supply = f(token.get("total_supply"), 0.0)
    # Blockscout total_supply may be raw integer string; convert using token decimals.
    if total_supply > 0 and decimals > 0 and total_supply > 10 ** (decimals + 3):
        total_supply /= 10 ** decimals
    supply_pct = (net_tokens / total_supply * 100) if total_supply > 0 else None
    base.update({"events_30d": events_30d, "cohort_size": len(cohort), "net_tokens": net_tokens,
                 "net_supply_pct": supply_pct, "in_events": in_events, "out_events": out_events,
                 "covered_30d": covered, "pages_read": transfer_pages,
                 "token_name": token.get("name") or discovered.get("token_name"),
                 "identity_note": discovered.get("identity"),
                 "contract_source_url": discovered.get("contract_source_url"),
                 "scope": discovered.get("scope", f"{chain} contract only")})
    if transfer_pages == 0:
        base.update(status="N/A — data transfer tidak tersedia", grade="N/A",
                    summary="Explorer tidak mengembalikan halaman transfer yang dapat dibaca; tidak ada kesimpulan arus whale.")
    elif transfer_api_failed or not covered:
        base.update(status="DATA PARSIAL", grade="C",
                    summary=f"Riwayat transfer 30D belum lengkap ({transfer_pages} halaman alamat); angka arus tidak dipakai sebagai sinyal akumulasi.")
    elif events_30d < 5 or (in_events + out_events) < 3:
        base.update(status="DATA TERBATAS", grade="C", summary="Transaksi yang berhubungan dengan holder besar terlalu sedikit untuk kesimpulan.")
    elif supply_pct is None:
        base.update(status="DATA TERBATAS", grade="C", summary="Suplai token tidak tersedia; net flow tidak dapat dibandingkan secara proporsional.")
    elif supply_pct >= 0.02:
        base.update(status="ARUS BERSIH MASUK", grade="B", summary=f"Transfer bersih menuju kelompok holder besar: +{supply_pct:.4f}% dari suplai (bukan bukti pembelian).")
    elif supply_pct <= -0.02:
        base.update(status="ARUS BERSIH KELUAR", grade="B", summary=f"Transfer bersih keluar dari kelompok holder besar: {supply_pct:.4f}% dari suplai (bukan bukti penjualan).")
    else:
        base.update(status="ARUS CAMPURAN", grade="C", summary=f"Arus bersih holder besar kecil: {supply_pct:+.4f}% dari suplai; belum menunjukkan arah yang jelas.")
    return base


def onchain_short_label(oc):
    oc = oc if isinstance(oc, dict) else {}
    grade = oc.get("grade", "N/A")
    status = str(oc.get("status", "N/A"))
    if status.startswith("ARUS BERSIH MASUK"):
        return f"ONCHAIN B: arus masuk {oc.get('net_supply_pct', 0):+.3f}% supply"
    if status.startswith("ARUS BERSIH KELUAR"):
        return f"ONCHAIN B: arus keluar {oc.get('net_supply_pct', 0):+.3f}% supply"
    if status.startswith("ARUS CAMPURAN"):
        return f"ONCHAIN C: arus campuran {oc.get('net_supply_pct', 0):+.3f}% supply"
    return f"ONCHAIN {grade}: {status}"


def fund_score(d):
    # Robust 10-point fundamental proxy based on market-cap rank only when
    # detailed developer/community endpoints are unavailable. This keeps the
    # score deterministic and avoids treating missing data as a negative signal.
    r=d.get("rank",9999)
    if r<=20:return 10
    if r<=50:return 8
    if r<=100:return 6
    if r<=200:return 4
    if r<=500:return 2
    return 0

def token_score(sym,d):
    m,fdv,c,t=[f(d.get(k),0.0) for k in ("mcap","fdv","circ","total")]
    ratio=m/fdv if fdv>0 else None
    cr=c/t if t>0 else None
    unlock=UNLOCK_365D.get(sym,None)
    if ratio is None or cr is None: return 0,ratio,cr,unlock
    s=4 if ratio>=.90 else 3 if ratio>=.75 else 1 if ratio>=.55 else 0
    s+=3 if cr>=.85 else 2 if cr>=.65 else 0
    if unlock is not None:
        if unlock>20:s-=3
        elif unlock>10:s-=1
    return clamp(s,0,10),ratio,cr,unlock

# ------------------------- COIN SCORE / VERDICT -------------------------
def flow_score(t):
    if not t:return 0
    s=0;s+=6 if t["cmf"]>.10 else 4 if t["cmf"]>.03 else 2 if t["cmf"]>0 else -4 if t["cmf"]<-.05 else 0
    vr=t["vol"]/t["vs9"] if t["vs9"]>0 else 1;s+=4 if vr>1.5 else 3 if vr>1.15 else 1 if vr>.8 else 0
    s+=2 if t["obv_slope"]>0 else -1 if t["obv_slope"]<0 else 0;s+=3 if t["tsi"]>0 else -1
    return clamp(s,0,15)

def mtf_tf_score(z):
    if not z:return 0
    s=0; rsi=z.get("rsi",50)
    s += 1 if 50<=rsi<=70 else 0
    if z.get("macd",0)>z.get("macds",0): s+=1
    if z.get("mh",0)>0: s+=1
    if z.get("tsi",0)>0: s+=1
    if z.get("stoch_k",50)>z.get("stoch_d",50): s+=1
    return clamp(s,0,5)

def mtf_score(t):
    mtf=t.get("mtf",{}); weights={"1W":0.50,"3D":0.30,"1D":0.20}
    available=[(tf,w,mtf[tf]) for tf,w in weights.items() if tf in mtf]
    if not available:return 0
    raw=sum(mtf_tf_score(z)*w for _,w,z in available); wsum=sum(w for _,w,_ in available)
    return clamp(15*raw/(5*wsum),0,15)

def tech_score(t):
    if not t:return 0
    s=0
    s+=2 if t["p"]>t["s20"] else 0; s+=2 if t["p"]>t["s50"] else 0
    s+=1 if t["p"]>t["s100"] else 0; s+=1 if t["p"]>t["s200"] else 0
    s+=1 if t["s20"]>t["s50"]>t["s100"] else 0
    s+=mtf_score(t)*0.4666666667
    s+=2 if 50<=t["rsi"]<=68 else 1 if t["rsi"]<=75 else -2 if t["rsi"]>75 else 1
    return clamp(s,0,15)

def _buy_confirmations(sym, t, m, liq):
    """Return passed market/coin confirmations and explicit blockers for a BUY/DCA verdict."""
    blockers=[]
    btc30=m.get("btc30")
    if btc30 is None:
        blockers.append("tren BTC 30D N/A")
    elif btc30 < -10:
        blockers.append(f"BTC melemah {btc30:+.1f}% dalam 30D")
    btc90=m.get("btc90")
    if btc90 is not None and btc90 < -20:
        blockers.append(f"BTC melemah {btc90:+.1f}% dalam 90D")
    if btc90 is None:
        blockers.append("tren BTC 90D belum tersedia")
    ethbtc30=m.get("ethbtc30")
    if ethbtc30 is None:
        blockers.append("tren ETH/BTC 30D N/A")
    elif ethbtc30 < -3:
        blockers.append(f"ETH/BTC turun {ethbtc30:+.1f}% dalam 30D")
    if liq.get("median30",0) <= MIN_MEDIAN_30D_QUOTE_IDR or liq.get("median90",0) <= MIN_MEDIAN_90D_QUOTE_IDR:
        blockers.append("likuiditas belum lolos batas 30D dan 90D")
    mtf=t.get("mtf",{})
    d1=mtf.get("1D",{}); d3=mtf.get("3D",{})
    trend_ok=(bool(d1) and bool(d3) and d1.get("mh",0)>0 and d3.get("mh",0)>0 and
              t.get("p",0)>0 and t.get("s50",0)>0 and t.get("p",0)>=t.get("s50",0))
    if not trend_ok:
        blockers.append("sinyal koin belum kompak (MACD histogram 1D/3D dan harga vs SMA50)")
    return not blockers,blockers


def verdict(sym,total,macro_reg,t,vw30,event,gate,cycle,m,liq,quality=0,accumulation=None,dca_zone=None):
    """Pre-reversal DCA policy: macro changes purchase tempo; it is not a buy veto."""
    if event[0] == "CRITICAL":
        return "🚨 BLOCK DCA — RISIKO KRITIS", "Risiko/event kritis: jangan menambah posisi sampai ditinjau ulang."
    if event[0] == "HIGH":
        return "🛑 PAUSE DCA — REVIEW RISIKO", event[1]
    if gate.get("hard_fail"):
        return "🔴 TIDAK LAYAK DCA", "; ".join(gate.get("reasons", [])[:2]) or "Hard gate gagal."
    if gate.get("data_gap"):
        return "🟡 WATCHLIST — DATA FUNDAMENTAL", "; ".join(gate.get("reasons", [])[:2]) or "Validasi tokenomics/market-cap sebelum DCA."
    if not accumulation or accumulation.get("effective_score") is None or accumulation.get("coverage", 0) < 45:
        return "🟡 WATCHLIST — FLOW BELUM CUKUP", "Data flow/discount belum cukup untuk menilai akumulasi pra-reversal."

    acc = f(accumulation.get("effective_score"), 0)
    setup = f(accumulation.get("setup_score"), 0)
    quality = f(quality, 0)
    macro_quality = macro_data_quality(m)[0]
    # A poor macro regime intentionally does not veto DCA. It only reduces tempo.
    tempo = "tempo konservatif" if macro_reg in {"RISK-OFF", "CAUTIOUS", "INVALID DATA", "DATA INCOMPLETE"} or macro_quality != "VALID" else "tempo terukur"
    short_extended = bool(vw30 and f(t.get("p"), 0) > vw30 * 1.10)

    if quality >= 60 and acc >= 62 and setup >= 60:
        if short_extended:
            return "🟡 CALON DCA — TUNGGU RETEST", f"Kualitas dan akumulasi proxy menarik; harga terlalu jauh di atas VWAP30D. Saat masuk kembali ke zona, gunakan {tempo}. Whale on-chain belum terverifikasi."
        return "🟢 DCA AWAL PRA-REVERSAL", f"Kandidat lolos hard gate; kualitas proxy {quality:.0f}/100 dan akumulasi proxy {acc:.0f}/100. Makro {macro_reg}: {tempo}, tidak perlu menunggu reversal penuh. Whale on-chain belum terverifikasi."
    if quality >= 52 and acc >= 50 and setup >= 52:
        return "🟡 DCA MIKRO / BANGUN POSISI", f"Sinyal awal cukup tetapi belum kuat; gunakan {tempo}, cicil kecil dan evaluasi tiap periode. Whale on-chain belum terverifikasi."
    if quality >= 60 and acc < 50:
        return "👀 KUALITAS ADA — TUNGGU FLOW", "Kualitas proxy cukup, tetapi akumulasi belum meyakinkan; jangan menyamakan harga turun dengan akumulasi."
    return "🟠 WATCHLIST PRA-CYCLE", f"Belum memenuhi kombinasi kualitas + akumulasi. Investment Score {total:.0f}/100; Quality Proxy {quality:.0f}/100; Accumulation Proxy {acc:.0f}/100."


def event(sym):
    d=EVENT_RISK_OVERRIDES.get(sym,{})
    return str(d.get("level","NONE")).upper(),str(d.get("reason","No manual event override"))

def analyze(sym,price,hist,source,m,btc_hist):
    t=tech(hist)
    if not t:
        return {"sym":sym,"price":price,"histdays":len(hist),"source":source,"score":0,
                "quality_score":0,"setup_score":0,"recovery":{"score":0,"label":"DATA TIDAK CUKUP","ath_multiple":None,"ath_drawdown_pct":None,"fundamental_status":"Belum diverifikasi"},
                "accumulation":{"effective_score":None,"score":None,"coverage":0,"label":"DATA FLOW TIDAK CUKUP","whale_status":"N/A — periksa data on-chain di shortlist"},
                "onchain":{"status":"N/A","grade":"N/A","summary":"Data teknikal tidak cukup; on-chain diperiksa hanya untuk shortlist/holding."},
                "dca_zone":{"zone":"DATA TIDAK CUKUP","ladder":{"now":None,"minus8":None,"minus15":None},"invalidation":"Data history kurang; jangan DCA otomatis."},
                "ver":"🟡 WATCHLIST / DATA GAP","reason":"Technical history tidak cukup."}
    t["p"]=price if price>0 else t.get("p",0)
    cmfs = cmf_multi_metrics(hist)
    flow_pattern = cmf_pattern(hist, cmfs, t)
    ms_raw,mreg,_=macro_score(m); rot_raw,_=rotation_score(m)
    d=cg_coin(sym); tokq=tokenomics_quality(d); liq=liquidity_metrics(hist)
    vws=vwap_set(hist); cycle=cycle_metrics(hist,vws)
    rel=relative_strength_metrics(hist,btc_hist); dd=drawdown_metrics(hist)
    fscore=fund_score(d); ts,fdvr,cr,un=token_score(sym,d); th=tech_score(t); fs=flow_score(t)
    ev=event(sym); gate=universe_gate(sym,hist,d,liq,tokq,ev)
    fg=m.get("fg"); sent=5 if fg is not None and 45<=fg<=70 else 3 if fg is not None and fg<=80 else 1
    # Macro/Rotation are informative only; investment score is not gated by macro regime.
    scored_raw = (cycle["score"] + liq["score"] + fs*(10/15) + fscore + ts +
                  th*(10/15) + rel["score"] + sent)
    total=clamp((scored_raw / 75.0) * 100.0, 0, 100)
    vw30=vws.get("30D",0); vd=(price/vw30-1)*100 if vw30 else None
    quality, quality_parts = quality_proxy_score(sym, d, tokq, liq, len(hist), ev)
    accumulation = accumulation_metrics(t, cmfs, cycle, dd, hist)
    setup_score = round(0.55 * quality + 0.45 * f(accumulation.get("effective_score"), 0), 1)
    accumulation["setup_score"] = setup_score
    dca_zone = dca_zone_metrics(price, cycle, vw30, dd, accumulation)
    recovery = recovery_potential_metrics(price, d, quality, accumulation, rel, gate, liq, ev, cycle, hist=hist, sym=sym)
    ver,reason=verdict(sym,total,mreg,t,vw30,ev,gate,cycle,m,liq,quality,accumulation,dca_zone)
    role=investment_role(gate,total,len(hist))
    if ver.startswith("🟢 DCA") or ver.startswith("🟡 DCA MIKRO"):
        role="🟢 PRA-REVERSAL DCA CANDIDATE"
    elif ver.startswith("🟡 CALON DCA"):
        role="🟡 DCA CANDIDATE — RETEST"
    elif ver.startswith("🚨") or ver.startswith("🛑") or ver.startswith("🔴 TIDAK"):
        role="🔴 NO DCA — RISK GATE"
    daily=t.get("mtf",{}).get("1D",{})
    entry=("EXTENDED — DON'T CHASE" if daily.get("rsi",t.get("rsi",0))>75 or (vd is not None and vd>10)
           else "FAIR / TREND ENTRY" if t.get("p",0)>t.get("s50",0) and daily.get("macd",t.get("macd",0))>daily.get("macds",t.get("macds",0))
           else "WAIT FOR CONFIRMATION")
    loss=("LOW" if d.get("rank",9999)<=20 and (fdvr or 1)>=.75
          else "MEDIUM" if d.get("rank",9999)<=100 and (fdvr or 0)>=.60 and (cr or 0)>=.50
          else "HIGH" if d.get("rank",9999)<=300 else "VERY HIGH")
    if ev[0] in {"HIGH","CRITICAL"}: loss=ev[0]
    ath=d.get("ath",0); adjusted_ath=None; dilution_status="N/A"
    peak_mcap=REFERENCE_PEAK_MKTCAP_IDR.get(sym,0); circ=f(d.get("circ"),0)
    if peak_mcap>0 and circ>0:
        adjusted_ath=peak_mcap/circ
        dilution_ratio=(ath/adjusted_ath) if adjusted_ath>0 and ath>0 else 1
        dilution_status=("🟢 MINIMAL DILUTION" if dilution_ratio<=1.15 else
                          "🟡 MODERATE DILUTION" if dilution_ratio<=1.75 else
                          "🔴 HIGH DILUTION / VALUE-TRAP RISK")
    market_phase = market_cycle_phase(hist, t, cycle, cmfs)
    return {
        "sym":sym,"price":price,"histdays":len(hist),"source":source,"fund_source":m.get("fund_source","N/A"),
        "market_phase":market_phase,
        "t":t,"macro":ms_raw,"mreg":mreg,"rot":rot_raw,"macro_quality":macro_data_quality(m)[0],"cycle":cycle,"liquidity":liq,"flow":fs,
        "fund":fscore,"tok":ts,"tech":th,"sent":sent,"rel":rel,"drawdown":dd,"score":total,
        "vwap":vws,"vw30":vw30,"vd":vd,"fdvr":fdvr,"cr":cr,"unlock":un,"event":ev,
        "gate":gate,"role":role,"ver":ver,"reason":reason,"entry":entry,"loss":loss,
        "quality_score":quality,"quality_parts":quality_parts,"accumulation":accumulation,
        "setup_score":setup_score,"recovery":recovery,"dca_zone":dca_zone,
        "market_cap_migration":recovery.get("market_cap_migration", {}),
        "ath":ath,"athpct":((ath/price)-1)*100 if ath>0 and price>0 else None,
        "adjusted_ath":adjusted_ath,"dilution_status":dilution_status,"tokenomics_quality":tokq,
        "cmf_multi":cmfs,"flow_pattern":flow_pattern
    }

def macro_report(m):
    """Ringkasan macro Telegram: kelompokkan data, tandai checklist, dan hindari dinding teks."""
    ms, reg, mn = macro_score(m)
    rs, rn = rotation_score(m)
    mq, mc, mt = macro_data_quality(m)
    rq, rc, rt = rotation_data_quality(m)

    def pct(value, suffix="%"):
        return f"{value:+.2f}{suffix}" if value is not None else "N/A"
    def val_usd(value):
        return usd(value) if value is not None and value > 0 else "N/A"
    def obs_line(label, key):
        return _meta_line(label, m.get("data_meta", {}).get(key, {}))

    btc = f"${m['btcusd']:,.0f}" if m.get("btcusd") else "N/A"
    btc_trend = f"30D {pct(m.get('btc30'))} | 90D {pct(m.get('btc90'))}"
    ethbtc = f"{m['ethbtc']:.5f}" if m.get("ethbtc") is not None else "N/A"
    ethbtc += f" ({pct(m.get('ethbtc30'))} 30D)" if m.get("ethbtc30") is not None else " (tren 30D N/A)"
    dom = f"{m['btcdom']:.2f}%" if m.get("btcdom") is not None and m.get("btcdom") > 0 else "N/A"
    dom += f" ({pct(m.get('btcdom30'), 'pp')} 30D)" if m.get("btcdom30") is not None else " (perubahan 30D N/A)"
    fg = f"{m['fg']:.0f} — {m.get('fl', 'N/A')}" if m.get("fg") is not None else "N/A"
    stable = val_usd(m.get("stable"))
    if m.get("stable30") is not None:
        stable += f" ({pct(m.get('stable30'))} 30D)"
    dxy = f"{m['dxy']:.2f}" if m.get("dxy") is not None else "N/A"
    if m.get("dxy30") is not None:
        dxy += f" ({pct(m.get('dxy30'))} 30D)"
    gold = f"${m['gold']:,.2f}" if m.get("gold") else "N/A"
    y10txt = f"{m['y10']:.2f}%" if m.get("y10") is not None else "N/A"
    dfftxt = f"{m['dff']:.2f}%" if m.get("dff") is not None else "N/A"
    btcgold = f"{m['btcusd']/m['gold']:.2f} oz" if m.get("btcusd") and m.get("gold") else "N/A"
    gate = ("🟢 Risk-on / alt expansion" if reg in {"STRONG RISK-ON", "BULLISH"} else
            "🟡 Transisi / selektif" if reg == "NEUTRAL / TRANSITION" else
            "🟠 Defensif / tunggu konfirmasi" if reg == "CAUTIOUS" else
            "🔴 Risk-off terukur" if reg == "RISK-OFF" else "⚪ Data makro belum cukup")
    macro_score_txt = f"{ms:.1f}/25" if ms is not None else "N/A"
    rotation_score_txt = f"{rs:.1f}/15" if rs is not None else "N/A"
    data_meta = m.get("data_meta", {})
    lines = [
        "🧭 *ZENA V3.6.3 | RINGKASAN MAKRO & TEMPO DCA*",
        f"🚦 *Kondisi pasar: {gate}*",
        f"📊 Kelengkapan data makro: *{mc}/{mt}* | Status: *{mq}* | Skor data tersedia: *{macro_score_txt}*",
        f"🔄 Kelengkapan rotasi: *{rc}/{rt}* | Status: *{rq}* | Skor data tersedia: *{rotation_score_txt}*",
        "",
        "*📌 GAMBARAN PASAR*",
        f"• BTC/USD: *{btc}* | Perubahan: {btc_trend}",
        f"• BTC Dominance: {dom}",
        f"• ETH/BTC: *{ethbtc}*",
        f"• Fear & Greed: *{fg}*",
        f"• Stablecoin: {stable}",
        f"• DEX volume: 24 jam {val_usd(m.get('dex24'))} | 7 hari {val_usd(m.get('dex7'))}",
        "",
        "*🌐 FAKTOR MAKRO*",
        f"• DXY: {dxy} | Emas: {gold} | BTC/Emas: {btcgold}",
        f"• US10Y: {y10txt}",
        f"• Fed rate (DFF): {dfftxt}",
        f"• US M2: {val_usd(m.get('m2'))}" + (f" ({pct(m.get('m230'))} bulanan)" if m.get("m230") is not None else " (perubahan N/A)"),
        "",
        "*✅ CHECKLIST PEMBACAAN*",
        f"{'✅' if m.get('btc30') is not None else '⚠️'} Tren BTC 30D tersedia: {pct(m.get('btc30'))}",
        f"{'✅' if m.get('ethbtc30') is not None else '⚠️'} Tren ETH/BTC 30D tersedia: {pct(m.get('ethbtc30'))}",
        f"{'✅' if mc == mt else '⚠️'} Data makro cukup: {mc}/{mt} komponen perubahan tersedia",
        f"{'✅' if rc == rt else '⚠️'} Data rotasi cukup: {rc}/{rt} komponen tersedia",
        "",
        "*📝 Catatan & tempo DCA*",
        f"Konteks: *{reg}*. Macro dan rotasi hanya mengatur kecepatan DCA; keduanya tidak masuk Investment Score dan bukan larangan beli otomatis.",
        "Sinyal yang hilang ditandai N/A, bukan dianggap netral atau bearish. Dalam risk-off, cicilan lebih konservatif dan bertahap; tetap evaluasi kualitas, likuiditas, tokenomics, dan risiko proyek.",
        "Transfer on-chain adalah proksi perpindahan token, bukan bukti bahwa whale membeli atau siapa pemilik dompet.",
        "",
        "*🕒 SUMBER & TANGGAL OBSERVASI*",
        obs_line("DXY", "dxy"), obs_line("US10Y", "us10y"),
        obs_line("Fed DFF", "dff"), obs_line("M2 FRED", "m2"),
        obs_line("ETH/BTC", "ethbtc"), obs_line("Stablecoin", "stable"),
        f"Sumber fundamental: {m.get('fund_source', 'N/A')}",
    ]
    lines += ["", "*Ringkasan makro:* " + ("; ".join(mn[:3]) if mn else "belum ada catatan yang dapat disimpulkan.")]
    lines += ["*Ringkasan rotasi:* " + ("; ".join(rn[:3]) if rn else "data rotasi belum cukup.")]
    return "\n".join(lines)



def market_cycle_phase(x, t=None, cycle=None, cmfs=None):
    """Heuristic BTC/ETH regime and Wyckoff-style phase labels from OHLCV; descriptive, not predictive."""
    t = t or {}
    cycle = cycle or {}
    cmfs = cmfs or {}
    if x is None or x.empty or len(x) < 220:
        return {"regime": "DATA TIDAK CUKUP", "phase": "BELUM TERKONFIRMASI", "confidence": "RENDAH", "reason": "Riwayat harian kurang untuk evaluasi tren panjang."}
    c = pd.to_numeric(x["close"], errors="coerce").dropna()
    if len(c) < 220:
        return {"regime": "DATA TIDAK CUKUP", "phase": "BELUM TERKONFIRMASI", "confidence": "RENDAH", "reason": "Data penutupan harian tidak cukup."}
    p = float(c.iloc[-1]); s50 = float(c.tail(50).mean()); s200 = float(c.tail(200).mean())
    r30 = period_return(x, 30); r90 = period_return(x, 90); r180 = period_return(x, 180)
    cmf30 = cmfs.get("30D")
    cmf90 = cmfs.get("90D")
    ratio4 = (cycle.get("ratios") or {}).get("4Y")
    # Market regime: require agreement between trend structure and medium-term returns.
    if r90 is not None and r90 >= 12 and p > s200 and s50 > s200:
        regime = "BULL MARKET"
    elif r90 is not None and r90 <= -15 and p < s200 and s50 < s200:
        regime = "BEAR MARKET"
    elif r90 is not None and -10 <= r90 <= 10 and abs(p / s50 - 1) <= 0.06:
        regime = "SIDEWAYS"
    else:
        regime = "TRANSISI / BELUM TERKONFIRMASI"
    # Independent phase label. Use OHLCV price structure/flow only as proxies, never as proof of whale activity.
    recent = x.tail(30)
    lows = pd.to_numeric(recent.get("low", pd.Series(dtype=float)), errors="coerce").dropna()
    shakeout = False
    if len(lows) >= 10 and len(c) >= 35:
        prior_low = float(pd.to_numeric(x["low"], errors="coerce").iloc[-31:-5].min()) if "low" in x else 0
        last_low = float(pd.to_numeric(x["low"], errors="coerce").iloc[-5:].min()) if "low" in x else 0
        shakeout = bool(prior_low > 0 and last_low < prior_low * 0.985 and p >= prior_low * 0.99 and (r30 is not None and r30 > -12))
    early_reversal = bool(r30 is not None and r30 > 3 and p > s50 and (r90 is None or r90 < 12) and (r180 is None or r180 < 25))
    below_value = (ratio4 is not None and ratio4 <= 0) or p < s200
    accumulation = bool(below_value and (regime in {"SIDEWAYS", "BEAR MARKET", "TRANSISI / BELUM TERKONFIRMASI"}) and
                        ((cmf30 is not None and cmf30 > 0) or (cmf90 is not None and cmf90 > 0) or (r30 is not None and r30 >= -5)))
    distribution = bool((p > s200 or (ratio4 is not None and ratio4 > 15)) and
                        ((cmf30 is not None and cmf30 < 0) or (r30 is not None and r30 < -5)) and
                        (r90 is None or r90 > -15))
    if shakeout:
        phase = "SHAKEOUT POTENSIAL"
        reason = "Harga sempat menembus low acuan lalu kembali mendekat; perlu konfirmasi penutupan dan volume."
    elif early_reversal:
        phase = "AWAL REVERSAL POTENSIAL"
        reason = "Return 30D positif dan harga di atas SMA50, tetapi tren panjang belum cukup kuat untuk menyatakan bull market."
    elif accumulation and not distribution:
        phase = "AKUMULASI POTENSIAL"
        reason = "Harga relatif di bawah nilai/tren panjang dengan sebagian tanda tekanan jual mereda; bukan bukti pasti pembelian institusi."
    elif distribution:
        phase = "DISTRIBUSI POTENSIAL"
        reason = "Harga relatif tinggi terhadap tren panjang, tetapi arus/momentum terbaru melemah; sinyal belum membuktikan penjualan pelaku besar."
    else:
        phase = "BELUM TERKONFIRMASI"
        reason = "Bukti akumulasi, distribusi, shakeout, atau reversal belum cukup selaras."
    signals = sum(v is not None for v in (r30, r90, r180, cmf30, cmf90)) + int(len(c) >= 200)
    confidence = "TINGGI" if signals >= 6 and regime in {"BULL MARKET", "BEAR MARKET", "SIDEWAYS"} else "SEDANG" if signals >= 4 else "RENDAH"
    return {"regime": regime, "phase": phase, "confidence": confidence,
            "return30": r30, "return90": r90, "return180": r180,
            "price_vs_sma50": (p / s50 - 1) * 100 if s50 else None,
            "price_vs_sma200": (p / s200 - 1) * 100 if s200 else None,
            "cmf30": cmf30, "cmf90": cmf90, "reason": reason}

def benchmark_report(benchmarks, m):
    """BTC and ETH market-reference report. These assets are benchmarks, not growth-shortlist entries."""
    lines = [
        "₿ *ZENA V3.6.3 | BTC & ETH SEBAGAI ACUAN PASAR*",
        "BTC dan ETH ditampilkan terpisah dari Top 10 growth; keduanya tetap dikecualikan dari daftar kandidat 5x–10x.",
        "",
    ]
    for sym in ("BTC", "ETH"):
        r = benchmarks.get(sym)
        if not r:
            lines += [f"*{sym}*", "⚠️ Data harga/riwayat tidak berhasil diambil.", ""]
            continue
        t = r.get("t") or {}
        hist_days = r.get("histdays", 0)
        d = cg_coin(sym) or {}
        rel = r.get("rel", {}).get("periods", {})
        vwap = r.get("vwap", {})
        mtf = t.get("mtf", {})
        cyc = r.get("cycle", {})
        dd = r.get("drawdown", {})
        ath_multiple = r.get("recovery", {}).get("ath_multiple")
        rank = d.get("rank")
        lines += [
            f"*{'🟠' if sym == 'BTC' else '🔷'} {sym} — BENCHMARK*",
            f"💵 Harga Indodax: *{idr(r.get('price', 0))}*",
            f"🏦 Market cap: {idr_compact(d.get('mcap', 0))} | Rank CoinGecko: {int(rank) if rank and rank < 9999 else 'N/A'}",
            "🧭 *FASE PASAR & SIKLUS*",
            f"• Kondisi: *{(r.get('market_phase') or {}).get('regime', 'N/A')}*",
            f"• Fase: *{(r.get('market_phase') or {}).get('phase', 'N/A')}* | Keyakinan: {(r.get('market_phase') or {}).get('confidence', 'RENDAH')}",
            f"• Return 30D/90D/180D: " + " / ".join((f"{(r.get('market_phase') or {}).get(k):+.1f}%" if (r.get('market_phase') or {}).get(k) is not None else "N/A") for k in ("return30", "return90", "return180")),
            f"• Posisi vs SMA50/SMA200: " + " / ".join((f"{(r.get('market_phase') or {}).get(k):+.1f}%" if (r.get('market_phase') or {}).get(k) is not None else "N/A") for k in ("price_vs_sma50", "price_vs_sma200")),
            f"• Alasan: {(r.get('market_phase') or {}).get('reason', 'N/A')}",
            "📈 *Perubahan harga*",
        ]
        for period in (30, 90, 180, 365):
            entry = rel.get(period, {})
            value = entry.get("coin")
            lines.append(f"• {period} hari: {value:+.1f}%" if value is not None else f"• {period} hari: N/A")
        lines += [
            "📊 *Peta VWAP*",
            f"• 4Y {idr(vwap.get('4Y', 0))} | 3Y {idr(vwap.get('3Y', 0))}",
            f"• 2Y {idr(vwap.get('2Y', 0))} | 1Y {idr(vwap.get('1Y', 0))}",
            f"• 180D {idr(vwap.get('180D', 0))} | 90D {idr(vwap.get('90D', 0))}",
            f"• 30D {idr(vwap.get('30D', 0))} | Zona: *{cyc.get('zone', 'N/A')}*",
            "🧭 *Tren teknikal multi-timeframe*",
        ]
        for tf, description in (("1W", "Mingguan"), ("3D", "Konfirmasi"), ("1D", "Timing")):
            z = mtf.get(tf)
            if z:
                lines.append(f"• {description} ({tf}): RSI {z.get('rsi', float('nan')):.1f} | MACD-H {z.get('mh', 0):+.4f} | TSI {z.get('tsi', 0):+.3f}")
            else:
                lines.append(f"• {description} ({tf}): N/A")
        cmf_multi = r.get("cmf_multi", {}) or {}
        lines.append("💧 *Arus pasar (CMF panjang/menengah)*: " + " | ".join(
            f"{k} {v:+.3f}" if v is not None else f"{k} N/A"
            for k, v in cmf_multi.items() if k in {"180D", "90D", "60D", "30D", "20D"}
        ))
        lines.append("💧 *CMF pendek*: " + " | ".join(
            f"{k} {v:+.3f}" if v is not None else f"{k} N/A"
            for k, v in cmf_multi.items() if k in {"14D", "10D", "7D", "3D", "1D"}
        ))
        lines.append(f"• RSI harian ringkas: {t.get('rsi', 0):.1f} | CMF20: {t.get('cmf', 0):+.3f} | OBV slope: {t.get('obv_slope', 0):+.0f}")
        ath_label = f"{ath_multiple:.2f}x dari harga kini" if ath_multiple is not None else "N/A"
        lines += [
            "✅ *CHECKLIST DATA*",
            f"{'✅' if hist_days >= 365 else '⚠️'} Riwayat: {hist_days} hari ({r.get('source', 'N/A')})",
            f"{'✅' if vwap.get('1Y') else '⚠️'} VWAP 1Y tersedia: {idr(vwap.get('1Y', 0))}",
            f"{'✅' if r.get('price', 0) > 0 else '⚠️'} Harga terkini tersedia: {idr(r.get('price', 0))}",
            f"📉 Jarak ke ATH historis: {ath_label}",
            "ℹ️ Ini adalah referensi pasar, bukan rekomendasi beli otomatis.",
            "",
        ]
        if sym == "ETH":
            ethbtc = f"{m.get('ethbtc'):.5f}" if m.get("ethbtc") is not None else "N/A"
            ethbtc30 = f"{m.get('ethbtc30'):+.2f}%" if m.get("ethbtc30") is not None else "N/A"
            lines.insert(len(lines) - 2, f"🔁 ETH/BTC: *{ethbtc}* | perubahan 30D: {ethbtc30}")
    lines += ["*Cara baca:* Bear/Bull/Sideways dinilai dari tren harga + SMA50/SMA200 + return 90D. Fase akumulasi/distribusi/shakeout/reversal adalah label heuristik terpisah dan perlu konfirmasi; CMF/OBV bukan bukti aktivitas whale. Gunakan BTC untuk kondisi pasar kripto utama dan ETH untuk kekuatan relatif ekosistem Ethereum."]
    return "\n".join(lines)

def coin_report(r):
    """Readable Telegram report: key decision first, then checks and technical detail."""
    sym = r.get("sym", "N/A")
    head = [
        f"🟡 *🪙 ZENA V3.6.3 | {sym}*",
    ]
    if not r.get("t"):
        head += [
            "*RINGKASAN*",
            f"🔵 💵 *HARGA: {idr(r.get('price', 0))}*",
            f"⚠️ Status: *DATA GAP* — {r.get('reason', 'Riwayat teknikal tidak cukup.')}",
            f"🔵 🔎 *ON-CHAIN: {onchain_short_label(r.get('onchain', {}))}*",
            "Tidak ada kesimpulan indikator teknikal karena data OHLCV tidak memadai.",
        ]
        return "\n".join(head)

    t = r.get("t", {})
    vws = r.get("vwap", {}) or {}
    mtf = t.get("mtf", {}) or {}
    gate = r.get("gate", {}) or {}
    liq = r.get("liquidity", {}) or {}
    cyc = r.get("cycle", {}) or {}
    rel = r.get("rel", {}) or {}
    dd = r.get("drawdown", {}) or {}
    acc = r.get("accumulation", {}) or {}
    rec = r.get("recovery", {}) or {}
    cm = r.get("market_cap_migration", {}) or {}
    dca = r.get("dca_zone", {}) or {}
    ladder = dca.get("ladder", {}) or {}
    cmfs = r.get("cmf_multi", {}) or {}
    flow_pattern = r.get("flow_pattern", {}) or {}
    on = r.get("onchain", {}) or {}
    tokq = r.get("tokenomics_quality", {}) or {}
    details = []

    def pct_text(value):
        return f"{value:+.1f}%" if value is not None else "N/A"
    def price_text(key):
        return idr(vws.get(key, 0)) if vws.get(key) else "N/A"
    def mtf_line(tf, purpose):
        z = mtf.get(tf)
        if not z:
            return f"⚠️ {purpose} ({tf}): data belum cukup"
        return (f"{'✅' if z.get('mh', 0) > 0 else '⚠️'} {purpose} ({tf}): "
                f"RSI {z.get('rsi', 0):.1f} | MACD-H {z.get('mh', 0):+.4f} | "
                f"TSI {z.get('tsi', 0):+.3f} | Stoch {z.get('stoch_k', 0):.1f}/{z.get('stoch_d', 0):.1f}")
    def rs_line(days):
        z = rel.get("periods", {}).get(days, {})
        if z.get("coin") is None or z.get("btc") is None or z.get("rs") is None:
            return f"• {days}D: data relatif belum cukup"
        return f"• {days}D: koin {z['coin']:+.1f}% | BTC {z['btc']:+.1f}% | relatif {z['rs']:+.1f}%"

    liq30 = f(liq.get("median30"), 0)
    liq90 = f(liq.get("median90"), 0)
    liquid_ok = liq30 > MIN_MEDIAN_30D_QUOTE_IDR and liq90 > MIN_MEDIAN_90D_QUOTE_IDR
    hard_ok = not gate.get("hard_fail", False)
    data_ok = not gate.get("data_gap", False)
    checklist_liq = "✅" if liquid_ok else "❌"
    checklist_gate = "✅" if hard_ok else "❌"
    checklist_data = "✅" if data_ok else "⚠️"
    ath_multiple = rec.get("ath_multiple")
    ath_line = f"{ath_multiple:.2f}x harga kini ({pct_text(rec.get('ath_drawdown_pct'))} dari ATH)" if ath_multiple is not None else "N/A"
    mcap_now = cm.get("current_mcap")
    mc5 = rec.get("mcap_5x")
    mc10 = rec.get("mcap_10x")
    rel365 = rec.get("rs365")
    avg_rs = rel.get("avg_rs")
    flow_windows_long = " | ".join(f"{k} {cmfs.get(k):+.3f}" if cmfs.get(k) is not None else f"{k} N/A"
                                    for k in ("180D", "90D", "60D", "30D", "20D"))
    flow_windows_short = " | ".join(f"{k} {cmfs.get(k):+.3f}" if cmfs.get(k) is not None else f"{k} N/A"
                                     for k in ("14D", "10D", "7D", "3D", "1D"))
    onchain_status = onchain_short_label(on)
    flow_note = flow_pattern.get("note", "Belum ada kesimpulan arus yang kuat.")
    fund_note = rec.get("fundamental_status", "Fundamental belum diverifikasi otomatis.")

    head += [
        "*📌 RINGKASAN KEPUTUSAN*",
        f"🔵 💵 *HARGA: {idr(r.get('price', 0))}*",
        f"🔵 🎯 *VERDICT: {r.get('ver', 'N/A')}*",
        f"🟡 📈 *SCORE INVESTASI: {r.get('score', 0):.0f}/100 | RECOVERY: {rec.get('score', 0):.0f}/100*",
        f"🧬 Quality: *{r.get('quality_score', 0):.0f}/100* | Akumulasi pasar: *{f(acc.get('effective_score'), 0):.0f}/100*",
        f"🧭 Zona: *{dca.get('zone', 'N/A')}* | Siklus: {cyc.get('zone', 'N/A')}",
        f"🔵 🐋 *ON-CHAIN: {onchain_status}*",
        f"📝 Kesimpulan singkat: {r.get('reason', 'Evaluasi data dan risiko sebelum menambah posisi.')}",
        "",
        "*✅ CHECKLIST SEBELUM DCA*",
        f"{checklist_gate} Hard gate: {gate.get('status', 'N/A')}",
        f"{checklist_liq} Likuiditas: 30D {idr(liq30)}/hari | 90D {idr(liq90)}/hari (wajib > Rp100 juta untuk keduanya)",
        f"{checklist_data} Kelengkapan data/tokenomics: {'cukup untuk screening' if data_ok else 'ada data yang harus diverifikasi'}",
        f"{'✅' if r.get('event', ('NONE', ''))[0] not in {'HIGH', 'CRITICAL'} else '🛑'} Risiko event: {r.get('event', ('NONE', ''))[0]}",
        "",
        "*💰 ZONA HARGA & VWAP*",
        f"• VWAP 4Y: {price_text('4Y')} | 3Y: {price_text('3Y')}",
        f"• VWAP 2Y: {price_text('2Y')} | 1Y: {price_text('1Y')}",
        f"• VWAP 180D: {price_text('180D')} | 90D: {price_text('90D')}",
        f"• VWAP 60D: {price_text('60D')} | 30D: {price_text('30D')} ({pct_text(r.get('vd'))} vs VWAP30D)",
        f"• Tahap DCA indikatif: 1) {idr(ladder.get('now', 0))}  2) {idr(ladder.get('minus8', 0))} (-8%)  3) {idr(ladder.get('minus15', 0))} (-15%)" if ladder.get("now") else "• Tahap DCA: N/A",
        "Catatan: level ini adalah tangga indikatif, bukan prediksi harga terendah.",
        "",
        "*📈 MOMENTUM TEKNIKAL*",
        mtf_line("1W", "Tren utama"),
        mtf_line("3D", "Konfirmasi"),
        mtf_line("1D", "Timing"),
        "",
        "*💧 ARUS PASAR & KEKUATAN RELATIF*",
        f"• CMF panjang/menengah: {flow_windows_long or 'N/A'}",
        f"• CMF pendek: {flow_windows_short or 'N/A'}",
        f"• CMF20: {t.get('cmf', 0):+.3f} | OBV slope: {t.get('obv_slope', 0):+.0f} | Volume/SMA9: {(t.get('vol', 0) / t.get('vs9', 1)):.2f}x",
        f"🔵 🧩 *POLA CMF: {flow_pattern.get('label', 'BELUM JELAS')}* — {flow_pattern.get('conclusion', 'Belum konklusif.')}",
        f"• Catatan flow: {flow_note}",
        rs_line(30), rs_line(90), rs_line(180), rs_line(365),
        f"• Rata-rata RS: {pct_text(avg_rs)} | RS365: {pct_text(rel365)}",
        "",
        "*🏛️ MARKET CAP & POTENSI PEMULIHAN*",
        f"• Market cap estimasi: {idr_compact(mcap_now)} | Kelas: {cm.get('current_tier', 'N/A')}",
        f"• Perpindahan kelas historis: {cm.get('migration_label', 'N/A')} (historis default adalah proxy, bukan market cap aktual)",
        f"• MCAP skenario 5x: {idr_compact(mc5)} | 10x: {idr_compact(mc10)} (suplai tetap, bukan target harga)",
        f"• Jarak ATH: {ath_line} | ATH bukan target otomatis",
        f"• Risiko pengenceran: {r.get('dilution_status', 'N/A')} | Circulating/total: {f(r.get('cr'), 0) * 100:.1f}%" if r.get("cr") is not None else f"• Risiko pengenceran: {r.get('dilution_status', 'N/A')} | Circulating/total: N/A",
        "",
        "*🔎 VALIDASI DATA & ON-CHAIN*",
        f"• Fundamental: {fund_note}",
        f"• Kualitas tokenomics: {'✅ data dasar tersedia' if tokq.get('complete') else '⚠️ ada data yang belum tersedia'} | Rank CoinGecko: {int(cg_coin(sym).get('rank', 9999)) if cg_coin(sym).get('rank', 9999) < 9999 else 'N/A'}",
        f"🔵 • *On-chain: {onchain_status}*",
        f"• Alasan/status: {on.get('summary', 'Data on-chain tidak tersedia.')}",
        f"• Jaringan/kontrak: {on.get('chain') or 'N/A'} | {on.get('explorer_url') or 'N/A'}",
        f"• Sumber kontrak: {on.get('contract_source_url') or 'N/A'}",
        f"• Riwayat harga: {r.get('histdays', 0)} hari via {r.get('source', 'N/A')} | Confidence: {'A' if r.get('histdays', 0) >= FULL_CYCLE_HISTORY_DAYS else 'B' if r.get('histdays', 0) >= CORE_HISTORY_DAYS else 'C'}",
        "",
        "*🧠 CARA MENGARTIKAN HASIL*",
        "✅ Checklist lolos berarti data/metrik terkait memenuhi aturan screening yang tertulis; bukan jaminan untung.",
        "⚠️ CMF/OBV dan arus transfer hanya petunjuk. Transfer ke dompet besar tidak membuktikan adanya pembelian whale.",
        "❗ Diskon besar atau ATH lama tidak cukup sendirian. Validasi penggunaan proyek, kompetisi, keamanan, unlock, dan sumber fundamental sebelum menambah posisi.",
    ]
    return "\n".join(head)

def discover_indodax_universe(tickers):
    """Daftar seluruh market IDR non-stablecoin, diurutkan berdasarkan turnover IDR 24 jam."""
    candidates = []
    for market, q in (tickers or {}).items():
        m = str(market).lower()
        if not m.endswith("_idr") or not isinstance(q, dict):
            continue
        sym = m[:-4].upper()
        if not sym or sym in STABLECOINS:
            continue
        last = f(q.get("last"), 0.0)
        turnover = f(q.get("vol_idr"), 0.0)
        if turnover <= 0:
            turnover = f(q.get("vol"), 0.0) * last
        if last <= 0:
            continue
        candidates.append((sym, turnover))
    candidates.sort(key=lambda z: z[1], reverse=True)
    return [sym for sym, _ in candidates[:MAX_ALL_COIN_SCAN]]


def parse_args():
    p=argparse.ArgumentParser(description="ZENA V3.6.3 — free-first on-chain holder-flow check + market-cap migration + recovery screening + Telegram")
    p.add_argument("--mode", choices=["full","shortlist","report"], default="full")
    p.add_argument("--no-telegram", action="store_true", help="Jalankan tanpa Telegram")
    p.add_argument("--test-telegram", action="store_true", help="Validasi token + chat ID dan kirim pesan test")
    p.add_argument("--telegram-token", default="", help="Override bot token tanpa menyimpan di environment")
    p.add_argument("--telegram-chat-id", default="", help="Override Telegram chat ID")
    return p.parse_args()

def main():
    global CG_MARKET_CACHE, CG_MARKET_CACHE_LOADED, CG_MARKET_SOURCE, TELEGRAM_BOT_TOKEN
    CG_MARKET_CACHE, CG_MARKET_CACHE_LOADED, CG_MARKET_SOURCE = {}, False, "N/A"
    args=parse_args()
    if args.telegram_token:
        TELEGRAM_BOT_TOKEN=args.telegram_token.strip()
    if args.telegram_chat_id:
        TELEGRAM_CHAT_ID=args.telegram_chat_id.strip()
    if args.no_telegram:
        TELEGRAM_BOT_TOKEN=""
        TELEGRAM_CHAT_ID=""

    if not args.no_telegram and not _telegram_configured():
        configure_telegram_interactive()

    if args.test_telegram:
        ok, detail = telegram_check(send_test=True)
        print(("✅ " if ok else "❌ ") + detail)
        if ok:
            return
        raise SystemExit(2)

    if args.mode in {"full","report"} and not args.no_telegram:
        # Do not silently run a Telegram-enabled scan when credentials are unavailable.
        # Interactive terminals already had a chance to enter them above.
        if not _telegram_configured():
            log.error("Telegram belum dikonfigurasi dan mode '%s' membutuhkan Telegram.", args.mode)
            log.error("Jalankan kembali di terminal lalu masukkan token + chat ID saat diminta, atau set ZENA_TELEGRAM_BOT_TOKEN dan ZENA_TELEGRAM_CHAT_ID.")
            log.error("Untuk screening tanpa Telegram gunakan: --no-telegram")
            raise SystemExit(2)
        ok, detail = telegram_check(send_test=False)
        if not ok:
            log.error("Telegram preflight gagal: %s", detail)
            log.error("Periksa token/chat ID lalu jalankan --test-telegram sebelum screening full.")
            raise SystemExit(2)
        log.info(detail)

    tick=indodax_tickers()
    usdt=f(tick.get("usdt_idr",{}).get("last"),16000)
    if usdt<=0: usdt=16000.0

    btc_hist,btc_src=history("BTC",usdt)
    m=macro()
    if args.mode in {"full","report"}:
        if not tg(macro_report(m), label="macro report"):
            log.warning("Macro report Telegram gagal; screening tetap dilanjutkan.")

    # BTC and ETH are reported as separate market benchmarks, not as growth candidates.
    benchmarks = {}
    for benchmark_sym in ("BTC", "ETH"):
        try:
            if benchmark_sym == "BTC":
                h_bench, src_bench = btc_hist, btc_src
            else:
                h_bench, src_bench = history("ETH", usdt)
            ticker_bench = indodax_ticker_entry(tick, benchmark_sym)
            p_bench = f(ticker_bench.get("last"), 0.0)
            if p_bench <= 0 and not h_bench.empty:
                p_bench = f(h_bench.iloc[-1].close, 0.0)
            if p_bench > 0:
                benchmarks[benchmark_sym] = analyze(benchmark_sym, p_bench, h_bench, src_bench, m, btc_hist)
                log.info("BENCHMARK DATA: %s | harga=%s | history=%s hari", benchmark_sym, idr(p_bench), len(h_bench))
            else:
                log.warning("BENCHMARK DATA GAP %s: harga IDR tidak tersedia.", benchmark_sym)
        except Exception as be:
            log.warning("BENCHMARK %s gagal; kandidat growth tetap diproses: %s", benchmark_sym, be)
    if args.mode in {"full", "report"}:
        if not tg(benchmark_report(benchmarks, m), label="BTC/ETH benchmark report"):
            log.warning("Laporan benchmark BTC/ETH tidak terkirim; screening tetap dilanjutkan.")

    global TARGET_COINS
    discovered = discover_indodax_universe(tick)
    if not discovered:
        log.error("Tidak dapat membangun universe dari ticker Indodax; gunakan daftar fallback.")
        discovered = list(TARGET_COINS)
    # Kandidat baru tidak termasuk BTC/ETH atau holding. Holding ikut dianalisis terpisah untuk perbandingan.
    candidate_symbols = [x for x in discovered if x not in BTC_ETH_EXCLUDE and x not in USER_HOLDINGS and x not in GOLD_BACKED_ASSETS]
    analysis_symbols = list(dict.fromkeys(candidate_symbols + sorted(USER_HOLDINGS)))
    TARGET_COINS = list(dict.fromkeys(analysis_symbols))
    log.info("ALL-COIN UNIVERSE: %s market IDR; kandidat=%s; holding pembanding=%s; BTC/ETH + stablecoin + token emas dikecualikan dari kandidat growth.",
             len(discovered), len(candidate_symbols), len(USER_HOLDINGS))
    results=[]
    for idx, sym in enumerate(analysis_symbols, 1):
        try:
            if idx == 1 or idx % 25 == 0:
                log.info("ALL-COIN SCREEN %s/%s — %s", idx, len(analysis_symbols), sym)
            ticker_entry=indodax_ticker_entry(tick, sym)
            p=f(ticker_entry.get("last"),0.0)
            h,src=history(sym,usdt)
            if p<=0 and not h.empty:
                p=f(h.iloc[-1].close)
            if p<=0:
                # Keep scan moving; count and log the unavailable quote instead of
                # aborting the universe. A result cannot be scored without price.
                log.warning("SKIP %s: harga Indodax tidak tersedia; simbol tetap tercatat di universe.",sym)
                continue
            if h.empty:
                log.warning("DATA GAP %s: riwayat OHLCV Indodax tidak tersedia; dinilai sebagai data tidak cukup.",sym)
            r=analyze(sym,p,h,src,m,btc_hist); results.append(r)
            # Hindari spam Telegram selama scan ratusan koin; laporan dikirim setelah ranking final.
            time.sleep(0.05)
        except Exception as e:
            log.exception("%s failed: %s",sym,e)

    scanned_with_price=len(results)
    scanned_with_history=sum(1 for r in results if r.get("histdays",0)>0)
    log.info("SCREEN SUMMARY: universe=%s; berhasil dinilai=%s; ada riwayat Indodax=%s; data gap/riwayat kosong=%s; tidak ada harga=%s.",
             len(analysis_symbols), scanned_with_price, scanned_with_history,
             max(0, scanned_with_price-scanned_with_history), max(0,len(analysis_symbols)-scanned_with_price))

    if results:
        holdings = [r for r in results if r.get("sym") in USER_HOLDINGS]
        candidates = [r for r in results if r.get("sym") not in USER_HOLDINGS and r.get("sym") not in BTC_ETH_EXCLUDE and r.get("sym") not in STABLECOINS and r.get("sym") not in GOLD_BACKED_ASSETS]
        ranked = sorted(candidates, key=lambda z: (
            bool(z.get("recovery", {}).get("priority_eligible", False)),
            not z.get("gate", {}).get("hard_fail", False),
            not z.get("gate", {}).get("data_gap", False),
            z.get("recovery", {}).get("score", 0), z.get("setup_score", 0)), reverse=True)
        eligible = [z for z in ranked if z.get("recovery", {}).get("priority_eligible", False)]
        top10 = eligible[:10]
        compare = sorted(top10 + holdings, key=lambda z: (
            bool(z.get("recovery", {}).get("priority_eligible", False)),
            not z.get("gate", {}).get("hard_fail", False),
            not z.get("gate", {}).get("data_gap", False),
            z.get("recovery", {}).get("score", 0), z.get("setup_score", 0)), reverse=True)
        # Free-first on-chain enrichment only for the final comparison list, avoiding hundreds of API calls.
        if ONCHAIN_ENABLED:
            onchain_targets = list(dict.fromkeys([z.get("sym") for z in top10 + holdings if z.get("sym")]))
            by_symbol_pre = {z.get("sym"): z for z in results}
            log.info("ON-CHAIN CHECK: %s kandidat terpilih/holding; sumber gratis; tidak mengubah skor investasi.", len(onchain_targets))
            for oi, osym in enumerate(onchain_targets, 1):
                try:
                    log.info("ON-CHAIN %s/%s — %s", oi, len(onchain_targets), osym)
                    by_symbol_pre[osym]["onchain"] = free_onchain_whale_check(osym, cg_coin(osym))
                except Exception as oe:
                    log.warning("ON-CHAIN %s gagal; lanjut screening: %s", osym, oe)
                    by_symbol_pre[osym]["onchain"] = {"status":"ERROR", "grade":"N/A", "summary":"Pemeriksaan gratis gagal; screening utama tetap berjalan."}
        lines = [
            "🏆 *ZENA V3.6.3 | HASIL SCREENING & BANDING PORTOFOLIO*",
                f"🔎 Universe: {len(discovered)} market IDR | Kandidat baru dinilai: {len(candidates)} | Prioritas lolos: *{len(eligible)}*",
            f"📁 Riwayat harga lengkap: {scanned_with_history}/{scanned_with_price} aset; data gap tidak otomatis dianggap buruk atau baik.",
            "BTC dan ETH ditampilkan dalam laporan benchmark terpisah. BTC/ETH, stablecoin, token emas, dan holding tidak dimasukkan ke Top 10 growth baru.",
            "",
            "*✅ TOP 10 KANDIDAT GROWTH*",
            "Urutan berdasarkan kualitas pemulihan, akumulasi pasar, kekuatan relatif, valuasi dan gerbang risiko. Bukan instruksi membeli sekaligus.",
        ]
        if top10:
            for i, r in enumerate(top10, 1):
                rec = r.get("recovery", {})
                acc = r.get("accumulation", {})
                cm = rec.get("market_cap_migration", {})
                gate = r.get("gate", {})
                liq = r.get("liquidity", {})
                liquid_ok = liq.get("median30", 0) > MIN_MEDIAN_30D_QUOTE_IDR and liq.get("median90", 0) > MIN_MEDIAN_90D_QUOTE_IDR
                passes = not gate.get("hard_fail") and not gate.get("data_gap") and liquid_ok
                ath = f"{rec.get('ath_multiple'):.1f}x" if rec.get("ath_multiple") is not None else "N/A"
                drawdown = f"{rec.get('ath_drawdown_pct'):+.0f}%" if rec.get("ath_drawdown_pct") is not None else "N/A"
                rs365 = f"{rec.get('rs365'):+.0f}%" if rec.get("rs365") is not None else "N/A"
                mc5 = idr_compact(rec.get("mcap_5x")) if rec.get("mcap_5x") is not None else "N/A"
                onchain_tag = onchain_short_label(r.get("onchain", {}))
                lines += [
                    f"🟡 *{i}. {r['sym']} — Recovery {rec.get('score', 0):.0f}/100 | Invest {r.get('score', 0):.0f}/100*",
                    f"   🟡 *Quality {r.get('quality_score', 0):.0f} | Akumulasi {f(acc.get('effective_score'), 0):.0f} | Setup {r.get('setup_score', 0):.0f}*",
                    f"   🟡 *Jarak ATH {drawdown} (ATH {ath}) | RS365 {rs365}*",
                    f"   🟡 *MCap {cm.get('current_tier', 'N/A')} | Skenario 5x {mc5}*",
                    f"   🟡 *{'✅' if passes else '⚠️'} Checklist gate & likuiditas: {'lolos syarat angka' if passes else 'periksa hard gate/data/likuiditas'}*",
                    f"   🔵 *On-chain: {onchain_tag}* — transfer bukan bukti pembelian whale",
                ]
        else:
            lines.append("⚠️ Belum ada kandidat yang lolos gerbang prioritas recovery. Jangan memaksa Top 10 dari aset yang lemah.")
            near_miss = [z for z in ranked if not z.get("recovery", {}).get("priority_eligible", False)][:5]
            if near_miss:
                lines.append("*PANTAU SAJA (belum lolos prioritas):*")
                for r in near_miss:
                    rec = r.get("recovery", {})
                    lines.append(f"🟡 *{r['sym']}* — Recovery {rec.get('score', 0):.0f}/100 | {rec.get('label', 'N/A')}")

        lines += ["", "*📊 PERINGKAT ULANG: TOP 10 + HOLDING*", "Perbandingan ini membantu melihat apakah kandidat baru lebih menarik daripada aset yang sudah dimiliki; status holding bukan rekomendasi jual otomatis."]
        for i, r in enumerate(compare, 1):
            tag = "HOLDING" if r.get("sym") in USER_HOLDINGS else "KANDIDAT BARU"
            rec = r.get("recovery", {})
            cm = rec.get("market_cap_migration", {})
            liq = r.get("liquidity", {})
            gate = r.get("gate", {})
            liquid_ok = liq.get("median30", 0) > MIN_MEDIAN_30D_QUOTE_IDR and liq.get("median90", 0) > MIN_MEDIAN_90D_QUOTE_IDR
            ok = not gate.get("hard_fail") and not gate.get("data_gap") and liquid_ok
            rank_header = f"🟡 *{i}. {r.get('sym', 'N/A')} — {tag}*"
            rank_metrics = (
                f"Recovery {rec.get('score', 0):.0f} | Invest {r.get('score', 0):.0f} | "
                f"Quality {r.get('quality_score', 0):.0f} | Acc {f(r.get('accumulation', {}).get('effective_score'), 0):.0f} | "
                f"MCap {cm.get('current_tier', 'N/A')} | RS365 {f(rec.get('rs365'), float('nan')):+.0f}%"
                if rec.get("rs365") is not None else
                f"Recovery {rec.get('score', 0):.0f} | Invest {r.get('score', 0):.0f} | Quality {r.get('quality_score', 0):.0f} | "
                f"Acc {f(r.get('accumulation', {}).get('effective_score'), 0):.0f} | MCap {cm.get('current_tier', 'N/A')} | RS365 N/A"
            )
            lines.append(f"{rank_header} {'✅' if ok else '⚠️'} *{rank_metrics}*")
        lines += [
            "",
            "*🧭 CARA MEMBACA*",
            "✅ Checklist menandakan syarat screening kuantitatif terpenuhi. ⚠️ berarti ada hal yang harus diperiksa ulang.",
            "ATH lama dan harga turun tajam tidak otomatis berarti murah. MCAP skenario 5x/10x mengasumsikan suplai tetap; itu skenario, bukan target.",
            "Market-cap historis memakai proxy bila snapshot terverifikasi tidak tersedia. Kualitas fundamental, keamanan, pemakaian jaringan, kompetisi, dan unlock tetap perlu pemeriksaan sumber proyek.",
            "On-chain gratis hanya tersedia untuk kontrak ERC-20 yang tercantum di registry; N/A berarti data tidak cukup, bukan sinyal akumulasi.",
            "*Holding yang dikenali:* SOL, SUI, RENDER, FET, AIOZ, ONDO, NEAR, MON. Tambahkan koin lain ke USER_HOLDINGS jika diperlukan.",
        ]
        if args.mode in {"full", "report"}:
            if not tg("\n".join(lines), label="final shortlist and portfolio comparison"):
                log.error("FINAL SHORTLIST TIDAK TERKIRIM KE TELEGRAM.")
            # Kirim detail coin hanya untuk 10 kandidat teratas + holding agar laporan tidak membanjiri chat.
            detail_list = list(dict.fromkeys([r.get("sym") for r in top10 + holdings]))
            by_symbol = {r.get("sym"): r for r in results}
            for sym in detail_list:
                if sym in by_symbol:
                    if not tg(coin_report(by_symbol[sym]), label=f"{sym} detail report"):
                        log.warning("Telegram gagal untuk detail %s.", sym)
                    if _telegram_configured(): time.sleep(TELEGRAM_SEND_DELAY)
        else:
            print("\n".join(lines))

if __name__=="__main__":
    main()
