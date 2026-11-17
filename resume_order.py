# -*- coding: utf-8 -*-
"""退单中(state=5)的单子怎么处置：放回处理中继续补齐，或者按已交付量部分退款。

用法（在 D:\\dy_share 下，cmd 窗口）：
    .venv312\\Scripts\\python.exe -X utf8 resume_order.py 3246292 resume
    .venv312\\Scripts\\python.exe -X utf8 resume_order.py 3246292 refund
    .venv312\\Scripts\\python.exe -X utf8 resume_order.py 3246292 refund 500   # 指定退款条数
    .venv312\\Scripts\\python.exe -X utf8 resume_order.py 3246292 show         # 只看状态

resume：把状态改回 2 处理中，备注写清楚，之后 12 个窗口会照常接管继续刷。
refund：默认退「下单量 - 已净增量」的缺量部分，改状态 7 完成退款。
"""
import sys

sys.path.insert(0, r'D:\dy_share')
sys.argv = ['resume_order.py', 'cfg_qw_dy_share_1594', '1', '24']

from function import FunctionApp  # noqa: E402

f = FunctionApp()


def detail(sn):
    d = f.get_order_detail(sn)
    if isinstance(d, list):
        d = d[0] if d else {}
    return d or {}


def main():
    if len(sys.argv) < 3:
        print('用法: resume_order.py <orderSN> resume|refund|show [退款条数]')
        return 2
    sn = int(sys.argv[1])
    action = sys.argv[2].lower()
    d = detail(sn)
    total = int(d.get('number') or 0)
    start = int(d.get('startNum') or 0)
    cur = int(d.get('currentNum') or 0)
    left = max(0, total - (cur - start))
    print(f'订单 {sn} | 状态 {d.get("state")} | 下单 {total} | 起始 {start} | 当前 {cur} '
          f'| 已净增 {cur - start} | 缺量 {left}')

    if action == 'show':
        return 0

    if action == 'resume':
        print('改回处理中 ->', f.update_order_state(sn, 2, '继续补齐剩余量，请勿退单'))
        print('两秒后复查 ->', detail(sn).get('state'))
        return 0

    if action == 'refund':
        num = int(sys.argv[3]) if len(sys.argv) > 3 else left
        print('退款条数', num)
        print('退款 ->', f.update_order_refund(sn, num, '部分退款（未交付部分）'))
        print('改状态7 ->', f.update_order_state(sn, 7, '订单退款'))
        print('两秒后复查 ->', detail(sn).get('state'))
        return 0

    print('未知动作', action)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
