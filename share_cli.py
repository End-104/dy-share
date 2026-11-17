# coding: utf-8
"""share_cli.py —— 单条视频刷分享量的命令行入口

用法：
    python share_cli.py --target "https://v.douyin.com/xxxx/" --count 1000
    python share_cli.py --target 7426187539512345678 --count 1000 --threads 30
    python share_cli.py --target 7426187539512345678 --count 500 --channels 1128,6368
    python share_cli.py --target 7426187539512345678 --count 20 --dry-run

--dry-run 只跑一条，打印每个通道的原始返回，用来确认字段名与风控状态。
"""

import argparse
import json
import sys

import dy_share
from dy_share import (
    InfoPool, ShareBooster, boost, log, report_1128, report_32, report_6368,
    resolve_aweme_id, share_count,
)


def dry_run(aweme_id: str, field: str) -> int:
    dy_share.SHARE_FIELD = field
    log('dry-run | aweme_id', aweme_id, '| 字段', field)
    log('当前分享量', share_count(aweme_id))

    device_pool = InfoPool(dy_share.infos)
    for name, func in (('1128', lambda: report_1128(aweme_id, dy_share.DevicePool(dy_share.devices34))),
                       ('32', lambda: report_32(aweme_id, device_pool)),
                       ('6368', lambda: report_6368(aweme_id))):
        ok, raw = func()
        log(f'[{name}] ok={ok}', str(raw)[:300])

    log('5 秒后复查分享量', share_count(aweme_id))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='抖音视频分享量上报')
    parser.add_argument('--target', required=True, help='分享链接 / 短链 / 19 位 aweme_id')
    parser.add_argument('--count', type=int, default=100, help='目标分享量')
    parser.add_argument('--threads', type=int, default=20, help='并发线程数')
    parser.add_argument('--timeout', type=float, default=None, help='总时长上限（秒）')
    parser.add_argument('--channels', default='1128,32', help='启用的通道，逗号分隔')
    parser.add_argument('--field', default='share_delta', help='上报字段名，抓包确认后改这里')
    parser.add_argument('--noise', type=int, default=2, help='批次内噪声条目数')
    parser.add_argument('--play-delta', type=int, default=1, help='顺带增加的播放量，0 表示不带')
    parser.add_argument('--link-mode', default='off', choices=['off', 'soft', 'strict'],
                        help='分享链路策略，路径确认前保持 off')
    parser.add_argument('--day-cap', type=int, default=None,
                        help='单条视频每自然日上限，默认 1200；0 表示不限')
    parser.add_argument('--dry-run', action='store_true', help='只跑一条并打印原始返回')
    args = parser.parse_args()

    aweme_id = resolve_aweme_id(args.target)
    if not aweme_id:
        log('解析不到 aweme_id', args.target)
        return 2

    dy_share.SHARE_FIELD = args.field
    dy_share.NOISE_RATIO = max(0, args.noise)
    dy_share.TARGET_PLAY_DELTA = max(0, args.play_delta)
    dy_share.SHARE_LINK_MODE = args.link_mode

    if args.dry_run:
        return dry_run(aweme_id, args.field)

    channels = tuple(c.strip() for c in args.channels.split(',') if c.strip())
    result = boost(aweme_id, args.count, threads=args.threads,
                   timeout=args.timeout, channels=channels,
                   day_cap=args.day_cap)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
