"""Kill/restart/cancel actual workers on generated audio, never existing projects."""
from __future__ import annotations
import argparse,json,os,signal,subprocess,sys,tempfile,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dataclasses import replace
from fastapi.testclient import TestClient
from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.database import Database


def until(fn,label,timeout=45):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        result=fn()
        if result:return result
        time.sleep(.03)
    raise AssertionError('Timed out: '+label)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('generated_mixture',type=Path)
    args=parser.parse_args()
    output=Path(tempfile.mkdtemp(prefix='scorecraft-recovery-audit-'))
    settings=replace(Settings.from_env(),data_dir=output/'data',app_mode='local',auth_enabled=False)
    env={**os.environ,'DATA_DIR':str(settings.data_dir),'APP_MODE':'local','AUTH_ENABLED':'false'}
    command=[sys.executable,'-c','from dataclasses import replace; from backend.app.config import Settings; from backend.app.worker import Worker; Worker(replace(Settings.from_env(),lease_seconds=2,worker_poll_seconds=.05)).run()']
    processes=[]
    with (output/'worker.log').open('wb') as log,TestClient(create_app(settings)) as client:
        def start():
            p=subprocess.Popen(command,cwd=ROOT,env=env,stdout=log,stderr=log);processes.append(p);return p
        def upload(label):
            with args.generated_mixture.open('rb') as stream:
                r=client.post('/api/projects',data={'input_mode':'mixed','title':'DEVELOPMENT '+label,'clip_end_seconds':'90'},files={'file':('DEVELOPMENT-mixture.wav',stream,'audio/wav')})
            assert r.status_code==201,r.text
            return r.json()['id']
        def state(identifier):return client.get('/api/projects/'+identifier).json()
        def inference(identifier):
            p=state(identifier)
            return p if p['status']=='processing' and p['jobs'][0]['stage'].startswith('Separating audio segment') else None
        try:
            first=upload('worker crash recovery');old=start()
            active=until(lambda:inference(first),'real separation starts')
            attempt=active['jobs'][0]['id']
            os.kill(old.pid,signal.SIGKILL);old.wait(timeout=5)
            replacement=start()
            complete=until(lambda:(p if (p:=state(first))['status'] in {'completed','failed'} else None),'recovery result')
            assert complete['status']=='completed',complete['jobs']
            assert len(complete['jobs'])==2 and complete['jobs'][0]['status']=='completed'
            until(lambda:not(settings.data_dir/'projects'/first/attempt).exists(),'abandoned output cleanup')
            second=upload('cancel actual inference')
            active=until(lambda:inference(second),'second real separation starts')
            canceled_attempt=active['jobs'][0]['id']
            assert client.post(f'/api/projects/{second}/cancel').status_code==200
            until(lambda:not(settings.data_dir/'projects'/second/canceled_attempt).exists(),'canceled output cleanup')
            assert state(second)['status']=='canceled' and not state(second)['separation_available']
            # No imported injected adapter: both attempts use actual model weights.
            report={'worker_crash':'SIGKILL during actual separation','recovered_attempts':len(complete['jobs']),
                    'recovered_status':complete['status'],'abandoned_attempt_removed':True,
                    'cancel_during_real_inference':'canceled','canceled_output_removed':True}
            (output/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report),flush=True)
            print('Saved',output/'report.json',flush=True)
        finally:
            for p in processes:
                if p.poll() is None:p.terminate()
            for p in processes:
                try:p.wait(timeout=5)
                except subprocess.TimeoutExpired:p.kill();p.wait()

if __name__=='__main__':main()
