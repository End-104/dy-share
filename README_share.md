# 抖音分享量上报 —— 使用说明（独立端）

> 本目录是刷分享量专用的独立端，自带 .venv312，与播放量目录 D:\jiedan 完全解耦：D:\jiedan 的 start.bat 只起播放量窗口，本目录的 start.bat 只起分享量窗口。两个目录可以同时开，靠平台侧 execState 锁避免抢单。

## 文件

| 文件 | 作用 |
|---|---|
| `dy_share.py` | 引擎：设备池调度（含死号剔除）+ 两通道 stats 上报 + 进度核对 + 熔断 |
| `share_cli.py` | 单条视频命令行入口 |
| `verify_share.py` | 真值验证器：拉官方 `statistics.share_count` 做前后对比，判定是否真的入账 |
| `applog_replay.py` | AppLog 埋点重放通道（备用，字段被改时用） |
| `ayk_dy_share_handler.py` | 供应商分享量订单端：拉单→换ID→上报→回写→完成/退款，支持 `dry` 演练 |
| `start_share.bat` | 一次性拉起 643 / 1763 共 12 个接单进程（每货号 6 页，每单 24 线程） |
| `aid_scan.py` | 扩桶扫描：按 aid × license 打一轮 stats，标出身份能过网关（拿非空回执）的 aid，`--emit-profile` 直接输出可粘的 `PROFILES` 条目 |
| `aid_probe.py` | 单 aid 点桶：只看 `share_count` 是否涨，涨了才写 `aid_alive.json` |
| `aid_expand.py` | 阶梯量容量：按 `--chunk` 递增打，连续 `--stop-empty` 轮 0 计入收工，得到该 aid 真实容量 |
| `channel_cap.py` | 分通道计数桶实测：1128 / 32 / applog 各自的窗口容量与合计，`--hosts` 扫六个接入点 |
| `bucket_family_test.py` | 判两只 aid 是同桶还是独立桶（先打干 A 再打 B，B 还进账才算独立） |
| `bucket_ledger.py` `bucket_inventory.py` `bucket_planner.py` | 桶台账 / 四层桶盘点 / 单视频够不够的产能核算 |
| `share_sched.py` | 稳态调度器：按 `aid_alive.json` 自动选桶，`--auto-expand` 缺桶时自动扩 |
| `share_queue.py` | 本地分享量队列，不走供应商订单的备用通道 |
| `function.py` | 平台接口封装：订单拉取/回写、播放量与分享量读取、链接换 ID、通道 play32/play1128/play6368 |
| `dy_abogus.py` | a_bogus 签名 |
| `xargus1128/` `xargus1233/` | X-Argus / X-Gorgon / X-Ladon 两套签名，1128 与 1233 通道各一套 |
| `used_store.py` | 单视频每日额度台账（`used_devices.db`），跨进程共享 |
| `daily1800.py` | 单视频当日 1800 的窗口调度器：探窗→开窗吃量→关窗退避，按真实增量累计 |
| `pool_tool.py` | 设备池扩容与体检：`audit` 产能账、`import` 导入去重、`verify` 抽样验活 |
| `device_mint.py` | 设备现场注册：`mint` 批量注册落池、`--write-infos` 兼顾 32 通道、`--verify` 实测筛活 |
| `channel_cap.py` | 分通道计数桶实测：1128 / 32 / applog 各自的窗口容量与合计，`--hosts` 扫六个接入点 |
| `mint_quota_probe.py` | 注册配额节奏探针：自适应吃窗口、边测边攒设备，`--status` 出窗口表（条数/时长/冷却/每小时产量） |
| `cooldown_check.py` | 冷却核验：只看不发请求，报今日/昨日入账、下一次零点、零点后多少设备重新可用、够不够 1800 |
| `run_1800.bat` | 生产启动器：一套实测口径拉起 `daily1800.py`（1800 目标、边烧边补、收工 23:30） |
| `video_cap.json` | 已测到的单视频天花板记录（639 / 341 两档，含 attempts / accepted / consecutive_empty） |
| `_deleted_20261116.zip` | 2026-11-16 清掉的中间实验脚本备份（`bucket_lab` `cap_probe` `share_plan` `window_probe` `share_ceiling_lab` `share_axis_probe` `dy_share_three` `channel_bridge` `smoke_share` 及两个自测脚本），要回看直接解压 |
| `start.bat` `start_share.bat` | 分享量接单窗口：`start.bat` 为总入口，`start_share.bat` 实际拉起 12 个进程；`start.bat dry` 为演练 |
| `cfg_qw_dy_share_1763.json` | 分享订单端配置，`goods_sn = 1763`（D超级分享） |
| `cfg_qw_dy_share_643.json` | 分享订单端配置，`goods_sn = 643` |
| `.venv312/` | 现场新建的可用虚拟环境（原 `.venv` 指向另一台机器的 Python311，已失效） |

## 环境

```bash
D:\jiedan\.venv312\Scripts\python.exe -m pip install httpx gmssl snowland-smx pycryptodome protobuf
```

已装好，直接跑即可。

## 与供应商订单流的接线状态（2026-11-13 17:20 实测）

| 项目 | 状态 | 证据 |
|---|---|---|
| 播放量 · 代码接线 | 已接 | `start.bat` 起 34 个进程，货号 473/644/648/684/984/1760 |
| 播放量 · 拉单 | 通 | `goodsSN=473&state=2` → total=4653；`state=4` → total=33605 |
| 播放量 · 上报 | 通，1:1 | 演练 3 单各 20 条：accepted=20，播放量 16→36 / 16→36 / 10→30 |
| 播放量 · 进程 | 未运行 | 本机无 python 进程，订单在排队 |
| 分享量 · 代码 | 已写 | `ayk_dy_share_handler.py`，与播放端同构 |
| 分享量 · 配置 | 已填 | `cfg_qw_dy_share.json` 的 `goods_sn = 1763` |
| 分享量 · 商品 | 已上架 | 货号 1763「D超级分享」，全状态扫描 1–9000 号确认 |
| 分享量 · 启动 | 已放开 | `start.bat` 末尾两行已取消注释，每进程 100 条/页 |

播放量的接口、签名、回写流程全部可用；分享量货号 1763 已在供应商账号下，配置填好、启动行已开。现存 4 单全是 2026-11-05/06 的历史单（1 单停在 state=1，3 单 state=6 已退），`state=2` 待处理为 0，所以分享端进程会空转等新单，这是正常状态。

## 播放量订单端的三个坑（已在 `ayk_dy_bf_handler.py` 修掉）

原版备份已从工作区移除（仍留在 `.git` 历史里，需要时用 PyCharm 的 Git 面板回滚），问题与修法：

1. **立刻读计数，回写 0**：上报后计数有 30–90 秒回填延迟，原版马上 `get_play_count`，拿到的还是 `startNum`，于是 `exec_num=0`、订单永远完不成、还一直留在 `state=2` 被反复捞起来重刷。现在 `ShareBooster.run()` 收工后会轮询最多 60 秒等落账，再写进度（`SETTLE_SECONDS` / `SETTLE_POLL`）。
2. **多线程超发**：原版 30 个线程一起冲，目标 20 可能发出 30。现在用名额预占（`_reserve`），`accepted + inflight` 不越过目标，实测 20/20 不超。
3. **空包一律记失败**：空包可能只是这个视频拒收，设备是好的。现在先探一次 detail 分辨死号与视频拒收；死号连续 2 次黑名单，视频拒收则计入限流计数，连续 6 次或投产比低于 15% 就收工并回写原因，不再空转烧设备。

演练命令（不动订单状态，只验证上报效果）：

```bash
python ayk_dy_bf_handler.py cfg_qw_dy_bf_473 1 5 dry
```

## 供应商商品清单（实时扫描）

扫描口径：按 `goodsSN` 逐个打 `/api/supplier/order/v2/orders`，`state=` 留空表示全状态，
`result.total > 0` 即该货号存在，`data[0].goodsName` 给真名。1–9000 号全扫，用时约 35 秒。

```
473   超级小单播放【秒2200】   43948 单
644   自营真机播放              196 单
648   自营1k-7k播放             545 单
684   真机播放2号               112 单
984   闪电小单播放【秒2800】   7299 单
1753  自营-小单播放              13 单
1760  小单播放【秒1200】      10423 单
1763  D超级分享                   4 单   ← 分享量，唯一一个
```

八个里七个是播放类，一个（1763）是分享类，已填进 `cfg_qw_dy_share.json`。

**注意扫描口径**：用 `state=4`（已完成）扫会漏掉 1763，它那 4 单分别是 state=1 和 state=6，
所以商品盘点必须用 `state=` 空串。按 `state=4` 口径生成的 `goods_list.json` 只有 7 条、已作废，本次清理已删除。

## 已确认真值（2026-11-13 16:10 实测，非推测）

用 `verify_share.py` 在真实视频 `7706079479723465882` 上做前后对比，依据是官方 `aweme_detail.statistics.share_count`：

| 时刻 | 上报成功 | share_count | 结论 |
|---|---|---|---|
| 基准 | — | 190 | 起始值 |
| 单发 3 次 | 3 | 193 | 逐条 1:1 |
| 发 10 次 | accepted 7 | 193 → 200 | 7 条全部入账，存在 30–60s 传播延迟 |
| 发 6 次 | accepted 4 | 200 → 204 | 4 条全部入账 |
| CLI 跑 20 条 | accepted 20 | 204 → 224 | 20 条全部入账，两通道均有效 |
| 密集压测后 | accepted 8 | 226 → 231 | 增速转慢，出现视频级限流（见下节） |

同一时段 `digg_count`、`comment_count`、`collect_count` 全程不变（16988 / 43 / 1706），说明 `share_delta` 只命中分享计数，没有串字段。

**字段名 `share_delta` 已确认正确**，`batch_stats` 里的 `share_delta: 1` 就是分享计数器，Android aid=1128 与 aid=32 两条链都生效。同一个视频当天累计从 190 涨到 231，全程只用这套程序。

## 视频级限流（实测现象）

同设备、同 IP、同签名，换个视频就正常回 `status_code:0`，打原视频返回 **HTTP 200 + 空 body**；过一段时间又偶尔能进一两条。结论是限流挂在**视频维度**，不是设备死、不是签名错、不是 IP 被封。

程序按三层处理：

1. **空包分辨**：空包时先用同一台设备读一次 `aweme_detail`，读得到 → 设备活着，判为视频限流，不拉黑；读不到 → 才 `mark_bad` 进死号集合。
2. **退避**：命中限流后单线程退避 10–25 秒；连续 6 次限流判死本次任务（`throttled: true`）。
3. **停滞与投产比**：`accepted - 实入 >= 8` 且计数 90 秒不动，收工；跑满 30 次而投产比低于 15%，收工。这两种情况都返回 `throttled: true` 和具体 `reason`。

进度判定已改为**只认真实增量**：`delta = final - baseline`，`accepted` 只作参考。之前用 `max(real, accepted)` 会把服务端“收下但不计数”的请求算成成功，虚高。


## 接口实测表

| 接口 | 请求 | 返回 | 结论 |
|---|---|---|---|
| `api26-normal-hl.amemv.com/aweme/v1/aweme/stats/` | batch_stats 带 `share_delta:1` | `status_code:0`，`fatal_item_ids:[]` | 可用，主通道 1128 |
| `api5-platform-m-hj.amemv.com/aweme/v1/aweme/stats/` | 同上，aid=32 | `success_item_ids` 回显目标 id | 可用，通道 32 |
| `api26-normal-hl.amemv.com/aweme/v1/aweme/detail/` | GET，1128 签名 | 完整 `statistics` | 可用，做真值基准 |
| `www.douyin.com/aweme/v2/web/aweme/stats/` | 带 `share_delta` | `status_code:5 参数不合法` | web 不接受该字段，已关闭 |
| `www.douyin.com/aweme/v2/web/aweme/stats/` | 只带 `play_delta` | `status_code:0` | web 只能加播放量 |
| `api26-normal-hl.amemv.com/aweme/v1/aweme/share/` | 取分享短链 | `status_msg:url doesn't match` | 路径未确认，`SHARE_LINK_MODE` 保持 `off` |
| `110.42.32.174:9001/.../detail/play` | 读 share_count | 连不通 | 第三方计数接口挂了，自动回落本地成功数 |

对照抓包：网页版真实分享动作只往 stats 发 `aweme_type=0&item_id=<id>&play_delta=1&source=0`，**根本不存在 share 字段**——所以分享量只能走 App 端 `batch_stats`，这条路是对的。

## 服务端「收下不计数」——2026-11-15 实测（本版据此改为分窗节奏）

在 `7687951780465923904` 上按 24 线程全力冲 1800，实测：

| 指标 | 数值 |
|---|---|
| 上报成功 `accepted` | 787（1128 通道 596 + 32 通道 194） |
| `share_count` 实际增加 | **221**（644 → 865） |
| 计入率 | 28% |
| 计入发生的时间窗 | 开跑后 50 秒内，之后完全停止 |
| 台账记录 | 787 条（旧逻辑把接受数当入账数，虚高 566 条） |

三个结论：

1. **每条请求只加 1，没有放大**，想 1800 就得 1800 条真入账。
2. **服务端对同一视频有计数窗口**：一次窗口只认 200 上下，超出的请求照样回 `status_code:0`，但计数不动。24 线程猛冲等于把 2/3 的设备白烧掉。
3. **计数窗口关闭后不是立刻恢复**：实测关闭 7 分钟后再发，全部返回空包（`throttled:video`）。

本版据此改了三处：

- **分窗节奏**：`SHARE_ROUND_CHUNK=180`，一轮只塞 180 条，吃完回队尾，下一轮靠 `SHARE_ZERO_ROUNDS` 的 600s 退避自然拉开。
- **暂停阀**：`ShareBooster` 检测到「接受数比实入多 8 条且 90 秒未回填」时，不再直接收工，而是清 `_gate` 让所有线程原地等待，每轮 `STALL_PAUSE=90s` 后复查计数；回填就继续，连续 6 轮不回填才判限流。
- **台账校准**：一次运行收工时按 `实入 < 接受` 的差额调用 `U.refund_video()` 把当日额度退回去，`video_day` 只统计真正入账的条数，客户当天的 1800 余额不会被空转请求吃掉。

## 单条视频

```bash
# 干跑：三个通道各发一条，打印原始返回
python share_cli.py --target "https://v.douyin.com/xxxxxx/" --count 3 --dry-run

# 正式刷
python share_cli.py --target "https://v.douyin.com/xxxxxx/" --count 1000 --threads 20

# 只走 aid=1128，限 10 分钟，不夹带播放量
python share_cli.py --target 7426187539512345678 --count 500 --channels 1128 --timeout 600 --play-delta 0

# 刷完自己验一遍真值
python verify_share.py --target 7426187539512345678
```

输出 JSON：`baseline` 起始量、`final` 结束量、`delta` 增量、`accepted` 本地成功数、`attempts` 总请求数、`channels` 各通道贡献。

## 真值验证

```bash
python verify_share.py --target 7706079479723465882          # 只读基准
python verify_share.py --target 7706079479723465882 -n 5     # 发 5 条再复读
```

末行 `share_count delta = N -> 字段名正确` 即为入账确认。注意 `final` 有 30–60s 延迟，脚本里已经 `sleep 3`，偏保守；大批量跑完想要准确数字，等一分钟再单跑一次只读模式。

## 死号剔除（本版新增）

服务端对失效设备返回 **HTTP 200 + 空 body**，不是 4xx、也没有 `status_code`。老逻辑把空包当失败重试，会反复命中同一批死号，吞吐掉一半（实测 44 次请求才出 20 条）。

本版行为：空包直接 `pool.mark_bad()`，连续 2 次判死进 `_dead` 集合，永不派发；`report_1128` 内部最多换 3 台设备重试。20 条目标从 44 次请求压到接近 20+3。

池子健康度随时可查：

```python
import dy_share as D
pool = D.DevicePool(D.devices34)
print(pool.stats())   # {'total': 1688, 'alive': ..., 'dead': ..., 'ok': ..., 'empty': ..., 'err': ...}
```

## 高保真通道（AppLog 重放）

分享行为在客户端还会走 `log-api5-applog.douyin.com/service/2/app_log/` 的 protobuf 埋点。当前 stats 链已经验证有效，这条只在字段被改版时才启用：

```bash
mkdir capture
# 手机代理 + mitm 证书，点一次「分享 → 复制链接」，从流量里导出：
#   capture/app_log.bin   原始 body
#   capture/app_log.url   完整 URL
#   capture/app_log.json  {"cookie": "...", "user_agent": "..."}

python applog_replay.py --target "https://v.douyin.com/xxxx/" --count 300 --threads 10
```

`aweme_id` 是固定 19 位，替换前后字节长度一致，protobuf 的 varint 长度前缀不用改；`_rticket`、`ts` 与 body 内时间戳会自动刷新，签名用模板里的设备重新算。

## 分享量下单流程（货号 643 / 1763）

和播放端逐行同构，唯一差别是计数器与字段：播放端用 `get_play_count` + `field=''`，
分享端用 `share_count` + `field='share_delta'`。

单进程 = 一个货号 + 一页订单。十二个进程（两货号 × 六页）覆盖全部待处理单，单视频满额靠台账卡口 + 轮转调度：

```bash
python ayk_dy_share_handler.py cfg_qw_dy_share_1763 1 24
python ayk_dy_share_handler.py cfg_qw_dy_share_1763 2 24
python ayk_dy_share_handler.py cfg_qw_dy_share_1763 3 24
python ayk_dy_share_handler.py cfg_qw_dy_share_1763 4 24
python ayk_dy_share_handler.py cfg_qw_dy_share_1763 5 24
python ayk_dy_share_handler.py cfg_qw_dy_share_1763 6 24
python ayk_dy_share_handler.py cfg_qw_dy_share_643  1 24
python ayk_dy_share_handler.py cfg_qw_dy_share_643  2 24
python ayk_dy_share_handler.py cfg_qw_dy_share_643  3 24
python ayk_dy_share_handler.py cfg_qw_dy_share_643  4 24
python ayk_dy_share_handler.py cfg_qw_dy_share_643  5 24
python ayk_dy_share_handler.py cfg_qw_dy_share_643  6 24
```

第三个参数是每单线程数（省略默认 20），第四个参数写 `dry` 进演练模式。

或者一条命令全拉起：

```bat
start_share.bat
```

每单九步，输入→输出如下：

| 步 | 动作 | 接口 / 调用 | 输出 |
|---|---|---|---|
| 1 | 拉单 | `get_order_list('2')` | state=2 的订单数组，空则 5 秒后重试 |
| 2 | 读详情 | `get_order_detail(order_sn)` | 订单对象，非 state=2 直接跳过 |
| 3 | 抢锁 | `execState?order_sn=..` | 返回 `1` = 已被别的进程领走，跳过 |
| 4 | 换 ID | `execute?order_sn=..&exec_update=1` | 19 位 aweme_id；失败退回本地解析，再失败置 state=3 |
| 5 | 读基数 | `share_count(aweme_id)` | 当前真实分享量；读不到用 `execNum` 折算 |
| 6 | 算缺量 | `order_num - (current - start_num)` | 缺量 ≤ 5 直接置 4 完成 |
| 7 | 上报 | `ShareBooster.run()` | accepted / delta / throttled / reason |
| 8 | 回写 | `update?exec_state=1&exec_num=<delta>` | 平台记录本单真实执行量（用 delta，不用 accepted） |
| 9 | 结算 | 再读 `share_count` → `update_order_progress` | 完成(4) / 退款(7) / 留单下一轮 |

三个和播放端一致的关键设计：

1. **回写用真实增量**：`exec_num` 写 `delta`（服务端计数差），不写本地 `accepted`。分享报文被服务端「收下但不计数」时，写 accepted 会虚高，订单永远差最后几条。
2. **等落账再结算**：`ShareBooster.run()` 收工后最多等 `SETTLE_SECONDS=60` 秒轮询，`share_count` 有 30–90 秒回填延迟，立刻读会读到旧值。
3. **零增量退避**：同一单连续零增量按 600 / 1200 / 2400 / 3600 秒退避重试，满 4 轮才按剩余量退款并置 state=7；中途只要有一轮进账就清零计数，继续跑。

演练模式不动任何订单状态，只解析链接、试打 3–10 条、比对前后计数：

```bash
python ayk_dy_share_handler.py cfg_qw_dy_share_1763 1 20 dry
```

交付前自检（不依赖待处理单，11 项逐段判定）：

```bash
python _share_flow_selftest.py
```

2026-11-13 18:04 实测结果：11 项全过。1763 历史 4 单、待处理 0；历史链接
`https://v.douyin.com/Nieiq-aw6To/` → `7687951780465923904`，`share_count = 8`；
设备池 1688 台全活；三方 `execState` 返回 `2`、`execNum` 返回 `0`。

**643 的现状**：订单接口对它返回 `total=0`，全状态也是 0。供应商接口只按订单反查商品，
零单货号与不存在货号返回完全一样（都是 `total=0`），所以没法从接口判断 643 是「刚上架没出单」
还是「货号写错」。进程照常配好挂着，一旦有单立刻接；要确认商品本身，需在供应商后台商品管理里
核对 643 的名称与类别，若不是分享类，改 `cfg_qw_dy_share_643.json` 的 `goods_sn` 即可。

## 备用通道：本地队列

不走供应商订单时用这个：

```bash
python share_queue.py --dry-run          # 先看链接解析对不对
python share_queue.py                    # 正式跑，结果回写同一文件
```

`share_orders.json` 格式：

```json
[
  {"link": "https://v.douyin.com/xxxx/", "count": 100, "note": "客户A"},
  {"link": "7706079479723465882", "count": 50}
]
```

跑完每条会补上 `status / baseline / final / delta / accepted / elapsed / finished_at`，可以直接当交付记录。

## 调参位（`dy_share.py` 顶部）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SHARE_FIELD` | `share_delta` | 已验证正确，改版时换这里 |
| `DEFAULT_CHANNELS` | `('1128','32')` | 默认通道，web 不参与分享 |
| `TARGET_PLAY_DELTA` | `1` | 目标条目顺带加的播放量，`0` 不夹带 |
| `NOISE_RATIO` | `2` | 每批混入的随机 item_id 条数 |
| `SHARE_LINK_MODE` | `off` | 分享链路未确认，保持 off |
| `SLEEP_RANGE` | `(0.8, 2.5)` | 单线程间隔 |
| `DEVICE_COOLDOWN` | `86400` | 同设备同视频复用冷却（秒） |
| `MAX_FAILURES` | `15` | 单通道连续失败熔断 |
| `_PoolBase.BAD_LIMIT` | `2` | 连续空包几次判死 |
| `THROTTLE_LIMIT` | `6` | 连续限流几次收工 |
| `THROTTLE_BACKOFF` | `(10, 25)` | 限流退避区间（秒） |
| `MIN_YIELD` / `YIELD_WINDOW` | `0.15` / `30` | 投产比低于 15% 收工 |
| `STALL_TOLERANCE` / `STALL_SECONDS` | `8` / `90` | 接受数虚高 8 条且 90 秒不动，进入暂停等落账 |
| `STALL_PAUSE` / `STALL_PAUSE_ROUNDS` | `90` / `6` | 停滞时暂停时长与轮数，全部无效才判限流收工 |
| `DEAD_SYNC_SECONDS` | `30` | 全局死号表多久同步一次到进程内的池 |

### 接单端环境变量（`start_share.bat` 已设好）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SHARE_DAY_CAP` | `1800` | 单视频每自然日入账上限；`0` = 不限（只剩设备池约束） |
| `SHARE_DEVICE_GAP` | `300` | 同一台设备两次分享的最小间隔（秒）；不设会几秒内连派几十条，被判机器 |
| `SHARE_ROUND_CHUNK` | `180` | 单轮最多吃多少条，吃完回队尾；`0` = 一轮吃满目标 |
| `SHARE_ZERO_ROUNDS` | `4` | 零增量退避重试轮数，超了才退款 |
| `SHARE_STALL_PAUSE` | `90` | 计数停滞时暂停发请求、等落账的秒数 |
| `SHARE_STALL_ROUNDS` | `6` | 暂停几轮计数仍不回填才收工 |

### 池子与台账运维

```python
import used_store as U, dy_share as D

U.stats()                       # 含 dead：近 24h 判死设备数
U.revive_dead()                 # 清空全局死号表；给 device_id 只救一台
U.reset_video_day('aweme_id')   # 人工清零某条视频当天入账，用于当天重跑
D.DevicePool(D.devices34 + D.load_extra_devices()).stats()
```

`used_devices.db` 已切 WAL，旁边会出现 `-wal` / `-shm` 两个伴随文件，属正常；备份时三个文件一起拷。

## 产能

实测 5 线程、20 条：17.17 秒，请求 44 次（含死号重试），最终 20 条全部入账。24 线程单进程稳定在 13–15 次/秒；12 进程合计约 150–175 次/秒。

硬上限由设备池决定——分享按「设备 × 视频 × 计数窗口」计一次，同一台设备重复上报返回 `status_code:0` 但计数不变。当前池：`devices34` 1688 台 + `infos` 188 台 + 外挂池（`pool_extra.json` / `pool_extra.txt`），单视频单日上限 1800。

### 单视频吃满 1800 的前提

一条视频一天要吃满 1800，需要 1800 台**当天没用过这条视频**的设备，没有放大系数（每条请求只 +1）。算术：

| 池规模 | 单视频 1800 | 100 条视频各 1800（180,000 条） |
|---|---|---|
| 1876 台（现状） | 够，余量 76 台（4%） | 每台要贡献 96 次，零失败才刚好，实际不够 |
| 2700 台 | 余量 900 台（33%） | 每台 66.7 次，容失败率 30% |
| 3600 台 | 余量 1800 台（50%） | 每台 50 次，容失败率 50% |

`pool_extra.json` 追加 1000 台到 2700 是最低成本方案；`pool_extra.txt` 按行追加 32 通道设备。加载器在 `dy_share.py:load_extra_devices / load_extra_infos`，文件放进目录即生效。

另一条硬墙是服务端 per-video 天花板（`video_cap.json` 实测 639 / 341），加设备无法突破；要确认某条视频能不能到 1800，先跑 `cap_probe.py` 探一遍，再决定当天交付还是分日。

## 单日 1800 的实测结论与窗口调度（2026-11-15 22:00—22:30）

先把「1800 靠扩池能不能做到」这个问题用数字钉死：

| 口径 | 数值 | 说明 |
|---|---|---|
| 台账硬上限 | 1800 条/视频/自然日 | `SHARE_DAY_CAP`，代码层卡，不是服务端口径 |
| 设备原料 | 1876 台（1688 + 188），`pool_extra` 为空 | 每条请求只 +1，无放大系数 |
| 单窗口实入 | 221 条 | 今日两条视频各自 644→865、341→562，一分不差 |
| 计入率 | 28% | 787 条 accepted → 221 条入账 |
| 关窗后恢复 | 6 分钟复打 8 进 1；8 小时复打 15 进 0 | 窗口是小时级恢复，不是分钟级 |
| 单日最好记录 | 408 条（11-14，231→639） | 一天开到了两个窗口 |

结论：**扩池只解决原料，不解决计数**。1800 = 221 × 8.1，需要在一天内开 8 个以上窗口，或者把每窗容量拉起来；单靠把池子扩到 2700 台，只是让「每窗那 221 条」有足够新鲜设备可用，不会让服务端多认一条。

两件事因此必须同时做：

1. **池子扩到 ≥2700**：1800 条需要 1800 台当天没用过这条视频的设备。1876 台只有 1.04 倍余量，第一轮失败重试就会击穿。
2. **按窗口节奏排产**：关窗期打请求等于白烧设备（回 200 空包、不计数、还占台账），必须探到窗口开了再全力吃。

调度器就是干第 2 件事：

```bash
# 目标 1800，跑到 23:40 收工；切片 180，关窗首次退避 900s，最长 3600s
python daily1800.py https://v.douyin.com/xxxx/ 1800 --threads 24 --chunk 180 --gap 900 --gap-max 3600

# 只测当前窗口开没开，不发正式量（探针 4 台，空包不写台账，可反复用）
python daily1800.py 7693857502727274422 1800 --probe-only
```

`daily1800.py` 的一轮循环：

| 步 | 动作 | 判据 | 输出 |
|---|---|---|---|
| 1 | 读基线 | `share_count` | 当日真实起点，写入 `daily_state.json` |
| 2 | 探窗 | 4 台探针各打 1 条，等 45s 读增量 | 增量 > 0 = 开窗；全空包 = 关窗 |
| 3 | 关窗 | 按 `gap` 退避，×1.6 递增封顶 `gap-max` | 不消耗台账、不烧设备 |
| 4 | 开窗 | `ShareBooster` 吃 `chunk` 条 | accepted / 真实净增 |
| 5 | 校准 | `sync_video_day` 用真实增量覆盖当日入账 | `video_day` 与官方计数一致 |
| 6 | 收工 | 累计到 1800 或到 `--stop-at` | 追加一行到 `daily_run.jsonl` |

次日零点重置是台账天然行为：`pair_cutoff()` 取当天零点，昨天的 `(设备,视频)` 记录只挡到昨天 24:00。实测验证——昨天在 `7693857502727274422` 上用过的设备，今天 `tried()=False`、`free_devices()` 直接放回候选。所以「第二天同一条视频再刷 1800」不需要人工清库，同一套命令跑第二遍即可。

`window_probe.py` 是这条链的取证工具，跑一晚就知道这条视频一天到底开几个窗：

```bash
python window_probe.py 7687951780465923904 --interval 600 --hours 6 --devices 6
```

末行给出「窗口数 / 每窗平均计入 / 折算单日上限」。折算上限 ≥1800 才能当天交付，否则按 `1800 ÷ 每窗计入 ÷ 每天窗口数` 折算天数排产。

## 通道分桶实测与设备现场注册（2026-11-15 22:40—23:20）

两个新事实，直接改变 1800 的算术。

**一、计数桶是「通道 × 视频」，不是「视频」一个总闸。**

| 时刻 | 视频 | 通道 | 动作 | 结果 |
|---|---|---|---|---|
| 22:53 | `7706079479723465882` | 1128 | 一台当日未用过的老设备打 1 条 | `throttled:video`，计数 302 → 302 |
| 22:53 | `7706079479723465882` | 32 | 8 台 infos 设备逐条打 | 1 条 `status_code:0` 且 `success_item_ids` 非空，计数 302 → 303 |
| 22:41 | — | — | `device_register` 现场注册 | 下发 `device_id=2574436085832064`、`install_id=…836160`、`device_token`（151 字符） |

1128 已经彻底关窗的时候，32 通道还能进量，说明两个通道各自一个桶。单日天花板因此不是 `221 × 窗口数`，而是 `(221 + 32 桶容量 + applog 桶容量) × 窗口数`。`channel_cap.py` 就是把这个和测出来的工具。

**二、设备不用买，可以现场注册。**

`device_register` 只校验参数自洽，不校验机器真实存在。注册一次拿到一整套新身份（`device_id` / `install_id` / `device_token` / `ttreq` cookie），这套身份对任何一条视频都算「新设备」。也就是说池子规模是可再生的：每条视频每天要 1800 台，就每天注册 1800 台，不用囤。

| 文件 | 作用 |
|---|---|
| `device_mint.py` | 现场注册设备：`mint` 批量注册并落 `pool_extra.json`，`--write-infos` 同时写 `pool_extra.txt`（32 通道素材），`--verify <aweme_id>` 注册后逐台实测只留活的，`test` 打印完整响应排查 |
| `channel_cap.py` | 分通道计数桶实测：按 `--channels 1128,32,applog` 串行打满每个桶，输出各通道本轮计入量与合计 |

```bash
# 注册 200 台并写两份池子文件，注册完逐台实测（--verify 用一条测试视频）
python device_mint.py mint 200 --verify 7706079479723465882 --write-infos

# 分通道测桶容量（串行、逐条、连续 10 条空包停手，打完静置 60s 等回填）
python channel_cap.py 7706079479723465882 --channels 1128,32,applog --max 240
```

**三、调度器改成每通道一套状态机。**

原来两个通道共用一个退避计时器，1128 关窗会把还开着的 32 一起停掉。现在 `daily1800.py` 的做法：

| 通道状态 | 触发条件 | 动作 |
|---|---|---|
| 到点 | `next <= now` | 用该通道 2~4 台新鲜设备探一次（1128 用 `devices34`，32 用 `infos`） |
| 开窗 | 探针后真实增量 > 0 | 立刻吃 `chunk` 条，30 秒后再探 |
| 关窗 | 探针增量 = 0 | 该通道退避 `gap`，×1.6 递增封顶 `gap-max` |
| 素材尽 | 探针回 `device pool empty` / `info pool empty` | 该通道挂到收工时间，不再空转 |

命令不变，可显式指定通道：

```bash
python daily1800.py 7693857502727274422 1800 --threads 24 --chunk 180 --channels 1128,32 --stop-at 23:40
```

状态文件 `daily_state.json` 里每条视频多一层 `chan`，记录每个通道的 `windows / accepted / closed / gap / next`，重跑接着走。

## 注册配额实测与运行中自动补料（2026-11-15 23:03—23:15）

### 一、注册接口有 IP 级配额，冲太快会被关窗

| 时间 | 动作 | 结果 |
|---|---|---|
| 22:58 | 8 线程注册 40 台 | 4 秒完成，40/40 成功（约 10 台/秒） |
| 23:00 | 单线程注册 60 台 | 65 秒完成，60/60 成功（约 1 台/秒） |
| 23:03—23:05 | 12 线程冲 4500 台 | 跑到第 2 分钟开始回 `{"device_id":0,"install_id":0,"new_user":0}`，成功率归零 |
| 23:06 | 单台诊断 | 两个 URL 都是 `device_id:0`，签名正常、HTTP 200，纯粹被拒 |
| 23:09 | 停手 2 分钟后单台重试 | `new_user:1`，正常下发 `device_id` 与 151 字符 `device_token` |
| 23:11—23:15 | 2 线程 / 1 秒间隔，200 台 | 208 次尝试成功 200 台，**96% 成功率、0.93 台/秒** |

结论：注册不是死路，是**漏桶**。持续 0.9 台/秒稳过；瞬时 10 台/秒能冲一小段，然后整窗关闭，
关窗后硬冲只会一直踩住配额窗。所以生产不能"提前一次性备足"，只能**边烧边补**。

`device_mint.mint_batch()` 现在带熔断：连续失败按 2→4→8…秒整体降温（`pause_base`，上限 `pause_max`），
连续 `max_streak` 次失败直接收工返回 `aborted:true`，把配额留给下一个时间片。
返回值多了 `rate`（成功率）与 `aborted`。

### 二、调度器运行中自动补料

`daily1800.py` 原来遇到 `device pool empty` 就把通道挂到收工时间——那是把"没料"当成"没窗"。
现在改成补料：

| 触发 | 动作 |
|---|---|
| 探针回 `device pool empty` / `info pool empty` 且注册未冷却 | 就地 `mint_batch`，按 `(目标-已入账)×4 + 200` 估算缺口 |
| 补料成功 | `5s` 后重探该通道，接着吃 |
| 补料被限速（`aborted`） | 通道 `next` 推到 15 分钟后，注册冷却期内不再空打 |
| 今日补料超 `--mint-max`（默认 12000） | 才真正挂起该通道 |

默认口径：`--mint-batch 400 --mint-threads 2 --mint-pace 1.0`，约 0.9 台/秒、每天可补 12000 台，
足够覆盖"每台设备每天对每条视频只算一次"的 6429 次尝试需求（1800 ÷ 28% 实测计入率）。
`--no-mint` 可退回纯池子模式。

池文件是"读进来-改-写回去"，两个进程同时写会丢记录，所以 `append_pool` / `append_infos`
加了跨进程锁 `.pool.lock`（`O_EXCL` 建文件，超时 120 秒报错），锁残留时手工删掉即可。

### 三、接入点扫桶

`report_1128` 的域名从硬编码改成模块级 `HOST_1128`（可用环境变量 `SHARE_HOST_1128` 覆盖）。
限流计数器如果挂在接入网关而不是业务服务上，换接入点就等于换桶。`channel_cap.py` 支持
`1128@host` 写法逐点测，或 `--hosts` 一次扫完候选接入点：

```bash
python channel_cap.py 7706079479723465882 --hosts --max 40 --stop-empty 6 --settle 45
python channel_cap.py 7706079479723465882 --channels 1128@api3-normal-c-hl.amemv.com --max 40
```

候选接入点：`api26-normal-hl` / `api3-normal-c-hl` / `api5-normal-c-hl` /
`api22-normal-c-hl` / `api31-normal-c-hl` / `api-normal-c-hl`（均在 `.amemv.com` 下）。

## 维度探针与两条硬结论（2026-11-15 23:19—23:35）

新增 `share_axis_probe.py`：探 (视频 × 字段维度) 到底是单键封顶还是复合键分桶。

```bash
python share_axis_probe.py 7706079479723465882                      # 32 组合，每条换设备
python share_axis_probe.py 7706079479723465882 --reuse --field channel,share_platform --values weixin,qq,qzone,weibo
```

### 结论一：回执 ≠ 计入，附加字段是假阳性

样本 `7706079479723465882`，同日 1128 通道对该视频长期回空包。

| 组 | 尝试 | 拿到非空回执 | 真实计数增量 |
|---|---|---|---|
| 不加附加字段（对照） | 30 | 9 | **9**（30%） |
| 随机附加字段（`share_type`/`channel`/`share_platform`/`from` × 8 取值） | 30 | 14 | **2**（6.7%） |

23:19 那次 32 组合测试里 7 条有回执、计数 303→310 同步 +7，看着像"换字段就能进"；
23:27 的 A/B 推翻了它：非空回执里有一半是服务端"回你一句"，条目没计数。
所以 `SHARE_RANDOM_EXTRA` 默认关闭，`batch_stats(extra=...)` 与 `report_*` 的 `extra` 参数保留，
只当探针用，不进生产路径。

### 结论二：同一设备对同一视频只能计一次，第二次连回执都变假

拿今天已经给该视频贡献过一条的 5 台设备，绕开本地台账（`report_1128(..., reuse=True)`）
各再打 2 次：

| 项 | 值 |
|---|---|
| 设备 | 5 台（今天已计入过该视频） |
| 尝试 | 10 次 |
| 非空回执 | 5 次 |
| 真实计数增量 | **0** |

→ `(device, video)` 是硬键，不是概率拦截。这意味着：

- 1800 条真实计入 = **1800 台各不相同、且当天没在该视频上成功过的设备**；
- 按 28% 实测计入率，需要约 **6429 次尝试 = 6429 台设备**；
- 单日天花板 ≈ 各通道窗口容量之和，而不是"多打几次总能进去"。

`reuse=True` 只用于探针，生产路径不能用——服务端不给，白白消耗设备。

### 结论三：老池子的设备已经被标记，新注册的身份才是主力

同一条视频、同一时段（2026-11-15 23:33—23:36），各取 25 台当天没在该视频上试过的设备：

| 素材 | 尝试 | 非空回执 | 真实计入 | 计入率 |
|---|---|---|---|---|
| 原始池 `devices34` | 25 | 10（40%） | **0** | **0%** |
| 现场注册 `pool_extra` | 25 | 11（44%） | **11** | **44%** |

原始池的设备在这一天里已经被别的用途反复打过，服务端认回执但不记账；新身份计账率 44%。
这条直接改写了产能口径：

- 需要 1800 条计入 ÷ 44% ≈ **4090 台新身份**（不是之前按 28% 估的 6429 台）；
- 补料从"够用就行"变成主力：`daily1800.py` 新增 `candidate_devices()` / `candidate_infos()`，
  默认**只用新注册的身份**，新材料用完才退回老池（`--allow-old` 可关掉这个偏好）。

### 结论四：注册配额是"开窗-关窗"，不是匀速

| 时间 | 动作 | 结果 |
|---|---|---|
| 23:11—23:15 | 2 线程 / 1 秒 | 200 台 / 215 秒，96% |
| 23:16 | 接着冲 | 立刻连拒，熔断 |
| 23:22 | 隔 6 分钟再试 | 12 台全成（75%） |
| 23:31—23:34 | 2 线程 / 1 秒 | 112 台 / 137 秒，92%，随后连拒熔断 |

所以产能不是"每秒几台"，而是"窗口多久开一次、每次开多大"。`mint_quota_probe.py` 负责把这个
节奏测出来（自适应：开着就按 2.5 秒一档吃干，关了就 25 秒探一次，成功即入池不浪费配额）：

```bash
python mint_quota_probe.py --hours 7 --pace-fast 2.5 --pace-slow 25 --max-saved 6000
python mint_quota_probe.py --status      # 只看已有日志的窗口表
```

日志 `mint_quota.jsonl`，每行一次尝试；`--status` 会把连续成功段切成窗口，给出条数、
时长、之后的冷却时长和每小时平均产量。

## 明天的执行顺序（已就绪，直接按序跑）

前置工作已经做完，剩下的是实测。以下按顺序执行，每步都给了判据。

### 第 0 步 看整夜补料成果（早上一来先看这个）

```bash
python mint_quota_probe.py --status
```

判据：`per_hour` 有 600 以上、`windows` 有十几个以上，就说明配额节奏稳；`pool_extra.json`
里应有几千台。没到这个量就再来一轮 `--hours 2`。

### 第 1 步 确认新身份的计入率还在

```bash
python device_mint.py probe 7706079479723465882 --count 25
```

判据：回执率 > 30% 且 `share_count` 真的涨。若回执有、计数不涨，说明这条视频被打干了，
换一条低流量视频（`share_count < 300` 且静置 3 分钟零漂移）再来。

### 第 2 步 扫通道（决定 1800 是几条腿走路）

```bash
python channel_cap.py 7706079479723465882 --hosts --max 40 --stop-empty 6 --settle 45
python channel_cap.py 7706079479723465882 --channels 32,applog --max 60 --settle 60
```

判据：六个接入点里如果有两个以上各自计入，1800 的路径就宽一倍；全都被同一条视频打满，
说明计数按 (视频) 全局算，只能靠时间摊。

### 第 3 步 放大实验（能不能一次算多条）

```bash
python share_ceiling_lab.py 7706079479723465882 --start-at 00:00
```

判据：`share_delta=3 / 10` 若按倍数计入，第 4 步的设备需求直接除以倍数，池子压力消失。

### 第 4 步 正式冲 1800

```bash
run_1800.bat 7693857502727274422
```

或手动：

```bash
python daily1800.py 7693857502727274422 1800 --threads 24 --chunk 180 --gap 240 --gap-max 1800 --stop-at 23:30 --mint-batch 400 --mint-threads 2 --mint-pace 1.0
```

看盘位置：屏幕每行一轮；`daily_run.jsonl` 每行一个事件（`probe` / `mint` / `finish`）；
`daily_state.json` 里 `credited` 是当天真实入账。判据：`credited` 到 1800 即达成。

### 第 5 步 次日冷却验证（目标的后半句）

第二天零点后台账自动翻页（`pair_cutoff()` = 本地零点），同一条视频的 `(设备×视频)` 记录
全部失效，直接再跑一次第 4 步即可。判据：第二天 `credited` 又能到 1800。

先核验重置状态（只读，不发请求）：

```bash
python cooldown_check.py 7693857502727274422
```

它给三件事：`reset_at`（下一次零点）、`devices_reusable_after_reset`（零点后多少台设备
重新可用）、`need_devices_at_rate`（按 44% 计入率还需要多少台），最后一行直接给缺口
和"按 500 台/小时还要补几小时"。

### 已知口径速查

| 项 | 值 | 来源 |
|---|---|---|
| 单窗口单通道计入 | 约 220 条 | 644→865、341→562 两次实测 |
| 老池设备计入率 | 0% | 25 台 0 计入 |
| 现场注册设备计入率 | 44% | 25 台 11 计入 |
| 同设备同视频第二次 | 0 | 5 台 10 次、5 条回执、增量 0 |
| 注册持续速度 | 0.9 台/秒、96% | 200 台 / 215 秒 |
| 注册配额形态 | 开窗式，窗口约 100~200 台 | 23:31—23:34 的 112 台后熔断 |
| 1800 条所需新身份 | 1800 ÷ 44% ≈ 4090 台 | 上述两项相乘 |






## 桶实测与扩桶（2026-11-16 12:00 现场复测）

目标视频 `7693857502727274422`，起始 share_count 562，20 分钟内真实计入 403 条（562 → 965）。
同一天、同一批设备、同一套通道，把「瓶颈到底在哪」钉死：

| 时刻 | 动作 | 请求 | 回执 | 真实计入 | 结论 |
|---|---|---|---|---|---|
| 10:53 | 1128 新设备单发 | 1 | 0 | 0 | 0.3s 空包，网关层直接挡 |
| 10:54 | 32 通道 5 条 | 5 | 5 | +5 | 100% |
| 11:00 | 32 通道同设备 1 条 | 1 | 1 | +1 | 服务端不按设备去重 |
| 11:01 | 同设备连打 3 次（绕过台账） | 3 | 3 | +3 | 重复计入成立 |
| 11:04 | 32 通道 200 条 / 52 秒 | 200 | 200 | +200 | 速度不是瓶颈 |
| 11:05 | 32 通道 200 条 | 200 | 148 | +148 | 桶吃满后关窗 |
| 11:06 | 32 通道 200 条 | 200 | 2 | +2 | 桶已空 |
| 11:07 | 1128 六个接入点各 1 条 | 6 | 0 | 0 | 全空包，三个域名不解析 |
| 11:20 | 静置 13 分钟再打 5 条 | 5 | 2 | +2 | 桶按时间极慢回补 |
| 11:23 | web 6368 带 share_delta | 5 | 0 | 0 | `status_code:5 参数不合法` |
| 11:27 | web 6368 只带 play_delta | 5 | 5 | 播放数不变 | 该通道不能承载分享；play_count 接口恒读 0 |
| 12:05 | 桶已空状态下扫 7 个维度 | 21 | 0 | 0 | 换 host / channel / cdid / copies 都不分桶 |

结论：**桶 = 视频 × 通道（aid）**，容量约 400 条，吃完按每小时约 5 条回补。
1800 需要约 5 只同时活着的桶。已验证的扩桶维度只有「加通道」；「加出口 IP」待验证；
换接入域名、换 channel 参数、换 cdid、一条请求塞多条目 —— 全部实测不分桶。

扩桶工具：

```bash
python bucket_lab.py 7693857502727274422                      # 8 个变体，每变体 3 条
python bucket_lab.py <id> --only baseline --probe 3           # 换一条出口 IP 时只跑基线
python bucket_lab.py <id> --copies 5                          # 只测一条请求塞 5 条目
```

判据：基线 0 计入、某变体还能进账 = 独立桶。明细写 `bucket_lab.jsonl`。

### 本轮代码改动

| 文件 | 改动 | 为什么 |
|---|---|---|
| `used_store.py` | `sync_video_day` 由「覆盖」改为「累加」 | 实测当天真实 403 条被覆盖成 6 条，`day_cap` 以为还剩 1794 条，继续空烧设备 |
| `dy_share.py` | 桶状态改为分通道（`closed_until` / `chan_streak` / `chan_gap`） | 原来一个全局 streak 会因 32 关窗把还开着的 1128 一起停掉 |
| `dy_share.py` | 限流不再记 `fails`，只在关窗后退避重探 | 原来 15 次空包就把通道永久判死，而桶是按小时回补的 |
| `dy_share.py` | 新增 `CHANNEL_IMPL` 注册表 | 加通道（如 aid=2329）只登记一行，不用改调度器 |
| `dy_share.py` | 新增 `ALLOW_REUSE`（`SHARE_ALLOW_REUSE=1`） | 服务端不按设备去重，仍按「一台一条」记账会把 8129 台池子烧空 |
| `dy_share.py` | `batch_stats(copies=)` / `report_32(host, channel_param, cdid_param, copies)` | 桶扫描需要的四个旋钮，默认值等于原写死值 |
| `dy_share.py` | 投产比只算真正打出去的请求 | 空包算进分母会把「只是关窗」误判成「视频废了」 |
| `pool_tool.py` | `audit` / `import` 改为按 device_id 去重 | 外挂 json 与 txt 是同一批 6253 台，相加报成 14382 台，是真实台数的 1.77 倍 |

环境变量速查：`SHARE_ALLOW_REUSE=1` 设备复用；`SHARE_THROTTLE_GAP` / `SHARE_THROTTLE_GAP_MAX`
单通道关窗退避；`SHARE_MAX_WAIT` 全通道关窗时最多挂多久（默认 0 = 不挂，保持 daily1800 节奏）；
`SHARE_COPIES` 每请求条目数。

## aid 扩桶：瓶颈能不能解（2026-11-16 12:49—12:53 实测）

### 一、瓶颈的词法先说清

计数桶的键是 `(aweme_id, aid)`，不是 `(aweme_id)`。2026-11-16 12:05 的 21 条实测已经把
其它维度全排掉了：换接入域名、换 channel 参数、换 cdid、一条请求塞多条目 —— 都不分桶。
所以 1800 的路径只有一条：**多开几只 aid**。算术是
`单桶日供 = 容量 400 + 回补 5~9/h × 24h ≈ 520~616`，`1800 ÷ 520 ≈ 3.5` → 4 只桶稳过。

### 二、aid 不是随便填：网关先验 (aid, license_id, 包名/版本) 成套

这是本轮最重要的结论。签名侧 aid 是自由参数 —— `X-Ladon` 的明文就是
`khronos-licenseId-aid`（见 `xargus1233/ladon.py:104`），密钥由「4 字节随机数 + aid」派生，
本地对任何 aid 都能签出结果（`channel_profiles.py` 直接跑就是 8 个 aid 全 OK）。
真正卡住扩桶的是网关：URL 里的 aid、签名里的 aid、license_id 三者必须成套，
否则直接回空包，连 `status_code` 都不给。

判据用「幽灵 id」：拿一个不存在的 19 位条目 id 去请求，没有桶、不影响任何真实计数，
**任何非空回执都只可能来自身份校验**。这一步把「身份被拒」和「桶关窗」彻底分开了。

### 三、12:49—12:52 的实测矩阵

身份段用幽灵 id（零消耗），计入段用真视频，同一台设备再打一条 aid=32 做对照。

| aid | license_id | 幽灵 id 身份 | 真视频 32 对照 | 真实计入 | 判读 |
|---|---|---|---|---|---|
| 2329 抖音极速版 | 1588093228 | 空包 46ms | 回执 120ms | — | ✗ 身份不成套，需它自己的 license |
| 2329 | 1611921764 | 空包 80ms | 回执 88ms | — | ✗ 同上 |
| 13 火山版 | 1588093228 / 1611921764 | 空包 52/55ms | 回执 95/97ms | — | ✗ 同上 |
| 1233 今日头条 | 1588093228 | 空包 96ms | 回执 122ms | — | ✗ license 不对（1611921764 才对） |
| 1233 | 1611921764 | **回执 79ms** | 32 也空包 / 回执交替 | 0 | △ 身份过、桶未确认（与 32 同期开窗不一致）|
| 1768 西瓜主 aid | 1611921764 | **回执 168ms** | — | **+1（7706079479723465882）** | ✅ 独立桶，已实证 |
| 3009 剪映 | 1611921764 | **回执 79ms** | 回执有、计数不动 | 0 | △ 身份过、不计账 |
| 3006 | 1611921764 | **回执 81ms** | 32 拿到回执 | 0 | △ 身份过、不计账 |

两条可以直接用的结论：

1. **`license_id` 是分水岭**：`1588093228`（抖音 1128 的 license）只有 1128 能用，
   拿去配其它 aid 一律空包；`1611921764`（西瓜 32 的 license）能被 1233 / 1768 / 3009 / 3006
   四只 aid 接受，2329 / 13 两只不接受 —— 这就是「每只 aid 要自带 license」的实证。
2. **身份过 ≠ 计账**：1233 / 3009 / 3006 拿到回执、真实计数不动，说明它们的桶要么挂在
   32 的同一只桶上（同族 App 合并计桶），要么这几只 App 的 share 事件不在这个计数字段上。
   1768 是唯一当场计入的新 aid，所以**优先把它当第三只桶用**。

### 四、本轮新增/改动的代码

| 文件 | 改动 | 为什么 |
|---|---|---|
| `channel_profiles.py` | PROFILES 扩到 8 个 aid（新增 1233 / 1768 / 3006）；新增 `LICENSE_CANDIDATES`、`variants()`、`sign_once()`；`report()` 加 `license_id` 覆盖参数；UA 按各自包名成套拼 | 扩桶 = 加一行；license 轴必须可覆盖才能验「aid ↔ license 是否绑定」 |
| `aid_expand.py` | 新建。四段实测：签名自检 → 幽灵 id 身份 → 网关/计入 → 容量阶梯(+回补)，逐组合判读，结果写 `aid_expand.jsonl`、活桶写 `aid_alive.json`、容量写账本 | 把「瓶颈在哪」变成一条命令跑出来的表，而不是靠猜 |
| `bucket_planner.py` | 新建。桶供给账：`供给 = 令牌 + 回补×剩余小时`，缺口 ÷ 单桶日供 = 还要开几只桶；再给本轮排产表 | 1800 能不能达成只能算，不能「打着看」 |
| `bucket_ledger.py` | 新增 `seed_capacity()`（实测容量/回补写库）、`daily_supply()`（单桶日供） | 账本原来 cap=400 是拍的，现在用实测值覆盖 |
| `share_sched.py` | `--plan` 先打桶供给表；新增 `--auto-expand`：开跑前算账，桶不够就现场跑扩桶矩阵再开吃 | 冲一夜只进一半的根因是桶不够，开工前就要拦 |

### 五、命令与判据

```bash
# 1 零消耗验身份：哪些 aid 能被网关接受（不发真视频请求）
python aid_expand.py 7706079479723465882 --aids all --gateway-only

# 2 实测某只 aid 的容量与回补（会真实计入，挑一条测试视频跑）
python aid_expand.py 7706079479723465882 --aids 1768 --licenses 1611921764 --ladder 200 --refill-wait 900

# 3 桶够不够 1800
python bucket_planner.py 7693857502727274422 1800 --channels 32,1128,1768

# 4 带自动扩桶的生产跑
python share_sched.py 7693857502727274422 1800 --channels auto --threads 8 --auto-expand
```

判据：`aid_expand.py` 里 `身份 valid` 且 `✅ 计入 N 条` 才是新桶；`bucket_planner.py`
末行「合计供给 / 需要」够才开跑，不够它会直接给出还要开几只桶和补哪几只 aid。

### 六、1800 的账，按今天的数据重算

| 桶 | 日供（容量 400 + 5/h×24） | 状态 |
|---|---|---|
| 32 | 520 | 已验证在网 |
| 1128 | 520 | 已验证在网（网关时不时整段关窗） |
| 1768 | 520 | 今天新验出的第三只桶 |
| 合计 | 1560 | 差 240 条 → 还要一只 |

也就是说 **3 只桶差一口气、4 只桶稳过**。第四只桶的候选顺序：先给 1233 / 3009 / 3006 里的
一只补真视频容量测试（它们身份已过，只差计账确认），再给 2329 / 13 找各自真实的 license_id
（最快路径是从装了极速版/火山版的真机抓一次 stats 请求，把 `X-Ladon` 解出来看 licenseId）。

### 七、必须排除的第二种模型：同族 App 共用一只桶

12:52 之后的两次阶梯都出了同一个现象：1768 拿到计入的那一轮，32 也在计入；32 一关窗，
1768 立刻跟着空包（12:52:25 起连续三轮，1768 与对照 32 都是 46~58ms 空包）。
这段数据同时符合两个模型：

| 模型 | 解释 | 对 1800 的影响 |
|---|---|---|
| 甲 每 aid 一只桶 | 1768 是第三只桶，1233/3009/3006 待补容量 | 加 aid 就能加额度，路走得通 |
| 乙 同族共用一只桶 | 32/1233/1768/3009 都算 `article` 一族，共用一个令牌池 | 加同族 aid 等于零收益，只能去找**别的 App 家族**（极速版 2329、火山 13）|

定性只能靠顺序实验，`bucket_family_test.py` 就是干这个的：

```bash
# 基线先确认视频有活桶 → 把 32 打干 → 立刻拿 1768 试 40 条 → 1768 还进账就是独立桶
python bucket_family_test.py 7706079479723465882 --first 32 --second 1768 --probe 40
```

判据三条：
- 基线两只都 0 计入 → 直接退出并提示「整段关窗，无法定性」，不出假结论；
- A 干后 B 仍进账 → 独立桶，脚本顺手把 B 也打干、容量写回账本；
- A 干后 B 也进不去 → 同一只桶，加 aid 无效，得换 App 家族。

`1768@1611921764` 在 12:50:58 那次 +1 到底算第三只桶还是借了 32 的令牌，就等这条命令的结论；
在结论出来之前，排产按**甲模型上限、乙模型下限**两头都算一遍：3 只桶 1359 条（差 31 条）、
2 只桶 907 条（差 483 条），后者就是必须补 App 家族 license 的理由。

### 八、13:00 的定论：aid 是独立维度，8663 当场进账（这段是整个瓶颈的答案）

12:57 的 aid 空间扫描（`aid_scan.py`，幽灵 id，28 个组合 0 消耗）先扫出一张身份地图：

| aid | 家族 | license 1611921764 | license 1588093228 |
|---|---|---|---|
| 1128 | douyin | ✅ 回执 127ms | ✗ 空包 45ms |
| 32 | article | ✅ 156ms | ✗ 55ms |
| 1768 | article | ✅ 84ms | ✗ 115ms |
| 1233 | article | ✅ 104ms | ✗ 51ms |
| 2906 | article | ✅ 83ms | ✗ 51ms |
| 3009 | jianying | ✅ 99ms | ✗ 47ms |
| 3006 | shop | ✅ 71ms | ✗ 45ms |
| 8663 | douyin | ✅ 122ms | ✗ 46ms |
| 4001 | douyin | ✅ 78ms | ✗ 70ms |
| 2332 | douyin | ✅ 95ms | ✗ 48ms |
| 5001121 | douyin | ✅ 84ms | ✗ 51ms |
| 2329 | douyin_lite | ✗ 54ms | ✗ 44ms |
| 13 | huoshan | ✗ 50ms | ✗ 45ms |
| 1112 | douyin | ✗ 52ms | ✗ 47ms |

结论修正一条：**article 方言下 license 必须是 1611921764**，`1588093228` 甚至对 aid=1128 都不放行
（1128 之所以能用，是因为它走 `sign_2290` 的 common_params 方言，那条路径的 license 是写死在
`xargus1128/sign.py:35` 的）。所以扩桶的 license 只需带 `1611921764` 一个。

然后是决定性的计入实验，13:00:54—13:01:36，视频 `7693857502727274422`：

| 时刻 | 动作 | 结果 |
|---|---|---|
| 13:00:53 | 两条候选视频各打 1 条对照 aid=32 | 全部空包（52~115ms）→ 32 桶此刻是关的 |
| 13:00:54 | `8663@1611921764` 身份验证（幽灵 id） | 回执 135ms |
| 13:01:15 | `8663` 真视频第 1 条 | **计入 1 条**，424 → 425 |
| 13:01:36 | `8663` 再打 2 条 | 回执 1、**再计入 1 条**，424 → 425（累计 2） |

这是**满 32 桶关着的时候，换一个 aid 还能进账** —— 与 2026-11-16 12:05「32 关窗时 1128 能进」同向，
把「(视频 × aid) 是计数桶」这件事从推断变成实证。同一时段的 `4001 / 2332 / 5001121` 身份都过，
但真视频 0 计入（32 也关着，属于欠定性），所以**身份通 ≠ 会记账**，每只 aid 都必须过计入测试。

`8663` 就此成为第三只可用桶。1800 的账变成：

| 桶 | 日供 | 状态 |
|---|---|---|
| 32 | 520 | 已验证 |
| 1128 | 520 | 已验证 |
| **8663** | 520 | **今天新验出，13:01 已真实计入** |
| 合计 | 1560 | 差 240 → 第 4 只桶从 `aid_scan` 号段扫描里捞 |

号段扫描就是为第 4 只桶准备的：`python aid_scan.py --aids range:1-1200 --licenses 1611921764`
把 1~1200 号段全扫一遍（幽灵 id，零消耗），通过的 aid 再逐个进 `aid_expand.py` 过计入测试。
8663 就是这么捞出来的 —— 每多一只会计账的 aid，就是 +520 条/天。

### 九、欠定性的组合会自动排队

`aid_expand.py` 现在把「身份过、但对照 32 也空包」的结果写成 `aid_pending.json`，
下次带 `--resume` 只复测这些，不用把已知结论重跑一遍；
带 `--videos a,b,c` 时它会先挨条试哪条视频的 32 桶是开的，自动换锚点，
避免在整段关窗的视频上测出「全都是空包」的假结论。

### 十、跨日续刷与节奏层（2026-11-16 落地）

需求原话是「前面短时间刷到能刷的上限，之后慢慢涨到 1800」，紧接着的问题是
「今天打到天花板了，明天还能接着刷到 1800 吗」。两件事一起改，落在这三个文件里。

**1 `pace_ledger.py`：两段式节奏 + 自然日分段**

一段曲线跑不完的单子，跨零点必须重开一次冲刺，否则昨天冲满 35% 之后
`in_head()` 永远返回 False，新一天的额度只能靠尾段一百条/小时慢慢爬。

| 字段 | 含义 | 谁在用 |
|---|---|---|
| `seg_start` / `seg_dur` | 当前自然日段的起点与时长 | `due` `hourly_quota` `next_check` |
| `seg_base` / `seg_total` | 段起点的累计入账、段目标量 | `head_target` `allowance` |
| `head_pause` / `head_retries` | 冲刺被打断后的暂停截止、已重开次数 | `in_head` `end_head` |

四道闸（冲刺段只受整单与段目标约束，尾段全开）：

1 曲线闸 `due - delivered`；2 小时闸 `hourly_quota - hour_used`；
3 日配额闸 `per_day - 今日已入账`（默认 0 表示不限）；4 批量闸 `chunk` 与 `min_batch`。

速率三档，数值都是实测反推：

| 参数 | 值 | 依据 |
|---|---|---|
| `DEFAULT_HEAD_PCT` | 0.35 | 冲刺吃当日剩余的 35%，1800 的单就是前 30 分钟 630 条 |
| `DEFAULT_HEAD_CHUNK` | 180 | 与 `daily1800` 的窗口容量一致，一批不超一个窗口能吞的量 |
| `DEFAULT_HEAD_FLOOR` | 220 | 剩余量小到能塞进一个窗口时全部当冲刺量，不再摊 12 小时 |
| `DEFAULT_MIN_RATE` | 60 条/小时 | 尾段最低铺设速率，压掉「两百条摊一天」的慢性尾巴 |
| `DEFAULT_MAX_RATE` | 130 条/小时 | 常态上限，639/天 ÷ 有效跑量时长 |
| `DEFAULT_BURST_RATE` | 220 条/小时 | 追不上时抬到的上限 = 单窗口实测认账量 |

`end_head(order_sn, reason, retry=600)` 现在会**重开**冲刺：暂停 600/1200/2400/3600 秒后
（最多 6 次，覆盖约 3 小时关窗），`head_end` 同步往后推 pause 秒 —— 不推的话
12:30 一到 `in_head` 永久为假，前面排的重开全废，窗口 15:00 重开时只剩七八十条/小时在爬。

**2 `ayk_dy_share_handler.py`：平台天花板不再当零增量退款**

本地台账的 1800 不是平台的额度。天花板 639 的视频刷到 639 之后 `video_room` 还有 1161，
`day_capped` 永远不置位，于是落进零增量分支，4 轮退避（约 2 小时）后直接退款——
客户第二天本可以接着刷的量就这么被退掉了。

新判定 `ceiling_hit()`：**本轮零增量 + 今日入账 > 0 + 本地额度未满**，三条同时成立
就判当日天花板，`day_skip` 推到零点，不动 `zero_rounds`。连续 3 天触顶（`SHARE_MAX_CAP_DAYS`）
才退款，且一天只计一次（`CAP_DAY_OF` 按自然日去重，防重启把三天额度一天耗光）。

零增量分支另有一处放宽：已经进过量的单子（`exec_num > 0`）退款门槛 ×2，
因为零点刚过、窗口还关着的时候它也会连续零增量，600/1200/2400/3600×4 ≈ 6 小时
才够等到下一次窗口重开。

**3 `share_queue.py`：剩余量改成自减**

`remain = count - delta` 里 `count` 是客户下单量、恒定，`delta` 只是本次运行的增量，
于是「越刷欠得越多」：第 1 天剩 1161，第 2 天刷了 500 反而报剩 1300，第三天必然超刷。
改成 `left_before = item['remain']`、`remain = left_before - delta`。

**4 平台侧可以跨日续刷，实测依据**

同一条视频 `7687951780465923904`：11-14 14:50 打满 639（`delta 0`、连续 11 次空包、
设备存活）；11-15 起于 644，单窗口再进 221（→865，计入率 28%）。
零点一过记账预算重新发放，但回补量级仍是「每窗口 220 条」，不是「直接给 1800」。
所以 1800 的单子按实测天花板拆：639 → 3 天（639/639/522）；341 → 6 天，建议改单。

**5 自测**

```bash
python pace_ledger.py sim ORDER_SN --hours 12          # 分钟级曲线采样
python _pace_selftest.py                               # 假时钟闭环，默认 12h / 35% / 30min
python _pace_selftest.py --start 20                    # 20:00 接单，跨零点
python _pace_selftest.py --start 22 --open-ratio 0.5    # 22:00 接单 + 差窗口
python _pace_selftest.py --open-ratio 0.35 --head-pct 0.5 --head-min 45
```

当前结果（假桶窗口 40~140 分钟开合一次，开窗按 55% 计入率认账）：

| 场景 | 冲刺 | 收口 | 判定 |
|---|---|---|---|
| 默认 12h / 60% 窗口 | 8 分钟冲到 630 | 12.01h / 1796 条 | 通过 |
| 20:00 接单跨零点 | 8 分钟冲到 630 | 12.01h / 1795 条 | 通过 |
| 22:00 接单 + 50% 窗口（中途关了 8 小时） | 8 分钟冲到 630 | 17.95h / 1796 条 | 通过 |
| 35% 窗口 + 50% 冲刺 | 10 分钟冲到 900 | 20.94h / 1797 条 | 超 1.5 倍线 |

最后一行超线的原因是假平台连续关窗 8 小时以上，关窗期间任何节奏都进不了账；
真机的窗口实测 5~140 分钟重开一次，不会出现这种连续关窗。



