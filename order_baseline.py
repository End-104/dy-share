# -*- coding: utf-8 -*-
"""订单基线台账：平台不下发 startNum 时，用本地首次见到的绝对分享量当基线。

为什么要这个文件：这个货号（1594 / 997）的三方接口对订单的 startNum 一律返回 0，
0 不是「视频起点是 0」，而是「平台没给基线」。而判满公式是
    净增 = 官方 share_count(绝对值) - startNum
startNum=0 时净增就等于视频历史总分享量：一条历史分享 500 的视频，下 100 条的
单在第一轮就满足 100 - (500 - 0) = -400 <= 5，直接被写 state=4（3252608 就是这么
在 40 秒内「完成」的，一条没发）。所以基线必须由我们自己在第一次接触这单时落盘，
之后无论平台给不给，都按本地基线算。

为什么一单一个文件、用 O_EXCL 建：12 个窗口并发处理同一单时，谁先把文件建出来
谁就是真起点，后到的读到已存在的记录直接用，不加锁也不会互相覆盖。
"""
import json
import os
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(BASE, 'baseline')


def _path(order_sn):
    return os.path.join(DIR, 'b_{}.json'.format(str(order_sn)))


def get(order_sn):
    """返回本地基线条数；没有记录返回 0。"""
    try:
        with open(_path(order_sn), 'r', encoding='utf-8') as fh:
            return int((json.load(fh) or {}).get('base') or 0)
    except Exception:
        return 0


def put(order_sn, aweme_id, base):
    """落一条基线，先写者胜，返回最终生效值。"""
    base = int(base or 0)
    if base <= 0:
        return get(order_sn)
    try:
        os.makedirs(DIR, exist_ok=True)
    except Exception:
        return get(order_sn)
    rec = {'order_sn': str(order_sn), 'aweme_id': str(aweme_id),
           'base': base, 'ts': int(time.time())}
    try:
        fd = os.open(_path(order_sn), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return get(order_sn)
    except Exception:
        return get(order_sn)
    try:
        os.write(fd, json.dumps(rec, ensure_ascii=False).encode('utf-8'))
    finally:
        os.close(fd)
    return get(order_sn) or base


def ensure(order_sn, aweme_id='', start_num=0):
    """平台给了有效 startNum 就按它落盘；给 0 返回本地基线（没有则 0，由调用方补读）。"""
    base = get(order_sn)
    if base > 0:
        return base
    if int(start_num or 0) > 0:
        return put(order_sn, aweme_id, start_num)
    return 0


def reset(order_sn):
    """人工重开一单时清掉旧基线，让下一次接触重新取快照。"""
    try:
        os.remove(_path(order_sn))
        return True
    except Exception:
        return False


def force(order_sn, aweme_id, base):
    """覆盖式落基线，返回最终值。

    为什么不能复用 put：put 是 O_EXCL 先写者胜，而换手时起点必须被改写。
    唯一需要改写的场景是「同一视频从上一单转给本单」——上一单刷出来的量不属于
    本单，本单如果沿用接单时那个基线，就会把别人的量算成自己的净增。
    """
    base = int(base or 0)
    if base <= 0:
        return get(order_sn)
    try:
        os.makedirs(DIR, exist_ok=True)
    except Exception:
        return get(order_sn)
    rec = {'order_sn': str(order_sn), 'aweme_id': str(aweme_id),
           'base': base, 'ts': int(time.time()), 'taken_over': True}
    tmp = _path(order_sn) + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(rec, fh, ensure_ascii=False)
        os.replace(tmp, _path(order_sn))
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return get(order_sn)
    return get(order_sn) or base


def _vpath(aweme_id):
    return os.path.join(DIR, 'v_{}.json'.format(str(aweme_id)))


def video_owner(aweme_id):
    """返回 (归属订单号, 上次活跃时间)；没人认领返回 ('', 0)。"""
    try:
        with open(_vpath(aweme_id), 'r', encoding='utf-8') as fh:
            rec = json.load(fh) or {}
        return str(rec.get('owner') or ''), float(rec.get('ts') or 0)
    except Exception:
        return '', 0.0


def _video_write(aweme_id, order_sn):
    rec = {'owner': str(order_sn), 'aweme_id': str(aweme_id), 'ts': int(time.time())}
    tmp = _vpath(aweme_id) + '.tmp'
    try:
        os.makedirs(DIR, exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(rec, fh, ensure_ascii=False)
        os.replace(tmp, _vpath(aweme_id))
        return True
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False


def video_lock(aweme_id, order_sn, ttl=3600.0):
    """独占一条视频的刷量权，返回 (拿到没, 上一个归属方)。

    为什么按视频独占：判满用的是视频的绝对分享量（净增 = share_count - 基线），
    两条单同时刷同一条视频时，A 刷出来的增量会被 B 算进自己的净增，B 一条不发
    也能判完成 —— 3253143 与 3253177 就是这么同时对着 7687127311098894452 跑，
    视频从 698 涨到 876（两单各 100），两单都按「净增 178 >= 100」写了完成。
    独占之后同一时刻只有一单能吃这条视频的增量，换手时用 force() 重设基线。
    """
    owner, ts = video_owner(aweme_id)
    now = time.time()
    if not owner:
        _video_write(aweme_id, order_sn)
        return True, ''
    if owner == str(order_sn):
        _video_write(aweme_id, order_sn)          # 续租，别让长单被 TTL 判死
        return True, ''
    if now - ts > float(ttl):
        _video_write(aweme_id, order_sn)          # 上一单早就没动静了，收回
        return True, owner
    return False, owner


def video_release(aweme_id, order_sn):
    """本单收口后立刻让位：不放的话同视频的下一单要干等 TTL。"""
    owner, _ = video_owner(aweme_id)
    if owner and owner == str(order_sn):
        try:
            os.remove(_vpath(aweme_id))
            return True
        except Exception:
            return False
    return False
