import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from threading import Event
from unittest.mock import patch

from subdivx_app.core import (AppError, Cancelled, SearchSpec, SessionRequired,
                              SubdivxClient, Subtitle, choose_members, rank,
                              safe_name, save_subtitles, validate_srt)

SRT = '1\n00:00:01,000 --> 00:00:03,500\n¡Hola, señor!\n\n2\n00:00:04,000 --> 00:00:05,000\nAdiós.\n'


def archive(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as zipped:
        for name, data in entries.items():
            zipped.writestr(name, data)
    return buffer.getvalue()


class MatchingTests(unittest.TestCase):
    def test_parse_movie_and_embedded_episode(self):
        self.assertEqual(SearchSpec.parse('Dune (2021)').label, 'Dune (2021)')
        spec = SearchSpec.parse('The Bear S02E03')
        self.assertEqual((spec.title, spec.kind, spec.season, spec.episode), ('The Bear', 'Série', 2, 3))
        self.assertEqual(SearchSpec.parse('1917').title, '1917')

    def test_conflicting_or_invalid_filters(self):
        for args in [('Dune 2021', 'Filme', '1984'), ('Lost', 'Série', '', '', '4'),
                     ('Lost S01E02', 'Série', '', '2'), ('Dune', 'Filme', 'abc')]:
            with self.assertRaises(AppError):
                SearchSpec.parse(*args)

    def test_reject_wrong_year_and_unrelated_title(self):
        spec = SearchSpec('Dune', year=2021)
        self.assertIsNone(rank(Subtitle(1, 'Dune (1984)', '', 999999), spec))
        self.assertIsNone(rank(Subtitle(2, 'The Matrix (2021)', '', 999999), spec))

    def test_episode_rejection_beats_popularity(self):
        spec = SearchSpec('The Bear', 'Série', season=1, episode=2)
        self.assertIsNone(rank(Subtitle(1, 'The Bear S01E03', '', 999999), spec))
        self.assertIsNone(rank(Subtitle(2, 'The Bear S02E02', '', 999999), spec))
        self.assertIsNotNone(rank(Subtitle(3, 'The Bear S01E02', '', 20), spec))

    def test_release_preference(self):
        spec = SearchSpec('Dune', year=2021, release='WEB-DL 1080p')
        good = rank(Subtitle(1, 'Dune (2021)', 'WEB-DL 1080p', 50), spec)
        other = rank(Subtitle(2, 'Dune (2021)', 'BluRay 720p', 50000), spec)
        self.assertGreater(good.score, other.score)

    def test_accent_and_html(self):
        from subdivx_app.core import plain
        self.assertEqual(plain('<b>Amélie &amp; amigos</b>'), 'Amélie & amigos')
        self.assertIsNotNone(rank(Subtitle(1, 'Amélie (2001)', '', 1), SearchSpec('Amelie', year=2001)))


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name)
        self.cancel = Event()

    def tearDown(self):
        self.temp.cleanup()

    def test_zip_extracts_only_srt_flattening_paths(self):
        data = archive({'../../escape.srt': SRT.encode(), 'run.exe': b'exe', 'other.ass': b'ass'})
        paths = save_subtitles(data, self.output, SearchSpec('Dune', year=2021), 123, self.cancel)
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0].parent, self.output / 'Dune (2021)')
        self.assertEqual(paths[0].read_text(encoding='utf-8'), SRT)
        self.assertEqual(len(list(self.output.rglob('*'))), 2)

    def test_episode_from_pack(self):
        data = archive({'The.Bear.S01E01.srt': SRT.encode(), 'The.Bear.S01E02.srt': SRT.encode()})
        paths = save_subtitles(data, self.output, SearchSpec('The Bear', 'Série', season=1, episode=2), 5, self.cancel)
        self.assertEqual(len(paths), 1)
        self.assertIn('S01E02', paths[0].name)

    def test_wrong_or_ambiguous_episode_not_saved(self):
        spec = SearchSpec('The Bear', 'Série', season=1, episode=2)
        for entries in [{'S01E03.srt': SRT.encode()}, {'sub.srt': SRT.encode()},
                        {'a.srt': SRT.encode(), 'b.srt': SRT.encode()}]:
            with self.assertRaises(AppError):
                save_subtitles(archive(entries), self.output, spec, 5, self.cancel)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_unmarked_file_requires_confirmed_publication(self):
        spec = SearchSpec('The Bear', 'Série', season=1, episode=2)
        paths = save_subtitles(archive({'sub.srt': SRT.encode()}), self.output, spec, 5, self.cancel, True)
        self.assertEqual(len(paths), 1)

    def test_preserves_existing_files(self):
        spec = SearchSpec('Dune')
        first = save_subtitles(SRT.encode(), self.output, spec, 5, self.cancel)[0]
        second = save_subtitles(SRT.encode(), self.output, spec, 5, self.cancel)[0]
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_file() and second.is_file())

    def test_legacy_encoding_and_invalid_data(self):
        self.assertIn('señor', validate_srt(SRT.encode('cp1252')))
        self.assertIn('señor', validate_srt(SRT.encode('utf-16')))
        for data in [b'<html>challenge</html>', b'', b'1\n99:99:00,000 --> 00:00:01,000\nBad\n']:
            with self.assertRaises(AppError):
                save_subtitles(data, self.output, SearchSpec('Dune'), 5, self.cancel)

    def test_cancel_and_size_limit(self):
        self.cancel.set()
        with self.assertRaises(Cancelled):
            save_subtitles(SRT.encode(), self.output, SearchSpec('Dune'), 5, self.cancel)
        self.cancel.clear()
        with patch('subdivx_app.core.MAX_SRT', 10):
            with self.assertRaises(AppError):
                save_subtitles(archive({'large.srt': SRT.encode()}), self.output, SearchSpec('Dune'), 5, self.cancel)

    def test_no_srt_and_safe_windows_names(self):
        with self.assertRaises(AppError):
            choose_members(['sub.ass'], SearchSpec('Dune'))
        self.assertEqual(safe_name('CON'), '_CON')
        self.assertEqual(safe_name('Dune: Part/Two?'), 'Dune_ Part_Two_')


class ProtocolTests(unittest.TestCase):
    def test_search_dynamic_field_token_and_ranking(self):
        client = SubdivxClient()
        items = {'aaData': [{'id': '5', 'titulo': 'Dune (2021)', 'descripcion': '<b>WEB-DL</b>',
                              'descargas': '123', 'nick': 'test'},
                             {'id': '6', 'titulo': 'Dune (1984)', 'descargas': '999999'}]}
        with patch.object(client, 'request', side_effect=[b'<script src="index-min.js?v=1.2.3">',
                                                          b'{"token":"example"}', json.dumps(items).encode()]) as request:
            result = client.search(SearchSpec('Dune', year=2021), Event(), lambda _: None)
        self.assertEqual([item.id for item in result], [5])
        form = request.call_args.kwargs['data']
        self.assertEqual(form['buscar123'], 'Dune 2021')
        self.assertEqual(form['token'], 'example')
        client.close()

    def test_block_does_not_become_empty_results(self):
        client = SubdivxClient()
        with patch.object(client, 'request', side_effect=[b'<script src="index-min.js?v=1.0">',
                                                          b'{"token":"t"}', b'<html>login</html>']):
            with self.assertRaises(SessionRequired):
                client.search(SearchSpec('Dune'), Event(), lambda _: None)
        client.close()

    def test_external_redirect_refused(self):
        client = SubdivxClient('private=value')
        with self.assertRaises(AppError):
            client.request('https://example.com/', Event())
        client.close()

    def test_header_injection_refused(self):
        with self.assertRaises(AppError):
            SubdivxClient('cookie\r\nInjected: header')

    def test_pasted_headers_and_hidden_newlines(self):
        client = SubdivxClient('Cookie: cf_clearance=abc\r\ndef;\n sdx=123\n',
                               'User-Agent: Mozilla/5.0\r\n Chrome/156.0.0.0 Safari/537.36\n')
        self.assertEqual(client.session.headers['Cookie'], 'cf_clearance=abcdef;sdx=123')
        self.assertEqual(client.session.headers['User-Agent'], 'Mozilla/5.0 Chrome/156.0.0.0 Safari/537.36')
        client.close()

    def test_cookie_pairs_on_separate_lines(self):
        client = SubdivxClient('Cookie\ncf_clearance=abc\nsdx=def')
        self.assertEqual(client.session.headers['Cookie'], 'cf_clearance=abc; sdx=def')
        client.close()


if __name__ == '__main__':
    unittest.main()
