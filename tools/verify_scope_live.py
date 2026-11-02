"""Verifica catálogo, capa, busca específica, cache e download na mesma sessão."""
import json
import sys
import tempfile
import time
from pathlib import Path
from threading import Event

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from subdivx_app.catalog import Catalog
from subdivx_app.browser import BrowserSubdivxClient
from subdivx_app.core import SearchSpec, validate_srt

cancel = Event()
catalog = Catalog()
started = time.monotonic()
movies = catalog.search('Harry Potter', None, cancel)
visible = sorted(movies[:8], key=lambda movie: movie.year or 9999)
print(json.dumps({'stage': 'catalog', 'seconds': round(time.monotonic() - started, 2),
                  'initial': [{'title': m.title, 'year': m.year, 'imdb': m.imdb_id} for m in visible]}, ensure_ascii=True), flush=True)
assert len(visible) == 8 and visible[0].year == 2001 and visible[-1].year == 2011
poster = catalog.poster(visible[0], cancel)
assert poster is not None
print(json.dumps({'stage': 'poster', 'dimensions': poster.size}), flush=True)
client = BrowserSubdivxClient()
try:
    spec = SearchSpec(visible[0].title, year=visible[0].year, release='WEB-DL 1080p')
    started = time.monotonic()
    results = client.search(spec, cancel, lambda text: print(text, flush=True))
    print(json.dumps({'stage': 'subtitles', 'seconds': round(time.monotonic() - started, 2),
                      'count': len(results), 'best': results[0].title if results else None}, ensure_ascii=True), flush=True)
    assert results
    started = time.monotonic()
    again = client.search(spec, cancel, lambda _: None)
    cached_seconds = time.monotonic() - started
    assert again[0].id == results[0].id and cached_seconds < 1
    print(json.dumps({'stage': 'cached_search', 'seconds': round(cached_seconds, 3)}), flush=True)
    with tempfile.TemporaryDirectory(prefix='subdivx-scope-') as temporary:
        started = time.monotonic()
        paths = client.download(results[0], spec, Path(temporary), cancel)
        for path in paths:
            validate_srt(path.read_bytes())
        print(json.dumps({'stage': 'download', 'seconds': round(time.monotonic() - started, 2),
                          'srt_count': len(paths), 'bytes': sum(path.stat().st_size for path in paths)}), flush=True)
finally:
    client.close(wait=True)
