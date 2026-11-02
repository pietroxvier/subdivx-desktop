"""Transporte opcional: requisições feitas dentro de um Firefox próprio."""
from __future__ import annotations

import base64
import secrets
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

from .core import (AppError, BASE, MAX_DOWNLOAD, SessionRequired,
                   SubdivxClient, check_cancel)

FETCH = r'''async ({url, method, form, limit}) => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 25000);
    try {
        const headers = {'X-Requested-With':'XMLHttpRequest'};
        const options = {method, headers, credentials:'same-origin', signal:controller.signal};
        if (form) options.body = new URLSearchParams(form);
        const response = await fetch(url, options);
        if (!response.ok) return {status:response.status};
        const reader = response.body.getReader();
        let size = 0, binary = '';
        while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            size += value.length;
            if (size > limit) {
                await reader.cancel();
                return {error:'size'};
            }
            for (let i = 0; i < value.length; i += 32768)
                binary += String.fromCharCode.apply(null, value.subarray(i, i + 32768));
        }
        return {status:response.status, data:btoa(binary)};
    } catch (error) {
        return {error:error.name === 'AbortError' ? 'timeout' : 'network'};
    } finally {
        clearTimeout(timeout);
    }
}'''


def reusable_cookie(cookie):
    """cookies() retorna campos informativos que add_cookies() não aceita."""
    allowed = {'name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure', 'sameSite'}
    result = {key: value for key, value in cookie.items() if key in allowed}
    if result.get('expires', 0) <= 0:
        result.pop('expires', None)
    if result.get('sameSite') == 'None' and not result.get('secure'):
        result.pop('sameSite', None)
    return result


class BrowserSubdivxClient(SubdivxClient):
    def __init__(self, cookie='', user_agent=''):
        super().__init__(cookie, user_agent)
        self.seed = secrets.randbelow(2**31)
        self._cookies = []
        self._page = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='subdivx-firefox')
        self._runtime = None
        self._closed = False
        self._close_future = None

    @contextmanager
    def browser(self, cancel, progress=lambda _: None):
        try:
            from invisible_playwright import InvisiblePlaywright
        except ImportError as exc:
            raise AppError('O modo navegador precisa ser instalado. Execute Instalar.bat.') from exc
        check_cancel(cancel)
        progress('Iniciando o Firefox próprio do app…')
        try:
            # O executor mantém todos os objetos Playwright na mesma thread.
            with InvisiblePlaywright(seed=self.seed, headless=True,
                                     timezone='America/Sao_Paulo') as browser:
                context = browser.new_context()
                cookies = list(self._cookies)
                if not cookies:
                    for part in self.session.headers.get('Cookie', '').split(';'):
                        name, separator, value = part.strip().partition('=')
                        if name and separator:
                            cookies.append({'name': name, 'value': value, 'url': BASE + '/'})
                if cookies:
                    context.add_cookies(cookies)
                page = context.new_page()
                self._page = page
                page.goto(BASE + '/', wait_until='domcontentloaded', timeout=60000)
                deadline = time.monotonic() + 30
                while True:
                    check_cancel(cancel)
                    content = page.content()
                    if 'index-min.js?v=' in content or 'sdx-min.css?v=' in content:
                        break
                    if time.monotonic() >= deadline:
                        raise SessionRequired('O navegador também recebeu uma verificação do Subdivx. Aguarde e tente novamente ou use o site no navegador.')
                    progress('Aguardando a página do Subdivx no Firefox…')
                    page.wait_for_timeout(1000)
                try:
                    yield page
                finally:
                    self._cookies = [reusable_cookie(cookie) for cookie in context.cookies()
                                     if cookie.get('domain', '').lstrip('.') in {'subdivx.com', 'www.subdivx.com'}]
                    self._page = None
        except AppError:
            raise
        except Exception:
            # Erros do protocolo Playwright podem incluir cookies nos detalhes.
            raise AppError('O Firefox de conexão não conseguiu concluir a operação. Verifique a conexão e execute Instalar.bat se o navegador ainda não foi instalado.') from None
        finally:
            self._page = None

    def request(self, path, cancel, method='GET', **kwargs):
        check_cancel(cancel)
        if self._page is None:
            raise AppError('A sessão do navegador ainda não foi iniciada.')
        url = urljoin(BASE + '/', path)
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname not in {'subdivx.com', 'www.subdivx.com'}:
            raise AppError('Endereço inesperado na consulta ao Subdivx.')
        if path == '/' and method == 'GET':
            return self._page.content().encode('utf-8')
        result = self._page.evaluate(FETCH, {'url': url, 'method': method,
                                            'form': kwargs.get('data'), 'limit': MAX_DOWNLOAD})
        check_cancel(cancel)
        if result.get('error') == 'size':
            raise AppError('A resposta excedeu o limite de 32 MB.')
        if result.get('error'):
            raise AppError('A consulta dentro do Firefox falhou ou excedeu o tempo limite. Tente novamente.')
        status = result.get('status', 0)
        if status in {401, 403}:
            raise SessionRequired('O Subdivx recusou esta consulta no navegador. Aguarde e tente pesquisar novamente.')
        if status == 429:
            raise AppError('O Subdivx limitou as consultas. Aguarde alguns minutos.')
        if not 200 <= status < 300:
            raise AppError(f'O Subdivx retornou HTTP {status}. Tente novamente.')
        return base64.b64decode(result['data'], validate=True)

    def search(self, spec, cancel, progress):
        return self._submit(lambda: SubdivxClient.search(self, spec, cancel, progress), cancel, progress)

    def download(self, item, spec, output, cancel):
        return self._submit(lambda: SubdivxClient.download(self, item, spec, output, cancel), cancel)

    def _submit(self, operation, cancel, progress=lambda _: None):
        check_cancel(cancel)
        if self._closed:
            raise AppError('A conexão já foi encerrada.')
        def run():
            check_cancel(cancel)
            if self._runtime is None:
                runtime = self.browser(cancel, progress)
                runtime.__enter__()
                self._runtime = runtime
            try:
                return operation()
            except SessionRequired:
                self._end_browser()
                raise
            except AppError:
                raise
            except Exception:
                try:
                    self._end_browser()
                except AppError:
                    pass
                raise AppError('A conexão do Firefox foi interrompida. Tente pesquisar novamente.') from None
        return self._executor.submit(run).result()

    def _end_browser(self):
        if self._runtime is not None:
            runtime, self._runtime = self._runtime, None
            runtime.__exit__(None, None, None)

    def close(self, wait=False):
        if not self._closed:
            self._closed = True
            self._close_future = self._executor.submit(self._end_browser)
            self._executor.shutdown(wait=False)
            super().close()
        if wait and self._close_future is not None:
            self._close_future.result(timeout=30)
