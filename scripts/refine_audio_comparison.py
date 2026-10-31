"""Local, evidence-led edition repair. Evidence is reviewed input, not a score of accuracy.

Restores explicitly selected source attacks and observed bass notes, preserving
source IDs and original timing. Does not generate harmony or silently rewrite
existing projects. --publish uses the app's atomic derived-project transaction.
"""
from __future__ import annotations
import argparse, copy, hashlib, json, shutil, sys, uuid
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.config import Settings
from backend.app.database import Database
from backend.app.engine import atomic_json
from backend.app.piano import PianoSettings, assess_playability, refine_piano_texture, arrange_piano, _locked
from backend.app.score import ScoreSettings, build_score, score_to_musicxml, write_score_midi
from backend.app.performance import write_performance_midi
from scripts.compare_piano_arrangements import compare_arrangements
from scripts.audio_hand_repair import repair_audio_local

VERSION='audio-evidence-repair-v1'


def restore_source_register(notes, source_notes, evidence):
    """Restore reviewed source octaves; never invent a substitute pitch class."""
    output=copy.deepcopy(notes)
    by_id=_indexed_notes(output,'current register notes')
    source=_indexed_notes(source_notes,'register source notes')
    changes=[];seen=set()
    for row in evidence:
        nid=row['id']
        if nid in seen or nid not in by_id or nid not in source:
            raise ValueError('Register evidence needs unique existing source IDs')
        seen.add(nid)
        note,raw=by_id[nid],source[nid]
        if note['pitch']!=row['current_pitch'] or raw['pitch']!=row['source_pitch']:
            raise ValueError('Register evidence is stale')
        if _locked(note) or note.get('fingering') is not None:
            raise ValueError('Do not change locked/manual register choices')
        if (raw['pitch']-note['pitch'])%12:
            raise ValueError('A register restoration must preserve pitch class')
        changes.append({'id':nid,'action':'restore_source_octave','before':note['pitch'],'after':raw['pitch'],
                        'reason':row.get('recommendation','Restore original detected register')})
        note['pitch']=raw['pitch'];note['provenance']='edited';note['locked']=False
        note['audio_review']={'kind':'source_register_restoration','previous_review':note.get('audio_review'),
                              'evidence':copy.deepcopy(row),'not_calibrated_accuracy':True}
    return output,changes


def _indexed_notes(notes, label):
    indexed = {}
    for note in notes:
        identifier = note.get('id')
        try:
            valid = isinstance(identifier, str) and str(uuid.UUID(identifier)) == identifier
        except (ValueError, AttributeError, TypeError):
            valid = False
        if not valid or identifier in indexed:
            raise ValueError(f'{label} needs unique canonical note IDs')
        indexed[identifier] = note
    return indexed


def _validate_candidate(row, source, label):
    if not isinstance(row, dict) or row.get('id') not in source:
        raise ValueError(f'{label} candidate ID is absent from its observed source')
    raw = source[row['id']]
    for field in ('pitch', 'start', 'end', 'velocity'):
        if field in row and row[field] != raw.get(field):
            raise ValueError(f'{label} evidence does not match its observed {field}')
    return raw


def restore_evidence(current, instrumental, bass, repeated, accepted_bass):
    """Apply only explicit source IDs; input lists stay immutable."""
    output=copy.deepcopy(current)
    by_id=_indexed_notes(output, 'Current notes')
    source=_indexed_notes(instrumental, 'Instrumental source')
    bass_source=_indexed_notes(bass, 'Bass source')
    for label, rows, observed in (('Repeated attack', repeated, source), ('Bass', accepted_bass, bass_source)):
        selected = set()
        for row in rows:
            _validate_candidate(row, observed, label)
            if row['id'] in selected:
                raise ValueError(f'{label} candidates must have unique IDs')
            selected.add(row['id'])
    for row in repeated:
        chain = row.get('chain_ids')
        if not isinstance(chain, list) or row['id'] not in chain or any(nid not in source for nid in chain):
            raise ValueError('Repeated-attack chain IDs must refer to the instrumental source')
        if any(neighbour.get('id') not in by_id or neighbour.get('id') not in source or neighbour.get('id') not in chain
               for neighbour in row.get('retained_neighbours', [])):
            raise ValueError('Retained neighbours must exist in both current notes and the source chain')
    changes=[]
    # Restore the retained neighbors too: a prior coarse-grid release must not
    # obscure an intervening reattack. Preserve existing octave arrangement.
    chains={nid for row in repeated for nid in row['chain_ids']}
    for nid in sorted(chains & by_id.keys()):
        n, raw=by_id[nid],source[nid]
        if _locked(n):
            continue
        edits={k:{'before':n.get(k),'after':raw.get(k)} for k in ('start','end','velocity') if n.get(k)!=raw.get(k)}
        n.update(start=raw['start'],end=raw['end'],velocity=raw.get('velocity'),role='riff',locked=False)
        if n.get('provenance') != 'manually_added': n['provenance'] = 'edited'
        n.pop('key_release_seconds',None)
        n['pedal_sustained']=False
        n['audio_review']={'kind':'source_reattack_chain','source_note_id':nid,'pitch_placement':'existing arrangement octave retained'}
        if edits: changes.append({'id':nid,'action':'restore_chain_timing','edits':edits})
    for row in repeated:
        nid=row['id']
        if nid in by_id: continue
        raw=source[nid]
        neighbours=[by_id[n['id']] for n in row['retained_neighbours'] if n['id'] in by_id]
        nearby=min(neighbours,key=lambda n:abs(n['start']-raw['start'])) if neighbours else None
        shift=nearby['pitch']-source[nearby['id']]['pitch'] if nearby else 0
        if shift%12: raise ValueError('Do not infer a chromatic transposition')
        if _locked(raw): shift = 0
        if not 0 <= raw['pitch']+shift <= 127: raise ValueError('Restored octave is outside the MIDI pitch range')
        n=copy.deepcopy(raw)
        if not _locked(raw):
            n.update(pitch=n['pitch']+shift,hand=nearby.get('hand') if nearby else ('right' if n['pitch']>=60 else 'left'),role='riff',locked=False,voice_id=None)
        n.update(source_note_id=nid,edition_source='instrumental',edition_role='accompaniment')
        if shift: n['provenance']='edited'
        n['audio_review']={'kind':'restored_source_reattack','source_note_id':nid,
            'flux_peak_seconds':row['nearest_flux_peak_seconds'],'flux_delta_ms':row['peak_delta_ms'],
            'arrangement_octave_shift':shift,'not_calibrated_accuracy':True}
        output.append(n);by_id[nid]=n
        changes.append({'id':nid,'action':'restore_source_reattack','octave_shift':shift})
    for row in accepted_bass:
        nid=row['id']
        if nid in by_id: raise ValueError('Bass identity already present')
        n=copy.deepcopy(bass_source[nid])
        if not _locked(n): n.update(role='bass',hand='left',locked=False,voice_id=None)
        n.update(edition_source='bass',edition_role='detected_bass',source_note_id=nid)
        n['audio_review']={'kind':'corroborated_bass_source','evidence':copy.deepcopy(row),'not_calibrated_accuracy':True}
        output.append(n);by_id[nid]=n
        changes.append({'id':nid,'action':'add_observed_bass','pitch':n['pitch'],'start':n['start'],'end':n['end']})
    # A low note of the 'other' stem remains detected material, but no longer
    # carries an assumed bass-instrument priority during an observed bass line.
    intervals=[(n['start'],n['end']) for n in output if n.get('edition_source')=='bass']
    for n in output:
        if (n.get('role')=='bass' and (n.get('arrangement_provenance') or {}).get('inferred_role')=='bass'
            and not _locked(n) and any(min(n['end'],b)-max(n['start'],a)>.04 for a,b in intervals)):
            n['role']='harmony'
            changes.append({'id':n['id'],'action':'prioritize_observed_bass','reason':'This other-stem event remains harmony; an observed separated bass now supplies the bass role.'})
    return sorted(output,key=lambda n:(n['start'],n['pitch'],n['id'])),changes


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',action='append',required=True,help='singer|solo=ID')
    p.add_argument('--instrumental',required=True);p.add_argument('--bass',required=True)
    p.add_argument('--repeated-evidence',type=Path,required=True);p.add_argument('--bass-evidence',type=Path,required=True)
    p.add_argument('--register-evidence',type=Path)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--publish',action='store_true')
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    settings=Settings.from_env()
    if settings.app_mode!='local': raise ValueError('This local editorial command is not a hosted endpoint')
    db=Database(settings.database_path,settings=settings)
    instrumental=db.get_project(args.instrumental);bass=db.get_project(args.bass)
    if any(not item or item['status']!='completed' or not isinstance(item.get('notes'),list) for item in (instrumental,bass)):
        raise ValueError('Completed instrumental and bass source transcriptions are required')
    if bass.get('transcription_profile')!='bass': raise ValueError('Bass evidence requires an observed bass transcription')
    repeats=json.loads(args.repeated_evidence.read_text())
    evidence=json.loads(args.bass_evidence.read_text())
    for item in args.source:
        label,project_id=item.split('=',1)
        if label not in {'singer','solo'}: raise ValueError('Edition label must be singer or solo')
        project=db.get_project(project_id);state=db.get_score(project_id)
        if not project or project['status']!='completed' or not state or state['source_notes_revision']!=project['notes_revision']:
            raise ValueError('A completed source with a current saved score is required')
        if any(x['owner_id']!=project['owner_id'] for x in [instrumental,bass]): raise ValueError('Sources must have one owner')
        if not instrumental.get('parent_project_id') or instrumental['parent_project_id']!=bass['parent_project_id']: raise ValueError('Sources must come from same song')
        if any(abs((x.get('source_origin_seconds') or 0)-(project.get('source_origin_seconds') or 0))>.001 for x in [instrumental,bass]): raise ValueError('Audio origins differ')
        if any(abs((x.get('clip_start_seconds') or 0)-(project.get('clip_start_seconds') or 0))>.001
               or x.get('clip_duration_seconds') is None or project.get('clip_duration_seconds') is None
               or abs(x['clip_duration_seconds']-project['clip_duration_seconds'])>.05 for x in (instrumental,bass)):
            raise ValueError('Evidence source clip bounds differ')
        if repeats['editions'][label]['project_id']!=project_id: raise ValueError('Evidence source mismatch')
        musical=ScoreSettings.model_validate(state['score']['settings'])
        musical.quantization='sixteenth'
        piano=PianoSettings.model_validate(project['arrangement']['settings'])
        notes,changes=restore_evidence(project['notes'],instrumental['notes'],bass['notes'],repeats['editions'][label]['candidates'],evidence['accepted'])
        for n in notes:
            if n.get('audio_review'):
                n['source_project_id']=bass['id'] if n.get('edition_source')=='bass' else instrumental['id']
        changes+=repeats['editions'][label].get('excluded_candidates',[])
        restored=copy.deepcopy(notes)
        protected={n['id']:n for n in notes if n.get('audio_review') or n.get('role') in {'melody','vocal_cue'} or _locked(n)}
        # Only implicated density/movement edits; no full-song fresh voicing.
        refined=refine_piano_texture(notes,piano,musical)
        notes=refined['notes'];changes+=refined['changes']
        for n in notes:
            if n['id'] in protected:
                assert all(n[k]==protected[n['id']][k] for k in ('pitch','start','end','velocity'))
        assert protected.keys()<={n['id'] for n in notes}
        # Apply reviewed register restorations after the older texture pass,
        # which predates audio evidence. The local repair below honors the
        # evidence marker and cannot undo these corrections for easier reach.
        if args.register_evidence:
            notes,register_changes=restore_source_register(notes,instrumental['notes'],[
                row for row in json.loads(args.register_evidence.read_text()) if row['edition']==label])
            changes+=register_changes
        for n in notes:
            if n.get('audio_review'):
                n['source_project_id']=bass['id'] if n.get('edition_source')=='bass' else instrumental['id']
        protected={n['id']:copy.deepcopy(n) for n in notes if n.get('audio_review') or n.get('role') in {'melody','vocal_cue'} or _locked(n)}
        hand_repair=repair_audio_local(notes,musical,piano)
        notes=hand_repair['notes'];changes+=hand_repair['changes']
        for n in notes:
            if n['id'] in protected:
                old=protected[n['id']]
                assert all(n[k]==old[k] for k in ('pitch','start','end','velocity')),('protected musical evidence changed',n['id'])
        assert protected.keys()<={n['id'] for n in notes}
        title="Weather map · "+("Singer accompaniment" if label=='singer' else "Solo piano · Melody +1 octave")+" · Audio comparison"
        score=build_score(notes,musical,title=title,clip_duration_seconds=project['clip_duration_seconds'],source_notes_revision=1)
        score['playability']=assess_playability(notes,piano,musical)
        score['edition_mode']=project['arrangement']['edition_mode']
        score['edition_label']=('Singer accompaniment' if label=='singer' else 'Solo piano · melody +1 octave')+' · audio comparison; listening review pending'
        xml=score_to_musicxml(score)
        result={'source_id':project_id,'source_notes_revision':project['notes_revision'],'source_score_revision':state['revision'],
                'settings':musical.model_dump(),'notes':notes,'score':score,'changes':changes,
                'source_restoration_count':len(repeats['editions'][label]['candidates']),'bass_added_count':len(evidence['accepted']),
                'refinement':refined['refinement'],'hand_repair':{k:v for k,v in hand_repair.items() if k not in {'notes','before','after'}},
                'comparison':compare_arrangements(project['notes'],notes,state['score'],score)}
        atomic_json(args.output/(label+'-candidate.private.json'),result)
        print(label,'notes',len(project['notes']),'->',len(notes),'checks',score['playability']['summary'],flush=True)
        if not args.publish: continue
        if score['playability']['summary']['error_count'] or not score['playability']['density']['target_met']:
            raise ValueError('Candidate still has hard hand conflicts or exceeds the selected density target; review it before publishing')
        fingerprint=hashlib.sha256(json.dumps({'version':VERSION,'source':project_id,'notes_rev':project['notes_revision'],
            'score_rev':state['revision'],'notes':notes,'settings':musical.model_dump(),'compiled_score':score,
            'evidence':[hashlib.sha256(x.read_bytes()).hexdigest() for x in [args.repeated_evidence,args.bass_evidence,args.register_evidence] if x]},sort_keys=True).encode()).hexdigest()
        identifier,result_id=str(uuid.uuid4()),str(uuid.uuid4())
        directory=settings.data_dir/'projects'/project_id
        storage=db.storage(project_id);old_output=directory/str(storage['result_job_id'])
        analysis=directory/str(storage['analysis_job_id'])/'normalized.wav'
        destination=settings.data_dir/'projects'/identifier;output=destination/result_id
        metadata={**copy.deepcopy(project['arrangement']),'source_project_id':project_id,'source_notes_revision':project['notes_revision'],
            'source_score_revision':state['revision'],'engine':VERSION,'generation':int(project['arrangement'].get('generation',1))+1,
            'method':'Explicit restoration of audio-corroborated attacks and bass; per-pitch chord dynamics; sixteenth-note grid',
            'changes':changes,'report':score['playability'],'audio_comparison':{'bass_project_id':bass['id'],
                'bass_notes_revision':bass['notes_revision'],'repeated_source_project_id':instrumental['id'],
                'repeated_source_notes_revision':instrumental['notes_revision'],'listening_review':False},
            'raw_midi_available':(old_output/'raw-performance.mid').is_file(),'analysis_available':analysis.is_file(),
            'detail':'Source-evidence editorial correction; full auditory fidelity and piano interpretation remain unverified.'}
        def prepare_files():
            # Called while create_arrangement holds a write fence, so sources
            # cannot change between validation, copying and publication.
            for source in (instrumental,bass):
                current=db.get_project(source['id'])
                if not current or current['notes_revision']!=source['notes_revision']: raise ValueError('Evidence source changed; rerun review')
            output.mkdir(parents=True,mode=0o700)
            shutil.copyfile(directory/'source',destination/'source')
            if analysis.is_file():shutil.copyfile(analysis,output/'normalized.wav')
            for name in ('raw-performance.mid','raw-note-events.json'):
                if (old_output/name).is_file():shutil.copyfile(old_output/name,output/name)
            write_performance_midi([n for n in notes if n.get('role')!='vocal_cue'],output/'score.mid')
            atomic_json(output/'notes.json',{'schema_version':2,'revision':1,'notes':notes})
            atomic_json(output/'audio-comparison.json',{'restoration':repeats['editions'][label],'bass_evidence':evidence,'changes':changes})
            atomic_json(output/'manifest.json',metadata)
        reserved=(directory/'source').stat().st_size+(analysis.stat().st_size if analysis.exists() else 0)+32*1024**2
        try:
            published=db.create_arrangement(project_id,identifier,result_id,project['notes_revision'],state['revision'],notes,score,xml,metadata,fingerprint,prepare_files,reserved)
        except BaseException:
            shutil.rmtree(destination,ignore_errors=True);raise
        if published['id']!=identifier:shutil.rmtree(destination,ignore_errors=True)
        if published['notes']!=notes or db.get_score(published['id'])['score']!=score:
            raise ValueError('The previously published comparison was edited; reopen it instead of relabeling this candidate as saved')
        result['id']=published['id'];result['revision']=db.get_score(published['id'])['revision']
        atomic_json(args.output/(label+'-candidate.private.json'),result)
        print('Saved',label,published['id'],flush=True)

if __name__=='__main__':main()
