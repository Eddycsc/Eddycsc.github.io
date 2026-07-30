"""Fetch TAIFEX public reports and regenerate the margin/institutional-positions/txo-chips pages.

Run manually with: python scripts/taifex_daily_update.py
Intended to run daily via .github/workflows/update-taifex.yml
"""
import json
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "scripts" / "state" / "taifex_snapshot.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

WHO_ZH = {"dealer": "自營商", "trust": "投信", "foreign": "外資及陸資"}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="ignore")


# ---------- generic table-row parsing (shared by futures/options 三大法人 reports) ----------

_TBODY_RE = re.compile(r"<TBODY>(.*?)</TBODY>", re.S | re.I)
_ROW_RE = re.compile(r"<TR class=\"12bk\">(.*?)</TR>", re.S | re.I)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_NUM_RE = re.compile(r"-?[\d,]+")
_DATE_RE = re.compile(r"日期(\d{4}/\d{2}/\d{2})")


def _parse_12col_rows(html: str) -> list[list[int]]:
    tbody = _TBODY_RE.search(html).group(1)
    rows = []
    for row_html in _ROW_RE.findall(tbody):
        texts = [_TAG_RE.sub("", c).replace("&nbsp;", " ").strip() for c in _CELL_RE.findall(row_html)]
        nums = [t.replace(",", "") for t in texts if t and _NUM_RE.fullmatch(t.replace(" ", ""))]
        if len(nums) < 12:
            continue
        rows.append([int(n) for n in nums[-12:]])
    return rows


def _row_to_oi(v: list[int]) -> dict:
    return {
        "oi_buy_lots": v[6], "oi_buy_amt": v[7],
        "oi_sell_lots": v[8], "oi_sell_amt": v[9],
        "oi_net_lots": v[10], "oi_net_amt": v[11],
    }


def parse_report_date(html: str) -> str:
    m = _DATE_RE.search(html)
    if not m:
        raise ValueError("could not find report date in page")
    return m.group(1)


def parse_three_major_futures(html: str) -> dict:
    rows = _parse_12col_rows(html)
    # Row order is fixed by the query's default commodity list (verified against
    # the official page): 0-2 = TXF, 9-11 = MXF, 12-14 = TMF (each dealer/trust/foreign).
    idx = {"TXF": (0, 1, 2), "MXF": (9, 10, 11), "TMF": (12, 13, 14)}
    out = {}
    for prod, (i0, i1, i2) in idx.items():
        out[prod] = {
            "dealer": _row_to_oi(rows[i0]),
            "trust": _row_to_oi(rows[i1]),
            "foreign": _row_to_oi(rows[i2]),
        }
    return out


def parse_three_major_options(html: str) -> dict:
    rows = _parse_12col_rows(html)
    # 0-2 = 臺指選擇權 Call (dealer/trust/foreign), 3-5 = Put
    return {
        "Call": {"dealer": _row_to_oi(rows[0]), "trust": _row_to_oi(rows[1]), "foreign": _row_to_oi(rows[2])},
        "Put": {"dealer": _row_to_oi(rows[3]), "trust": _row_to_oi(rows[4]), "foreign": _row_to_oi(rows[5])},
    }


# ---------- margin table ----------

def parse_margin_table(html: str) -> dict:
    products = ["臺股期貨", "小型臺指", "微型臺指期貨",
                "臺指選擇權風險保證金(A)值", "臺指選擇權風險保證金(B)值", "臺指選擇權風險保證金(C)值"]
    row_re = re.compile(r"<td>([^<]+)</td>\s*<td[^>]*>([\d,]+)</td>\s*<td[^>]*>([\d,]+)</td>\s*<td[^>]*>([\d,]+)</td>")
    out = {}
    for name, settle, maint, orig in row_re.findall(html):
        name = name.strip()
        if name in products:
            out[name] = {
                "settlement": int(settle.replace(",", "")),
                "maintenance": int(maint.replace(",", "")),
                "original": int(orig.replace(",", "")),
            }
    updated = re.search(r"更新日期：(\d{4}/\d{2}/\d{2})", html)
    out["_updated"] = updated.group(1) if updated else "未知"
    if len(out) < 7:
        raise ValueError(f"margin table parse incomplete, got {list(out.keys())}")
    return out


# ---------- option chain (OI by strike per expiry series) ----------

_CHAIN_TBODY_RE = re.compile(r"<tbody>(.*?)</tbody>", re.S)
_CHAIN_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
_CHAIN_CELL_RE = re.compile(r"<t[dD][^>]*>(.*?)</t[dD]>", re.S)


def parse_option_chain(html: str) -> dict:
    body = _CHAIN_TBODY_RE.search(html).group(1)
    rows = []
    for row_html in _CHAIN_ROW_RE.findall(body):
        cells_raw = _CHAIN_CELL_RE.findall(row_html)
        if len(cells_raw) < 14:
            continue
        cells = [_TAG_RE.sub("", c).replace("&nbsp;", " ").strip() for c in cells_raw]
        settle_month, expiry_date, strike, cp = cells[1], cells[2], cells[3], cells[4]
        try:
            strike_v = float(strike.replace(",", ""))
            oi = int(cells[13].replace(",", "") or 0)
        except ValueError:
            continue
        rows.append({"settle": settle_month, "expiry": expiry_date, "strike": strike_v, "cp": cp, "oi": oi})

    from collections import defaultdict
    series = defaultdict(list)
    for r in rows:
        series[r["settle"]].append(r)

    out = {}
    for settle, items in series.items():
        calls = [r for r in items if r["cp"] == "Call"]
        puts = [r for r in items if r["cp"] == "Put"]
        max_call = max(calls, key=lambda r: r["oi"], default=None)
        max_put_candidate = max(puts, key=lambda r: r["oi"], default=None)
        max_put = max_put_candidate if max_put_candidate and max_put_candidate["oi"] > 0 else None
        if settle.endswith(("F1", "F2", "F3", "F4", "F5")):
            category = "週五選"
        elif settle.endswith(("W1", "W2", "W3", "W4", "W5")):
            category = "週選"
        else:
            category = "月選"
        out[settle] = {
            "category": category,
            "expiry": items[0]["expiry"] if items else None,
            "total_call_oi": sum(r["oi"] for r in calls),
            "total_put_oi": sum(r["oi"] for r in puts),
            "max_call_strike": max_call["strike"] if max_call else None,
            "max_call_oi": max_call["oi"] if max_call else 0,
            "max_put_strike": max_put["strike"] if max_put else None,
            "max_put_oi": max_put["oi"] if max_put else 0,
        }
    if not out:
        raise ValueError("option chain parse produced no series")
    return out


# ---------- rendering ----------

def fmt(n: int) -> str:
    return f"{n:,}"


def render_margin_table(margin: dict) -> str:
    m = margin
    updated = m["_updated"]
    return f"""---
layout: page
title: 保證金基準表
permalink: /futures-options/margin-table/
---

臺灣期貨交易所會依市場波動狀況，不定期調整結算保證金、維持保證金與原始保證金的金額。下表為股價指數類商品的最新公告數字，**由每日自動更新程序同步**。

保證金概念說明可參考 [期貨入門](/futures-options/futures-basics/) 與 [選擇權入門](/futures-options/options-basics/)。

## 股價指數類商品保證金一覽表

單位：新臺幣元

| 商品別 | 結算保證金 | 維持保證金 | 原始保證金 |
|---|---:|---:|---:|
| 臺股期貨（TX） | {fmt(m['臺股期貨']['settlement'])} | {fmt(m['臺股期貨']['maintenance'])} | {fmt(m['臺股期貨']['original'])} |
| 小型臺指（MTX） | {fmt(m['小型臺指']['settlement'])} | {fmt(m['小型臺指']['maintenance'])} | {fmt(m['小型臺指']['original'])} |
| 微型臺指期貨（TMF） | {fmt(m['微型臺指期貨']['settlement'])} | {fmt(m['微型臺指期貨']['maintenance'])} | {fmt(m['微型臺指期貨']['original'])} |
| 臺指選擇權風險保證金（A值） | {fmt(m['臺指選擇權風險保證金(A)值']['settlement'])} | {fmt(m['臺指選擇權風險保證金(A)值']['maintenance'])} | {fmt(m['臺指選擇權風險保證金(A)值']['original'])} |
| 臺指選擇權風險保證金（B值） | {fmt(m['臺指選擇權風險保證金(B)值']['settlement'])} | {fmt(m['臺指選擇權風險保證金(B)值']['maintenance'])} | {fmt(m['臺指選擇權風險保證金(B)值']['original'])} |
| 臺指選擇權風險保證金（C值） | {fmt(m['臺指選擇權風險保證金(C)值']['settlement'])} | {fmt(m['臺指選擇權風險保證金(C)值']['maintenance'])} | {fmt(m['臺指選擇權風險保證金(C)值']['original'])} |

<p class="data-source-note">資料來源：<a href="https://www.taifex.com.tw/cht/5/indexMarging" target="_blank" rel="noopener noreferrer">臺灣期貨交易所－結算保證金一覽表</a>，期交所公告更新日期：{updated}</p>

## 幾個注意事項

- 期貨商向交易人收取的保證金及追繳標準，不得低於期交所公告的原始保證金與維持保證金水準，實際上各期貨商可能會收取更高的金額
- 小型臺指期貨週到期契約的保證金與小型臺指期貨相同；臺指選擇權週到期契約的風險保證金（A/B/C值），與臺指選擇權（月契約）相同
- 選擇權的 A、B、C 值對應不同的風險保證金計算情境（例如賣方單一部位、混合部位等），實際計收方式請以期交所公告的計算規則為準
- 這張表會隨期交所公告變動，交易前請以 [期交所官網最新公告](https://www.taifex.com.tw/cht/5/indexMarging) 為準

<p class="futures-disclaimer">以上內容僅為整理期交所公開資料，不構成投資建議。期貨與選擇權交易具有高風險，操作前請詳閱商品規則並評估自身風險承受度。</p>
"""


def render_institutional_positions(date: str, fut: dict) -> str:
    def rows(prod):
        lines = []
        for who in ["dealer", "trust", "foreign"]:
            v = fut[prod][who]
            lines.append(f"| {WHO_ZH[who]} | {fmt(v['oi_buy_lots'])} | {fmt(v['oi_sell_lots'])} | {v['oi_net_lots']:+,} |")
        return "\n".join(lines)

    return f"""---
layout: page
title: 三大法人未平倉部位
permalink: /futures-options/institutional-positions/
---

臺灣期貨交易所每個交易日都會公告「三大法人－區分各期貨契約」報表，記錄自營商、投信、外資及陸資三類法人在每個期貨商品上的交易口數與未平倉餘額。這裡整理其中「台指」相關的三個期貨商品：臺股期貨（TXF）、小型臺指期貨（MXF）、微型臺指期貨（TMF），並計算出各法人的淨部位。**本頁由每日自動更新程序同步。**

「未平倉餘額」等同一般俗稱的「未沖銷部位」，代表收盤當下仍持有、尚未平倉的部位；淨部位（多空淨額）＝多方口數－空方口數，正值代表淨多單、負值代表淨空單。

## 未平倉餘額（口數）

資料日期：{date}

### 臺股期貨（TXF）

| 身份別 | 多方 | 空方 | 淨部位 |
|---|---:|---:|---:|
{rows('TXF')}

### 小型臺指期貨（MXF）

| 身份別 | 多方 | 空方 | 淨部位 |
|---|---:|---:|---:|
{rows('MXF')}

### 微型臺指期貨（TMF）

| 身份別 | 多方 | 空方 | 淨部位 |
|---|---:|---:|---:|
{rows('TMF')}

<p class="data-source-note">資料來源：<a href="https://www.taifex.com.tw/cht/3/futContractsDate" target="_blank" rel="noopener noreferrer">臺灣期貨交易所－三大法人期貨每日交易口數與契約金額、未沖銷部位查詢</a></p>

## 換算成台指期貨等值的合計淨部位

臺股期貨、小型臺指、微型臺指的契約乘數不同（分別是指數每點新臺幣 200 元、50 元、10 元），口數不能直接相加比較。把小型臺指、微型臺指的口數依乘數比例換算成臺股期貨等值口數（小型臺指 ×0.25、微型臺指 ×0.05）後加總，可以得到每個法人在「台指期貨系列」上比較有意義的整體淨部位：

| 法人 | 臺股期貨淨部位 | 小型臺指等值 | 微型臺指等值 | 合計等值淨部位 |
|---|---:|---:|---:|---:|
{render_equiv_rows(fut)}

這個合計欄位是自行換算的推導值，不是期交所直接公告的數字，計算方式是：

```
合計等值淨部位 = 臺股期貨淨部位 + 小型臺指淨部位 × 0.25 + 微型臺指淨部位 × 0.05
```

## 使用上的提醒

- 這份資料**每日自動抓取臺灣期交所最新公告並更新**，資料日期以上方標示為準；期交所每個交易日收盤後公告新的數字
- 三大法人的未平倉，是眾多法人機構的加總結果，不代表單一機構的交易策略，也不能簡單解讀成「多空訊號」
- 投信在台指期貨系列常有大量淨多部位，主要是因為部分槓桿型 ETF（例如 2 倍做多台股 ETF）需要用期貨複製曝險，屬於被動避險性質的部位，不一定代表投信主動看多後市

<p class="futures-disclaimer">以上內容為期交所公開資料的整理與換算，不構成投資建議。期貨與選擇權交易具有高風險，操作前請詳閱商品規則並評估自身風險承受度。</p>
"""


def render_equiv_rows(fut: dict) -> str:
    lines = []
    for who in ["dealer", "trust", "foreign"]:
        tx = fut["TXF"][who]["oi_net_lots"]
        mx = fut["MXF"][who]["oi_net_lots"] * 0.25
        tm = fut["TMF"][who]["oi_net_lots"] * 0.05
        total = tx + mx + tm
        lines.append(f"| {WHO_ZH[who]} | {tx:+,} | {mx:+,.1f} | {tm:+,.1f} | **約 {total:+,.0f}** |")
    return "\n".join(lines)


def render_txo_chips(date: str, prev_date: str, chain: dict, opt3: dict, fut: dict, prev_fut: dict, prev_opt: dict) -> str:
    def cat_rows(cat, label):
        items = sorted((k for k, v in chain.items() if v["category"] == cat), key=lambda k: chain[k]["expiry"] or "")
        lines = []
        for k in items:
            v = chain[k]
            pcr = (v["total_put_oi"] / v["total_call_oi"]) if v["total_call_oi"] else 0
            expiry_fmt = f"{v['expiry'][0:4]}/{v['expiry'][4:6]}/{v['expiry'][6:8]}" if v["expiry"] else "-"
            lines.append(f"| {label} | {k} | {expiry_fmt} | {fmt(v['total_call_oi'])} | {fmt(v['total_put_oi'])} | {pcr:.2f} |")
        return lines

    listing_rows = cat_rows("週五選", "週五選") + cat_rows("週選", "週選（週三選）") + cat_rows("月選", "月選")

    def sr_rows(cat, label):
        items = sorted((k for k, v in chain.items() if v["category"] == cat), key=lambda k: chain[k]["expiry"] or "")
        lines = []
        for k in items:
            v = chain[k]
            call_txt = f"{fmt(int(v['max_call_strike']))}" if v["max_call_strike"] is not None else "—"
            put_txt = f"{fmt(int(v['max_put_strike']))} | {fmt(v['max_put_oi'])}" if v["max_put_strike"] is not None else f"— | {fmt(v['max_put_oi'])}（尚無賣權未平倉）"
            lines.append(f"| {k}（{label}） | {call_txt} | {fmt(v['max_call_oi'])} | {put_txt} |")
        return lines

    sr_all = sr_rows("週五選", "週五選") + sr_rows("週選", "週選") + sr_rows("月選", "月選")

    def opt3_table(cp):
        lines = []
        for who in ["dealer", "trust", "foreign"]:
            v = opt3[cp][who]
            lines.append(f"| {WHO_ZH[who]} | {fmt(v['oi_buy_lots'])} | {fmt(v['oi_sell_lots'])} | {v['oi_net_lots']:+,} |")
        return "\n".join(lines)

    def gross(who, f, o):
        total = 0
        for prod in ["TXF", "MXF", "TMF"]:
            v = f[prod][who]
            total += v["oi_buy_amt"] + v["oi_sell_amt"]
        for cp in ["Call", "Put"]:
            v = o[cp][who]
            total += v["oi_buy_amt"] + v["oi_sell_amt"]
        return total

    def detail(who, f, o):
        d = {}
        for prod in ["TXF", "MXF", "TMF"]:
            v = f[prod][who]
            d[prod] = v["oi_buy_amt"] + v["oi_sell_amt"]
        for cp in ["Call", "Put"]:
            v = o[cp][who]
            d[cp] = v["oi_buy_amt"] + v["oi_sell_amt"]
        return d

    value_rows = []
    detail_rows = []
    for who in ["dealer", "trust", "foreign"]:
        today_v = gross(who, fut, opt3)
        prev_v = gross(who, prev_fut, prev_opt)
        change = today_v - prev_v
        value_rows.append(
            f"| {WHO_ZH[who]} | {fmt(today_v)}（約{today_v/100000:,.0f}億） | {fmt(prev_v)}（約{prev_v/100000:,.0f}億） | **{change:+,}（約{change/100000:+,.0f}億）** |"
        )
        d = detail(who, fut, opt3)
        detail_rows.append(f"| {WHO_ZH[who]} | {fmt(d['TXF'])} | {fmt(d['MXF'])} | {fmt(d['TMF'])} | {fmt(d['Call'])} | {fmt(d['Put'])} |")

    return f"""---
layout: page
title: 台指選擇權籌碼分析
permalink: /futures-options/txo-chips/
---

整理臺指選擇權（TXO）目前三種到期契約類型（週三到期的「週選」、週五到期的「週五選」、月契約與季月契約的「月選」）的未平倉籌碼，並結合三大法人在台指期貨系列（大台、小台、微台）與臺指選擇權的未平倉部位，計算三大法人合計持有的契約市值與日增減。**本頁由每日自動更新程序同步。**

資料日期：{date}（比較基準日：{prev_date}）

## 一、目前掛牌的三種到期契約

| 類型 | 契約代碼 | 到期日 | 買權未平倉 | 賣權未平倉 | Put/Call 比 |
|---|---|---|---:|---:|---:|
{chr(10).join(listing_rows)}

<p class="data-source-note">資料來源：<a href="https://www.taifex.com.tw/cht/3/optDailyMarketReport" target="_blank" rel="noopener noreferrer">臺灣期貨交易所－選擇權每日交易行情</a></p>

週選、週五選各自最多同時掛牌 2 個到期序列（當週加次二週），月選則是 3 個連續近月＋2 個接續季月，符合 [選擇權入門](/futures-options/options-basics/) 提到的官方到期規則。

## 二、各契約最大未平倉量支撐與壓力

「最大未平倉量」是選擇權市場最常用的籌碼觀察方式：買權未平倉量最大的履約價，通常被視為短線的**壓力**；賣權未平倉量最大的履約價，通常被視為短線的**支撐**。邏輯是：能在單一履約價累積這麼大量的未平倉，多半是持續被賣方（資金部位較大的一方）承接的結果。

| 契約 | 壓力（買權最大OI履約價） | 買權OI量 | 支撐（賣權最大OI履約價） | 賣權OI量 |
|---|---:|---:|---:|---:|
{chr(10).join(sr_all)}

<p class="data-source-note">資料來源：同上，最大未平倉量履約價由每日行情表逐履約價比對計算</p>

## 三、三大法人對台指選擇權的未平倉口數

臺灣期貨交易所公告的三大法人資訊，只細分到「臺指選擇權整體的買權／賣權」，**沒有按週選／週五選／月選個別契約公布三大法人身份別**，所以下表是所有到期序列合計的數字，無法拆解到單一契約。

**買權（Call）**

| 身份別 | 未平倉買方（口） | 未平倉賣方（口） | 淨部位（口） |
|---|---:|---:|---:|
{opt3_table('Call')}

**賣權（Put）**

| 身份別 | 未平倉買方（口） | 未平倉賣方（口） | 淨部位（口） |
|---|---:|---:|---:|
{opt3_table('Put')}

<p class="data-source-note">資料來源：<a href="https://www.taifex.com.tw/cht/3/callsAndPutsDate" target="_blank" rel="noopener noreferrer">臺灣期貨交易所－三大法人-選擇權買賣權分計</a></p>

## 四、莊家籌碼落在哪個履約價

期交所並不會公布「自營商在某個履約價持有多少部位」這種精細到履約價層級、又區分身份別的資料——公開資料只到「整體買權／賣權的身份別未平倉」（如第三節）或「不分身份別、逐履約價的未平倉量」（如第二節）兩種granularity，兩者無法直接相乘還原出「自營商在某履約價的部位」。

實務上，市場慣用的替代做法，就是把第二節「最大未平倉量」的履約價，當作籌碼集中最明顯的位置來觀察，理由是：能承接這麼大量未平倉的一方，通常是資金部位較大、以收權利金為主要策略的賣方（也就是俗稱的「莊家」）。這是根據公開的未平倉量分佈做的推論，**不是期交所直接公告「自營商在此履約價」的數字**，解讀時請以這個限制為前提。

## 五、三大法人合計契約市值與日增減（含大台、小台、微台）

把台指期貨系列（臺股期貨 TXF、小型臺指 MXF、微型臺指 TMF）與臺指選擇權（TXO 買權＋賣權）的未平倉契約金額全部加總，可以得到每個法人目前在「台指全系列商品」上總共持有多少市值部位。這裡採計「持有部位市值」＝多方契約金額＋空方契約金額（即不論多空方向，全部未平倉部位的市值加總）。

單位：新臺幣千元

| 法人 | {date} 市值 | {prev_date} 市值 | 較昨日增減 |
|---|---:|---:|---:|
{chr(10).join(value_rows)}

<p class="data-source-note">計算方式：分別加總 TXF／MXF／TMF／TXO買權／TXO賣權 五項商品的「未平倉買方契約金額＋未平倉賣方契約金額」；原始數字取自臺灣期貨交易所三大法人期貨與選擇權每日公告</p>

### 各商品市值明細（千元，多方＋空方合計）

| 法人 | TXF | MXF | TMF | TXO買權 | TXO賣權 |
|---|---:|---:|---:|---:|---:|
{chr(10).join(detail_rows)}

## 使用上的提醒

- 這是**單一交易日（{date}）的快照**與前一個交易日（{prev_date}）的比較，**由每日自動更新程序同步**
- 「最大未平倉量支撐壓力」與「莊家籌碼」是市場慣用的推論方法，不是期交所直接公告的身份別履約價資料，僅供參考
- 契約市值的加總是把期貨和選擇權的名目市值直接相加，兩者的風險屬性不同（期貨市值接近實際曝險，選擇權市值和實際風險曝險不完全等同），解讀時請留意這個差異

<p class="futures-disclaimer">以上內容為期交所公開資料的整理、換算與市場慣用方法的說明，不構成投資建議。期貨與選擇權交易具有高風險，操作前請詳閱商品規則並評估自身風險承受度。</p>
"""


# ---------- orchestration ----------

def main():
    margin_html = fetch("https://www.taifex.com.tw/cht/5/indexMarging")
    fut_html = fetch("https://www.taifex.com.tw/cht/3/futContractsDate")
    opt_html = fetch("https://www.taifex.com.tw/cht/3/callsAndPutsDate")
    chain_html = fetch("https://www.taifex.com.tw/cht/3/optDailyMarketReport")

    margin = parse_margin_table(margin_html)
    fut = parse_three_major_futures(fut_html)
    opt3 = parse_three_major_options(opt_html)
    chain = parse_option_chain(chain_html)
    today = parse_report_date(fut_html)

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.exists():
        snapshot = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    else:
        snapshot = None

    (ROOT / "futures-options" / "margin-table.md").write_text(render_margin_table(margin), encoding="utf-8")

    if snapshot is None or snapshot["date"] == today:
        print(f"no prior snapshot to compare, or no new trading day (today={today}); "
              f"skipping institutional-positions.md and txo-chips.md regeneration")
    else:
        prev_date = snapshot["date"]
        prev_fut = snapshot["fut"]
        prev_opt = snapshot["opt"]
        (ROOT / "futures-options" / "institutional-positions.md").write_text(
            render_institutional_positions(today, fut), encoding="utf-8"
        )
        (ROOT / "futures-options" / "txo-chips.md").write_text(
            render_txo_chips(today, prev_date, chain, opt3, fut, prev_fut, prev_opt), encoding="utf-8"
        )
        print(f"regenerated pages for {today} (compared against {prev_date})")

    STATE_PATH.write_text(json.dumps({"date": today, "fut": fut, "opt": opt3}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("done")


if __name__ == "__main__":
    main()
