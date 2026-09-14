"""
本機同步：取回雲端（GitHub Actions）每日更新的資料，寫到本機分析工具讀的位置。

  1. git pull          更新本 repo 的個股 daily/ weekly/ 與指標快照
  2. 下載 Release「latest」的合併總表（gz）並解壓
  3. 全部寫到 TARGET（technical-indicator-analyst 等工具固定讀這個資料夾）

用法:
  python sync_local.py                 # 同步到預設 TARGET
  python sync_local.py --target D:/data  # 同步到其他資料夾
"""
import argparse, datetime, gzip, os, shutil, subprocess, sys, time, urllib.request

REPO = 'chenyicheng2008/TWSTOCK'
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TARGET = r'C:\Users\cheny\Pictures\Claud\fugle\data'
LOG = os.path.join(HERE, 'sync_local.log')

# (Release 附件名, 寫出檔名, 是否 gzip)
ASSETS = [
    ('tw_all_weekly.csv.gz',      'tw_all_weekly.csv',         True),
    ('tw_all_daily_1yr.csv.gz',   'tw_all_daily_1yr.csv',      True),
    ('tw_weekly_indicators.csv',  'tw_weekly_indicators.csv',  False),
    ('tw_monthly_indicators.csv', 'tw_monthly_indicators.csv', False),
]


def log(msg):
    line = f'{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}'
    print(line, flush=True)
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def download(name, dest):
    url = f'https://github.com/{REPO}/releases/download/latest/{name}'
    req = urllib.request.Request(url, headers={'User-Agent': 'twstock-sync'})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, 'wb') as f:
        shutil.copyfileobj(r, f)


def replace(src, dst):
    """先寫暫存檔再替換：檔案被 Excel 開著時只會失敗這一檔，不會留下半個檔"""
    try:
        os.replace(src, dst)
        return True
    except PermissionError:
        log(f'  ✗ {os.path.basename(dst)} 被其他程式鎖定（Excel？），本次略過')
        os.remove(src)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--target', default=DEFAULT_TARGET)
    a = ap.parse_args()
    os.makedirs(a.target, exist_ok=True)
    log(f'開始同步 → {a.target}')
    ok = True

    # 1. git pull（網路瞬斷時重試：09-14 曾出現 fetch-pack unexpected disconnect）
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')     # 不跳互動式登入，避免卡住
    for k in range(1, 4):
        # CREATE_NO_WINDOW：排程以 pythonw 執行時，git 不另開主控台視窗（視窗被關掉會中止同步）
        r = subprocess.run(['git', '-C', HERE, 'pull', '--ff-only', '-q'],
                           capture_output=True, text=True, env=env,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if r.returncode == 0:
            log('  ✓ git pull' + (f'（第 {k} 次成功）' if k > 1 else ''))
            break
        log(f'  ✗ git pull 第 {k} 次失敗: {(r.stderr or r.stdout).strip()[:200]}')
        if k < 3:
            time.sleep(10)
    else:
        ok = False

    # 2. 個股檔：repo → TARGET
    for d in ('daily', 'weekly'):
        src = os.path.join(HERE, d)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(a.target, d), dirs_exist_ok=True)
            log(f'  ✓ {d}/ {len(os.listdir(src))} 檔')

    # 3. Release 附件
    for name, out, is_gz in ASSETS:
        tmp = os.path.join(a.target, out + '.part')
        try:
            if is_gz:
                gz = tmp + '.gz'
                download(name, gz)
                with gzip.open(gz, 'rb') as fi, open(tmp, 'wb') as fo:
                    shutil.copyfileobj(fi, fo)
                os.remove(gz)
            else:
                download(name, tmp)
        except Exception as e:
            log(f'  ✗ {name} 下載失敗: {str(e)[:120]}')
            ok = False
            continue
        if replace(tmp, os.path.join(a.target, out)):
            mb = os.path.getsize(os.path.join(a.target, out)) / 1048576
            log(f'  ✓ {out} ({mb:.1f} MB)')
        else:
            ok = False

    log('完成' if ok else '完成（有部分失敗，見上方）')
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
