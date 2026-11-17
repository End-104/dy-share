# coding: utf-8
"""device_mint.py —— 设备池扩容：现场注册新设备

为什么要这个文件：池子里 1688 台 1128 设备的身份是固定的，一条视频一天要 1800 条
入账，靠现有池子只能撑一天多一点。服务端接受「新注册设备」的分享上报，所以池子不是
买来的常量，而是可以现场生成的：拿 ttwid 打底，带着 X-Argus / X-Gorgon / X-Ladon
打 /service/2/device_register/，服务端会下发一套全新的 device_id / install_id /
device_token，这套身份对同一条视频来说就是一台干净的新设备。

用法：
    python device_mint.py mint 200                     # 注册 200 台，落 pool_extra.json
    python device_mint.py mint 20 --verify 7693857502727274422
                                                       # 注册后逐台实测一次上报，只留活的
    python device_mint.py probe 7693857502727274422    # 只验证已有池子的接受率
    python device_mint.py test                         # 注册 1 台并打印完整响应，排查用

字段口径与 devices34 完全一致，所以 pool_extra.json 能被 load_extra_devices 直接吃。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xargus1128.sign import sign_2290
from function import http_client, request_id

# ---------------------------------------------------------------- 机型库
# 为什么给多机型：全套请求都长成一个机型，服务端按机型聚合后这批设备一模一样，
# 异常比例一超阈值整批丢弃。机型、系统版本、分辨率必须自洽（分辨率要和 dpi 匹配）。
MODELS = [
    ('Xiaomi', '2410DPN6CC', 30, '15', 440, '1080*2135'),
    ('Xiaomi', '23127PN0CC', 34, '14', 440, '1220*2712'),
    ('Redmi', '2312DRA50C', 33, '13', 450, '1080*2400'),
    ('OPPO', 'PJZ110', 34, '14', 450, '1240*2772'),
    ('OPPO', 'PHY110', 33, '13', 394, '1080*2412'),
    ('vivo', 'V2309A', 34, '14', 453, '1260*2800'),
    ('vivo', 'V2244A', 33, '13', 388, '1080*2400'),
    ('HUAWEI', 'ALN-AL80', 31, '12', 425, '1224*2700'),
    ('HONOR', 'MAG-AN00', 33, '13', 437, '1200*2640'),
    ('samsung', 'SM-S9110', 34, '14', 425, '1080*2340'),
    ('realme', 'RMX3823', 33, '13', 450, '1240*2772'),
    ('OnePlus', 'PJD110', 34, '14', 450, '1260*2780'),
]

UA_1128 = ('com.ss.android.ugc.aweme/220900 (Linux; U; Android {os_version}; '
           '{model} Build/{build}; Cronet/TTNetVersion:b4d74d15 2023-04-19)')

REGISTER_URLS = (
    'https://log.snssdk.com/service/2/device_register/',
    'https://api.amemv.com/service/2/device_register/',
)

POOL_EXTRA_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pool_extra.json')
POOL_EXTRA_TXT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pool_extra.txt')
_LOCK = threading.Lock()

# 池文件是「读进来-改-写回去」，两个进程同时写就会丢记录：
# 进程 A 读到 100 条、进程 B 读到 100 条，各自加 50 条写回，最后只剩 150 条。
# 调度器运行中就地补料时，注册进程和调度进程会同时写，所以要用跨进程锁。
_POOL_LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.pool.lock')


class _pool_lock:
    """跨进程互斥（O_EXCL 建锁文件），拿不到就等，超时抛异常。"""

    def __init__(self, timeout: float = 120.0):
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        deadline = time.time() + self.timeout
        while True:
            try:
                self.fd = os.open(_POOL_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                if time.time() > deadline:
                    raise TimeoutError('池文件锁等待超时，检查 .pool.lock 是否需要手工删')
                time.sleep(0.2)

    def __exit__(self, *exc):
        try:
            if self.fd is not None:
                os.close(self.fd)
        finally:
            self.fd = None
            try:
                os.unlink(_POOL_LOCK_PATH)
            except FileNotFoundError:
                pass
        return False


# ---------------------------------------------------------------- 身份素材
def _hex(n: int) -> str:
    return ''.join(random.choice('0123456789abcdef') for _ in range(n))


def _digits(n: int) -> str:
    return ''.join(random.choice('0123456789') for _ in range(n))


def new_profile() -> dict:
    """生成一台设备的硬件身份。

    为什么要自己造：device_register 只校验「参数自洽」，不校验「这台机器真实存在」。
    cdid 是安装实例 id，换一个就是一次全新安装；openudid 是设备级 id。
    """
    brand, model, os_api, os_version, dpi, resolution = random.choice(MODELS)
    return {
        'cdid': str(uuid.uuid4()),
        'openudid': _hex(16),
        'clientudid': str(uuid.uuid4()),
        'brand': brand,
        'model': model,
        'os_api': os_api,
        'os_version': os_version,
        'dpi': dpi,
        'resolution': resolution,
        'build': _hex(6).upper(),
        'install_id': _digits(16),
    }


def common_query(p: dict, device_id: str = '0', install_id: str = '0') -> str:
    """注册请求的公共参数。device_id=0 / iid=0 是「我是新设备」的语义。"""
    return (
        'ac=wifi&aid=1128&app_name=aweme&app_type=normal'
        f'&cdid={p["cdid"]}&channel=wandoujia_1128_0413'
        f'&device_brand={p["brand"]}&device_id={device_id}'
        '&device_platform=android'
        f'&device_type={p["model"]}&dpi={p["dpi"]}&iid={install_id}'
        '&language=zh&manifest_version_code=220901&os=android'
        f'&os_api={p["os_api"]}&os_version={p["os_version"]}'
        '&package=com.ss.android.ugc.aweme'
        f'&resolution={p["resolution"]}&ssmix=a'
        '&version_code=220900&version_name=22.9.0'
    )


def register_body(p: dict) -> str:
    """注册请求体。

    为什么带这么多无关字段：服务端按「字段齐全度」给设备打分，字段太少会被判成
    脚本注册，下发的 device_id 在统计接口里直接不计数。这就是设备池里那批
    「池子有、打上去空包」的来源。
    """
    now = int(time.time())
    header = {
        'device_id': 0,
        'install_id': 0,
        'aid': 1128,
        'app_name': 'aweme',
        'version_code': 220900,
        'version_name': '22.9.0',
        'manifest_version_code': 220901,
        'update_version_code': 220900,
        'package': 'com.ss.android.ugc.aweme',
        'channel': 'wandoujia_1128_0413',
        'device_platform': 'android',
        'os': 'android',
        'os_api': str(p['os_api']),
        'os_version': str(p['os_version']),
        'device_type': p['model'],
        'device_brand': p['brand'],
        'device_model': p['model'],
        'resolution': p['resolution'],
        'dpi': str(p['dpi']),
        'language': 'zh',
        'timezone': 8,
        'access': 'wifi',
        'carrier': '',
        'mcc_mnc': '',
        'sim_region': 'cn',
        'region': 'CN',
        'tz_name': 'Asia/Shanghai',
        'tz_offset': 28800,
        'cpu_abi': 'arm64-v8a',
        'build_serial': p['openudid'],
        'rom': f'{p["brand"]}/13',
        'rom_version': f'{p["model"]}-{p["os_version"]}',
        'cdid': p['cdid'],
        'openudid': p['openudid'],
        'clientudid': p['clientudid'],
        'req_id': request_id(),
    }
    body = {
        'magic_tag': 'ss_app_log',
        'header': header,
        'device': {
            'is_guest_mode': 0,
            'is_jailbroken': 0,
            'is_emulator': 0,
            'adm': json.dumps({'display_name': p['model'], 'version': p['os_version']}),
        },
        '_gen_time': now,
        '_event_v': 1,
    }
    return json.dumps(body, ensure_ascii=False, separators=(',', ':'))


def _cookie_from_response(res) -> dict:
    """把 set-cookie 收成 dict。odin_tt / ttreq 是后续鉴权的关键，缺一个就会空包。"""
    out = {}
    try:
        for line in res.headers.get_list('set-cookie'):
            m = re.match(r'([^=;]+)=([^;]*)', line)
            if m:
                out[m.group(1).strip()] = m.group(2).strip()
    except Exception:
        pass
    return out


def _pick(j: dict, *names):
    for n in names:
        v = j.get(n)
        if v not in (None, '', 0, '0'):
            return v
    return None


def register_one(p: dict, verbose: bool = False):
    """注册一台设备，成功返回 devices34 同结构的记录，失败返回 None。"""
    body = register_body(p)
    query = common_query(p)
    last = ''
    for base in REGISTER_URLS:
        url = base + '?' + query
        try:
            headers = {
                'User-Agent': UA_1128.format(os_version=p['os_version'], model=p['model'],
                                             build=p['build']),
                'Content-Type': 'application/json; charset=utf-8',
                'x-tt-request-tag': 's=0;p=0',
                'x-ss-dp': '1128',
                'sdk-version': '2',
                'passport-sdk-version': '20374',
                'x-vc-bdturing-sdk-version': '3.1.0.cn',
                'Cookie': f'install_id=0; store-region=cn-gx; store-region-src=did',
            }
            headers.update(sign_2290(params=url, payload=body, cookie=headers['Cookie']))
            res = http_client.post(url, data=body.encode('utf-8'), headers=headers, timeout=20)
            if res.status_code != 200:
                last = f'http {res.status_code}'
                continue
            text = res.text.strip()
            if not text:
                last = 'empty body'
                continue
            try:
                j = res.json()
            except Exception:
                last = 'not json: ' + text[:120]
                continue
            if verbose:
                print('[register-raw]', json.dumps(j, ensure_ascii=False)[:1200])
            device_id = _pick(j, 'device_id', 'device_id_str')
            install_id = _pick(j, 'install_id', 'install_id_str', 'iid')
            token = _pick(j, 'device_token', 'x_tt_dt', 'device_token_str')
            if not device_id:
                last = 'no device_id: ' + json.dumps(j, ensure_ascii=False)[:200]
                continue
            jar = _cookie_from_response(res)
            jar.setdefault('install_id', str(install_id or ''))
            jar.setdefault('store-region', 'cn-gx')
            jar.setdefault('store-region-src', 'did')
            jar.setdefault('passport_csrf_token', _hex(32))
            jar.setdefault('passport_csrf_token_default', jar['passport_csrf_token'])
            cookie = '; '.join(f'{k}={v}' for k, v in jar.items() if v)
            return {
                'device_id': str(device_id),
                'install_id': str(install_id or ''),
                'device_token': str(token or ''),
                'cdid': p['cdid'],
                'device_brand': p['brand'],
                'device_type': p['model'],
                'dpi': p['dpi'],
                'resolution': p['resolution'],
                'os_api': p['os_api'],
                'os_version': p['os_version'],
                'channel': 'mint_1128',
                'cookie': cookie,
                'user_agent': UA_1128.format(os_version=p['os_version'], model=p['model'],
                                             build=p['build']),
                '_minted': int(time.time()),
                '_source': base,
            }
        except Exception as exc:
            last = f'{type(exc).__name__}: {exc}'
    if verbose:
        print('[register-fail]', last)
    return None


# ---------------------------------------------------------------- 验证
def verify_one(rec: dict, aweme_id: str) -> tuple:
    """拿新设备真打一条分享上报，看服务端认不认。

    为什么必须实测：注册成功只说明身份有效，不说明这套身份能计入分享量。
    池子里那批空包设备就是「注册过但统计侧不认」的，不实测等于把废号灌进池子。
    """
    import dy_share as D
    try:
        pool = D.DevicePool([rec], cooldown=0)
        ok, raw = D.report_1128(aweme_id, pool, D.SHARE_FIELD, day_cap=0)
        return ok, str(raw)[:120]
    except Exception as exc:
        return False, f'{type(exc).__name__}: {exc}'


def load_existing_ids() -> set:
    import dy_share as D
    ids = {str(d.get('device_id')) for d in D.devices34 if d.get('device_id')}
    for d in D.load_extra_devices():
        ids.add(str(d.get('device_id')))
    return ids


def append_pool(recs: list) -> int:
    """追加到 pool_extra.json，按 device_id 去重。

    为什么去重而不覆盖：同一 device_id 在池里出现两次，调度器会当两份材料用，
    第二次必然是空包，等于把接受率直接砍半。
    """
    with _LOCK, _pool_lock():
        cur = []
        if os.path.exists(POOL_EXTRA_JSON):
            try:
                with open(POOL_EXTRA_JSON, 'r', encoding='utf-8') as f:
                    cur = json.load(f) or []
            except Exception:
                cur = []
        seen = {str(d.get('device_id')) for d in cur}
        added = 0
        for r in recs:
            if str(r.get('device_id')) in seen:
                continue
            cur.append(r)
            seen.add(str(r.get('device_id')))
            added += 1
        with open(POOL_EXTRA_JSON, 'w', encoding='utf-8') as f:
            json.dump(cur, f, ensure_ascii=False, indent=1)
        return added


def append_infos(recs: list) -> int:
    """把同一批设备也写成 32 通道的 infos 行。

    为什么要两份：aid=32 通道的桶和 1128 是分开的（22:53 实测：1128 全空包时
    32 仍能计入），但 32 通道只吃 `hash----device_id----install_id-xxx----接码url`
    这种单行串，池子里原本只有 188 条，几百条的量就打不动了。同一台注册过的设备
    在两条通道上都算「新鲜设备」，所以写成两份不浪费。
    """
    with _LOCK, _pool_lock():
        old = set()
        if os.path.exists(POOL_EXTRA_TXT):
            try:
                with open(POOL_EXTRA_TXT, encoding='utf-8') as f:
                    for ln in f:
                        parts = ln.split('----')
                        if len(parts) >= 3:
                            old.add(parts[1].strip())
            except Exception:
                pass
        added = 0
        with open(POOL_EXTRA_TXT, 'a', encoding='utf-8') as f:
            for r in recs:
                did = str(r.get('device_id'))
                if did in old:
                    continue
                iid = str(r.get('install_id') or _digits(16))
                line = (f'{_hex(320)}-1.0.1----{did}----{iid}-{_digits(10)}'
                        f'----https://smsapis.com/api/get_sms?key={_hex(32)}')
                f.write(line + '\n')
                old.add(did)
                added += 1
        return added


def mint_batch(count: int, threads: int = 8, sleep: float = 0.0, verify: str = '',
               write_infos: bool = True, dry_run: bool = False,
               verbose: bool = True, existing: set | None = None,
               pause_base: float = 2.0, pause_max: float = 90.0,
               max_streak: int = 10, flush_every: int = 50) -> dict:
    """注册一批新身份并落盘，返回统计字典。

    为什么抽成函数而不是留在 cmd_mint 里：daily1800.py 的调度器在运行中池子烧光时
    要就地补料——再起一个子进程会和调度器抢池文件、各自维护一份 visited 表，还会因为
    子进程退出时机不明导致「补了料但调度器看不见」。同进程调用只碰文件，不碰进程。

    为什么带熔断（pause_base / max_streak）：23:03 那次 12 线程冲 4500 台，跑到
    23:05 服务端开始回 `device_id:0`——注册接口是按 IP + 时间窗配额的，不是无限量。
    被限速后继续硬冲只会把配额窗一直踩住，再也出不来。所以一旦连续失败就整体降温，
    连续 max_streak 次失败直接收工，把剩余额度留给后面的时间。

    为什么边跑边落盘（flush_every）：一批 400 台按 0.9 台/秒要跑 7 分钟，一批 1900 台
    要跑半小时。只在结尾写一次的话，中途被杀或断电，整批注册全部作废——而那些身份
    服务端已经下发过了，配额是真花掉了。每 50 台落一次盘，崩了最多丢 49 台。
    """
    existing = set(existing) if existing else load_existing_ids()
    got: list = []
    pending: list = []
    saved = [0]
    saved_infos = [0]
    lock = threading.Lock()
    stop = threading.Event()
    tried = [0]
    streak = [0]           # 连续失败计数，成功即清零
    cool_until = [0.0]     # 全体降温和解冻时间点
    aborted = [0]
    t0 = time.time()

    def worker():
        while not stop.is_set():
            wait = cool_until[0] - time.time()
            if wait > 0:
                time.sleep(min(wait, 5.0))
                continue
            with lock:
                if len(got) >= count or tried[0] >= count * 3:
                    stop.set()
                    return
                tried[0] += 1
            rec = register_one(new_profile())
            if not rec:
                pause = 0.0
                with lock:
                    streak[0] += 1
                    s = streak[0]
                    if s >= max_streak:
                        aborted[0] = 1
                        stop.set()
                    else:
                        pause = min(pause_max, pause_base * (2 ** min(5, s - 1)))
                        cool_until[0] = max(cool_until[0], time.time() + pause)
                if verbose and pause:
                    print(f'[throttle] 连续失败 {s} 次，全体降温 {pause:g}s', flush=True)
                continue
            did = str(rec.get('device_id'))
            with lock:
                streak[0] = 0
                if did in existing:
                    continue
                existing.add(did)     # 先占位，避免并发重复入库
            if verify:
                ok, raw = verify_one(rec, verify)
                if verbose:
                    print(f'[{"+" if ok else "-"}] {did} verify={ok} {str(raw)[:60]}', flush=True)
                if not ok:
                    with lock:
                        existing.discard(did)
                    continue
            with lock:
                got.append(rec)
                pending.append(rec)
                n = len(got)
                flush = (len(pending) >= flush_every) and not dry_run
                chunk = list(pending) if flush else []
                if flush:
                    pending.clear()
                if verbose and (n % 10 == 0 or n >= count):
                    print(f'[mint] {n}/{count} 注册成功率 '
                          f'{n / max(1, tried[0]):.0%} 用时 {time.time() - t0:.0f}s',
                          flush=True)
            if chunk:
                saved[0] += append_pool(chunk)
                if write_infos:
                    saved_infos[0] += append_infos(chunk)
            if sleep:
                time.sleep(sleep)

    ths = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, threads))]
    for t in ths:
        t.start()
    for t in ths:
        t.join()

    got = got[:count]
    rest = pending if not dry_run else []
    if rest:
        saved[0] += append_pool(rest)
        if write_infos:
            saved_infos[0] += append_infos(rest)
    return {'minted': len(got), 'attempts': tried[0],
            'saved': saved[0] if not dry_run else len(got),
            'saved_infos': saved_infos[0], 'dry_run': dry_run,
            'aborted': bool(aborted[0]),
            'rate': round(len(got) / max(1, tried[0]), 3),
            'sec': round(time.time() - t0, 1)}


# ---------------------------------------------------------------- 主流程
def cmd_test(args):
    p = new_profile()
    print('[profile]', json.dumps(p, ensure_ascii=False))
    rec = register_one(p, verbose=True)
    if not rec:
        return 1
    print('[device]', json.dumps({k: v for k, v in rec.items() if k != 'cookie'},
                                 ensure_ascii=False))
    if args.verify:
        ok, raw = verify_one(rec, args.verify)
        print(f'[verify] ok={ok} raw={raw}')
    return 0


def cmd_mint(args):
    """批量注册。默认单线程（1 台/秒），--threads N 并发到 ~N 台/秒。

    为什么可以并发：注册请求之间没有任何共享状态，每台设备的 cdid/openudid 都是
    独立新生成的，服务端按设备维度下发身份，并发不会串号。并发跑 1800 台从 30
    分钟压到 4 分钟，这才是「每天现注册够当天用量」在时间上可行的地方。
    """
    existing = load_existing_ids()
    print(f'[pool] 现有 {len(existing)} 台，本次目标 {args.count} 台，并发 {args.threads}')
    out = mint_batch(args.count, threads=args.threads, sleep=args.sleep,
                     verify=args.verify or '', write_infos=args.write_infos,
                     dry_run=args.dry_run, verbose=True, existing=existing,
                     pause_base=args.pause_base, pause_max=args.pause_max,
                     max_streak=args.max_streak)
    print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_probe(args):
    """只测现有池子对某条视频的接受率，不注册。"""
    import dy_share as D
    devs = D.devices34[:args.count]
    ok = 0
    for i, d in enumerate(devs, 1):
        pool = D.DevicePool([d], cooldown=0)
        good, raw = D.report_1128(args.aweme_id, pool, D.SHARE_FIELD, day_cap=0)
        ok += 1 if good else 0
        print(f'[{i}/{len(devs)}] {"ok" if good else "no"} {str(raw)[:50]}', flush=True)
    print(json.dumps({'tested': len(devs), 'accepted': ok,
                      'rate': round(ok / max(1, len(devs)), 3)}, ensure_ascii=False))
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)

    t = sub.add_parser('test')
    t.add_argument('--verify', default='')

    m = sub.add_parser('mint')
    m.add_argument('count', type=int)
    m.add_argument('--verify', default='')
    m.add_argument('--sleep', type=float, default=1.0)
    m.add_argument('--threads', type=int, default=1,
                   help='并发注册线程数；8 线程约 8 台/秒')
    m.add_argument('--dry-run', action='store_true')
    m.add_argument('--write-infos', action='store_true',
                   help='同时写 pool_extra.txt（32 通道素材）')
    m.add_argument('--pause-base', type=float, default=2.0,
                   help='连续失败后的首次降温秒数，之后每次翻倍')
    m.add_argument('--pause-max', type=float, default=90.0,
                   help='降温上限秒数')
    m.add_argument('--max-streak', type=int, default=10,
                   help='连续失败多少次直接收工（说明配额窗已关）')

    p = sub.add_parser('probe')
    p.add_argument('aweme_id')
    p.add_argument('--count', type=int, default=20)

    args = ap.parse_args()
    return {'test': cmd_test, 'mint': cmd_mint, 'probe': cmd_probe}[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
