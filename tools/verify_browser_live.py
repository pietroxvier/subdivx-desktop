"""Busca real; passe --download para salvar o primeiro SRT classificado."""
import argparse
import json
import sys
from pathlib import Path
from threading import Event

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from subdivx_app.browser import BrowserSubdivxClient
from subdivx_app.core import SearchSpec, validate_srt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--title', default='Dune')
    parser.add_argument('--year', default='2021')
    parser.add_argument('--release', default='WEB-DL 1080p')
    parser.add_argument('--download', action='store_true')
    args = parser.parse_args()
    client = BrowserSubdivxClient()
    spec = SearchSpec.parse(args.title, year=args.year, release=args.release)
    try:
        results = client.search(spec, Event(), lambda message: print(message, flush=True))
        print(json.dumps({'results': len(results), 'top': [
            {'id': item.id, 'title': item.title, 'score': item.score} for item in results[:3]]},
            ensure_ascii=True), flush=True)
        if not results:
            raise SystemExit('Nenhum resultado na busca real.')
        if args.download:
            if results[0].score < 65:
                raise SystemExit('Compatibilidade insuficiente para o teste de download.')
            paths = client.download(results[0], spec, Path(__file__).resolve().parents[1] / 'legendas', Event())
            for path in paths:
                validate_srt(path.read_bytes())
            print(json.dumps({'download': 'OK', 'srt_files': [str(path) for path in paths],
                              'bytes': [path.stat().st_size for path in paths]}, ensure_ascii=True), flush=True)
    finally:
        client.close()


if __name__ == '__main__':
    main()
