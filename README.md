# 微信 Claw Bot(@tencent-weixin/openclaw-weixin)

基于腾讯官方开放的 **openclaw-weixin**/**openclaw-weixin-api**/**openclaw-weixin-cli** 实现的微信个人账号 Bot，支持接入任意 AI 模型，实现微信自动对话。

---

## 简介

2026 年腾讯通过 [OpenClaw](https://docs.openclaw.ai) 平台正式开放了微信个人账号的 Bot API，官方名称为 **微信 ClawBot 插件功能**，底层协议为 **iLink**，接入域名 `ilinkai.weixin.qq.com` 为腾讯官方服务器。

本项目提供 Python 和 Node.js 两种实现，可直接接入兼容 Anthropic 格式的 AI 接口（Claude、GPT 等），实现收到微信消息后自动 AI 回复。**免openclaw部署和登录直接接入与调用。**

---

## 功能

- 扫码登录微信（生成二维码链接）
- 长轮询实时接收消息
- 调用 AI 接口生成回复
- 发送前显示"正在输入"状态
- 支持多用户并发对话
- 内置梯度重试（AI 接口失败自动重试）

---

## 文件结构

```
.
├── bot.py         # Python 实现（推荐）
├── bot.js         # Node.js 实现
├── dusapi.py      # AI 接口封装（Python，兼容 Anthropic 格式）
└── README.md
```

---

## 快速开始

### Python 版

**安装依赖：**
```bash
pip install aiohttp requests
```

**配置 `bot.py` 顶部：**
```python
config = DusConfig(
    api_key="your-api-key",        # AI 服务的 API Key
    base_url="https://api.xxx.com", # AI 服务地址（Anthropic 兼容格式）
    model1="claude-sonnet-4-5",    # 使用的模型
    prompt="你是一个有帮助的AI助手，请用中文简洁地回复。",
)
```

**运行：**
```bash
python bot.py
```

---

### Node.js 版

**要求：** Node.js 18+

**配置 `package.json`（如不存在则创建）：**
```json
{ "type": "module" }
```

**运行：**
```bash
node bot.js
```

---

### 登录流程

1. 运行后终端会打印一个二维码链接
2. 将链接在手机微信中打开，按照微信提示连接即可
3. 扫码确认后终端显示"登录成功"，Bot 开始监听消息
4. 在微信中向 Bot 发送消息，即可收到 AI 回复
5. 微信机器人token存储在bot_token.txt文件中，重启程序会自动读取该文件，无需重新扫码登录。如果需要切换账号，只需删除该文件即可。
---

## AI 接口说明（dusapi.py）

`DusAPI` 封装了兼容 Anthropic 格式的 HTTP 接口，支持所有使用 `x-api-key` + `/v1/messages` 格式的服务，包括：

- [DusAPI](https://dusapi.com)（兼容多模型）
- Anthropic 官方 API
- 其他 Anthropic 格式的第三方代理

**配置项：**

| 参数 | 说明 | 默认值 |
|---|---|---|
| `api_key` | API 密钥 | 必填 |
| `base_url` | 接口地址 | 必填 |
| `model1` | 模型名称 | `claude-sonnet-4-5` |
| `prompt` | 系统提示词 | `你是一个有帮助的AI助手。` |

---

## iLink Bot API 核心说明

### 请求头

每个请求都需要携带以下 Header：

```
Content-Type: application/json
AuthorizationType: ilink_bot_token
X-WECHAT-UIN: <随机uint32转base64，每次请求重新生成>
Authorization: Bearer <bot_token>
```

### 消息收发流程

```
POST getupdates（长轮询，服务器 hold 35s）
  └─ 收到用户消息
       ├─ POST getconfig  → 获取 typing_ticket（每用户缓存，有效24h）
       ├─ POST sendtyping { status: 1 }  → 显示"正在输入"
       ├─ 调用 AI 接口
       ├─ POST sendmessage  → 发送回复
       └─ POST sendtyping { status: 2 }  → 取消"正在输入"
```

### sendmessage 必填字段

官方 SDK 要求 `sendmessage` 包含以下完整结构，缺少任意字段会导致消息静默丢失（HTTP 200 但不投递）：

```json
{
  "msg": {
    "from_user_id": "",
    "to_user_id": "<用户ID@im.wechat>",
    "client_id": "openclaw-weixin-<随机hex>",
    "message_type": 2,
    "message_state": 2,
    "context_token": "<从收到的消息中原样取>",
    "item_list": [
      { "type": 1, "text_item": { "text": "回复内容" } }
    ]
  },
  "base_info": { "channel_version": "1.0.2" }
}
```

> **注意**：`context_token` 必须使用当前收到消息中的值，不可复用旧消息的 token。

---

## 注意事项

1. **每次扫码登录 Bot ID 会变化**，这是 iLink 平台的设计，属于正常现象。
2. **仅限合规使用**，需遵守《微信 ClawBot 功能使用条款》，腾讯保留对内容过滤和限速的权利。
3. 本项目仅支持**文本消息**，图片/语音/文件等媒体消息需额外实现 CDN 加密上传流程。
4. Bot 不建议用于核心业务，腾讯可随时变更或终止该服务。

---

## 依赖

| 环境 | 依赖 |
|---|---|
| Python | `aiohttp`、`requests` |
| Node.js | 无需额外安装（Node.js 18+ 内置 fetch） |

---

## 相关资源

- [OpenClaw 官方文档](https://docs.openclaw.ai)
- [官方 npm 包](https://www.npmjs.com/package/@tencent-weixin/openclaw-weixin)
- [DusAPI（兼容多模型的 AI 接口）](https://dusapi.com)
