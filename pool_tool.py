# -*- coding: utf-8 -*-
"""pool_tool.py —— 设备池扩容与体检

为什么单独一个工具：单视频单日 1800 条需要 1800 台「当天没在这条视频上用过的
设备」（每条请求只 +1，没有放大系数）。池子里现在 1688 + 188 = 1876 台，
刚好够一条视频、零余量。多视频或者失败重试就不够，必须先扩容再谈交付。

三条子命令：
    python pool_tool.py audit                         # 体检：规模、死号、今日消耗、产能表
    python pool_tool.py import devices.json [--probe]  # 导入：去重、校验字段、写 pool_extra.json
    python pool_tool.py import infos.txt --txt [--probe]
    python pool_tool.py verify 20                     # 抽 20 台外挂设备读 detail 验活

导入文件的两种格式：
    JSON：devices34 同结构的数组，每条至少要有
          device_id / cookie / device_token / install_id / cdid
    TXT ：每行一条 infos 原始串 `hash-1.0.1----device_id----install_id-xxx----接码url`

为什么要去重：同一台设备在池子里出现两次，第二轮派发会被台账挡掉，
等于凭空少一台，扩容做了个寂寞。这里按 device_id 全局去重（含 devices34）。
"""
from __future__ import annotations

import json
import os
import sys

import dy_share as D
import used_store as U

HERE = os.path.dirname(os.path.abspath(__file__))
EXTRA_JSON = os.path.join(HERE, D.POOL_EXTRA_JSON)
EXTRA_TXT = os.path.join(HERE, D.POOL_EXTRA_TXT)
NEED = ('device_id', 'cookie', 'device_token', 'install_id', 'cdid')


# ---------------------------------------------------------------- 体检
def audit():
    base_ids = {d.get('device_id') for d in D.devices34 if d.get('device_id')}
    info_ids = set()
    for raw in D.infos:
        parts = str(raw).split('----')
        if len(parts) > 1 and parts[1].strip():
            info_ids.add(parts[1].strip())
    extra = D.load_extra_devices()
    extra_txt = D.load_extra_infos()
    extra_txt_ids = set()
    for raw in extra_txt:
        parts = str(raw).split('----')
        if len(parts) > 1:
            extra_txt_ids.add(parts[1].strip())
    extra_ids = {d.get('device_id') for d in extra if d.get('device_id')}

    # 四个来源必须并集去重再报数：pool_extra.json 和 pool_extra.txt 装的是同一批设备
    # 的两种格式（实测 6253 台两边 id 完全相同），直接相加会把池子报成 14382 台，
    # 是真实台数的 1.77 倍，排产按这个数算就敢接 7 条视频的单。
    all_ids = base_ids | info_ids | extra_ids | extra_txt_ids
    total = len(all_ids)
    overlap = len(base_ids & info_ids)
    log = D.log
    log(f'内置 1128 设备 {len(base_ids)} 台')
    log(f'内置 32 通道设备 {len(info_ids)} 台（与 1128 重合 {overlap} 台）')
    log(f'外挂 1128 设备 {len(extra_ids)} 台（pool_extra.json）')
    log(f'外挂 32 通道 {len(extra_txt_ids)} 台（pool_extra.txt，'
        f'与外挂 1128 重合 {len(extra_ids & extra_txt_ids)} 台）')
    log(f'去重后合计可用 {total} 台')
    log(f'近 24h 判死 {U.dead_count()} 台，台账 {U.stats()}')

    print('\n单视频当日 1800 的原料账（2026-11-16 实测：桶容量约 400 条/视频/通道，'
          '池子规模不参与计数）：')
    for buckets in (1, 2, 3, 5):
        cap = buckets * 400
        verdict = '够' if cap >= 1800 else '不够'
        print(f'  {buckets} 只桶 × 400 = {cap:>5} 条 | 1800 需要 5 只桶 | {verdict}')

    print('结论口径：设备早就不是瓶颈（8129 台、且服务端不按设备去重，'
          '同一台连打 3 次 3 次全计入）；决定单日上限的是「活着的桶 × 每桶 400」。'
          '加桶只有两条已验证/待验证的路：加通道（aid）、加出口 IP，'
          '换 host / 换 channel 参数 / 换 cdid / 一条请求塞多条目都不分桶（已实测）。')


# ---------------------------------------------------------------- 导入
def _norm_device(d: dict) -> dict | None:
    if not isinstance(d, dict):
        return None
    if not all(str(d.get(k) or '').strip() for k in NEED):
        return None
    d.setdefault('channel', 'xiaomi_1128_64')
    d.setdefault('device_brand', 'xiaomi')
    d.setdefault('device_type', 'M2012K11AC')
    d.setdefault('os_api', 30)
    d.setdefault('os_version', '11')
    d.setdefault('resolution', '1080*2400')
    d.setdefault('dpi', 440)
    return d


def do_import(path: str, as_txt: bool = False, probe: bool = False):
    if not os.path.exists(path):
        print(f'文件不存在：{path}')
        return 2
    known = {d.get('device_id') for d in D.devices34 if d.get('device_id')}
    known |= {d.get('device_id') for d in D.load_extra_devices()}
    for raw in D.load_extra_infos():
        parts = str(raw).split('----')
        if len(parts) > 1:
            known.add(parts[1].strip())

    added, skipped = 0, 0
    if as_txt:
        lines = []
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.count('----') < 2:
                    skipped += 1
                    continue
                did = line.split('----')[1].strip()
                if not did or did in known:
                    skipped += 1
                    continue
                known.add(did)
                lines.append(line)
                added += 1
        old = []
        if os.path.exists(EXTRA_TXT):
            with open(EXTRA_TXT, encoding='utf-8') as f:
                old = [ln.strip() for ln in f if ln.strip()]
        with open(EXTRA_TXT, 'w', encoding='utf-8') as f:
            for line in old + lines:
                f.write(line + '\n')
        print(f'导入 32 通道设备 {added} 条（跳过重复/无效 {skipped} 条）→ {EXTRA_TXT}')
    else:
        with open(path, encoding='utf-8') as f:
            rows = json.load(f)
        if isinstance(rows, dict):
            rows = rows.get('devices') or rows.get('data') or []
        old = []
        if os.path.exists(EXTRA_JSON):
            try:
                with open(EXTRA_JSON, encoding='utf-8') as f:
                    old = json.load(f)
            except Exception:
                old = []
        out = list(old)
        for row in rows:
            d = _norm_device(row)
            if not d:
                skipped += 1
                continue
            if d['device_id'] in known:
                skipped += 1
                continue
            known.add(d['device_id'])
            out.append(d)
            added += 1
        with open(EXTRA_JSON, 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print(f'导入 1128 设备 {added} 条（跳过重复/字段不全 {skipped} 条）→ {EXTRA_JSON}')

    # 同样按 device_id 去重：外挂 json 与 txt 是同一批设备的两种格式，相加会翻倍
    ids = {d.get('device_id') for d in D.devices34 if d.get('device_id')}
    ids |= {str(x).split('----')[1].strip() for x in D.infos if str(x).count('----') > 1}
    ids |= {d.get('device_id') for d in D.load_extra_devices() if d.get('device_id')}
    ids |= {str(x).split('----')[1].strip() for x in D.load_extra_infos()
            if str(x).count('----') > 1}
    total = len(ids)
    print(f'当前合计 {total} 台（去重后）；单视频 1800 的原料覆盖率 {total / 1800:.2f}x')
    if probe:
        verify(10 if added < 10 else 10)
    return 0


# ---------------------------------------------------------------- 验活
def verify(sample: int = 20):
    pool = D.load_extra_devices()
    rows = pool[:sample] if pool else []
    if not rows:
        print('外挂 1128 池为空，跳过验活；32 通道设备无法单独验活（字段不全，跑不了签名链）')
        return 1
    target = os.environ.get('POOL_VERIFY_AWEME', '')
    if not target:
        vids = [r[0] for r in U.video_day_report()]
        target = vids[0] if vids else ''
    if not target:
        print('没有可用于验活的 aweme_id，先给 POOL_VERIFY_AWEME 指定一条在架视频')
        return 1
    alive = 0
    for d in rows:
        if D.read_statistics(d, target) is not None:
            alive += 1
    print(f'抽样 {len(rows)} 台，存活 {alive} 台（命中率 {alive / len(rows):.0%}）'
          f'，判活基准视频 {target}')
    return 0


def main(argv: list) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == 'audit':
        audit()
        return 0
    if cmd == 'import':
        if len(argv) < 3:
            print('用法：python pool_tool.py import <文件> [--txt] [--probe]')
            return 2
        return do_import(argv[2], '--txt' in argv, '--probe' in argv)
    if cmd == 'verify':
        n = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else 20
        return verify(n)
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
