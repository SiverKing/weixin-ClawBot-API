import time
import requests
from dataclasses import dataclass, field
# dusapi注册地址：https://dusapi.com
version = "1.0.1"

# dusapi注册地址：https://dusapi.com
def log(message, level="INFO"):
    print(f"[{level}] {message}")


@dataclass
class DusConfig:
    api_key: str
    base_url: str
    model1: str = "claude-sonnet-4-5"
    prompt: str = "你是一个有帮助的AI助手。"


class DusAPI:
    """
    DusAPI 兼容接口封装类
    两种模型均使用 Anthropic 格式（x-api-key + /v1/messages），
    根据模型名称自动选择响应解析方式：
    - 包含 'claude' → 按 claude.py 解析（content[0]['text']）
    - 包含 'gpt' 或其他 → 按 gpt.py 解析（遍历 content 找 type=='text'）
    """

    def __init__(self, config: DusConfig):
        self.config = config
        self.DS_NOW_MOD = config.model1
        self.api_key = config.api_key
        self.base_url = config.base_url.rstrip('/')

    def chat(self, message, model=None, stream=False, prompt=None, history=None):
        if model is None:
            model = self.DS_NOW_MOD
        if prompt is None:
            prompt = self.config.prompt

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            'user-agent': f'siver-weixin_clawbot-api/{version}'
        }
        # Anthropic /v1/messages 格式：system 必须是顶层字段，messages 只允许 user/assistant
        messages = []
        if history:
            for h in history:
                role = "assistant" if h.get('attr') == 'self' else "user"
                t = h.get('time', '')
                content = f"[{t}] {h.get('content', '')}" if t else h.get('content', '')
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": message})
        payload = {
            "model": model,
            "max_tokens": 1024,
            "system": prompt,
            "messages": messages,
        }
        api_endpoint = f"{self.base_url}/v1/messages"
        # 梯度重试间隔（秒）：第1次失败后等2s，第2次4s，第3次8s，第4次16s，第5次32s
        retry_delays = [2, 4, 8, 16, 32]
        max_retries  = 5
        last_error   = None

        for attempt in range(max_retries + 1):
            try:
                response = requests.post(api_endpoint, headers=headers, json=payload, timeout=30)
                response.raise_for_status()
                response.encoding = 'utf-8'
                response_data = response.json()

                if 'claude' in model.lower():
                    result = response_data['content'][0]['text']
                else:
                    result = None
                    for content_block in response_data['content']:
                        if content_block.get('type') == 'text':
                            result = content_block['text']
                            break
                    if result is None:
                        log(level="WARN", message="DusAPI 响应中未找到文本内容")
                        return "AI 未返回有效内容"

                if attempt > 0:
                    log(message=f"DusAPI 第 {attempt} 次重试成功：{result[:100]}...")
                else:
                    log(message=f"DusAPI 返回成功：{result[:100]}...")
                return result

            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    delay = retry_delays[attempt]
                    log(level="WARNING", message=f"DusAPI 第 {attempt + 1} 次失败（{type(e).__name__}），{delay}s 后重试...")
                    time.sleep(delay)
                else:
                    log(level="ERROR", message=f"DusAPI 已重试 {max_retries} 次，最终失败: {last_error}")

        return "API接口失效，请联系管理员"
