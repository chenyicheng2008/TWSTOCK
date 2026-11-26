"""印出官方最新交易日（TWSE 與 TPEx 取較新者），供 workflow 判斷資料是否已最新。

單獨一支、不依賴第三方套件：workflow 的 check 工作沒有安裝 pandas。
TWSE OpenAPI 常比 TPEx 慢一天（11-26 22:17 實測 TWSE 仍為 11-25、TPEx 已是 11-26），
因此必須取兩者較新者，否則備援排程會把尚未更新的資料誤判為已最新而略過。
"""
import gzip, json, sys, time, urllib.request

SOURCES = (
    ('TWSE', 'https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL'),
    ('TPEx', 'https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes'),
)


def _lax_ctx():
    """TPEx 的 CA 憑證缺 Subject Key Identifier，Python 3.13+ 的嚴格檢查會拒絕；
    僅關閉該結構檢查，憑證鏈與主機名驗證保留。"""
    import ssl
    c = ssl.create_default_context()
    c.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return c


def fetch_date(url, tries=3):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json',
        'Accept-Encoding': 'gzip'})
    for k in range(1, tries + 1):
        try:
            with urllib.request.urlopen(req, timeout=40, context=_lax_ctx()) as r:
                body, enc = r.read(), r.headers.get('Content-Encoding')
            rows = json.loads((gzip.decompress(body) if enc == 'gzip' else body).decode('utf-8'))
            d = str(rows[0]['Date']).strip()          # 民國 1151126
            return f'{int(d[:-4]) + 1911}-{d[-4:-2]}-{d[-2:]}'
        except Exception as e:
            if k == tries:
                print(f'{url}: {type(e).__name__}', file=sys.stderr)
                return None
            time.sleep(2 * k)


def main():
    got = {}
    for name, url in SOURCES:
        d = fetch_date(url)
        print(f'{name}={d}', file=sys.stderr)
        if d:
            got[name] = d
    if not got:
        sys.exit('兩個官方來源都取不到日期')
    print(max(got.values()))


if __name__ == '__main__':
    main()
