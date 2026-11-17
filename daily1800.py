# coding: utf-8
"""daily1800.py —— 单视频「当日 1800 条」多通道窗口调度器

为什么按通道分开调度：服务端对分享计数的限流是「通道 × 视频」粒度的桶。
2026-11-15 22:53 实测：同一条视频（7706079479723465882），1128 通道对每台新设备
都回空包、真实计数不动，而同一时刻打 aid=32 通道，8 条里进 1 条、计数 +1。
所以「一个通道关窗」不等于「这条视频不能再进量」，把两个通道的退避混在一起算，
会把还开着的那个通道一起停掉。

调度模型（每个通道一套状态机）：
    通道 C 到点了 -> 用 2~4 台该通道的新鲜设备探一次
        计数动了 -> 该通道窗口开着 -> 立刻全力吃 chunk 条 -> 下一轮马上再探
        计数不动 -> 该通道窗口关了 -> 该通道退避 gap 起，指数涨到 gap_max
    两个通道互不阻塞，谁开着吃谁，都关着就等最早到点的那个。

为什么探针要用「该通道自己的新设备」：探针只看窗口开没开，用过的设备会拿到
device pool empty，那是设备耗尽，不是窗口关闭，混在一起会误判退避。

用法：
    python daily1800.py <链接或 19 位 aweme_id>                       # 目标 1800
    python daily1800.py <id> 1800 --threads 24 --chunk 180
    python daily1800.py <id> 1800 --gap 900 --gap-max 3600
    python daily1800.py <id> 1800 --stop-at 23:40
    python daily1800.py <id> 1800 --channels 1128,32                 # 指定通道
    python daily1800.py <id> 1800 --probe-only                       # 只探窗口

输出：屏幕每行一轮；明细追加 daily_run.jsonl；状态写 daily_state.json
（同一天重跑接着上次进度，不从头算）。
"""
from __future__ import annotations

import json
import os
import sys
import time

import dy_share as D
import used_store as U

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, 'daily_state.json')
LOG_FILE = os.path.join(BASE, 'daily_run.jsonl')
CHANNELS = ('1128', '32')


# ---------------------------------------------------------------- 小工具
def stamp() -> str:
    return time.strftime('%H:%M:%S')


def log(*parts):
    print(f'{stamp()} | ' + ' | '.join(str(p) for p in parts), flush=True)


def append_log(rec: dict):
    rec = dict(rec, ts=time.strftime('%Y-%m-%d %H:%M:%S'))
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(data: dict):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def stop_epoch(stop_at: str) -> float:
    """把 --stop-at HH:MM 换成本地时间戳；已经过了就表示今天不跑了。"""
    hh, mm = (int(x) for x in stop_at.split(':'))
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))


def free_room(aweme_id: str, day_cap: int) -> int:
    return max(0, day_cap - U.video_day_used(aweme_id)) if day_cap else 10 ** 9


# ---------------------------------------------------------------- 就地补料
def top_up(rec: dict, need: int, opts: dict) -> int:
    """池子被今天的尝试烧光后就地注册新身份，返回真正落盘的台数。

    为什么必须就地补而不是提前备足：一条视频一天要 6429 次尝试（1800 ÷ 0.28 实测
    计入率），池子按最大需求常备就是每天白养一万条死身份；而注册是 10 台/秒的活，
    边烧边补在时间上完全来得及。补料按「还差多少入账 ×4」估算，不多注册。
    """
    used = int(rec.get('minted') or 0)
    budget = max(0, int(opts['mint_max']) - used)
    n = min(int(need), int(opts['mint_batch']), budget)
    if n <= 0:
        log(f'今日补料额度已用尽（已注册 {used} 台 / 上限 {opts["mint_max"]}）')
        return 0
    import device_mint as M
    log(f'池子烧空，就地注册 {n} 台新身份（今日第 {used + 1}~{used + n} 台）')
    try:
        out = M.mint_batch(n, threads=int(opts['mint_threads']),
                           sleep=float(opts['mint_pace']), write_infos=True,
                           verbose=False, pause_base=float(opts['mint_pause']),
                           max_streak=int(opts['mint_streak']))
    except Exception as exc:            # 注册链路故障不能把调度器带死
        log(f'注册失败：{exc!r}')
        rec['mint_cool'] = time.time() + 300
        return 0
    if out.get('aborted'):
        # device_id:0 说明注册配额窗关了（23:05 实测：12 线程冲太快就会被关），
        # 这时候不能挂到收工，等 15 分钟重试，中间让别的通道继续吃。
        rec['mint_cool'] = time.time() + 900
        log(f'注册被限速（连续失败），冷却到 '
            f'{time.strftime("%H:%M:%S", time.localtime(rec["mint_cool"]))} 再补')
    rec['minted'] = used + int(out.get('saved') or 0)
    log(f'补料完成 落盘 {out["saved"]} 台设备 / {out["saved_infos"]} 条 info 行，'
        f'成功率 {out.get("rate")}，用时 {out["sec"]}s')
    append_log({'event': 'mint', 'minted': out.get('saved'),
                'infos': out.get('saved_infos'), 'sec': out.get('sec'),
                'rate': out.get('rate'), 'aborted': out.get('aborted'),
                'total_today': rec['minted']})
    return int(out.get('saved') or 0)


# ---------------------------------------------------------------- 设备素材
def candidate_devices(aweme_id: str, prefer_minted: bool = True) -> list:
    """选出本次可用的 1128 设备清单。

    优先只用现场注册的新身份。理由（2026-11-15 23:33 实测，同一条视频同一时段）：
        原始池 1688 台里挑 25 台没试过的 → 25 次上报 10 条非空回执，真实计入 **0**
        现场注册的 25 台           → 25 次上报 11 条非空回执，真实计入 **11**
    老池子的设备在这一天里已经被反复标记过，服务端认回执但不计账；新身份计账率 44%。
    所以补料不是"够用就行"，而是主力——有新材料就先用新材料，没有才退回老池。
    """
    dead = U.dead_ids()
    minted, orig = [], []
    for d in D.load_extra_devices():
        did = str(d.get('device_id') or '')
        if did and did not in dead and not U.tried(did, aweme_id):
            minted.append(d)
    if prefer_minted and minted:
        return minted
    for d in D.devices34:
        did = str(d.get('device_id') or '')
        if did and did not in dead and not U.tried(did, aweme_id):
            orig.append(d)
    return minted + orig


def candidate_infos(aweme_id: str, prefer_minted: bool = True) -> list:
    """32 通道素材，优先新写的 info 行（理由同上）。"""
    dead = U.dead_ids()
    minted, orig = [], []
    for raw in D.load_extra_infos():
        try:
            did = str(D.InfoPool._parse(raw)['device_id'])
        except Exception:
            continue
        if did not in dead and not U.tried(did, aweme_id):
            minted.append(raw)
    if prefer_minted and minted:
        return minted
    for raw in D.infos:
        try:
            did = str(D.InfoPool._parse(raw)['device_id'])
        except Exception:
            continue
        if did not in dead and not U.tried(did, aweme_id):
            orig.append(raw)
    return minted + orig


def pick_probe_devices(aweme_id: str, n: int) -> list:
    """1128 通道的探针设备：今天没在这条视频上试过、且不在全局死号表里的。"""
    return candidate_devices(aweme_id)[:n]


def pick_probe_infos(aweme_id: str, n: int) -> list:
    """32 通道的探针素材，返回 infos 的原始行（report_32 吃原始行）。"""
    return candidate_infos(aweme_id)[:n]


# ---------------------------------------------------------------- 单通道探测
def probe(aweme_id: str, channel: str, n: int, settle: int = 45):
    """用一个通道探窗口，返回 (是否开窗, 真实计入条数, 本机接受条数, 说明)。

    判据是真实 share_count 增量，不是 HTTP 回执：服务端在关窗期照样回
    status_code:0 / success_item_ids，只有计数会不动。
    """
    before = D.share_count(aweme_id, retries=2)
    accepted = 0
    note = ''
    if channel == '1128':
        probes = pick_probe_devices(aweme_id, n)
        if not probes:
            return False, 0, 0, 'device pool empty'
        pool = D.DevicePool(probes, cooldown=0)
        for _ in probes:
            ok, raw = D.report_1128(aweme_id, pool, D.SHARE_FIELD, day_cap=U.DAY_CAP)
            accepted += 1 if ok else 0
            note = str(raw)[:40]
            time.sleep(1.5)
    elif channel == '32':
        probes = pick_probe_infos(aweme_id, n)
        if not probes:
            return False, 0, 0, 'info pool empty'
        pool = D.InfoPool(probes, cooldown=0)
        for _ in probes:
            ok, raw = D.report_32(aweme_id, pool, D.SHARE_FIELD, day_cap=U.DAY_CAP)
            accepted += 1 if ok else 0
            note = str(raw)[:40]
            time.sleep(1.5)
    else:
        return False, 0, 0, f'unknown channel {channel}'
    time.sleep(settle)
    after = D.share_count(aweme_id, retries=2)
    delta = (after - before) if (before >= 0 and after >= 0) else -1
    return (delta > 0), max(0, delta), accepted, note


# ---------------------------------------------------------------- 开窗内吃量
def burst(aweme_id: str, channel: str, chunk: int, threads: int, day_cap: int,
          prefer_minted: bool = True) -> dict:
    """在指定通道上全力吃一轮。

    为什么一轮只吃 chunk（默认 180）条：单个窗口实测只认 220 上下，
    塞 400 条里后 180 条必然「收下不计数」，等于白烧设备。
    180 卡在窗口容量以内，剩下的交给下一个窗口。
    """
    device_pool = D.DevicePool(candidate_devices(aweme_id, prefer_minted), cooldown=0)
    info_pool = D.InfoPool(candidate_infos(aweme_id, prefer_minted), cooldown=0)
    device_pool.gap = 0
    info_pool.gap = 0
    return D.ShareBooster(
        aweme_id, chunk, threads=threads, timeout=60 * 15,
        channels=(channel,), verbose=True, field=D.SHARE_FIELD,
        counter=D.share_count, day_cap=day_cap,
        device_pool=device_pool, info_pool=info_pool).run()


# ---------------------------------------------------------------- 主循环
def main(argv: list) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    raw = argv[1]
    target = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else 1800
    opts = {
        'threads': 24,
        'chunk': D.WINDOW_CHUNK,
        'gap': D.WINDOW_GAP,
        'gap_max': D.WINDOW_GAP_MAX,
        'stop_at': '23:40',
        'probe_only': '--probe-only' in argv,
        'probe_n': D.WINDOW_PROBE_N,
        'day_cap': U.DAY_CAP,
        'channels': CHANNELS,
        'mint_batch': 400,
        'mint_max': 12000,
        'mint_threads': 2,
        'mint_pace': 1.0,
        'mint_pause': 5.0,
        'mint_streak': 8,
        'prefer_minted': '--allow-old' not in argv,
        'auto_mint': '--no-mint' not in argv,
    }
    flags = {'--threads': 'threads', '--chunk': 'chunk', '--gap': 'gap',
             '--gap-max': 'gap_max', '--probing': 'probe_n', '--cap': 'day_cap',
             '--mint-batch': 'mint_batch', '--mint-max': 'mint_max',
             '--mint-threads': 'mint_threads'}
    for i, a in enumerate(argv):
        if a in flags and i + 1 < len(argv):
            opts[flags[a]] = int(argv[i + 1])
        if a == '--stop-at' and i + 1 < len(argv):
            opts['stop_at'] = argv[i + 1]
        if a == '--channels' and i + 1 < len(argv):
            opts['channels'] = tuple(x.strip() for x in argv[i + 1].split(',') if x.strip())

    aweme_id = D.resolve_aweme_id(raw)
    if not aweme_id or len(aweme_id) != 19:
        log('解析不到 19 位 aweme_id', raw)
        return 2

    day = U.today()
    state = load_state()
    rec = state.get(aweme_id) or {}
    if rec.get('day') != day:
        rec = {
            'day': day, 'baseline': D.share_count(aweme_id, retries=2), 'started': stamp(),
            'credited': 0, 'rounds': 0, 'minted': 0,
            'chan': {c: {'windows': 0, 'accepted': 0, 'closed': 0, 'gap': opts['gap'],
                         'next': 0.0} for c in opts['channels']},
        }
        state[aweme_id] = rec
        save_state(state)
    for c in opts['channels']:      # 老状态文件补字段
        rec.setdefault('chan', {}).setdefault(
            c, {'windows': 0, 'accepted': 0, 'closed': 0, 'gap': opts['gap'], 'next': 0.0})
    rec.setdefault('minted', 0)

    base = rec['baseline']
    real = D.share_count(aweme_id, retries=2)
    credited = U.video_day_used(aweme_id, day)
    if real >= 0 and base >= 0 and real - base > credited:
        credited = real - base
        U.sync_video_day(aweme_id, credited, day)

    dev_total = len(D.devices34) + len(D.load_extra_devices())
    info_total = len(D.infos) + len(D.load_extra_infos())
    log(f'目标 {target} | aweme={aweme_id} | 基线 {base} | 当前 {real} | '
        f'今日已入账 {credited} | 线程 {opts["threads"]} | 切片 {opts["chunk"]} | '
        f'通道 {"/".join(opts["channels"])} | 池 {dev_total}+{info_total}')
    append_log({'event': 'start', 'aweme_id': aweme_id, 'target': target,
                'baseline': base, 'credited': credited, 'opts':
                {k: (list(v) if isinstance(v, tuple) else v) for k, v in opts.items()}})

    deadline = stop_epoch(opts['stop_at'])
    if deadline < time.time():
        log(f'今天已过收工时间 {opts["stop_at"]}，明天零点后再跑')

    while time.time() < deadline and credited < target:
        chan = rec['chan']
        now = time.time()
        due = [c for c in opts['channels'] if chan[c]['next'] <= now]
        if not due:
            # 所有通道都在退避：睡到最早到点的那个，中途也看一眼收工时间
            wake = min(chan[c]['next'] for c in opts['channels'])
            sleepy = max(1.0, min(wake - now, max(0.0, deadline - now)))
            time.sleep(min(sleepy, 300.0))
            continue

        for c in due:
            if time.time() >= deadline or credited >= target:
                break
            if free_room(aweme_id, opts['day_cap']) <= 0:
                log('当日入账额度已满，收工')
                credited = target
                break
            st = chan[c]
            alive, delta, acc, note = probe(aweme_id, c, opts['probe_n'])
            append_log({'event': 'probe', 'channel': c, 'alive': alive,
                        'delta': delta, 'accepted': acc, 'note': note})
            if not alive:
                st['closed'] += 1
                if 'pool empty' in note:
                    # 素材用尽 ≠ 窗口关闭。这时唯一的正确动作是补料：注册 10 台/秒，
                    # 补完立刻再探，别把这条通道挂到收工。补料配额用完才真挂起。
                    got = 0
                    if opts['auto_mint']:
                        cooled = float(rec.get('mint_cool') or 0) > time.time()
                        if cooled:
                            st['next'] = float(rec['mint_cool'])
                            log(f'[{c}] 池空但注册在冷却，'
                                f'{st["next"] - time.time():.0f}s 后再补')
                        else:
                            need = int(max(target - credited, 1) * 4.0) + 200
                            got = top_up(rec, need, opts)
                            st['next'] = (float(rec.get('mint_cool') or 0)
                                          if not got else time.time() + 5)
                        state[aweme_id] = rec
                        save_state(state)
                        if got:
                            log(f'[{c}] 池子已补 {got} 台，5s 后重探该通道')
                            continue
                        if st['next'] > time.time():
                            continue
                    st['exhausted'] = True
                    st['next'] = deadline
                    log(f'[{c}] 该通道今日素材已用尽（{note}），不再探测')
                    state[aweme_id] = rec
                    save_state(state)
                    continue
                st['gap'] = min(int(max(st['gap'], opts['gap']) * 1.6), opts['gap_max'])
                st['next'] = time.time() + st['gap']
                log(f'[{c}] 窗口关闭（探针 {acc} 条，计数 {delta:+d}），'
                    f'该通道退避 {st["gap"]}s')
                rec['rounds'] += 1
                state[aweme_id] = rec
                save_state(state)
                continue

            st['windows'] += 1
            st['gap'] = opts['gap']
            chunk = max(20, min(target - credited, opts['chunk']))
            log(f'[{c}] 窗口开着（探针计入 {delta} 条），本轮吃 {chunk} 条')
            before = D.share_count(aweme_id, retries=2)
            res = burst(aweme_id, c, chunk, opts['threads'], opts['day_cap'],
                        opts.get('prefer_minted', True))
            time.sleep(20)      # 等落账，计数是异步回填的
            after = D.share_count(aweme_id, retries=2)
            gained = (after - before) if (before >= 0 and after >= 0) else res['accepted']
            credited = U.video_day_used(aweme_id, day)
            if after >= 0 and base >= 0 and after - base > credited:
                credited = after - base
                U.sync_video_day(aweme_id, credited, day)
            st['accepted'] += res['accepted']
            rec.update({'rounds': rec['rounds'] + 1, 'credited': credited,
                        'last_final': after})
            st['next'] = time.time() + 30      # 开着的通道 30 秒后马上再探
            state[aweme_id] = rec
            save_state(state)
            log(f'[{c}] 上报成功 {res["accepted"]} / 尝试 {res["attempts"]} → '
                f'真实净增 {gained} | 今日累计 {credited}/{target} | '
                f'原因 {res["reason"] or "-"}')
            append_log({'event': 'burst', 'channel': c, 'chunk': chunk,
                        'accepted': res['accepted'], 'attempts': res['attempts'],
                        'gained': gained, 'credited': credited,
                        'throttled': res['throttled'], 'reason': res['reason']})
            if opts['probe_only']:
                log('--probe-only：测完窗口就收工')
                deadline = 0
                break
            if gained <= max(2, chunk // 10):
                log('本轮几乎没进账，窗口疑似刚关，该通道 120s 后再探')
                st['next'] = time.time() + 120

    final = D.share_count(aweme_id, retries=2)
    credited = U.video_day_used(aweme_id, day)
    out = {'event': 'finish', 'aweme_id': aweme_id, 'day': day, 'target': target,
           'baseline': base, 'final': final,
           'real_delta': (final - base) if final >= 0 else -1,
           'credited': credited, 'rounds': rec.get('rounds', 0),
           'chan': {c: {'windows': v['windows'], 'accepted': v['accepted'],
                        'closed': v['closed']} for c, v in rec['chan'].items()},
           'time': stamp()}
    append_log(out)
    log('收工 ' + json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        log('手动中断，进度已写入 daily_state.json')
        sys.exit(130)
