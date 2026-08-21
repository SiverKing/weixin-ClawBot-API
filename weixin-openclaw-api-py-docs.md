# 微信 iLink/openclaw-weixin-api Bot Python 实现：协议分析与踩坑全记录

> 本文记录从零开始用 Python 实现微信 iLink Bot（ClawBot）API 的完整过程，包括协议分析、调试排查、关键踩坑点和最终可用代码。协议基线已更新到 `@tencent-weixin/openclaw-weixin@2.4.6`（npm `latest`，2026-06-22 发布），最后核对日期为 2026-08-19。

> 本文中的“协议”来自腾讯公开 npm 发布包和官方 GitHub 源码的实现行为，不等同于一份承诺长期稳定的正式开放 API 规范。实现独立客户端时应保留版本隔离、错误降级和重新登录能力。

---

## 一、背景

2026 年腾讯通过 OpenClaw 平台正式开放了微信个人账号的 Bot API，底层协议叫 **iLink（智联）**，接入域名为 `https://ilinkai.weixin.qq.com`，纯 HTTP/JSON，无需 SDK 可直接 `fetch` / `requests` 调用。

官方只发布了 Node.js 包（`@tencent-weixin/openclaw-weixin`），没有 Python 实现。本文通过分析公开 npm 包源码，在 Python 中复现这套 HTTP 协议。

---

## 二、协议逆向分析

### 2.1 信息来源

- 当前协议基线：`@tencent-weixin/openclaw-weixin@2.4.6`
- npm 版本页：[`@tencent-weixin/openclaw-weixin`](https://www.npmjs.com/package/@tencent-weixin/openclaw-weixin?activeTab=versions)
- npm 发布包：[`openclaw-weixin-2.4.6.tgz`](https://registry.npmjs.org/@tencent-weixin/openclaw-weixin/-/openclaw-weixin-2.4.6.tgz)，npm SHA-1：`c7744c5b2d0232703c886b2f4e71681b0170695d`
- 旧版对照基线：`1.0.2`、`2.0.1`、`2.4.3`、`2.4.5`
- 腾讯官方 GitHub 仓库：[`Tencent/openclaw-weixin`](https://github.com/Tencent/openclaw-weixin)
- 当前官方标签：`v2.4.6`
- npm dist-tag：`latest=2.4.6`、`legacy=1.0.3`
- 注意：2.4.6 随包 README 的兼容性表仍写着“2.0.x / OpenClaw >=2026.3.22”，但实际 `package.json` 已要求 `openclaw >=2026.5.12`。版本和宿主要求应以发布包 `package.json`、npm 元数据及 CHANGELOG 为准。
- 2.4.6 没有单独的 CHANGELOG 条目。对两个 npm tarball 逐文件计算 SHA-256 后，`src/` 36 个文件与 `dist/` 72 个文件全部一致；2.4.6 只更新版本元数据并从发布文件中移除了 `npm-shrinkwrap.json`。因此最新的实质性协议/行为变化来自 2.4.5。
- 源码目录结构：
  ```
  src/
  ├── auth/       # QR 码登录、账号存储
  ├── api/        # iLink HTTP API 封装（关键）
  ├── cdn/        # 媒体文件 AES-128-ECB 加解密 + CDN 上传
  ├── messaging/  # 消息收发、inbound/outbound 处理（关键）
  ├── monitor/    # 长轮询主循环
  ├── config/     # 配置 schema
  ├── media/      # 入站媒体解析、SILK 转码、MIME 识别
  └── storage/    # 状态持久化
  ```

#### 上游版本时间线

| 版本 | 发布时间 | 主要变化 |
|---|---|---|
| `2.0.1` | 2026-03-24 | 2.x 主线起点，要求 OpenClaw >=2026.3.22 |
| `2.1.1` | 2026-03-27 | 增加 `iLink-App-Id`、`iLink-App-ClientVersion`；媒体增加完整 URL 字段 |
| `2.1.3` | 2026-04-02 | 引入流式 Markdown 过滤器 |
| `2.1.9` | 2026-04-20 | 外发 `message_sending` / `message_sent` hook |
| `2.1.10` | 2026-04-24 | 首次引入启动/停止通知 |
| `2.3.1` | 2026-04-28 | `bot_agent`、`local_token_list`、配对码、`binded_redirect`、`notifystart/notifystop` |
| `2.4.1` | 2026-05-04 | 发布包携带预编译 `dist`，兼容较新的 OpenClaw 插件加载机制 |
| `2.4.2` | 2026-05-07 | 修复 Node 24 手动 `Content-Length` 导致全部请求失败；修复新版宿主 runtime 注入 |
| `2.4.3` | 2026-05-08 | 修复生产构建中 iLink 应用请求头为空；修复 `binded_redirect` 被当成登录失败 |
| `2.4.4` | 2026-05-22 | 长轮询支持外部取消；增加工具调用进度消息 |
| `2.4.5` | 2026-06-22 | 网络错误分类、发送结果校验、明确 `-14` 为失效 token；最低宿主升到 2026.5.12 |
| `2.4.6` | 2026-06-22 | 发布包整理，无运行协议变化 |

### 2.2 完整 API 列表

| Endpoint | Method | 功能 |
|---|---|---|
| `/ilink/bot/get_bot_qrcode` | POST | 获取登录二维码（`?bot_type=3`），请求体携带 `local_token_list`；旧版 GET 只建议作为兼容回退 |
| `/ilink/bot/get_qrcode_status` | GET | 长轮询扫码状态（`?qrcode=...`，需要时追加 `&verify_code=...`） |
| `/ilink/bot/getupdates` | POST | 长轮询收消息（核心，服务器 hold 35s） |
| `/ilink/bot/getconfig` | POST | 获取账号配置和 `typing_ticket` |
| `/ilink/bot/sendtyping` | POST | 发送"正在输入"状态 |
| `/ilink/bot/sendmessage` | POST | 发送消息 |
| `/ilink/bot/getuploadurl` | POST | 获取 CDN 预签名上传地址（媒体消息用） |
| `/ilink/bot/msg/notifystart` | POST | 通知服务端客户端已启动，最佳努力调用 |
| `/ilink/bot/msg/notifystop` | POST | 通知服务端客户端将停止，最佳努力调用 |

所有路径都拼接到登录响应的 `baseurl`；首次获取二维码固定使用 `https://ilinkai.weixin.qq.com`。二维码状态轮询遇到 `scaned_but_redirect` 后，才切换到 `redirect_host`。

### 2.3 请求头规范

2.4.6 的 POST CGI 请求头如下：

```python
{
    "Content-Type": "application/json",
    "AuthorizationType": "ilink_bot_token",
    "X-WECHAT-UIN": base64(str(random_uint32)),  # 每次 POST 请求随机生成
    "iLink-App-Id": "bot",
    "iLink-App-ClientVersion": "132102",         # 2.4.6 对应整数版本号
    "Authorization": f"Bearer {bot_token}",       # 登录后才有
}
```

说明：

- `X-WECHAT-UIN`：随机生成一个 uint32，转十进制字符串，再进行 base64 编码；每次 POST 请求重新生成。
- `Authorization`：获取二维码和轮询扫码状态时没有 token，不发送该字段；登录后的业务 CGI 才发送。
- `iLink-App-ClientVersion`：编码公式为 `(major << 16) | (minor << 8) | patch`，因此 2.4.6 为 `132102`。
- `SKRouteTag`：官方实现支持从配置读取后作为可选请求头，属于路由/调试能力，普通生产客户端不应自行伪造。
- GET 的二维码状态请求，官方 2.4.6 只发送公共应用头 `iLink-App-Id`、`iLink-App-ClientVersion`（以及可选 `SKRouteTag`）。额外发送无 token 的通用头通常可兼容，但不属于最新官方最小集合。
- 不要手动设置 `Content-Length`。2.4.2 已因 Node 24/undici 拒绝调用方预设该字段而移除，交给 HTTP 客户端自动计算。

每个业务 POST 请求体还应携带 `base_info`：

```json
{
  "base_info": {
    "channel_version": "2.4.6",
    "bot_agent": "weixin-ClawBot-API/1.2.0 (python)"
  }
}
```

`bot_agent` 只用于日志归因和监控聚合，不参与鉴权或路由。格式为一个或多个空格分隔的 `Name/Version` token，每个 token 可选跟随 `(comment)`；仅允许 ASCII，总长度不超过 256 字节。不合法 token 应丢弃，最终为空时官方回退为 `OpenClaw`。

### 2.4 完整消息流

```
登录流程：
  POST get_bot_qrcode?bot_type=3 { local_token_list }（固定入口）
  → 得到 qrcode + qrcode_img_content（通常为 URL）
  → GET get_qrcode_status（35s 长轮询）
  → 处理扫码、验证码、节点跳转、二维码刷新
  → status="confirmed" 时得到 bot_token / ilink_bot_id / ilink_user_id / baseurl

启动：
  POST msg/notifystart（最佳努力，不应因失败阻断消息循环）

收发消息流程（每条消息）：
  POST getupdates（长轮询，hold 35s） → 得到 msgs[]
  → 校验 ret / errcode，保存 get_updates_buf
  ↓ 收到用户消息
  POST getconfig（首次每用户调用一次，缓存 typing_ticket）
  POST sendtyping { status: 1 }  ← 显示"正在输入"
  ↓ 调用 AI 生成回复
  POST sendmessage（带完整字段并校验 ret）
  POST sendtyping { status: 2 }  ← 放在 finally 中取消"正在输入"

停止：
  取消正在进行的 getupdates 长轮询
  POST msg/notifystop（独立短超时、最佳努力）
```

---

### 2.5 登录协议与完整状态机

#### 2.5.1 获取二维码

最新官方实现始终从固定入口获取二维码：

```http
POST https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode?bot_type=3
Content-Type: application/json
```

请求体：

```json
{
  "local_token_list": ["<已有 bot_token，最多最近 10 个>"]
}
```

首次运行传空数组。`local_token_list` 用于让服务端判断扫描的账号是否已经绑定到当前客户端；它不是新的鉴权方式，也不能替代本地安全保存 token。

响应：

```json
{
  "qrcode": "<轮询状态时使用的二维码标识>",
  "qrcode_img_content": "https://liteapp.weixin.qq.com/q/..."
}
```

`qrcode` 是敏感的短期凭据，日志中应脱敏；`qrcode_img_content` 通常是二维码页面/图片 URL，不应直接当作 base64 图片写入文件。1.x 的 GET 获取方式只保留为兼容回退，不属于 2.4.6 主流程。

#### 2.5.2 轮询二维码状态

```http
GET /ilink/bot/get_qrcode_status?qrcode=<urlencoded qrcode>
GET /ilink/bot/get_qrcode_status?qrcode=<urlencoded qrcode>&verify_code=<urlencoded code>
```

官方客户端为单次状态长轮询设置约 35 秒客户端超时；网络超时、网关 524 等临时错误按 `wait` 处理后继续轮询。完整状态如下：

| `status` | 含义 | 正确处理 |
|---|---|---|
| `wait` | 等待扫码 | 保持当前二维码继续轮询 |
| `scaned` | 已扫码，正在手机端验证/确认 | 显示提示；若上次携带配对码，则说明配对码已接受并清除暂存值 |
| `need_verifycode` | 手机端要求数字配对码，或上次配对码错误 | 从终端读取数字，下次状态请求带 `verify_code` |
| `verify_code_blocked` | 多次输入错误被限制 | 清除配对码并刷新二维码；达到刷新上限后终止本轮登录 |
| `scaned_but_redirect` | 状态轮询需要切换 IDC 节点 | 读取 `redirect_host`，后续状态轮询改用 `https://<redirect_host>` |
| `binded_redirect` | 账号已绑定到本客户端 | 视为“已经完成”，继续使用本地已有 token；不能当作失败抛异常 |
| `expired` | 二维码过期 | 从固定入口重新申请二维码 |
| `confirmed` | 登录确认完成 | 读取并保存登录结果，结束轮询 |

`confirmed` 响应字段：

```json
{
  "status": "confirmed",
  "bot_token": "<Bearer token>",
  "ilink_bot_id": "<账号/机器人标识>",
  "ilink_user_id": "<扫码用户标识>",
  "baseurl": "https://<后续业务 API 节点>"
}
```

官方 2.4.6 客户端要求 `confirmed` 时至少能取得 `ilink_bot_id`，并把 `bot_token`、`baseurl`、扫码用户等按账号持久化。二维码会话本地 TTL 为 5 分钟，整体等待默认约 480 秒，二维码过期或配对码被限制时最多刷新 3 次；这些数值是当前官方客户端策略，不是服务端保证不变的协议常量。

独立客户端需要特别处理 `binded_redirect`：只有本地确实还保留有效 token 时，才能把它视为成功；如果本地没有可复用 token，应重新走登录流程，不能凭该状态构造 token。

### 2.6 业务 API 请求与响应

以下示例统一省略请求头。除特别说明外，请求体都应加入：

```json
{
  "base_info": {
    "channel_version": "2.4.6",
    "bot_agent": "weixin-ClawBot-API/1.2.0 (python)"
  }
}
```

#### 2.6.1 getupdates：长轮询收消息

请求：

```http
POST /ilink/bot/getupdates
```

```json
{
  "get_updates_buf": "<上次成功响应返回的完整游标；首次为空字符串>",
  "base_info": {
    "channel_version": "2.4.6",
    "bot_agent": "weixin-ClawBot-API/1.2.0 (python)"
  }
}
```

响应：

```json
{
  "ret": 0,
  "errcode": 0,
  "errmsg": "",
  "msgs": [],
  "get_updates_buf": "<下一次请求原样回传>",
  "longpolling_timeout_ms": 35000
}
```

字段规则：

- `get_updates_buf` 是当前主游标；旧字段 `sync_buf` 仅为 deprecated 兼容字段。
- 只在本次响应成功且新游标非空时替换并持久化本地游标。
- `longpolling_timeout_ms` 是服务端建议的下一次长轮询客户端超时；官方 2.4.6 会把正数值直接用于下一轮请求。
- 客户端自身超时属于长轮询正常控制流：保留原游标并继续下一轮，不应计为业务失败。
- `ret != 0` 或 `errcode != 0` 必须进入错误处理，不可把空 `msgs` 当作成功后立即无间隔重试。

#### 2.6.2 getconfig：获取 typing ticket

请求：

```http
POST /ilink/bot/getconfig
```

```json
{
  "ilink_user_id": "<from_user_id>",
  "context_token": "<当前入站消息的 context_token，可选但建议带上>",
  "base_info": {
    "channel_version": "2.4.6",
    "bot_agent": "weixin-ClawBot-API/1.2.0 (python)"
  }
}
```

响应：

```json
{
  "ret": 0,
  "errmsg": "",
  "typing_ticket": "<base64 ticket>"
}
```

`typing_ticket` 可按“微信账号 + 对端用户”缓存。重新登录、token 切换或服务端返回失效错误后应清空相关缓存。

#### 2.6.3 sendtyping：输入状态

```http
POST /ilink/bot/sendtyping
```

```json
{
  "ilink_user_id": "<目标用户>",
  "typing_ticket": "<getconfig 返回值>",
  "status": 1,
  "base_info": {
    "channel_version": "2.4.6",
    "bot_agent": "weixin-ClawBot-API/1.2.0 (python)"
  }
}
```

`status=1` 表示正在输入，`status=2` 表示取消。AI 调用和消息发送应放在 `try` 中，取消输入状态应放在 `finally` 中；即使 AI 或 `sendmessage` 失败，也要尽力发送 `status=2`。

#### 2.6.4 sendmessage：发送消息

```http
POST /ilink/bot/sendmessage
```

文本消息完整请求：

```json
{
  "msg": {
    "from_user_id": "",
    "to_user_id": "<目标用户 ID>",
    "client_id": "openclaw-weixin-<本次发送唯一 ID>",
    "message_type": 2,
    "message_state": 2,
    "context_token": "<当前会话上下文>",
    "run_id": "<可选：同一轮 agent/tool 运行 ID>",
    "item_list": [
      {
        "type": 1,
        "text_item": { "text": "回复内容" }
      }
    ]
  },
  "base_info": {
    "channel_version": "2.4.6",
    "bot_agent": "weixin-ClawBot-API/1.2.0 (python)"
  }
}
```

响应：

```json
{
  "ret": 0,
  "errmsg": ""
}
```

从 2.4.5 起，官方客户端明确解析 `SendMessageResp`：只有 HTTP 成功、JSON 可解析且 `ret` 为 `0`（或字段缺省）时才认定为成功；`ret != 0` 必须抛错或返回失败，不能仍打印“已回复”。`client_id` 应在每次发送时唯一，可作为本地消息 ID 使用。

`context_token` 的规则：

- 回复当前入站消息时，原样使用该消息的 token。
- 主动给已建立会话的用户发消息时，使用按“账号 + 用户”保存的最近有效 token。
- 不得把 A 用户、A 账号或历史已失效的 token 用到另一条会话。

#### 2.6.5 notifystart / notifystop：生命周期通知

启动：

```http
POST /ilink/bot/msg/notifystart
```

停止：

```http
POST /ilink/bot/msg/notifystop
```

请求体均为：

```json
{
  "base_info": {
    "channel_version": "2.4.6",
    "bot_agent": "weixin-ClawBot-API/1.2.0 (python)"
  }
}
```

响应均为 `{ "ret": 0, "errmsg": "" }` 形式。它们用于服务端在线状态对账，官方客户端按最佳努力处理：启动通知失败只告警，不能阻止消息循环；停止通知应使用独立短超时，避免长轮询取消信号同时把通知也取消。

#### 2.6.6 getuploadurl：媒体上传凭据

```http
POST /ilink/bot/getuploadurl
```

完整请求字段：

```json
{
  "filekey": "<随机 16 字节的 hex>",
  "media_type": 1,
  "to_user_id": "<目标用户>",
  "rawsize": 12345,
  "rawfilemd5": "<明文 MD5 hex>",
  "filesize": 12352,
  "thumb_rawsize": 1024,
  "thumb_rawfilemd5": "<缩略图明文 MD5 hex>",
  "thumb_filesize": 1040,
  "no_need_thumb": false,
  "aeskey": "<16 字节 AES key 的 hex>",
  "base_info": {
    "channel_version": "2.4.6",
    "bot_agent": "weixin-ClawBot-API/1.2.0 (python)"
  }
}
```

`media_type`：`1=IMAGE`、`2=VIDEO`、`3=FILE`、`4=VOICE`。不需要缩略图时设 `no_need_thumb=true` 并省略三个 `thumb_*` 字段。

响应：

```json
{
  "upload_param": "<旧式上传加密参数>",
  "thumb_upload_param": "<缩略图参数，可选>",
  "upload_full_url": "<服务端直接返回的完整上传 URL，优先使用>"
}
```

客户端应优先使用 `upload_full_url`；不存在时才根据 `upload_param`、`filekey` 和 CDN 基础地址拼接旧式上传 URL。

### 2.7 错误语义、超时、退避与状态持久化

#### 2.7.1 三层成功条件

每个请求都要依次满足：

1. 网络请求成功，没有 DNS/TCP/TLS/超时错误；
2. HTTP 为 2xx，响应体可解析为 JSON；
3. 业务层 `ret` / `errcode` 为 0 或缺省。

不能只凭 HTTP 200 判断消息已经投递。日志应记录脱敏后的 URL、HTTP 状态、`ret`、`errcode`、`errmsg`，不得打印完整 `bot_token`、二维码标识、`typing_ticket`、`context_token`、CDN 加密参数或 AES key。

#### 2.7.2 当前官方默认超时

| 请求类型 | 2.4.6 默认值 |
|---|---:|
| `getupdates` 长轮询 | 35 秒，并动态采用服务端 `longpolling_timeout_ms` |
| `sendmessage` / `getuploadurl` | 15 秒 |
| `getconfig` / `sendtyping` / 生命周期通知 | 10 秒 |
| `get_qrcode_status` 单次长轮询 | 35 秒 |

#### 2.7.3 token 失效：`-14`

2.4.5 将内部命名从“session expired”改为“stale token”，明确 `ret=-14` 或 `errcode=-14` 表示 bot token 已失效/过期，而不只是普通会话超时。官方插件会暂停该账号全部请求 1 小时，避免快速重试打满接口。

独立客户端可采用更适合自己的策略：立即停止业务请求，通知用户重新登录；如果具备安全保存的账号 token 和交互渠道，可启动受控重连。无论选择哪种方式，都不能在 `-14` 后继续无间隔轮询。

#### 2.7.4 网络错误分类与退避

官方 2.4.5 将 fetch 级错误分类为：

| 类别 | 典型错误 |
|---|---|
| `dns` | `ENOTFOUND`、`EAI_AGAIN`、`getaddrinfo` |
| `tcp` | `ECONNREFUSED`、`ETIMEDOUT`、`ENETUNREACH`、`EHOSTUNREACH`、连接超时 |
| `tls` | SSL/TLS/CERT、证书校验、socket TLS 错误 |
| `timeout` | 客户端主动取消/超时 |
| `unknown` | 无法归入以上类别的网络错误 |

长轮询监控的参考退避策略：普通失败先等待 2 秒；连续失败达到 3 次后等待 30 秒，并重置连续失败计数。HTTP 4xx 通常不应盲目重试；CDN 5xx/网络错误可有限重试。

#### 2.7.5 必须持久化的状态

- `get_updates_buf`：按账号保存，成功响应后原子更新；重启后恢复。
- `context_token`：按“账号 ID + 用户 ID”保存最近有效值，支持重启后的主动发送。
- 登录账号信息：`bot_token`、`baseurl`、`ilink_bot_id`、`ilink_user_id`；token 应加密或交给系统凭据存储，不应明文写入日志/仓库。
- `typing_ticket`：可仅内存缓存；token 切换后清空。

切换到新的 bot 账号/token 时，不得无条件复用另一个账号的游标和上下文 token。持久化文件应采用临时文件写入后原子替换，避免进程退出时留下半截 JSON。

#### 2.7.6 优雅停止

2.4.4 为长轮询增加外部取消信号。Python 实现应保存消息循环和重连任务引用，在 Ctrl+C、服务停止或配置热更新时：先取消正在进行的 `getupdates`，等待任务退出，再用独立超时调用 `notifystop`，最后关闭 `aiohttp.ClientSession`。

### 2.8 消息能力、群聊边界与工具进度

基础文字私聊协议从 2.0 到 2.4.6 没有破坏性变化，主路径仍是 `getupdates → getconfig/sendtyping → sendmessage`。类型定义已经包含 `session_id`、`group_id`、`run_id`、引用消息以及工具调用进度项。

群聊仍不能据此宣称正式支持：官方插件把入站 `ChatType` 固定为 `direct`，发送路径也没有群聊分支。收到带 `group_id` 的消息时应单独记录或拒绝处理，避免把群消息错误地私聊回复给发言人。

2.4.4 新增两种可选进度消息：

- `item_list[].type=11`：`tool_call_start_item`，包含 `tool_name`、`tool_call_id`。
- `item_list[].type=12`：`tool_call_result_item`，包含 `tool_name`、`tool_call_id`、`status`。

这两种类型服务于 agent 工具执行进度，不是普通聊天必需能力。没有 tool-calling 流程的独立 AI 客户端可以忽略，但解析入站 `item_list` 时不应把未知类型当作文本。

### 2.9 媒体上传、下载与加解密

#### 2.9.1 出站上传流程

1. 读取明文文件，计算 `rawsize` 与 MD5。
2. 随机生成 16 字节 `aeskey` 和 16 字节 `filekey`；API 中通常以 hex 传递。
3. AES-128-ECB 使用 PKCS#7 padding，密文长度为 `ceil((rawsize + 1) / 16) * 16`。
4. 调用 `getuploadurl`，传明文/密文大小、MD5、媒体类型、目标用户、AES key。
5. 优先向 `upload_full_url` 发起 `POST`；否则使用 `upload_param` 拼接 CDN URL。
6. 请求头使用 `Content-Type: application/octet-stream`，请求体为 AES-128-ECB 密文。
7. CDN HTTP 200 响应头 `x-encrypted-param` 是后续消息里的下载加密参数。
8. 构造对应 `image_item` / `video_item` / `file_item`，把 AES key 转为 `CDNMedia.aes_key` 所需的 base64，并调用 `sendmessage`。

CDN 上传遇到 4xx 应立即失败；网络错误或 5xx 可有限重试，官方实现最多 3 次。文本说明和媒体附件建议分为独立 `sendmessage` 请求，每个请求的 `item_list` 只放一个 item。

#### 2.9.2 入站下载流程

- 优先使用 `CDNMedia.full_url`；没有完整 URL 时，只有在明确允许旧式 CDN 拼接回退的情况下才使用 `encrypt_query_param`。
- 图片可能把原始 hex key 放在 `image_item.aeskey`；此时先 hex 解码为 16 字节，再按需要转 base64。
- `CDNMedia.aes_key` 在实际数据中有两种编码：`base64(16-byte raw key)`，或 `base64(32-char hex string)`；客户端必须兼容两者。
- 下载密文后使用 AES-128-ECB + PKCS#7 解密。
- 语音常见为 SILK；能转码时保存 WAV，否则保留原始 `.silk`。`voice_item.text` 若存在，可直接作为语音转文字内容。
- 官方入站媒体大小上限当前为 100 MiB；独立客户端也应设置下载大小、超时和磁盘配额。

#### 2.9.3 CDN 地址与最小外发 item

官方默认 CDN 基础地址当前为 `https://novac2c.cdn.weixin.qq.com/c2c`。仅在服务端未给出完整 URL 且启用兼容回退时，按以下形式拼接，并对参数做 URL 编码：

```text
上传：{cdnBaseUrl}/upload?encrypted_query_param={upload_param}&filekey={filekey}
下载：{cdnBaseUrl}/download?encrypted_query_param={encrypt_query_param}
```

上传完成后，2.4.6 官方发送器构造的最小媒体 item 如下；它会把文字说明和媒体分别放进独立的 `sendmessage`。为便于紧凑展示，下面的 `alternatives` 只是文档占位，不属于线上的请求字段：

```json
{
  "msg": {
    "item_list": [
      {
        "type": 2,
        "image_item": {
          "media": {
            "encrypt_query_param": "<x-encrypted-param>",
            "aes_key": "<base64(32 字符 hex key)>",
            "encrypt_type": 1
          },
          "mid_size": 12352
        }
      }
    ]
  },
  "alternatives": {
    "video": {
      "type": 5,
      "video_item": {
        "media": {
          "encrypt_query_param": "<x-encrypted-param>",
          "aes_key": "<base64(32 字符 hex key)>",
          "encrypt_type": 1
        },
        "video_size": 12352
      }
    },
    "file": {
      "type": 4,
      "file_item": {
        "media": {
          "encrypt_query_param": "<x-encrypted-param>",
          "aes_key": "<base64(32 字符 hex key)>",
          "encrypt_type": 1
        },
        "file_name": "example.bin",
        "len": "12345"
      }
    }
  }
}
```

上例用 `alternatives` 并排说明三种形态；真正请求必须删除 `alternatives`，并让 `msg.item_list` 只包含一种 item。协议枚举虽然包含 `UploadMediaType.VOICE=4` 与 `voice_item`，但官方 2.4.6 的通用外发媒体分支只封装了图片、视频和文件；语音在官方代码中主要是入站下载/转码能力，独立客户端的语音外发应视为需单独联调的能力。

### 2.10 协议变化与 OpenClaw 宿主变化的边界

| 变化 | 独立 Python HTTP 客户端 | OpenClaw 插件宿主 |
|---|---|---|
| `iLink-App-*`、`base_info`、登录状态、生命周期 API、错误码 | 需要关注 | 需要关注 |
| `dist/index.js`、`runtimeExtensions`、`openclaw.plugin.json` UI 声明 | 无影响 | 需要关注 |
| Node 24 `Content-Length` 修复 | 不手动设置即可 | 直接影响旧插件 |
| OpenClaw runtime 注入方式变化 | 无影响 | 直接影响 2026.5.x 宿主 |
| `message_sending/message_sent` hook、Markdown 过滤器 | 可选借鉴 | 宿主功能 |
| Node >=22、OpenClaw >=2026.5.12 | 不适用于 Python 裸调 | 2.4.6 的实际最低要求 |

因此，独立 Python 客户端不需要安装 Node 22 或 OpenClaw，但仍应跟进公开发布包里的 HTTP 字段、错误语义和登录状态机。

---

## 三、踩坑记录

### 踩坑 1：qrcode_img_content 是 URL 不是图片

**现象**：收到 `qrcode_img_content` 后尝试保存为 PNG，看图软件报格式不支持。

**原因**：`qrcode_img_content` 实际上是一个 HTTPS 链接（`https://liteapp.weixin.qq.com/q/...`），不是 base64 图片数据。

**解法**：根据内容类型分支处理。以 `http` 开头时，优先下载二维码图片并在终端按黑白块渲染；缺少 `Pillow/qrcode` 依赖或下载失败时，退回打印 URL，让用户手动在微信打开。

---

### 踩坑 2：aiohttp 拒绝解析 JSON（Content-Type 不匹配）

**现象**：
```
aiohttp.client_exceptions.ContentTypeError: 200, message='Attempt to decode JSON
with unexpected mimetype: application/octet-stream'
```

**原因**：iLink 服务器返回的 Content-Type 是 `application/octet-stream`，而 aiohttp 的 `.json()` 默认只接受 `application/json`。

**解法**：所有 `.json()` 调用加上 `content_type=None`：
```python
data = await res.json(content_type=None)
```

---

### 踩坑 3：只有第一条消息能收到回复（最关键的坑）

**现象**：Bot 日志显示"已回复"，`sendmessage` 返回 HTTP 200，但微信只收到第一条回复，后续消息全部丢失。

**排查过程**：
1. 排查了限速问题（加 sleep 无效）
2. 排查了 `context_token` 复用问题（复用第一条的 token 无效）
3. 排查了 `baseurl` 是否需要不同域名（实测与 BASE_URL 相同）
4. 打印 HTTP 状态码和原始响应体：HTTP 200，响应体为 `{}`（空对象）

**定位**：通过比对 npm 包 `src/api/api.ts` 和 `src/messaging/` 发现，早期 Python 实现的 `sendmessage` payload 缺少官方客户端发送的核心字段，同时没有实现 `getconfig` + `sendtyping` 的标准交互流程。

**具体差异对比**：

| 字段 | 我们发送的 | SDK 实际发送的 |
|---|---|---|
| `msg.from_user_id` | ❌ 未包含 | `""` （空字符串，必填） |
| `msg.client_id` | ❌ 未包含 | `"openclaw-weixin-<随机hex>"` |
| 顶层 `base_info` | ❌ 未包含 | `channel_version` + `bot_agent` |
| `getconfig` | ❌ 未调用 | 获取并缓存 `typing_ticket`；不是 `sendmessage` 的鉴权前置 |
| `sendtyping` | ❌ 未调用 | 推荐在生成回复前后调用，失败不应阻断正文发送 |

**解法**：补全消息和 `base_info` 字段，并按官方客户端的用户体验流程实现 `getconfig` → `sendtyping(1)` → `sendmessage` → `sendtyping(2)`。其中真正决定正文发送的是 `sendmessage` 的字段、token、上下文和业务返回码；`getconfig/sendtyping` 是输入状态能力，不能把它们误写成服务端鉴权条件。

2.x 迁移后，`base_info` 建议更新为：

```json
{
  "channel_version": "2.4.6",
  "bot_agent": "weixin-ClawBot-API/1.2.0 (python)"
}
```

并且 `sendtyping` 请求体也补上 `base_info`，与官方 2.x 行为一致。自 2.4.5 起还必须检查 `sendmessage` 响应的 `ret/errmsg`；HTTP 200 不能单独证明已投递。

---

## 四、最终实现

### 项目文件

```
.
├── bot.py        # 主程序：微信 iLink Bot（Python，推荐）
├── bot.js        # 主程序：微信 iLink Bot（Node.js）
├── dusapi.py     # AI 接口封装：兼容 Anthropic 格式的通用 API 客户端
├── deepseek.py   # AI 接口封装：DeepSeek OpenAI-compatible /chat/completions
├── requirements.txt
└── config.json   # 运行时配置文件（首次运行自动生成）
```

### dusapi.py — AI 接口封装

支持 Anthropic 格式的 API（`x-api-key` + `/v1/messages`），根据模型名自动切换解析方式，内置梯度重试（2s → 4s → 8s → 16s → 32s，最多重试 5 次）。

```python
from dataclasses import dataclass

@dataclass
class DusConfig:
    api_key: str
    base_url: str
    model1: str = "claude-sonnet-4-5"
    prompt: str = "你是一个有帮助的AI助手。"
```

### deepseek.py — DeepSeek 接口封装

DeepSeek 使用 OpenAI-compatible Chat Completions：

```http
POST https://api.deepseek.com/chat/completions
Authorization: Bearer <api_key>
Content-Type: application/json
```

默认模型为 `deepseek-v4-flash`，普通微信对话默认关闭 thinking：

```json
{
  "model": "deepseek-v4-flash",
  "messages": [
    { "role": "system", "content": "你是一个有帮助的AI助手。" },
    { "role": "user", "content": "你好" }
  ],
  "max_tokens": 1024,
  "stream": false,
  "thinking": { "type": "disabled" }
}
```

配置由 `bot.py` 启动时选择 provider 后注入：

```python
ai = DeepSeekAPI(DeepSeekConfig(
    api_key=_raw_cfg["api_key"],
    base_url=_raw_cfg["base_url"],
    model=_raw_cfg["model"],
    prompt=_raw_cfg["prompt"],
))
```

### bot.py — v1.0.0 历史最小示例

> 以下代码仅保留早期排障过程，**不代表 2.4.6 推荐实现，也不应直接复制到生产环境**。它仍使用旧登录方式和 `channel_version=1.0.2`，且缺少 2.4.6 的应用请求头、完整登录状态机、生命周期通知、业务错误校验、游标持久化、受控退避和优雅取消。当前协议实现应以第二章为准；仓库现行代码的兼容差距见本章末尾的“当前项目与 2.4.6 的关系”。

```python
import asyncio
import base64
import random
import re
import aiohttp
from concurrent.futures import ThreadPoolExecutor
from dusapi import DusAPI, DusConfig

# dusapi注册地址：https://dusapi.com
# 或自行更改为你要接入的接口/AI，想先测试可以直接运行，接口返回失败也会有返回消息
# ========== 配置 ==========
config = DusConfig(
    api_key="sk-",
    base_url="https://api.dusapi.com",
    model1="gpt-5",
    prompt="你是一个有帮助的AI助手，请用中文简洁地回复。字数尽量少一些",
)
ai = DusAPI(config)
executor = ThreadPoolExecutor(max_workers=4)
# ==========================

BASE_URL = "https://ilinkai.weixin.qq.com"


def make_headers(token=None):
    uin = str(random.randint(0, 0xFFFFFFFF))
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": base64.b64encode(uin.encode()).decode(),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def api_post(session, path, body, token=None, base_url=None):
    url = f"{base_url or BASE_URL}/{path}"
    async with session.post(url, json=body, headers=make_headers(token)) as res:
        text = await res.text()
        print(f"  [{path}] HTTP {res.status} → {text[:200]}")
        try:
            import json
            return json.loads(text)
        except Exception:
            return {}


async def main():
    async with aiohttp.ClientSession() as session:
        # 1. 获取二维码
        async with session.get(
            f"{BASE_URL}/ilink/bot/get_bot_qrcode?bot_type=3"
        ) as res:
            data = await res.json(content_type=None)

        qrcode = data["qrcode"]
        qrcode_img_content = data.get("qrcode_img_content", "")

        print("qrcode:", qrcode)
        print("qrcode_img_content 前100字符:", str(qrcode_img_content)[:100])

        if qrcode_img_content:
            content = str(qrcode_img_content)
            if content.startswith("data:image/"):
                header, b64 = content.split(",", 1)
                m = re.search(r"data:image/(\w+)", header)
                ext = m.group(1) if m else "png"
                with open(f"qrcode.{ext}", "wb") as f:
                    f.write(base64.b64decode(b64))
                print(f"二维码已保存到 qrcode.{ext}")
            elif content.startswith("http"):
                print("二维码图片地址:", content)
                print("请将图片地址复制后在微信里发给文件传输助手，然后在手机端微信打开链接即可连接！！")
            elif content.startswith("<svg"):
                with open("qrcode.svg", "w", encoding="utf-8") as f:
                    f.write(content)
                print("二维码已保存到 qrcode.svg，用浏览器打开")
            else:
                with open("qrcode.png", "wb") as f:
                    f.write(base64.b64decode(content))
                print("二维码已保存到 qrcode.png")

        # 2. 等待扫码
        print("等待扫码...")
        bot_token = None
        while True:
            async with session.get(
                f"{BASE_URL}/ilink/bot/get_qrcode_status?qrcode={qrcode}"
            ) as res:
                status = await res.json(content_type=None)

            if status.get("status") == "confirmed":
                bot_token = status["bot_token"]
                bot_base_url = status.get("baseurl", "")
                print(f"登录成功！baseurl={bot_base_url}")
                break
            await asyncio.sleep(1)

        # 3. 长轮询收消息
        get_updates_buf = ""
        # 按用户缓存 typing_ticket（有效期24h）
        typing_ticket_cache = {}
        print("开始监听消息...")
        while True:
            result = await api_post(
                session,
                "ilink/bot/getupdates",
                {"get_updates_buf": get_updates_buf, "base_info": {"channel_version": "1.0.2"}},
                bot_token,
            )
            get_updates_buf = result.get("get_updates_buf") or get_updates_buf

            for msg in result.get("msgs") or []:
                if msg.get("message_type") != 1:
                    continue
                text = msg.get("item_list", [{}])[0].get("text_item", {}).get("text", "")
                from_id = msg["from_user_id"]
                context_token = msg["context_token"]
                print(f"收到消息: {text}")

                # getconfig 获取 typing_ticket（每个用户缓存一次）
                if from_id not in typing_ticket_cache:
                    cfg = await api_post(
                        session,
                        "ilink/bot/getconfig",
                        {"ilink_user_id": from_id, "context_token": context_token,
                         "base_info": {"channel_version": "1.0.2"}},
                        bot_token,
                    )
                    typing_ticket_cache[from_id] = cfg.get("typing_ticket", "")
                typing_ticket = typing_ticket_cache[from_id]

                # sendtyping status=1 表示"正在输入"
                if typing_ticket:
                    await api_post(
                        session,
                        "ilink/bot/sendtyping",
                        {"ilink_user_id": from_id, "typing_ticket": typing_ticket, "status": 1},
                        bot_token,
                    )

                # 调用 AI
                loop = asyncio.get_event_loop()
                # 或者替换为你自已要用的接口
                reply = await loop.run_in_executor(executor, ai.chat, text)

                # sendmessage（补全 SDK 所需字段）
                client_id = f"openclaw-weixin-{random.randint(0, 0xFFFFFFFF):08x}"
                send_result = await api_post(
                    session,
                    "ilink/bot/sendmessage",
                    {
                        "msg": {
                            "from_user_id": "",
                            "to_user_id": from_id,
                            "client_id": client_id,
                            "message_type": 2,
                            "message_state": 2,
                            "context_token": context_token,
                            "item_list": [{"type": 1, "text_item": {"text": reply}}],
                        },
                        "base_info": {"channel_version": "1.0.2"},
                    },
                    bot_token,
                )
                print(f"sendmessage 返回: {send_result}")
                print(f"已回复: {reply[:50]}...")

                # sendtyping status=2 取消"正在输入"
                if typing_ticket:
                    await api_post(
                        session,
                        "ilink/bot/sendtyping",
                        {"ilink_user_id": from_id, "typing_ticket": typing_ticket, "status": 2},
                        bot_token,
                    )


asyncio.run(main())

```

### 当前项目与 2.4.6 的关系

截至 2026-08-19，运行代码已按本章的 2.4.6 基线完成 Python 主路径加固：

| 文件 | 当前实现基线 | 与 2.4.6 的关系 |
|---|---|---|
| `bot.py` | `channel_version=2.4.6` | 已接入 2.4.6 请求头、POST 登录、配对码/跳转、业务错误校验、`-14` 受控重登录、游标/上下文持久化、生命周期通知和优雅停止；媒体外发仍未实现 |
| `bot.js` | `channel_version=1.0.2` | 属于 legacy 示例，不应作为 2.4.6 兼容实现 |
| 本文第二章 | `2.4.6` | 当前升级和兼容改造的目标协议基线 |

因此，文档中出现的旧版本代码只用于还原项目演进过程；实施升级时应以第二章的端点、字段、状态机和错误语义为准，不应把“文档已更新”理解为“代码已完成升级”。

---

## 五、消息结构参考

### 5.1 消息信封字段

`getupdates.msgs[]` 与 `sendmessage.msg` 共用同一套消息信封。字段是否出现取决于方向和消息类型：

| 字段 | 类型 | 方向 | 说明 |
|---|---|---|---|
| `seq` | number | 入站 | 服务端序号；不要代替 `get_updates_buf` 当游标 |
| `message_id` | number | 入站 | 服务端消息 ID；不要参与算术，持久化时建议转字符串以避免跨语言大整数精度损失 |
| `from_user_id` | string | 双向 | 入站为用户 ID；Bot 外发通常传空字符串 |
| `to_user_id` | string | 双向 | 消息接收方 |
| `client_id` | string | 外发为主 | 客户端生成的唯一消息 ID；每次发送都应重新生成 |
| `create_time_ms` / `update_time_ms` / `delete_time_ms` | number | 可选 | 毫秒时间戳；客户端必须容忍缺省 |
| `message_type` | number | 双向 | `0=NONE`、`1=USER`、`2=BOT` |
| `message_state` | number | 双向 | `0=NEW`、`1=GENERATING`、`2=FINISH` |
| `context_token` | string | 双向 | 回复上下文；按账号和用户隔离保存 |
| `item_list` | array | 双向 | 文本、媒体或工具进度 item 列表 |
| `session_id` / `group_id` / `run_id` | string | 可选 | 会话、群和 agent run 元信息；出现不等于官方插件已经支持群聊发送 |

解析器应允许服务端增加未知字段，并允许可选字段缺省。不要用严格反序列化把一次协议增量变成整个消息循环崩溃。

### 5.2 收到的消息（inbound）

```json
{
  "seq": 1,
  "message_id": 7441535359615655688,
  "from_user_id": "o9cq80xxx@im.wechat",
  "to_user_id": "2a4d413230a5@im.bot",
  "create_time_ms": 1787078400000,
  "message_type": 1,
  "message_state": 2,
  "context_token": "AARzJWAF...",
  "item_list": [
    {
      "type": 1,
      "text_item": { "text": "你好" }
    }
  ]
}
```

### 5.3 发送的消息（outbound）

```json
{
  "msg": {
    "from_user_id": "",
    "to_user_id": "o9cq80xxx@im.wechat",
    "client_id": "openclaw-weixin-a3f0b12c",
    "message_type": 2,
    "message_state": 2,
    "context_token": "AARzJWAF...",
    "item_list": [
      { "type": 1, "text_item": { "text": "你好！有什么可以帮你？" } }
    ]
  },
  "base_info": {
    "channel_version": "2.4.6",
    "bot_agent": "weixin-ClawBot-API/1.2.0 (python)"
  }
}
```

外发成功必须同时满足 HTTP 2xx、JSON 可解析、`ret` 为 `0` 或缺省。`item_list` 在当前官方外发流程中通常一次只发送一个 item；文字说明和媒体附件分别发送。

### 5.4 `item_list[].type` 完整枚举

| type | 含义 |
|---|---|
| 0 | 未指定/空 item；忽略 |
| 1 | 文本 |
| 2 | 图片（CDN AES-128-ECB 加密） |
| 3 | 语音（silk 编码） |
| 4 | 文件附件 |
| 5 | 视频 |
| 11 | 工具调用开始：`tool_call_start_item` |
| 12 | 工具调用结果：`tool_call_result_item` |

不同 `type` 对应的容器如下：

| type | item 容器 | 关键内容 |
|---|---|---|
| 1 | `text_item` | `text` |
| 2 | `image_item` | `media`、`thumb_media`、`aeskey`、`url`、`mid_size`、`thumb_size/height/width`、`hd_size` |
| 3 | `voice_item` | `media`、`encode_type`、`bits_per_sample`、`sample_rate`、`playtime`、可选转写 `text` |
| 4 | `file_item` | `media`、`file_name`、`md5`、`len` |
| 5 | `video_item` | `media`、`video_size`、`play_length`、`video_md5`、`thumb_media`、`thumb_size/height/width` |
| 11 | `tool_call_start_item` | `tool_name`、`tool_call_id` |
| 12 | `tool_call_result_item` | `tool_name`、`tool_call_id`、`status` |

每个 `MessageItem` 还可能带 `create_time_ms`、`update_time_ms`、`is_completed`、`msg_id` 和 `ref_msg`；`ref_msg` 由被引用的 `message_item` 与摘要 `title` 组成。

媒体容器内的 `CDNMedia` 完整字段为 `encrypt_query_param`、`aes_key`、`encrypt_type` 和 `full_url`；`encrypt_type=0` 表示只加密 fileid，`1` 表示打包缩略图/中图等信息。优先使用 `full_url`；`aes_key` 的两种实际编码兼容规则、加解密流程和 100 MiB 入站限制见 2.9。

`voice_item.encode_type` 当前枚举注释为：`1=PCM`、`2=ADPCM`、`3=FEATURE`、`4=SPEEX`、`5=AMR`、`6=SILK`、`7=MP3`、`8=OGG-SPEEX`。解析未知 item 或编码类型时应跳过或保留原始媒体并记录类型值，不能默认读取 `text_item`。

---

## 六、运行方式

```bash
# 安装依赖
pip install -r requirements.txt

# 运行
python bot.py
```

运行后：
1. 启动时先选择 AI provider：`DusAPI` 或 `DeepSeek`
2. 首次使用某 provider 时进入交互式配置向导，保存到 `config.json` 的 `providers.<name>` 下
3. 再次运行显示当前 provider 配置（Key 脱敏），可确认、重新配置或切换 provider
4. 终端打印二维码 URL；安装 `qrcode[pil]` / `Pillow` 后会直接渲染二维码
5. 手机扫码连接，如出现数字配对码，按终端提示输入
6. 给 Bot 发第一条消息，自动收到指令列表；后续消息走 AI 回复

---

## 七、注意事项

1. **实测扫码后 Bot ID 可能变化**（`to_user_id` 中的 `@im.bot` 部分）。不要把首次扫码得到的 ID 硬编码为永久身份；每次 `confirmed` 都应以响应中的 `ilink_bot_id` 为准。

2. **回复时使用当前入站消息的 `context_token`**；主动发送时使用按“账号 + 用户”保存的最近有效 token。不得跨账号或跨用户复用。

3. **`getconfig` 的 `typing_ticket` 可以缓存**。官方插件按用户在未来 24 小时内随机安排刷新，失败后从 2 秒指数退避、最长 1 小时；切换 token/账号后必须清空。它只用于输入状态，不是 `sendmessage` 的鉴权凭据。

4. **腾讯保留对 API 的控制权**，包括限速、内容过滤、随时终止服务，不建议将核心业务完全依赖这套 API。

5. **媒体消息**（图片/视频/文件/语音）需要先 AES-128-ECB 加密上传到 CDN，再在 `item_list` 中引用 CDN 参数。本文已经记录协议，但本仓库当前 Python/Node.js 运行代码仍只实现文本。

6. **`config.json` 含有 API Key**，请勿提交到版本控制。

7. **群聊字段当前只适合识别，不适合作为正式群聊功能**。2.x 类型中有 `group_id`，但官方插件能力仍声明 direct chat，发送路径没有群聊分支。

8. **HTTP 200 不等于业务成功**。至少检查 JSON、`ret`、`errcode`、`errmsg`；2.4.5 起 `sendmessage` 明确执行该校验。

9. **`ret=-14` / `errcode=-14` 是失效 token**。应停止紧密轮询并进入受控重新登录流程，不能当成普通瞬时网络故障无限重试。

10. **`get_updates_buf` 是按账号隔离的服务端游标**。成功响应后原子保存，重启后恢复；切换账号时不能沿用旧账号游标。

11. **启动和停止通知采用最佳努力语义**。`notifystart` 失败不应阻断收消息，`notifystop` 应使用独立短超时，并在长轮询取消后调用。

---

## 八、版本更新记录

### v1.1.0（2026-03）

在 v1.0.0 基础协议实现之上，新增以下功能：

#### 配置文件管理

API Key 等配置从代码中抽离，保存为独立的 `config.json`：

- 首次运行交互式引导创建，所有字段均有默认值
- 再次运行显示当前配置，API Key 仅显示首尾各 5 位，中间以星号替换
- 选择 N 可删除旧配置并重新填写

```json
{
  "api_key": "your-api-key",
  "base_url": "https://api.dusapi.com",
  "model": "gpt-5",
  "prompt": "你是一个有帮助的AI助手，请用中文简洁地回复。字数尽量少一些"
}
```

#### 项目的 24 小时主动重连策略

早期项目按 24 小时建立了主动提醒与重连计时器。这个时长是项目侧的保守调度参数，并非 2.4.6 协议保证的固定 token 生命周期；当前实现还必须以服务端返回的 `ret/errcode=-14` 作为 token 已失效的确定信号。`reconnect_timer_task` 与主消息循环并发运行：

```
登录 → 开始倒计时
  ↓（session_duration - warning_before 秒后）
向最近联系用户发出预警（Y 立即重连 / N 稍后提醒）
  ├─ Y → 申请新二维码发给用户，轮询扫码状态
  │       扫码成功 → bot_token_ref[0] 原子替换，旧连接无缝切换
  ├─ N → 等待 reminder_interval 秒后再次询问
  └─ 剩余时间 ≤ force_before → 强制重连，无需确认
```

所有时间参数集中在顶部 `RECONNECT_CONFIG` 字典，方便测试时调小：

```python
RECONNECT_CONFIG = {
    "session_duration":    24 * 3600,  # 生产值；测试时改为 300
    "warning_before":       2 * 3600,  # 生产值；测试时改为 60
    "reminder_interval":      30 * 60, # 生产值；测试时改为 30
    "force_before":           30 * 60, # 生产值；测试时改为 60
    "qrcode_scan_timeout":       600,  # 生产值；测试时改为 120
}
```

**关键实现细节：**
- `bot_token` 用列表包装为 `bot_token_ref = [bot_token]`，支持跨协程原子替换
- `bot_base_url_ref` 同样包装，重连后 baseurl 一并更新
- 重连期间旧 token 继续服务消息循环，扫码成功后下一次 `getupdates` 自动用新 token
- `do_reconnect()` 有重入守卫（`reconnect_in_progress`），防止强制触发与用户 Y 双重启动
- 扫码超时后重置 `login_time_ref`，避免立即再次触发警告

#### Bot 指令系统

消息处理优先级（高→低）：

1. **重连确认**：`warning_active` 为真时，`Y`/`N` 触发重连流程，不走 AI
2. **首次交互**：用户在本次会话首条消息，自动回复指令列表，不走 AI
3. **指令处理**：`/time` 返回剩余连接时间，不走 AI
4. **AI 回复**：其余所有消息正常转发给 AI 接口

`/time` 响应示例：
```
当前连接剩余时间：21 小时 43 分钟
```

---

### v1.2.0（2026-05）

基于 `@tencent-weixin/openclaw-weixin` 2.x 公开源码继续比对，Python 版新增以下变化：

#### 2.x 登录流程适配

- `get_bot_qrcode` 改为 POST 优先，并携带 `local_token_list`
- POST 未返回 `qrcode` 时自动回退旧版 GET
- `get_qrcode_status` 支持 `scaned`、`scaned_but_redirect`、`binded_redirect`、`need_verifycode`、`verify_code_blocked`、`expired`
- 支持手机端数字配对码输入
- 支持扫码轮询节点跳转：`scaned_but_redirect` + `redirect_host`

#### 2.x 元信息补齐

- Header 增加 `iLink-App-Id: bot`
- Header 增加 `iLink-App-ClientVersion`
- `base_info` 增加 `bot_agent`
- `sendtyping` 请求体也携带 `base_info`

#### 终端二维码

- `qrcode_img_content` 为 URL 时，优先尝试下载二维码图片并渲染到终端
- 缺少 `Pillow/qrcode` 或下载失败时回退打印 URL
- 依赖统一写入 `requirements.txt`

#### 多 AI provider 配置

`config.json` 从旧版扁平结构：

```json
{
  "api_key": "your-api-key",
  "base_url": "https://api.dusapi.com",
  "model": "gpt-5",
  "prompt": "..."
}
```

迁移为：

```json
{
  "provider": "deepseek",
  "providers": {
    "dusapi": {
      "api_key": "...",
      "base_url": "https://api.dusapi.com",
      "model": "gpt-5",
      "prompt": "..."
    },
    "deepseek": {
      "api_key": "...",
      "base_url": "https://api.deepseek.com",
      "model": "deepseek-v4-flash",
      "prompt": "..."
    }
  }
}
```

启动时先选择 provider，再确认或创建对应配置。旧版扁平配置会自动迁移到 `providers.dusapi`。

---

### 协议文档修订（2026-08-19，不代表程序版本升级）

- 将上游基线更新为 npm `latest`：`@tencent-weixin/openclaw-weixin@2.4.6`。
- 核对 2.4.5 与 2.4.6 发布包：2.4.6 未改变运行协议，主要是版本/发布包元数据整理；最新实质变化仍来自 2.4.5。
- 补录 2.4.4 的长轮询取消与工具调用进度 item。
- 补录 2.4.5 的网络错误分类、`sendmessage` 业务结果校验、`-14` 失效 token 语义及宿主最低版本变化。
- 完整记录 2.4.6 请求头、登录状态机、全部已知 API、超时/退避、状态持久化、生命周期通知、消息枚举与媒体 CDN 流程。
- 标明当前 Python 运行代码已经切换到 2.4.6；媒体 CDN 能力仍作为后续扩展项，文中的旧代码片段仅用于说明协议演进。

*协议资料基于腾讯公开发布的 [`@tencent-weixin/openclaw-weixin@2.4.6`](https://www.npmjs.com/package/@tencent-weixin/openclaw-weixin) 与 [`Tencent/openclaw-weixin` v2.4.6](https://github.com/Tencent/openclaw-weixin/tree/v2.4.6)，结合本仓库 Python/Node.js 实测整理；最后核对日期：2026-08-19。*
