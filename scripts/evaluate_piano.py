"""Reproducible score-only piano regressions; never substitutes audio inference.

An optional existing-project snapshot stays private in the ignored output tree.
Generated note fixtures test arrangement constraints, not transcription accuracy.
"""
from __future__ import annotations
import argparse
import copy
import json
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.app.config import Settings
from backend.app.piano import PianoSettings,arrange_piano,assess_playability
from backend.app.readability import prepare_readable_notes
from backend.app.score import ScoreSettings,build_score,score_to_musicxml,write_score_midi
from backend.app.pdf_export import engrave_pdf,validate_pdf


def fixture():
    notes=[]
    chords=[[48,60,64,67,72,76],[50,62,65,69,74,77],[43,55,59,62,67,71],[48,60,64,67,72,76]]
    for bar in range(8):
        for beat in (0,.75,1.5):
            start=bar*2+beat
            for i,pitch in enumerate(chords[bar%4]):
                nid=str(uuid.uuid5(uuid.NAMESPACE_URL,f'DEVELOPMENT-piano-{bar}-{beat}-{i}'))
                # Close releases straddle the original sixteenth-grid rounding
                # boundary, creating needless notation voices before cleanup.
                end=start+(.184 if i%2 else .191)
                notes.append({'id':nid,'pitch':pitch,'start':start+(i%3)*.003,'end':end,'velocity':75,
                    'part_id':'part-1','hand':'left' if i==0 else 'right','provenance':'detected',
                    'source_note_ids':[nid], 'original_timing':{'pitch':pitch,'start':start+(i%3)*.003,'end':end,'velocity':75},
                    'model_score':None,'score_timing':None})
    return notes


def inspect_exports(score,xml,midi):
    import pretty_midi
    from music21 import converter
    from fractions import Fraction
    import xml.etree.ElementTree as ET
    # music21 parses the XML, but stripTies() does not fully merge polyphonic
    # chord ties. Independently follow MusicXML voices/staves and exact durations.
    converter.parseData(xml)
    document=ET.fromstring(xml)
    cue_ids={p.get('id') for p in document.findall('part-list/score-part') if 'cue' in (p.findtext('part-name') or '').lower()}
    events=[]
    for part_index,part in enumerate(document.findall('part')):
        if part.get('id') in cue_ids:continue
        absolute=Fraction(0);divisions=1;active={}
        for measure in part.findall('measure'):
            cursor=Fraction(0);previous=Fraction(0);extent=Fraction(0)
            for item in measure:
                if item.tag=='attributes' and item.findtext('divisions'):
                    divisions=int(item.findtext('divisions'))
                elif item.tag in ('forward','backup'):
                    cursor+=(-1 if item.tag=='backup' else 1)*Fraction(int(item.findtext('duration')),divisions)
                    assert cursor>=0,'MusicXML voice backup exceeds the measure'
                elif item.tag=='note':
                    duration=Fraction(int(item.findtext('duration','0')),divisions)
                    onset=previous if item.find('chord') is not None else cursor
                    if item.find('chord') is None:previous=onset;cursor+=duration
                    extent=max(extent,cursor,onset+duration)
                    written=item.find('pitch')
                    if written is None:continue
                    pitch=(int(written.findtext('octave'))+1)*12+{'C':0,'D':2,'E':4,'F':5,'G':7,'A':9,'B':11}[written.findtext('step')]+int(written.findtext('alter','0'))
                    onset+=absolute;release=onset+duration
                    key=(part_index,item.findtext('staff','1'),item.findtext('voice','1'),pitch)
                    ties={tie.get('type') for tie in item.findall('tie')}
                    if 'stop' in ties:
                        assert key in active,'MusicXML tie stop has no start'
                        assert events[active[key]][2]==onset,'MusicXML tie has a timing gap'
                        events[active[key]][2]=release
                        if 'start' not in ties:del active[key]
                        continue
                    events.append([pitch,onset,release])
                    if 'start' in ties:
                        assert key not in active,'MusicXML starts a second tie before the first ends'
                        active[key]=len(events)-1
            absolute+=extent
        assert not active,'MusicXML has unfinished ties'
    beats=[(pitch,round(float(start)*60/score['tempo']['bpm'],4),round(float(end)*60/score['tempo']['bpm'],4)) for pitch,start,end in events]
    model=pretty_midi.PrettyMIDI(str(midi))
    events=[(n.pitch,round(n.start,4),round(n.end,4)) for p in model.instruments for n in p.notes]
    assert len(beats)==len(events),(len(beats),len(events))
    for a,b in zip(sorted(beats),sorted(events)):
        assert a[0]==b[0] and abs(a[1]-b[1])<.001 and abs(a[2]-b[2])<.001,(a,b)
    return {'musicxml_round_trip':True,'midi_matches_musicxml_pitches_onsets_durations':True,'note_events':len(events)}


def score_stats(score):
    events=[e for p in score['parts'] for s in p['staves'] for m in s['measures'] for v in m['voices'] for e in v['events']]
    return {'parts':len(score['parts']),'staves':sum(len(p['staves']) for p in score['parts']),
            'max_voices_per_staff':max(len(m['voices']) for p in score['parts'] for s in p['staves'] for m in s['measures']),
            'visible_rests':sum(e['kind']=='rest' and not e.get('hidden') for e in events),
            'tied_fragments':sum(bool(e.get('tie')) for e in events),
            'readability':score.get('readability')}


def evaluate(name,notes,musical,output,before_score=None,before_xml=None):
    started=time.monotonic()
    original=copy.deepcopy(notes)
    musical=ScoreSettings.model_validate(musical)
    before_score=before_score or build_score(notes,musical,title='DEVELOPMENT piano regression — before',source_notes_revision=1)
    before_xml=before_xml or score_to_musicxml(before_score)
    piano=PianoSettings()
    repaired=arrange_piano(notes,piano,musical)
    readable_settings=musical.model_copy(update={'piano_split':True,'readability_mode':'accompaniment'})
    readable=prepare_readable_notes(repaired['notes'],readable_settings)
    after_notes=readable['notes']
    after_score=build_score(after_notes,readable_settings,title=('DEVELOPMENT piano regression' if name=='development' else before_score['title'])+' — accompaniment',source_notes_revision=1)
    after_report=assess_playability(after_notes,piano,readable_settings)
    after_score['playability']=after_report
    assert notes==original,'Original transcription changed'
    report={'fixture':name=='development','human_validation':False,'original_unchanged':True,
            'input_notes':len(notes),'output_notes':len(after_notes),'before':repaired['before_report'],'after':after_report,
            'changes':repaired['changes'],'readability_changes':readable['changes'],'exports':{}}
    for phase,score,xml in [('before',before_score,before_xml),('after',after_score,score_to_musicxml(after_score))]:
        stem=output/f'{name}-{phase}'
        stem.with_suffix('.musicxml').write_text(xml)
        stem.with_suffix('.json').write_text(json.dumps(score,indent=2)+'\n')
        write_score_midi(score,stem.with_suffix('.mid'))
        verification=inspect_exports(score,xml,stem.with_suffix('.mid'))
        pdf=engrave_pdf(xml,Settings(data_dir=output/'scratch'),1)
        stem.with_suffix('.pdf').write_bytes(pdf)
        report['exports'][phase]={**verification,'pages':validate_pdf(pdf),'notation':score_stats(score)}
    report['wall_seconds']=round(time.monotonic()-started,3)
    (output/f'{name}-report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'case':name,'notes_before':len(notes),'notes_after':len(after_notes),'before':report['before']['summary'],
          'after':after_report['summary'],'pages':{k:v['pages'] for k,v in report['exports'].items()},'seconds':report['wall_seconds']}),flush=True)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'output/pdf/piano-review')
    parser.add_argument('--project-snapshot',type=Path)
    parser.add_argument('--only-project',action='store_true')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    if not args.only_project:
        evaluate('development',fixture(),{'tempo_bpm':120,'quantization':'sixteenth','piano_split':True},args.output)
    if args.project_snapshot:
        snapshot=json.loads(args.project_snapshot.read_text())
        before_xml=(args.project_snapshot.parent/'existing-project-before.musicxml').read_text()
        evaluate('existing-project',json.loads(snapshot['project']['notes_json']),snapshot['score']['settings'],args.output,snapshot['score'],before_xml)


if __name__=='__main__':main()
