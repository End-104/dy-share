"""三单账本重置 v3：基线取视频首次接手时的绝对分享量，剩下的按 12 小时曲线重排。

前两轮踩到的两个坑（都已在代码里修掉）：
1. function.api_ok 拿 res.json() 当 dict 用，而三方接口 execState/update 返回裸标量
   "2"/"0"，于是一直判失败，抢锁永远过不去 → 三单看着「没反应」。已改 api_ok。
2. pace_state.json 被 12 个窗口并发读改写：427 条净增被记了 5 次（delivered 冲到
   2135），P.done() 立刻判完成，两单在 671/1200、966/1000 就被置 state=4。
   pace_ledger 现在有跨进程锁 + 幂等 P.align()，账本这里手动重建一次干净记录。

基线（视频首次接手时的绝对量）：
  3246292 → 671，3246294 → 966，3246295 → 4（这条是新视频，起点 4）
欠量 = 下单量 - (当前绝对量 - 基线)
"""
import sys
import time

import dy_share as D
import used_store as U
import pace_ledger as P
from function import FunctionApp

# order_sn, aweme_id, 下单量, 基线
FIX = [
    ('3246292', '7684211497166263284', 1200, 671),
    ('3246294', '7681210369646798440', 1000, 966),
    ('3246295', '7680824934961102498', 800, 4),
]

sys.argv = ['_repair_orders.py', 'cfg_qw_dy_share_1763', '1', '24']
fn = FunctionApp()

for sn, aweme_id, total, base in FIX:
    cur = D.share_count(aweme_id)
    if cur < 0:
        print(f'{sn} 分享量读取失败，跳过')
        continue
    net = max(0, cur - base)
    left = max(0, total - net)
    now = time.time()

    # 当日计入归零：先前被 sync_video_day 累加到 3056/6150，超过 1800 上限
    U.reset_video_day(aweme_id)

    rec = {
        'order_sn': sn, 'total': total, 'start_num': base,
        'start_ts': now, 'duration': 43200.0, 'shape': 'linear',
        'head_pct': 0.35, 'head_min': 30.0, 'head_end': now + 1800.0,
        'delivered': net, 'day_log': {P._today(): net}, 'hour_log': {},
        'per_day': 0, 'hourly_tol': 1.3, 'lag': 0, 'ticks': 0,
        'created': time.strftime('%Y-%m-%d %H:%M:%S'),
        'seg_start': now, 'seg_base': net, 'seg_total': total, 'seg_dur': 43200.0,
        'head_end_reason': '', 'head_pause': 0.0, 'head_retries': 0, 'last_ts': now,
    }
    with P._lock():
        data = P._load()
        data[sn] = rec
        P._save(data)

    msg1 = fn.update_order_progress(sn, base, cur)
    msg2 = fn.update_order_state(sn, 2, '账本校正后重新放行')
    d = fn.get_order_detail(sn)
    print(f'{sn} 当前={cur} 基线={base} 已交付={net} 欠量={left} '
          f'→ state={d.get("state")} startNum={d.get("startNum")} '
          f'currentNum={d.get("currentNum")} 应达={P.due(P.get(sn))}')
    print(f'   progress={msg1} state={msg2}')
    time.sleep(0.3)
