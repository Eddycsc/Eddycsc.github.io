"""Fetch TAIFEX/TWSE/TPEx/Yahoo Finance data and regenerate the global-indices page.

Run manually with: python scripts/global_indices_update.py
Intended to run daily via .github/workflows/update-market-data.yml

Data is tiered by reliability:
  Tier 1 (official, expected-stable): TAIFEX TX near-month futures, TWSE 加權/電子指數,
      TPEx OTC index. Any real failure here aborts the whole page regeneration.
  Tier 2 (Yahoo Finance, unofficial, expected to occasionally flake): 13 international
      index/futures symbols. Each is fetched independently; a single symbol failing only
      degrades that one row to "-", never the rest of the page.
"""
import datetime
import json
import re
import ssl
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "scripts" / "state" / "global_indices_snapshot.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
TAIPEI = datetime.timezone(datetime.timedelta(hours=8))

_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _relaxed_ssl_context() -> ssl.SSLContext:
    # Some TWSE/TPEx cert chains trip urllib's default strict x509 checks
    # (Missing Subject Key Identifier) even though browsers/curl accept them.
    # Only relax that one check; keep the rest of certificate verification intact.
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


def fetch_text(url: str, data: bytes = None) -> str:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30, context=_relaxed_ssl_context()) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def fetch_json(url: str) -> dict:
    return json.loads(fetch_text(url))


def now_taipei() -> datetime.datetime:
    return datetime.datetime.now(TAIPEI)


def clean_cell(html: str) -> str:
    return _TAG_RE.sub("", html).replace("&nbsp;", " ").strip()


def to_num(s):
    if s is None:
        return None
    s = s.replace(",", "").replace("▲", "").replace("▼", "").replace("%", "").strip()
    if s in ("", "-", "—"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---------- Tier 1: TAIFEX TX 近月 ----------

FUT_URL = "https://www.taifex.com.tw/cht/3/futDailyMarketReport"
_IVORY_TR_RE = re.compile(r'<tr[^>]*bgcolor="ivory"[^>]*>(.*?)</tr>', re.S | re.I)
_THEAD_RE = re.compile(r"<thead>(.*?)</thead>", re.S | re.I)
_TH_RE = re.compile(r"<th[^>]*>(.*?)</th>", re.S | re.I)

# TAIFEX's report table has a different column set depending on whether the day
# session has fully closed and been processed yet (e.g. 結算價/成交量 columns are
# split differently, or show "-", while settlement is still being computed). Rather
# than assume a fixed column layout, match columns by their header label so a
# leaner/incomplete report is detected instead of silently misreading the wrong cell.


def _no_whitespace(s: str) -> str:
    return re.sub(r"\s+", "", s)


def fetch_tx(query_date: str) -> dict:
    """query_date is 'YYYY/MM/DD'. Returns None if there's no ivory row, or if the
    settlement price isn't published yet (non-trading day / not yet processed)."""
    params = {
        "queryType": "2",
        "marketCode": "0",
        "dateaddcnt": "",
        "commodity_id": "TX",
        "commodity_id2": "",
        "queryDate": query_date,
        "MarketCode": "0",
    }
    body = urllib.parse.urlencode(params).encode()
    html = fetch_text(FUT_URL, data=body)

    thead_m = _THEAD_RE.search(html)
    row_m = _IVORY_TR_RE.search(html)
    if not thead_m or not row_m:
        return None

    headers = [_no_whitespace(clean_cell(h)) for h in _TH_RE.findall(thead_m.group(1))]
    cells = [clean_cell(c) for c in _TD_RE.findall(row_m.group(1))]
    if len(cells) != len(headers) or not cells or cells[0] != "TX":
        return None

    by_label = dict(zip(headers, cells))

    def cell_for(*label_fragments):
        for label, value in by_label.items():
            if all(frag in label for frag in label_fragments):
                return value
        return None

    settlement = to_num(cell_for("結算價"))
    change = to_num(cell_for("漲跌價"))
    change_pct = to_num(cell_for("漲跌%"))
    expiry = cell_for("到期")
    total_vol = to_num(cell_for("合計成交量"))
    if total_vol is None:
        total_vol = to_num(cell_for("成交量"))  # single-column fallback (partial-day report)

    if settlement is None:
        # Settlement not published yet for this date (e.g. still mid-session, or a
        # non-trading day that nonetheless returned a stale placeholder row) - treat
        # as "no usable data yet" rather than rendering a wrong/partial number.
        return None

    return {
        "date": query_date,
        "contract": expiry,
        "settlement": settlement,
        "change": change,
        "change_pct": change_pct,
        "total_volume": int(total_vol) if total_vol is not None else None,
    }


# ---------- Tier 1: TWSE 加權指數 / 電子工業類指數 ----------

MI_INDEX_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?response=json&date={date}&type=IND"


def _find_index_row(data: dict, name: str):
    if data.get("stat") != "OK":
        return None
    for table in data.get("tables") or []:
        for row in (table or {}).get("data") or []:
            if row[0] == name:
                return row
    return None


def fetch_twse_indices(start_date: datetime.date, names: list, max_back_days: int = 10) -> dict:
    """Walk back from start_date until both index names are found on the same trading day.
    TWSE's index report is typically published about a trading day behind TAIFEX."""
    d = start_date
    for _ in range(max_back_days):
        date_str = d.strftime("%Y%m%d")
        data = fetch_json(MI_INDEX_URL.format(date=date_str))
        rows = {name: _find_index_row(data, name) for name in names}
        if all(rows.values()):
            out = {"date": f"{date_str[0:4]}/{date_str[4:6]}/{date_str[6:8]}"}
            for name, row in rows.items():
                close = to_num(row[1])
                dir_html = row[2] or ""
                points = to_num(row[3]) or 0.0
                sign = -1 if "green" in dir_html else 1
                pct = to_num(row[4])
                out[name] = {
                    "close": close,
                    "change": sign * points,
                    "change_pct": pct if pct is not None else sign * points / (close - sign * points) * 100,
                }
            return out
        d -= datetime.timedelta(days=1)
    raise RuntimeError(f"could not find TWSE index data for {names} within {max_back_days} days back from {start_date}")


# ---------- Tier 1: TPEx 櫃買指數 (OTC index) ----------

TPEX_INDEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_index"


def fetch_otc_index() -> dict:
    data = fetch_json(TPEX_INDEX_URL)
    if not data:
        raise RuntimeError("tpex_index returned no data")
    latest = data[-1]
    close = to_num(latest["Close"])
    change = to_num(latest["Change"])
    prev_close = close - change
    date_raw = latest["Date"]
    return {
        "date": f"{date_raw[0:4]}/{date_raw[4:6]}/{date_raw[6:8]}",
        "close": close,
        "change": change,
        "change_pct": (change / prev_close * 100) if prev_close else 0.0,
    }


# ---------- Tier 2: Yahoo Finance international symbols ----------

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1mo&interval=1d"
STALE_SECONDS = 4 * 24 * 3600  # flag feeds that haven't updated in 4+ days (catches frozen tickers)

# (symbol, display label, section, has_volume)
YAHOO_SYMBOLS = [
    ("NQ=F", "NQ 那斯達克100期貨", "intl", True),
    ("ES=F", "ES 標普500期貨", "intl", True),
    ("^N225", "日經225", "intl", False),
    ("^KS11", "韓國 KOSPI", "intl", False),
    ("XIN9.FGI", "富時中國A50", "intl", False),
    ("^STI", "新加坡海峽時報指數", "intl", False),
    ("^GDAXI", "德國 DAX", "intl", False),
    ("GC=F", "黃金期貨（GC）", "comdty", True),
    ("ZW=F", "小麥期貨", "comdty", True),
    ("ZC=F", "玉米期貨", "comdty", True),
    ("CL=F", "輕原油期貨（WTI）", "comdty", True),
    ("DX-Y.NYB", "美元指數（DXY）", "comdty", False),
    ("^VIX", "VIX 恐慌指數", "comdty", False),
]


def fetch_yahoo(symbol: str, has_volume: bool, now_ts: float) -> dict:
    data = fetch_json(YAHOO_CHART_URL.format(symbol=urllib.parse.quote(symbol)))
    result = data["chart"]["result"][0]
    meta = result["meta"]

    market_time = meta.get("regularMarketTime")
    if market_time is None or (now_ts - market_time) > STALE_SECONDS:
        raise RuntimeError(f"{symbol}: feed looks stale (regularMarketTime={market_time})")

    price = meta.get("regularMarketPrice")
    prev_close = meta.get("previousClose")
    closes = result.get("indicators", {}).get("quote", [{}])[0].get("close") or []
    if prev_close is None:
        # meta.previousClose can be briefly null around futures contract rollovers;
        # fall back to the last completed close in the daily series.
        completed = [c for c in closes[:-1] if c is not None]
        prev_close = completed[-1] if completed else None
    if price is None or prev_close is None:
        raise RuntimeError(f"{symbol}: missing price/previousClose")

    change = price - prev_close
    change_pct = (change / prev_close * 100) if prev_close else None

    vol_note = None
    vol_pct = None
    if has_volume:
        volumes = result.get("indicators", {}).get("quote", [{}])[0].get("volume") or []
        timestamps = result.get("timestamp") or []
        pairs = [(t, v) for t, v in zip(timestamps, volumes) if v is not None]
        # collapse consecutive identical volumes (weekend/holiday placeholder duplicates)
        distinct = []
        for t, v in pairs:
            if not distinct or distinct[-1][1] != v:
                distinct.append((t, v))
        if len(distinct) >= 2:
            current_vol = distinct[-1][1]
            prev_vol = distinct[-2][1]
            completed_vols = [v for _, v in distinct[:-1]]
            if len(completed_vols) >= 3:
                baseline = statistics.median(completed_vols)
                if baseline > 0 and prev_vol < 0.2 * baseline:
                    vol_note = "—（疑似合約換月，成交量不具比較性）"
            if vol_note is None:
                vol_pct = ((current_vol - prev_vol) / prev_vol * 100) if prev_vol else None
                # An extreme swing (regardless of what the median check caught) is a
                # symptom of the same rollover/contract-transition noise - never publish
                # a wild-looking percentage, show the honest caveat instead.
                if vol_pct is not None and abs(vol_pct) > 300:
                    vol_note = "—（疑似合約換月，成交量不具比較性）"
                    vol_pct = None
        else:
            vol_note = "—"

    return {
        "price": price,
        "change": change,
        "change_pct": change_pct,
        "vol_pct": vol_pct,
        "vol_note": vol_note if has_volume else "—",
    }


def fetch_all_yahoo() -> dict:
    now_ts = now_taipei().timestamp()
    out = {}
    failed = []
    for symbol, _label, _section, has_volume in YAHOO_SYMBOLS:
        try:
            out[symbol] = fetch_yahoo(symbol, has_volume, now_ts)
        except Exception as e:  # noqa: BLE001 - Tier 2, isolate failures per-symbol on purpose
            failed.append((symbol, str(e)))
    if failed:
        print("WARNING: Yahoo Finance symbols failed (rendered as —):", file=sys.stderr)
        for symbol, err in failed:
            print(f"  {symbol}: {err}", file=sys.stderr)
    return out


# ---------- state (TX volume day-over-day comparison) ----------

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(date: str, contract: str, volume) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps({"date": date, "tx_contract": contract, "tx_volume": volume}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def tx_volume_change_text(tx: dict, state: dict) -> str:
    if tx["total_volume"] is None:
        return "—"
    if state.get("tx_contract") == tx["contract"] and state.get("tx_volume"):
        pct = (tx["total_volume"] - state["tx_volume"]) / state["tx_volume"] * 100
        return f"{pct:+.2f}%"
    return "—（換月或無前一日資料，不具比較性）"


# ---------- rendering ----------

def fmt(n, decimals=2):
    if n is None:
        return "—"
    return f"{n:,.{decimals}f}"


def fmt_signed(n, decimals=2):
    if n is None:
        return "—"
    return f"{n:+,.{decimals}f}"


def render(tx: dict, twse: dict, otc: dict, yahoo: dict, tx_vol_change: str) -> str:
    date_disp = tx["date"].replace("/", "/")
    twse_date_note = ""
    if twse["date"] != tx["date"]:
        twse_date_note = f"（資料日期 {twse['date']}，證交所公開資料較台指期慢更新）"

    tw_rows = [
        f"| 台指期（TX，近月） | {fmt(tx['settlement'], 0)} | {fmt_signed(tx['change'], 0)} | "
        f"{fmt_signed(tx['change_pct'])}% | {tx_vol_change} |",
        f"| 加權指數（發行量加權股價指數） | {fmt(twse['發行量加權股價指數']['close'])} | "
        f"{fmt_signed(twse['發行量加權股價指數']['change'])} | {fmt_signed(twse['發行量加權股價指數']['change_pct'])}% | — |",
        f"| 電子加權指數（電子工業類指數） | {fmt(twse['電子工業類指數']['close'])} | "
        f"{fmt_signed(twse['電子工業類指數']['change'])} | {fmt_signed(twse['電子工業類指數']['change_pct'])}% | — |",
        f"| OTC 櫃買指數 | {fmt(otc['close'])} | {fmt_signed(otc['change'])} | {fmt_signed(otc['change_pct'])}% | — |",
    ]

    def yahoo_row(symbol: str, label: str) -> str:
        v = yahoo.get(symbol)
        if v is None:
            return f"| {label} | — | — | — | —（資料暫時無法取得） |"
        vol_col = v["vol_note"] if v["vol_note"] else (f"{v['vol_pct']:+.2f}%" if v["vol_pct"] is not None else "—")
        return (
            f"| {label} | {fmt(v['price'])} | {fmt_signed(v['change'])} | "
            f"{fmt_signed(v['change_pct'])}% | {vol_col} |"
        )

    intl_rows = [yahoo_row(sym, label) for sym, label, section, _ in YAHOO_SYMBOLS if section == "intl"]
    comdty_rows = [yahoo_row(sym, label) for sym, label, section, _ in YAHOO_SYMBOLS if section == "comdty"]

    gold = yahoo.get("GC=F")
    gold_note = ""
    if gold and gold.get("vol_note") and "換月" in gold["vol_note"]:
        gold_note = "\n- 黃金期貨（GC）目前偵測到成交量疑似受合約換月影響，換月期間新舊合約成交量交接，不具日增減比較性"

    return f"""---
layout: page
title: 相關指數速覽
permalink: /futures-options/global-indices/
---

整理台指期、台股相關指數，以及主要與台股連動的國際指數、商品期貨的收盤價、漲跌幅與成交量變化，方便快速掃描全球市場氣氛。**本頁由每日自動更新程序同步。**

資料時間：{date_disp}（台股／台指期）；國際指數與商品期貨為近似即時報價

## 台股與台指期

| 商品 | 收盤價/結算價 | 漲跌點 | 漲跌幅 | 成交量增減 |
|---|---:|---:|---:|---:|
{chr(10).join(tw_rows)}

<p class="data-source-note">台指期資料來源：<a href="https://www.taifex.com.tw/cht/3/futDailyMarketReport" target="_blank" rel="noopener noreferrer">臺灣期貨交易所－期貨每日交易行情</a>；加權指數／電子指數資料來源：<a href="https://www.twse.com.tw/" target="_blank" rel="noopener noreferrer">臺灣證券交易所</a>{twse_date_note}；OTC 指數資料來源：<a href="https://www.tpex.org.tw/" target="_blank" rel="noopener noreferrer">證券櫃檯買賣中心</a>（資料日期 {otc['date']}）</p>

## 國際指數

| 商品 | 收盤價/指數 | 漲跌點 | 漲跌幅 | 成交量增減 |
|---|---:|---:|---:|---:|
{chr(10).join(intl_rows)}

## 商品期貨與美元指數

| 商品 | 收盤價 | 漲跌點 | 漲跌幅 | 成交量增減 |
|---|---:|---:|---:|---:|
{chr(10).join(comdty_rows)}

<p class="data-source-note">國際指數與商品期貨資料來源：Yahoo Finance 公開報價介面</p>

## 使用上的提醒

- 「成交量增減」只對有實際成交量意義的**期貨契約**（台指期、NQ、ES、GC、小麥、玉米、輕原油）計算；純粹的**指數**（日經225、韓指、A50、新加坡指數、DAX、美元指數、VIX）本身不是交易標的，沒有自身的成交量，故不列出
- 國際指數與期貨的「成交量增減」比較的是目前這一筆（可能還在交易中、尚未收盤）的成交量對比上一個已收盤完整交易日，兩者本質不是同一種東西，數字通常會偏向負值——這是資料結構造成的正常現象，不代表成交量真的萎縮，僅供粗略參考{gold_note}
- 加權指數與電子指數採用證交所公開資料，發布時間可能比台指期慢，兩者日期可能不同，請留意上方標示的資料日期
- 國際指數與期貨為市場公開報價，可能有數分鐘至數十分鐘的延遲，僅供參考，實際交易請以看盤軟體即時報價為準

<p class="futures-disclaimer">以上內容為市場公開資料整理，不構成投資建議。期貨與商品交易具有高風險，操作前請審慎評估自身風險承受度。</p>
"""


# ---------- orchestration ----------

def main():
    today = now_taipei().date()
    query_date = today.strftime("%Y/%m/%d")

    try:
        tx = fetch_tx(query_date)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"ERROR: failed to fetch TAIFEX TX data: {e}", file=sys.stderr)
        sys.exit(1)

    if tx is None:
        print(f"no usable TAIFEX TX settlement data for {query_date} yet "
              f"(non-trading day, or today's session not finalized); skipping")
        return

    try:
        twse = fetch_twse_indices(today, ["發行量加權股價指數", "電子工業類指數"])
        otc = fetch_otc_index()
    except (urllib.error.URLError, TimeoutError, RuntimeError, KeyError) as e:
        print(f"ERROR: Tier 1 official source failed, skipping page regeneration: {e}", file=sys.stderr)
        sys.exit(1)

    yahoo = fetch_all_yahoo()  # Tier 2: never raises, degrades per-symbol

    state = load_state()
    tx_vol_change = tx_volume_change_text(tx, state)

    content = render(tx, twse, otc, yahoo, tx_vol_change)
    (ROOT / "futures-options" / "global-indices.md").write_text(content, encoding="utf-8")

    if tx["total_volume"] is not None:
        save_state(tx["date"], tx["contract"], tx["total_volume"])

    print(f"regenerated global-indices.md (TX date {tx['date']}, TWSE index date {twse['date']}, OTC date {otc['date']})")


if __name__ == "__main__":
    main()
