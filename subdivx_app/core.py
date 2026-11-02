from __future__ import annotations

import html
import copy
import io
import math
import os
import re
import shutil
import tempfile
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from threading import Event
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests

BASE = 'https://subdivx.com'
DEFAULT_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
MAX_DOWNLOAD = 32 * 1024 * 1024
MAX_SRT = 12 * 1024 * 1024
MAX_TOTAL = 48 * 1024 * 1024
EPISODE = re.compile(r'\b(?:s(\d{1,2})e(\d{1,3})|(\d{1,2})x(\d{1,3}))\b', re.I)
SEASON = re.compile(r'\b(?:s|season\s*|temporada\s*)(\d{1,2})\b', re.I)
YEAR = re.compile(r'\b(?:19|20)\d{2}\b')
TIMING = re.compile(r'^\s*(\d{1,3}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*'
                    r'(\d{1,3}):(\d{2}):(\d{2})[,.](\d{3})[^\n]*\n([^\n]+)', re.M)
RELEASE = re.compile(r'\b(?:2160p|1080p|720p|480p|web[ ._-]?dl|webrip|bluray|blu[ ._-]?ray|'
                     r'brrip|bdrip|hdtv|dvdrip|x26[45]|h[ .]?26[45]|hevc|hdr|remux)\b', re.I)


class AppError(Exception):
    """Mensagem que pode ser apresentada diretamente na interface."""


class SessionRequired(AppError):
    pass


class Cancelled(AppError):
    pass


def check_cancel(cancel: Event) -> None:
    if cancel.is_set():
        raise Cancelled('Operação cancelada.')


def plain(value: object) -> str:
    return html.unescape(re.sub(r'<[^>]*>', ' ', str(value or ''))).strip()


def normalized(text: str) -> str:
    folded = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode()
    return ' '.join(re.findall(r'[a-z0-9]+', folded.lower()))


def clean_header(value: str, name: str) -> str:
    """Aceita um cabeçalho colado do DevTools, sem permitir outros headers."""
    value = value.strip().lstrip('\ufeff')
    if name.lower() == 'cookie' and value.startswith(('[', '{')):
        import json
        try:
            exported = json.loads(value)
            if isinstance(exported, dict):
                exported = exported.get('cookies', [exported])
            if not isinstance(exported, list):
                raise ValueError
            pairs = []
            for cookie in exported:
                if not isinstance(cookie, dict):
                    raise ValueError
                domain = str(cookie.get('domain', 'subdivx.com')).lstrip('.')
                if domain not in {'subdivx.com', 'www.subdivx.com'}:
                    continue
                key, data = str(cookie['name']), str(cookie['value'])
                if not re.fullmatch(r'[A-Za-z0-9_!#$%&\x27*+.^`|~-]+', key) or any(char in data for char in ';\r\n\x00'):
                    raise ValueError
                pairs.append(f'{key}={data}')
            if exported and not pairs:
                raise ValueError
            value = '; '.join(pairs)
        except (ValueError, TypeError, KeyError) as exc:
            raise AppError('O JSON de cookies é inválido ou não contém cookies do Subdivx.') from exc
    value = re.sub(r'^' + re.escape(name) + r'\s*:\s*', '', value, count=1, flags=re.I)
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if lines and lines[0].lower() == name.lower():
        lines.pop(0)
    if any(re.match(r'^[A-Za-z][A-Za-z0-9_-]*\s*:', line) for line in lines):
        raise AppError(f'Cole somente o cabeçalho {name}, sem os outros cabeçalhos da requisição.')
    if name.lower() == 'cookie':
        result = ''
        for line in lines:
            # Cookies separados por linha recebem ";". Quebras no meio de um
            # valor longo são removidas, preservando o token sem espaços extras.
            if result and not result.endswith(';') and not line.startswith(';') and re.match(r'^[^=;\s]+=', line):
                result += '; '
            result += line
        value = result
    else:
        value = ' '.join(lines)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise AppError(f'O campo {name} contém caracteres de controle inválidos.')
    return value


def episode_of(text: str) -> tuple[int | None, int | None]:
    match = EPISODE.search(text)
    if match:
        return int(match[1] or match[3]), int(match[2] or match[4])
    match = SEASON.search(text)
    return (int(match[1]), None) if match else (None, None)


def identity_title(text: str) -> str:
    text = re.split(r'\s*[([](?:19|20)\d{2}[)\]]', text, maxsplit=1)[0]
    match = RELEASE.search(text)
    if match:
        text = text[:match.start()]
    text = EPISODE.sub(' ', text)
    text = SEASON.sub(' ', text)
    text = YEAR.sub(' ', text) if len(normalized(text).split()) > 1 else text
    return normalized(text)


@dataclass(frozen=True)
class SearchSpec:
    title: str
    kind: str = 'Filme'
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    release: str = ''

    @classmethod
    def parse(cls, title: str, kind: str = 'Filme', year: str = '',
              season: str = '', episode: str = '', release: str = '') -> SearchSpec:
        title = title.strip()
        if len(title) < 2:
            raise AppError('Digite pelo menos dois caracteres do título.')
        def number(value: str, label: str, low: int, high: int) -> int | None:
            if not value.strip():
                return None
            if not value.strip().isdigit() or not low <= int(value) <= high:
                raise AppError(f'{label}: informe um número entre {low} e {high}.')
            return int(value)
        y = number(year, 'Ano', 1888, 2100)
        s = number(season, 'Temporada', 0, 99)
        e = number(episode, 'Episódio', 1, 999)
        embedded_s, embedded_e = episode_of(title)
        if embedded_s is not None:
            if (s is not None and s != embedded_s) or (e is not None and embedded_e is not None and e != embedded_e):
                raise AppError('O episódio no título difere dos campos de temporada/episódio.')
            s = embedded_s if s is None else s
            e = embedded_e if e is None else e
            title = SEASON.sub(' ', EPISODE.sub(' ', title)).strip(' ._-')
            kind = 'Série'
        match = re.search(r'\s+\(?((?:19|20)\d{2})\)?\s*$', title)
        if match:
            if y is not None and y != int(match[1]):
                raise AppError('O ano no título difere do campo Ano.')
            y = int(match[1])
            title = title[:match.start()].strip()
        if e is not None and s is None:
            raise AppError('Informe também a temporada para buscar um episódio.')
        if kind == 'Filme' and (s is not None or e is not None):
            raise AppError('Selecione Série para usar temporada e episódio.')
        if len(normalized(title)) < 2:
            raise AppError('Informe o nome da obra além do ano/episódio.')
        return cls(title, kind, y, s, e, release.strip())

    @property
    def label(self) -> str:
        result = self.title + (f' ({self.year})' if self.year else '')
        if self.season is not None:
            result += f' S{self.season:02d}'
        if self.episode is not None:
            result += f'E{self.episode:02d}'
        return result

    def queries(self) -> list[str]:
        base = self.title.replace('.', ' ').replace('_', ' ')
        if self.season is not None and self.episode is not None:
            choices = [f'{base} S{self.season:02d}E{self.episode:02d}',
                       f'{base} {self.season}x{self.episode:02d}', f'{base} S{self.season:02d}', base]
        elif self.season is not None:
            choices = [f'{base} S{self.season:02d}', base]
        else:
            choices = [f'{base} {self.year}', base] if self.year else [base]
            # Um prefixo de franquia + ano encontra nomes regionais diferentes,
            # mantendo o nome completo para a comparação dos resultados.
            head = re.split(r'\s+and the\s+|:\s*', base, maxsplit=1, flags=re.I)[0]
            if self.year and head != base and len(head.split()) >= 2:
                choices.insert(0, f'{head} {self.year}')
        folded = unicodedata.normalize('NFKD', base).encode('ascii', 'ignore').decode()
        if folded != base:
            choices.append(folded)
        return list(dict.fromkeys(choices))[:5]


@dataclass
class Subtitle:
    id: int
    title: str
    description: str
    downloads: int = 0
    uploader: str = ''
    score: float = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def page_url(self) -> str:
        return BASE + '/'


def rank(item: Subtitle, spec: SearchSpec) -> Subtitle | None:
    wanted, actual = identity_title(spec.title), identity_title(item.title)
    a, b = set(wanted.split()), set(actual.split())
    coverage = len(a & b) / max(1, len(a))
    precision = len(a & b) / max(1, len(b))
    similarity = SequenceMatcher(None, wanted, actual).ratio()
    title_score = .5 * coverage + .2 * precision + .3 * similarity
    if title_score < .47:
        return None
    reasons = [f'Título: {round(title_score * 100)} de 100']
    score = title_score * 76
    title_years = set(YEAR.findall(item.title))
    if spec.year:
        if title_years and str(spec.year) not in title_years:
            return None
        if str(spec.year) in title_years:
            score += 8
            reasons.append('Ano confere')
        else:
            reasons.append('Ano não informado no resultado')
    season, episode = episode_of(item.title)
    if season is None:
        season, episode = episode_of(item.description)
    if spec.kind == 'Filme' and season is not None:
        return None
    if spec.season is not None:
        if season is not None and season != spec.season:
            return None
        if spec.episode is not None and episode is not None and episode != spec.episode:
            return None
        if season == spec.season:
            score += 5
        if spec.episode is not None and episode == spec.episode:
            score += 5
            reasons.append('Temporada e episódio conferem')
        elif episode is None:
            reasons.append('Pacote ou episódio não identificado; arquivos serão conferidos')
    if spec.release:
        requested = set(normalized(spec.release).split())
        available = set(normalized(item.title + ' ' + item.description).split())
        matches = len(requested & available) / max(1, len(requested))
        score += 10 * matches
        reasons.append(f'Versão do vídeo: {round(matches * 100)} de 100')
    score += min(4, math.log10(max(1, item.downloads)))
    item.score = round(min(99, score), 1)
    item.reasons = reasons
    return item


class SubdivxClient:
    def __init__(self, cookie: str = '', user_agent: str = DEFAULT_UA):
        cookie = clean_header(cookie, 'Cookie')
        user_agent = clean_header(user_agent, 'User-Agent')
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': user_agent or DEFAULT_UA,
                                     'Referer': BASE + '/', 'Origin': BASE,
                                     'X-Requested-With': 'XMLHttpRequest'})
        if cookie.strip():
            self.session.headers['Cookie'] = cookie.strip()
        self.last_search = 0.0
        self._search_cache = {}

    def close(self) -> None:
        self.session.close()

    def request(self, path: str, cancel: Event, method: str = 'GET', **kwargs) -> bytes:
        url = urljoin(BASE + '/', path)
        for _ in range(5):
            check_cancel(cancel)
            parsed = urlparse(url)
            if parsed.scheme != 'https' or parsed.hostname not in {'subdivx.com', 'www.subdivx.com'}:
                raise AppError('O site redirecionou para um endereço inesperado.')
            try:
                with self.session.request(method, url, timeout=(10, 25), stream=True,
                                          allow_redirects=False, **kwargs) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        url = urljoin(url, response.headers.get('Location', ''))
                        if response.status_code in {301, 302, 303}:
                            method, kwargs = 'GET', {}
                        continue
                    if response.status_code in {401, 403}:
                        raise SessionRequired('O Subdivx solicitou uma sessão válida. Abra Sessão e cole o Cookie e o User-Agent do navegador que acessa o site.')
                    if response.status_code == 429:
                        raise AppError('O Subdivx limitou as consultas. Aguarde alguns minutos antes de tentar novamente.')
                    response.raise_for_status()
                    content = bytearray()
                    for chunk in response.iter_content(64 * 1024):
                        check_cancel(cancel)
                        content.extend(chunk)
                        if len(content) > MAX_DOWNLOAD:
                            raise AppError('A resposta excedeu o limite de 32 MB.')
                    data = bytes(content)
                    if any(marker in data[:10000].lower() for marker in
                           (b'just a moment', b'cf-chl-', b'challenge-platform')):
                        raise SessionRequired('O Cloudflare pediu verificação. Renove a sessão em Sessão e tente novamente.')
                    return data
            except requests.RequestException as exc:
                raise AppError('Não foi possível acessar o Subdivx. Verifique a conexão e tente novamente.') from exc
        raise AppError('O Subdivx retornou redirecionamentos em excesso.')

    def search(self, spec: SearchSpec, cancel: Event, progress: Callable[[str], None]) -> list[Subtitle]:
        import json
        check_cancel(cancel)
        cache_key = (normalized(spec.title), spec.kind, spec.year, spec.season, spec.episode, normalized(spec.release))
        cached = self._search_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 300:
            progress('Usando a pesquisa recente · sem nova consulta ao site.')
            return copy.deepcopy(cached[1])
        homepage = self.request('/', cancel).decode('utf-8', 'replace')
        version = re.search(r'(?:index-min\.js|sdx-min\.css)\?v=([0-9.]+)', homepage, re.I)
        if not version:
            raise AppError('Não foi possível identificar a versão do Subdivx. O site pode ter mudado ou solicitado login.')
        try:
            token = json.loads(self.request('/inc/gt.php?gt=1', cancel)).get('token')
        except (ValueError, AttributeError) as exc:
            raise SessionRequired('O Subdivx não forneceu o token de busca. Renove a sessão.') from exc
        if not token:
            raise SessionRequired('A sessão do Subdivx não retornou um token de busca.')
        found: dict[int, Subtitle] = {}
        for query in spec.queries():
            progress(f'Buscando: {query}')
            if cancel.wait(max(0, 5 - (time.monotonic() - self.last_search))):
                check_cancel(cancel)
            self.last_search = time.monotonic()
            raw = self.request('/inc/ajax.php', cancel, 'POST', data={
                'tabla': 'resultados', 'filtros': '', 'buscar' + version[1].replace('.', ''): query,
                'token': str(token)})
            try:
                payload = json.loads(raw)
            except ValueError as exc:
                raise SessionRequired('A busca retornou uma página em vez de resultados. Renove a sessão.') from exc
            if not isinstance(payload, dict) or not isinstance(payload.get('aaData'), list):
                raise AppError('O formato da busca do Subdivx mudou ou a sessão expirou.')
            for row in payload['aaData']:
                if not isinstance(row, dict):
                    continue
                try:
                    ident = int(row['id'])
                    downloads = int(re.sub(r'\D', '', str(row.get('descargas') or 0)) or '0')
                except (KeyError, ValueError, TypeError):
                    continue
                if ident <= 0 or ident in found:
                    continue
                candidate = rank(Subtitle(ident, plain(row.get('titulo')), plain(row.get('descripcion')),
                                          downloads, plain(row.get('nick'))), spec)
                if candidate:
                    found[ident] = candidate
            # Se o resultado mais específico falhou, tenta as variantes com intervalo.
            if found:
                break
        result = sorted(found.values(), key=lambda x: (x.score, x.downloads), reverse=True)[:1000]
        if len(self._search_cache) >= 20:
            self._search_cache.pop(next(iter(self._search_cache)))
        self._search_cache[cache_key] = (time.monotonic(), copy.deepcopy(result))
        return result

    def download(self, item: Subtitle, spec: SearchSpec, output: Path, cancel: Event) -> list[Path]:
        data = self.request(f'/descargar.php?f=1&id={item.id}', cancel)
        marked = episode_of(item.title)
        if marked[0] is None:
            marked = episode_of(item.description)
        confirmed = spec.episode is not None and marked == (spec.season, spec.episode)
        return save_subtitles(data, output, spec, item.id, cancel, confirmed)


def safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', unicodedata.normalize('NFC', name))
    name = re.sub(r'\s+', ' ', name).strip(' .')[:100].rstrip(' .') or 'Legenda'
    if name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        name = '_' + name
    return name


def validate_srt(data: bytes) -> str:
    if not data or len(data) > MAX_SRT:
        raise AppError('Legenda vazia ou maior que 12 MB.')
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        text = data.decode('utf-16')
    else:
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = data.decode('cp1252', 'replace')
    text = text.replace('\r\n', '\n').replace('\r', '\n').strip() + '\n'
    matches = list(TIMING.finditer(text))
    if not matches or '\x00' in text:
        raise AppError('O arquivo recebido não contém uma legenda SRT válida.')
    for match in matches:
        values = [int(x) for x in match.groups()[:8]]
        if any(values[i] >= 60 for i in (1, 2, 5, 6)):
            raise AppError('A legenda contém timestamps inválidos.')
        start = ((values[0] * 60 + values[1]) * 60 + values[2]) * 1000 + values[3]
        end = ((values[4] * 60 + values[5]) * 60 + values[6]) * 1000 + values[7]
        if end < start or not match[9].strip():
            raise AppError('A legenda contém um intervalo inválido.')
    return text


def configure_rar() -> None:
    import rarfile
    for tool in (shutil.which('unrar'), r'C:\Program Files\WinRAR\UnRAR.exe'):
        if tool and Path(tool).is_file():
            rarfile.UNRAR_TOOL = tool
            return
    for tool in (shutil.which('7z'), r'C:\Program Files\7-Zip\7z.exe'):
        if tool and Path(tool).is_file():
            rarfile.SEVENZIP_TOOL = tool
            return


def choose_members(names: list[str], spec: SearchSpec) -> list[str]:
    selected = []
    for name in names:
        if not name.lower().endswith('.srt'):
            continue
        # Nunca usa caminhos do arquivo compactado como destinos de extração.
        season, episode = episode_of(name)
        if spec.season is not None and season is not None and season != spec.season:
            continue
        if spec.episode is not None and episode is not None and episode != spec.episode:
            continue
        selected.append(name)
    if spec.episode is not None:
        exact = [name for name in selected if episode_of(name) == (spec.season, spec.episode)]
        if exact:
            selected = exact
        elif len(selected) != 1 or episode_of(selected[0])[1] is not None:
            raise AppError('O pacote não identifica o episódio solicitado. Escolha outro resultado.')
        # Um único arquivo sem episódio só será aceito se o resultado confirmou o episódio.
    if not selected:
        raise AppError('Nenhum SRT compatível foi encontrado. O pacote pode conter apenas ASS/SUB ou outro episódio.')
    if len(selected) > 100:
        raise AppError('O pacote contém mais de 100 legendas; escolha um resultado mais específico.')
    return selected


def save_subtitles(data: bytes, output: Path, spec: SearchSpec, ident: int,
                   cancel: Event, allow_unmarked_episode: bool = False) -> list[Path]:
    check_cancel(cancel)
    files: list[tuple[str, str]] = []
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = choose_members([info.filename for info in archive.infolist() if not info.is_dir()], spec)
            infos = [archive.getinfo(name) for name in names]
            if sum(info.file_size for info in infos) > MAX_TOTAL or any(info.file_size > MAX_SRT for info in infos):
                raise AppError('O tamanho das legendas descompactadas excede o limite.')
            for info in infos:
                check_cancel(cancel)
                if spec.episode is not None and episode_of(info.filename)[1] is None and not allow_unmarked_episode:
                    raise AppError('O arquivo não identifica o episódio solicitado. Escolha outro resultado.')
                with archive.open(info) as stream:
                    files.append((info.filename, validate_srt(stream.read(MAX_SRT + 1))))
    elif data.startswith(b'Rar!'):
        try:
            import rarfile
            configure_rar()
            with tempfile.TemporaryDirectory(prefix='subdivx-') as temporary:
                archive_path = Path(temporary) / 'download.rar'
                archive_path.write_bytes(data)
                with rarfile.RarFile(archive_path) as archive:
                    names = choose_members([i.filename for i in archive.infolist() if not i.isdir()], spec)
                    infos = [archive.getinfo(name) for name in names]
                    if sum(i.file_size for i in infos) > MAX_TOTAL or any(i.file_size > MAX_SRT for i in infos):
                        raise AppError('As legendas descompactadas excedem o limite.')
                    for info in infos:
                        check_cancel(cancel)
                        if spec.episode is not None and episode_of(info.filename)[1] is None and not allow_unmarked_episode:
                            raise AppError('O arquivo não identifica o episódio solicitado. Escolha outro resultado.')
                        with archive.open(info) as stream:
                            files.append((info.filename, validate_srt(stream.read(MAX_SRT + 1))))
        except ImportError as exc:
            raise AppError('Instale as dependências pelo Instalar.bat para abrir RAR.') from exc
        except rarfile.Error as exc:
            raise AppError('Não foi possível abrir o RAR. Verifique se o 7-Zip ou WinRAR está instalado e se o pacote não tem senha.') from exc
    else:
        if spec.episode is not None and not allow_unmarked_episode:
            raise AppError('Este arquivo não permite confirmar o episódio. Escolha outro resultado.')
        files = [(spec.label + '.srt', validate_srt(data))]
    check_cancel(cancel)
    directory = output / safe_name(spec.title + (f' ({spec.year})' if spec.year else ''))
    directory.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    try:
        for index, (name, text) in enumerate(files, 1):
            check_cancel(cancel)
            basename = safe_name(Path(name.replace('\\', '/')).stem)
            target = directory / f'{basename} [sdx-{ident}].srt'
            suffix = 2
            while True:
                try:
                    # Criação exclusiva: nunca substitui uma legenda existente.
                    with target.open('x', encoding='utf-8', newline='\n') as stream:
                        saved.append(target)
                        stream.write(text)
                    break
                except FileExistsError:
                    target = directory / f'{basename} [sdx-{ident}] ({suffix}).srt'
                    suffix += 1
        return saved
    except BaseException:
        for path in saved:
            path.unlink(missing_ok=True)
        raise
