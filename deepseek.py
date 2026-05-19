import time
from dataclasses import dataclass

import requests

version = "1.0.1"


def log(message, level="INFO"):
    print(f"[{level}] {message}")


@dataclass
class DeepSeekConfig:
    api_key: str
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"
    prompt: str = "你是一个有帮助的AI助手。"


class DeepSeekAPI:
    """DeepSeek OpenAI-compatible chat/completions client."""

    def __init__(self, config: DeepSeekConfig):
        self.config = config
        self.api_key = config.api_key
        self.base_url = config.base_url.rstrip("/")
        self.model = config.model

    def chat(self, message, model=None, stream=False, prompt=None, history=None):
        if stream:
            log("DeepSeekAPI 当前封装未启用流式响应，已按非流式请求处理", "WARN")
        if model is None:
            model = self.model
        if prompt is None:
            prompt = self.config.prompt

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"siver-weixin_clawbot-api/{version}",
        }
        messages = [{"role": "system", "content": prompt}]
        if history:
            for h in history:
                role = "assistant" if h.get("attr") == "self" else "user"
                text = h.get("content", "")
                t = h.get("time", "")
                content = f"[{t}] {text}" if t else text
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": message})

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": 1024,
            "stream": False,
        }
        if model == "deepseek-v4-flash":
            payload["thinking"] = {"type": "disabled"}
        endpoint = f"{self.base_url}/chat/completions"
        retry_delays = [2, 4, 8, 16, 32]
        max_retries = 5
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                response = requests.post(endpoint, headers=headers, json=payload, timeout=60)
                response.raise_for_status()
                data = response.json()
                result = data["choices"][0]["message"].get("content", "")
                if not result:
                    log("DeepSeekAPI 响应中未找到文本内容", "WARN")
                    return "AI 未返回有效内容"

                if attempt > 0:
                    log(f"DeepSeekAPI 第 {attempt} 次重试成功：{result[:100]}...")
                else:
                    log(f"DeepSeekAPI 返回成功：{result[:100]}...")
                return result

            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    delay = retry_delays[attempt]
                    log(f"DeepSeekAPI 第 {attempt + 1} 次失败（{type(e).__name__}），{delay}s 后重试...", "WARNING")
                    time.sleep(delay)
                else:
                    log(f"DeepSeekAPI 已重试 {max_retries} 次，最终失败: {last_error}", "ERROR")

        return "API接口失效，请联系管理员"
