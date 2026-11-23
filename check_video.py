# -*- coding: utf-8 -*-
"""视频核对工具：抖音真实分享量 vs 平台订单进度。

用法：
    python check_video.py https://v.douyin.com/ZxCjQC9lkZY/
    python check_video.py 7709349999184579273

输出三段：
    1 抖音侧   statistics.share_count（真实绝对值）、play/digg、可见性
    2 平台侧   该视频在 1594 / 997 两个货号下的所有订单（startNum / currentNum / 交付）
    3 对账     实差 = share_count - startNum，与平台记的 delivered 比

只读，不发任何写请求。
"""
import json
import sys
import time

sys.path.insert(0, r'D:\share\dy-share')

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

import dy_share as D
from function import FunctionApp, http_client

GOODS = ('1594', '997')
STATES = ('1', '2', '3', '4', '5', '6', '7')
MAX_PAGE = 20


def make_app():
    if len(sys.argv) > 1 and not sys.argv[1].startswith('http') \
            and not sys.argv[1].isdigit():
        cfg = sys.argv[1]
    else:
        cfg = 'cfg_qw_dy_share_1594'
    saved = sys.argv[:]
    sys.argv = ['check_video', cfg, '1', '100']
    try:
        return FunctionApp()
    finally:
        sys.argv = saved


def raw_detail(aweme_id, pool):
    dev = pool.get()
    ticket = int(time.time() * 1000)
    ts = int(time.time())
    url = (f'{D.DETAIL_HOST}?aweme_id={aweme_id}&_rticket={ticket}'
           f'&ts={ts}&need_personal_recommend=1&')
    common = D.APP_COMMON.format(
        cdid=dev['cdid'], brand=dev['device_brand'], device_id=dev['device_id'],
        model=dev['device_type'], dpi=dev['dpi'], install_id=dev['install_id'],
        os_api=dev['os_api'], os_version=dev['os_version'], resolution=dev['resolution'])
    headers = {
        'User-Agent': D.UA_APP_1128.format(os_version=dev['os_version'],
                                           model=dev['device_type']),
        'x-tt-dt': dev['device_token'],
        'x-tt-request-tag': 's=0;p=0',
        'x-ss-dp': '1128',
        'sdk-version': '2',
        'passport-sdk-version': '20374',
        'x-vc-bdturing-sdk-version': '3.1.0.cn',
        'Cookie': dev['cookie'],
        'x-common-params-v2': common,
    }
    headers.update(D.sign_2290(params=url + common, payload='', cookie=headers['Cookie']))
    res = D.http_client.get(url, headers=headers, timeout=D.TIME_WAIT)
    try:
        return (res.json().get('aweme_detail') or {})
    except Exception:
        return {}


def fetch(app, goods, state, page):
    url = ('/api/supplier/order/v2/orders?page={}&goodsSN={}&orderSN=&state={}'
           '&limit=100').format(page, goods, state)
    try:
        r = http_client.post(app.api_url + url,
                             headers=app.get_headers(url, False), timeout=30)
        body = r.json() or {}
    except Exception as exc:
        print('  ! 订单列表读失败 goods={} state={} p{}: {}'.format(
            goods, state, page, exc))
        return []
    return ((body.get('result') or {}).get('data')) or []


def order_link(o):
    for p in (o.get('params') or []):
        v = str(p.get('value') or '').strip()
        if 'douyin.com' in v or v.isdigit():
            return v
    return ''


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    arg = sys.argv[1].strip()

    # ---------- 1 抖音侧 ----------
    aweme = D.resolve_aweme_id(arg) if not arg.isdigit() else arg
    if not aweme:
        print('解析失败：{}'.format(arg))
        return 1
    pool = D.DevicePool(D.devices34 + D.load_extra_devices())
    d1 = raw_detail(aweme, pool)
    s1 = d1.get('statistics') or {}
    st = d1.get('status') or {}
    print('===== 1 抖音侧 =====')
    print('输入      : {}'.format(arg))
    print('aweme_id  : {}'.format(aweme))
    print('分享量    : {}'.format(s1.get('share_count')))
    print('点赞/评论 : {} / {}'.format(s1.get('digg_count'), s1.get('comment_count')))
    print('播放/曝光 : {} / {}  (接口侧长期恒 0，不能当判据)'.format(
        s1.get('play_count'), s1.get('exposure_count')))
    print('可见性    : private={} delete={} prohibited={} allow_share={} review={}'.format(
        st.get('private_status'), st.get('is_delete'), st.get('is_prohibited'),
        st.get('allow_share'), (st.get('review_result') or {}).get('review_status')))
    print('作者      : {}'.format((d1.get('author') or {}).get('nickname')))
    print('发布戳    : {}'.format(d1.get('create_time')))

    print('\n再读一次（间隔 30s，判断计数是否冻结）')
    time.sleep(30)
    d2 = raw_detail(aweme, pool)
    s2 = d2.get('statistics') or {}
    sc1, sc2 = s1.get('share_count'), s2.get('share_count')
    print('  t0={}  t30={}  -> {}'.format(
        sc1, sc2, '未变化（这 30 秒内没有新分享被计入）' if sc1 == sc2 else '有变化'))

    # ---------- 2 平台侧 ----------
    app = make_app()
    print('\n===== 2 平台侧 =====')
    print('api={}  goods={}'.format(app.api_url, GOODS))
    hits = []
    total = 0
    for goods in GOODS:
        for state in STATES:
            for page in range(1, MAX_PAGE + 1):
                data = fetch(app, goods, state, page)
                total += len(data)
                for o in data:
                    blob = json.dumps(o, ensure_ascii=False)
                    if aweme in blob or arg.rstrip('/').split('/')[-1] in blob:
                        hits.append((goods, state, o))
                if len(data) < 100:
                    break
    print('扫描订单 {} 条，命中 {} 条'.format(total, len(hits)))
    if not hits:
        print('  → 该视频在本平台没有任何订单记录。平台显示 0 = 从来没为它上报过数据，')
        print('    与抖音上的分享量无关。')
    for goods, state, o in hits:
        number = int(o.get('number') or 0)
        start = int(o.get('startNum') or 0)
        current = int(o.get('currentNum') or 0)
        delivered = current - start
        print('  goods={} state={} sn={}'.format(goods, state, o.get('orderSN')))
        print('    下单={} 平台start={} 平台current={} 平台记交付={} 退款条数={}'.format(
            number, start, current, delivered, o.get('refundNumber')))
        print('    链接={}'.format(order_link(o)))
        if sc1 is not None:
            top = int(sc1)
            real = top - start
            flag = []
            if delivered == 0 and real > 0:
                flag.append('平台显示 0 但实际有净增 {} 条 → 进度没回写'.format(real))
            if abs(real - delivered) > 5:
                flag.append('平台交付 {} vs 实差 {} 差 {} 条'.format(
                    delivered, real, real - delivered))
            if top == start:
                flag.append('share_count 与 startNum 相等 → 基线落在刷完之后')
            print('    抖音share={} 实差={} {}'.format(top, real, ' | '.join(flag) or '一致'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
