"""
台股全市場重建 — yfinance 版（10年日線 → 週線/月線 + 技術指標）

相較 FinLab+Fugle 版的優勢：
  - 單一來源、auto_adjust 恆錨定今日 → 不會有「兩段還原基準拼接錯位」問題
  - 全市場 10 年歷史約 4~5 分鐘（FinLab+Fugle 約 35~40 分鐘）
  - 免 token、免每日配額；但需自行節流（實測 chunk=100 + 2 秒穩定、零限流）

安全設計：預設輸出到 *_yf 目錄，不覆蓋正式資料，供交叉比對驗證。
  python tw_rebuild_from_yfinance.py              # → daily_yf/ weekly_yf/（比對用）
  python tw_rebuild_from_yfinance.py --production # → daily/ weekly/（正式，會覆蓋）

市場別後綴由 tw_market_category.csv 提供（.TW 上市 / .TWO 上櫃）。
"""
import os, sys, csv, json, time, argparse, warnings
warnings.filterwarnings('ignore')
import pandas as pd

# 資料根目錄：環境變數 TWSTOCK_DIR，否則為本檔所在資料夾
BASE = os.environ.get('TWSTOCK_DIR',
                      os.path.dirname(os.path.abspath(__file__)))
MARKET_CSV = f'{BASE}/tw_market_category.csv'
FIELDNAMES = ['date', 'open', 'high', 'low', 'close', 'volume', 'turnover', 'change']

CHUNK, PAUSE = 100, 2          # 實測穩定的節流參數
PERIOD = 'max'                 # 完整歷史（台股多可回溯至 2000 年），耗時與 10y 幾乎相同


def load_tickers():
    """回傳 [(symbol, yf_ticker)]。

    股票池 = FinLab security_categories（有正確市場別後綴）
           ∪ all_symbols_full.json（ISIN 清單，涵蓋 FinLab 未收錄者）
    FinLab 未收錄的個股先預設 .TW；download_all 失敗時會自動改試 .TWO。
    """
    mc = pd.read_csv(MARKET_CSV, encoding='utf-8-sig', dtype=str)
    mc = mc[mc['yf_ticker'].notna() & (mc['yf_ticker'] != '-')]
    pairs = [(r['symbol'], r['yf_ticker']) for _, r in mc.iterrows()]
    known = {s for s, _ in pairs}

    syms_file = os.path.join(os.path.dirname(MARKET_CSV), 'all_symbols_full.json')
    extra = 0
    try:
        with open(syms_file, encoding='utf-8') as f:
            for s in json.load(f):
                s = str(s).strip()
                if s and s not in known:
                    pairs.append((s, f'{s}.TW'))   # 後綴未知，失敗時自動改試 .TWO
                    known.add(s); extra += 1
    except Exception as e:
        print(f'  (讀 all_symbols_full.json 失敗，僅用 FinLab 清單: {str(e)[:50]})', flush=True)
    if extra:
        print(f'  股票池: FinLab {len(pairs)-extra} + ISIN補充 {extra} = {len(pairs)} 檔', flush=True)
    return pairs


def alt_suffix(tk):
    return tk.replace('.TWO', '.TW') if tk.endswith('.TWO') else tk.replace('.TW', '.TWO')


def download_all(pairs):
    """分批下載，回傳 {symbol: DataFrame}；含一輪重試 + 反向後綴重試"""
    import yfinance as yf
    out, failed = {}, []
    t0 = time.time()

    def fetch(batch):          # batch: [(sym, tk)]
        got, miss = {}, []
        tks = [tk for _, tk in batch]
        try:
            d = yf.download(tks, period=PERIOD, auto_adjust=True,
                            group_by='ticker', progress=False, threads=True)
            lv = d.columns.get_level_values(0)
            for sym, tk in batch:
                if tk in lv:
                    sub = d[tk].dropna(subset=['Close'])
                    if len(sub):
                        got[sym] = sub
                        continue
                miss.append((sym, tk))
        except Exception as e:
            print(f'  批次失敗: {str(e)[:70]}', flush=True)
            miss = batch
        return got, miss

    for i in range(0, len(pairs), CHUNK):
        g, m = fetch(pairs[i:i + CHUNK])
        out.update(g); failed += m
        if (i // CHUNK) % 5 == 0:
            print(f'  {i + CHUNK}/{len(pairs)} 成功{len(out)} ({time.time()-t0:.0f}s)', flush=True)
        time.sleep(PAUSE)

    # 重試1：原後綴（多為暫時性 DNS/網路錯誤）
    if failed:
        print(f'重試 {len(failed)} 檔（原後綴）…', flush=True)
        time.sleep(5)
        still = []
        for i in range(0, len(failed), CHUNK):
            g, m = fetch(failed[i:i + CHUNK])
            out.update(g); still += m
            time.sleep(PAUSE)
        failed = still

    # 重試2：反向後綴（市場別對照可能過期，如轉上市/轉上櫃）
    if failed:
        print(f'重試 {len(failed)} 檔（反向後綴）…', flush=True)
        time.sleep(5)
        swapped = [(s, alt_suffix(t)) for s, t in failed]
        still = []
        for i in range(0, len(swapped), CHUNK):
            g, m = fetch(swapped[i:i + CHUNK])
            out.update(g); still += m
            time.sleep(PAUSE)
        failed = still

    return out, [s for s, _ in failed]


def to_ohlc(df):
    """yfinance 欄位 → 本專案格式（小寫、去時區）"""
    d = df.rename(columns=str.lower)[['open', 'high', 'low', 'close', 'volume']].copy()
    if getattr(d.index, 'tz', None) is not None:
        d.index = d.index.tz_localize(None)
    return d.sort_index()


def resample_ohlc(daily, rule):
    agg = {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}
    return daily.resample(rule, label='left', closed='left').agg(agg).dropna(subset=['close'])


def write_csv(fpath, df):
    d = df.copy()
    d['change'] = d['close'].diff().round(2).fillna(0)
    d['turnover'] = 0                      # yfinance 無成交金額（現行資料本就為 0）
    d[['open', 'high', 'low', 'close']] = d[['open', 'high', 'low', 'close']].round(2)
    d['volume'] = d['volume'].fillna(0).astype('int64')
    d.index = pd.to_datetime(d.index).strftime('%Y-%m-%d')
    d.reset_index(names='date')[FIELDNAMES].to_csv(fpath, index=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--production', action='store_true',
                    help='寫入正式 daily/ weekly/（預設寫 *_yf 比對目錄）')
    ap.add_argument('--limit', type=int, default=0, help='只跑前 N 檔（測試用）')
    a = ap.parse_args()

    daily_dir = f'{BASE}/daily' if a.production else f'{BASE}/daily_yf'
    weekly_dir = f'{BASE}/weekly' if a.production else f'{BASE}/weekly_yf'
    os.makedirs(daily_dir, exist_ok=True); os.makedirs(weekly_dir, exist_ok=True)
    print(f'輸出目錄: {daily_dir} / {weekly_dir}', flush=True)

    pairs = load_tickers()
    if a.limit:
        pairs = pairs[:a.limit]
    print(f'個股數: {len(pairs)}', flush=True)

    t0 = time.time()
    data, failed = download_all(pairs)
    print(f'\n下載完成: {len(data)} 檔成功、{len(failed)} 檔失敗、{(time.time()-t0)/60:.1f} 分鐘', flush=True)

    daily_keep = pd.Timestamp.today() - pd.Timedelta(days=420)
    wrote = 0
    for sym, raw in data.items():
        d = to_ohlc(raw)
        if len(d) < 30:
            continue
        write_csv(f'{daily_dir}/{sym}.csv', d[d.index >= daily_keep])
        write_csv(f'{weekly_dir}/{sym}.csv', resample_ohlc(d, 'W-MON'))
        wrote += 1
    print(f'寫出 {wrote} 檔 → {daily_dir} / {weekly_dir}', flush=True)
    if failed:
        print(f'失敗清單（多為已下市）: {failed[:20]}{" …" if len(failed)>20 else ""}', flush=True)
    print(f'\n總耗時 {(time.time()-t0)/60:.1f} 分鐘', flush=True)


if __name__ == '__main__':
    main()
