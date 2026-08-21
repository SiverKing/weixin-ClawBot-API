# 微信 Claw Bot（Python）

这是一个直接调用腾讯微信 iLink Bot 接口的 Python 客户端示例。它无需部署 OpenClaw，即可通过扫码登录微信个人账号，并把收到的文字消息交给 DusAPI 或 DeepSeek 生成回复。

当前运行代码按腾讯微信 iLink Bot 2.4.6 的公开 HTTP 行为对齐。协议细节和版本差异记录在 [weixin-openclaw-api-py-docs.md](weixin-openclaw-api-py-docs.md)。

## 更强大的选择：Siver WX机器人（wxbot_plus）

如果你需要更强大的微信自动化能力，推荐作者的另一款项目 —— [**Siver WX机器人（SiverWXbot_plus）**](https://github.com/SiverKing/SiverWXbot_plus)：

- **内置 AI 面板管家（Agent）**：支持自然语言对话完成面板配置、Prompt 编写、运行状态检查、故障排查，甚至源码版定制开发
- **已打通本项目的 ClawBot 连接**：内置 agent 可直接接入微信 ClawBot，用手机微信远程管理机器人，电脑端离线也能用
- **功能完整**：多 AI 平台接入、多 Prompt 管理、对话记忆、图片识别、关键词回复、自定义规则转发、定时任务、朋友圈自动化、Web 管理面板等 50+ 管理命令

详细介绍见：[AI 面板管家与 ClawBot 连接说明](https://wxbot.siverking.online/docs.html?c=ai%E9%9D%A2%E6%9D%BF%E7%AE%A1%E7%90%86)

## 功能

- 固定入口申请二维码，支持扫码状态长轮询、数字配对码和节点跳转
- `getupdates` 长轮询收消息，并持久化 `get_updates_buf` 游标
- `getconfig`、输入状态和完整文本 `sendmessage` 流程
- 校验 HTTP、JSON、`ret/errcode`，识别 `-14` 失效 token 并受控重新登录
- 启动/停止时最佳努力调用 `notifystart` / `notifystop`
- 连接到期前提醒、确认和自动重连
- DusAPI / DeepSeek provider 配置与 API Key 脱敏显示
- 终端二维码渲染；缺少图像依赖时保留二维码链接

## 文件结构

```text
.
├── bot.py              # Bot 主程序
├── dusapi.py           # DusAPI 兼容封装
├── deepseek.py         # DeepSeek 兼容封装
├── requirements.txt    # Python 依赖
├── config.json         # 首次运行自动生成，请勿提交
├── weixin_state.json   # 首次登录自动生成，包含敏感连接状态，请勿提交
└── README.md
```

## 快速开始

```bash
pip install -r requirements.txt
python bot.py
```

首次运行会选择 AI provider，并填写 API Key、接口地址、模型和系统提示词。也可以从 [Releases](https://github.com/SiverKing/weixin-ClawBot-API/releases) 下载打包版本。登录成功后会按账号保存连接状态，正常重启直接复用；服务端返回 `-14` 或手动执行重连时进入受控重新扫码。

首次登录或 token 失效后的登录步骤：

1. 选择并确认 AI 配置。
2. 使用手机微信扫描终端显示的二维码。
3. 如果手机要求数字配对码，在终端输入配对码。
4. 登录成功后，在微信中发送消息；首次交互会收到指令列表。

## 配置文件

`config.json` 支持多个 provider，旧版扁平配置会自动迁移：

```json
{
  "provider": "deepseek",
  "providers": {
    "dusapi": {
      "api_key": "your-dusapi-key",
      "base_url": "https://api.dusapi.com",
      "model": "gpt-5",
      "prompt": "你是一个有帮助的AI助手，请用中文简洁地回复。字数尽量少一些"
    },
    "deepseek": {
      "api_key": "your-deepseek-key",
      "base_url": "https://api.deepseek.com",
      "model": "deepseek-v4-flash",
      "prompt": "你是一个有帮助的AI助手，请用中文简洁地回复。字数尽量少一些"
    }
  }
}
```

启动时 API Key 只显示首尾各 5 位。`config.json` 和运行状态文件含有敏感凭据，请妥善保管。

| Provider | 配置文件 | 默认地址 | 默认模型 |
|---|---|---|---|
| DusAPI | `dusapi.py` | `https://api.dusapi.com` | `gpt-5` |
| DeepSeek | `deepseek.py` | `https://api.deepseek.com` | `deepseek-v4-flash` |

## Bot 指令

| 指令 | 说明 |
|---|---|
| `/help` 或 `/指令` | 查看指令列表 |
| `/time` | 查询当前连接剩余时间 |
| `/重新连接` | 请求立即重连，随后回复 `Y` 或 `N` |

非指令文字会转发给 AI。图片、文件和未提供文字转写的语音目前只会收到能力提示，不会被错误地送入 AI。

## 自动重连

`bot.py` 顶部的 `RECONNECT_CONFIG` 可调整项目侧的提醒策略：

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `session_duration` | `24 * 3600` | 项目侧连接计时窗口（秒） |
| `warning_before` | `2 * 3600` | 提前提醒时间（秒） |
| `reminder_interval` | `30 * 60` | 用户回复 N 后再次提醒间隔（秒） |
| `force_before` | `30 * 60` | 剩余时间低于此值时强制重连（秒） |
| `qrcode_scan_timeout` | `480` | 整体扫码等待上限（秒） |

这些是客户端调度参数，不是服务端承诺的固定 token 生命周期。服务端明确返回 `ret=-14` 或 `errcode=-14` 时，程序会停止紧密轮询并重新走二维码登录。

## iLink 2.4.6 协议要点

### 请求头与基础信息

登录后的 POST 请求使用以下头部；`Content-Length` 由 `aiohttp` 自动计算，不手动设置：

```text
Content-Type: application/json
AuthorizationType: ilink_bot_token
X-WECHAT-UIN: <随机 uint32 的十进制字符串再 base64>
iLink-App-Id: bot
iLink-App-ClientVersion: 132102
Authorization: Bearer <bot_token>
```

每个 CGI 请求体都带有：

```json
{
  "base_info": {
    "channel_version": "2.4.6",
    "bot_agent": "weixin-ClawBot-API/1.2.0 (python)"
  }
}
```

二维码状态 GET 请求只发送公共应用头；首次申请二维码固定使用 `https://ilinkai.weixin.qq.com`。扫码状态遇到 `scaned_but_redirect` 后，才切换到服务端返回的节点。

### 登录流程

```text
POST /ilink/bot/get_bot_qrcode?bot_type=3
  body: { "local_token_list": [最多 10 个本地 token] }
GET  /ilink/bot/get_qrcode_status?qrcode=...
  → wait / scaned / need_verifycode / scaned_but_redirect
  → binded_redirect（本地仍有 token 时复用）
  → confirmed（保存 bot_token、baseurl、账号 ID）
POST /ilink/bot/msg/notifystart
```

### 消息流程

```text
POST getupdates（携带上次成功的 get_updates_buf）
  → 校验 ret/errcode，保存新游标
  → 按 longpolling_timeout_ms 调整下一次长轮询
POST getconfig
POST sendtyping { status: 1 }
调用 AI
POST sendmessage（校验 ret）
POST sendtyping { status: 2 }（finally 中尽力执行）
```

`sendmessage.msg.context_token` 必须使用当前入站消息的 token；`client_id` 每次发送都唯一。HTTP 200 不代表消息投递成功，必须同时检查 JSON 和业务返回码。

程序停止时会先取消长轮询和重连任务，再以独立短超时调用 `POST /ilink/bot/msg/notifystop`。

## 注意事项

1. 本项目当前以文字私聊为主；媒体消息需要额外实现 AES-128-ECB 加密和 CDN 上传/下载。
2. 不要把 `config.json`、`weixin_state.json`、二维码或 token 提交到版本控制。
3. 微信服务端可能限速、过滤内容或调整接口，生产环境请增加监控和人工重登预案。
4. 请遵守微信 ClawBot 功能条款及当地法律法规。

## 依赖

详见 `requirements.txt`：`aiohttp`、`requests`、`qrcode[pil]`；打包可选 `pyinstaller`。

## 致谢

本项目参考并借鉴了以下项目，在此表示衷心感谢：

- 感谢 [OpenClaw](https://github.com/openclaw/openclaw) 项目及其[官方文档](https://docs.openclaw.ai)，本项目最初基于其架构思路开发
- 感谢 [DeepSeek](https://www.deepseek.com) 提供的 API 服务
- 感谢 [腾讯微信openclaw-weixin](https://github.com/Tencent/openclaw-weixin) 提供的接口能力
