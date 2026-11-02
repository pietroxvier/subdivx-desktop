"""Abre a GUI por alguns segundos e verifica seleção, sessão e fechamento."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import customtkinter as ctk
from subdivx_app.core import SearchSpec, Subtitle, rank
from subdivx_app.gui import App

ctk.set_appearance_mode('dark')
app = App()
errors = []
app.report_callback_exception = lambda *exc: errors.append(exc)

def check():
    try:
        app.spec = SearchSpec('Dune', year=2021, release='WEB-DL 1080p')
        item = rank(Subtitle(123, 'Dune (2021) [EXEMPLO DE TESTE]',
                             'Dados fictícios para verificar a interface. WEB-DL 1080p.', 1200, 'Teste'), app.spec)
        app.show_results([item])
        app.update_idletasks()
        assert app.selected.id == 123
        assert app.download_button.cget('state') == 'normal'
        app.session_dialog()
        app.update_idletasks()
        assert app.session_window.winfo_exists()
        app.session_window.destroy()
        app.after(1200, app.close)
    except Exception:
        import traceback
        traceback.print_exc()
        errors.append(True)
        app.close()

app.after(400, check)
app.mainloop()
if errors:
    raise SystemExit('GUI smoke failed: ' + str(errors))
print('GUI smoke OK: build, result selection, session dialog, clean shutdown')
