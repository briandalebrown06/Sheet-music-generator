"""Historical 300-second configured-limit audit; use audit_hour_support.py for current defaults."""
from __future__ import annotations
import argparse,json,os,sys,tempfile,time,wave
from dataclasses import replace
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import numpy as np
import soundfile as sf
from fastapi.testclient import TestClient
from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.worker import Worker
from scripts.generate_multitrack_fixture import generate


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    output=args.output or Path(tempfile.mkdtemp(prefix='scorecraft-long-audit-'))
    output.mkdir(parents=True,exist_ok=True)
    settings=replace(Settings.from_env(),data_dir=output/'data',app_mode='local',auth_enabled=False,max_duration_seconds=300)
    worker=Worker(settings)
    results=[]
    rate=22050
    t=np.arange(rate*301)/rate
    local=t%.75
    pitches=np.array([60,64,67,62])[np.floor(t/.75).astype(int)%4]
    hz=440*2**((pitches-69)/12)
    envelope=np.clip(np.minimum(local/.02,(.65-local)/.04),0,1)
    audio=.12*envelope*(np.sin(2*np.pi*hz*t)+.35*np.sin(4*np.pi*hz*t))
    valid=output/'DEVELOPMENT-300-second-performance.wav'
    over=output/'DEVELOPMENT-301-second-over-limit.wav'
    sf.write(valid,audio[:300*rate],rate,subtype='PCM_16')
    sf.write(over,audio,rate,subtype='PCM_16')
    fixture=generate(output/'multitrack')
    samples,sr=sf.read(output/'multitrack'/fixture['files']['mixture']['filename'],always_2d=True)
    mixed=output/'DEVELOPMENT-90-second-mixture.wav'
    sf.write(mixed,np.tile(samples,(8,1))[:90*sr],sr,subtype='PCM_16')
    with TestClient(create_app(settings)) as client:
        for name,path,mode,start,end,expected in [
            ('maximum-allowed-solo',valid,'solo',.25,299.75,'completed'),
            ('over-limit-rejected-despite-short-selection',over,'solo',0,1,'failed'),
            ('multi-window-mixed',mixed,'mixed',.25,89.75,'completed')]:
            with path.open('rb') as stream:
                response=client.post('/api/projects',data={'title':'DEVELOPMENT AUDIT '+name,'input_mode':mode,
                    'clip_start_seconds':start,'clip_end_seconds':end},files={'file':(path.name,stream,'audio/wav')})
            assert response.status_code==201,response.text
            identifier=response.json()['id']
            began=time.monotonic();assert worker.run_once();wall=time.monotonic()-began
            project=client.get(f'/api/projects/{identifier}').json()
            assert project['status']==expected,project['jobs']
            row={'case':name,'status':project['status'],'worker_wall_seconds':wall,
                 'original_seconds':project['duration_seconds'],'clip_seconds':project['clip_duration_seconds'],
                 'note_count':project['note_count'],'error':project['jobs'][0]['error']}
            if mode=='mixed' and expected=='completed':
                stems=client.get(f'/api/projects/{identifier}/stems').json()['stems']
                row['stems']=[{'category':s['category'],'duration':s['duration_seconds'],'original_offset':s['source_origin_seconds']} for s in stems]
                assert len(stems)==4 and all(abs(s['duration_seconds']-89.5)<1e-8 and s['source_origin_seconds']==.25 for s in stems)
            if expected=='failed':assert '301' not in str(project.get('notes')) and '300-second limit' in row['error']
            if mode=='solo' and expected=='completed':
                assert project['notes'] and max(n['end'] for n in project['notes'])<=299.5
                assert client.get(f'/api/projects/{identifier}/exports/raw-midi').status_code==200
            results.append(row);print(json.dumps(row),flush=True)
    report={'fixture':'Original procedural test audio; references never injected into inference',
            'platform':sys.platform,'python':sys.version.split()[0],'results':results}
    (output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print('Saved',output/'report.json',flush=True)

if __name__=='__main__':main()
