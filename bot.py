import asyncio
import base64
import io
import json
import os
import random
import re
import aiohttp
import time
import urllib.request
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor
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
    "qrcode_scan_timeout":       600,  # 等待用户扫码最长时间（秒）
}
# =============================================

# ========== 配置文件 ==========
CONFIG_FILE = "config.json"
_DEFAULT_PROMPT = "你是一个有帮助的AI助手，请用中文简洁地回复。字数尽量少一些"
CHANNEL_VERSION = "2.4.3"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = str((2 << 16) | (4 << 8) | 3)
BOT_AGENT = "weixin-ClawBot-API/1.0.1 (python)"

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


def make_headers(token=None):
    uin = str(random.randint(0, 0xFFFFFFFF))
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": base64.b64encode(uin.encode()).decode(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def base_info():
    return {
        "channel_version": CHANNEL_VERSION,
        "bot_agent": BOT_AGENT,
    }


async def api_get(session, path, token=None, base_url=None):
    url = f"{base_url or BASE_URL}/{path}"
    async with session.get(url, headers=make_headers(token)) as res:
        text = await res.text()
        print(f"  [GET {path}] HTTP {res.status} → {text[:200]}")
        try:
            return json.loads(text)
        except Exception:
            return {}


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


async def send_msg_safe(session, to_id, context_token, text, bot_token_ref, bot_base_url_ref):
    """发送微信消息，失败时降级为控制台打印，不抛异常。"""
    if not to_id or not context_token:
        print(f"[重连通知] {text}")
        return
    try:
        client_id = f"openclaw-weixin-{random.randint(0, 0xFFFFFFFF):08x}"
        await api_post(
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
        )
    except Exception as e:
        print(f"[重连通知] 发送失败({e})，降级打印: {text}")


async def do_reconnect(session, bot_token_ref, bot_base_url_ref, last_contact,
                       typing_ticket_cache, reconnect_asked, warning_active,
                       reconnect_in_progress, login_time_ref, cfg):
    """执行重连流程。防重入，失败时优雅降级，成功后原子替换 token。"""
    if reconnect_in_progress[0]:
        return
    reconnect_in_progress[0] = True
    warning_active[0] = False
    reconnect_asked.clear()

    print("[重连] 开始重连流程...")
    from_id = last_contact["from_id"]
    ctx = last_contact["context_token"]

    _base = bot_base_url_ref[0] or BASE_URL
    try:
        data = await fetch_login_qrcode(session, _base, [bot_token_ref[0]] if bot_token_ref[0] else [])
        qrcode = data["qrcode"]
        qrcode_url = data.get("qrcode_img_content", qrcode)
    except Exception as e:
        print(f"[重连] 获取二维码失败: {e}")
        reconnect_in_progress[0] = False
        login_time_ref[0] = time.time()
        return

    # 发送二维码给用户（失败时控制台打印）
    qr_msg = f"[重连] 请扫码完成新连接：{qrcode_url}"
    print(qr_msg)
    render_terminal_qr(qrcode_url)
    await send_msg_safe(session, from_id, ctx, qr_msg, bot_token_ref, bot_base_url_ref)

    # 轮询扫码状态（带超时）
    login_result = await wait_login_confirmation(
        session,
        qrcode,
        _base,
        timeout_seconds=cfg["qrcode_scan_timeout"],
        allow_already_connected=True,
    )
    if login_result.get("already_connected"):
        print("[重连] 服务端提示已连接过此 OpenClaw，继续沿用当前连接")
        new_token = bot_token_ref[0]
        new_base_url = bot_base_url_ref[0]
    else:
        new_token = login_result.get("bot_token")
        new_base_url = login_result.get("baseurl", bot_base_url_ref[0])

    if new_token is None:
        # 扫码超时：重置计时，不 crash
        print("[重连] 扫码超时，重连未完成")
        await send_msg_safe(session, from_id, ctx,
                            "[失败] 扫码超时，重连未完成，下次到期前会再次提醒",
                            bot_token_ref, bot_base_url_ref)
        login_time_ref[0] = time.time()
        reconnect_in_progress[0] = False
        return

    # 成功：原子替换 token 和 base_url
    bot_token_ref[0] = new_token
    bot_base_url_ref[0] = new_base_url
    typing_ticket_cache.clear()
    print("[重连] 新连接已建立，token 已切换")
    await send_msg_safe(session, from_id, ctx,
                        "[完成] 新连接已建立，已自动切换，继续使用",
                        bot_token_ref, bot_base_url_ref)

    reconnect_in_progress[0] = False
    login_time_ref[0] = time.time()


async def reconnect_timer_task(session, bot_token_ref, bot_base_url_ref, last_contact,
                                typing_ticket_cache, reconnect_asked, warning_active,
                                reconnect_in_progress, login_time_ref, cfg):
    """独立定时器任务，与主消息循环并发运行。"""
    while True:
        # 等待到发警告的时间点
        elapsed = time.time() - login_time_ref[0]
        first_wait = max(0, cfg["session_duration"] - cfg["warning_before"] - elapsed)
        await asyncio.sleep(first_wait)

        # 检查剩余时间（可能因测试值设置而已超过 force_before）
        remaining = login_time_ref[0] + cfg["session_duration"] - time.time()
        if remaining <= cfg["force_before"]:
            force_msg = "[自动] 连接即将到期，开始强制重新连接..."
            print(force_msg)
            if not last_contact["from_id"] or not last_contact["context_token"]:
                print("[自动] 尚无最近联系人，跳过本轮自动重连提醒")
                login_time_ref[0] = time.time()
                continue
            await send_msg_safe(session, last_contact["from_id"], last_contact["context_token"],
                                force_msg, bot_token_ref, bot_base_url_ref)
            await do_reconnect(session, bot_token_ref, bot_base_url_ref, last_contact,
                               typing_ticket_cache, reconnect_asked, warning_active,
                               reconnect_in_progress, login_time_ref, cfg)
            continue

        # 发初次警告
        remaining_h = remaining / 3600
        warn_msg = f"[提醒] 连接还剩约 {remaining_h:.1f} 小时到期，是否现在重新连接？回复 Y 立即重连，N 稍后提醒"
        print(warn_msg)
        if not last_contact["from_id"] or not last_contact["context_token"]:
            print("[提醒] 尚无最近联系人，跳过本轮连接到期提醒")
            login_time_ref[0] = time.time()
            continue
        await send_msg_safe(session, last_contact["from_id"], last_contact["context_token"],
                            warn_msg, bot_token_ref, bot_base_url_ref)
        warning_active[0] = True

        # 询问循环
        while True:
            remaining = login_time_ref[0] + cfg["session_duration"] - time.time()
            if remaining <= cfg["force_before"]:
                force_msg = "[自动] 连接即将到期，开始强制重新连接..."
                print(force_msg)
                await send_msg_safe(session, last_contact["from_id"], last_contact["context_token"],
                                    force_msg, bot_token_ref, bot_base_url_ref)
                await do_reconnect(session, bot_token_ref, bot_base_url_ref, last_contact,
                                   typing_ticket_cache, reconnect_asked, warning_active,
                                   reconnect_in_progress, login_time_ref, cfg)
                break

            wait_secs = max(0.0, min(float(cfg["reminder_interval"]),
                                     remaining - cfg["force_before"]))
            try:
                await asyncio.wait_for(reconnect_asked.wait(), timeout=wait_secs)
                # 用户回 Y，执行重连
                await do_reconnect(session, bot_token_ref, bot_base_url_ref, last_contact,
                                   typing_ticket_cache, reconnect_asked, warning_active,
                                   reconnect_in_progress, login_time_ref, cfg)
                break
            except asyncio.TimeoutError:
                # 定时到，重新评估
                remaining = login_time_ref[0] + cfg["session_duration"] - time.time()
                if remaining <= cfg["force_before"]:
                    continue  # 下一轮循环走强制重连分支
                remaining_m = remaining / 60
                remind_msg = (f"[提醒] 连接还剩约 {remaining_m:.0f} 分钟，"
                              f"是否现在重新连接？回复 Y 立即重连，N 继续等待")
                print(remind_msg)
                # 用最新的 last_contact（可能已更新）
                await send_msg_safe(session, last_contact["from_id"], last_contact["context_token"],
                                    remind_msg, bot_token_ref, bot_base_url_ref)


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


async def fetch_login_qrcode(session, base_url=BASE_URL, local_token_list=None):
    body = {"local_token_list": local_token_list or []}
    data = await api_post(session, "ilink/bot/get_bot_qrcode?bot_type=3", body, None, base_url)
    if data.get("qrcode"):
        return data
    print("POST 获取二维码未返回 qrcode，尝试兼容旧版 GET 流程。")
    return await api_get(session, "ilink/bot/get_bot_qrcode?bot_type=3", None, base_url)


async def poll_login_status(session, qrcode, base_url=BASE_URL, verify_code=None):
    endpoint = f"ilink/bot/get_qrcode_status?qrcode={quote(qrcode, safe='')}"
    if verify_code:
        endpoint += f"&verify_code={quote(verify_code, safe='')}"
    status = await api_get(session, endpoint, None, base_url)
    state = status.get("status", "")

    if state == "confirmed" or status.get("bot_token"):
        return {
            "bot_token": status.get("bot_token"),
            "baseurl": status.get("baseurl") or status.get("base_url") or base_url,
            "ilink_bot_id": status.get("ilink_bot_id"),
            "ilink_user_id": status.get("ilink_user_id"),
        }
    if state == "binded_redirect" or status.get("binded_redirect"):
        return {"already_connected": True}
    if state == "expired":
        return {"expired": True}
    if state == "scaned_but_redirect":
        redirect_host = status.get("redirect_host")
        if redirect_host:
            return {"redirect_base": f"https://{redirect_host}"}
        print("服务端要求切换扫码轮询节点，但未返回 redirect_host，继续使用当前节点。")
        return {}
    if state == "scaned":
        return {"scanned": True, "verify_code_accepted": bool(verify_code)}
    elif state in ("need_verifycode", "verify_code_blocked") or status.get("need_verifycode"):
        if state == "verify_code_blocked":
            return {"verify_code_blocked": True}
        return {"need_verifycode": True, "retry_verifycode": bool(verify_code)}
    elif state and state != "wait":
        print(f"登录状态: {state}，原始响应: {status}")

    if status.get("local_token_list"):
        print("服务端返回 local_token_list 信息，继续等待扫码确认。")
    return {}


async def wait_login_confirmation(session, qrcode, base_url=BASE_URL, timeout_seconds=None,
                                  allow_already_connected=False):
    deadline = time.time() + timeout_seconds if timeout_seconds else None
    current_base_url = base_url
    pending_verify_code = None
    scanned_printed = False

    while True:
        if deadline and time.time() >= deadline:
            return {"timeout": True}

        try:
            result = await poll_login_status(session, qrcode, current_base_url, pending_verify_code)
        except Exception as e:
            print(f"轮询扫码状态失败，稍后重试: {e}")
            await asyncio.sleep(1)
            continue

        if result.get("bot_token"):
            return result
        if result.get("already_connected"):
            return result if allow_already_connected else {"already_connected": True}
        if result.get("expired"):
            return result
        if result.get("verify_code_blocked"):
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
            prompt = "你输入的数字不匹配，请重新输入: " if result.get("retry_verifycode") else "请输入手机微信显示的数字配对码: "
            pending_verify_code = input(prompt).strip()
            continue

        await asyncio.sleep(1)


async def login_with_qrcode(session, base_url=BASE_URL):
    refresh_count = 0
    max_refresh_count = 3
    while True:
        data = await fetch_login_qrcode(session, base_url)
        qrcode = data["qrcode"]
        qrcode_img_content = data.get("qrcode_img_content", "")

        print("qrcode:", qrcode)
        save_qrcode_content(str(qrcode_img_content or qrcode))
        print("等待扫码...")

        login_result = await wait_login_confirmation(session, qrcode, base_url)
        if login_result.get("bot_token"):
            return login_result
        if login_result.get("already_connected"):
            print("服务端提示此端已连接过，但当前独立程序没有可复用 token，将重新生成二维码。")
        elif login_result.get("expired"):
            print("二维码已过期，正在重新生成...")
        elif login_result.get("verify_code_blocked"):
            print("多次输入配对码错误，正在刷新二维码...")
        elif login_result.get("timeout"):
            print("登录等待超时，正在重新生成二维码...")

        refresh_count += 1
        if refresh_count >= max_refresh_count:
            raise RuntimeError("二维码多次失效或登录失败，请稍后重试。")


async def main():
    async with aiohttp.ClientSession() as session:
        # 1. 获取二维码并等待扫码
        login_result = await login_with_qrcode(session)
        bot_token = login_result["bot_token"]
        bot_base_url = login_result.get("baseurl", "")
        print(f"登录成功！baseurl={bot_base_url}")
        print(f"{'='*40}\n{COMMANDS_MSG}\n{'='*40}")

        # 3. 共享状态（可变引用，传给定时器任务和消息循环）
        bot_token_ref = [bot_token]
        bot_base_url_ref = [bot_base_url]
        last_contact = {"from_id": None, "context_token": None}
        typing_ticket_cache = {}
        welcomed_users = set()
        reconnect_asked = asyncio.Event()
        warning_active = [False]
        reconnect_in_progress = [False]
        login_time_ref = [time.time()]
        manual_reconnect_pending = {}  # {from_id: True} 等待用户确认手动重连

        # 4. 启动定时器任务（与消息循环并发）
        asyncio.create_task(reconnect_timer_task(
            session, bot_token_ref, bot_base_url_ref, last_contact,
            typing_ticket_cache, reconnect_asked, warning_active,
            reconnect_in_progress, login_time_ref, RECONNECT_CONFIG,
        ))

        # 5. 长轮询收消息
        get_updates_buf = ""
        print("开始监听消息...")
        while True:
            result = await api_post(
                session,
                "ilink/bot/getupdates",
                {"get_updates_buf": get_updates_buf, "base_info": base_info()},
                bot_token_ref[0],
                bot_base_url_ref[0] or None,
            )
            get_updates_buf = result.get("get_updates_buf") or get_updates_buf

            for msg in result.get("msgs") or []:
                if msg.get("message_type") != 1:
                    continue
                text = msg.get("item_list", [{}])[0].get("text_item", {}).get("text", "")
                from_id = msg["from_user_id"]
                context_token = msg["context_token"]
                print(f"收到消息: {text}")

                # 更新最近联系人（定时器任务用于发通知）
                last_contact["from_id"] = from_id
                last_contact["context_token"] = context_token

                # 优先级 1：手动重连 Y/N 确认（/重新连接 发出后等待回复）
                if manual_reconnect_pending.get(from_id) and text.strip().upper() in ("Y", "N"):
                    del manual_reconnect_pending[from_id]
                    if text.strip().upper() == "Y":
                        await send_msg_safe(session, from_id, context_token,
                                            "好的，正在重新连接...",
                                            bot_token_ref, bot_base_url_ref)
                        await do_reconnect(session, bot_token_ref, bot_base_url_ref, last_contact,
                                           typing_ticket_cache, reconnect_asked, warning_active,
                                           reconnect_in_progress, login_time_ref, RECONNECT_CONFIG)
                    else:
                        await send_msg_safe(session, from_id, context_token,
                                            "已取消重新连接",
                                            bot_token_ref, bot_base_url_ref)
                    continue

                # 优先级 2：定时预警 Y/N 处理
                if warning_active[0] and text.strip().upper() in ("Y", "N"):
                    if text.strip().upper() == "Y":
                        reconnect_asked.set()
                        await send_msg_safe(session, from_id, context_token,
                                            "好的，正在重新连接...",
                                            bot_token_ref, bot_base_url_ref)
                    else:
                        await send_msg_safe(session, from_id, context_token,
                                            "好的，稍后再提醒您",
                                            bot_token_ref, bot_base_url_ref)
                    continue

                # 优先级 3：首次交互，发送指令列表
                if from_id not in welcomed_users:
                    welcomed_users.add(from_id)
                    await send_msg_safe(session, from_id, context_token,
                                        COMMANDS_MSG, bot_token_ref, bot_base_url_ref)
                    continue

                # /help  /指令 — 返回指令列表
                if text.strip() in ("/help", "/指令"):
                    await send_msg_safe(session, from_id, context_token,
                                        COMMANDS_MSG, bot_token_ref, bot_base_url_ref)
                    continue

                # /time 指令
                if text.strip() == "/time":
                    _rem = max(0, login_time_ref[0] + RECONNECT_CONFIG["session_duration"] - time.time())
                    _h, _m, _s = int(_rem // 3600), int((_rem % 3600) // 60), int(_rem % 60)
                    _ts = f"{_h} 小时 {_m} 分钟" if _h > 0 else f"{_m} 分钟 {_s} 秒"
                    await send_msg_safe(session, from_id, context_token,
                                        f"当前连接剩余时间：{_ts}",
                                        bot_token_ref, bot_base_url_ref)
                    continue

                # /重新连接 — 手动触发重连，等待 Y/N 确认
                if text.strip() == "/重新连接":
                    if reconnect_in_progress[0]:
                        await send_msg_safe(session, from_id, context_token,
                                            "重连正在进行中，请稍候...",
                                            bot_token_ref, bot_base_url_ref)
                    else:
                        manual_reconnect_pending[from_id] = True
                        await send_msg_safe(session, from_id, context_token,
                                            "确认要立即重新连接吗？\n回复 Y 确认重连 / N 取消",
                                            bot_token_ref, bot_base_url_ref)
                    continue

                # getconfig 获取 typing_ticket（每个用户缓存一次）
                if from_id not in typing_ticket_cache:
                    cfg = await api_post(
                        session,
                        "ilink/bot/getconfig",
                        {"ilink_user_id": from_id, "context_token": context_token,
                         "base_info": base_info()},
                        bot_token_ref[0],
                        bot_base_url_ref[0] or None,
                    )
                    typing_ticket_cache[from_id] = cfg.get("typing_ticket", "")
                typing_ticket = typing_ticket_cache[from_id]

                # sendtyping status=1 表示"正在输入"
                if typing_ticket:
                    await api_post(
                        session,
                        "ilink/bot/sendtyping",
                        {"ilink_user_id": from_id, "typing_ticket": typing_ticket,
                         "status": 1, "base_info": base_info()},
                        bot_token_ref[0],
                        bot_base_url_ref[0] or None,
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
                        "base_info": base_info(),
                    },
                    bot_token_ref[0],
                    bot_base_url_ref[0] or None,
                )
                print(f"sendmessage 返回: {send_result}")
                print(f"已回复: {reply[:50]}...")

                # sendtyping status=2 取消"正在输入"
                if typing_ticket:
                    await api_post(
                        session,
                        "ilink/bot/sendtyping",
                        {"ilink_user_id": from_id, "typing_ticket": typing_ticket,
                         "status": 2, "base_info": base_info()},
                        bot_token_ref[0],
                        bot_base_url_ref[0] or None,
                    )


print(
    "\n"
    "╔══════════════════════════════════════════════════════════╗\n"
    "║          微信 ClawBot  ·  WeChat iLink Bot               ║\n"
    "║  Copyright (c) 2026 SiverKing. All rights reserved.     ║\n"
    "║  GitHub : https://github.com/SiverKing/weixin-ClawBot-API║\n"
    "╚══════════════════════════════════════════════════════════╝"
)

_raw_cfg = load_or_create_config()
if _raw_cfg["provider"] == "deepseek":
    ai = DeepSeekAPI(DeepSeekConfig(
        api_key=_raw_cfg["api_key"],
        base_url=_raw_cfg["base_url"],
        model=_raw_cfg["model"],
        prompt=_raw_cfg["prompt"],
    ))
else:
    ai = DusAPI(DusConfig(
        api_key=_raw_cfg["api_key"],
        base_url=_raw_cfg["base_url"],
        model1=_raw_cfg["model"],
        prompt=_raw_cfg["prompt"],
    ))
asyncio.run(main())
