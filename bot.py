import asyncio
import base64
import io
import inspect
import json
import os
import re
import secrets
import time
import urllib.request
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor

import aiohttp

from dusapi import DusAPI, DusConfig
from deepseek import DeepSeekAPI, DeepSeekConfig

executor = ThreadPoolExecutor(max_workers=4)
ai = None  # 启动时从配置文件加载后初始化

# ========== 自动重连配置（可调参数） ==========
# 测试时将数值改小，例如：
#   "session_duration": 300, "warning_before": 60, "reminder_interval": 30,
#   "force_before": 60, "qrcode_scan_timeout": 120
RECONNECT_CONFIG = {
    "session_duration":    24 * 3600,  # 会话总时长（秒）
    "warning_before":       2 * 3600,  # 提前多久发出警告（秒）
    "reminder_interval":      30 * 60, # 用户回 N 后多久再问（秒）
    "force_before":           30 * 60, # 最后多久强制重连（秒）
    "qrcode_scan_timeout":       480,  # 官方客户端默认整体等待时长（秒）
}
# =============================================

# ========== 配置文件 ==========
CONFIG_FILE = "config.json"
STATE_FILE = "weixin_state.json"
_DEFAULT_PROMPT = "你是一个有帮助的AI助手，请用中文简洁地回复。字数尽量少一些"
CHANNEL_VERSION = "2.4.6"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = str((2 << 16) | (4 << 8) | 6)
BOT_AGENT = "weixin-ClawBot-API/1.2.0 (python)"
DEFAULT_BOT_AGENT = "OpenClaw"
BOT_AGENT_MAX_LEN = 256

# iLink 2.4.6 官方客户端默认超时。长轮询超时属于正常控制流，不能当作业务失败。
QR_STATUS_TIMEOUT = 35
LONG_POLL_TIMEOUT = 35
API_TIMEOUT = 15
CONFIG_TIMEOUT = 10
CONFIG_CACHE_TTL = 24 * 60 * 60
CONFIG_CACHE_INITIAL_RETRY = 2
CONFIG_CACHE_MAX_RETRY = 60 * 60
MAX_QR_REFRESH_COUNT = 3
MAX_CONSECUTIVE_FAILURES = 3
RETRY_DELAY = 2
BACKOFF_DELAY = 30
MAX_LONG_POLL_TIMEOUT = 120

PROVIDERS = {
    "dusapi": {
        "label": "DusAPI",
        "base_url": "https://api.dusapi.com",
        "model": "gpt-5",
        "prompt": _DEFAULT_PROMPT,
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "prompt": _DEFAULT_PROMPT,
    },
}


def mask_key(key: str) -> str:
    """保留前5位和后5位，中间用星号替换。"""
    if len(key) <= 10:
        return key
    return key[:5] + "*" * (len(key) - 10) + key[-5:]


def load_config_file() -> dict:
    if not os.path.exists(CONFIG_FILE):
        return {"provider": "dusapi", "providers": {}}

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # 兼容旧版扁平配置：{api_key, base_url, model, prompt}
    if "providers" not in cfg:
        old_provider_cfg = {
            "api_key": cfg.get("api_key", ""),
            "base_url": cfg.get("base_url", PROVIDERS["dusapi"]["base_url"]),
            "model": cfg.get("model", PROVIDERS["dusapi"]["model"]),
            "prompt": cfg.get("prompt", _DEFAULT_PROMPT),
        }
        cfg = {
            "provider": "dusapi",
            "providers": {"dusapi": old_provider_cfg},
        }
    cfg.setdefault("provider", "dusapi")
    cfg.setdefault("providers", {})
    return cfg


def save_config_file(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _empty_runtime_state() -> dict:
    return {
        "bot_token": "",
        "baseurl": "",
        "ilink_bot_id": "",
        "ilink_user_id": "",
        "get_updates_buf": "",
        "contexts": {},
        "last_contact": {"from_id": "", "context_token": ""},
    }


def load_runtime_state() -> dict:
    """加载 iLink 运行状态；token、游标和上下文按本地账号隔离保存。"""
    state = _empty_runtime_state()
    if not os.path.exists(STATE_FILE):
        return state
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            state.update({k: raw.get(k, v) for k, v in state.items()})
            if not isinstance(state.get("contexts"), dict):
                state["contexts"] = {}
            if not isinstance(state.get("last_contact"), dict):
                state["last_contact"] = {"from_id": "", "context_token": ""}
    except (OSError, ValueError, TypeError) as exc:
        print(f"[状态] 无法读取 {STATE_FILE}，将从空状态开始: {exc}")
    return state


def save_runtime_state(state: dict):
    """原子保存 token、baseurl、get_updates_buf 和 context_token。"""
    temp_file = f"{STATE_FILE}.tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, STATE_FILE)
    except (OSError, TypeError, ValueError) as exc:
        print(f"[状态] 保存 {STATE_FILE} 失败: {exc}")
        try:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        except OSError:
            pass


def sanitize_bot_agent(raw: str | None) -> str:
    """按 2.4.6 的 UA 风格规则清洗 bot_agent，避免非法元数据污染请求。"""
    if not isinstance(raw, str) or not raw.strip():
        return DEFAULT_BOT_AGENT

    product_re = re.compile(r"^[A-Za-z0-9_.-]{1,32}/[A-Za-z0-9_.+\-]{1,32}$")
    comment_re = re.compile(r"^[\x20-\x27\x2A-\x7E]{1,64}$")
    raw_tokens = raw.strip().split()
    tokens = []
    i = 0
    while i < len(raw_tokens):
        token = raw_tokens[i]
        if token.startswith("(") and not token.endswith(")"):
            while i + 1 < len(raw_tokens) and not token.endswith(")"):
                i += 1
                token += " " + raw_tokens[i]
        tokens.append(token)
        i += 1

    accepted = []
    pending = None
    for token in tokens:
        if token.startswith("(") and token.endswith(")"):
            comment = token[1:-1]
            if pending and comment_re.fullmatch(comment):
                accepted.append(f"{pending} ({comment})")
                pending = None
            elif pending:
                accepted.append(pending)
                pending = None
            continue
        if pending:
            accepted.append(pending)
        pending = token if product_re.fullmatch(token) else None
    if pending:
        accepted.append(pending)

    if not accepted:
        return DEFAULT_BOT_AGENT

    result = " ".join(accepted)
    if len(result.encode("utf-8")) <= BOT_AGENT_MAX_LEN:
        return result
    kept = []
    size = 0
    for token in accepted:
        extra = len(token.encode("utf-8")) + (1 if kept else 0)
        if size + extra > BOT_AGENT_MAX_LEN:
            break
        kept.append(token)
        size += extra
    return " ".join(kept) if kept else DEFAULT_BOT_AGENT


def choose_provider(default_provider: str) -> str:
    print("\n请选择 AI 提供商：")
    keys = list(PROVIDERS.keys())
    for index, key in enumerate(keys, 1):
        default_mark = "（默认）" if key == default_provider else ""
        print(f"  {index}. {PROVIDERS[key]['label']} {default_mark}")

    while True:
        choice = input("输入序号或名称后回车: ").strip().lower()
        if not choice:
            return default_provider if default_provider in PROVIDERS else "dusapi"
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(keys):
                return keys[idx]
        if choice in PROVIDERS:
            return choice
        print("输入无效，请重新选择。")


def prompt_provider_config(provider: str, old_cfg: dict | None = None) -> dict:
    defaults = PROVIDERS[provider]
    old_cfg = old_cfg or {}
    print(f"\n配置 {defaults['label']}：")

    old_key = old_cfg.get("api_key", "")
    key_prompt = f"请输入 API Key（当前 {mask_key(old_key)}，留空沿用）: " if old_key else "请输入 API Key: "
    api_key = input(key_prompt).strip() or old_key

    old_base_url = old_cfg.get("base_url", defaults["base_url"])
    base_url = input(f"请输入 API 地址（留空默认/沿用 {old_base_url}）: ").strip() or old_base_url

    old_model = old_cfg.get("model", defaults["model"])
    model = input(f"请输入模型名称（留空默认/沿用 {old_model}）: ").strip() or old_model

    old_prompt = old_cfg.get("prompt", defaults["prompt"])
    prompt = input("请输入系统提示词（留空默认/沿用当前值）: ").strip() or old_prompt

    return {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "prompt": prompt,
    }


def load_or_create_config() -> dict:
    """先选择 AI 提供商，再确认或创建对应配置。"""
    sep = "=" * 60
    dash = "-" * 60
    cfg = load_config_file()

    while True:
        provider = choose_provider(cfg.get("provider", "dusapi"))
        cfg["provider"] = provider
        provider_cfg = cfg["providers"].get(provider)
        label = PROVIDERS[provider]["label"]

        if not provider_cfg:
            print(f"\n未找到 {label} 配置，需要创建。")
            provider_cfg = prompt_provider_config(provider)
            cfg["providers"][provider] = provider_cfg
            save_config_file(cfg)
            print(f"\n配置已保存到 {CONFIG_FILE}\n")
            return {"provider": provider, **provider_cfg}

        print(f"\n{sep}")
        print(f"  当前选择：{label}")
        print("  当前配置如下：")
        print(sep)
        print(f"  API Key  : {mask_key(provider_cfg.get('api_key', ''))}")
        print(f"  API 地址 : {provider_cfg.get('base_url', '')}")
        print(f"  模型     : {provider_cfg.get('model', '')}")
        prompt_preview = provider_cfg.get("prompt", "")[:50]
        print(f"  提示词   : {prompt_preview}{'...' if len(provider_cfg.get('prompt','')) > 50 else ''}")
        print(dash)

        choice = input("\n使用此配置继续？(直接回车或输入 Y 继续 / 输入 N 重新配置 / 输入 S 切换提供商): ").strip().upper()
        if choice == "N":
            provider_cfg = prompt_provider_config(provider, provider_cfg)
            cfg["providers"][provider] = provider_cfg
            save_config_file(cfg)
            print(f"\n配置已保存到 {CONFIG_FILE}\n")
            return {"provider": provider, **provider_cfg}
        if choice == "S":
            continue
        else:
            save_config_file(cfg)
            return {"provider": provider, **provider_cfg}
# ==============================

BASE_URL = "https://ilinkai.weixin.qq.com"
COMMANDS_MSG = (
    "连接成功！\n"
    "可用指令：\n"
    "/help  /指令   - 查看全部指令列表\n"
    "/time          - 查询当前连接剩余时间\n"
    "/重新连接       - 立即触发重新连接（需确认）\n"
    "\n非指令输入即为 AI 对话"
)


class ILinkAPIError(RuntimeError):
    """iLink HTTP、JSON 或业务层错误。"""

    def __init__(self, message, *, path="", status=None, ret=None, errcode=None,
                 response=None, network_type=None):
        super().__init__(message)
        self.path = path
        self.status = status
        self.ret = ret
        self.errcode = errcode
        self.response = response or {}
        self.network_type = network_type

    @property
    def code(self):
        return self.ret if self.ret not in (None, 0) else self.errcode

    @property
    def is_stale_token(self):
        return self.code == -14


def make_common_headers():
    """2.4.6 GET/POST 共用的最小应用头。"""
    return {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
    }


def make_headers(token=None):
    """2.4.6 业务 POST 请求头；不手动设置 Content-Length。"""
    uin = str(secrets.randbits(32))
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": base64.b64encode(uin.encode()).decode(),
        **make_common_headers(),
    }
    if token and str(token).strip():
        headers["Authorization"] = f"Bearer {str(token).strip()}"
    return headers


def base_info():
    return {
        "channel_version": CHANNEL_VERSION,
        "bot_agent": sanitize_bot_agent(BOT_AGENT),
    }


def generate_client_id() -> str:
    """生成与 2.4.6 客户端格式一致、可用于消息幂等的唯一 ID。"""
    return f"openclaw-weixin:{int(time.time() * 1000)}-{secrets.token_hex(4)}"


def _redact_text(value):
    """避免把 token、二维码和上下文凭据写入终端日志。"""
    if isinstance(value, (dict, list, tuple)):
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value or "")
    else:
        text = str(value or "")
    patterns = (
        r"(?i)(bot_token|token|context_token|typing_ticket|qrcode|qrcode_img_content|verify_code|aeskey|aes_key|encrypt_query_param)=([^&\s,}\"]+)",
        r'(?i)("(?:bot_token|token|context_token|typing_ticket|qrcode|qrcode_img_content|verify_code|aeskey|aes_key|encrypt_query_param)"\s*:\s*")([^"]+)(")',
        r"(?i)('(?:bot_token|token|context_token|typing_ticket|qrcode|qrcode_img_content|verify_code|aeskey|aes_key|encrypt_query_param)'\s*:\s*')([^']+)(')",
        r'(?i)("local_token_list"\s*:\s*)\[[^\]]*\]',
    )
    for index, pattern in enumerate(patterns):
        if index == len(patterns) - 1:
            text = re.sub(pattern, r"\1[***]", text)
        else:
            text = re.sub(pattern, lambda m: f"{m.group(1)}***{m.group(3) if m.lastindex and m.lastindex >= 3 else ''}", text)
    return text[:500]


def _redact_path(path):
    # 查询串通常包含二维码、配对码或签名；保留参数名便于排障，不记录值。
    path = re.sub(r"(?i)([?&](?:qrcode|verify_code)=)[^&]*", r"\1***", str(path))
    return _redact_text(path)


def _network_type(exc):
    text = str(exc).upper()
    if any(code in text for code in ("ENOTFOUND", "EAI_AGAIN", "GETADDRINFO")):
        return "dns"
    if any(code in text for code in ("ECONNREFUSED", "ETIMEDOUT", "ENETUNREACH", "EHOSTUNREACH", "CONNECT_TIMEOUT")):
        return "tcp"
    if any(code in text for code in ("SSL", "TLS", "CERT", "UNABLE_TO_VERIFY")):
        return "tls"
    if isinstance(exc, asyncio.TimeoutError) or "TIMEOUT" in text:
        return "timeout"
    return "unknown"


def _parse_json_response(text, path, status):
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ILinkAPIError(
            f"{path} 返回内容不是有效 JSON: {exc}",
            path=path,
            status=status,
        ) from exc
    if not isinstance(data, dict):
        raise ILinkAPIError(
            f"{path} 返回 JSON 类型错误: {type(data).__name__}",
            path=path,
            status=status,
        )
    return data


def _log_response(method, path, status, text):
    print(f"  [{method} {_redact_path(path)}] HTTP {status} → {_redact_text(text)}")


def ensure_business_success(data, path):
    """检查 ret/errcode；字段缺省按官方客户端兼容规则视为成功。"""
    ret = data.get("ret")
    errcode = data.get("errcode")
    if ret not in (None, 0) or errcode not in (None, 0):
        message = data.get("errmsg") or "iLink 业务请求失败"
        raise ILinkAPIError(
            f"{path} ret={ret!r} errcode={errcode!r}: {message}",
            path=path,
            ret=ret,
            errcode=errcode,
            response=data,
        )
    return data


async def api_get(session, path, token=None, base_url=None, *, timeout=QR_STATUS_TIMEOUT,
                  long_poll=False):
    """执行 GET；二维码状态长轮询超时返回 wait，其余错误抛出。"""
    del token  # 2.4.6 官方 GET 二维码状态只发送公共应用头，不发送 Bearer。
    url = f"{(base_url or BASE_URL).rstrip('/')}/{path.lstrip('/')}"
    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout) if timeout else None
        async with session.get(url, headers=make_common_headers(), timeout=client_timeout) as res:
            text = await res.text()
            _log_response("GET", path, res.status, text)
            if not 200 <= res.status < 300:
                error_data = {}
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        error_data = parsed
                except (TypeError, ValueError):
                    pass
                raise ILinkAPIError(
                    f"GET {path} HTTP {res.status}: {error_data.get('errmsg', '')}".rstrip(),
                    path=path,
                    status=res.status,
                    ret=error_data.get("ret"),
                    errcode=error_data.get("errcode"),
                    response=error_data,
                )
            return _parse_json_response(text, path, res.status)
    except asyncio.TimeoutError as exc:
        if long_poll:
            return {"status": "wait", "_timeout": True}
        raise ILinkAPIError(
            f"GET {path} 超时", path=path, network_type="timeout",
        ) from exc
    except asyncio.CancelledError:
        raise
    except ILinkAPIError:
        raise
    except (aiohttp.ClientError, OSError) as exc:
        kind = _network_type(exc)
        raise ILinkAPIError(
            f"GET {path} 网络错误({kind}): {exc}",
            path=path,
            network_type=kind,
        ) from exc


async def api_post(session, path, body, token=None, base_url=None, *, timeout=API_TIMEOUT,
                   long_poll=False, fallback_cursor=""):
    """执行 JSON POST；不手动设置 Content-Length。"""
    url = f"{(base_url or BASE_URL).rstrip('/')}/{path.lstrip('/')}"
    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout) if timeout else None
        async with session.post(
            url,
            json=body,
            headers=make_headers(token),
            timeout=client_timeout,
        ) as res:
            text = await res.text()
            _log_response("POST", path, res.status, text)
            if not 200 <= res.status < 300:
                error_data = {}
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        error_data = parsed
                except (TypeError, ValueError):
                    pass
                raise ILinkAPIError(
                    f"POST {path} HTTP {res.status}: {error_data.get('errmsg', '')}".rstrip(),
                    path=path,
                    status=res.status,
                    ret=error_data.get("ret"),
                    errcode=error_data.get("errcode"),
                    response=error_data,
                )
            return _parse_json_response(text, path, res.status)
    except asyncio.TimeoutError as exc:
        if long_poll:
            return {
                "ret": 0,
                "msgs": [],
                "get_updates_buf": fallback_cursor,
                "_timeout": True,
            }
        raise ILinkAPIError(
            f"POST {path} 超时", path=path, network_type="timeout",
        ) from exc
    except asyncio.CancelledError:
        raise
    except ILinkAPIError:
        raise
    except (aiohttp.ClientError, OSError) as exc:
        kind = _network_type(exc)
        raise ILinkAPIError(
            f"POST {path} 网络错误({kind}): {exc}",
            path=path,
            network_type=kind,
        ) from exc


async def send_msg_safe(session, to_id, context_token, text, bot_token_ref, bot_base_url_ref):
    """发送微信消息，失败时降级为控制台打印，不抛异常。"""
    if not to_id or not context_token:
        print(f"[重连通知] {_redact_text(text)}")
        return False
    try:
        client_id = generate_client_id()
        result = await api_post(
            session,
            "ilink/bot/sendmessage",
            {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_id,
                    "client_id": client_id,
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": [{"type": 1, "text_item": {"text": text}}],
                },
                "base_info": base_info(),
            },
            bot_token_ref[0],
            bot_base_url_ref[0] or None,
            timeout=API_TIMEOUT,
        )
        ensure_business_success(result, "sendmessage")
        safe_text = _redact_text(text)
        print(f"[消息] 已发送: {safe_text[:50]}{'...' if len(safe_text) > 50 else ''}")
        return True
    except ILinkAPIError as exc:
        if exc.is_stale_token:
            raise
        print(f"[重连通知] 发送失败({_redact_text(exc)})，降级打印: {_redact_text(text)}")
        return False
    except Exception as e:
        print(f"[重连通知] 发送失败({_redact_text(e)})，降级打印: {_redact_text(text)}")
        return False


async def send_typing_safe(session, user_id, typing_ticket, status,
                           bot_token_ref, bot_base_url_ref):
    """尽力发送输入状态；失效 token 继续向上抛出以触发受控重登录。"""
    if not user_id or not typing_ticket:
        return False
    try:
        result = await api_post(
            session,
            "ilink/bot/sendtyping",
            {
                "ilink_user_id": user_id,
                "typing_ticket": typing_ticket,
                "status": status,
                "base_info": base_info(),
            },
            bot_token_ref[0],
            bot_base_url_ref[0] or None,
            timeout=CONFIG_TIMEOUT,
        )
        ensure_business_success(result, "sendtyping")
        return True
    except ILinkAPIError as exc:
        if exc.is_stale_token:
            raise
        print(f"[输入状态] status={status} 发送失败: {_redact_text(exc)}")
        return False


async def get_typing_ticket_safe(session, user_id, context_token, cache,
                                 bot_token_ref, bot_base_url_ref):
    """按用户缓存 getconfig；失败时指数退避，但不阻断正常文字回复。"""
    now = time.time()
    entry = cache.get(user_id)
    if not isinstance(entry, dict):
        entry = None

    if entry is None or now >= float(entry.get("next_fetch_at", 0)):
        fetch_ok = False
        try:
            result = await api_post(
                session,
                "ilink/bot/getconfig",
                {
                    "ilink_user_id": user_id,
                    "context_token": context_token,
                    "base_info": base_info(),
                },
                bot_token_ref[0],
                bot_base_url_ref[0] or None,
                timeout=CONFIG_TIMEOUT,
            )
            ensure_business_success(result, "getconfig")
            cache[user_id] = {
                "typing_ticket": str(result.get("typing_ticket") or ""),
                "next_fetch_at": now + secrets.randbelow(CONFIG_CACHE_TTL + 1),
                "retry_delay": CONFIG_CACHE_INITIAL_RETRY,
            }
            fetch_ok = True
        except ILinkAPIError as exc:
            if exc.is_stale_token:
                raise
            print(f"[配置] getconfig 失败，忽略输入状态: {_redact_text(exc)}")
        except Exception as exc:
            print(f"[配置] getconfig 异常，忽略输入状态: {_redact_text(exc)}")

        if not fetch_ok:
            if entry is None:
                cache[user_id] = {
                    "typing_ticket": "",
                    "next_fetch_at": now + CONFIG_CACHE_INITIAL_RETRY,
                    "retry_delay": CONFIG_CACHE_INITIAL_RETRY,
                }
            else:
                previous_delay = max(
                    CONFIG_CACHE_INITIAL_RETRY,
                    float(entry.get("retry_delay", CONFIG_CACHE_INITIAL_RETRY)),
                )
                next_delay = min(previous_delay * 2, CONFIG_CACHE_MAX_RETRY)
                entry["next_fetch_at"] = now + next_delay
                entry["retry_delay"] = next_delay

    cached = cache.get(user_id) or {}
    return str(cached.get("typing_ticket") or "")


def extract_message_text(msg: dict) -> str:
    """遍历完整 item_list，提取文本/语音转写，避免只读取第一项。"""
    parts = []
    for item in msg.get("item_list") or []:
        if not isinstance(item, dict):
            continue
        text_item = item.get("text_item") or {}
        if text_item.get("text"):
            parts.append(str(text_item["text"]))
            continue
        voice_item = item.get("voice_item") or {}
        if voice_item.get("text"):
            parts.append(str(voice_item["text"]))
    return "\n".join(parts).strip()


async def do_reconnect(session, bot_token_ref, bot_base_url_ref, last_contact,
                       typing_ticket_cache, reconnect_asked, warning_active,
                       reconnect_in_progress, login_time_ref, cfg, runtime_state=None):
    """执行重连流程，并同步保存 2.4.6 token、账号 ID 与游标状态。"""
    runtime_state = runtime_state if isinstance(runtime_state, dict) else _empty_runtime_state()
    if reconnect_in_progress[0]:
        return
    reconnect_in_progress[0] = True
    warning_active[0] = False
    reconnect_asked.clear()

    print("[重连] 开始重连流程...")
    from_id = last_contact.get("from_id")
    ctx = last_contact.get("context_token")
    current_token = bot_token_ref[0]

    try:
        async def deliver_qrcode(content):
            """把当前二维码同时输出到终端并尽力发送给最近联系人。"""
            qr_msg = f"[重连] 请扫码完成新连接：{content}"
            print(f"[重连] 请扫码完成新连接：{_redact_text(content)}")
            # HTTP 图片链接已由 save_qrcode_content 渲染，其他格式在这里补渲染。
            if not content.startswith("http"):
                render_terminal_qr(content)
            await send_msg_safe(session, from_id, ctx, qr_msg, bot_token_ref, bot_base_url_ref)

        try:
            login_result = await login_with_qrcode(
                session,
                [current_token] if current_token else [],
                existing_state=runtime_state,
                on_qrcode=deliver_qrcode,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[重连] 二维码登录失败: {_redact_text(exc)}")
            await send_msg_safe(
                session,
                from_id,
                ctx,
                "[失败] 二维码登录未完成，下次到期前会再次提醒",
                bot_token_ref,
                bot_base_url_ref,
            )
            login_time_ref[0] = time.time()
            return
        if login_result.get("already_connected") and current_token:
            print("[重连] 服务端提示已连接过此 OpenClaw，继续沿用当前连接")
            new_token = current_token
            new_base_url = bot_base_url_ref[0] or BASE_URL
            new_bot_id = runtime_state.get("ilink_bot_id", "")
            new_user_id = runtime_state.get("ilink_user_id", "")
        else:
            new_token = login_result.get("bot_token")
            new_base_url = login_result.get("baseurl") or bot_base_url_ref[0] or BASE_URL
            new_bot_id = login_result.get("ilink_bot_id", "")
            new_user_id = login_result.get("ilink_user_id") or runtime_state.get("ilink_user_id", "")

        if not new_token:
            reason = "登录状态异常" if login_result.get("login_error") else "扫码超时"
            print(f"[重连] {reason}，重连未完成")
            await send_msg_safe(
                session,
                from_id,
                ctx,
                f"[失败] {reason}，重连未完成，下次到期前会再次提醒",
                bot_token_ref,
                bot_base_url_ref,
            )
            login_time_ref[0] = time.time()
            return

        old_bot_id = str(runtime_state.get("ilink_bot_id") or "")
        account_changed = bool(new_bot_id and old_bot_id != new_bot_id)
        old_base_url = bot_base_url_ref[0] or BASE_URL
        credentials_changed = bool(
            account_changed or new_token != current_token or new_base_url != old_base_url
        )
        if account_changed:
            runtime_state["contexts"] = {}
            last_contact["from_id"] = None
            last_contact["context_token"] = None

        if credentials_changed and current_token:
            await notify_lifecycle(
                session,
                "ilink/bot/msg/notifystop",
                current_token,
                old_base_url,
            )

        # 成功：原子替换 token、base_url；同账号保留游标，换账号时隔离状态。
        bot_token_ref[0] = new_token
        bot_base_url_ref[0] = new_base_url
        runtime_state.update({
            "bot_token": new_token,
            "baseurl": new_base_url,
            "ilink_bot_id": new_bot_id,
            "ilink_user_id": new_user_id,
            "get_updates_buf": "" if account_changed else runtime_state.get("get_updates_buf", ""),
            "last_contact": dict(last_contact),
        })
        save_runtime_state(runtime_state)
        if credentials_changed:
            typing_ticket_cache.clear()
            await notify_lifecycle(session, "ilink/bot/msg/notifystart", new_token, new_base_url)
            print("[重连] 新连接已建立，凭据已切换")
        else:
            print("[重连] 当前连接仍然有效，无需切换凭据")
        if not account_changed:
            completion_text = (
                "[完成] 新连接已建立，已自动切换，继续使用"
                if credentials_changed
                else "[完成] 当前连接仍然有效，可以继续使用"
            )
            await send_msg_safe(
                session,
                from_id,
                ctx,
                completion_text,
                bot_token_ref,
                bot_base_url_ref,
            )
        login_time_ref[0] = time.time()
    finally:
        reconnect_in_progress[0] = False


async def reconnect_timer_task(session, bot_token_ref, bot_base_url_ref, last_contact,
                                typing_ticket_cache, reconnect_asked, warning_active,
                                reconnect_in_progress, login_time_ref, cfg, runtime_state=None):
    """独立定时器任务，与主消息循环并发运行。"""
    runtime_state = runtime_state if isinstance(runtime_state, dict) else _empty_runtime_state()
    while True:
        try:
            elapsed = time.time() - login_time_ref[0]
            first_wait = max(0, cfg["session_duration"] - cfg["warning_before"] - elapsed)
            await asyncio.sleep(first_wait)
            remaining = login_time_ref[0] + cfg["session_duration"] - time.time()

            if remaining <= cfg["force_before"]:
                force_msg = "[自动] 连接即将到期，开始强制重新连接..."
                print(force_msg)
                if not last_contact.get("from_id") or not last_contact.get("context_token"):
                    print("[自动] 尚无最近联系人，跳过本轮自动重连提醒")
                    login_time_ref[0] = time.time()
                    continue
                await send_msg_safe(session, last_contact["from_id"], last_contact["context_token"],
                                    force_msg, bot_token_ref, bot_base_url_ref)
                await do_reconnect(session, bot_token_ref, bot_base_url_ref, last_contact,
                                   typing_ticket_cache, reconnect_asked, warning_active,
                                   reconnect_in_progress, login_time_ref, cfg, runtime_state)
                continue

            remaining_h = remaining / 3600
            warn_msg = f"[提醒] 连接还剩约 {remaining_h:.1f} 小时到期，是否现在重新连接？回复 Y 立即重连，N 稍后提醒"
            print(warn_msg)
            if not last_contact.get("from_id") or not last_contact.get("context_token"):
                print("[提醒] 尚无最近联系人，跳过本轮连接到期提醒")
                login_time_ref[0] = time.time()
                continue
            await send_msg_safe(session, last_contact["from_id"], last_contact["context_token"],
                                warn_msg, bot_token_ref, bot_base_url_ref)
            warning_active[0] = True

            while True:
                remaining = login_time_ref[0] + cfg["session_duration"] - time.time()
                if remaining <= cfg["force_before"]:
                    force_msg = "[自动] 连接即将到期，开始强制重新连接..."
                    print(force_msg)
                    await send_msg_safe(session, last_contact["from_id"], last_contact["context_token"],
                                        force_msg, bot_token_ref, bot_base_url_ref)
                    await do_reconnect(session, bot_token_ref, bot_base_url_ref, last_contact,
                                       typing_ticket_cache, reconnect_asked, warning_active,
                                       reconnect_in_progress, login_time_ref, cfg, runtime_state)
                    break

                wait_secs = max(0.0, min(float(cfg["reminder_interval"]),
                                         remaining - cfg["force_before"]))
                try:
                    await asyncio.wait_for(reconnect_asked.wait(), timeout=wait_secs)
                    # 用户回 Y，执行重连
                    await do_reconnect(session, bot_token_ref, bot_base_url_ref, last_contact,
                                       typing_ticket_cache, reconnect_asked, warning_active,
                                       reconnect_in_progress, login_time_ref, cfg, runtime_state)
                    break
                except asyncio.TimeoutError:
                    remaining = login_time_ref[0] + cfg["session_duration"] - time.time()
                    if remaining <= cfg["force_before"]:
                        continue  # 下一轮循环走强制重连分支
                    remaining_m = remaining / 60
                    remind_msg = (f"[提醒] 连接还剩约 {remaining_m:.0f} 分钟，"
                                  f"是否现在重新连接？回复 Y 立即重连，N 继续等待")
                    print(remind_msg)
                    # 用最新的 last_contact（可能已更新）
                    if last_contact.get("from_id") and last_contact.get("context_token"):
                        await send_msg_safe(session, last_contact["from_id"], last_contact["context_token"],
                                            remind_msg, bot_token_ref, bot_base_url_ref)
        except asyncio.CancelledError:
            raise
        except ILinkAPIError as exc:
            print(f"[自动重连] iLink 请求失败: {_redact_text(exc)}，稍后重新评估")
            await asyncio.sleep(RETRY_DELAY)
        except Exception as exc:
            print(f"[自动重连] 任务异常: {_redact_text(exc)}，稍后重新评估")
            await asyncio.sleep(RETRY_DELAY)


def render_terminal_qr(content: str):
    if not content:
        return
    print("\n扫码地址:", content)
    if content.startswith("http") and render_terminal_image_from_url(content):
        return
    render_generated_qr(content)


def render_terminal_image_from_url(url: str) -> bool:
    try:
        from PIL import Image
    except ImportError:
        return False

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        image = Image.open(io.BytesIO(data)).convert("L")
        max_width = 72
        scale = max(1, int(image.width / max_width))
        width = max(1, int(image.width / scale))
        height = max(1, int(image.height / scale))
        image = image.resize((width, height))
        print()
        for y in range(height):
            print("".join("██" if image.getpixel((x, y)) < 128 else "  " for x in range(width)))
        print()
        return True
    except Exception as e:
        print(f"二维码图片渲染失败，改用本地二维码生成方式: {e}")
        return False


def render_generated_qr(content: str):
    try:
        import qrcode
    except ImportError:
        print("未安装 qrcode/Pillow，无法在终端渲染二维码；安装 `pip install qrcode pillow` 后会自动显示。")
        return

    qr = qrcode.QRCode(border=1)
    qr.add_data(content)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    print()
    for row in matrix:
        print("".join("██" if cell else "  " for cell in row))
    print()


def save_qrcode_content(content: str):
    if not content:
        return
    if content.startswith("data:image/"):
        header, b64 = content.split(",", 1)
        m = re.search(r"data:image/(\w+)", header)
        ext = m.group(1) if m else "png"
        with open(f"qrcode.{ext}", "wb") as f:
            f.write(base64.b64decode(b64))
        print(f"二维码已保存到 qrcode.{ext}")
    elif content.startswith("<svg"):
        with open("qrcode.svg", "w", encoding="utf-8") as f:
            f.write(content)
        print("二维码已保存到 qrcode.svg，用浏览器打开")
    elif content.startswith("http"):
        render_terminal_qr(content)
    else:
        try:
            with open("qrcode.png", "wb") as f:
                f.write(base64.b64decode(content))
            print("二维码已保存到 qrcode.png")
        except Exception:
            render_terminal_qr(content)


async def fetch_login_qrcode(session, local_token_list=None, base_url=None):
    """按 2.4.6 协议申请二维码；local_token_list 最多传 10 个。"""
    # 兼容旧版调用 fetch_login_qrcode(session, base_url, local_token_list)。
    # 最新协议始终使用固定二维码入口，旧 base_url 参数不会改变入口。
    if isinstance(local_token_list, str):
        legacy_tokens = base_url if isinstance(base_url, (list, tuple)) else []
        local_token_list = legacy_tokens
    tokens = []
    for token in local_token_list or []:
        token = str(token or "").strip()
        if token and token not in tokens:
            tokens.append(token)
    body = {"local_token_list": tokens[:10]}
    data = await api_post(
        session,
        "ilink/bot/get_bot_qrcode?bot_type=3",
        body,
        None,
        BASE_URL,
        # 官方 2.1.4 起不再为申请二维码设置固定客户端超时。
        timeout=None,
    )
    ensure_business_success(data, "get_bot_qrcode")
    if data.get("qrcode"):
        return data

    # 只保留旧服务端的 GET 兼容兜底；2.4.6 官方流程为 POST。
    print("POST 获取二维码未返回 qrcode，尝试兼容旧版 GET 流程。")
    data = await api_get(
        session,
        "ilink/bot/get_bot_qrcode?bot_type=3",
        None,
        BASE_URL,
        timeout=None,
    )
    ensure_business_success(data, "get_bot_qrcode")
    return data


async def poll_login_status(session, qrcode, base_url=BASE_URL, verify_code=None):
    """轮询二维码状态。GET 请求不携带 Bearer token，超时视为 wait。"""
    endpoint = f"ilink/bot/get_qrcode_status?qrcode={quote(qrcode, safe='')}"
    if verify_code:
        endpoint += f"&verify_code={quote(verify_code, safe='')}"
    status = await api_get(
        session,
        endpoint,
        None,
        base_url or BASE_URL,
        timeout=QR_STATUS_TIMEOUT,
        long_poll=True,
    )
    if status.get("_timeout"):
        return {"status": "wait"}
    ensure_business_success(status, "get_qrcode_status")
    state = status.get("status", "")

    if state == "confirmed" or status.get("bot_token"):
        bot_token = status.get("bot_token")
        ilink_bot_id = status.get("ilink_bot_id")
        if not bot_token or not ilink_bot_id:
            return {"login_error": "confirmed 响应缺少 bot_token 或 ilink_bot_id"}
        return {
            "bot_token": bot_token,
            "baseurl": status.get("baseurl") or status.get("base_url") or base_url or BASE_URL,
            "ilink_bot_id": ilink_bot_id,
            "ilink_user_id": status.get("ilink_user_id", ""),
        }
    if state == "binded_redirect" or status.get("binded_redirect"):
        return {"already_connected": True}
    if state == "expired":
        return {"expired": True}
    if state == "scaned_but_redirect":
        redirect_host = status.get("redirect_host")
        if redirect_host:
            redirect_base = str(redirect_host)
            if not redirect_base.startswith("http"):
                redirect_base = f"https://{redirect_base}"
            return {"redirect_base": redirect_base.rstrip("/")}
        print("服务端要求切换扫码轮询节点，但未返回 redirect_host，继续使用当前节点。")
        return {}
    if state == "scaned":
        return {"scanned": True, "verify_code_accepted": bool(verify_code)}
    if state in ("need_verifycode", "verify_code_blocked") or status.get("need_verifycode"):
        if state == "verify_code_blocked":
            return {"verify_code_blocked": True}
        return {"need_verifycode": True, "retry_verifycode": bool(verify_code)}
    if state and state != "wait":
        print(f"登录状态: {state}，原始响应: {_redact_text(status)}")
    return {}


async def wait_login_confirmation(session, qrcode, base_url=BASE_URL, timeout_seconds=None,
                                  allow_already_connected=False):
    timeout_seconds = timeout_seconds or RECONNECT_CONFIG["qrcode_scan_timeout"]
    deadline = time.time() + timeout_seconds
    current_base_url = base_url or BASE_URL
    pending_verify_code = None
    scanned_printed = False

    while True:
        if time.time() >= deadline:
            return {"timeout": True}

        try:
            result = await poll_login_status(session, qrcode, current_base_url, pending_verify_code)
        except ILinkAPIError as exc:
            print(f"轮询扫码状态失败({exc.network_type or '业务'}): {_redact_text(exc)}，稍后重试")
            await asyncio.sleep(1)
            continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"轮询扫码状态失败: {_redact_text(exc)}，稍后重试")
            await asyncio.sleep(1)
            continue

        if result.get("bot_token"):
            return result
        if result.get("login_error"):
            return result
        if result.get("already_connected"):
            return result if allow_already_connected else {"already_connected": True}
        if result.get("expired") or result.get("verify_code_blocked"):
            return result
        if result.get("redirect_base"):
            current_base_url = result["redirect_base"]
            print(f"扫码轮询切换到新节点: {current_base_url}")
            continue
        if result.get("scanned"):
            if pending_verify_code and result.get("verify_code_accepted"):
                pending_verify_code = None
            if not scanned_printed:
                print("已扫码，等待手机端确认...")
                scanned_printed = True
        if result.get("need_verifycode"):
            prompt = (
                "你输入的数字不匹配，请重新输入: "
                if result.get("retry_verifycode")
                else "请输入手机微信显示的数字配对码: "
            )
            pending_verify_code = (await asyncio.to_thread(input, prompt)).strip()
            continue

        await asyncio.sleep(1)


async def login_with_qrcode(session, local_token_list=None, existing_state=None, on_qrcode=None):
    """执行官方二维码登录，最多展示 MAX_QR_REFRESH_COUNT 个二维码。"""
    # 与官方实现一致：初始二维码计为第 1 次，最多共展示 3 个二维码。
    refresh_count = 1
    deadline = time.time() + RECONNECT_CONFIG["qrcode_scan_timeout"]
    existing_state = existing_state or {}
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise RuntimeError("登录等待超时，请重新运行后再试。")
        data = await fetch_login_qrcode(session, local_token_list)
        qrcode = data.get("qrcode")
        if not qrcode:
            raise RuntimeError("二维码响应缺少 qrcode")
        qrcode_img_content = data.get("qrcode_img_content", "")

        print("qrcode:", _redact_text(qrcode))
        qr_content = str(qrcode_img_content or qrcode)
        try:
            save_qrcode_content(qr_content)
        except Exception as exc:
            print(f"二维码保存失败，将继续使用链接: {_redact_text(exc)}")
        if on_qrcode:
            callback_result = on_qrcode(qr_content)
            if inspect.isawaitable(callback_result):
                await callback_result
        print("等待扫码...")

        remaining = deadline - time.time()
        if remaining <= 0:
            raise RuntimeError("登录等待超时，请重新运行后再试。")
        login_result = await wait_login_confirmation(
            session,
            qrcode,
            BASE_URL,
            timeout_seconds=remaining,
            allow_already_connected=True,
        )
        if login_result.get("bot_token"):
            return login_result
        if login_result.get("already_connected"):
            old_token = str(existing_state.get("bot_token") or "").strip()
            if old_token:
                print("服务端提示已连接过，复用本地保存的 token。")
                return {
                    "bot_token": old_token,
                    "baseurl": existing_state.get("baseurl") or BASE_URL,
                    "ilink_bot_id": existing_state.get("ilink_bot_id", ""),
                    "ilink_user_id": existing_state.get("ilink_user_id", ""),
                    "already_connected": True,
                }
            print("服务端提示此端已连接过，但本地没有可复用 token，将重新生成二维码。")
        elif login_result.get("expired"):
            print("二维码已过期，正在重新生成...")
        elif login_result.get("verify_code_blocked"):
            print("多次输入配对码错误，正在刷新二维码...")
        elif login_result.get("login_error"):
            raise RuntimeError(login_result["login_error"])
        elif login_result.get("timeout"):
            raise RuntimeError("登录等待超时，请重新运行后再试。")

        refresh_count += 1
        if refresh_count > MAX_QR_REFRESH_COUNT:
            raise RuntimeError("二维码多次失效或登录失败，请稍后重试。")


async def notify_lifecycle(session, endpoint, token, base_url):
    """发送 msg/notifystart 或 msg/notifystop 生命周期通知。"""
    try:
        result = await api_post(
            session,
            endpoint,
            {"base_info": base_info()},
            token,
            base_url or BASE_URL,
            timeout=CONFIG_TIMEOUT,
        )
        ensure_business_success(result, endpoint)
        print(f"[生命周期] {endpoint} 已通知")
        return True
    except ILinkAPIError as exc:
        print(f"[生命周期] {endpoint} 通知失败: {_redact_text(exc)}")
        return False
    except Exception as exc:
        print(f"[生命周期] {endpoint} 通知异常: {_redact_text(exc)}")
        return False


async def main():
    """运行 Python Bot，并实现 2.4.6 的登录、长轮询和优雅停止流程。"""
    runtime_state = load_runtime_state()
    saved_token = str(runtime_state.get("bot_token") or "").strip()

    async with aiohttp.ClientSession() as session:
        if saved_token:
            # 官方客户端会按账号复用本地凭据；若服务端随后返回 -14，
            # 消息循环会停止紧密轮询并进入受控二维码重登录。
            print("[登录] 复用本地保存的微信连接；token 失效时会自动要求重新扫码。")
            login_result = {
                "bot_token": saved_token,
                "baseurl": runtime_state.get("baseurl") or BASE_URL,
                "ilink_bot_id": runtime_state.get("ilink_bot_id", ""),
                "ilink_user_id": runtime_state.get("ilink_user_id", ""),
            }
        else:
            login_result = await login_with_qrcode(
                session,
                [],
                existing_state=runtime_state,
            )
        bot_token = str(login_result.get("bot_token") or "").strip()
        if not bot_token:
            raise RuntimeError("登录响应缺少 bot_token")

        bot_base_url = login_result.get("baseurl") or runtime_state.get("baseurl") or BASE_URL
        old_account_id = str(runtime_state.get("ilink_bot_id") or "")
        new_account_id = str(login_result.get("ilink_bot_id") or old_account_id)
        if new_account_id and old_account_id != new_account_id:
            # 账号切换时不能复用旧账号的游标和上下文。
            runtime_state["get_updates_buf"] = ""
            runtime_state["contexts"] = {}
            runtime_state["last_contact"] = {"from_id": "", "context_token": ""}

        runtime_state.update({
            "bot_token": bot_token,
            "baseurl": bot_base_url,
            "ilink_bot_id": new_account_id,
            "ilink_user_id": login_result.get("ilink_user_id") or runtime_state.get("ilink_user_id", ""),
        })
        save_runtime_state(runtime_state)

        print(f"登录成功！baseurl={bot_base_url}")
        print(f"{'=' * 40}\n{COMMANDS_MSG}\n{'=' * 40}")

        bot_token_ref = [bot_token]
        bot_base_url_ref = [bot_base_url]
        saved_contact = runtime_state.get("last_contact") or {}
        last_contact = {
            "from_id": saved_contact.get("from_id") or None,
            "context_token": saved_contact.get("context_token") or None,
        }
        typing_ticket_cache = {}
        welcomed_users = set(runtime_state.get("contexts", {}).keys())
        reconnect_asked = asyncio.Event()
        warning_active = [False]
        reconnect_in_progress = [False]
        login_time_ref = [time.time()]
        manual_reconnect_pending = {}

        await notify_lifecycle(session, "ilink/bot/msg/notifystart", bot_token, bot_base_url)

        async def apply_new_login(result):
            """原子替换登录凭据，并在账号变化时隔离游标/上下文。"""
            new_token = str(result.get("bot_token") or "").strip()
            if not new_token:
                raise RuntimeError("重新登录响应缺少 bot_token")
            old_id = str(runtime_state.get("ilink_bot_id") or "")
            new_id = str(result.get("ilink_bot_id") or old_id)
            old_token = str(runtime_state.get("bot_token") or bot_token_ref[0] or "")
            old_base_url = str(runtime_state.get("baseurl") or bot_base_url_ref[0] or BASE_URL)
            new_base_url = result.get("baseurl") or old_base_url
            account_changed = bool(new_id and old_id != new_id)
            credentials_changed = bool(
                account_changed or new_token != old_token or new_base_url != old_base_url
            )
            if account_changed:
                runtime_state["get_updates_buf"] = ""
                runtime_state["contexts"] = {}
                runtime_state["last_contact"] = {"from_id": "", "context_token": ""}
                last_contact["from_id"] = None
                last_contact["context_token"] = None
                welcomed_users.clear()
            bot_token_ref[0] = new_token
            bot_base_url_ref[0] = new_base_url
            runtime_state.update({
                "bot_token": new_token,
                "baseurl": bot_base_url_ref[0],
                "ilink_bot_id": new_id,
                "ilink_user_id": result.get("ilink_user_id") or runtime_state.get("ilink_user_id", ""),
                "get_updates_buf": runtime_state.get("get_updates_buf") or "",
            })
            typing_ticket_cache.clear()
            save_runtime_state(runtime_state)
            login_time_ref[0] = time.time()
            if credentials_changed:
                await notify_lifecycle(
                    session,
                    "ilink/bot/msg/notifystart",
                    bot_token_ref[0],
                    bot_base_url_ref[0],
                )

        async def handle_message(msg):
            if not isinstance(msg, dict) or msg.get("message_type") != 1:
                return
            from_id = str(msg.get("from_user_id") or "")
            context_token = str(msg.get("context_token") or "")
            if not from_id or not context_token:
                print("[消息] 缺少 from_user_id/context_token，跳过")
                return
            text = extract_message_text(msg)
            print(f"收到消息: {text or '[非文本消息]'}")

            last_contact.update({"from_id": from_id, "context_token": context_token})
            runtime_state.setdefault("contexts", {})[from_id] = context_token
            runtime_state["last_contact"] = dict(last_contact)
            save_runtime_state(runtime_state)

            normalized = text.strip().upper()
            if manual_reconnect_pending.get(from_id) and normalized in ("Y", "N"):
                manual_reconnect_pending.pop(from_id, None)
                if normalized == "Y":
                    await send_msg_safe(session, from_id, context_token, "好的，正在重新连接...",
                                        bot_token_ref, bot_base_url_ref)
                    await do_reconnect(
                        session, bot_token_ref, bot_base_url_ref, last_contact,
                        typing_ticket_cache, reconnect_asked, warning_active,
                        reconnect_in_progress, login_time_ref, RECONNECT_CONFIG,
                        runtime_state,
                    )
                else:
                    await send_msg_safe(session, from_id, context_token, "已取消重新连接",
                                        bot_token_ref, bot_base_url_ref)
                return

            if warning_active[0] and normalized in ("Y", "N"):
                if normalized == "Y":
                    reconnect_asked.set()
                    await send_msg_safe(session, from_id, context_token, "好的，正在重新连接...",
                                        bot_token_ref, bot_base_url_ref)
                else:
                    await send_msg_safe(session, from_id, context_token, "好的，稍后再提醒您",
                                        bot_token_ref, bot_base_url_ref)
                return

            if from_id not in welcomed_users:
                welcomed_users.add(from_id)
                await send_msg_safe(session, from_id, context_token, COMMANDS_MSG,
                                    bot_token_ref, bot_base_url_ref)
                return

            if not text:
                await send_msg_safe(
                    session, from_id, context_token,
                    "当前版本暂支持文字消息（语音转文字消息除外），图片/文件请稍后处理。",
                    bot_token_ref, bot_base_url_ref,
                )
                return

            if text in ("/help", "/指令"):
                await send_msg_safe(session, from_id, context_token, COMMANDS_MSG,
                                    bot_token_ref, bot_base_url_ref)
                return
            if text == "/time":
                remaining = max(0, login_time_ref[0] + RECONNECT_CONFIG["session_duration"] - time.time())
                hours, minutes, seconds = int(remaining // 3600), int((remaining % 3600) // 60), int(remaining % 60)
                display = f"{hours} 小时 {minutes} 分钟" if hours else f"{minutes} 分钟 {seconds} 秒"
                await send_msg_safe(session, from_id, context_token, f"当前连接剩余时间：{display}",
                                    bot_token_ref, bot_base_url_ref)
                return
            if text == "/重新连接":
                if reconnect_in_progress[0]:
                    await send_msg_safe(session, from_id, context_token, "重连正在进行中，请稍候...",
                                        bot_token_ref, bot_base_url_ref)
                else:
                    manual_reconnect_pending[from_id] = True
                    await send_msg_safe(
                        session, from_id, context_token,
                        "确认要立即重新连接吗？\n回复 Y 确认重连 / N 取消",
                        bot_token_ref, bot_base_url_ref,
                    )
                return

            typing_ticket = await get_typing_ticket_safe(
                session,
                from_id,
                context_token,
                typing_ticket_cache,
                bot_token_ref,
                bot_base_url_ref,
            )

            typing_started = False
            try:
                typing_started = await send_typing_safe(
                    session, from_id, typing_ticket, 1, bot_token_ref, bot_base_url_ref,
                )
                try:
                    loop = asyncio.get_running_loop()
                    reply = await loop.run_in_executor(executor, ai.chat, text)
                except Exception as exc:
                    print(f"[AI] 调用失败: {_redact_text(exc)}")
                    reply = "抱歉，AI 服务暂时不可用，请稍后再试。"
                reply = str(reply or "").strip() or "抱歉，我暂时没有生成有效回复。"

                client_id = generate_client_id()
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
                        "base_info": base_info(),
                    },
                    bot_token_ref[0],
                    bot_base_url_ref[0] or None,
                    timeout=API_TIMEOUT,
                )
                ensure_business_success(send_result, "sendmessage")
                safe_reply = _redact_text(reply)
                print(f"已回复: {safe_reply[:50]}{'...' if len(safe_reply) > 50 else ''}")
            finally:
                if typing_started:
                    await send_typing_safe(
                        session, from_id, typing_ticket, 2, bot_token_ref, bot_base_url_ref,
                    )

        async def message_loop():
            get_updates_buf = str(runtime_state.get("get_updates_buf") or "")
            long_poll_timeout = LONG_POLL_TIMEOUT
            consecutive_failures = 0
            print("开始监听消息...")
            while True:
                try:
                    # 定时/手动重连可能由另一个协程清空持久化游标。
                    # 每轮先同步一次，避免新连接继续使用旧账号的游标。
                    state_cursor = str(runtime_state.get("get_updates_buf") or "")
                    if state_cursor != get_updates_buf:
                        get_updates_buf = state_cursor
                        long_poll_timeout = LONG_POLL_TIMEOUT

                    request_token = bot_token_ref[0]
                    request_base_url = bot_base_url_ref[0] or BASE_URL
                    result = await api_post(
                        session,
                        "ilink/bot/getupdates",
                        {"get_updates_buf": get_updates_buf, "base_info": base_info()},
                        request_token,
                        request_base_url,
                        timeout=long_poll_timeout,
                        long_poll=True,
                        fallback_cursor=get_updates_buf,
                    )
                    if result.get("_timeout"):
                        await asyncio.sleep(0)
                        continue
                    if (
                        request_token != bot_token_ref[0]
                        or request_base_url != (bot_base_url_ref[0] or BASE_URL)
                    ):
                        # 重连期间完成的旧长轮询响应不能覆盖新连接的状态。
                        get_updates_buf = str(runtime_state.get("get_updates_buf") or "")
                        long_poll_timeout = LONG_POLL_TIMEOUT
                        continue
                    ensure_business_success(result, "getupdates")
                    consecutive_failures = 0

                    new_cursor = result.get("get_updates_buf")
                    if isinstance(new_cursor, str) and new_cursor:
                        get_updates_buf = new_cursor
                        runtime_state["get_updates_buf"] = new_cursor
                        save_runtime_state(runtime_state)

                    suggested_ms = result.get("longpolling_timeout_ms")
                    try:
                        if suggested_ms is not None and float(suggested_ms) > 0:
                            long_poll_timeout = max(
                                1.0,
                                min(MAX_LONG_POLL_TIMEOUT, float(suggested_ms) / 1000.0),
                            )
                    except (TypeError, ValueError):
                        pass

                    for msg in result.get("msgs") or []:
                        await handle_message(msg)
                except asyncio.CancelledError:
                    raise
                except ILinkAPIError as exc:
                    if exc.is_stale_token:
                        print("[iLink] ret/errcode=-14，当前 token 已失效，进入受控重新登录。")
                        runtime_state["bot_token"] = ""
                        save_runtime_state(runtime_state)
                        try:
                            await asyncio.sleep(RETRY_DELAY)
                            fresh_login = await login_with_qrcode(session, [], existing_state={})
                            await apply_new_login(fresh_login)
                            # 同账号重新登录沿用该账号游标；切换账号时
                            # apply_new_login 会清空持久化状态，这里同步本地变量。
                            get_updates_buf = str(runtime_state.get("get_updates_buf") or "")
                            long_poll_timeout = LONG_POLL_TIMEOUT
                            consecutive_failures = 0
                            continue
                        except asyncio.CancelledError:
                            raise
                        except Exception as relogin_exc:
                            print(f"[iLink] 重新登录失败: {_redact_text(relogin_exc)}")
                            consecutive_failures += 1
                            await asyncio.sleep(BACKOFF_DELAY)
                            continue
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        delay = BACKOFF_DELAY
                        consecutive_failures = 0
                    else:
                        delay = RETRY_DELAY
                    print(f"[iLink] getupdates 失败({exc.network_type or '业务'}): {_redact_text(exc)}；{delay}s 后重试")
                    await asyncio.sleep(delay)
                except Exception as exc:
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        delay = BACKOFF_DELAY
                        consecutive_failures = 0
                    else:
                        delay = RETRY_DELAY
                    print(f"[消息循环] 未分类异常: {_redact_text(exc)}；{delay}s 后重试")
                    await asyncio.sleep(delay)

        timer_task = asyncio.create_task(reconnect_timer_task(
            session, bot_token_ref, bot_base_url_ref, last_contact,
            typing_ticket_cache, reconnect_asked, warning_active,
            reconnect_in_progress, login_time_ref, RECONNECT_CONFIG, runtime_state,
        ))
        message_task = asyncio.create_task(message_loop())
        try:
            await message_task
        finally:
            for task in (message_task, timer_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(message_task, timer_task, return_exceptions=True)
            if bot_token_ref[0]:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(notify_lifecycle(
                            session,
                            "ilink/bot/msg/notifystop",
                            bot_token_ref[0],
                            bot_base_url_ref[0] or BASE_URL,
                        )),
                        timeout=CONFIG_TIMEOUT + 1,
                    )
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError as exc:
                    print(f"[生命周期] 停止通知超时: {_redact_text(exc)}")
                except Exception as exc:
                    print(f"[生命周期] 停止通知未完成: {_redact_text(exc)}")


def create_ai_client(raw_cfg: dict):
    """根据配置创建 AI 客户端，保持启动入口与协议代码解耦。"""
    if raw_cfg["provider"] == "deepseek":
        return DeepSeekAPI(DeepSeekConfig(
            api_key=raw_cfg["api_key"],
            base_url=raw_cfg["base_url"],
            model=raw_cfg["model"],
            prompt=raw_cfg["prompt"],
        ))
    return DusAPI(DusConfig(
        api_key=raw_cfg["api_key"],
        base_url=raw_cfg["base_url"],
        model1=raw_cfg["model"],
        prompt=raw_cfg["prompt"],
    ))


if __name__ == "__main__":
    print(
        "\n"
        "╔══════════════════════════════════════════════════════════╗\n"
        "║          微信 ClawBot  ·  WeChat iLink Bot               ║\n"
        "║  Copyright (c) 2026 SiverKing. All rights reserved.     ║\n"
        "║  GitHub : https://github.com/SiverKing/weixin-ClawBot-API║\n"
        "╚══════════════════════════════════════════════════════════╝"
    )
    _raw_cfg = load_or_create_config()
    ai = create_ai_client(_raw_cfg)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已停止 Bot。")
