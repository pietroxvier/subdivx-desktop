"""Diagnóstico sem cookies: imprime somente status e estrutura, nunca tokens."""
import json
import re
from pathlib import Path
import requests

session = requests.Session()
session.headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
base = 'https://subdivx.com'
for path in ['/', '/inc/gt.php?gt=1']:
    try:
        r = session.get(base + path, timeout=25)
        print(json.dumps({'path': path, 'status': r.status_code, 'url': r.url, 'type': r.headers.get('content-type'), 'version': re.findall(r'(?:index-min\.js|sdx-min\.css)\?v=([0-9.]+)', r.text), 'challenge': 'Just a moment' in r.text}, ensure_ascii=True))
    except requests.RequestException as exc:
        print(type(exc).__name__)
