# TWSTOCK — 台股全市場日線 / 週線資料

每個交易日台灣時間 18:30，由 GitHub Actions 自動更新（`.github/workflows/update-data.yml`）。

## 資料

| 位置 | 內容 |
|---|---|
| `daily/{代號}.csv` | 近 420 天日線（還原價） |
| `weekly/{代號}.csv` | 完整歷史週線（週一為 bar 標籤） |
| `tw_weekly_indicators.csv` / `tw_monthly_indicators.csv` | 最新一根週/月 K 的技術指標快照 |
| Release [`latest`](../../releases/tag/latest) | 合併總表 `tw_all_weekly.csv.gz`、`tw_all_daily_1yr.csv.gz`（每日覆蓋，不進 git 歷史） |

欄位：`date, open, high, low, close, volume, turnover, change`

## 流程（`tw_rebuild_hybrid.py`）

0. 取 TWSE / TPEx OpenAPI 全市場原始收盤價當錨點（失敗的市場才退回 FinLab）
1. yfinance 下載全市場長歷史
2. 找出錨點日後有除權息、或 yfinance 落後的個股，用 Fugle 校正
3. 寫出個股 CSV、週/月指標、合併總表

## 設定

Repo → Settings → Secrets and variables → Actions → New repository secret：

| Secret | 必要 | 用途 |
|---|---|---|
| `FUGLE_API_KEY` | ✅ | Fugle 行情 API（校正近期除權息與最新日） |
| `FINLAB_TOKEN` | 選用 | 市場別對照表更新、錨點備援 |

## 本機同步

```
python sync_local.py
```

`git pull` 並下載最新合併總表到 `C:\Users\cheny\Pictures\Claud\fugle\data`。
