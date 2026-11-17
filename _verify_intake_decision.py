# -*- coding: utf-8 -*-
"""只读校验：把守护进程的完成判定在真实平台数据上跑一遍，不写任何状态。"""
import sys

sys.path.insert(0, r'D:\dy_share')
import dy_share_intake as I


def main():
    for goods in I.GOODS:
        cfg = I.load_cfg(goods)
        app = I.Api(cfg)
        rows = I.sweep(app, '2') or []
        print('== goods {} 处理中 {} 张 =='.format(goods, len(rows)))
        for r in rows:
            sn = str(r.get('orderSN'))
            d = app.detail(sn) or {}
            number = int(d.get('number') or r.get('number') or 0)
            start = int(d.get('startNum') or r.get('startNum') or 0)
            cur = int(d.get('currentNum') or 0)
            remain = number - (cur - start)
            print('   {} 下单 {} 起量 {} 当前 {} 已交付 {} 缺 {} -> {}'.format(
                sn, number, start, cur, cur - start, remain,
                '判完成（写 4）' if (str(d.get('state')) == '2' and remain <= I.LOSE_NUM)
                else '继续刷'))
        one = I.sweep(app, '1') or []
        print('   已付款待接单: {}'.format([str(x.get('orderSN')) for x in one]))
        three = I.sweep(app, '3') or []
        print('   异常待回收: {}'.format([str(x.get('orderSN')) for x in three]))


main()
