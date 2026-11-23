# coding: utf-8
"""dy_share.py —— 抖音视频分享量上报引擎

复用同目录既有模块，不重复维护签名与设备池：
    function.py     infos / devices34 设备池、全局连接池 http_client、request_id()
    dy_abogus.py    web 端 a_bogus
    xargus1128/     aid=1128 签名链（X-Gorgon / X-Argus / X-Ladon）
    xargus1233/     aid=32 签名链

对外接口：
    resolve_aweme_id(text)                    分享链接 / 短链 / 裸 ID -> 19 位 aweme_id
    share_count(aweme_id)                     读服务端真实 share_count，失败返回 -1
    boost(aweme_id, target, threads=20)       刷到目标数量，返回统计 dict
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

from dy_abogus import get_ab
from xargus1128.sign import sign_2290
from xargus1233.sign import sign1233

from function import infos, devices34, http_client, request_id, TIME_WAIT
import used_store as U

# ---------------------------------------------------------------- 配置区
SHARE_FIELD = 'share_delta'     # 抓包确认后只改这一行：share_delta / share_count / action
TARGET_PLAY_DELTA = 1           # 目标条目里 play_delta 的取值，0 表示不夹带播放量
NOISE_RATIO = 2                 # 每个批次混入的随机 item_id 条数
SHARE_LINK_MODE = 'off'         # off / soft / strict；路径需抓包确认后才打开
SLEEP_RANGE = (0.8, 2.5)        # 单线程两次上报之间的间隔（秒）
DEVICE_COOLDOWN = 86400         # 同一设备对同一视频的复用冷却（秒）
VIDEO_DAY_CAP = U.DAY_CAP       # 单条视频每个自然日最多入账多少条，默认 1800
MAX_FAILURES = 15               # 单通道连续失败上限
THROTTLE_LIMIT = 6             # 连续「设备存活但空包」几次判定视频被限流
THROTTLE_BACKOFF = (10, 25)     # 疑似限流时的退避区间（秒）
MIN_YIELD = 0.15                # 投产比低于此值判定视频吃不下量
YIELD_WINDOW = 30               # 至少跑多少次才判投产比
STALL_TOLERANCE = 8             # 接受数超出实入数多少条算异常
STALL_SECONDS = 90              # 计数停滞多久判定视频吃不下量
STALL_PAUSE = int(os.environ.get('SHARE_STALL_PAUSE', 90))       # 停滞时暂停多久等落账
STALL_PAUSE_ROUNDS = int(os.environ.get('SHARE_STALL_ROUNDS', 6))  # 暂停几轮都不动才收工
SETTLE_SECONDS = 60             # 收工后等计数回填的最长时间
SETTLE_POLL = 8                 # 回填轮询间隔
# 计数窗口探测：服务端对单条视频是「开一段窗口、认满就关」，关掉之后整段时间回空包。
# 这三个值是 daily1800.py 的默认口径：探针条数、首次退避、退避上限（秒）。
WINDOW_PROBE_N = int(os.environ.get('SHARE_WINDOW_PROBE', 4))
WINDOW_GAP = int(os.environ.get('SHARE_WINDOW_GAP', 900))
WINDOW_GAP_MAX = int(os.environ.get('SHARE_WINDOW_GAP_MAX', 3600))
WINDOW_CHUNK = int(os.environ.get('SHARE_WINDOW_CHUNK', 180))
WEB_SHARE_FIELD = False         # web stats 接口对未知参数直接回 status_code:5，故默认不带

# ---- 分通道桶状态（2026-11-16 实测：32 通道对本视频约 400 条后关窗，静置 25 分钟只回补 2 条）
# 桶是「视频 × 通道」粒度，所以关窗判定必须按通道分开记，不能用一个全局 streak 一熔全停。
# 关窗后按 THROTTLE_MIN_GAP 起退避、每次 ×1.6 涨到 THROTTLE_MAX_GAP 封顶，到点自动重探。
THROTTLE_MIN_GAP = int(os.environ.get('SHARE_THROTTLE_GAP', 120))
THROTTLE_MAX_GAP = int(os.environ.get('SHARE_THROTTLE_GAP_MAX', 1800))
# 全部通道都关窗时最多空等到什么时候，到点收工，别把订单进程挂死
MAX_WAIT = int(os.environ.get('SHARE_MAX_WAIT', 1800))
# 设备复用：2026-11-16 实测同一台 32 通道设备对同一视频连打 3 次，3 次全部计入，
# 服务端不按设备去重。打开后不再为每条请求烧一台新设备，池子只当失败重试用。
ALLOW_REUSE = os.environ.get('SHARE_ALLOW_REUSE', '0') != '0'

# 1128 通道的接入域名。服务端同一套 stats 接口在多个接入点后面，限流计数器有可能
# 挂在接入网关而不是业务服务上——那样换个接入点就是换一个桶。默认用实测通过的那个，
# channel_cap.py 可以用 `1128@host` 的写法逐台试其它接入点，不传就还是这一个。
HOST_1128 = os.environ.get('SHARE_HOST_1128', 'api26-normal-hl.amemv.com')

# 分享计数上报的「附加字段」随机化。默认关闭。
# 2026-11-15 23:19 第一次测（7706079479723465882，当天 1128 通道对该视频长期回空包）：
# 32 个 (字段,取值) 组合里 7 条拿到回执，计数 303→310，看起来像"换字段就能进"。
# 但 23:27 同一视频做 A/B（各 30 条、全新设备、60 秒等落账）：
#     不加附加字段 → 回执 9 条，真实计入 9 条（30%）
#     随机附加字段 → 回执 14 条，真实计入 2 条（6.7%）
# 结论：回执 ≠ 计入。加未知字段只是让服务端"回你一句"，条目照样不计数。
# 所以默认关掉；要复测用 SHARE_RANDOM_EXTRA=1 打开。
SHARE_RANDOM_EXTRA = os.environ.get('SHARE_RANDOM_EXTRA', '0') != '0'
EXTRA_FIELDS = ('share_type', 'channel', 'share_platform', 'from')
EXTRA_VALUES = ('weixin', 'wechat_moments', 'qq', 'qzone', 'douyin_message',
                'copy_link', 'weibo', 'save_local')


def random_extra() -> dict:
    return {random.choice(EXTRA_FIELDS): random.choice(EXTRA_VALUES)}
DEFAULT_CHANNELS = ('1128', '32')   # 实测能承载分享字段的两条 Android 通道
COUNT_API = 'http://110.42.32.174:9001/douyin/aweme/detail/play'
DEAD_SYNC_SECONDS = 30          # 全局死号表多久同步一次到进程内的池

UA_WEB = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0")
UA_APP_32 = ("Dalvik/2.1.0 (Linux; U; Android 10; MI 6 Build/QQ3A.200805.001) "
             "VideoArticle/10.9.8 cronet/TTNetVersion:d656c124 2024-06-24 "
             "QuicVersion:1886aac1 2023-11-17")

_LOG_LOCK = threading.Lock()

# ---------------------------------------------------------------- 设备池扩容
POOL_EXTRA_JSON = 'pool_extra.json'   # 追加 1128 设备：devices34 同结构
POOL_EXTRA_TXT = 'pool_extra.txt'     # 追加 32 设备：infos 同结构原始行


_EXTRA_CACHE = {}


def load_extra_devices(path: str = POOL_EXTRA_JSON) -> list:
    """从 pool_extra.json 追加设备，文件不存在就返回空表，原池行为完全不变。

    为什么外挂文件而不是往 devices34 里插：那是 1688 条内联字面量，扩容要动文件
    结构，容易改坏；外挂 JSON 只追加、不重写原表，出问题删文件即回滚。
    每条必须有 device_id / cookie / device_token / install_id / cdid，
    缺任一就走不了 1128 签名链，这条直接跳过。

    为什么按 mtime 缓存：这个文件已经 5.8 MB，ShareBooster.__init__ 与每次 burst
    都要解析一次，8 个进程 8 线程同时开跑时光在解析上就花掉十几秒。
    """
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    hit = _EXTRA_CACHE.get(path)
    if hit and hit[0] == mtime:
        return list(hit[1])
    try:
        with open(path, encoding='utf-8') as f:
            rows = json.load(f)
    except Exception:
        return []
    need = ('device_id', 'cookie', 'device_token', 'install_id', 'cdid')
    out = []
    for d in rows:
        if not isinstance(d, dict):
            continue
        if not all(str(d.get(k) or '').strip() for k in need):
            continue
        d.setdefault('channel', 'xiaomi_1128_64')
        d.setdefault('device_brand', 'xiaomi')
        d.setdefault('device_type', 'M2012K11AC')
        d.setdefault('os_api', 30)
        d.setdefault('os_version', '11')
        d.setdefault('resolution', '1080*2400')
        d.setdefault('dpi', 440)
        out.append(d)
    _EXTRA_CACHE[path] = (mtime, out)
    return list(out)


def load_extra_infos(path: str = POOL_EXTRA_TXT) -> list:
    """从 pool_extra.txt 追加 32 通道设备，每行一条 infos 原始串。

    为什么单独一个 txt：infos 的元素是 `hash-1.0.1----device_id----install_id-…----接码url`
    这种单行字符串，用 txt 一行一条最省事，不用转义。
    同样按 mtime 缓存：几万行文本每次 burst 重读一遍纯属浪费。
    """
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    hit = _EXTRA_CACHE.get(path)
    if hit and hit[0] == mtime:
        return list(hit[1])
    try:
        with open(path, encoding='utf-8') as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except Exception:
        return []
    out = [ln for ln in lines if ln.count('----') >= 2]
    _EXTRA_CACHE[path] = (mtime, out)
    return list(out)


def log(*parts):
    line = ' | '.join(str(p) for p in parts)
    with _LOG_LOCK:
        print(f'{time.strftime("%H:%M:%S")} | {line}', flush=True)


def _mark_used(device_id: str, aweme_id: str, ok: bool):
    """台账登记 (设备 × 视频)。

    设备复用模式下不登记：实测服务端不按设备去重（同一台连打 3 次 3 次全计入），
    再按「一台设备只用一次」记账，等于把 8000 台的池子在 1800 条上烧空，
    还让 free_devices 误判「设备池已用尽」提前收工。
    """
    if ALLOW_REUSE:
        return
    U.mark(device_id, aweme_id, ok)


# ---------------------------------------------------------------- 基础工具
def resolve_aweme_id(text: str) -> str:
    """把分享链接、短链、modal_id 链接、裸 ID 统一成 19 位 aweme_id。"""
    text = (text or '').strip().replace('\t', '')
    if re.fullmatch(r'\d{19}', text):
        return text
    if 'v.douyin.com' in text:
        url = text if text.startswith('http') else 'https://' + text
        try:
            res = http_client.get(url, follow_redirects=False, timeout=TIME_WAIT)
            loc = res.headers.get('location', '')
            if loc:
                text = loc
        except Exception:
            pass
    m = re.search(r'(?:video|note|slides)/(\d{19})', text)
    if m:
        return m.group(1)
    m = re.search(r'modal_id=(\d{19})', text)
    if m:
        return m.group(1)
    m = re.search(r'(\d{19})', text)
    return m.group(1) if m else ''


_COUNT_CACHE = {}
_COUNT_LOCK = threading.Lock()
_COUNT_API_DOWN_UNTIL = 0.0


def share_count(aweme_id: str, retries: int = 3, ttl: float = 8.0) -> int:
    """读真实分享量。优先官方 detail（准），第三方接口兜底。返回 -1 表示都不可用。

    两处缓存都是必需的：
      结果缓存 ttl 秒——主循环每秒调一次，detail 单次超时 5 秒；不缓存的话单线程
      会被读计数卡死，停滞判定、落账判定、done>=target 全部失真。
      三方接口负缓存 10 分钟——110.42.32.174:9001 早已连不通，原来每次调用都白等
      5 秒超时再重试，多线程同时等就是几十秒僵死，还会拖住设备轮转。
    """
    global _COUNT_API_DOWN_UNTIL
    now = time.time()
    if ttl > 0:
        with _COUNT_LOCK:
            hit = _COUNT_CACHE.get(aweme_id)
            if hit and hit[0] >= 0 and now - hit[1] < ttl:
                return hit[0]
    pool = _probe_pool()
    val = -1
    for _ in range(max(1, retries)):
        device = pool.get() if pool else None
        if device:
            stats = read_statistics(device, aweme_id)
            if stats and stats.get('share_count') is not None:
                val = int(stats['share_count'])
                break
        if time.time() >= _COUNT_API_DOWN_UNTIL:
            try:
                res = http_client.get(COUNT_API, params={'aweme_id': aweme_id},
                                      timeout=TIME_WAIT)
                if res.status_code == 200 and 'share_count' in res.text:
                    val = int(res.json()['aweme_detail']['statistics']['share_count'])
                    break
            except Exception:
                _COUNT_API_DOWN_UNTIL = time.time() + 600
        time.sleep(0.3)
    if val >= 0:
        with _COUNT_LOCK:
            _COUNT_CACHE[aweme_id] = (val, time.time())
    return val


_PROBE = {'pool': None}


def _probe_pool():
    """读接口专用的设备池，冷却为 0，纯轮转不占上报配额。"""
    if _PROBE['pool'] is None:
        try:
            _PROBE['pool'] = DevicePool(devices34, cooldown=0)
        except Exception:
            _PROBE['pool'] = False
    return _PROBE['pool'] or None


def gen_mstoken(n: int = 107) -> str:
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
    return ''.join(random.choice(alphabet) for _ in range(n))


_TTWID = {'value': '', 'ts': 0.0}
_TTWID_LOCK = threading.Lock()


def get_ttwid(force: bool = False) -> str:
    with _TTWID_LOCK:
        now = time.time()
        if not force and _TTWID['value'] and now - _TTWID['ts'] < 3600:
            return _TTWID['value']
        body = {
            "region": "cn", "aid": 1768, "needFid": False,
            "service": "www.ixigua.com",
            "migrate_info": {"ticket": "", "source": "node"},
            "cbUrlProtocol": "https", "union": True,
        }
        try:
            res = http_client.post('https://ttwid.bytedance.com/ttwid/union/register/',
                                   json=body, timeout=TIME_WAIT)
            for cookie in res.headers.get_list('set-cookie'):
                m = re.search(r'ttwid=([^;]+)', cookie)
                if m:
                    _TTWID['value'] = m.group(1)
                    _TTWID['ts'] = now
                    return _TTWID['value']
        except Exception:
            pass
        return _TTWID['value']


class _PoolBase:
    """设备池公共逻辑：冷却取用 + 死号剔除 + 健康度统计。

    死号判定：服务端对失效设备返回 200 空包（不是 4xx，也没有 status_code），
    这种情况必须把设备拉黑，否则它会一直占着池子把整轮请求吞掉。
    """

    BAD_LIMIT = 2  # 连续空包几次就判死

    def __init__(self, items, cooldown: float = DEVICE_COOLDOWN):
        self._items = [x for x in items if x.get('device_id')]
        self._cooldown = cooldown
        self._used = {}
        self._bad = {}
        self._dead = set()
        self._dead_local = set()   # 本进程亲自判死的，复活全局表也不放开
        self._dead_ts = 0.0
        self.gap = 0.0             # 同一台设备两次分享的最小间隔；0 = 不控（播放量老行为）
        self._stat = {'ok': 0, 'empty': 0, 'err': 0}
        self._lock = threading.Lock()

    def __len__(self):
        return len(self._items)

    @property
    def alive(self) -> int:
        return len(self._items) - len(self._dead)

    def stats(self) -> dict:
        with self._lock:
            return {'total': len(self._items), 'alive': self.alive,
                    'dead': len(self._dead), **self._stat}

    def mark_ok(self, device_id: str):
        with self._lock:
            self._stat['ok'] += 1
            self._bad[device_id] = 0

    def mark_bad(self, device_id: str, reason: str = 'empty'):
        """记一次失败；连续 BAD_LIMIT 次就进死号集合，永不派发。

        判死同时写进全局 dead 表：别的接单进程 30 秒内就能看到，不用各自再试一遍。
        """
        with self._lock:
            self._stat['empty' if reason == 'empty' else 'err'] += 1
            n = self._bad.get(device_id, 0) + 1
            self._bad[device_id] = n
            if n >= self.BAD_LIMIT:
                self._dead.add(device_id)
                self._dead_local.add(device_id)
        if n >= self.BAD_LIMIT:
            try:
                U.mark_dead(device_id)
            except Exception:
                pass

    def _sync_dead(self):
        """把别的进程判死的设备拉进本地死号集合，避免重复踩坑。

        只是「取并集 + 定期刷新」：全局表里被 revive 掉的设备，本进程如果没亲自判过死，
        下一轮同步就会重新出现在候选里。
        """
        now = time.time()
        if now - self._dead_ts < DEAD_SYNC_SECONDS:
            return
        self._dead_ts = now
        try:
            shared = U.dead_ids()
        except Exception:
            return
        with self._lock:
            self._dead = set(self._dead_local) | shared

    def get(self, aweme_id: str = "", gap_seconds: float = 0.0, soft_max: int = 0):
        """取一台可用设备。

        aweme_id 非空时接台账过滤：这条视频没用过、且距上次分享已过 gap_seconds。
        过滤后没有可用设备就返回 None——绝不能回退复用，重复上报换回的是空包，
        还会把 throttle_streak 记满，让整轮提前熔断。

        gap_seconds 没传时用 self.gap：接单链路要靠它给同一台设备压节奏，
        否则一台设备会在几秒内被连派给几十条视频，服务端直接判机器。
        """
        self._sync_dead()
        gap_seconds = gap_seconds or self.gap
        with self._lock:
            if not self._items:
                return None
            now = time.time()
            pool = [d for d in self._items if d['device_id'] not in self._dead]
            if not pool:
                return None
            if aweme_id:
                if gap_seconds <= 0:
                    # 只要求这条视频没被这台设备占用过：一次取全量封锁集合在内存里比。
                    # 原来每派一台设备就把整池（8000+）切成 500 一块查 17 条 IN 查询，
                    # 24 线程等于每秒几百条复杂 SQL，还全被进程锁串行化。
                    blocked = U.blocked_ids(aweme_id)
                    cand = [d for d in pool if d['device_id'] not in blocked]
                    if not cand:
                        return None
                    pool = cand
                else:
                    cand_ids = U.free_devices([d['device_id'] for d in pool],
                                              aweme_id, gap_seconds, soft_max)
                    if not cand_ids:
                        return None
                    keep = set(cand_ids)
                    pool = [d for d in pool if d['device_id'] in keep]
            else:
                # 非分享场景（如只刷播放量）沿用老行为：进程内冷却 + 跑完一轮复用最久未用的 10%
                fresh = [d for d in pool
                         if now - self._used.get(d['device_id'], 0.0) >= self._cooldown]
                if not fresh:
                    keep_n = max(1, len(pool) // 10)
                    fresh = sorted(pool,
                                   key=lambda d: self._used.get(d['device_id'], 0.0))[:keep_n]
                pool = fresh
            device = random.choice(pool)
            self._used[device['device_id']] = now
            return device


class DevicePool(_PoolBase):
    """devices34 的元素：device_id / install_id / cookie / device_token / cdid ..."""

    def __init__(self, devices, cooldown: float = DEVICE_COOLDOWN):
        super().__init__(list(devices), cooldown)


class InfoPool(_PoolBase):
    """infos 的原始格式：hash-1.0.1----device_id----install_id-xxx----接码url"""

    def __init__(self, raw_infos, cooldown: float = DEVICE_COOLDOWN):
        super().__init__([self._parse(x) for x in raw_infos], cooldown)

    @staticmethod
    def _parse(raw) -> dict:
        arr = str(raw).split('----')
        device_id = arr[1].strip() if len(arr) > 1 else ''
        install_raw = arr[2].strip() if len(arr) > 2 else ''
        install_id = install_raw.split('-')[0]
        if not device_id:
            # 空串会让命中判定失效，直接给个哨兵值由基类过滤掉
            device_id = ''
        return {'device_id': device_id, 'install_id': install_id}


# ---------------------------------------------------------------- 链路层：先拿分享短链
def share_link_1128(aweme_id: str, device: dict):
    """返回 (share_url, share_link_id)；失败返回 (None, None)"""
    try:
        ticket = int(time.time() * 1000)
        ts = int(time.time())
        url = (f'https://api26-normal-hl.amemv.com/aweme/v1/aweme/share/?_rticket={ticket}'
               f'&ts={ts}&channel=copy_link&aweme_id={aweme_id}&share_scene=1&')
        headers = {
            'User-Agent': f'com.ss.android.ugc.aweme/220901 (Linux; U; Android {device["os_version"]}; '
                          f'zh_CN; {device["device_type"]}; Build/RKQ1.200826.002; '
                          f'Cronet/TTNetVersion:4b3ac0f8 2022-07-22 QuicVersion:6fe86402 2022-05-31)',
            'x-tt-dt': device['device_token'],
            'x-ss-dp': '1128',
            'x-tt-request-tag': 's=0;p=0',
            'sdk-version': '2',
            'passport-sdk-version': '20374',
            'x-vc-bdturing-sdk-version': '3.1.0.cn',
            'Cookie': device['cookie'],
            'x-common-params-v2': (
                f"ac=wifi&aid=1128&app_name=aweme&app_type=normal&cdid={device['cdid']}"
                f"&channel=wandoujia_1128_0413&device_brand={device['device_brand']}"
                f"&device_id={device['device_id']}&device_platform=android&device_type={device['device_type']}"
                f"&dpi={device['dpi']}&iid={device['install_id']}&language=zh&manifest_version_code=220901"
                f"&os=android&os_api={device['os_api']}&os_version={device['os_version']}"
                f"&package=com.ss.android.ugc.aweme&resolution={device['resolution']}&ssmix=a"
                f"&version_code=220900&version_name=22.9.0"),
        }
        headers.update(sign_2290(params=url + headers['x-common-params-v2'],
                                 payload='', cookie=headers['Cookie']))
        headers.pop('X-SS-STUB', None)
        res = http_client.get(url, headers=headers, timeout=TIME_WAIT)
        if res.status_code != 200:
            return None, None
        data = res.json()
        info = data.get('share_info') or {}
        return info.get('share_url'), (info.get('share_link_id')
                                       or info.get('item_share_id') or '')
    except Exception:
        return None, None


_LINK_CACHE = {}
_LINK_LOCK = threading.Lock()
LINK_TTL = 600


def cached_share_link(aweme_id: str, device=None, cookie: str = ''):
    """同一个视频的短链缓存 10 分钟。
    为什么缓存：一次分享只需一次 share_link，20 线程并发时每次都去取会产生
    20 倍的短链请求，短链接口的频控比 stats 接口严得多。
    """
    now = time.time()
    with _LINK_LOCK:
        hit = _LINK_CACHE.get(aweme_id)
        if hit and now - hit[2] < LINK_TTL and hit[1]:
            return hit[0], hit[1]
    url, link_id = get_share_link(aweme_id, device=device, cookie=cookie)
    if link_id:
        with _LINK_LOCK:
            _LINK_CACHE[aweme_id] = (url, link_id, now)
    return url, link_id


def share_link_6368(aweme_id: str, cookie: str = ''):
    """web 端取短链，靠 a_bogus，不依赖登录态。"""
    base = ('https://www.douyin.com/aweme/v1/web/aweme/share/?device_platform=webapp&aid=6383'
            '&channel=channel_pc_web&pc_client_type=1&version_code=170400&version_name=17.4.0'
            f'&aweme_id={aweme_id}&channel_type=copy_link'
            f'&_rticket={int(time.time() * 1000)}&')
    try:
        full = base + '&a_bogus=' + get_ab(base.split('?')[0], UA_WEB, f'aweme_id={aweme_id}')
        res = http_client.get(full, headers={
            'user-agent': UA_WEB,
            'referer': 'https://www.douyin.com/',
            'cookie': cookie,
        }, timeout=TIME_WAIT)
        if res.status_code != 200:
            return None, None
        data = res.json()
        info = data.get('share_info') or {}
        if not info:
            info = (data.get('data') or {}).get('share_info') or {}
        return info.get('share_url'), (info.get('share_link_id')
                                       or info.get('item_share_id') or '')
    except Exception:
        return None, None


def get_share_link(aweme_id: str, device=None, cookie: str = ''):
    """优先 Android 通道（带登录态更可信），失败再落 web。"""
    if device:
        url, link_id = share_link_1128(aweme_id, device)
        if link_id:
            return url, link_id
    return share_link_6368(aweme_id, cookie)


# ---------------------------------------------------------------- 计数层：batch_stats 构造
def batch_stats(aweme_id: str, share_link_id: str = '', field: str = SHARE_FIELD,
                noise: int = NOISE_RATIO, extra: dict | None = None,
                copies: int = 1):
    """目标条目带 share 字段，噪声条目只带 play_delta。
    为什么混噪声：批次里行为 100% 一致，风控按比例异常直接整批丢弃。

    extra 是给目标条目追加的字段（如 share_type / channel），用来测「分享类型」
    这类维度是不是独立的计数单元——如果服务端的桶按 (视频 × 分享类型) 分，
    那么在某个类型上打满之后，换个类型还能继续进量。

    copies 是「同一个条目在一批里重复几条」，每条各带一次分享字段。
    为什么要这个开关：服务端计数单位是「条目」还是「请求」决定了桶的用法——
    按条目计就是一条请求吃多个令牌（桶容量不变但请求数少 N 倍，能绕开请求频次限制），
    按请求计就还是一条一条发。copies>1 已实测不放大（一条请求只 +1），保留开关只为复验。
    """
    if extra is None and field and SHARE_RANDOM_EXTRA:
        extra = random_extra()      # 每条换一个附加字段，见 SHARE_RANDOM_EXTRA 的注释
    ts = int(time.time())
    copies = max(1, int(copies or 1))
    ids = [aweme_id] * copies + [str(random.randint(7500000000000000000,
                                                    7599999999999999999))
                                 for _ in range(noise)]
    random.shuffle(ids)
    cid = str(random.randint(1806726683227275, 1906726683227275))
    items = []
    for idx, item_id in enumerate(ids):
        item = {
            "follower_status": 0,
            "is_commerce": 0,
            "request_id": request_id(),
            "follow_status": 0,
            "is_familiar": 0,
            "pre_is_feed_card_template": False,
            "item_id": item_id,
            "play_delta": TARGET_PLAY_DELTA,
            "first_install_time": 1700076504,
            "cid": cid,
            "is_in_scope": "false",
            "action_time": ts - random.randint(60, 900),
            "aweme_type": 0,
            "order": 3 + idx,
            "is_from_ad": 0,
            "hide_mix_entry": False,
            "previous_page": "homepage_hot",
            "tab_type": 0,
            "nearby_level": 0,
        }
        if item_id == aweme_id and field:
            item[field] = 1
            if share_link_id:
                item['share_link_id'] = share_link_id
            if extra:
                item.update(extra)
        elif item_id == aweme_id and not field:
            pass
        items.append(item)
    return {
        'batch_item_ids': ','.join(ids),
        'batch_stats': json.dumps(items, ensure_ascii=False, separators=(',', ':')),
        'batch_time': f'1{random.randint(100, 999)}',
        'target_batch_count': str(len(ids)),
        'target_batch_time': '5',
    }


APP_COMMON = (
    "ac=wifi&aid=1128&app_name=aweme&app_type=normal&cdid={cdid}"
    "&channel=wandoujia_1128_0413&device_brand={brand}"
    "&device_id={device_id}&device_platform=android&device_type={model}"
    "&dpi={dpi}&iid={install_id}&language=zh&manifest_version_code=220901"
    "&os=android&os_api={os_api}&os_version={os_version}"
    "&package=com.ss.android.ugc.aweme&resolution={resolution}&ssmix=a"
    "&version_code=220900&version_name=22.9.0"
)

UA_APP_1128 = ("com.ss.android.ugc.aweme/220901 (Linux; U; Android {os_version}; zh_CN; "
               "{model}; Build/RKQ1.200826.002; Cronet/TTNetVersion:4b3ac0f8 2022-07-22 "
               "QuicVersion:6fe86402 2022-05-31)")


# ---------------------------------------------------------------- 设备存活探测
DETAIL_HOST = 'https://api26-normal-hl.amemv.com/aweme/v1/aweme/detail/'
STAT_KEYS = ('play_count', 'share_count', 'digg_count', 'comment_count',
             'download_count', 'forward_count', 'collect_count')


def _detail_body(device: dict, aweme_id: str):
    """用给定的设备读一次 detail，返回整个响应 json；读不到返回 None。

    为什么留整包：视频被删时 aweme_detail 是 null，删的原因只出现在
    filter_detail.filter_reason（status_deleted / status_self_see /
    status_prohibited ...）。只取 aweme_detail 就永远分不清「删了」和「抖了」。
    """
    try:
        ticket = int(time.time() * 1000)
        ts = int(time.time())
        url = (f'{DETAIL_HOST}?aweme_id={aweme_id}&_rticket={ticket}'
               f'&ts={ts}&need_personal_recommend=1&')
        common = APP_COMMON.format(
            cdid=device['cdid'], brand=device['device_brand'],
            device_id=device['device_id'], model=device['device_type'],
            dpi=device['dpi'], install_id=device['install_id'],
            os_api=device['os_api'], os_version=device['os_version'],
            resolution=device['resolution'])
        headers = {
            'User-Agent': UA_APP_1128.format(os_version=device['os_version'],
                                             model=device['device_type']),
            'x-tt-dt': device['device_token'],
            'x-tt-request-tag': 's=0;p=0',
            'x-ss-dp': '1128',
            'sdk-version': '2',
            'passport-sdk-version': '20374',
            'x-vc-bdturing-sdk-version': '3.1.0.cn',
            'Cookie': device['cookie'],
            'x-common-params-v2': common,
        }
        headers.update(sign_2290(params=url + common, payload='',
                                 cookie=headers['Cookie']))
        res = http_client.get(url, headers=headers, timeout=TIME_WAIT)
        if res.status_code != 200 or not res.text.strip():
            return None
        return res.json()
    except Exception:
        return None


def _detail_raw(device: dict, aweme_id: str):
    """只要 aweme_detail 本体；视频不在了（null）返回 None。"""
    body = _detail_body(device, aweme_id)
    if not isinstance(body, dict):
        return None
    return body.get('aweme_detail') or None


def read_statistics(device: dict, aweme_id: str):
    """用给定的设备读一次 detail，返回 statistics 字典；读不到返回 None。

    保持原语义不变：视频被删时这里照样返回 None，probe_alive 靠它判设备死活，
    语义一改就会把好设备当死号。判定视频存亡走 video_state，那个读 status。
    """
    raw = _detail_raw(device, aweme_id)
    if not raw:
        return None
    stats = raw.get('statistics') or {}
    return {k: stats[k] for k in STAT_KEYS if k in stats} or None


_GONE_CACHE = {}
GONE_TTL = float(os.environ.get('SHARE_GONE_TTL', 300))
# filter_reason 是 aweme_detail 为 null 时唯一的线索。实测不存在的 id 返回 core_dep，
# 抖音对「删了 / 不存在 / 不可见」统一给这个兜底值；带 status_ 前缀的才是明确结论。
_GONE_REASON = {
    'status_deleted': 'deleted',
    'status_not_exist': 'deleted',
    'core_dep': 'deleted',
    'status_self_see': 'private',
    'status_friend_see': 'private',
    'status_prohibited': 'prohibited',
}


def video_state(aweme_id: str, tries: int = 3) -> str:
    """判定视频当前状态：alive / deleted / private / prohibited / unreachable。

    deleted / private / prohibited 是确定结论，调用方可以据此直接结算；
    unreachable 只说明这次没读到，绝不能当成视频没了去写完成。

    为什么必须读 filter_detail：视频被删时 aweme_detail 是 null，整包只剩
    filter_detail.filter_reason 这一个线索；只看 aweme_detail 的话删除、限流、
    设备死号三者完全同形，只能靠「连续读不到几次」猜。

    为什么 core_dep 要两次确认：它是兜底 reason，风控瞬时隐藏、详情组装失败
    也会返回它。一次就认会把暂时不可见的视频写成完成，客户一分钱拿不回来。

    为什么要缓存 300 秒：settle_sweep 每圈要对一批到期单判一次，不缓存就是
    每圈多打几次 detail，白耗设备也白担风控。
    缓存只存确定结论，不存 unreachable —— 抖一次就缓存五分钟，整个池子的
    判定都会被带成假的，反而误判成「视频没了」写完成。
    """
    aweme_id = str(aweme_id or '')
    if not aweme_id:
        return 'unreachable'
    now = time.time()
    hit = _GONE_CACHE.get(aweme_id)
    if hit and now - hit[1] < GONE_TTL:
        return hit[0]
    pool = _probe_devices()
    if not pool:
        return 'unreachable'
    state = 'unreachable'
    soft = 0
    for _ in range(max(2, int(tries))):
        body = _detail_body(random.choice(pool), aweme_id)
        if not isinstance(body, dict):
            continue
        ad = body.get('aweme_detail')
        if ad:
            st = ad.get('status') or {}
            if st.get('is_delete'):
                state = 'deleted'
            elif st.get('is_prohibited'):
                state = 'prohibited'
            elif (st.get('is_private') or int(st.get('private_status') or 0) != 0
                  or st.get('self_see') or int(st.get('part_see') or 0) != 0):
                state = 'private'
            else:
                state = 'alive'
            break
        reason = str((body.get('filter_detail') or {}).get('filter_reason') or '')
        mapped = _GONE_REASON.get(reason) or ''
        if mapped and reason != 'core_dep':
            state = mapped
            break
        if mapped == 'deleted':
            soft += 1
            if soft >= 2:
                state = 'deleted'
                break
            continue
        # 既没有 aweme_detail 也没有认识的 reason：这次算没读到，接着试
    if state != 'unreachable':
        _GONE_CACHE[aweme_id] = (state, now)
        if len(_GONE_CACHE) > 4096:
            cut = now - GONE_TTL
            for k in [k for k, v in _GONE_CACHE.items() if v[1] < cut]:
                _GONE_CACHE.pop(k, None)
    return state


def probe_alive(device: dict, aweme_id: str) -> bool:
    """拿这台设备读一次 detail：能读到 stats 说明设备活着。

    空包有两种成因，必须分开：设备死了，或者这个视频当下被限流。
    分不清就会把好设备当死号拉黑，一次限流误杀一片。
    """
    return read_statistics(device, aweme_id) is not None


_PROBE_DEVICES = None


def _probe_devices() -> list:
    """存活探测用的设备表：1128 全池，首次调用时把外挂池一起并进来。"""
    global _PROBE_DEVICES
    if _PROBE_DEVICES is None:
        _PROBE_DEVICES = list(devices34) + load_extra_devices()
    return _PROBE_DEVICES


def probe_video_alive(aweme_id: str, tries: int = 2) -> bool:
    """换几台 1128 设备轮流读 detail，判断这条视频还在不在（只探视频，不动当前设备）。

    为什么单独写一个：32 通道的 infos 字段不全（没有 device_token / cdid / 分辨率），
    跑不了 1128 的签名链，没法用「自己这台设备」做探活；只能借外部设备看视频状态。
    """
    pool = _probe_devices()
    if not pool:
        return False
    for _ in range(max(1, int(tries))):
        try:
            if read_statistics(random.choice(pool), aweme_id) is not None:
                return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------- 通道 A：Android aid=1128
def report_1128(aweme_id: str, pool: DevicePool, field: str = SHARE_FIELD,
                day_cap: int = None, extra: dict | None = None,
                reuse: bool = False, copies: int = 1):
    """aid=1128 上报。空包=死号，换设备重试，最多 3 台。

    day_cap 是本轮生效的单视频每日上限（None 用全局 1200，0 表示不限）。
    为什么要传进来而不是用全局：share_cli 允许 --day-cap 200 这种临时口径，
    写库那一层如果还按 1200 判，上层调低了下面照样能刷过线。
    """
    cap = VIDEO_DAY_CAP if day_cap is None else int(day_cap)
    last = 'no device'
    # reuse=True 时不查 (设备×视频) 台账，用来验证服务端是否允许同一台设备对同一条
    # 视频重复计入。台账挡的是我们自己，不是服务端，测「上限在哪」必须把这层摘掉。
    ledger_key = '' if (reuse or ALLOW_REUSE) else (aweme_id if field else '')
    if field and cap and U.day_cap_hit(aweme_id, cap):
        # 台账已到顶就直接收手，别再多打一条：多发的那一条服务端照样计数，
        # 会把「封顶 1200」冲成 1200+并发数，客户对账时对不上。
        return False, 'day cap reached'
    for _ in range(3):
        device = pool.get(ledger_key)
        if not device:
            return False, 'device pool empty'
        link_id = ''
        if SHARE_LINK_MODE != 'off':
            _, link_id = cached_share_link(aweme_id, device=device)
            if SHARE_LINK_MODE == 'strict' and not link_id:
                return False, 'share_link missing'
        try:
            ticket = int(time.time() * 1000)
            ts = int(time.time())
            url = (f'https://{HOST_1128}/aweme/v1/aweme/stats/'
                   f'?_rticket={ticket}&need_personal_recommend=1&ts={ts}&')
            payload = batch_stats(aweme_id, link_id, field, extra=extra, copies=copies)
            common = APP_COMMON.format(
                cdid=device['cdid'], brand=device['device_brand'],
                device_id=device['device_id'], model=device['device_type'],
                dpi=device['dpi'], install_id=device['install_id'],
                os_api=device['os_api'], os_version=device['os_version'],
                resolution=device['resolution'])
            headers = {
                'User-Agent': UA_APP_1128.format(os_version=device['os_version'],
                                                 model=device['device_type']),
                'x-tt-dt': device['device_token'],
                'x-tt-request-tag': 's=0;p=0',
                'x-ss-dp': '1128',
                'sdk-version': '2',
                'passport-sdk-version': '20374',
                'x-vc-bdturing-sdk-version': '3.1.0.cn',
                'Cookie': device['cookie'],
                'x-common-params-v2': common,
            }
            headers.update(sign_2290(params=url + common,
                                     payload=urllib.parse.urlencode(payload),
                                     cookie=headers['Cookie']))
            res = http_client.post(url, data=payload, headers=headers, timeout=TIME_WAIT)
            if res.status_code != 200:
                last = f'http {res.status_code}'
                pool.mark_bad(device['device_id'], 'err')
                _mark_used(device['device_id'], aweme_id, False)
                continue
            text = res.text
            if 'X-TT-System-Error' in text:
                pool.mark_bad(device['device_id'], 'err')
                _mark_used(device['device_id'], aweme_id, False)
                last = 'system error'
                continue
            if not text.strip():
                # 空包要先分清是死号还是视频被限流，不能一律拉黑
                if probe_alive(device, aweme_id):
                    # 设备是活的，是视频不吃量；不记台账，这台设备还能留给别的视频
                    return False, 'throttled:video'
                pool.mark_bad(device['device_id'], 'empty')
                _mark_used(device['device_id'], aweme_id, False)
                last = 'empty body(dead device)'
                continue
            pool.mark_ok(device['device_id'])
            _mark_used(device['device_id'], aweme_id, True)
            U.bump_day(device['device_id'])
            if field:
                # 视频日入账量：写库时原子判上限，到顶就这条不算成功
                if not U.bump_video(aweme_id, cap=cap):
                    return False, 'day cap reached'
            return True, text[:200]
        except Exception as exc:
            pool.mark_bad(device['device_id'], 'err')
            _mark_used(device['device_id'], aweme_id, False)
            last = str(exc)
    return False, last


# ---------------------------------------------------------------- 通道 A'：aid=32
def report_32(aweme_id: str, pool: InfoPool, field: str = SHARE_FIELD,
              day_cap: int = None, extra: dict | None = None,
              host: str = '', channel_param: str = '', cdid_param: str = '',
              copies: int = 1):
    """aid=32 通道上报。空包=视频不吃量（先探视频活着没），返回 throttled:video。

    host / channel_param / cdid_param / copies 是桶扫描用的旋钮，默认值等于原来的写死值，
    日常调用不传就跟改动前完全一样。桶按哪些维度分，用 bucket_family_test.py 与
    channel_cap.py 实测，不靠猜：1128 关窗时 32 还开着，桶至少是 (视频 × 通道) 粒度。
    """
    cap = VIDEO_DAY_CAP if day_cap is None else int(day_cap)
    if field and cap and U.day_cap_hit(aweme_id, cap):
        return False, 'day cap reached'
    device = pool.get('' if ALLOW_REUSE else (aweme_id if field else ''))
    if not device:
        return False, 'info pool empty'
    link_id = ''
    if SHARE_LINK_MODE != 'off':
        _, link_id = cached_share_link(aweme_id)
        if SHARE_LINK_MODE == 'strict' and not link_id:
            return False, 'share_link missing'
    try:
        install_id = device['install_id']
        device_id = device['device_id']
        cookie = f'install_id={install_id};'
        ticket = int(time.time() * 1000)
        # 通道参数可覆盖：桶如果按「视频 × 渠道 × 入口」分，换一个 channel 值、
        # 换一个 cdid、换一个接入域名就是换一只新桶。这三个默认值保持实测通过的原值，
        # 只有 channel_cap.py --hosts 做接入点扫描时才传别的值。
        host = host or 'api5-platform-m-hj.amemv.com'
        chan = channel_param or 'xigua_pc_download_1'
        cdid = cdid_param or 'e5372299-f4a8-4a77-bff6-7d5776ca011b'
        url = (f'https://{host}/aweme/v1/aweme/stats/'
               '?device_platform=android&os=android&ssmix=a'
               f'&_rticket={ticket}&cdid={cdid}'
               f'&channel={chan}&aid=32&app_name=video_article'
               '&version_code=100908&version_name=10.9.8&manifest_version_code=698'
               '&update_version_code=10090807&resolution=1080*1920&dpi=480'
               '&device_type=MI+6&device_brand=Xiaomi&language=zh&os_api=29&os_version=10'
               '&ac=wifi&package=com.ss.android.article.video&device_platform_ext=phone'
               f'&host_abi=armeabi-v7a&iid={install_id}&device_id={device_id}')
        payload = batch_stats(aweme_id, link_id, field, extra=extra, copies=copies)
        body = urllib.parse.urlencode(payload)
        headers = {
            'Host': host,
            'Content-Length': str(len(body)),
            'X-SS-REQ-TICKET': str(ticket),
            'x-bd-kmsv': '1',
            'x-vc-bdturing-sdk-version': '3.7.2.cn',
            'sdk-version': '2',
            'passport-sdk-version': '50559',
            'User-Agent': UA_APP_32,
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'X-SS-STUB': hashlib.md5(body.encode()).hexdigest().upper(),
        }
        headers.update(sign1233(url, data=body, cookie=cookie, aid=32))
        res = http_client.post(url, data=body, headers=headers, timeout=TIME_WAIT)
        if res.status_code != 200:
            pool.mark_bad(device['device_id'], 'err')
            _mark_used(device['device_id'], aweme_id, False)
            return False, f'http {res.status_code}'
        text = res.text
        if 'X-TT-System-Error' in text:
            pool.mark_bad(device['device_id'], 'err')
            _mark_used(device['device_id'], aweme_id, False)
            return False, 'system error'
        if not text.strip():
            if field and probe_video_alive(aweme_id):
                # 设备能读 detail、上报却是空包 = 视频不吃量，不是号死了。
                # 不写台账、不拉黑，这台设备还能留给别的视频，否则一次限流误杀一整池。
                return False, 'throttled:video'
            pool.mark_bad(device['device_id'], 'empty')
            _mark_used(device['device_id'], aweme_id, False)
            return False, 'empty body(dead device)'
        pool.mark_ok(device['device_id'])
        _mark_used(device['device_id'], aweme_id, True)
        U.bump_day(device['device_id'])
        if field:
            if not U.bump_video(aweme_id, cap=cap):
                return False, 'day cap reached'
        return True, text[:200]
    except Exception as exc:
        pool.mark_bad(device['device_id'], 'err')
        _mark_used(device['device_id'], aweme_id, False)
        return False, str(exc)


# ---------------------------------------------------------------- 通道 B：PC web
def _web_context():
    ttwid = get_ttwid()
    ms_token = gen_mstoken()
    cookies = [f'msToken={ms_token}', 'enter_pc_once=1',
               'device_web_cpu_core=8', 'device_web_memory_size=8']
    if ttwid:
        cookies.insert(0, f'ttwid={ttwid}')
    return '; '.join(cookies), ms_token


def report_6368(aweme_id: str, field: str = SHARE_FIELD):
    try:
        cookie, ms_token = _web_context()
        link_id = ''
        if SHARE_LINK_MODE != 'off':
            _, link_id = cached_share_link(aweme_id, cookie=cookie)
            if SHARE_LINK_MODE == 'strict' and not link_id:
                return False, 'share_link missing'
        ticket = int(time.time() * 1000)
        url = ('https://www.douyin.com/aweme/v2/web/aweme/stats/?device_platform=webapp'
               '&aid=6383&channel=channel_pc_web&pc_client_type=1&pc_libra_divert=Windows'
               '&update_version_code=170400&support_h265=1&support_dash=1'
               '&version_code=170400&version_name=17.4.0&cookie_enabled=true'
               '&screen_width=2560&screen_height=1440&browser_language=zh-CN'
               '&browser_platform=Win32&browser_name=Edge&browser_version=138.0.0.0'
               '&browser_online=true&engine_name=Blink&engine_version=138.0.0.0'
               '&os_name=Windows&os_version=10&cpu_core_num=8&device_memory=8'
               f'&platform=PC&downlink=10&effective_type=4g&round_trip_time=0'
               f'&_rticket={ticket}&msToken={ms_token}&')
        body = f'aweme_type=0&item_id={aweme_id}&play_delta={TARGET_PLAY_DELTA}'
        if field and WEB_SHARE_FIELD:
            body += f'&{field}=1'
        body += '&source=0'
        if link_id:
            body += f'&share_link_id={link_id}'
        full = url + '&a_bogus=' + get_ab(url.split('?')[0], UA_WEB, body)
        headers = {
            'accept': 'application/json, text/plain, */*',
            'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'origin': 'https://www.douyin.com',
            'referer': 'https://www.douyin.com/?recommend=1',
            'user-agent': UA_WEB,
            'x-secsdk-csrf-token': 'DOWNGRADE',
            'cookie': cookie,
        }
        res = http_client.post(full, headers=headers, data=body, timeout=TIME_WAIT)
        if res.status_code != 200:
            return False, f'http {res.status_code}'
        text = res.text.replace('"', '')
        return ('fatal_item_ids' in text), text[:200]
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------- 通道注册表
# 桶是「视频 × 通道」粒度：2026-11-16 实测 1128 对目标视频全天空包的两小时里，
# 32 通道照样吃进 400 条。所以「加桶」的唯一已验证路径就是「加通道」，而加通道
# 只该是登记一行，不应该去动调度器。以后接 aid=2329（抖音极速版）这类新通道，
# 写一个 report_2329 再 register_channel('2329', ...) 就进轮转。
CHANNEL_IMPL = {}


def register_channel(name: str, fn):
    """登记一个通道实现，签名 fn(booster, aweme_id) -> (ok, raw)。"""
    CHANNEL_IMPL[name] = fn
    return fn


def _ch_1128(b, aweme):
    return report_1128(aweme, b.device_pool, b.field, day_cap=b.day_cap, copies=b.copies)


def _ch_32(b, aweme):
    return report_32(aweme, b.info_pool, b.field, day_cap=b.day_cap, copies=b.copies)


def _ch_6368(b, aweme):
    return report_6368(aweme, b.field)


register_channel('1128', _ch_1128)
register_channel('32', _ch_32)
register_channel('6368', _ch_6368)


# ---------------------------------------------------------------- 调度器
@dataclass
class BoostResult:
    aweme_id: str
    target: int
    baseline: int
    final: int
    accepted: int
    attempts: int
    elapsed: float
    channels: dict = field(default_factory=dict)
    throttled: bool = False
    day_capped: bool = False
    day_cap: int = 0
    day_used: int = 0
    reason: str = ''

    def as_dict(self) -> dict:
        return {
            'aweme_id': self.aweme_id,
            'target': self.target,
            'baseline': self.baseline,
            'final': self.final,
            'delta': (self.final - self.baseline) if self.final >= 0 and self.baseline >= 0 else self.accepted,
            'accepted': self.accepted,
            'attempts': self.attempts,
            'elapsed': round(self.elapsed, 2),
            'channels': self.channels,
            'throttled': self.throttled,
            'day_capped': self.day_capped,
            'day_cap': self.day_cap,
            'day_used': self.day_used,
            'reason': self.reason,
        }


class ShareBooster:
    """多通道并发上报，按失败率降权，靠真实 share_count 判定收工。"""

    def __init__(self, aweme_id: str, target: int, threads: int = 20,
                 timeout: float = None, channels=DEFAULT_CHANNELS,
                 verbose: bool = True, device_pool=None, info_pool=None,
                 field: str = SHARE_FIELD, counter=None, day_cap: int = None,
                 max_wait: int = 0):
        self.aweme_id = aweme_id
        self.target = max(1, int(target))
        self.threads = max(1, int(threads))
        self.timeout = timeout
        self.channels = tuple(channels)
        self.verbose = verbose
        self.field = field          # 空串 = 只报播放量，不报分享
        self.counter = counter or share_count
        # 单请求塞几个同视频条目：SHARE_COPIES=5 就是一条请求带 5 次分享字段。
        # 默认 1，行为与改动前一致；copies>1 已实测不放大，保留只为复验。
        self.copies = max(1, int(os.environ.get('SHARE_COPIES', '1')))

        # 单视频每日上限：只有报分享字段时才生效；播放模式 day_cap=0，行为与改动前一致
        self.day_cap = 0 if not field else (VIDEO_DAY_CAP if day_cap is None else int(day_cap))
        self.day_capped = False
        self.pool_empty = {c: False for c in self.channels}

        self._stop = threading.Event()
        self._gate = threading.Event()   # 暂停阀：计数停滞时清掉，工作线程原地等待
        self._gate.set()
        self.paused = 0                  # 本轮已经暂停了几次
        self._lock = threading.Lock()
        self.attempts = 0
        self.accepted = 0
        self.fails = {c: 0 for c in self.channels}
        self.oks = {c: 0 for c in self.channels}
        # 每通道一个桶状态：关窗到什么时候、下一次退避多长、本轮连续空包几次。
        # 为什么要分开：32 关窗时 1128 可能还开着，共用一个 streak 会把还开着的通道一起停掉。
        self.closed_until = {c: 0.0 for c in self.channels}
        self.chan_gap = {c: THROTTLE_MIN_GAP for c in self.channels}
        self.chan_streak = {c: 0 for c in self.channels}
        # 全通道关窗时是原地等桶回补还是直接收工。默认 0 = 收工，保持 daily1800 既有的
        # 「探窗→吃→退避」节奏不变；只有显式要「一直挂到桶开」的调用才传秒数。
        self.max_wait = max(0, int(MAX_WAIT if max_wait is None else max_wait))
        self.baseline = -1
        self.final = -1
        self.throttle_streak = 0
        self.throttled_attempts = 0     # 被桶挡回来的空包数，算投产比时要剔除
        self.all_closed_since = 0.0
        self.throttled = False
        self.reason = ''
        self.inflight = 0

        self.device_pool = device_pool or DevicePool(devices34 + load_extra_devices())
        self.info_pool = info_pool or InfoPool(infos + load_extra_infos())

    # ---------- 内部
    def _pick(self):
        now = time.time()
        alive = [c for c in self.channels
                 if self.fails[c] < MAX_FAILURES and self.closed_until.get(c, 0.0) <= now]
        if not alive:
            return None
        weights = [max(1, MAX_FAILURES - self.fails[c]) for c in alive]
        return random.choices(alive, weights=weights, k=1)[0]

    def _any_open(self) -> bool:
        now = time.time()
        return any(self.fails[c] < MAX_FAILURES and self.closed_until.get(c, 0.0) <= now
                   for c in self.channels)

    def _next_open_in(self) -> float:
        """最近的通道还要多久重探；全部判死时返回 -1。"""
        waits = [max(0.0, self.closed_until.get(c, 0.0) - time.time())
                 for c in self.channels if self.fails[c] < MAX_FAILURES]
        return min(waits) if waits else -1.0

    def _close_channel(self, channel: str):
        """把某个通道判成关窗，按指数退避排下一次重探。"""
        gap = max(THROTTLE_MIN_GAP, min(THROTTLE_MAX_GAP, self.chan_gap[channel]))
        self.closed_until[channel] = time.time() + gap
        self.chan_gap[channel] = min(THROTTLE_MAX_GAP, int(gap * 1.6))
        log(f'[{channel}] 桶关窗，{gap}s 后重探')

    def _all_dead(self) -> bool:
        return all(self.fails[c] >= MAX_FAILURES for c in self.channels)

    def _progress(self):
        real = self.counter(self.aweme_id, retries=1) if self.counter is share_count \
            else self.counter(self.aweme_id)
        if real >= 0 and self.baseline >= 0:
            return max(real - self.baseline, 0), real
        return self.accepted, real

    def _reserve(self) -> bool:
        """预占一个名额，保证 accepted + inflight 不超过目标，避免多线程冲量超发。

        单视频每日上限在这里再挡一道：台账里的入账数 + 正在飞的数量到顶就收手。
        为什么挡两道：目标值只在开跑时算一次，多个订单进程同时刷同一条视频时，
        只有每次预占都回查台账，才不会被别的进程吃掉额度。
        """
        with self._lock:
            if self._stop.is_set():
                return False
            if self.accepted + self.inflight >= self.target:
                return False
            inflight_now = self.inflight
        if self.day_cap:
            room = U.video_room(self.aweme_id, self.day_cap)
            if room <= inflight_now:
                # 额度已经被在飞的请求占满。这里只拒绝新名额，不急着封顶：
                # 在飞的那几条有可能失败，失败后额度会还回来，提前封顶会把没跑满的视频推到明天。
                if U.day_cap_hit(self.aweme_id, self.day_cap):
                    with self._lock:
                        self.day_capped = True
                return False
        with self._lock:
            if self._stop.is_set():
                return False
            if self.accepted + self.inflight >= self.target:
                return False
            self.inflight += 1
            return True

    def report_once(self) -> bool:
        channel = self._pick()
        if channel is None:
            with self._lock:
                self.inflight = max(0, self.inflight - 1)
            return False
        with self._lock:
            self.attempts += 1
        try:
            impl = CHANNEL_IMPL.get(channel)
            if impl is None:
                ok, raw = False, f'unknown channel {channel}'
            else:
                ok, raw = impl(self, self.aweme_id)
        except Exception as exc:
            ok, raw = False, str(exc)
        # 池子空了不是失败，是「这条视频今天的设备已经用完」，单独记一笔好收工
        self.pool_empty[channel] = (not ok and isinstance(raw, str)
                                    and str(raw).endswith('pool empty'))
        closed_now = False
        with self._lock:
            self.inflight = max(0, self.inflight - 1)
            if ok:
                self.oks[channel] += 1
                self.accepted += 1
                self.fails[channel] = 0
                self.throttle_streak = 0
                self.chan_streak[channel] = 0
                # 桶又肯收就说明它重新开了，退避档位回到初始值
                self.chan_gap[channel] = THROTTLE_MIN_GAP
                self.closed_until[channel] = 0.0
            elif str(raw).startswith('throttled'):
                # 视频级限流不是设备或通道故障，不记 fails（记了这条通道 15 次后就永久退出，
                # 而桶其实是按小时回补的，永久退出等于自断一条腿）。只记本通道的连续空包，
                # 够次数就把这个通道关到 closed_until，到点自动重探。
                self.chan_streak[channel] += 1
                self.throttle_streak += 1
                self.throttled_attempts += 1
                if self.chan_streak[channel] >= THROTTLE_LIMIT:
                    closed_now = True
                    self.chan_streak[channel] = 0
            else:
                self.fails[channel] += 1
        if closed_now:
            self._close_channel(channel)
            if not self._any_open():
                self.throttled = True
                self.reason = ('全部通道桶已关：' +
                               ', '.join(f'{c} 最晚 {int(self._next_open_in())}s 后重探'
                                         for c in self.channels if self.fails[c] < MAX_FAILURES))
        if self.verbose and not ok and str(raw).startswith('throttled'):
            log(f'[{channel}] 空包 {self.chan_streak[channel]}/{THROTTLE_LIMIT}', str(raw)[:80])
        elif self.verbose and not ok and self.fails[channel] in (1, 5, MAX_FAILURES):
            log(f'[{channel}] 失败 {self.fails[channel]} 次', str(raw)[:120])
        return ok

    def _worker(self, index: int):
        while not self._stop.is_set():
            if not self._gate.is_set():
                # 计数停滞中：暂停发请求，等主线程确认落账再继续，别把设备白烧掉
                self._gate.wait(timeout=2)
                continue
            if not self._any_open():
                # 所有通道的桶都关着。max_wait=0 时直接退出线程（老行为，交给上层
                # daily1800 的窗口状态机去退避）；给了秒数就原地等最近的通道重探。
                wait = self._next_open_in()
                if self.max_wait <= 0 or wait < 0:
                    self._stop.set()
                    return
                time.sleep(min(max(wait, 1.0), 30.0))
                continue
            if not self._reserve():
                # 目标已占满或已中止
                if self.accepted >= self.target:
                    self._stop.set()
                return
            try:
                self.report_once()
            except Exception:
                pass
            if self.throttle_streak:
                time.sleep(random.uniform(*THROTTLE_BACKOFF))
            else:
                time.sleep(random.uniform(*SLEEP_RANGE))

    # ---------- 对外
    def _read_counter(self, retries: int = 3) -> int:
        try:
            if self.counter is share_count:
                return self.counter(self.aweme_id, retries=retries)
            return self.counter(self.aweme_id)
        except Exception:
            return -1

    def run(self) -> dict:
        start = time.time()
        # 单视频每日上限：开跑前先把目标压到今天的剩余额度，别让订单把 1200 冲过去
        if self.day_cap:
            room = U.video_room(self.aweme_id, self.day_cap)
            if room < self.target:
                log(f'视频 {self.aweme_id} 今日已入账 {self.day_cap - room} 条，'
                    f'本轮目标由 {self.target} 下调为 {room}')
                self.target = room
            if self.target <= 0:
                self.day_capped = True
                self.reason = f'今日已达单视频上限 {self.day_cap} 条'
                log(self.reason)
                return BoostResult(
                    aweme_id=self.aweme_id, target=0, baseline=-1, final=-1,
                    accepted=0, attempts=0, elapsed=round(time.time() - start, 2),
                    channels={}, throttled=False, day_capped=True,
                    day_cap=self.day_cap,
                    day_used=U.video_day_used(self.aweme_id),
                    reason=self.reason).as_dict()
        self.baseline = self._read_counter()
        if self.baseline < 0:
            log('计数接口不可用，进度按本地上报数计算')
        log(f'开始 | aweme_id={self.aweme_id} | 目标={self.target} | 起始={self.baseline} '
            f'| 线程={self.threads} | 设备池={len(self.device_pool)}/{len(self.info_pool)}')

        workers = [threading.Thread(target=self._worker, args=(i,), daemon=True,
                                    name=f'share-{i}') for i in range(self.threads)]
        for worker in workers:
            worker.start()

        last_report = 0.0
        last_real = -1
        last_real_ts = time.time()
        stall_since = 0.0
        while any(w.is_alive() for w in workers):
            time.sleep(1)
            if self._stop.is_set():
                break
            if self.timeout and time.time() - start > self.timeout:
                log('到达时间上限，收工')
                self._stop.set()
                break
            done, real = self._progress()
            if real >= 0:
                self.final = real
                if real != last_real:
                    last_real = real
                    last_real_ts = time.time()
                    stall_since = 0.0
                elif self.accepted - done >= STALL_TOLERANCE:
                    # 服务端收下却不计数：先暂停发请求，等计数跟上再继续。
                    # 直接收工会把本来能吃 1800 的视频当限流放弃；直接继续发则把设备白烧掉，
                    # 服务端对这些请求不会再计第二次，额度就这样被空转消耗掉。
                    if not stall_since:
                        stall_since = time.time()
                    elif time.time() - stall_since >= STALL_SECONDS:
                        moved = False
                        while not self._stop.is_set():
                            self._gate.clear()      # 暂停阀：所有工作线程原地等待
                            self.paused += 1
                            log(f'计数停滞 {int(time.time() - stall_since)}s，'
                                f'暂停 {STALL_PAUSE}s 等落账'
                                f'（第 {self.paused}/{STALL_PAUSE_ROUNDS} 轮）')
                            slept = 0.0
                            while slept < STALL_PAUSE and not self._stop.is_set():
                                chunk = min(5.0, STALL_PAUSE - slept)
                                time.sleep(chunk)
                                slept += chunk
                                if self.timeout and time.time() - start > self.timeout:
                                    break
                            if self._stop.is_set():
                                break
                            now_real = self._read_counter(1)
                            if now_real >= 0 and now_real > last_real:
                                moved = True
                                last_real = now_real
                                self.final = now_real
                                break
                            if self.paused >= STALL_PAUSE_ROUNDS:
                                break
                            if self.timeout and time.time() - start > self.timeout:
                                break
                        if moved:
                            self.paused = 0
                            stall_since = 0.0
                            last_real_ts = time.time()
                            self._gate.set()
                            log(f'计数已回填到 {self.final}，继续发')
                            continue
                        self.throttled = True
                        self.reason = (f'计数停滞：接受 {self.accepted} 条、实入 {done} 条，'
                                       f'暂停 {self.paused} 轮仍未回填')
                        log(self.reason)
                        self._stop.set()
                        break
            if done >= self.target:
                self._stop.set()
                break
            if self.day_capped:
                log('单视频每日上限已到，收工')
                self._stop.set()
                break
            if all(self.pool_empty.get(c, False) for c in self.channels):
                self.reason = '设备池已用尽：这条视频今天可用的设备为 0'
                log(self.reason)
                self._stop.set()
                break
            if self._all_dead():
                log('全部通道熔断，收工')
                self._stop.set()
                break
            # 全部通道都关窗时按 max_wait 计时；max_wait=0 表示不等，直接收工
            if not self._any_open():
                if self.max_wait <= 0:
                    self.reason = self.reason or '全部通道桶已关，本轮收工'
                    self._stop.set()
                    break
                if not self.all_closed_since:
                    self.all_closed_since = time.time()
                    log(f'全部通道关窗，先等桶回补（最多 {self.max_wait}s）')
                elif time.time() - self.all_closed_since >= self.max_wait:
                    self.reason = f'全部通道关窗超过 {self.max_wait}s，本轮收工'
                    log(self.reason)
                    self._stop.set()
                    break
            else:
                self.all_closed_since = 0.0
            eff_attempts = self.attempts - self.throttled_attempts
            if eff_attempts >= YIELD_WINDOW and self.accepted / max(1, eff_attempts) < MIN_YIELD:
                # 投产比只算真正打出去的请求：被桶挡回来的空包不算「视频吃不下量」，
                # 把它们算进分母会让本来只是关窗的视频被误判成废视频。
                self.throttled = True
                self.reason = (f'投产比过低 {self.accepted}/{eff_attempts}'
                               f'={self.accepted / max(1, eff_attempts):.0%}，判定视频限流')
                log(self.reason)
                self._stop.set()
                break
            if self.verbose and time.time() - last_report >= 15:
                last_report = time.time()
                log(f'进度 {done}/{self.target} | 上报成功 {self.accepted} | '
                    f'尝试 {self.attempts} | 真实 {real}')

        self._stop.set()
        for worker in workers:
            worker.join(timeout=3)

        # 收工复核：只有台账里的实际入账到顶才算「今日封顶」。
        # 为什么不在 _reserve 里直接定论：预留出去的名额可能整批失败，提前封顶会把
        # 没跑满 1200 的视频推到明天，客户那边看就是「少刷了」。
        if self.day_cap and not self.day_capped and U.day_cap_hit(self.aweme_id, self.day_cap):
            self.day_capped = True
            if not self.reason:
                self.reason = f'今日已达单视频上限 {self.day_cap} 条'
            log(self.reason)

        # 计数有 30–90s 回填延迟，立刻读会读到旧值，导致进度回写成 0。
        # 这里等它落账，最多 SETTLE_SECONDS 秒，别把订单挂死。
        final = self.final
        deadline = time.time() + SETTLE_SECONDS
        while time.time() < deadline:
            time.sleep(SETTLE_POLL)
            now = self._read_counter(1)
            if now < 0:
                continue
            final = now
            if self.baseline >= 0 and now - self.baseline >= self.accepted:
                break
        if final >= 0:
            self.final = final

        # 台账校准：直接以真实 share_count 增量覆盖当日入账，不用「接受数 - 退回」。
        # 实测 787 条被收下、只有 221 条真正计数，accept 口径会虚高 3.5 倍，
        # 导致 room 算不准、订单提前判定完成。credited 才是唯一真值。
        if self.day_cap and self.final >= 0 and self.baseline >= 0:
            credited = max(0, self.final - self.baseline)
            done = U.sync_video_day(self.aweme_id, credited)
            if done or credited:
                log(f'台账校准：接受 {self.accepted} / 实入 {credited}，'
                    f'当日入账改为 {done} 条')
        result = BoostResult(
            aweme_id=self.aweme_id, target=self.target, baseline=self.baseline,
            final=self.final, accepted=self.accepted, attempts=self.attempts,
            elapsed=time.time() - start, channels=dict(self.oks),
            throttled=self.throttled, day_capped=self.day_capped,
            day_cap=self.day_cap,
            day_used=U.video_day_used(self.aweme_id) if self.day_cap else 0,
            reason=self.reason)
        log('结束', json.dumps(result.as_dict(), ensure_ascii=False))
        return result.as_dict()


def boost(aweme_id: str, target: int, threads: int = 20, timeout: float = None,
          channels=DEFAULT_CHANNELS, verbose: bool = True,
          device_pool=None, info_pool=None, day_cap: int = None,
          max_wait: int = 0) -> dict:
    return ShareBooster(aweme_id, target, threads=threads, timeout=timeout,
                        channels=channels, verbose=verbose,
                        device_pool=device_pool, info_pool=info_pool,
                        day_cap=day_cap, max_wait=max_wait).run()


if __name__ == '__main__':
    import sys
    raw = sys.argv[1] if len(sys.argv) > 1 else ''
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    threads = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    aweme = resolve_aweme_id(raw)
    if not aweme:
        log('解析不到 aweme_id', raw)
        sys.exit(2)
    print(boost(aweme, count, threads=threads))
