"""Native export diagnostics must not leak private score text into application logs."""
import logging
from types import SimpleNamespace
import pytest
from backend.app import pdf_export
from backend.app.config import Settings


def test_engraver_failure_logs_exit_without_private_diagnostic(tmp_path,monkeypatch,caplog):
    secret='EXPLICIT_PRIVATE_SCORE_TITLE_FIXTURE'
    monkeypatch.setattr(pdf_export,'pdf_capability',lambda:{'available':True})
    def failed_process(command,**options):
        options['stderr'].write(secret.encode());options['stderr'].flush()
        return SimpleNamespace(wait=lambda timeout:None,returncode=7)
    monkeypatch.setattr(pdf_export.subprocess,'Popen',failed_process)
    with caplog.at_level(logging.ERROR),pytest.raises(pdf_export.PdfExportError):
        pdf_export.engrave_pdf('<score-partwise/>',Settings(data_dir=tmp_path),1)
    assert 'exit 7' in caplog.text and secret not in caplog.text
    assert not list(tmp_path.iterdir()),'Failed native output and private diagnostics must be removed'


@pytest.mark.parametrize("failure_stage", ["source_write", "directory_creation"])
def test_full_audio_storage_returns_clear_failure_and_no_project(tmp_path,monkeypatch,failure_stage):
    import errno,io,wave
    from pathlib import Path
    from fastapi.testclient import TestClient
    from backend.app.main import create_app
    payload=io.BytesIO()
    with wave.open(payload,'wb') as file:
        file.setnchannels(1);file.setsampwidth(2);file.setframerate(22050);file.writeframes(b'\x00\x01'*22050)
    app=create_app(Settings(data_dir=tmp_path))
    original=Path.open
    def full_disk(path,mode='r',*args,**kwargs):
        if failure_stage=='source_write' and path.name=='source' and mode=='wb':
            raise OSError(errno.ENOSPC,'EXPLICIT PRIVATE PATH FIXTURE')
        return original(path,mode,*args,**kwargs)
    monkeypatch.setattr(Path,'open',full_disk)
    original_mkdir=Path.mkdir
    def full_directory(path,*args,**kwargs):
        if failure_stage=='directory_creation' and path.parent.name=='projects':
            raise OSError(errno.ENOSPC,'EXPLICIT PRIVATE DIRECTORY FIXTURE')
        return original_mkdir(path,*args,**kwargs)
    monkeypatch.setattr(Path,'mkdir',full_directory)
    with TestClient(app) as client:
        response=client.post('/api/projects',files={'file':('fixture.wav',payload.getvalue(),'audio/wav')})
        assert response.status_code==507 and 'storage is full' in response.json()['detail']
        assert 'PRIVATE PATH' not in response.text and 'PRIVATE DIRECTORY' not in response.text
        assert client.get('/api/projects').json()==[]
        assert not list((tmp_path/'projects').iterdir())
