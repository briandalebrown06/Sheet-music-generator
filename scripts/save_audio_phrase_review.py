"""Apply an explicitly reviewed phrase grid to saved local comparison editions.

Dry-run by default. Publication uses both saved revision fences, preserves the
existing project, and keeps source timing and the original raw MIDI untouched.
"""
from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.config import Settings
from backend.app.database import Database
from backend.app.engine import atomic_json
from backend.app.performance import write_performance_midi
from backend.app.piano import PianoSettings, assess_playability
from backend.app.score import ScoreSettings, build_score, score_to_musicxml, write_score_midi
from scripts.audio_hand_repair import repair_audio_local
from scripts.audio_phrase_quantization import quantize_reviewed_phrases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    settings = Settings.from_env()
    if settings.app_mode != 'local':
        raise ValueError('This editorial command is only for the local app.')
    db = Database(settings.database_path, settings=settings)
    root = Path('output/audio-comparison/corrected')
    evidence = json.loads((root/'reviewed-reattacks.private.json').read_text())
    instrumental = db.get_project('728a7bf3-b972-4001-9ede-d351c5789707')
    for label in ('singer', 'solo'):
        path = root/(label+'-candidate.private.json')
        candidate = json.loads(path.read_text())
        project = db.get_project(candidate['id'])
        state = db.get_score(project['id'])
        if project['owner_id'] != instrumental['owner_id']:
            raise ValueError('Review sources must have one owner.')
        if (project['notes'] != candidate['notes'] or state['score'] != candidate['score']
                or state['revision'] != candidate['revision']):
            raise ValueError('The saved comparison changed; review it before applying evidence.')
        if candidate.get('phrase_review'):
            print(label, 'already saved', candidate['revision'], flush=True)
            continue
        musical = ScoreSettings.model_validate(state['score']['settings'])
        piano = PianoSettings.model_validate(project['arrangement']['settings'])
        phrase = quantize_reviewed_phrases(project['notes'], instrumental['notes'],
                                         evidence['editions'][label]['candidates'], musical)
        protected = set()
        for note in phrase['notes']:
            if note.get('phrase_quantization'):
                protected.add(note['id'])
                note['audio_review'] = {'kind': 'reviewed_phrase_grid',
                    'previous_review': note.get('audio_review'),
                    'method': phrase['method'], 'not_calibrated_accuracy': True}
            if note.get('audio_review') or note.get('role') in ('melody', 'vocal_cue'):
                protected.add(note['id'])
        prior = {n['id']: n for n in phrase['notes']}
        repair = repair_audio_local(phrase['notes'], musical, piano)
        notes = repair['notes']
        for note in notes:
            if note['id'] in protected:
                assert all(note.get(k) == prior[note['id']].get(k)
                           for k in ('pitch', 'start', 'end', 'velocity', 'original_timing'))
        original = {n['id']: n for n in project['notes']}
        assert original.keys() == {n['id'] for n in notes}
        assert all(n.get('original_timing') == original[n['id']].get('original_timing') for n in notes)
        score = build_score(notes, musical, title=project['title'],
                            clip_duration_seconds=project['clip_duration_seconds'],
                            source_notes_revision=project['notes_revision']+1)
        score['playability'] = assess_playability(notes, piano, musical)
        score['edition_mode'] = state['score']['edition_mode']
        score['edition_label'] = state['score']['edition_label']
        summary = score['playability']['summary']
        if summary['error_count'] or score['playability']['density']['over_target_frames']:
            raise ValueError(f'{label}: unresolved hand constraints: {summary}')
        xml = score_to_musicxml(score)
        # Inspect the written MIDI, not just the retained source-note IDs.
        import pretty_midi
        check_path = root/(label+'-phrase-check.mid')
        write_score_midi(score, check_path)
        midi = pretty_midi.PrettyMIDI(str(check_path))
        attacks = sorted(n.start for part in midi.instruments for n in part.notes
                         if n.pitch == 70 and n.start < 2.34)
        assert len(attacks) == 14 and len({round(t, 5) for t in attacks}) == 14, attacks
        review = {k: v for k, v in phrase.items() if k != 'notes'}
        review['hand_repairs'] = repair['changes']
        review['summary'] = summary
        review['opening_midi_attacks_seconds_from_score_origin'] = attacks
        atomic_json(root/(label+'-phrase-final.private.json'), review)
        print(label, 'timing edits', len(phrase['changes']), 'hand edits', len(repair['changes']),
              'unresolved chains', len(phrase['unresolved']), 'summary', summary, flush=True)
        if not args.publish:
            continue
        edit_id = str(uuid.uuid4())
        directory = settings.data_dir/'projects'/project['id']/'edits'/edit_id
        directory.mkdir(parents=True, mode=0o700)
        try:
            performance = [n for n in notes if n.get('role') != 'vocal_cue'] if project.get('output_mode') == 'accompaniment' else notes
            write_performance_midi(performance, directory/'score.mid')
            atomic_json(directory/'notes.json', {'schema_version': 2,
                'revision': project['notes_revision']+1,
                'time_basis': 'seconds relative to selected clip; reviewed phrase grid is editorial; original_timing is unchanged',
                'notes': notes})
            atomic_json(directory/'phrase-review.json', review)
            db.check_storage_usage(project['owner_id'])
            saved = db.save_notes(project['id'], project['notes_revision'], notes, edit_id,
                score_bundle={'score': score, 'musicxml': xml}, score_revision=state['revision'], return_project=True)
            if not saved:
                raise ValueError('A newer review was saved; no changes published.')
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        if not (root/(label+'-candidate-r1.private.json')).exists():
            atomic_json(root/(label+'-candidate-r1.private.json'), candidate)
        candidate.update(notes=notes, score=score, revision=state['revision']+1, phrase_review=review)
        candidate['changes'] += phrase['changes'] + repair['changes']
        atomic_json(path, candidate)
        assert db.get_project(project['id'])['notes'] == notes
        assert db.get_score(project['id'])['score'] == score
        print('Saved', label, project['id'], 'revision', candidate['revision'], flush=True)


if __name__ == '__main__':
    main()
