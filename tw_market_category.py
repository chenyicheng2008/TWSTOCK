"""
產出台股市場別 / 產業別對照表 → tw_market_category.csv

資料源：FinLab `security_categories`（約 1MB 用量，很輕）
輸出欄位：
  symbol      股票代號
  name        公司簡稱
  market      原始市場別代碼 (sii/otc/etf/other_securities/pub)
  market_tw   中文市場別 (上市/上櫃/ETF/其他/公開發行)
  category    產業別 (如 半導體業)
  yf_suffix   yfinance 代碼後綴 (.TW / .TWO)，非個股者留空
  yf_ticker   yfinance 完整代碼 (如 2330.TW)，非個股者留空

用法: python tw_market_category.py
"""
import os, warnings
warnings.filterwarnings('ignore')
import pandas as pd

# 資料根目錄：環境變數 TWSTOCK_DIR，否則為本檔所在資料夾
BASE = os.environ.get('TWSTOCK_DIR',
                      os.path.dirname(os.path.abspath(__file__)))
OUT  = f'{BASE}/tw_market_category.csv'
FINLAB_TOKEN = os.environ.get('FINLAB_TOKEN', '')   # 金鑰只從環境變數讀取

MARKET_TW = {
    'sii': '上市', 'otc': '上櫃', 'etf': 'ETF',
    'other_securities': '其他', 'pub': '公開發行',
}
# yfinance 後綴：上市 .TW、上櫃 .TWO；ETF 依掛牌市場，多數在上市
YF_SUFFIX = {'sii': '.TW', 'otc': '.TWO'}


def build():
    # 沒有 token 時 finlab.login 可能跳出互動式登入，在 GitHub Actions 會卡住
    if not FINLAB_TOKEN:
        raise RuntimeError('未設定 FINLAB_TOKEN，略過市場別對照表更新')
    import finlab
    from finlab import data
    finlab.login(FINLAB_TOKEN)
    sc = data.get('security_categories')

    df = sc[['symbol', 'name', 'market', 'category']].copy()
    df['symbol'] = df['symbol'].astype(str).str.strip()
    df['market_tw'] = df['market'].map(MARKET_TW).fillna(df['market'])
    df['yf_suffix'] = df['market'].map(YF_SUFFIX).fillna('')
    df['yf_ticker'] = df.apply(
        lambda r: f"{r['symbol']}{r['yf_suffix']}" if r['yf_suffix'] else '', axis=1)

    df = df[['symbol', 'name', 'market', 'market_tw', 'category',
             'yf_suffix', 'yf_ticker']].sort_values('symbol')
    # 空值一律寫成 '-'，避免讀回時被 pandas 轉成 NaN
    df[['yf_suffix', 'yf_ticker']] = df[['yf_suffix', 'yf_ticker']].replace('', '-')
    df.to_csv(OUT, index=False, encoding='utf-8-sig')

    print(f'已輸出 {OUT}：{len(df)} 檔', flush=True)
    print('\n市場別分布:')
    print(df['market_tw'].value_counts().to_string())
    print(f'\n產業別數: {df["category"].nunique()}')
    return df


def load_market_map():
    """給其他腳本 import 用：回傳 {symbol: (market, yf_ticker)}"""
    if not os.path.exists(OUT):
        build()
    df = pd.read_csv(OUT, encoding='utf-8-sig', dtype=str).fillna('')
    return {r['symbol']: (r['market'], '' if r['yf_ticker'] == '-' else r['yf_ticker'])
            for _, r in df.iterrows()}


if __name__ == '__main__':
    build()
