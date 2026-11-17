# -*- coding: utf-8 -*-
"""订单基线台账：平台不下发 startNum 时，用本地首次见到的绝对分享量当基线。

为什么要这个文件：这个货号（1763 / 643）的三方接口对订单的 startNum 一律返回 0，
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
