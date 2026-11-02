"""Catálogo Cinemeta: identificação IMDb, título, ano e capa, sem chave."""
from dataclasses import dataclass
import io
import re
from urllib.parse import quote

import requests
from PIL import Image

from .core import AppError, check_cancel, normalized


@dataclass(frozen=True)
class Movie:
    imdb_id: str
    title: str
    year: int | None

    @property
    def label(self):
        return self.title + (f' ({self.year})' if self.year else '')

    @property
    def imdb_url(self):
        return f'https://www.imdb.com/title/{self.imdb_id}/'


class Catalog:
    def __init__(self):
        self._cache = {}

    def search(self, title, year, cancel):
        check_cancel(cancel)
        key = (normalized(title), year)
        if key in self._cache:
            return list(self._cache[key])
        try:
            response = requests.get('https://v3-cinemeta.strem.io/catalog/movie/top/search='
                                    + quote(title, safe='') + '.json', timeout=(4, 8))
            response.raise_for_status()
            if len(response.content) > 2 * 1024 * 1024:
                raise ValueError
            rows = response.json().get('metas', [])
            if not isinstance(rows, list):
                raise ValueError
        except (requests.RequestException, ValueError, AttributeError):
            raise AppError('O catálogo de filmes não respondeu. Você pode buscar diretamente no Subdivx.') from None
        check_cancel(cancel)
        result, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            imdb = str(row.get('imdb_id') or row.get('id') or '')
            name = str(row.get('name') or '').strip()
            match = re.search(r'\b(?:19|20)\d{2}\b', str(row.get('releaseInfo', '')))
            release_year = int(match[0]) if match else None
            if not re.fullmatch(r'tt\d{5,12}', imdb) or not name or imdb in seen:
                continue
            if year and release_year != year:
                continue
            seen.add(imdb)
            result.append(Movie(imdb, name, release_year))
        # Preserva a relevância do serviço. A GUI ordena cronologicamente
        # somente os oito primeiros, sem puxar extras pouco relacionados.
        if len(self._cache) >= 30:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = tuple(result[:40])
        return list(self._cache[key])

    @staticmethod
    def poster(movie, cancel):
        check_cancel(cancel)
        if not re.fullmatch(r'tt\d{5,12}', movie.imdb_id):
            return None
        try:
            with requests.get(f'https://images.metahub.space/poster/small/{movie.imdb_id}/img',
                              timeout=(4, 6), stream=True) as response:
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_content(65536):
                    check_cancel(cancel)
                    data.extend(chunk)
                    if len(data) > 2 * 1024 * 1024:
                        return None
            with Image.open(io.BytesIO(data)) as image:
                if image.width > 3000 or image.height > 4500:
                    return None
                image.thumbnail((240, 360))
                return image.convert('RGB').copy()
        except (requests.RequestException, OSError, ValueError):
            return None
