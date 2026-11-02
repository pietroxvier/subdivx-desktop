import base64
import json
import unittest
import threading
from threading import Event
from unittest.mock import MagicMock, patch

from subdivx_app.browser import BrowserSubdivxClient, reusable_cookie
from subdivx_app.core import AppError, Cancelled, SearchSpec, SessionRequired, SubdivxClient


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.client = BrowserSubdivxClient()

    def tearDown(self):
        self.client.close()

    def test_binary_download_preserves_bytes(self):
        data = b'PK\x03\x04\x00\xff\x80'
        page = self.client._page = MagicMock()
        page.evaluate.return_value = {'status': 200, 'data': base64.b64encode(data).decode()}
        self.assertEqual(self.client.request('/descargar.php?f=1&id=123', Event()), data)

    def test_refused_and_rate_limited_requests(self):
        page = self.client._page = MagicMock()
        page.evaluate.return_value = {'status': 403}
        with self.assertRaises(SessionRequired):
            self.client.request('/inc/ajax.php', Event())
        page.evaluate.return_value = {'status': 429}
        with self.assertRaises(AppError):
            self.client.request('/inc/ajax.php', Event())

    def test_missing_context_and_external_url(self):
        with self.assertRaises(AppError):
            self.client.request('/inc/ajax.php', Event())
        self.client._page = MagicMock()
        with self.assertRaises(AppError):
            self.client.request('https://example.com', Event())
        self.client._page.evaluate.assert_not_called()

    def test_cancel_before_launch(self):
        cancel = Event()
        cancel.set()
        with patch('invisible_playwright.InvisiblePlaywright') as launcher:
            with self.assertRaises(Cancelled):
                self.client.search(SearchSpec('Dune'), cancel, lambda _: None)
            launcher.assert_not_called()

    def test_search_browser_lifetime_and_ephemeral_session(self):
        with patch('invisible_playwright.InvisiblePlaywright') as launcher:
            browser = launcher.return_value.__enter__.return_value
            context = browser.new_context.return_value
            page = context.new_page.return_value
            page.content.return_value = '<script src="index-min.js?v=1.2.3">'
            payload = {'aaData': [{'id': 1, 'titulo': 'Dune (2021)', 'descargas': 10}]}
            page.evaluate.side_effect = [
                {'status': 200, 'data': base64.b64encode(b'{"token":"local"}').decode()},
                {'status': 200, 'data': base64.b64encode(json.dumps(payload).encode()).decode()}]
            context.cookies.return_value = [{'name': 'sdx', 'value': 'LOCAL', 'domain': 'subdivx.com', 'path': '/'},
                                             {'name': 'unrelated', 'value': 'LOCAL', 'domain': 'example.com', 'path': '/'}]
            results = self.client.search(SearchSpec('Dune', year=2021), Event(), lambda _: None)
            self.assertEqual([item.id for item in results], [1])
            self.assertIsNotNone(self.client._page)
            self.client.close(wait=True)
            self.assertIsNone(self.client._page)
            self.assertEqual(len(self.client._cookies), 1)
            launcher.return_value.__exit__.assert_called_once()

    def test_cookie_editor_json_supported(self):
        cookie = json.dumps([{'name': 'sdx', 'value': 'LOCAL', 'domain': 'subdivx.com'},
                             {'name': 'other', 'value': 'IGNORED', 'domain': 'example.com'}])
        with_client = SubdivxClient(cookie)
        self.assertEqual(with_client.session.headers['Cookie'], 'sdx=LOCAL')
        with_client.close()
        for invalid in ['[{"name":"sdx","value":"a; Injected=b"}]', '[broken json]']:
            with self.assertRaises(AppError):
                SubdivxClient(invalid)

    def test_exported_cookie_can_be_reimported(self):
        data = reusable_cookie({'name': 'sdx', 'value': 'LOCAL', 'domain': 'subdivx.com',
                                'path': '/', 'expires': -1, 'size': 35, 'session': True,
                                'httpOnly': False, 'secure': False, 'sameSite': 'None'})
        self.assertEqual(data, {'name': 'sdx', 'value': 'LOCAL', 'domain': 'subdivx.com',
                                'path': '/', 'httpOnly': False, 'secure': False})

    def test_search_and_download_share_one_browser_and_thread(self):
        thread_ids = []
        def operation(*args, **kwargs):
            thread_ids.append(threading.get_ident())
            return []
        with patch('invisible_playwright.InvisiblePlaywright') as launcher:
            browser = launcher.return_value.__enter__.return_value
            context = browser.new_context.return_value
            context.new_page.return_value.content.return_value = '<script src="index-min.js?v=1.0">'
            context.cookies.return_value = []
            with patch.object(SubdivxClient, 'search', side_effect=operation), patch.object(SubdivxClient, 'download', side_effect=operation):
                self.client.search(SearchSpec('Movie'), Event(), lambda _: None)
                self.client.search(SearchSpec('Movie II'), Event(), lambda _: None)
                self.client.download(None, None, None, Event())
            self.client.close(wait=True)
            self.assertEqual(len(set(thread_ids)), 1)
            self.assertNotEqual(thread_ids[0], threading.get_ident())
            launcher.assert_called_once()
            launcher.return_value.__exit__.assert_called_once()


if __name__ == '__main__':
    unittest.main()
