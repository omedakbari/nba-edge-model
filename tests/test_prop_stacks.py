import importlib.util
from pathlib import Path
import unittest
spec = importlib.util.spec_from_file_location('stacks', Path(__file__).resolve().parents[1] / 'scripts/build_prop_stacks.py')
stacks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stacks)

class StackTests(unittest.TestCase):
    def player(self, game, book='Book', p=.8):
        return dict(player_name='Player '+game, stat='PTS', direction='OVER', line=10.5,
                    prob=p, decimal_odds=1.5, game_id=game, team=game+'A', opp=game+'B',
                    book=book, approval={'approved': True})

    def test_unvalidated_joint_never_approved(self):
        r = stacks.build_report({'picks': [self.player('1'), self.player('2')]}, {}, '2026-10-26')
        self.assertEqual(len(r['stacks']), 1)
        self.assertIsNone(r['stacks'][0]['conservative_joint'])
        self.assertFalse(r['stacks'][0]['approved'])

    def test_overlap_and_cross_book_excluded(self):
        for picks in ([self.player('1'), self.player('1')], [self.player('1'), self.player('2', 'Other')]):
            self.assertEqual(stacks.build_report({'picks': picks}, {}, '2026-10-26')['stacks'], [])

    def test_probability_and_value_required(self):
        p = self.player('1', p=.7)
        p['decimal_odds'] = 1.1
        self.assertEqual(stacks.build_report({'picks': [p, self.player('2', p=.6)]}, {}, '2026-10-26')['status'], 'PASS')

    def test_mixed_stack(self):
        g = dict(game_id='2', home='H', away='A', model_wp=.8, home_ml=-200, away_ml=170, odds_provider='Book')
        r = stacks.build_report({'picks': [self.player('1')]}, {'picks': [g]}, '2026-10-26')
        self.assertEqual({x['kind'] for x in r['singles']}, {'game', 'player'})
        self.assertEqual(len(r['stacks']), 1)
