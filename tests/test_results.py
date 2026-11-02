import json
import unittest
from threading import Event
from unittest.mock import patch

from subdivx_app.catalog import Catalog
from subdivx_app.core import SearchSpec, Subtitle, SubdivxClient
from subdivx_app.results import group_titles


class ResultTests(unittest.TestCase):
    def test_franchise_groups_by_movie_and_orders_by_year(self):
        items = [Subtitle(1, 'Harry Potter and the Chamber of Secrets (2002)', 'WEB-DL', score=90),
                 Subtitle(2, 'Harry Potter and the Philosopher’s Stone (2001)', 'BluRay', score=70),
                 Subtitle(3, 'Harry Potter and the Philosopher’s Stone (2001) - 4K', '4K', score=80),
                 Subtitle(4, 'Harry Potter and the Chamber of Secrets (2002) aka HP 2', 'HD', score=80)]
        groups = group_titles(items, SearchSpec('Harry Potter'))
        self.assertEqual([group.year for group in groups], [2001, 2002])
        self.assertEqual([len(group.items) for group in groups], [2, 2])
        self.assertEqual(groups[0].items[0].id, 3)

    def test_remakes_and_episodes_stay_separate(self):
        groups = group_titles([Subtitle(1, 'Dune (1984)', ''), Subtitle(2, 'Dune (2021)', '')], SearchSpec('Dune'))
        self.assertEqual(len(groups), 2)
        groups = group_titles([Subtitle(1, 'Lost S01E01', ''), Subtitle(2, 'Lost S01E02', '')], SearchSpec('Lost', 'Série'))
        self.assertEqual(len(groups), 2)

    def test_recent_search_does_not_request_site_again(self):
        client = SubdivxClient()
        payload = {'aaData': [{'id': 1, 'titulo': 'Dune (2021)', 'descargas': 5}]}
        with patch.object(client, 'request', side_effect=[b'<script src="index-min.js?v=1.0">',
                                                          b'{"token":"LOCAL"}', json.dumps(payload).encode()]) as request:
            spec = SearchSpec('Dune', year=2021)
            first = client.search(spec, Event(), lambda _: None)
            second = client.search(spec, Event(), lambda _: None)
            self.assertEqual(request.call_count, 3)
            self.assertEqual(first, second)
            self.assertIsNot(first[0], second[0])
        client.close()

    def test_catalog_preserves_relevance_before_chronological_display(self):
        rows = [{'id': f'tt00000{i:02d}', 'name': f'Movie {i}', 'releaseInfo': str(2010 - i)} for i in range(10)]
        with patch('subdivx_app.catalog.requests.get') as get:
            get.return_value.content = b'{}'
            get.return_value.json.return_value = {'metas': rows}
            catalog = Catalog()
            movies = catalog.search('Movie', None, Event())
            self.assertEqual([movie.title for movie in movies[:8]], [f'Movie {i}' for i in range(8)])
            catalog.search('Movie', None, Event())
            self.assertEqual(get.call_count, 1)

    def test_catalog_ignores_invalid_ids_and_wrong_year(self):
        with patch('subdivx_app.catalog.requests.get') as get:
            get.return_value.content = b'{}'
            get.return_value.json.return_value = {'metas': [
                {'id': '../../bad', 'name': 'Movie', 'releaseInfo': '2001'},
                {'id': 'tt0241527', 'name': 'Movie', 'releaseInfo': '2001'},
                {'id': 'tt0295297', 'name': 'Movie II', 'releaseInfo': '2002'}]}
            movies = Catalog().search('Movie', 2001, Event())
            self.assertEqual([movie.imdb_id for movie in movies], ['tt0241527'])

    def test_selected_franchise_title_has_narrow_year_query(self):
        spec = SearchSpec('Harry Potter and the Sorcerer’s Stone', year=2001)
        self.assertEqual(spec.queries()[0], 'Harry Potter 2001')


if __name__ == '__main__':
    unittest.main()
