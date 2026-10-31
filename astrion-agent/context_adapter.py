"""可选的服务端微压缩适配器。默认纯客户端；不修改模型 API 协议。

HTTP 服务只选择允许清理的工具结果 ID；不能改写文本、用户指令或证据。
它是独立的上下文编辑服务契约，不假定某个 Chat Completions 服务支持私有字段。
"""
from __future__ import annotations

import json
import os
from urllib.parse import urlparse
import urllib.request


class HttpContextAdapter:
    name = 'http-context-edits-v1'

    def __init__(self, url, *, key=None, timeout=5):
        parsed = urlparse(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError('上下文服务必须使用不含内嵌凭据的 HTTP(S) URL')
        if timeout <= 0 or timeout > 30:
            raise ValueError('上下文服务 timeout 必须在 0 到 30 秒之间')
        self.url, self.key, self.timeout = url, key, timeout

    def select(self, candidates, budget_chars):
        payload = {'version': 1, 'operation': 'select_tool_results',
                   'budget_chars': budget_chars, 'candidates': candidates}
        headers = {'Content-Type': 'application/json'}
        if self.key:
            headers['Authorization'] = f'Bearer {self.key}'
        request = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers, method='POST')
        # 适配器按请求选择，不跟随重定向向其他地址转发配置的凭据。
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=self.timeout) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError('上下文服务响应过大')
        result = json.loads(raw)
        if not isinstance(result, dict) or result.get('version') != 1:
            raise ValueError('上下文服务协议不匹配')
        selected = result.get('clear_ids')
        allowed = {c['id'] for c in candidates}
        if (not isinstance(selected, list) or any(type(i) is not int or i not in allowed for i in selected)
                or len(set(selected)) != len(selected)):
            raise ValueError('上下文服务返回了越界或重复的清理 ID')
        return selected


def adapter_from_env():
    url = os.environ.get('ASTRION_CONTEXT_SERVICE_URL', '').strip()
    if not url:
        return None
    return HttpContextAdapter(url, key=os.environ.get('ASTRION_CONTEXT_SERVICE_KEY'),
                              timeout=float(os.environ.get('ASTRION_CONTEXT_SERVICE_TIMEOUT', '5')))
