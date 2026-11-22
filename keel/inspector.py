"""Local synthetic inspector. No filesystem routes, discovery, or providers."""
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import json
from keel.legal.controls import lint_legal_output

class Inspector(BaseHTTPRequestHandler):
    def reply(self, kind, data):
        self.send_response(200); self.send_header('Content-Type',kind); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        if self.path!='/': return self.send_error(404)
        self.reply('text/html; charset=utf-8',(Path(__file__).parent/'ui/web/static/index.html').read_bytes())
    def do_POST(self):
        if self.path!='/api/legal/lint': return self.send_error(404)
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=16384: return self.send_error(400)
            text=json.loads(self.rfile.read(size))['text']
            if not isinstance(text,str): return self.send_error(400)
        except (ValueError,KeyError,TypeError): return self.send_error(400)
        in_ledger = {'S0001'}.__contains__
        result = lint_legal_output(text, in_ledger)
        result['source_ids'] = [{'id': sid, 'found': in_ledger(sid)} for sid in result['citations']]
        self.reply('application/json', json.dumps(result).encode())
    def log_message(self,*args): pass
if __name__=='__main__':
    with HTTPServer(('127.0.0.1',0),Inspector) as server:
        print(f'Visit http://127.0.0.1:{server.server_port}/ (Ctrl+C to stop)',flush=True); server.serve_forever()
