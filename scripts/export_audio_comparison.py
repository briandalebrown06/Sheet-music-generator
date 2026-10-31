"""Verify saved local audio-comparison editions and download their current exports."""
from __future__ import annotations
import hashlib,json,sys,urllib.request
from email.message import Message
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.evaluate_piano import inspect_exports

def main():
    root=Path('output/audio-comparison/corrected');out=Path('output/pdf/weather-map-audio-comparison');out.mkdir(parents=True,exist_ok=True)
    def request(path):
        with urllib.request.urlopen('http://127.0.0.1:8000/api'+path,timeout=240) as r:return r.read(),dict(r.headers)
    def get(path):return json.loads(request(path)[0])
    report={'listening_review':False,'editions':{}}
    for label in ('singer','solo'):
        candidate=json.loads((root/(label+'-candidate.private.json')).read_text());id=candidate['id']
        project=get(f'/projects/{id}');state=get(f'/projects/{id}/score')
        assert project['notes']==candidate['notes']
        assert state['score']==candidate['score']
        parent=get('/projects/'+candidate['source_id']);parent_score=get('/projects/'+candidate['source_id']+'/score')
        assert parent['notes_revision']==candidate['source_notes_revision'] and parent_score['revision']==candidate['source_score_revision']
        exports={}
        for format in ('musicxml','midi','pdf'):
            content,headers=request(f'/projects/{id}/score/exports/{format}?revision={state["revision"]}')
            hdr=Message();hdr['Content-Disposition']=next(v for k,v in headers.items() if k.lower()=='content-disposition');filename=hdr.get_filename()
            assert filename and Path(filename).name==filename
            path=out/filename;path.write_bytes(content)
            exports[format]={'path':str(path.resolve()),'sha256':hashlib.sha256(content).hexdigest(),'bytes':len(content)}
        agreement=inspect_exports(state['score'],Path(exports['musicxml']['path']).read_text(),Path(exports['midi']['path']))
        reference=get(f'/projects/{id}/playback/reference');assert reference['available'] and reference['title']=='Weather map'
        audio,headers=request(reference['audio_url'].removeprefix('/api'))
        original=Path('.data/projects/f406f795-456a-4dca-891a-2c47f384dd7a/source')
        assert hashlib.sha256(audio).hexdigest()==hashlib.sha256(original.read_bytes()).hexdigest()
        oldraw=request(f'/projects/{candidate["source_id"]}/exports/raw-midi')[0]
        newraw=request(f'/projects/{id}/exports/raw-midi')[0];assert oldraw==newraw
        report['editions'][label]={'id':id,'title':project['title'],'score_revision':state['revision'],'exports':exports,
            'verification':agreement,'reopened':True,'old_revision_unchanged':True,'reference_bytes_match_original':True,
            'raw_instrumental_midi_unchanged':True,'summary':state['score']['playability']['summary']}
        print(label,id,state['revision'],agreement,flush=True)
    (out/'verification.private.json').write_text(json.dumps(report,indent=2))
if __name__=='__main__':main()
