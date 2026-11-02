"""Fluxo da escolha da obra: oito capas, escopo por ano, cinco versões."""
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import customtkinter as ctk
from PIL import Image
from subdivx_app.catalog import Movie
from subdivx_app.core import Subtitle
import subdivx_app.gui as gui

movies = [Movie(f'tt00000{i:02d}', f'Franchise Movie {i}', 2000 + i) for i in range(10)]
items = [Subtitle(i + 1, 'Franchise Movie 0 (2000)', f'WEB-DL 1080p version {i}', 500 - i,
                  'Test', 90 - i) for i in range(12)]
errors = []
client = MagicMock()
client.search.return_value = items
with (patch.object(gui, 'make_client', return_value=client),
      patch.object(gui.Catalog, 'search', return_value=movies),
      patch.object(gui.Catalog, 'poster', side_effect=lambda *args: Image.new('RGB', (120, 180), '#456789'))):
    ctk.set_appearance_mode('dark')
    app = gui.App()
    app.title('Subdivx - TESTE DE ORGANIZAÇÃO COM DADOS FICTÍCIOS')
    deadline = time.monotonic() + 8
    def fail(exc):
        errors.append(repr(exc))
        app.close()
    def begin():
        app.query.insert(0, 'Franchise')
        app.search_button.invoke()
        app.after(150, choose)
    def choose():
        try:
            assert time.monotonic() < deadline
            if not app.catalog_movies:
                app.after(80, choose)
                return
            assert app.selected is None
            assert client.search.call_count == 0, 'Subdivx consulted before choosing a movie'
            buttons = [b for b in app.result_buttons if b.cget('text') == 'Buscar legendas']
            assert len(buttons) == 8
            buttons[0].invoke()
            app.after(150, verify)
        except Exception as exc:
            fail(exc)
    def verify():
        try:
            assert time.monotonic() < deadline
            if app.selected is None:
                app.after(80, verify)
                return
            assert client.search.call_args.args[0].year == 2000
            assert len(app.version_buttons) == 5
            assert app.cover.cget('image') is not None
            more = next(b for b in app.result_buttons if b.cget('text').startswith('Mostrar mais 5'))
            more.invoke()
            assert len(app.version_buttons) == 10
            back = next(b for b in app.result_buttons if b.cget('text') == 'Trocar filme')
            back.invoke()
            assert app.selected is None
            assert app.download_button.cget('state') == 'disabled'
            assert client.search.call_count == 1
            app.close()
        except Exception as exc:
            fail(exc)
    app.after(400, begin)
    app.mainloop()
if errors:
    raise SystemExit('Catalog GUI failed: ' + str(errors))
print('Catalog GUI OK: 8 films, covers, no early Subdivx lookup, specific year, 5 versions, pagination, back without network')
