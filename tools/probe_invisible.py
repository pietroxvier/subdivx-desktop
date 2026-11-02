"""Teste isolado do Subdivx com InvisiblePlaywright, sem exportar a sessão."""
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    from invisible_playwright import InvisiblePlaywright

    print('Iniciando Firefox de teste...', flush=True)
    with InvisiblePlaywright(headless=True, timezone='America/Sao_Paulo') as browser:
        page = browser.new_page()
        response = page.goto('https://subdivx.com/', wait_until='domcontentloaded', timeout=60000)
        print(json.dumps({'stage': 'navigation', 'status': response.status if response else None,
                          'title': page.title()}, ensure_ascii=True), flush=True)
        deadline = time.monotonic() + 25
        version = None
        while time.monotonic() < deadline:
            version = re.search(r'(?:index-min\.js|sdx-min\.css)\?v=([0-9.]+)', page.content(), re.I)
            if version:
                break
            page.wait_for_timeout(1000)
        if not version:
            print(json.dumps({'stage': 'blocked', 'title': page.title(), 'frontend_found': False}), flush=True)
            return 2
        print('Frontend do Subdivx carregado.', flush=True)
        result = page.evaluate('''async (suffix) => {
            const headers = {'X-Requested-With': 'XMLHttpRequest'};
            const tokenResponse = await fetch('/inc/gt.php?gt=1', {headers, credentials:'same-origin'});
            if (!tokenResponse.ok) return {stage:'token', status:tokenResponse.status};
            const tokenPayload = await tokenResponse.json();
            if (!tokenPayload.token) return {stage:'token', token_found:false};
            const body = new URLSearchParams({tabla:'resultados', filtros:'', token:tokenPayload.token});
            body.set('buscar' + suffix, 'Dune 2021');
            const response = await fetch('/inc/ajax.php', {method:'POST', headers, body, credentials:'same-origin'});
            if (!response.ok) return {stage:'search', status:response.status};
            const payload = await response.json();
            return {stage:'search', status:response.status, results:(payload.aaData || []).map(
                x => ({id:x.id, title:x.titulo, description:x.descripcion, downloads:x.descargas}))};
        }''', version[1].replace('.', ''))
        print(json.dumps(result, ensure_ascii=True), flush=True)
        return 0 if result.get('results') else 3


if __name__ == '__main__':
    raise SystemExit(main())
