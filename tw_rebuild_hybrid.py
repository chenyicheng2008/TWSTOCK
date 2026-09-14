"""
台股全市場重建 — 混合版（yfinance 長歷史 + Fugle 除權息校正）

設計理由（實測依據）：
  - yfinance：全市場 10 年歷史僅 4.4 分鐘、免配額，88.8% 與 Fugle 完全一致
  - 但 yfinance 對「最近數日」的除權息還原有延遲（07-16 已正確、07-21/07-24 未更新）
  - Fugle：除權息即時正確，但 60 次/分，全市場要 35 分鐘
  → 只對「近期可能除權息」的個股用 Fugle 校正，其餘直接採用 yfinance

還原基準對齊（關鍵）：
  對需校正的個股，以「Fugle 在重疊日的還原收盤價」為錨點重新縮放 yfinance 歷史，
  確保兩段落在同一還原基準，避免接縫假跳空。

用法:
  python tw_rebuild_hybrid.py                    # 寫入 daily/ weekly/ + 指標 + 合併總表
  python tw_rebuild_hybrid.py --dry              # 寫入 *_hy 目錄（不動正式資料）
  python tw_rebuild_hybrid.py --gap-days 6 --gap-th 3.0
"""
import os, sys, csv, json, time, argparse, warnings
warnings.filterwarnings('ignore')
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tw_wk_mo_indicators import (
    BASE, FIELDNAMES, fugle_daily, resample_ohlc, add_indicators, merge_all_csv,
)
from tw_rebuild_from_yfinance import load_tickers, download_all, to_ohlc

FUGLE_BACKFILL_DAYS = 20          # Fugle 校正抓取天數（需涵蓋近期除權息）


def write_csv(fpath, df):
    d = df.copy()
    d['change'] = d['close'].diff().round(2).fillna(0)
    if 'turnover' not in d:
        d['turnover'] = 0
    d[['open', 'high', 'low', 'close']] = d[['open', 'high', 'low', 'close']].round(2)
    d['volume'] = d['volume'].fillna(0).astype('int64')
    d['turnover'] = d['turnover'].fillna(0).astype('int64')
    d.index = pd.to_datetime(d.index).strftime('%Y-%m-%d')
    d.reset_index(names='date')[FIELDNAMES].to_csv(fpath, index=False)


TWSE_ALL = 'https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL'
TPEX_ALL = 'https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes'

# (名稱, URL, 代號欄, 收盤欄, 是否放寬 X509 結構嚴格度)
RAW_SOURCES = (
    ('TWSE', TWSE_ALL, 'Code', 'ClosingPrice', False),
    ('TPEx', TPEX_ALL, 'SecuritiesCompanyCode', 'Close', True),
)

# fetch_raw_close() 順便記下每檔屬於哪個官方清單（TWSE=上市、TPEx=上櫃），
# 供 main() 校正 yfinance 後綴
MARKET_OF = {}
# 市場別快取：TPEx 常在傳輸途中斷線，某市場當天抓不到時改用快取校正後綴
MARKET_CACHE = f'{BASE}/market_of.json'
YF_SUFFIX_BY_SOURCE = {'TWSE': '.TW', 'TPEx': '.TWO'}


def _roc_to_date(s):
    """民國日期字串 1150827 → Timestamp('2026-08-27')"""
    s = str(s).strip()
    return pd.Timestamp(f'{int(s[:-4]) + 1911}-{s[-4:-2]}-{s[-2:]}')


def _lax_ssl_context():
    """關閉 VERIFY_X509_STRICT 的 TLS context。

    Python 3.13+ 預設啟用 VERIFY_X509_STRICT，會拒絕不符 RFC 5280 的憑證鏈。
    TPEx 的 CA 憑證缺 Subject Key Identifier，因此驗證失敗（certifi 換憑證庫
    無效——問題不在信任根，而在憑證結構）。
    僅清除該旗標：憑證鏈驗證與主機名驗證都完整保留，不是 CERT_NONE。
    """
    import ssl
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


def _fetch_one(url, code_key, close_key, lax, tries=4):
    """下載單一市場的全市場收盤價。

    TPEx 傳大檔時會時好時壞地在傳到一半重置連線（HTTP 200 已送出、
    body 傳一半即 ConnectionReset / IncompleteRead）。對策：
      - 要求 gzip：傳輸量約降為 1/10（4.4MB → 0.42MB），中斷機率大幅降低
      - 退避重試：gzip 仍會偶發中斷，重試才是真正兜底
    """
    import gzip, json, urllib.request
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json',
        'Accept-Encoding': 'gzip'})
    for k in range(1, tries + 1):
        try:
            with urllib.request.urlopen(req, timeout=40,
                                        context=_lax_ssl_context() if lax else None) as r:
                body, enc = r.read(), r.headers.get('Content-Encoding')
            rows = json.loads((gzip.decompress(body) if enc == 'gzip' else body)
                              .decode('utf-8'))
            break
        except Exception as e:
            if k == tries:
                raise
            print(f'    第 {k} 次中斷（{type(e).__name__}），{2 * k}s 後重試…', flush=True)
            time.sleep(2 * k)
    out = {}
    for row in rows:
        try:
            px = float(str(row[close_key]).replace(',', ''))
        except (ValueError, TypeError, KeyError):
            continue                           # 無成交日為 '--'
        if px > 0:
            out[row[code_key].strip()] = (_roc_to_date(row['Date']), px)
    return out


def fetch_raw_close():
    """全市場「原始收盤價」錨點 → ({symbol: (date, close)}, [失敗來源名])

    來源：TWSE / TPEx OpenAPI（免費、無配額、每日更新）。
    取代原本的 FinLab：免費帳號資料被截斷，錨點日會凍結在過去某日並
    隨時間越來越舊，導致校正池無止境膨脹。兩市場各用自己的最新日當錨點。

    每個市場獨立成敗：單一市場掛掉時，另一市場已取得的資料仍保留，
    只有失敗的那個市場才需要退回 FinLab。
    """
    raw, failed = {}, []
    try:
        with open(MARKET_CACHE, encoding='utf-8') as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    for name, url, code_key, close_key, lax in RAW_SOURCES:
        try:
            got = _fetch_one(url, code_key, close_key, lax)
            raw.update(got)
            MARKET_OF.update(dict.fromkeys(got, name))
            print(f'  {name}: {len(got)} 檔', flush=True)
        except Exception as e:
            failed.append(name)
            print(f'  {name}: 失敗（{str(e)[:70]}）', flush=True)
            # 只借用市場別校正 yfinance 後綴，不當價格錨點（快取沒有當天收盤價）
            old = {s: m for s, m in cache.items() if m == name}
            MARKET_OF.update(old)
            if old:
                print(f'    改用快取的 {name} 市場別 {len(old)} 檔（僅用於校正後綴）', flush=True)
    if len(failed) < len(RAW_SOURCES):      # 至少一個市場成功才寫回；失敗市場保留舊值
        cache.update(MARKET_OF)
        with open(MARKET_CACHE, 'w', encoding='utf-8') as f:
            json.dump(dict(sorted(cache.items())), f, ensure_ascii=False, indent=0)
    return raw, failed


def fetch_raw_close_finlab():
    """備援錨點來源：FinLab。免費帳號資料會被截斷、錨點日偏舊，僅在 OpenAPI 失效時使用。"""
    import finlab
    from finlab import data as fdata
    token = os.environ.get('FINLAB_TOKEN', '')
    if not token:          # 沒有 token 時 finlab.login 可能跳出互動式登入
        raise RuntimeError('未設定 FINLAB_TOKEN，無法使用 FinLab 備援')
    finlab.login(token)
    df = fdata.get('price:收盤價')
    anchor = df.index[-1]
    last = df.loc[anchor].dropna()
    return {s: (anchor, float(v)) for s, v in last.items() if v > 0}


def pick_candidates(prepared, raw, tol=0.005):
    """挑出需 Fugle 校正的個股。

    原理：以全市場「原始收盤價」的最新日為錨點。
    還原價在該日的值 = 原始價 × (該日之後所有除權息的還原因子)，
    因此 yfinance還原價 / 原始價 偏離 1 即代表錨點日後發生過除權息
    （或兩來源還原基準不一致），這類個股才需要 Fugle 即時校正。

    比「偵測跌幅」可靠：yfinance 若已完成還原，序列不會跳空、跌幅法會漏抓。

    raw: {symbol: (anchor_date, close)}，由 fetch_raw_close() 提供。
    上市/上櫃兩市場的最新日可能差一天，故逐檔採用各自的錨點日。
    """
    if raw:
        adates = sorted({d for d, _ in raw.values()})
        print(f'  錨點日(原始收盤價): {", ".join(str(d.date()) for d in adates[-2:])}',
              flush=True)

    # 全市場最新交易日：取「有足夠檔數支持的最大日期」
    #
    # 不可用眾數：yfinance 常整批落後一天，此時眾數就是那個落後日，
    # 落後的個股反而成為基準、無法被判定為 stale，Fugle 池會反向縮小。
    # 改用最大日期，並要求至少 MIN_SUPPORT 檔佐證以排除單檔異常日期。
    from collections import Counter
    cnt = Counter(d.index[-1] for d in prepared.values() if len(d))
    min_support = max(5, int(0.01 * sum(cnt.values())))
    ok_days = [dt for dt, n in cnt.items() if n >= min_support]
    market_last = max(ok_days) if ok_days else None
    if market_last is not None:
        print(f'  全市場最新交易日: {market_last.date()} '
              f'({cnt[market_last]} 檔已到，門檻 {min_support})', flush=True)

    cands, checked, stale = [], 0, 0
    for s, d in prepared.items():
        # (a) 資料落後於全市場最新交易日 → yfinance 對該檔更新延遲，需 Fugle 補最新日
        if market_last is not None and len(d) and d.index[-1] < market_last:
            cands.append(s); stale += 1; continue
        # (b) 還原基準偏離 → 錨點日後發生除權息，需 Fugle 校正
        if s not in raw:
            cands.append(s); continue          # 無從比對 → 保守校正
        adate, apx = raw[s]
        if apx == 0 or adate not in d.index:
            cands.append(s); continue
        checked += 1
        if abs(d.loc[adate, 'close'] / apx - 1) > tol:
            cands.append(s)
    print(f'  已比對 {checked} 檔；其中資料落後(yfinance延遲) {stale} 檔', flush=True)
    return cands


def fugle_correct(sym, ydf):
    """用 Fugle 校正近期資料，並把 yfinance 歷史錨定到 Fugle 還原基準"""
    to = pd.Timestamp.today().strftime('%Y-%m-%d')
    frm = (pd.Timestamp.today() - pd.Timedelta(days=FUGLE_BACKFILL_DAYS)).strftime('%Y-%m-%d')
    rows = fugle_daily(sym, frm, to)
    time.sleep(1.05)
    if not rows:
        return ydf, False
    f = pd.DataFrame(rows)
    f['date'] = pd.to_datetime(f['date'].str[:10])
    f = f.set_index('date')[['open', 'high', 'low', 'close', 'volume']].astype(float).sort_index()

    common = ydf.index.intersection(f.index)
    if len(common) == 0:
        return ydf, False
    anchor = common[0]                              # 重疊區最早日當錨點
    if ydf.loc[anchor, 'close'] == 0:
        return ydf, False
    scale = f.loc[anchor, 'close'] / ydf.loc[anchor, 'close']

    hist = ydf[ydf.index < f.index[0]].copy()
    for c in ['open', 'high', 'low', 'close']:
        hist[c] = hist[c] * scale
    return pd.concat([hist, f]).sort_index(), True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry', action='store_true', help='寫入 *_hy 目錄，不動正式資料')
    ap.add_argument('--tol', type=float, default=0.005,
                    help='還原價/原始價 偏離門檻，超過即送 Fugle 校正 (預設0.5%%)')
    ap.add_argument('--limit', type=int, default=0)
    a = ap.parse_args()

    sfx = '_hy' if a.dry else ''
    ddir, wdir = f'{BASE}/daily{sfx}', f'{BASE}/weekly{sfx}'
    os.makedirs(ddir, exist_ok=True); os.makedirs(wdir, exist_ok=True)
    print(f'輸出: {ddir} / {wdir}', flush=True)

    pairs = load_tickers()
    if a.limit:
        pairs = pairs[:a.limit]

    from tw_wk_mo_indicators import FUGLE_KEY
    if not FUGLE_KEY:      # 沒金鑰時 fugle_daily 會默默回空，校正全數失敗卻不報錯
        sys.exit('未設定 FUGLE_API_KEY 環境變數，中止')

    # ── 0. 先取錨點：兼作前置檢查 ───────────────────────────────────
    # 放在 yfinance 下載之前，來源掛掉時數秒內就中止，
    # 不必白跑 4 分鐘的全市場下載才失敗。
    t0 = time.time()
    print('[0/3] 取得全市場原始收盤價（錨點）…', flush=True)
    try:
        raw, failed = fetch_raw_close()
    except Exception as e:
        print(f'  OpenAPI 全數失敗（{str(e)[:60]}）', flush=True)
        raw, failed = {}, [n for n, *_ in RAW_SOURCES]
    if failed:
        # 只補失敗市場缺的代號，已取得的 OpenAPI 錨點（較新）優先保留
        print(f'  {"/".join(failed)} 失敗，以 FinLab 補缺…', flush=True)
        try:
            fb = fetch_raw_close_finlab()
            add = {k: v for k, v in fb.items() if k not in raw}
            raw.update(add)
            print(f'  FinLab 補入 {len(add)} 檔', flush=True)
        except Exception as e:
            print(f'  FinLab 備援也失敗（{str(e)[:60]}）', flush=True)
    if not raw:
        sys.exit('錨點來源全數失敗，中止（未動用 yfinance）')

    # 依官方清單校正 yfinance 後綴（上市 .TW / 上櫃 .TWO）
    # 市場別對照表（FinLab）沒收錄的新上市股票，原本一律預設 .TW，只能靠
    # 「反向後綴重試」補救；重試失敗時整檔漏抓（09-14 有 29 檔上櫃股因此停在前一日）
    fixed = 0
    for i, (s, tk) in enumerate(pairs):
        want = YF_SUFFIX_BY_SOURCE.get(MARKET_OF.get(s))
        if want and tk != s + want:
            pairs[i] = (s, s + want)
            fixed += 1
    print(f'  依官方清單校正 yfinance 後綴: {fixed} 檔', flush=True)

    # ── 1. yfinance 全市場長歷史 ────────────────────────────────────
    print(f'[1/3] yfinance 下載 {len(pairs)} 檔…', flush=True)
    data, failed = download_all(pairs)
    print(f'  完成 {len(data)} 檔、{(time.time()-t0)/60:.1f} 分鐘', flush=True)

    # ── 2. 篩出需 Fugle 校正者 ──────────────────────────────────────
    prepared = {s: to_ohlc(df) for s, df in data.items() if len(df) >= 30}
    print(f'[2/3] 篩選需 Fugle 校正的個股…', flush=True)
    cands = pick_candidates(prepared, raw, tol=a.tol)
    print(f'  需校正: {len(cands)} 檔 (約 {len(cands)/57:.1f} 分鐘)', flush=True)

    corrected = 0
    durs = []                                   # 單檔耗時，用於診斷 Fugle 回應速度
    for i, s in enumerate(cands, 1):
        t1 = time.time()
        prepared[s], ok = fugle_correct(s, prepared[s])
        durs.append((time.time() - t1, s))
        corrected += ok
        if i % 100 == 0:
            print(f'  校正 {i}/{len(cands)} (成功{corrected}) '
                  f'近百檔均 {sum(d for d, _ in durs[-100:])/len(durs[-100:]):.1f}s',
                  flush=True)
    if durs:
        v = sorted(d for d, _ in durs)
        n = len(v)
        print(f'  Fugle 單檔耗時: 中位 {v[n//2]:.1f}s / p90 {v[int(n*0.9)]:.1f}s / '
              f'最大 {v[-1]:.1f}s (sleep 基準 1.05s)', flush=True)
        slow = sorted((d for d in durs if d[0] > 5), reverse=True)[:5]
        if slow:
            print('  最慢個股: ' + ', '.join(f'{s}={d:.0f}s' for d, s in slow), flush=True)

    # ── 3. 寫檔 + 指標 ──────────────────────────────────────────────
    print(f'[3/3] 寫出與計算指標…', flush=True)
    keep = pd.Timestamp.today() - pd.Timedelta(days=420)
    wk_rows, mo_rows, wrote = [], [], 0
    for s, d in prepared.items():
        wk = resample_ohlc(d, 'W-MON')
        mo = resample_ohlc(d, 'MS')
        if len(wk) < 10 or len(mo) < 6:
            continue
        recent = d[d.index >= keep]
        if recent.empty:        # 長期無交易(已下市/停牌)：不可寫出空檔覆蓋既有資料
            continue
        write_csv(f'{ddir}/{s}.csv', recent)
        write_csv(f'{wdir}/{s}.csv', wk)
        wi = add_indicators(wk, [5, 10, 20, 60]).iloc[-1]
        mi = add_indicators(mo, [3, 6, 12]).iloc[-1]
        wk_rows.append({'symbol': s, 'date': wk.index[-1].strftime('%Y-%m-%d'), **wi.to_dict()})
        mo_rows.append({'symbol': s, 'date': mo.index[-1].strftime('%Y-%m-%d'), **mi.to_dict()})
        wrote += 1

    if not a.dry:
        pd.DataFrame(wk_rows).to_csv(f'{BASE}/tw_weekly_indicators.csv',
                                     index=False, encoding='utf-8-sig')
        pd.DataFrame(mo_rows).to_csv(f'{BASE}/tw_monthly_indicators.csv',
                                     index=False, encoding='utf-8-sig')
        merge_all_csv()
        try:
            from tw_market_category import build as bmc
            bmc()
        except Exception as e:
            print(f'market_category 更新失敗: {str(e)[:60]}', flush=True)

    print(f'\n完成：寫出 {wrote} 檔、Fugle 校正 {corrected}/{len(cands)}、'
          f'總耗時 {(time.time()-t0)/60:.1f} 分鐘', flush=True)
    if wk_rows:
        print(f'週指標最新日: {max(r["date"] for r in wk_rows)}', flush=True)
        print(f'月指標最新日: {max(r["date"] for r in mo_rows)}', flush=True)


if __name__ == '__main__':
    main()
