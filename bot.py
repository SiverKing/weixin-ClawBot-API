import asyncio
import base64
import json
import os
import random
import re
import aiohttp
import time
from concurrent.futures import ThreadPoolExecutor
from dusapi import DusAPI, DusConfig

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


def mask_key(key: str) -> str:
    """保留前5位和后5位，中间用星号替换。"""
    if len(key) <= 10:
        return key
    return key[:5] + "*" * (len(key) - 10) + key[-5:]


def load_or_create_config() -> dict:
    """检查配置文件，不存在则引导用户创建，存在则显示并确认。"""
    sep = "=" * 60
    dash = "-" * 60
    while True:
        if not os.path.exists(CONFIG_FILE):
            print(f"\n{sep}")
            print("  首次运行，需要配置 API 信息")
            print(sep)
            print()
            print("  !! 重要提示 !!")
            print("  当前版本仅支持 DusAPI")
            print("  注册地址：https://dusapi.com")
            print("  如需使用其他 AI 接口，请前往 GitHub 拉取源代码自行修改")
            print(dash)

            api_key = input("\n请输入 API Key（留空使用默认值 your-api-key）: ").strip()
            if not api_key:
                api_key = "your-api-key"

            base_url = input("请输入 API 地址（留空默认 https://api.dusapi.com）: ").strip()
            if not base_url:
                base_url = "https://api.dusapi.com"

            model = input("请输入模型名称（留空默认 gpt-5）: ").strip()
            if not model:
                model = "gpt-5"

            prompt = input(f"请输入系统提示词（留空使用默认值）: ").strip()
            if not prompt:
                prompt = _DEFAULT_PROMPT

            cfg = {
                "api_key": api_key,
                "base_url": base_url,
                "model": model,
                "prompt": prompt,
            }
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            print(f"\n配置已保存到 {CONFIG_FILE}\n")
            return cfg

        else:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)

            print(f"\n{sep}")
            print("  检测到配置文件，当前配置如下：")
            print(sep)
            print(f"  API Key  : {mask_key(cfg.get('api_key', ''))}")
            print(f"  API 地址 : {cfg.get('base_url', '')}")
            print(f"  模型     : {cfg.get('model', '')}")
            prompt_preview = cfg.get("prompt", "")[:50]
            print(f"  提示词   : {prompt_preview}{'...' if len(cfg.get('prompt','')) > 50 else ''}")
            print(dash)

            choice = input("\n使用此配置继续？(直接回车或输入 Y 继续 / 输入 N 重新配置): ").strip().upper()
            if choice == "N":
                os.remove(CONFIG_FILE)
                continue  # 回到循环顶部重新创建
            return cfg
# ==============================

BASE_URL = "https://ilinkai.weixin.qq.com"
COMMANDS_MSG = "连接成功！\n可用指令：\n/time - 查询当前连接剩余时间\n\n非指令输入即为接口/AI对话"


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
                "base_info": {"channel_version": "1.0.2"},
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

    # 获取新二维码（必须带 bot_type=3，使用动态 base_url）
    _base = bot_base_url_ref[0] or BASE_URL
    try:
        async with session.get(
            f"{_base}/ilink/bot/get_bot_qrcode?bot_type=3"
        ) as res:
            data = await res.json(content_type=None)
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
    await send_msg_safe(session, from_id, ctx, qr_msg, bot_token_ref, bot_base_url_ref)

    # 轮询扫码状态（带超时）
    deadline = time.time() + cfg["qrcode_scan_timeout"]
    new_token = None
    new_base_url = None
    while time.time() < deadline:
        try:
            async with session.get(
                f"{_base}/ilink/bot/get_qrcode_status?qrcode={qrcode}"
            ) as res:
                status = await res.json(content_type=None)
            if status.get("status") == "confirmed":
                new_token = status["bot_token"]
                new_base_url = status.get("baseurl", bot_base_url_ref[0])
                break
        except Exception:
            pass
        await asyncio.sleep(1)

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
        bot_base_url = ""
        while True:
            async with session.get(
                f"{BASE_URL}/ilink/bot/get_qrcode_status?qrcode={qrcode}"
            ) as res:
                status = await res.json(content_type=None)

            if status.get("status") == "confirmed":
                bot_token = status["bot_token"]
                bot_base_url = status.get("baseurl", "")
                print(f"登录成功！baseurl={bot_base_url}")
                print(f"{'='*40}\n{COMMANDS_MSG}\n{'='*40}")
                break
            await asyncio.sleep(1)

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
                {"get_updates_buf": get_updates_buf, "base_info": {"channel_version": "1.0.2"}},
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

                # Y/N 重连询问处理（优先于 AI 回复）
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

                # 首次交互：发送指令列表
                if from_id not in welcomed_users:
                    welcomed_users.add(from_id)
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

                # getconfig 获取 typing_ticket（每个用户缓存一次）
                if from_id not in typing_ticket_cache:
                    cfg = await api_post(
                        session,
                        "ilink/bot/getconfig",
                        {"ilink_user_id": from_id, "context_token": context_token,
                         "base_info": {"channel_version": "1.0.2"}},
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
                        {"ilink_user_id": from_id, "typing_ticket": typing_ticket, "status": 1},
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
                        "base_info": {"channel_version": "1.0.2"},
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
                        {"ilink_user_id": from_id, "typing_ticket": typing_ticket, "status": 2},
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
ai = DusAPI(DusConfig(
    api_key=_raw_cfg["api_key"],
    base_url=_raw_cfg["base_url"],
    model1=_raw_cfg["model"],
    prompt=_raw_cfg["prompt"],
))
asyncio.run(main())
