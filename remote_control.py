#!/usr/bin/env python3
"""Local-only approval dashboard, exposed ONLY via private Tailscale Serve."""
import html
import json
import os
import secrets
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path

PRIVATE=Path(os.environ.get('GENSPARK_PRIVATE_DIR','private-runtime'))


def atomic(path,data):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False),encoding='utf8')
    os.chmod(tmp,0o600); tmp.replace(path)


def read(path):
    try:return json.loads(path.read_text(encoding='utf8'))
    except (OSError,ValueError):return {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        st=read(PRIVATE/'request.json')
        rid=st.get('id','')
        msg=html.escape(st.get('message','Сейчас подтверждение не требуется'))
        page=f'''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Подтверждение Genspark</title><style>body{{font:18px sans-serif;max-width:650px;margin:25px auto;padding:15px}}
button{{font:18px sans-serif;padding:18px;margin:10px}}pre{{white-space:pre-wrap}}</style>
<h2>Браузер работает на GitHub</h2><pre>{msg}</pre>
<p>Откройте удалённый экран и выполните действие вручную. Затем вернитесь сюда.</p>
<form method="POST"><input type="hidden" name="id" value="{html.escape(rid)}">
<button name="action" value="continue">Выполнено — продолжить</button>
<button name="action" value="pause">Сохранить и остановить</button></form>
<p><a href="/">Обновить статус</a></p>'''.encode()
        self.send_response(200);self.send_header('Content-Type','text/html;charset=utf-8')
        self.send_header('Cache-Control','no-store');self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','no-referrer'); self.end_headers();self.wfile.write(page)
    def do_POST(self):
        from urllib.parse import parse_qs,urlparse
        origin=self.headers.get('Origin','')
        if origin and urlparse(origin).netloc != self.headers.get('Host',''):
            self.send_error(403);return
        length=int(self.headers.get('Content-Length','0'))
        if length>4096:self.send_error(413);return
        d=parse_qs(self.rfile.read(length).decode())
        st=read(PRIVATE/'request.json')
        rid=d.get('id',[''])[0]; action=d.get('action',[''])[0]
        if not st.get('id') or not secrets.compare_digest(rid,st['id']) or action not in ('continue','pause'):
            self.send_error(409);return
        atomic(PRIVATE/'approval.json',{'id':rid,'action':action})
        self.send_response(303);self.send_header('Location','/');self.end_headers()


if __name__=='__main__':
    PRIVATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    ThreadingHTTPServer(('127.0.0.1',6081),Handler).serve_forever()
