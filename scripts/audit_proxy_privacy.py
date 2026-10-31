"""Exercise the real Caddy error logger using synthetic credentials on loopback."""
from __future__ import annotations
import argparse,json,os,subprocess,tempfile,time,urllib.error,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--caddy',type=Path,default=ROOT/'.runtime/audit-tools/caddy')
    args=parser.parse_args()
    marker='EXPLICIT_PRIVATE_TEST_SENTINEL_72441'
    with tempfile.TemporaryDirectory(prefix='scorecraft-proxy-audit-') as directory:
        path=Path(directory)
        config=(ROOT/'deploy/Caddyfile').read_text().replace('{$APP_DOMAIN}','http://127.0.0.1:5190').replace('api:8000','127.0.0.1:1')
        (path/'Caddyfile').write_text(config)
        env={**os.environ,'ACME_EMAIL':'fixture@example.invalid','XDG_DATA_HOME':str(path/'data'),'XDG_CONFIG_HOME':str(path/'config')}
        with (path/'runtime.log').open('wb') as log:
            process=subprocess.Popen([str(args.caddy.resolve()),'run','--config',str(path/'Caddyfile'),'--adapter','caddyfile'],env=env,stdout=log,stderr=log)
            try:
                deadline=time.monotonic()+8
                while True:
                    try:
                        request=urllib.request.Request('http://127.0.0.1:5190/api/'+marker+'?private='+marker,headers={
                            'Cookie':'fixture='+marker,'X-CSRF-Token':marker,'Authorization':'Bearer '+marker})
                        urllib.request.urlopen(request,timeout=2)
                        raise AssertionError('Dead upstream should return 502')
                    except urllib.error.HTTPError as exc:
                        assert exc.code==502
                        break
                    except urllib.error.URLError:
                        if time.monotonic()>deadline:raise
                        time.sleep(.05)
            finally:
                process.terminate();process.wait(timeout=5)
        logs=(path/'runtime.log').read_text()
        assert 'http.log.error' in logs,'An actual upstream error must have been logged'
        assert marker not in logs,'Private URL or credential fixture leaked into runtime logs'
        print(json.dumps({'caddy_error_status':502,'runtime_error_observed':True,'synthetic_private_url_and_headers_redacted':True}))

if __name__=='__main__':main()
