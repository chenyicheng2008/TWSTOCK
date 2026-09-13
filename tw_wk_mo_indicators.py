"""
台股週線/月線更新 + 週/月技術指標（以最新日期為準，視當前週/月為已收盤）

流程：
  1. FinLab 全市場還原 OHLC（長歷史，免費層到 T-數日）為基底
  2. Fugle 逐檔補足最新交易日（到今日最新，60/min 限速）
  3. 日線合併 → 重採樣週線(W-MON) / 月線(MS)；當前未完成的週/月「視為已收盤」納入為最後一根
  4. 計算週/月技術指標：KD(9,3,3)、MACD(12,26,9)、MA、RSI(14)、OBV
  5. 更新本地 weekly/ 與 daily/；輸出最新快照面板 tw_weekly_indicators.csv / tw_monthly_indicators.csv

用法：
  python tw_wk_mo_indicators.py            # 全市場
  python tw_wk_mo_indicators.py 2330 2434  # 只跑指定股（測試用）
"""
import os, sys, csv, json, time, datetime, urllib.request, warnings
warnings.filterwarnings('ignore')
import pandas as pd, numpy as np

# 資料根目錄：環境變數 TWSTOCK_DIR，否則為本檔所在資料夾（本機與 GitHub Actions 通用）
BASE   = os.environ.get('TWSTOCK_DIR',
                        os.path.dirname(os.path.abspath(__file__)))
WEEKLY_DIR = f'{BASE}/weekly'
DAILY_DIR  = f'{BASE}/daily'
SYMBOLS_FILE = f'{BASE}/all_symbols_full.json'
# 金鑰一律從環境變數讀取（GitHub Actions 由 Secrets 注入），不寫進程式碼
FUGLE_KEY    = os.environ.get('FUGLE_API_KEY', '')
FINLAB_TOKEN = os.environ.get('FINLAB_TOKEN', '')
FIELDNAMES = ['date','open','high','low','close','volume','turnover','change']

today = datetime.date.today().isoformat()

# ── 技術指標 ─────────────────────────────────────────────────────────
def kd(df, n=9, k_sm=3, d_sm=3):
    low_n  = df['low'].rolling(n, min_periods=1).min()
    high_n = df['high'].rolling(n, min_periods=1).max()
    rng = (high_n - low_n).replace(0, np.nan)
    rsv = ((df['close'] - low_n) / rng * 100).fillna(50)
    a_k = 1.0 / k_sm; a_d = 1.0 / d_sm
    K = np.empty(len(df)); D = np.empty(len(df))
    pk = pd = 50.0
    for i, r in enumerate(rsv.values):
        pk = pk*(1-a_k) + r*a_k
        pd = pd*(1-a_d) + pk*a_d
        K[i], D[i] = pk, pd
    return pd_series(K, df.index), pd_series(D, df.index)

def pd_series(arr, idx): return pd.Series(arr, index=idx)

def macd(close, fast=12, slow=26, sig=9):
    dif = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    dea = dif.ewm(span=sig, adjust=False).mean()
    return dif, dea, (dif - dea)   # DIF, DEA(MACD), OSC(柱)

def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100/(1+rs)).fillna(50)

def obv(close, vol):
    sign = np.sign(close.diff().fillna(0))
    return (sign * vol).cumsum()

def add_indicators(df, ma_list):
    out = pd.DataFrame(index=df.index)
    out['close'] = df['close'].round(2)
    out['K'], out['D'] = kd(df)
    out['K'] = out['K'].round(1); out['D'] = out['D'].round(1)
    dif, dea, osc = macd(df['close'])
    out['DIF'] = dif.round(3); out['MACD'] = dea.round(3); out['OSC'] = osc.round(3)
    out['RSI'] = rsi(df['close']).round(1)
    out['OBV'] = obv(df['close'], df['volume']).astype('int64')
    for m in ma_list:
        out[f'MA{m}'] = df['close'].rolling(m, min_periods=1).mean().round(2)
    return out

# ── 重採樣（當前未完成週/月視為已收盤 → 自然納入為最後一根）──────────
def resample_ohlc(daily, rule):
    agg = {'open':'first','high':'max','low':'min','close':'last','volume':'sum'}
    return daily.resample(rule, label='left', closed='left').agg(agg).dropna(subset=['close'])

# ── 資料來源 ─────────────────────────────────────────────────────────
def fugle_daily(sym, frm, to):
    url = (f'https://api.fugle.tw/marketdata/v1.0/stock/historical/candles/{sym}'
           f'?from={frm}&to={to}&timeframe=D&adjusted=true'
           f'&fields=open,high,low,close,volume,turnover,change&sort=asc')
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={'X-API-KEY': FUGLE_KEY})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read()).get('data', [])
        except Exception as e:
            if '429' in str(e): time.sleep((attempt+1)*10)
            else: return []
    return []

def write_daily_csv(fpath, df):
    df = df.copy()
    df['change'] = df['close'].diff().round(2).fillna(0)
    df.index = pd.to_datetime(df.index).strftime('%Y-%m-%d')
    df[['open','high','low','close']] = df[['open','high','low','close']].round(2)
    df['volume'] = df['volume'].fillna(0).astype('int64')
    if 'turnover' not in df: df['turnover'] = 0
    df['turnover'] = df['turnover'].fillna(0).astype('int64')
    df.reset_index(names='date')[FIELDNAMES].to_csv(fpath, index=False)

def write_weekly_csv(fpath, wk):
    wk = wk.copy()
    wk['change'] = wk['close'].diff().round(2).fillna(0)
    wk['turnover'] = 0
    wk.index = pd.to_datetime(wk.index).strftime('%Y-%m-%d')
    wk[['open','high','low','close']] = wk[['open','high','low','close']].round(2)
    wk['volume'] = wk['volume'].fillna(0).astype('int64')
    wk.reset_index(names='date')[FIELDNAMES].to_csv(fpath, index=False)

# ── 主流程 ───────────────────────────────────────────────────────────
def main():
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    if not FINLAB_TOKEN:
        sys.exit('未設定 FINLAB_TOKEN 環境變數（此舊流程需要 FinLab）')
    import finlab
    from finlab import data as fdata
    finlab.login(FINLAB_TOKEN)
    adj = {k: fdata.get(f'etl:adj_{k}') for k in ['open','high','low','close']}
    raw_c = fdata.get('price:收盤價'); vol = fdata.get('price:成交股數')
    fl_latest = adj['close'].index[-1].date().isoformat()

    # 目標最新日：以 Fugle 為準（免費 FinLab 常落後）
    probe = fugle_daily('2330', fl_latest, today)
    target_end = probe[-1]['date'][:10] if probe else fl_latest
    print(f'FinLab最新={fl_latest}  Fugle目標最新={target_end}', flush=True)

    with open(SYMBOLS_FILE, encoding='utf-8') as f:
        symbols = args if args else json.load(f)

    wk_rows, mo_rows = [], []
    done = topup = 0
    for sym in symbols:
        if sym not in adj['close'].columns:
            continue
        c_adj = adj['close'][sym].dropna()
        c_raw = raw_c[sym].dropna()
        if c_adj.empty or c_raw.empty:
            continue
        # Fugle 補最新交易日（FinLab 落後時）— 先抓，因為要用它來錨定還原基準
        e = None
        if fl_latest < target_end:
            ext = fugle_daily(sym, fl_latest, target_end)
            time.sleep(1.05)
            if ext:
                e = pd.DataFrame(ext)
                e['date'] = pd.to_datetime(e['date'].str[:10])
                e = e.set_index('date')[['open','high','low','close','volume']].astype(float)
                topup += 1

        # 還原基準對齊（重要）：
        # FinLab 的 adj 只還原到它自己的最後日(fl_latest)；若個股在 fl_latest 之後才除權息，
        # Fugle(adjusted=true) 已把股利還原進去，兩段基準會差一個股利、接縫出現假跌幅。
        # 因此改用「Fugle 在重疊日的還原收盤價」當錨點，把 FinLab 歷史對齊到當前基準。
        anchor_dt = pd.Timestamp(fl_latest)
        c_adj.index = pd.to_datetime(c_adj.index)
        if e is not None and anchor_dt in e.index and anchor_dt in c_adj.index \
                and c_adj.loc[anchor_dt] != 0:
            scale = e.loc[anchor_dt, 'close'] / c_adj.loc[anchor_dt]
        else:
            scale = c_raw.iloc[-1] / c_adj.iloc[-1]    # 退回：無重疊日時用市價錨定

        daily = pd.DataFrame({
            'open':  adj['open'][sym]*scale, 'high': adj['high'][sym]*scale,
            'low':   adj['low'][sym]*scale,  'close': adj['close'][sym]*scale,
            'volume': vol[sym]}).dropna(subset=['close'])
        daily.index = pd.to_datetime(daily.index)

        if e is not None:
            daily = pd.concat([daily[daily.index < e.index[0]], e])
        daily = daily.sort_index()
        daily[['open','high','low','close']] = daily[['open','high','low','close']].round(2)

        # 重採樣（含未完成當週/當月）
        wk = resample_ohlc(daily, 'W-MON')
        mo = resample_ohlc(daily, 'MS')
        if len(wk) < 10 or len(mo) < 6:
            continue

        # 更新本地檔（daily 只留近420天、weekly 全歷史）
        write_daily_csv(f'{DAILY_DIR}/{sym}.csv',
                        daily[daily.index >= (pd.Timestamp(today) - pd.Timedelta(days=420))])
        write_weekly_csv(f'{WEEKLY_DIR}/{sym}.csv', wk)

        # 指標快照（最後一根＝當前週/月，視為已收盤）
        wi = add_indicators(wk, [5,10,20,60]).iloc[-1]
        mi = add_indicators(mo, [3,6,12]).iloc[-1]
        wk_rows.append({'symbol': sym, 'date': wk.index[-1].strftime('%Y-%m-%d'), **wi.to_dict()})
        mo_rows.append({'symbol': sym, 'date': mo.index[-1].strftime('%Y-%m-%d'), **mi.to_dict()})

        done += 1
        if done % 200 == 0:
            print(f'{done} 檔完成 (Fugle補{topup})', flush=True)

    pd.DataFrame(wk_rows).to_csv(f'{BASE}/tw_weekly_indicators.csv', index=False, encoding='utf-8-sig')
    pd.DataFrame(mo_rows).to_csv(f'{BASE}/tw_monthly_indicators.csv', index=False, encoding='utf-8-sig')
    # 週/月指標最新日以「全市場最大值」回報，而非迴圈最後一檔（避免誤導）
    wk_latest = max((r['date'] for r in wk_rows), default='-')
    mo_latest = max((r['date'] for r in mo_rows), default='-')
    print(f'\n完成：{done} 檔，Fugle補足 {topup} 檔', flush=True)
    print(f'週指標最新日：{wk_latest}  → tw_weekly_indicators.csv', flush=True)
    print(f'月指標最新日：{mo_latest}  → tw_monthly_indicators.csv', flush=True)

    merge_all_csv()

    # 市場別/產業別對照表（FinLab security_categories，用量約1MB）
    try:
        from tw_market_category import build as build_market_category
        build_market_category()
    except Exception as e:
        print(f'market_category 更新失敗（不影響主資料）: {str(e)[:80]}', flush=True)


def merge_all_csv():
    """重新合併全市場週/日線總表（tw_all_weekly.csv / tw_all_daily_1yr.csv）。
    務必在個股 weekly/daily CSV 更新後執行，否則總表會停留在舊資料。"""
    import glob
    for dirname, outfile in [(WEEKLY_DIR, f'{BASE}/tw_all_weekly.csv'),
                             (DAILY_DIR, f'{BASE}/tw_all_daily_1yr.csv')]:
        files = sorted(glob.glob(f'{dirname}/*.csv'))
        total = 0
        with open(outfile, 'w', newline='', encoding='utf-8') as fout:
            writer = csv.DictWriter(fout, fieldnames=['symbol']+FIELDNAMES)
            writer.writeheader()
            for fp in files:
                sym = os.path.basename(fp).replace('.csv', '')
                with open(fp, newline='', encoding='utf-8') as fin:
                    for row in csv.DictReader(fin):
                        row['symbol'] = sym
                        writer.writerow(row)
                        total += 1
        print(f'{os.path.basename(outfile)}: {len(files)} 支, {total:,} 筆', flush=True)


if __name__ == '__main__':
    main()
