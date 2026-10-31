"""可选上下文编辑服务的可运行参考实现：python3 -m context_service --port 8766。

使用确定性策略选择客户端已允许清理的全部候选。部署者可替换 select()，
客户端仍校验 ID 范围；此服务既不访问原始证据，也不调用模型。
"""
import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os


def select(payload):
    if (not isinstance(payload, dict) or payload.get('version') != 1
            or payload.get('operation') != 'select_tool_results'
            or type(payload.get('budget_chars')) is not int or payload['budget_chars'] < 1):
        raise ValueError('invalid request')
    candidates = payload.get('candidates')
    if not isinstance(candidates, list) or any(
            not isinstance(c, dict) or type(c.get('id')) is not int or c['id'] < 1
            or type(c.get('chars')) is not int or c['chars'] < 0
            or not isinstance(c.get('tool'), str) for c in candidates):
        raise ValueError('invalid candidates')
    ids = [c['id'] for c in candidates]
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate candidates')
    return {'version': 1, 'clear_ids': ids}


class ContextHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.path != '/context-edits':
            self.send_error(404)
            return
        key = getattr(self.server, 'context_key', None)
        if key and not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + key):
            self.send_error(401)
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 65536:
                raise ValueError('invalid size')
            self.connection.settimeout(5)
            result = select(json.loads(self.rfile.read(size)))
        except (ValueError, TypeError, OSError):
            self.send_error(400)
            return
        raw = json.dumps(result).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), ContextHandler)
    server.context_key = os.environ.get('ASTRION_CONTEXT_SERVICE_KEY')
    print(f'Context editing service: http://127.0.0.1:{server.server_port}/context-edits', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
