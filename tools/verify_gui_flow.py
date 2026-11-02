"""Teste integrado da GUI com rede simulada e saída em diretório temporário."""
import io
import json
import sys
import tempfile
import time
import traceback
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import customtkinter as ctk
import subdivx_app.gui as gui
from subdivx_app.core import SubdivxClient


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def main():
    errors = []
    notices = []
    with tempfile.TemporaryDirectory(prefix='subdivx-gui-test-') as temporary:
        root = Path(temporary)
        with (patch.object(gui, 'CONFIG', root / 'config.json'),
              patch.object(gui.Catalog, 'search', return_value=[]),
              patch.object(gui, 'make_client', side_effect=lambda cookie, ua, mode: SubdivxClient(cookie, ua)),
              patch.object(gui, 'OUTPUT', root / 'legendas'),
              patch.object(gui.messagebox, 'showinfo', side_effect=lambda *a, **kw: notices.append(a)),
              patch.object(gui.messagebox, 'showerror', side_effect=lambda *a, **kw: errors.append(a)),
              patch.object(gui.messagebox, 'showwarning', side_effect=lambda *a, **kw: errors.append(a))):
            ctk.set_appearance_mode('dark')
            app = gui.App()
            app.title('Subdivx - TESTE LOCAL COM DADOS SIMULADOS')
            deadline = time.monotonic() + 12

            def failed(exc):
                errors.append(repr(exc))
                traceback.print_exc()
                app.close()

            def begin():
                try:
                    entries = [w for w in app.session_window.winfo_children() if isinstance(w, ctk.CTkEntry)]
                    entries[0].delete(0, 'end')
                    entries[0].insert(0, 'Cookie: cf_clearance=FAKE\r\nTOKEN;\n sdx=LOCAL_TEST\n')
                    entries[1].delete(0, 'end')
                    entries[1].insert(0, 'User-Agent: Mozilla/5.0\n LocalTest/1.0\n')
                    apply = next(w for w in descendants(app.session_window)
                                 if isinstance(w, ctk.CTkButton) and w.cget('text') == 'Aplicar sessão')
                    apply.invoke()
                    assert not errors, errors
                    assert app.client.session.headers['Cookie'] == 'cf_clearance=FAKETOKEN;sdx=LOCAL_TEST'
                    assert app.client.session.headers['User-Agent'] == 'Mozilla/5.0 LocalTest/1.0'
                    config = json.loads(gui.CONFIG.read_text(encoding='utf-8'))
                    assert set(config) == {'user_agent', 'mode'}
                    assert 'FAKETOKEN' not in gui.CONFIG.read_text(encoding='utf-8')
                    payload = {'aaData': [{'id': '999', 'titulo': 'Dune (2021)',
                                           'descripcion': 'EXEMPLO LOCAL. WEB-DL 1080p.',
                                           'descargas': '100', 'nick': 'Teste'}]}
                    archive = io.BytesIO()
                    with zipfile.ZipFile(archive, 'w') as package:
                        package.writestr('Dune.WEB-DL.srt', '1\n00:00:01,000 --> 00:00:02,000\nTeste local.\n')

                    def fake_request(path, cancel, method='GET', **kwargs):
                        if path == '/':
                            return b'<script src="index-min.js?v=1.2.3">'
                        if path.startswith('/inc/gt.php'):
                            return b'{"token":"FAKE"}'
                        if path == '/inc/ajax.php':
                            assert kwargs['data']['buscar123'] == 'Dune 2021'
                            return json.dumps(payload).encode()
                        if path == '/descargar.php?f=1&id=999':
                            return archive.getvalue()
                        raise AssertionError('Unexpected endpoint')

                    app.client.request = fake_request
                    app.query.insert(0, 'Dune 2021')
                    # Simula o próximo clique após o Windows processar o
                    # fechamento da janela de sessão e a troca de foco.
                    app.after(300, app.search_button.invoke)
                    app.after(100, await_results)
                except Exception as exc:
                    failed(exc)

            def await_results():
                try:
                    assert time.monotonic() < deadline, 'Search timed out'
                    assert not errors, errors
                    if app.selected is None:
                        app.after(80, await_results)
                        return
                    assert app.selected.id == 999
                    assert 'Dune (2021)' == app.detail_title.cget('text')
                    app.download_button.invoke()
                    app.after(100, await_download)
                except Exception as exc:
                    failed(exc)

            def await_download():
                try:
                    assert time.monotonic() < deadline, 'Download timed out'
                    assert not errors, errors
                    files = list(gui.OUTPUT.rglob('*.srt'))
                    if not files or not notices:
                        app.after(80, await_download)
                        return
                    assert len(files) == 1
                    assert files[0].parent.name == 'Dune (2021)'
                    assert 'Teste local.' in files[0].read_text(encoding='utf-8')
                    app.close()
                except Exception as exc:
                    failed(exc)

            def open_session():
                app.session_dialog()
                app.after(500, begin)

            app.after(350, open_session)
            app.mainloop()
    if errors:
        raise SystemExit(f'GUI flow failed: {errors}')
    print('GUI flow OK (OFFLINE): pasted session, no cookie on disk, search thread, ranking, confirmation, ZIP download, UTF-8 SRT, shutdown')


if __name__ == '__main__':
    main()
