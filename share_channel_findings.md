# 抖音 share_count 三通道实测记录

测试时间：2026-11-12 20:14 — 20:47（Asia/Shanghai）
运行环境：`D:\jiedan\.venv312`（Python 3.12.7，httpx 0.28.1 / gmssl 3.2.2 / snowland-smx 0.3.1）
脚本：`D:\jiedan\dy_share_three.py`（470+ 行，编译通过，可运行）

---

## 1 端点存活矩阵

| 通道 | host | 路径 | HTTP | 回执 | 判定 |
|---|---|---|---|---|---|
| 1128 抖音 Android | api26-normal-hl.amemv.com | `/aweme/v1/aweme/share/` | 200 | `message: Url does not match` | 死：路径不在该 aid 网关注册表 |
| 1128 抖音 Android | api26-normal-hl.amemv.com | `/aweme/v1/share/aweme/` | 200 | `status_code:1, Url does not match` | 死 |
| 1128 抖音 Android | api26-normal-hl.amemv.com | `/aweme/v1/aweme/stats/` | 200 | 空 body | 通但无回执；原版 `play1128` 同样空 → 设备池疑似整体失效 |
| 32 西瓜 Android | api5-platform-m-hj.amemv.com | `/aweme/v1/aweme/share/` | 404 | TLB 404 页 | 死：host 不路由 |
| 6383 PC Web | www.douyin.com | `/aweme/v2/web/aweme/share/` | 404 | `Unsupported path(Janus)` | 死 |
| 6383 PC Web | www.douyin.com | `/aweme/v2/web/aweme/stats/` | 200 | 见字段矩阵 | 路径活 |
| applog | log.snssdk.com | `/service/2/app_log/` | 200 | `{"blocklist":{...}}` | 活：事件可投递 |
| applog | mcs.snssdk.com | `/service/2/app_log/` | 404 | Not Found | 死：主机迁移到 log/applog |
| 读取 | 110.42.32.174:9001 | `/douyin/aweme/detail/play?aweme_id=` | 200 | `aweme_detail.statistics.share_count` | 活（下架视频返回 `filter_detail.core_dep`） |
| 读取 | www.douyin.com | `/aweme/v1/web/aweme/detail/` | 403 | `Blocked by ArgusSecurityPlugin Signature Not Found` | 需 msToken + X-Bogus，仅 a_bogus 不够 |

## 2 Web stats 字段接受度

| body 字段 | status_code | 含义 |
|---|---|---|
| `item_id` + `aweme_type`（无 delta） | 0 | 通过 |
| `play_delta=1` | 0 | 通过（播放通道生产可用，已交叉验证） |
| `share_delta=1` | 5 `参数不合法` | 字段名被识别但校验拒绝 |
| `share_delta=1&stats_type=1` | 5 `参数不合法` | 同上 |
| `share_count=1` | 0 | 假阳性：未知字段被静默忽略 |
| `action_type=share` | 0 | 假阳性：同上 |
| `share_xxx=1`（乱写） | 0 | 证明 0 ≠ 生效，只是忽略未知字段 |
| `share_delta=1`（在架低流量视频，连打 10 次） | 200 空 body | 无回执，计数不动 |

## 3 对照实验（决定性）

低流量样本：`aweme_id=7706079479723465882`，`share_count=187`，110 秒静置零漂移。

| 窗口 | 动作 | before | after | delta |
|---|---|---|---|---|
| A 静置控制 | 不发任何请求 | 187 | 187 | **0** |
| B applog | 20 条 `share_video` 事件，20 个不同 device_id | 187 | 187 | **0** |
| C web stats | 10 次 `share_delta=1`（a_bogus 全通） | 187 | 187 | **0** |

高流量样本 `7703872151216377585`（share≈422k）对照，噪声淹没信号，不可用：

| 窗口 | 动作 | delta |
|---|---|---|
| A 静置 | — | +29 |
| B applog ×20 | 20 | +28 |
| C web_delta ×10 | 10 | +23 |

→ 自然增长 ±29/100s，任何 ≤20 条的注入量都无法从噪声里区分。选样本必须挑 `share_count < 300` 且静置 100s 零漂移的视频，否则结论无效。

## 4 结论

1. 三条通道的传输层全部打通：`sign_2290`、`sign1233`、`get_ab` 签名被服务端接受，a_bogus 可让 web stats 返回业务码而不是风控码。
2. 客户端上报型写入对 `share_count` **无效**：applog 事件投递成功、web `share_delta` 通过风控，但计数一次都没动。
3. `share_delta` 这个字段在服务端是存在的（乱写字段返回 0，写 `share_delta` 返回 5），但被业务校验拦截 → 说明分享计数不是靠客户端 delta 自增，而是服务端对分享行为本身记账。
4. 因此播放通道那套「伪造 stats 上报」搬不过来。分享计数更可能绑定分享链路：发放 `share_id` / `u_code` → 落地页被打开 → 回调记账。要打这条链，必须先抓到一次真实分享请求。

## 4b 2026-11-13 复测：已抓到真实分享请求，计数点排除到只剩「投递」

### 登录态复用
| 尝试 | 结果 |
|---|---|
| 复制 profile（Local State + Network/Cookies）到独立目录 | 失败：cookie 为 `v20` 前缀，App-Bound Encryption 绑定原数据目录，解密失败后 Chrome 直接丢弃 |
| 加 `--disable-features=AppBoundEncryption` | 无效，仍然只有 25 个 cookie，无 `sessionid` |
| Playwright 直接开真实 profile | 失败：`DevTools remote debugging requires a non-default data directory` |
| **真实 profile + mitmproxy + HTML 注入** | **成功**：`im/user/info`、`history/write` 等登录态接口全部出现，可全自动点击 |

### 网页端分享链路（实测）
```
点 [data-e2e="video-player-share"]
  → 面板弹出，埋点 share_menu_show（click_method=icon, share_type=video）
  → 面板打开瞬间就预生成短链：
     GET /aweme/v1/web/web_shorten/?target=<iesdouyin share URL>&belong=aweme&persist=1&...&a_bogus=
     → {"code":0,"data":"https://v.douyin.com/JaO84tikcBM/"}
```

短链 target 里的关键参数：

| 参数 | 值（本次） | 作用 |
|---|---|---|
| `u_code` | `16i7244ha` | 分享者身份码 |
| `share_sign` | `KHk7IhUc0dtWKRHUR0ZqRF9dx_tb33gn0X.0o4KmJOs-` | 分享签名 |
| `mid` | `7706079367164005161` | 分享物料 id |
| `did` / `iid` | `MS4wLjABAAAA...` | 设备/安装 id |
| `ts` | `1794540529` | 分享时间戳 |
| `from` | `web_code_link` | 来源标记 |

### 计数排除实验（样本 `7706079479723465882`，share=187）
| 动作 | before | after | delta |
|---|---|---|---|
| 生成分享短链 1 次（web_shorten 成功返回 v.douyin.com 短链） | 187 | 187 | **0** |
| 用 iPhone UA 独立指纹打开该短链，落到 iesdouyin 分享落地页 | 187 | 187 | **0** |

→ 生成链接不算、打开链接也不算。`share_count` 记的是**投递成功**：IM 私信送达、或微信/朋友圈回调确认。
下一步只剩两个候选计数点：网页端「分享 → 私信」真实发出消息；App 端分享到微信后的回调。

## 5 下一步抓包配方（定位真实分享请求）

frida 挂 okhttp / Cronet，过滤含 `share` 的 URL，拿到真实 path + body + query：

```javascript
Java.perform(function () {
    var Builder = Java.use('okhttp3.Request$Builder');
    var RealCall = Java.use('okhttp3.RealCall');
    RealCall.execute.implementation = function () {
        var req = this.request();
        var u = req.url().toString();
        if (u.indexOf('share') >= 0 || u.indexOf('stats') >= 0) {
            console.log('[REQ] ' + req.method() + ' ' + u);
            var body = req.body();
            if (body !== null) {
                try {
                    var Buf = Java.use('okio.Buffer');
                    var b = Buf.$new();
                    body.writeTo(b);
                    console.log('[BODY] ' + b.readUtf8());
                } catch (e) { console.log('[BODY-ERR] ' + e); }
            }
        }
        return this.execute();
    };
});
```

Cronet / TTNet 走 native，okhttp 钩不到时改用 mitmproxy 系统代理 + 安装用户证书，抓 `*.amemv.com`、`*.snssdk.com`、`*.douyin.com`，只看 `POST` 且 body 含 `aweme_id` 的请求。

拿到真实请求后，把它填进 `CANDIDATES`（`path` / `body` / `status`），跑：

```
.venv312\Scripts\python.exe -X utf8 dy_share_three.py probe 7706079479723465882
```

## 6 命令

```
cd D:\jiedan
.venv312\Scripts\python.exe -X utf8 dy_share_three.py dump
.venv312\Scripts\python.exe -X utf8 dy_share_three.py baseline 7706079479723465882 5
.venv312\Scripts\python.exe -X utf8 dy_share_three.py probe    7706079479723465882
.venv312\Scripts\python.exe -X utf8 dy_share_three.py run      7706079479723465882 100 20
```
