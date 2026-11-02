from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

from .core import (AppError, Cancelled, DEFAULT_UA, SearchSpec,
                   SessionRequired, SubdivxClient, Subtitle, identity_title)
from .browser import BrowserSubdivxClient
from .results import group_titles
from .catalog import Catalog

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / 'legendas'
CONFIG = ROOT / 'config.json'
BG, PANEL, EDGE = '#0e1219', '#171e28', '#283445'
TEXT, MUTED, ACCENT = '#edf2f8', '#a5b2c4', '#36bda8'


def make_client(cookie, user_agent, mode='browser'):
    cls = SubdivxClient if mode == 'http' else BrowserSubdivxClient
    return cls(cookie, user_agent)


def cancel_callbacks(window, all_callbacks=False):
    commands = set(window._tclCommands or [])
    owners = {}
    def collect(widget):
        for command in widget._tclCommands or []:
            owners[command] = widget
        for child in widget.winfo_children():
            collect(child)
    collect(window)
    for timer in window.tk.call('after', 'info'):
        try:
            script = window.tk.call('after', 'info', timer)[0]
            command = str(script).split()[0]
            if all_callbacks or command in commands:
                if command in owners:
                    owners[command].after_cancel(timer)
                else:
                    window.tk.call('after', 'cancel', timer)
        except (tk.TclError, IndexError):
            pass


class SessionWindow(ctk.CTkToplevel):
    def destroy(self):
        cancel_callbacks(self)
        super().destroy()


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title('Subdivx · Legendas')
        self.geometry('1080x790')
        self.minsize(920, 720)
        self.configure(fg_color=BG)
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.closing = False
        self.mode = self.load_config().get('mode', 'browser')
        self.client = make_client(os.environ.get('SUBDIVX_COOKIE_HEADER', ''), self.load_ua(), self.mode)
        self.cookie = os.environ.get('SUBDIVX_COOKIE_HEADER', '')
        self.user_agent = self.load_ua()
        self.spec = None
        self.selected = None
        self.groups = []
        self._download_spec = None
        self.catalog = Catalog()
        self.catalog_movies = []
        self.active_movie = None
        self.poster_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='subdivx-capas')
        self.poster_cancel = threading.Event()
        self.poster_generation = 0
        self.poster_cache = {}
        self.poster_jobs = set()
        self.poster_labels = {}
        self.result_buttons = []
        self.controls = []
        self.session_window = None
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.build()
        self.after(80, self.poll)

    def load_ua(self):
        return self.load_config().get('user_agent', DEFAULT_UA)

    def load_config(self):
        try:
            value = json.loads(CONFIG.read_text(encoding='utf-8'))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, AttributeError):
            return {}

    def label(self, parent, text, size=13, color=TEXT, **kwargs):
        return ctk.CTkLabel(parent, text=text, text_color=color,
                            font=ctk.CTkFont(family='Segoe UI', size=size), **kwargs)

    def entry(self, parent, placeholder, width=90):
        widget = ctk.CTkEntry(parent, placeholder_text=placeholder, width=width, height=36,
                              fg_color=BG, border_color=EDGE)
        self.controls.append(widget)
        return widget

    def button(self, parent, text, command, **kwargs):
        return ctk.CTkButton(parent, text=text, command=command, height=36,
                             fg_color=ACCENT, hover_color='#259580', text_color='#061c18', **kwargs)

    def build(self):
        header = ctk.CTkFrame(self, fg_color='transparent')
        header.pack(fill='x', padx=26, pady=(20, 14))
        self.label(header, 'SUBDIVX / LEGENDAS', 24).pack(side='left')
        self.session_button = ctk.CTkButton(header, text='Sessão do site', command=self.session_dialog,
                                          width=130, fg_color=PANEL, border_width=1, border_color=EDGE)
        self.session_button.pack(side='right')
        self.label(self, 'Escolha o filme. Confira a versão. Salve o SRT.', 14, MUTED,
                   anchor='w').pack(fill='x', padx=26, pady=(0, 14))
        search = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=12)
        search.pack(fill='x', padx=26)
        search.grid_columnconfigure(0, weight=1)
        self.query = self.entry(search, 'Nome do filme ou série · ex.: Dune 2021 ou The Bear S01E02', 510)
        self.query.grid(row=0, column=0, padx=(16, 10), pady=(16, 10), sticky='ew')
        self.query.bind('<Return>', lambda _: self.search())
        self.search_button = self.button(search, 'Pesquisar', self.search, width=120)
        self.search_button.grid(row=0, column=1, padx=(0, 16), pady=(16, 10))
        filters = ctk.CTkFrame(search, fg_color='transparent')
        filters.grid(row=1, column=0, columnspan=2, padx=16, pady=(0, 14), sticky='ew')
        self.kind = ctk.CTkOptionMenu(filters, values=['Filme', 'Série'], width=100, height=36,
                                     fg_color=EDGE, button_color=EDGE)
        self.kind.pack(side='left', padx=(0, 10))
        self.controls.append(self.kind)
        self.year = self.entry(filters, 'Ano', 72)
        self.season = self.entry(filters, 'Temp.', 72)
        self.episode = self.entry(filters, 'Episódio', 82)
        self.release = self.entry(filters, 'Versão opcional · WEB-DL 1080p / grupo', 300)
        for widget in (self.year, self.season, self.episode, self.release):
            widget.pack(side='left', padx=(0, 10), fill='x', expand=widget is self.release)
        self.label(self, 'Espanhol · Buscas amplas são organizadas por obra e ano. As versões aparecem depois de escolher o título.',
                   12, MUTED, anchor='w').pack(fill='x', padx=26, pady=(10, 12))
        body = ctk.CTkFrame(self, fg_color='transparent')
        body.pack(fill='both', expand=True, padx=26)
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=4, minsize=320)
        body.grid_columnconfigure(1, weight=6, minsize=440)
        self.results = ctk.CTkScrollableFrame(body, fg_color=PANEL, corner_radius=12,
                                             label_text='RESULTADOS', label_fg_color=PANEL)
        self.results.grid(row=0, column=0, sticky='nsew', padx=(0, 12))
        self.empty = self.label(self.results, 'Sua pesquisa aparece aqui.\n\nO resultado mais compatível\nfica no topo.', 14, MUTED)
        self.empty.pack(padx=18, pady=50)
        detail = ctk.CTkFrame(body, fg_color=PANEL, corner_radius=12)
        detail.grid(row=0, column=1, sticky='nsew')
        film_header = ctk.CTkFrame(detail, fg_color='transparent')
        film_header.pack(fill='x', padx=22, pady=(20, 12))
        self.cover = self.label(film_header, '', 11, MUTED, width=84, height=126)
        title_block = ctk.CTkFrame(film_header, fg_color='transparent')
        title_block.pack(side='left', fill='x', expand=True)
        self.detail_title = self.label(title_block, 'Confira antes de baixar', 22, anchor='w', justify='left', wraplength=470)
        self.detail_title.pack(fill='x', pady=(0, 6))
        self.detail_meta = self.label(title_block, 'Título completo e descrição da publicação no Subdivx.', 12, MUTED,
                                      anchor='w', justify='left', wraplength=470)
        self.detail_meta.pack(fill='x')
        self.description = ctk.CTkTextbox(detail, fg_color=BG, text_color=TEXT, corner_radius=8,
                                         wrap='word', font=('Segoe UI', 13))
        self.description.pack(fill='both', expand=True, padx=22, pady=(0, 12))
        self.set_description('Pesquise um título e selecione uma publicação.\n\nVocê poderá conferir o nome completo, o ano, a versão do vídeo e a descrição antes de confirmar o download.\n\nSem a versão exata do vídeo, a sincronia não pode ser garantida.')
        detail.bind('<Configure>', lambda event: (
            self.detail_title.configure(wraplength=max(230, event.width - (144 if self.cover.winfo_manager() else 44))),
            self.detail_meta.configure(wraplength=max(230, event.width - (144 if self.cover.winfo_manager() else 44)))))
        self.source_button = ctk.CTkButton(detail, text='Abrir Subdivx no navegador', command=self.open_source,
                                          fg_color=EDGE, hover_color='#35455d', state='disabled')
        self.source_button.pack(fill='x', padx=22, pady=(0, 8))
        self.download_button = self.button(detail, 'Confirmar título e baixar SRT', self.download, state='disabled')
        self.download_button.pack(fill='x', padx=22, pady=(0, 18))
        footer = ctk.CTkFrame(self, fg_color='transparent')
        footer.pack(fill='x', padx=26, pady=(14, 18))
        self.status = self.label(footer, 'Pronto · Conexão pelo Firefox.' if self.mode != 'http' else 'Pronto · HTTP direto.', 12, MUTED, anchor='w', wraplength=650, justify='left')
        self.status.pack(side='left', fill='x', expand=True)
        self.cancel_button = ctk.CTkButton(footer, text='Cancelar', command=self.cancel.set,
                                          width=85, fg_color=EDGE, state='disabled')
        self.cancel_button.pack(side='right', padx=(10, 0))
        ctk.CTkButton(footer, text='Abrir legendas', command=self.open_output, width=115,
                      fg_color=EDGE).pack(side='right')
        self.query.focus_set()

    def set_description(self, text):
        self.description.configure(state='normal')
        self.description.delete('1.0', 'end')
        self.description.insert('1.0', text)
        self.description.configure(state='disabled')

    def busy(self, value):
        state = 'disabled' if value else 'normal'
        for widget in [*self.controls, self.search_button, self.session_button, *self.result_buttons]:
            widget.configure(state=state)
        self.cancel_button.configure(state='normal' if value else 'disabled')
        self.download_button.configure(state='normal' if not value and self.selected else 'disabled')
        self.source_button.configure(state='normal' if self.selected else 'disabled')

    def start(self, operation, callback):
        if self.worker and self.worker.is_alive():
            return
        self.cancel.clear()
        self.busy(True)
        def run():
            try:
                result = operation()
                self.events.put(('done', callback, result))
            except Cancelled as exc:
                self.events.put(('cancelled', str(exc)))
            except AppError as exc:
                self.events.put(('error', str(exc), isinstance(exc, SessionRequired)))
            except Exception:
                logging.exception('Falha na operação')
                self.events.put(('error', 'Não foi possível concluir a operação. O pacote pode estar danificado ou o site pode ter mudado. Detalhes em app.log.', False))
        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def progress(self, text):
        self.events.put(('progress', text))

    def poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                if self.closing:
                    continue
                if event[0] == 'poster':
                    _, generation, imdb_id, picture = event
                    if generation == self.poster_generation:
                        if picture is not None:
                            self.poster_cache[imdb_id] = picture
                        for widget in self.poster_labels.get(imdb_id, []):
                            if widget.winfo_exists():
                                self.paint_poster(widget, picture, (64, 96))
                        if self.active_movie and self.active_movie.imdb_id == imdb_id:
                            self.paint_poster(self.cover, picture, (84, 126))
                elif event[0] == 'metadata':
                    current_key = (identity_title(self._download_spec.title), self._download_spec.year) if self._download_spec else None
                    if event[1] == self.poster_generation and event[2] is not None and event[3] == current_key:
                        self.set_cover(event[2])
                elif event[0] == 'progress':
                    self.status.configure(text=event[1])
                elif event[0] == 'done':
                    self.busy(False)
                    event[1](event[2])
                elif event[0] == 'cancelled':
                    self.busy(False)
                    self.status.configure(text=event[1])
                elif event[0] == 'error':
                    self.busy(False)
                    self.status.configure(text='Sessão necessária.' if event[2] else 'Não foi possível concluir.')
                    messagebox.showwarning('Subdivx', event[1], parent=self)
                    if event[2]:
                        self.session_dialog()
        except queue.Empty:
            pass
        if self.closing:
            if not self.worker or not self.worker.is_alive():
                self.client.close()
                cancel_callbacks(self, all_callbacks=True)
                self.destroy()
                return
        self.after(80, self.poll)

    def search(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            spec = SearchSpec.parse(self.query.get(), self.kind.get(), self.year.get(),
                                    self.season.get(), self.episode.get(), self.release.get())
        except AppError as exc:
            messagebox.showinfo('Confira a pesquisa', str(exc), parent=self)
            return
        self.spec = spec
        self.selected = None
        self.groups = []
        self._download_spec = None
        self.catalog_movies = []
        self.active_movie = None
        self.cover.pack_forget()
        self.poster_cancel.set()
        self.poster_cancel = threading.Event()
        self.poster_generation += 1
        self.poster_labels.clear()
        self.poster_jobs.clear()
        self.detail_title.configure(text=spec.label)
        self.detail_meta.configure(text='Consultando o Subdivx…')
        self.set_description('Aguarde os resultados da pesquisa.')
        for widget in self.results.winfo_children():
            widget.destroy()
        self.result_buttons.clear()
        if spec.kind == 'Filme' and spec.year is None:
            self.status.configure(text='Identificando filmes no catálogo…')
            def discover():
                try:
                    return self.catalog.search(spec.title, spec.year, self.cancel)
                except Cancelled:
                    raise
                except AppError:
                    return None
            self.start(discover, self.show_catalog)
        else:
            self.search_subtitles(spec)

    def search_subtitles(self, spec):
        self.spec = spec
        self.selected = None
        self._download_spec = None
        self.clear_results()
        self.status.configure(text=f'Buscando legendas apenas para {spec.label}…')
        self.detail_title.configure(text=spec.label)
        self.detail_meta.configure(text='Consulta específica ao Subdivx…')
        self.set_description('Aguarde a consulta das versões de legenda para esta obra.')
        self.start(lambda: self.client.search(spec, self.cancel, self.progress), self.show_results)

    def show_catalog(self, movies=None, limit=8):
        if movies is not None:
            self.catalog_movies = movies
        self.clear_results()
        self.selected = None
        self._download_spec = None
        self.active_movie = None
        self.cover.pack_forget()
        self.results.configure(label_text='1. ESCOLHA O FILME')
        self.download_button.configure(state='disabled')
        self.source_button.configure(state='disabled')
        self.detail_title.configure(text='Qual filme você procura?')
        self.detail_meta.configure(text='Capas e identificação IMDb via Cinemeta\nPrimeiros títulos em ordem de lançamento.')
        self.set_description('Escolha o filme pela capa, pelo título e pelo ano.\n\nSomente depois da escolha o app consulta as legendas dessa obra no Subdivx.\n\nOs oito primeiros títulos do catálogo aparecem inicialmente. Outros resultados, como especiais e produções relacionadas, ficam em “Mostrar outros títulos”.\n\nA ordem apresentada é a de lançamento, não a ordem dos acontecimentos da história.')
        visible = sorted(self.catalog_movies[:limit], key=lambda movie: (movie.year is None, movie.year or 9999, movie.title))
        for movie in visible:
            row = ctk.CTkFrame(self.results, fg_color=BG, corner_radius=8)
            row.pack(fill='x', padx=4, pady=4)
            poster = self.label(row, 'Capa…', 11, MUTED, width=64, height=96)
            poster.pack(side='left', padx=(10, 8), pady=10)
            text = ctk.CTkFrame(row, fg_color='transparent')
            text.pack(side='left', fill='x', expand=True, padx=(0, 8), pady=6)
            self.label(text, movie.title, 13, anchor='w', justify='left', wraplength=210).pack(fill='x')
            self.label(text, str(movie.year or 'Ano não informado'), 12, MUTED, anchor='w').pack(fill='x')
            self.list_button(text, 'Buscar legendas', lambda selected=movie: self.choose_movie(selected))
            self.poster_labels.setdefault(movie.imdb_id, []).append(poster)
            self.queue_poster(movie)
        if len(self.catalog_movies) > limit:
            self.list_button(self.results, f'Mostrar outros títulos ({len(self.catalog_movies) - limit})',
                             lambda: self.show_catalog(limit=limit + 8))
        if not visible:
            self.label(self.results, 'O catálogo não encontrou o filme\nou está indisponível.', 13, MUTED).pack(padx=12, pady=30)
        self.list_button(self.results, 'Buscar diretamente no Subdivx', lambda: self.search_subtitles(self.spec))
        self.status.configure(text=f'{len(visible)} títulos exibidos. Escolha o filme antes de consultar as legendas.')

    def choose_movie(self, movie):
        self.set_cover(movie)
        self.search_subtitles(replace(self.spec, title=movie.title, year=movie.year))

    def paint_poster(self, label, picture, size):
        if picture is None:
            label.configure(image=None, text='Sem capa')
        else:
            label.configure(image=ctk.CTkImage(light_image=picture, dark_image=picture, size=size), text='')

    def queue_poster(self, movie):
        if movie.imdb_id in self.poster_cache:
            for label in self.poster_labels.get(movie.imdb_id, []):
                if label.winfo_exists():
                    self.paint_poster(label, self.poster_cache[movie.imdb_id], (64, 96))
            return
        if movie.imdb_id in self.poster_jobs:
            return
        self.poster_jobs.add(movie.imdb_id)
        generation, cancel = self.poster_generation, self.poster_cancel
        def load():
            try:
                picture = self.catalog.poster(movie, cancel)
                self.events.put(('poster', generation, movie.imdb_id, picture))
            except Cancelled:
                pass
        self.poster_pool.submit(load)

    def set_cover(self, movie):
        self.active_movie = movie
        self.cover.pack(side='left', padx=(0, 12), before=self.cover.master.winfo_children()[-1])
        self.paint_poster(self.cover, self.poster_cache.get(movie.imdb_id), (84, 126))
        self.queue_poster(movie)

    def show_results(self, items):
        self.groups = group_titles(items, self.spec)
        if not items:
            self.clear_results()
            self.label(self.results, 'Nenhum resultado compatível.\n\nTente o título original ou\nrevise ano e episódio.', 14, MUTED).pack(padx=16, pady=40)
            self.detail_meta.configure(text='Nenhum resultado compatível encontrado.')
            self.set_description('Tente pesquisar o título original ou em espanhol. Confira o ano e, para séries, a temporada e o episódio. Não foram baixados arquivos.')
            self.status.configure(text='Nenhuma obra compatível encontrada.')
            return
        if len(self.groups) == 1:
            self.show_versions(self.groups[0])
        else:
            self.show_groups()

    def clear_results(self):
        for widget in self.results.winfo_children():
            widget.destroy()
        self.result_buttons.clear()
        self.poster_labels.clear()
        self.results._parent_canvas.yview_moveto(0)

    def list_button(self, parent, text, command):
        button = ctk.CTkButton(parent, text=text, command=command, height=32,
                              fg_color=EDGE, hover_color='#35455d')
        button.pack(fill='x', padx=12, pady=(6, 12))
        self.result_buttons.append(button)
        return button

    def show_groups(self, limit=12):
        self.clear_results()
        self.selected = None
        self._download_spec = None
        self.active_movie = None
        self.cover.pack_forget()
        self.results.configure(label_text='1. ESCOLHA A OBRA')
        self.download_button.configure(state='disabled')
        self.source_button.configure(state='disabled')
        total = sum(len(group.items) for group in self.groups)
        self.detail_title.configure(text='Qual filme você procura?')
        self.detail_meta.configure(text=f'{len(self.groups)} obras · {total} publicações agrupadas\nDa mais antiga para a mais recente.')
        self.set_description('Escolha uma obra na lista ao lado.\n\nAs publicações com o mesmo título e ano foram reunidas. Você verá as versões de legenda somente para o filme escolhido.\n\nA ordem de lançamento ajuda a reconhecer continuações, sem precisar saber o nome completo. A lista inclui as obras identificadas nesta pesquisa do Subdivx; ela pode não representar a franquia inteira.')
        for group in self.groups[:limit]:
            row = ctk.CTkFrame(self.results, fg_color=BG, corner_radius=8)
            row.pack(fill='x', padx=4, pady=4)
            self.label(row, group.title, 14, anchor='w', justify='left', wraplength=280).pack(fill='x', padx=12, pady=(10, 2))
            self.label(row, f'{group.year or "Ano não informado"} · {len(group.items)} versões', 12, MUTED, anchor='w').pack(fill='x', padx=12)
            self.list_button(row, 'Ver legendas desta obra', lambda selected=group: self.show_versions(selected))
        if len(self.groups) > limit:
            self.list_button(self.results, f'Mostrar mais obras ({len(self.groups) - limit})', lambda: self.show_groups(limit + 12))
        self.status.configure(text=f'{len(self.groups)} obras em ordem de lançamento. Escolha uma para reduzir a lista.')

    def show_versions(self, group, limit=5):
        previous = self.selected if self.selected in group.items else None
        self.clear_results()
        self.results.configure(label_text='2. ESCOLHA A LEGENDA')
        self._download_spec = replace(self.spec, title=group.title, year=group.year or self.spec.year)
        if self.active_movie is None:
            generation, cancel, lookup = self.poster_generation, self.poster_cancel, self._download_spec
            lookup_key = (identity_title(lookup.title), lookup.year)
            def identify():
                try:
                    movies = self.catalog.search(lookup.title, lookup.year, cancel)
                    matches = [movie for movie in movies if identity_title(movie.title) == identity_title(lookup.title)]
                    self.events.put(('metadata', generation, matches[0] if len(matches) == 1 else None, lookup_key))
                except AppError:
                    pass
            self.poster_pool.submit(identify)
        if len(self.groups) > 1:
            self.list_button(self.results, 'Voltar às obras', self.show_groups)
        elif self.catalog_movies:
            self.list_button(self.results, 'Trocar filme', lambda: self.show_catalog(self.catalog_movies))
        self.label(self.results, group.label, 14, anchor='w', justify='left', wraplength=280).pack(fill='x', padx=12, pady=(8, 2))
        self.label(self.results, f'{min(limit, len(group.items))} de {len(group.items)} versões · por compatibilidade', 11, MUTED,
                   anchor='w', wraplength=280).pack(fill='x', padx=12, pady=(0, 8))
        self.version_buttons = {}
        for index, item in enumerate(group.items[:limit]):
            card = ctk.CTkFrame(self.results, fg_color=BG, corner_radius=8)
            card.pack(fill='x', padx=4, pady=5)
            prefix = 'Mais compatível' if index == 0 else 'Compatibilidade'
            self.label(card, f'{prefix} · {item.score:g}/100', 11, MUTED, anchor='w').pack(fill='x', padx=12, pady=(10, 4))
            summary = item.description.strip().replace('\n', ' ')
            if len(summary) > 150:
                summary = summary[:147].rsplit(' ', 1)[0] + '…'
            self.label(card, summary or item.title, 13, anchor='w', justify='left', wraplength=280).pack(fill='x', padx=12)
            self.label(card, f'{item.downloads:,} downloads · {item.uploader}', 11, MUTED,
                       anchor='w', wraplength=290).pack(fill='x', padx=12, pady=(4, 6))
            button = ctk.CTkButton(card, text='Conferir esta legenda', command=lambda value=item: self.select(value),
                                   height=30, fg_color=EDGE, hover_color='#35455d')
            button.pack(fill='x', padx=12, pady=(0, 12))
            self.result_buttons.append(button)
            self.version_buttons[item.id] = button
        if len(group.items) > limit:
            self.list_button(self.results, f'Mostrar mais 5 versões ({len(group.items) - limit} restantes)',
                             lambda: self.show_versions(group, limit + 5))
        self.select(previous or group.items[0])
        self.status.configure(text=f'{group.label} · {len(group.items)} versões. Confira o release antes de baixar.')

    def select(self, item):
        self.selected = item
        for ident, button in getattr(self, 'version_buttons', {}).items():
            button.configure(text='Legenda selecionada' if ident == item.id else 'Conferir esta legenda',
                             fg_color='#245e55' if ident == item.id else EDGE)
        self.detail_title.configure(text=item.title)
        self.detail_meta.configure(text=f'Compatibilidade estimada: {item.score:g}/100 · {item.downloads:,} downloads\nPublicado por {item.uploader or "não informado"}')
        text = 'CONFERÊNCIA\n' + '\n'.join('• ' + reason for reason in item.reasons)
        text += '\n\nDESCRIÇÃO DO SUBDIVX\n' + (item.description or 'Sem descrição.')
        text += '\n\nBUSCA\n' + self.spec.label
        if self.active_movie:
            text += '\n\nCATÁLOGO\n' + self.active_movie.label + '\nIMDb: ' + self.active_movie.imdb_id
        if self.spec.release:
            text += '\nVersão: ' + self.spec.release
        text += '\n\nA popularidade desempata resultados; ela não garante tradução ou sincronia. O botão abaixo confirma a publicação selecionada.'
        self.set_description(text)
        self.download_button.configure(state='normal')
        self.source_button.configure(state='normal')

    def download(self):
        if not self.selected or not self.spec:
            return
        item, spec = self.selected, self._download_spec or self.spec
        self.status.configure(text='Baixando e verificando os arquivos SRT…')
        self.start(lambda: self.client.download(item, spec, OUTPUT, self.cancel), self.downloaded)

    def downloaded(self, paths):
        self.status.configure(text=f'{len(paths)} SRT(s) salvo(s) em legendas / {paths[0].parent.name}')
        messagebox.showinfo('Legenda salva', f'{len(paths)} arquivo(s) SRT em UTF-8:\n\n' + '\n'.join(str(path) for path in paths), parent=self)

    def open_source(self):
        if self.selected:
            webbrowser.open(self.selected.page_url)

    def open_output(self):
        OUTPUT.mkdir(exist_ok=True)
        if sys.platform == 'win32':
            os.startfile(str(OUTPUT))
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', str(OUTPUT)])
        else:
            subprocess.Popen(['xdg-open', str(OUTPUT)])

    def session_dialog(self):
        if self.session_window and self.session_window.winfo_exists():
            self.session_window.focus_force()
            return
        window = self.session_window = SessionWindow(self)
        window.title('Sessão do Subdivx')
        window.geometry('740x580')
        window.minsize(650, 570)
        window.configure(fg_color=BG)
        window.transient(self)
        window.grab_set()
        self.label(window, 'Conexão com o Subdivx', 22, anchor='w').pack(fill='x', padx=24, pady=(24, 12))
        instructions = ('Firefox: recomendado. O app cria uma sessão própria;\n'
                        'cookies são opcionais e o User-Agent é definido pelo Firefox.\n\n'
                        'HTTP direto: use Cookie e User-Agent da requisição ajax.php\n'
                        'em F12 → Rede → Headers no navegador que acessa o site.\n\n'
                        'Cookies ficam somente na memória, inclusive no modo Firefox.')
        self.label(window, instructions, 13, MUTED, anchor='w', justify='left').pack(fill='x', padx=24, pady=(0, 16))
        mode_menu = ctk.CTkOptionMenu(window, values=['Firefox (recomendado)', 'HTTP direto'])
        mode_menu.set('HTTP direto' if self.mode == 'http' else 'Firefox (recomendado)')
        mode_menu.pack(fill='x', padx=24, pady=(0, 16))
        self.label(window, 'Cookie opcional · cabeçalho ou JSON exportado', 13, anchor='w').pack(fill='x', padx=24)
        cookie_entry = ctk.CTkEntry(window, show='•', height=36)
        cookie_entry.pack(fill='x', padx=24, pady=(4, 12))
        cookie_entry.insert(0, self.cookie)
        self.label(window, 'User-Agent · usado apenas no HTTP direto', 13, anchor='w').pack(fill='x', padx=24)
        ua_entry = ctk.CTkEntry(window, height=36)
        ua_entry.pack(fill='x', padx=24, pady=(4, 20))
        ua_entry.insert(0, self.user_agent)
        def apply():
            cookie, ua = cookie_entry.get().strip(), ua_entry.get().strip()
            mode = 'http' if mode_menu.get() == 'HTTP direto' else 'browser'
            try:
                client = make_client(cookie, ua, mode)
                cookie = client.session.headers.get('Cookie', '')
                ua = client.session.headers['User-Agent']
                temporary = CONFIG.with_suffix('.tmp')
                temporary.write_text(json.dumps({'user_agent': ua or DEFAULT_UA, 'mode': mode}, indent=2), encoding='utf-8')
                temporary.replace(CONFIG)
            except (AppError, OSError) as exc:
                messagebox.showerror('Sessão', str(exc), parent=window)
                return
            self.client.close()
            self.client, self.cookie, self.user_agent = client, cookie, ua
            self.mode = mode
            self.status.configure(text='Sessão atualizada. Você já pode pesquisar novamente.')
            window.destroy()
        actions = ctk.CTkFrame(window, fg_color='transparent')
        actions.pack(fill='x', padx=24)
        ctk.CTkButton(actions, text='Abrir Subdivx', command=lambda: webbrowser.open('https://subdivx.com/'),
                      fg_color=EDGE).pack(side='left')
        self.button(actions, 'Aplicar sessão', apply).pack(side='right')

    def close(self):
        self.closing = True
        self.cancel.set()
        self.poster_cancel.set()
        self.poster_pool.shutdown(wait=False, cancel_futures=True)
        if self.session_window and self.session_window.winfo_exists():
            self.session_window.destroy()
        self.withdraw()


def main():
    logging.basicConfig(filename=ROOT / 'app.log', level=logging.ERROR,
                        format='%(asctime)s %(levelname)s %(message)s', encoding='utf-8')
    ctk.set_appearance_mode('dark')
    ctk.set_default_color_theme('dark-blue')
    App().mainloop()
