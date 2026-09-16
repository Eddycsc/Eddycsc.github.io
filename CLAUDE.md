# CLAUDE.md

Jekyll + GitHub Pages 個人站。`futures-options/` 的行情頁由 GitHub Actions 每交易日抓
期交所／證交所公開資料自動重產並 commit，其餘頁面是手寫的。先看地圖再動手，不要整包
掃描或整份 `cat` 腳本。

## 檔案地圖

| 路徑 | 性質 |
|---|---|
| `scripts/common.py` | 共用抓取／重試／錯誤格式，所有對外請求都走這裡 |
| `scripts/taifex_daily_update.py` | → margin-table、institutional-positions、txo-chips |
| `scripts/twse_daily_update.py` | → twse-data |
| `scripts/global_indices_update.py` | → global-indices |
| `scripts/state/*.json` | 機器寫的前一交易日快照，**不要手改或整份讀** |
| 上述四頁與 `global-indices.md` | **自動產生，手改會被覆蓋**；改版面請改腳本裡的 `render_*()` |
| 其餘 `.md`、`_posts/` | 手寫，可直接編輯 |
| `.github/workflows/` | 兩個排程：TAIFEX 09:00 UTC、指數＋證交所 11:00 UTC |

## 開發與除錯

- 檢查語法 `python -m py_compile scripts/*.py`；沒有測試套件，也不用開 bundler。
- **雲端沙箱連不到 taifex/twse（proxy 回 403）**，實跑必定失敗，別追；要驗證邏輯就 stub
  掉 `common.fetch_text` / `fetch_json`。
- 排程失敗只要看 job log 最後一行 `ERROR: <步驟>: <原因>`（`failed_only: true`、
  `tail_lines: 40`），步驟名對應 `main()` 裡的 `step(...)`，不必重讀整支腳本。
- 來源分級：Tier 1 官方報表失敗即中止該頁；Tier 2（Yahoo 國際商品）逐檔降級成「—」；
  Tier 3（融資維持率）失敗只降級該區塊。

## 省 token

- 一個對話只做一件事，做完 `/clear`；長對話每一輪都要重送整段歷史。
- 例行修改用一般 effort 就夠，`max`/`xhigh` 留給難查的 bug。
- 先 `grep -n` 定位再 `sed -n` 取片段；MCP 查詢帶 `minimal_output`、`perPage` ≤ 10。
