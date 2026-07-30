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