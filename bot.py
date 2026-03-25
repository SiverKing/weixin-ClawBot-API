import asyncio
import base64
import random
import re
import aiohttp
import time
from concurrent.futures import ThreadPoolExecutor
from dusapi import DusAPI, DusConfig

# dusapi注册地址：https://dusapi.com
# 或自行更改为你要接入的接口/AI，想先测试可以直接运行，接口返回失败也会自动回复消息
# ========== 配置 ==========
config = DusConfig(
    api_key="you-api-key",
    base_url="https://api.dusapi.com",
    model1="gpt-5",
    prompt="你是一个有帮助的AI助手，请用中文简洁地回复。字数尽量少一些",
)
ai = DusAPI(config)
executor = ThreadPoolExecutor(max_workers=4)
# ==========================

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
