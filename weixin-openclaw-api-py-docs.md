# 微信 iLink/openclaw-weixin-api Bot Python 实现：逆向分析与踩坑全记录

> 本文记录从零开始用 Python 实现微信 iLink Bot（ClawBot）API 的完整过程，包括协议分析、调试排查、关键踩坑点和最终可用代码。

---

## 一、背景

2026 年腾讯通过 OpenClaw 平台正式开放了微信个人账号的 Bot API，底层协议叫 **iLink（智联）**，接入域名为 `https://ilinkai.weixin.qq.com`，纯 HTTP/JSON，无需 SDK 可直接 `fetch` / `requests` 调用。

官方只发布了 Node.js 包（`@tencent-weixin/openclaw-weixin`），没有 Python 实现。本文通过逆向分析 npm 包源码，在 Python 中完整复现了这套协议。

---

## 二、协议逆向分析

### 2.1 信息来源

- 腾讯 npm 包：`@tencent-weixin/openclaw-weixin@1.0.2`（41 个 TypeScript 源文件，完全公开）
- 通过 unpkg CDN 直接获取：`https://unpkg.com/@tencent-weixin/openclaw-weixin@1.0.2/`
- 腾讯官方 GitHub 仓库：`https://github.com/Tencent/openclaw-weixin`
- 最新 2.x 线源码：`main` 分支，README 将 `2.0.x` 标记为 active/latest，`1.0.x` 为 legacy
- 源码目录结构：
  ```
  src/
  ├── auth/       # QR 码登录、账号存储
  ├── api/        # iLink HTTP API 封装（关键）
  ├── cdn/        # 媒体文件 AES-128-ECB 加解密 + CDN 上传
  ├── messaging/  # 消息收发、inbound/outbound 处理（关键）
  ├── monitor/    # 长轮询主循环
  ├── config/     # 配置 schema
  └── storage/    # 状态持久化
  ```

### 2.2 完整 API 列表

| Endpoint | Method | 功能 |
|---|---|---|
| `/ilink/bot/get_bot_qrcode` | GET/POST | 获取登录二维码（`?bot_type=3`）。1.0.2 可 GET；2.x 官方实现使用 POST 并携带 `local_token_list` |
| `/ilink/bot/get_qrcode_status` | GET | 轮询扫码状态（`?qrcode=xxx`） |
| `/ilink/bot/getupdates` | POST | 长轮询收消息（核心，服务器 hold 35s） |
| `/ilink/bot/getconfig` | POST | 获取 `typing_ticket`（**必须调用**） |
| `/ilink/bot/sendtyping` | POST | 发送"正在输入"状态 |
| `/ilink/bot/sendmessage` | POST | 发送消息 |
| `/ilink/bot/getuploadurl` | POST | 获取 CDN 预签名上传地址（媒体消息用） |

### 2.3 请求头规范

每次请求都必须带以下 Header：

```python
{
    "Content-Type": "application/json",
    "AuthorizationType": "ilink_bot_token",
    "X-WECHAT-UIN": base64(str(random_uint32)),  # 每次请求随机生成，防重放
    "iLink-App-Id": "bot",                       # 2.x 新增/明确
    "iLink-App-ClientVersion": "132099",         # 2.4.3 对应整数版本号
    "Authorization": f"Bearer {bot_token}",       # 登录后才有
}
```

`X-WECHAT-UIN` 的生成方式：随机生成一个 uint32，转十进制字符串，再 base64 编码。**每次请求都要重新生成**。

2.x 还会在请求体的 `base_info` 中加入 `bot_agent`：

```json
{
  "base_info": {
    "channel_version": "2.4.3",
    "bot_agent": "weixin-ClawBot-API/1.0.1 (python)"
  }
}
```

### 2.4 完整消息流

```
登录流程：
  POST get_bot_qrcode?bot_type=3 → 得到 qrcode + qrcode_img_content（URL）
  GET get_qrcode_status（轮询） → status="confirmed" 时得到 bot_token

收发消息流程（每条消息）：
  POST getupdates（长轮询，hold 35s） → 得到 msgs[]
  ↓ 收到用户消息
  POST getconfig（首次每用户调用一次，缓存 typing_ticket）
  POST sendtyping { status: 1 }  ← 显示"正在输入"
  ↓ 调用 AI 生成回复
  POST sendmessage（带完整字段）
  POST sendtyping { status: 2 }  ← 取消"正在输入"
```

---

### 2.5 openclaw-weixin 2.x 增量逆向结果

这次对比 2.x 源码后的结论：**基础文字私聊协议没有本质变化**。`getupdates`、`getconfig`、`sendtyping`、`sendmessage` 仍然是主路径，`context_token` 仍然必须从当前 inbound 消息原样带回。变化集中在登录流程、元信息、媒体字段和插件宿主能力。

#### 登录流程变化

1. 2.x 官方实现获取二维码时优先使用：

```http
POST /ilink/bot/get_bot_qrcode?bot_type=3
```

请求体：

```json
{
  "local_token_list": []
}
```

`local_token_list` 用于告诉服务端本地已有连接 token，处理重复扫码、已绑定连接等场景。本项目 Python 版保留兼容：POST 没拿到 `qrcode` 时自动退回旧版 GET。

2. `get_qrcode_status` 不再只需要识别 `confirmed`。实际需要处理：

| status | 含义 | Python 处理 |
|---|---|---|
| `wait` | 等待扫码 | 继续轮询 |
| `scaned` | 已扫码，等待手机端确认 | 打印提示 |
| `confirmed` | 已确认 | 读取 `bot_token`、`baseurl` |
| `scaned_but_redirect` | 需要切换扫码状态轮询节点 | 使用 `redirect_host` 切换 base URL |
| `binded_redirect` | 服务端认为已有连接/绑定跳转 | 重连时沿用当前 token；首次登录时刷新二维码 |
| `need_verifycode` | 微信端要求数字配对码 | 终端提示用户输入手机显示的数字 |
| `verify_code_blocked` | 配对码多次错误 | 刷新二维码 |
| `expired` | 二维码过期 | 重新生成二维码 |

#### 请求头与 base_info

2.x 请求相比 1.0.2 多了宿主识别信息：

```python
headers = {
    "Content-Type": "application/json",
    "AuthorizationType": "ilink_bot_token",
    "X-WECHAT-UIN": base64(str(random_uint32)),
    "iLink-App-Id": "bot",
    "iLink-App-ClientVersion": "132099",
    "Authorization": f"Bearer {bot_token}",
}
```

`132099` 是 `2.4.3` 的整数编码：`(2 << 16) | (4 << 8) | 3`。

请求体中的 `base_info` 从：

```json
{ "channel_version": "1.0.2" }
```

扩展为：

```json
{
  "channel_version": "2.4.3",
  "bot_agent": "weixin-ClawBot-API/1.0.1 (python)"
}
```

#### getupdates 字段

`get_updates_buf` 仍是主游标字段。2.x 类型定义里也能看到兼容字段 `sync_buf`，但当前 Python 实现继续使用 `get_updates_buf`。响应里正式包含：

```json
{
  "msgs": [],
  "get_updates_buf": "...",
  "longpolling_timeout_ms": 35000,
  "errcode": 0
}
```

#### 群聊字段结论

2.x 的 `WeixinMessage` 类型里有 `group_id`、`session_id`，但官方插件能力声明仍是 `chatTypes: ["direct"]`，入站转换也把 `ChatType` 写死为 `direct`，发送消息时没有群聊分支。因此当前判断：

- `group_id` 可用于识别/记录疑似群聊消息；
- 不建议基于它宣称正式支持群聊；
- 如果 `getupdates` 收到带 `group_id` 的消息，应先打印日志或忽略，避免误把群消息私聊回复给发言人。

#### 媒体字段扩展

2.x 类型定义中媒体相关字段更完整，包括：

- `full_url`
- `upload_full_url`
- `aeskey`
- `no_need_thumb`
- `thumb_upload_param`
- `ref_msg`

这些字段对图片、语音、文件、视频、引用消息有用。本项目当前仍只实现文本收发，媒体消息需要继续实现 AES-128-ECB 加密、CDN 上传和 `item_list` 媒体引用。

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

**定位**：通过逆向 npm 包 `src/api/api.ts` 和 `src/messaging/` 发现，Python 实现的 `sendmessage` payload 缺少 SDK 中的必要字段，且漏掉了 `getconfig` + `sendtyping` 的前置调用。

**具体差异对比**：

| 字段 | 我们发送的 | SDK 实际发送的 |
|---|---|---|
| `msg.from_user_id` | ❌ 未包含 | `""` （空字符串，必填） |
| `msg.client_id` | ❌ 未包含 | `"openclaw-weixin-<随机hex>"` |
| 顶层 `base_info` | ❌ 未包含 | `{"channel_version": "1.0.2"}` |
| `getconfig` 前置调用 | ❌ 未调用 | 每个用户首次必须调用 |
| `sendtyping` | ❌ 未调用 | 发送前后各调用一次 |

**解法**：补全所有缺失字段，并按 SDK 的完整流程实现 `getconfig` → `sendtyping(1)` → `sendmessage` → `sendtyping(2)`。

2.x 迁移后，`base_info` 建议更新为：

```json
{
  "channel_version": "2.4.3",
  "bot_agent": "weixin-ClawBot-API/1.0.1 (python)"
}
```

并且 `sendtyping` 请求体也补上 `base_info`，与官方 2.x 行为一致。

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

### bot.py — 主程序完整代码（v1.0.0 基础版，已迭代，见下方更新说明）

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

---

## 五、消息结构参考

### 收到的消息（inbound）

```json
{
  "seq": 1,
  "message_id": 7441535359615655688,
  "from_user_id": "o9cq80xxx@im.wechat",
  "to_user_id": "2a4d413230a5@im.bot",
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

### 发送的消息（outbound）

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
    "channel_version": "2.4.3",
    "bot_agent": "weixin-ClawBot-API/1.0.1 (python)"
  }
}
```

### 消息类型（item_list[].type）

| type | 含义 |
|---|---|
| 1 | 文本 |
| 2 | 图片（CDN AES-128-ECB 加密） |
| 3 | 语音（silk 编码） |
| 4 | 文件附件 |
| 5 | 视频 |

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

1. **每次扫码登录 Bot ID 都会变化**（`to_user_id` 中的 `@im.bot` 部分），不同于普通机器人的固定 ID。这是 iLink 当前的设计。

2. **`context_token` 必须用当前消息的**，不能复用历史 token，否则后续消息无法送达。

3. **`getconfig` 的 `typing_ticket` 可以缓存**，SDK 缓存 24h，同一用户无需每条消息都重新获取。重连后会清空缓存，下一条消息自动重新获取。

4. **腾讯保留对 API 的控制权**，包括限速、内容过滤、随时终止服务，不建议将核心业务完全依赖这套 API。

5. **媒体消息**（图片/视频/文件）需要先 AES-128-ECB 加密上传到 CDN，再在 `item_list` 中引用 CDN 参数，本文未实现，仅支持文本。

6. **`config.json` 含有 API Key**，请勿提交到版本控制。

7. **群聊字段当前只适合识别，不适合作为正式群聊功能**。2.x 类型中有 `group_id`，但官方插件能力仍声明 direct chat，发送路径没有群聊分支。

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

#### 24 小时自动重连

iLink 连接有效期 24 小时，到期须重新扫码。新增 `reconnect_timer_task` 异步任务与主消息循环并发运行：

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

基于 `@tencent-weixin/openclaw-weixin` 2.x 源码继续逆向，Python 版新增以下变化：

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

*基于 `@tencent-weixin/openclaw-weixin@1.0.2` 与官方 2.x 源码逆向分析 + Python/Node.js 实测，最近更新至 2026 年 5 月。*
