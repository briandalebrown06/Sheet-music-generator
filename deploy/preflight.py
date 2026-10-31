"""Fail-closed deployment preflight and process health probes (no credentials)."""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import urllib.request
from urllib.parse import urlsplit


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--initialize',action='store_true')
    mode.add_argument('--api-health',action='store_true')
    mode.add_argument('--worker-health',action='store_true')
    args=parser.parse_args()
    from backend.app.config import Settings
    settings=Settings.from_env()
    if args.api_health:
        request=urllib.request.Request('http://127.0.0.1:8000/api/health',headers={'Host':urlsplit(settings.public_origin).netloc})
        with urllib.request.urlopen(request,timeout=4) as response:
            assert response.status==200 and json.load(response)['status']=='ok'
        return
    if args.worker_health:
        from backend.app.database import Database
        if not Database(settings.database_path).worker_status()['online']:
            raise RuntimeError('Background worker heartbeat is stale.')
        return
    if settings.app_mode!='hosted' or not settings.authentication_required:
        raise RuntimeError('Deployment initialization requires hosted mode and authentication.')
    if os.name!='posix':
        raise RuntimeError('Deployment requires POSIX local-disk file locks.')
    if sys.version_info[:2]!=(3,11):
        raise RuntimeError('Model environment requires Python 3.11.')
    settings.prepare()
    from backend.app.database import Database
    from backend.app.auth_store import AuthStore
    Database(settings.database_path,settings.max_job_attempts).initialize()
    AuthStore(settings.database_path).initialize()
    from backend.app.engine import capabilities
    from backend.app.pdf_export import pdf_capability
    from backend.app.separation import separation_capability,verified_weights
    required=capabilities(settings.ffmpeg_binary,settings.basic_pitch_runtime)
    if not all(required.values()) or not pdf_capability()['available'] or not separation_capability(settings)['available']:
        raise RuntimeError('Runtime dependencies or model weights are missing; install models before startup.')
    verified_weights(settings)
    spec=importlib.util.find_spec('basic_pitch')
    from pathlib import Path
    model=Path(next(iter(spec.submodule_search_locations)))/'saved_models/icassp_2022/nmp.onnx'
    if not model.is_file() or model.stat().st_size==0:
        raise RuntimeError('Bundled Basic Pitch ONNX checkpoint is missing.')
    with sqlite3.connect(settings.database_path) as conn:
        if conn.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise RuntimeError('Database integrity check failed; restore a verified backup.')
        accounts=conn.execute('SELECT COUNT(*) FROM auth_users WHERE disabled=0').fetchone()[0]
    if not accounts:
        raise RuntimeError('Provision an account with backend.app.admin before starting hosted services.')
    minimum_free = max(2 * settings.max_upload_bytes, min(settings.max_separation_storage_bytes, 16 * 1024**3))
    if shutil.disk_usage(settings.data_dir).free < minimum_free:
        raise RuntimeError(f'Artifact storage needs at least {minimum_free} free bytes for configured upload/analysis scratch. Free disk space before startup.')
    print('Preflight passed: authenticated configuration, database, decoder, installed models and engraver. No inference benchmark or container qualification is implied.')

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(f'Preflight failed: {exc}',file=sys.stderr)
        raise SystemExit(1)
