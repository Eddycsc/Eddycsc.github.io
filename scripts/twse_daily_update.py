"""Fetch TWSE public reports and regenerate the twse-data page.

Run manually with: python scripts/twse_daily_update.py
Intended to run daily via .github/workflows/update-market-data.yml
"""
import datetime
import json
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
TAIPEI = datetime.timezone(datetime.timedelta(hours=8))


def _relaxed_ssl_context() -> ssl.SSLContext:
    # Some TWSE/TPEx cert chains trip urllib's default strict x509 checks
    # (Missing Subject Key Identifier) even though browsers/curl accept them.
    # Only relax that one check; keep the rest of certificate verification intact.
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30, context=_relaxed_ssl_context()) as resp:
        return json.loads(resp.read().decode("utf-8"))


def today_str() -> str:
    return datetime.datetime.now(TAIPEI).strftime("%Y%m%d")


# ---------- BFI82U: 三大法人買賣金額統計表 (stock market) ----------

BFI82U_URL = "https://www.twse.com.tw/rwd/zh/fund/BFI82U?response=json&date={date}"

_BFI_LABELS = {
    "自營商(自行買賣)": "自營商（自行買賣）",
    "自營商(避險)": "自營商（避險）",
    "投信": "投信",
    "外資及陸資(不含外資自營商)": "外資及陸資",
}


def parse_bfi82u(data: dict) -> dict:
    rows = {r[0]: r for r in data["data"]}
    out = {}
    for src_label, disp_label in _BFI_LABELS.items():
        r = rows[src_label]
        out[disp_label] = {
            "buy": int(r[1].replace(",", "")),
            "sell": int(r[2].replace(",", "")),
            "net": int(r[3].replace(",", "")),
        }
    total = rows["合計"]
    out["合計"] = {
        "buy": int(total[1].replace(",", "")),
        "sell": int(total[2].replace(",", "")),
        "net": int(total[3].replace(",", "")),
    }
    return out


# ---------- MI_MARGN: 信用交易統計 (margin/short balance totals) ----------

MI_MARGN_URL = "https://www.twse.com.tw/exchangeReport/MI_MARGN?response=json&date={date}"


def parse_mi_margn(data: dict) -> dict:
    table = data["tables"][0]
    rows = {r[0]: r for r in table["data"]}
    financing_units = rows["融資(交易單位)"]
    financing_amt = rows["融資金額(仟元)"]
    return {
        "units_prev": int(financing_units[4].replace(",", "")),
        "units_today": int(financing_units[5].replace(",", "")),
        "amt_prev_thousand": int(financing_amt[4].replace(",", "")),
        "amt_today_thousand": int(financing_amt[5].replace(",", "")),
    }


# ---------- MI_MARGN (per-stock) + STOCK_DAY_ALL: 融資維持率 ----------

MI_MARGN_PERSTOCK_URL = "https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN"
STOCK_DAY_ALL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"


def compute_maintenance_ratio(margin_amt_today_thousand: int) -> dict | None:
    try:
        margn_rows = fetch_json(MI_MARGN_PERSTOCK_URL)
        price_rows = fetch_json(STOCK_DAY_ALL_URL)
    except urllib.error.URLError as e:
        print(f"WARNING: maintenance ratio fetch failed: {e}", file=sys.stderr)
        return None

    price_map = {}
    for row in price_rows:
        try:
            price_map[row["Code"]] = float(row["ClosingPrice"])
        except (KeyError, ValueError):
            continue

    total_collateral_value = 0.0
    total_lots = 0
    for row in margn_rows:
        code = row.get("股票代號", "")
        if code.startswith("00"):
            continue  # exclude ETFs / beneficiary certificates
        lots_str = None
        for k, v in row.items():
            if "融資" in k and "今" in k and "餘額" in k:
                lots_str = v
                break
        if not lots_str:
            continue
        try:
            lots = int(lots_str.replace(",", ""))
        except ValueError:
            continue
        price = price_map.get(code)
        if price is None:
            continue
        total_collateral_value += lots * 1000 * price
        total_lots += lots

    if total_lots == 0 or margin_amt_today_thousand == 0:
        return None

    ratio = (total_collateral_value / 1000) / margin_amt_today_thousand * 100
    return {
        "collateral_value_thousand": total_collateral_value / 1000,
        "ratio_pct": ratio,
    }


# ---------- rendering ----------

def yi(n: float) -> str:
    return f"{n / 1e8:,.2f}億"


def yi_signed(n: float) -> str:
    return f"{n / 1e8:+,.2f}億"


def render(date: str, bfi: dict, margn: dict, maint: dict | None) -> str:
    bfi_rows = []
    for label in ["自營商（自行買賣）", "自營商（避險）", "投信", "外資及陸資"]:
        v = bfi[label]
        net_txt = yi_signed(v["net"]) if label in ("投信", "外資及陸資") else yi(v["net"])
        if label in ("投信", "外資及陸資"):
            net_txt = f"**{net_txt}**"
        bfi_rows.append(f"| {label} | {yi(v['buy'])} | {yi(v['sell'])} | {net_txt} |")
    total = bfi["合計"]
    bfi_rows.append(f"| 合計 | {yi(total['buy'])} | {yi(total['sell'])} | **{yi_signed(total['net'])}** |")

    net_desc = []
    for label in ["自營商（自行買賣）", "自營商（避險）", "投信", "外資及陸資"]:
        net = bfi[label]["net"]
        direction = "買超" if net >= 0 else "賣超"
        net_desc.append(f"{label}{direction}約{abs(net) / 1e8:,.0f}億元")
    total_direction = "買超" if total["net"] >= 0 else "賣超"
    bfi_summary = (
        f"三大法人合計{total_direction}約{abs(total['net']) / 1e8:,.0f}億元；"
        f"其中，{'、'.join(net_desc)}。"
    )

    margin_units_diff = margn["units_today"] - margn["units_prev"]
    margin_amt_prev = margn["amt_prev_thousand"] * 1000
    margin_amt_today = margn["amt_today_thousand"] * 1000
    margin_amt_diff = margin_amt_today - margin_amt_prev
    margin_direction = "增加" if margin_amt_diff >= 0 else "減少"
    margin_effect = "升溫、加槓桿" if margin_amt_diff >= 0 else "降溫、去槓桿"
    margin_summary = (
        f"融資餘額單日{margin_direction}約{abs(margin_amt_diff) / 1e8:,.0f}億元，"
        f"代表整體市場融資部位在{margin_effect}。"
    )

    date_disp = f"{date[0:4]}/{date[4:6]}/{date[6:8]}"

    if maint:
        maint_section = f"""## 融資維持率

證交所沒有直接公告這個比率，但可以用官方公式加上兩份官方原始數據自己算出來：

```
融資維持率 = 不含 ETF 之所有融資股票市值 ÷ 大盤融資餘額
          = （所有非 ETF 個股的融資股數 × 該股收盤價）加總 ÷ 大盤融資金額餘額
```

| 項目 | 數值 |
|---|---:|
| 非 ETF 融資股票市值合計 | {yi(maint['collateral_value_thousand'] * 1000)} |
| 大盤融資金額餘額 | {yi(margin_amt_today)} |
| **融資維持率** | **約 {maint['ratio_pct']:.2f}%** |

<p class="data-source-note">計算方式：逐檔比對<a href="https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN" target="_blank" rel="noopener noreferrer">個股融資餘額（股數）</a>與<a href="https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL" target="_blank" rel="noopener noreferrer">個股當日收盤價</a>，排除股票代號開頭為「00」的 ETF／受益證券後加總市值，除以大盤融資金額餘額；這是自行依公開資料計算的推導值，不是證交所直接公告的數字</p>

一般以 130% 為融資維持率的追繳警戒線，收盤低於這個水準會觸發整戶擔保維持率追繳程序。
"""
        maint_limitation = ""
    else:
        maint_section = ""
        maint_limitation = "- **整體市場融資維持率**：這次自動更新暫時無法取得個股融資餘額或收盤價資料，故無法計算，下次更新會重新嘗試。\n"

    return f"""---
layout: page
title: 證交所數據
permalink: /futures-options/twse-data/
---

整理臺灣證券交易所公告的三大法人買賣超（股票市場）與融資餘額變化。這裡談的是**股票市場**的三大法人，跟 [三大法人未平倉部位](/futures-options/institutional-positions/) 頁面談的**期貨市場**三大法人是兩件事，不要混為一談。**本頁由每日自動更新程序同步。**

資料日期：{date_disp}

## 三大法人買賣超（股票市場）

| 身份別 | 買進金額 | 賣出金額 | 買賣差額 |
|---|---:|---:|---:|
{chr(10).join(bfi_rows)}

<p class="data-source-note">資料來源：<a href="https://www.twse.com.tw/zh/trading/foreign/bfi82u.html" target="_blank" rel="noopener noreferrer">臺灣證券交易所－三大法人買賣金額統計表</a></p>

{bfi_summary}

## 融資餘額變化

| 項目 | 前一交易日餘額 | 當日餘額 | 增減 |
|---|---:|---:|---:|
| 融資張數 | {margn['units_prev']:,} 張 | {margn['units_today']:,} 張 | {margin_units_diff:+,} 張 |
| 融資金額 | {yi(margin_amt_prev)} | {yi(margin_amt_today)} | **{yi_signed(margin_amt_diff)}** |

<p class="data-source-note">資料來源：<a href="https://www.twse.com.tw/zh/trading/margin/mi-margn.html" target="_blank" rel="noopener noreferrer">臺灣證券交易所－信用交易統計</a></p>

{margin_summary}

{maint_section}## 資料限制說明

以下是使用者常見的需求，但證交所公開資料沒有現成算好的數字，這裡誠實說明無法提供的原因，不自行編造或用不相關的數字冒充：
{maint_limitation}

- **加權成分股個別對指數的點數貢獻排行**：證交所公開資料只有「成交量前二十名證券」，是依成交股數排名（且包含槓桿型 ETF 等非成分股商品），跟「指數成分股權重」是完全不同的概念，兩者不能互相替代。要精確計算個股對指數的點數貢獻，需要每檔成分股在指數中的即時權重（通常來自自由流通市值佔比），這項原始權重數字證交所並未以公開、可程式化查詢的格式提供，所以這裡沒有放這個排行，避免用錯誤的資料誤導判斷。

<p class="futures-disclaimer">以上內容為證交所公開資料的整理，不構成投資建議。股票與信用交易具有風險，操作前請詳閱相關規則並評估自身風險承受度。</p>
"""


# ---------- orchestration ----------

def main():
    date = today_str()

    try:
        margn_raw = fetch_json(MI_MARGN_URL.format(date=date))
    except urllib.error.URLError as e:
        print(f"ERROR: failed to fetch MI_MARGN: {e}", file=sys.stderr)
        sys.exit(1)

    if margn_raw.get("stat") != "OK":
        print(f"no MI_MARGN data for {date} ({margn_raw.get('stat')}), likely a non-trading day; skipping")
        return

    try:
        bfi_raw = fetch_json(BFI82U_URL.format(date=date))
    except urllib.error.URLError as e:
        print(f"ERROR: failed to fetch BFI82U: {e}", file=sys.stderr)
        sys.exit(1)

    if bfi_raw.get("stat") != "OK" or bfi_raw.get("date") != date:
        print(f"BFI82U data not yet available for {date} (got stat={bfi_raw.get('stat')!r}, "
              f"date={bfi_raw.get('date')!r}); skipping")
        return

    bfi = parse_bfi82u(bfi_raw)
    margn = parse_mi_margn(margn_raw)
    maint = compute_maintenance_ratio(margn["amt_today_thousand"])

    content = render(date, bfi, margn, maint)
    (ROOT / "futures-options" / "twse-data.md").write_text(content, encoding="utf-8")
    print(f"regenerated twse-data.md for {date}")


if __name__ == "__main__":
    main()