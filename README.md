# Eddy的備忘錄

用 Jekyll 建置的個人部落格，透過 GitHub Pages 免費發布於：
https://Eddycsc.github.io

## 如何新增文章

在 `_posts/` 資料夾新增一個檔案，檔名格式為 `YYYY-MM-DD-標題.md`，內容格式參考已存在的範例文章。檔案開頭需要有以下區塊：

```
---
layout: post
title: "文章標題"
date: 2026-01-01 12:00:00 +0800
categories: 分類
---

正文內容...
```

推送到 GitHub 後，Pages 會自動重新編譯發布，通常一兩分鐘內生效。

## 本機預覽（選用）

需要安裝 Ruby 與 Bundler：

```
bundle install
bundle exec jekyll serve
```

然後開啟 http://localhost:4000

## 期交所資料自動更新

`futures-options/margin-table.md`、`futures-options/institutional-positions.md`、`futures-options/txo-chips.md` 三頁由 `.github/workflows/update-taifex.yml` 排程，每個交易日 17:00（台北時間）自動執行 `scripts/taifex_daily_update.py`：抓取臺灣期交所公開報表、重新計算、覆寫這三個檔案，並自動 commit + push。若當天沒有新的交易日資料（例如假日），腳本會自動略過、不產生變更。

`scripts/state/taifex_snapshot.json` 存放前一個交易日的原始資料，用來計算日增減，請勿手動刪除。若要立即手動觸發一次更新，可以在 GitHub 上對這個 repo 執行 Actions → Update TAIFEX data → Run workflow，或本機執行：

```
python scripts/taifex_daily_update.py
```