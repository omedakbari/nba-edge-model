"""config/model.yaml is the only home of hand-set blend weights (AUDIT_2026-11-24.md item 5)."""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import model_config  # noqa: E402

# A multiplication by a bare blend-looking ratio, e.g. "0.35 * p", "x * 0.65", ".5 * model".
BLEND_LITERAL = re.compile(r"(?<![\w.])0?\.(35|65|40|60|50?)\s*\*|\*\s*0?\.(35|65|40|60|50?)(?![\d])")
# Research/ablation scripts compare blend arms on purpose; they never feed the live board or card.
RESEARCH = {'scripts/phase1_calibration_audit.py', 'scripts/market_blend_research.py',
            'scripts/phase2_isotonic_fg3m.py'}   # 0.60 there is a date split, not a blend
CODE_DIRS = ['src', 'scripts', 'dashboard', 'worker/prop_worker', 'config']


def _code_lines(path: Path):
    for i, line in enumerate(path.read_text(errors='replace').splitlines(), 1):
        code = line.split('#', 1)[0]
        if code.strip():
            yield i, code


def test_loader_reads_every_weight():
    w = model_config.WEIGHTS
    assert w['props_model']['l10_weight'] == 0.40 and w['card']['hist_weight'] == 0.35
    assert w['card']['no_market_shrink'] == 0.5 and w['team_ratings']['season_weight'] == 0.65
    assert w['prop_prior']['signal_weight'] == 0.5


@pytest.mark.parametrize('bad', ['1.5', '-0.1', '"x"'])
def test_loader_rejects_out_of_range(tmp_path, bad):
    text = (ROOT / 'config/model.yaml').read_text().replace('hist_weight: 0.35', f'hist_weight: {bad}')
    p = tmp_path / 'model.yaml'
    p.write_text(text)
    with pytest.raises(ValueError, match='card.hist_weight'):
        model_config.load(p)


def test_every_consumer_reads_the_config():
    from config import settings
    from src.models import team_ratings
    sys.path.insert(0, str(ROOT / 'worker'))
    from prop_worker import card, config as wconfig
    w = model_config.WEIGHTS
    assert settings.BLEND_L10_WEIGHT == w['props_model']['l10_weight']
    assert settings.PROP_PRIOR_SIGNAL_WEIGHT == w['prop_prior']['signal_weight']
    assert team_ratings.SEASON_WEIGHT == w['team_ratings']['season_weight']
    assert team_ratings.FORM_WEIGHT == pytest.approx(1 - w['team_ratings']['season_weight'])
    assert wconfig.MODEL_YAML == model_config.PATH
    assert card.W_HIST == w['card']['hist_weight'] and card.W_MKT == pytest.approx(1 - card.W_HIST)
    assert card.NO_MARKET_SHRINK == w['card']['no_market_shrink']


def test_no_hard_coded_blend_ratios_outside_config():
    hits = []
    for d in CODE_DIRS:
        for path in (ROOT / d).rglob('*.py'):
            rel = path.relative_to(ROOT).as_posix()
            if rel in RESEARCH or '/tests/' in rel or 'ablation' in rel:
                continue
            for i, code in _code_lines(path):
                if BLEND_LITERAL.search(code):
                    hits.append(f'{rel}:{i}: {code.strip()}')
    assert hits == [], 'hard-coded blend-like ratio; move it to config/model.yaml or allowlist:\n' + '\n'.join(hits)
