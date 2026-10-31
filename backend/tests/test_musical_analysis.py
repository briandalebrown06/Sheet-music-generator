"""Explicit synthetic note fixtures test suggestions, not transcription quality."""
from copy import deepcopy
from fractions import Fraction
import math
import random

import pytest

from backend.app.musical_analysis import grid_fit, recommend_musical_settings


def event(index, start, duration=.18, pitch=60):
    return {"id": f"generated-analysis-fixture-{index}", "pitch": pitch,
            "start": start, "end": start + duration, "velocity": 80,
            "original_timing": {"start": start, "end": start + duration},
            "provenance": "detected", "part_id": "generated-test"}


def rhythm_fixture(bpm=101.37, phase=.117, count=130, span_offset=0.):
    # Vary the spacing between eighth-note attacks, including syncopations and
    # rests; this provides more evidence than a metronome's ambiguous quarters.
    steps = (0, 1, 2, 4, 5, 7, 8, 10, 11, 14)
    result = []
    for i in range(count):
        step = (i // len(steps)) * 16 + steps[i % len(steps)]
        jitter = .004 * math.sin(i * 1.71)
        start = span_offset + phase + step * 30 / bpm + jitter
        result.append(event(i, start, pitch=(60, 64, 67, 65, 62)[i % 5]))
    return result


def test_constant_tempo_is_refined_and_does_not_mutate_notes():
    source = rhythm_fixture()
    original = deepcopy(source)
    result = recommend_musical_settings(source)
    settings = result['suggested_settings']
    assert settings['tempo_bpm'] == pytest.approx(101.37, abs=.03)
    assert settings['confirmed_fields'] == []
    assert 'tempo_bpm' in settings['estimated_fields']
    assert 'time_signature' not in settings
    assert source == original
    assert result['tempo_candidates'][0]['median_grid_error_ms'] < 5
    assert result['tempo_candidates'][0]['aliases_bpm']
    assert all('confidence' not in row and 'accuracy' not in row for row in result['tempo_candidates'])


def test_chord_doublings_do_not_reweight_attack_evidence():
    source = rhythm_fixture()
    doubled = source + [{**n, 'id': n['id'] + '-octave', 'pitch': n['pitch'] + 12} for n in source]
    one, two = recommend_musical_settings(source), recommend_musical_settings(doubled)
    assert one['tempo_candidates'] == two['tempo_candidates']
    assert one['key_candidates'] == two['key_candidates']
    assert one['evidence']['pitch_class_weights'] == two['evidence']['pitch_class_weights']


def test_early_note_is_preserved_by_provisional_pickup_alignment():
    source = [event('early', 0, .04)] + rhythm_fixture(100, .25)
    result = recommend_musical_settings(source)
    settings = result['suggested_settings']
    origin = settings['first_downbeat_seconds'] - float(Fraction(settings['pickup_beats'])) * 60 / settings['tempo_bpm']
    assert origin <= min(n['start'] for n in source) + 1e-6
    assert settings['pickup_beats'] == '1/2'
    assert any('downbeat' in text for text in result['warnings'])


def test_empty_sparse_silent_event_input_does_not_invent_settings():
    for source in ([], [event(0, 0, 5)], [event(0, 0), event(1, .3)]):
        result = recommend_musical_settings(source)
        assert result['status'] == 'insufficient_evidence'
        assert 'tempo_bpm' not in result['suggested_settings']
        assert 'key_signature' not in result['suggested_settings']


def test_invalid_events_are_ignored_without_leaking_nonfinite_values():
    source = rhythm_fixture() + [event('nan', float('nan')), event('inf', 0, float('inf')),
                                {'pitch': 'bad'}, event('negative', -1)]
    result = recommend_musical_settings(source)
    assert result['evidence']['note_count'] == 130
    assert result['suggested_settings']['tempo_bpm'] == pytest.approx(101.37, abs=.03)


def test_irregular_onsets_do_not_certify_a_pulse():
    rng = random.Random(882)
    source = [event(i, rng.uniform(0, 160), pitch=60 + i % 12) for i in range(240)]
    result = recommend_musical_settings(source)
    assert 'tempo_bpm' not in result['suggested_settings']
    assert result['tempo_candidates']  # alternatives remain inspectable


def test_key_template_and_relative_key_are_explicit_estimates():
    # Durations follow a known C-major template, transposed to Eb. This fixture
    # tests template arithmetic and naming, not key recognition on real songs.
    profile = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88)
    source = [event(i, i * 8, weight, 60 + (i + 3) % 12) for i, weight in enumerate(profile)]
    result = recommend_musical_settings(source)
    first = result['key_candidates'][0]
    assert (first['tonic'], first['mode'], first['key_signature']) == ('Eb', 'major', -3)
    assert first['template_correlation'] == pytest.approx(1)
    assert any(row['tonic'] == 'C' and row['mode'] == 'minor' for row in result['key_candidates'])
    assert result['suggested_settings']['key_signature'] == -3
    assert 'key_signature' in result['suggested_settings']['estimated_fields']
    assert any('relative major/minor' in text for text in result['warnings'])


def test_minor_key_enharmonic_name_matches_signature():
    profile = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17)
    source = [event(i, i * 8, weight, 60 + (i + 3) % 12) for i, weight in enumerate(profile)]
    result = recommend_musical_settings(source)
    first = result['key_candidates'][0]
    assert (first['tonic'], first['mode'], first['key_signature']) == ('Eb', 'minor', -6)


def test_long_clip_uses_bounded_samples_and_preserves_grid_precision():
    source = rhythm_fixture(99.73, .12, 7000)
    result = recommend_musical_settings(source)
    assert result['evidence']['sampled_onset_count'] <= 768
    assert result['suggested_settings']['tempo_bpm'] == pytest.approx(99.73, abs=.002)
    assert result['tempo_candidates'][0]['median_grid_error_ms'] < 6


def test_grid_comparison_reports_absolute_and_normalized_errors():
    source = rhythm_fixture(100, .1)
    good = grid_fit(source, 100)
    wrong = grid_fit(source, 120, 0)
    assert good['median_grid_error_ms'] < wrong['median_grid_error_ms']
    assert good['median_error_fraction_of_grid'] < wrong['median_error_fraction_of_grid']
    assert good['grid_phase_seconds'] == pytest.approx(.1, abs=.005)


def test_sparse_attacks_across_a_long_recording_do_not_make_a_tempo():
    source = [event(i, i * 400, pitch=60 + i % 7) for i in range(8)]
    result = recommend_musical_settings(source)
    assert 'tempo_bpm' not in result['suggested_settings']
    assert result['tempo_candidates'] == []


def test_near_integer_tempo_is_readable_with_explicit_bounded_drift():
    source = rhythm_fixture(99.995, .12, 300)
    result = recommend_musical_settings(source)
    assert result['suggested_settings']['tempo_bpm'] == 100
    assert result['tempo_candidates'][0]['bpm'] == pytest.approx(99.995, abs=.002)
    rounding = result['evidence']['tempo_rounding']
    assert rounding['fine_fit_bpm'] != rounding['suggested_bpm']
    assert rounding['end_of_span_drift_ms'] < 25


def test_near_integer_rounding_does_not_accumulate_long_clip_drift():
    source = rhythm_fixture(99.97, .12, 7000)
    result = recommend_musical_settings(source)
    assert result['suggested_settings']['tempo_bpm'] == pytest.approx(99.97, abs=.002)
    assert result['evidence']['tempo_rounding'] is None
