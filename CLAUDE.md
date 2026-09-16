# CLAUDE.md

Jekyll + GitHub Pages 個人站台。除了幾篇手寫文章外，`futures-options/` 的行情頁面是
GitHub Actions 每個交易日抓期交所／證交所公開資料後自動重產並 commit 的。

先讀這份地圖再動手，不要整包掃描或整份 `cat` 腳本（三支腳本合計約 1,300 行）。

## 檔案地圖

| 路徑 | 性質 |
|---|---|
| `scripts/common.py` | 共用 HTTP 抓取、重試、錯誤格式（所有對外請求都走這裡） |
| `scripts/taifex_daily_update.py` | 產生 margin-table / institutional-positions / txo-chips |
| `scripts/twse_daily_update.py` | 產生 twse-data |
| `scripts/global_indices_update.py` | 產生 global-indices |
| `scripts/state/*.json` | 機器寫入的前一交易日快照，用來算日增減：**不要手改、不要整份讀** |
| `futures-options/{margin-table,institutional-positions,txo-chips,twse-data,global-indices}.md` | **全自動產生，手改會被下一次排程覆蓋**；要改版面請改對應腳本裡的 `render_*()` |
| `futures-options/{index,futures-basics,options-basics,glossary}.md`、`about.md`、`_posts/` | 人工撰寫，可直接編輯 |
| `.github/workflows/update-taifex.yml` | 每交易日 09:00 UTC 跑 TAIFEX 更新 |
| `.github/workflows/update-market-data.yml` | 每交易日 11:00 UTC 跑指數 + 證交所更新 |

## 驗證方式

這個 repo 沒有測試套件，也不需要開 bundler 就能改腳本：

```
python -m py_compile scripts/*.py            # 語法檢查
python scripts/taifex_daily_update.py        # 實跑（需要對外網路）
```

**Claude Code 雲端沙箱連不到 taifex.com.tw / twse.com.tw（proxy 回 403）**，實跑一定失敗，
不要花好幾輪去追。要驗證邏輯就 stub 掉 `common.fetch_text` / `fetch_json` 再呼叫該模組的
函式，或直接跑 `main()` 前先把腳本內的 `fetch_json` 指向假資料。

## 上游資料分級

- **Tier 1（期交所、證交所報表）**：失敗就中止該頁重產，`step()` 會輸出一行
  `ERROR: <步驟>: <原因>` 並以 exit 1 結束。
- **Tier 2（Yahoo Finance 國際商品）**：逐檔隔離，單檔失敗只會讓那一列顯示「—」。
- **Tier 3（`openapi.twse.com.tw` 融資維持率）**：失敗只降級該區塊，頁面照常產生。

排程失敗時的固定流程（照做通常兩輪內結束）：取該次 job 的 log（`failed_only: true`、
`tail_lines: 40` 就夠），日誌最後一行就是 `ERROR: <步驟>: <原因>`，步驟名稱直接對應
腳本 `main()` 裡同名的 `step(...)`，不需要再把整支腳本讀一遍。

## 省 token 的工作習慣

這個 repo 的工作幾乎都是小幅改 markdown / Python，成本應該很低，會爆量通常是下面幾件事：

- **一個對話只做一件事，做完 `/clear`**。對話不清掉的話，每一輪都要重送整段歷史，
  成本隨對話長度線性累加；跨好幾天的長對話是最貴的用法。
- 例行工作（改文案、看 log、跑腳本）用一般 effort 就夠；`max`/`xhigh` effort 會大幅
  增加每輪的思考輸出，留給真的難查的 bug。
- 讀檔先 `grep -n` 定位，再 `sed -n 'a,bp'` 取片段；不要整份 `cat` 腳本或自動產生的頁面。
- GitHub MCP 查詢帶 `minimal_output: true`、`perPage` ≤ 10；看 workflow 失敗用
  `failed_only: true` + `tail_lines: 40`，不要抓整份 log（一次可以吃掉數千 token）。
- 想固定用較便宜的模型跑這個 repo，在 `.claude/settings.json` 加一行即可：
  `"model": "claude-sonnet-5"`。
