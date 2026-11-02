"""Abre la interfaz real con un catálogo público para capturar el README."""
import sys
import ctypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if sys.platform == 'win32':
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Pietro.SubdivxDesktop')
import customtkinter as ctk
from subdivx_app.gui import App

ctk.set_appearance_mode('dark')
app = App()
app.title('Subdivx · Legendas · Vista para README')
app.geometry('1180x880')
app.after(500, lambda: (app.query.insert(0, 'Harry Potter'), app.search()))
app.mainloop()
