"""Small, dependency-free deployment target. No infrastructure credentials."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def response(path):
    if path not in ('/', '/healthz'):
        return 404, {'error': 'not found'}
    return 200, {'service': 'beet-cd-demo', 'status': 'ok',
                 'release': os.environ.get('RELEASE_SHA', 'local'),
                 'message': 'BEET CI passed; K3s deployment is healthy.'}

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, body = response(self.path)
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
