"""Verify saved local editions and download their real revisioned exports.

This is an integration check, not a transcription-accuracy or pianist test.
Use an ignored output directory because project snapshots contain private notes.
"""
import argparse
import json
import time
import urllib.request
from pathlib import Path
from email.message import Message
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.evaluate_piano import inspect_exports, score_stats


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--base',default='http://127.0.0.1:8000/api')
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    def get(path):
        with urllib.request.urlopen(args.base+path,timeout=180) as r:
            return r.read(),{key.lower(): value for key, value in r.headers.items()}
    def data(path):return json.loads(get(path)[0])
    source=data('/projects/'+args.source)
    source_snapshot=json.dumps(source,sort_keys=True)
    entries=data('/projects/'+args.source+'/piano-editions')['editions']
    by_mode={}
    for entry in entries:by_mode.setdefault(entry['edition_mode'],entry)
    assert set(by_mode)=={'faithful','accompaniment','solo_piano'},'Create all three editions first.'
    report={'source_project_id':args.source,'pianist_review':False,'listening_review':False,'editions':{}}
    for mode,entry in by_mode.items():
        base='/projects/'+entry['id'];project=data(base);state=data(base+'/score');score=state['score']
        (args.output/f'{mode}-project.private.json').write_text(json.dumps(project,indent=2))
        (args.output/f'{mode}-score.private.json').write_text(json.dumps(state,indent=2))
        exports={}
        for format in ('musicxml','midi','pdf'):
            content,headers=get(base+f'/score/exports/{format}?revision={state["revision"]}')
            header=Message();header['Content-Disposition']=headers['content-disposition']
            name=header.get_filename();assert name and Path(name).name==name
            target=args.output/name;target.write_bytes(content)
            assert int(headers['x-score-revision'])==state['revision']
            exports[format]={'path':str(target.resolve()),'bytes':len(content)}
        agreement=inspect_exports(score,Path(exports['musicxml']['path']).read_text(),Path(exports['midi']['path']))
        with urllib.request.urlopen(args.base+base,timeout=30) as response:
            assert json.load(response)['notes']==project['notes']
        if mode=='faithful':
            fields=('id','pitch','start','end','velocity')
            assert [[n.get(k) for k in fields] for n in project['notes']]==[[n.get(k) for k in fields] for n in source['notes']]
        if mode=='solo_piano':
            vocal=data('/projects/'+project['arrangement']['melody_source_project_id'])
            originals={n['id']:n for n in vocal['notes']}
            shift=project['arrangement'].get('melody_octave_shift',0)
            for note in project['notes']:
                if note.get('edition_source')=='vocal':
                    raw=originals[note['source_note_id']]
                    assert note['pitch']==raw['pitch']+shift
                    assert note['original_timing']==raw['original_timing']
        for part in score['parts']:
            for staff in part['staves']:
                for measure in staff['measures']:
                    from backend.app.score import fraction
                    for voice in measure['voices']:
                        assert sum(fraction(e['duration']) for e in voice['events'])==fraction(measure['duration'])
        report['editions'][mode]={'id':entry['id'],'revision':state['revision'],'notes':len(project['notes']),
            'vocal_notes':sum(n.get('edition_source')=='vocal' for n in project['notes']),
            'playability':score['playability']['summary'],'issues':score['playability']['issues'],
            'score_statistics':score_stats(score),'settings':score['settings'],
            'exports':exports,'verification':agreement,'reopened':True,
            'readability_changes':len(project['arrangement']['readability_changes'])}
        (args.output/'verification.private.json').write_text(json.dumps(report,indent=2))
        print(json.dumps({'mode':mode,'id':entry['id'],'notes':len(project['notes']),'errors':score['playability']['summary']['error_count'],'warnings':score['playability']['summary']['warning_count'],'xml_midi_agree':True}),flush=True)
    assert json.dumps(data('/projects/'+args.source),sort_keys=True)==source_snapshot
    report['source_unchanged']=True
    (args.output/'verification.private.json').write_text(json.dumps(report,indent=2))

if __name__=='__main__':main()
