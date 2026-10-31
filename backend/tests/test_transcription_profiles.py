"""Synthetic detector frames and real generated audio, never production fixtures."""
import json
import os
from types import SimpleNamespace

import numpy as np
import pytest

from backend.app.audio import AnalysisError
from backend.app.engine import BasicPitchEngine
from backend.app.transcription_profiles import PyinPartEngine, frame_events, get_transcription_engine


def test_profile_selection_preserves_solo_and_rejects_drums():
    settings=SimpleNamespace(basic_pitch_runtime="onnx")
    assert isinstance(get_transcription_engine(settings,{}),BasicPitchEngine)
    assert isinstance(get_transcription_engine(settings,{"transcription_profile":"other"}),BasicPitchEngine)
    assert get_transcription_engine(settings,{"transcription_profile":"vocals"}).name=="pyin-vocals"
    assert get_transcription_engine(settings,{"transcription_profile":"bass"}).name=="pyin-bass"
    with pytest.raises(AnalysisError,match="Drum notation"):
        get_transcription_engine(settings,{"transcription_profile":"drums"})


def test_vibrato_hysteresis_preserves_one_observed_note():
    t=np.arange(300)*.01
    pitch=69+.6*np.sin(2*np.pi*5.4*t)
    values=(pitch,np.ones(300,dtype=bool),np.ones(300),np.ones(300),.01,3)
    assert len(frame_events(*values))>10
    cleaned=frame_events(*values,hysteresis=.75,transition_seconds=.05,median_seconds=.07)
    assert len(cleaned)==1 and cleaned[0][2]==69


def test_silence_and_real_semitone_changes_are_not_filled_or_merged():
    pitch=np.r_[np.full(70,60.),np.full(30,61.),np.full(20,61.),np.full(80,61.)]
    voiced=np.ones(200,dtype=bool);voiced[100:120]=False
    events=frame_events(pitch,voiced,np.ones(200),np.ones(200),.01,2,hysteresis=.75,transition_seconds=.05,median_seconds=.07)
    assert [e[2] for e in events]==[60,61,61]
    assert events[1][1]<events[2][0]
    assert events[1][0]==pytest.approx(.695)


def test_deep_energy_reattack_retains_repeated_pitch():
    rms=np.ones(100);rms[40:50]=.1
    events=frame_events(np.full(100,60.),np.ones(100,dtype=bool),np.ones(100),rms,.01,1,hysteresis=.75,transition_seconds=.05)
    assert len(events)==2 and [e[2] for e in events]==[60,60]


def test_unvoiced_frames_never_create_notes():
    assert frame_events(np.full(100,np.nan),np.zeros(100,dtype=bool),np.zeros(100),np.zeros(100),.01,1)==[]


@pytest.mark.skipif(os.getenv("RUN_MODEL_TESTS")!="1",reason="Opt-in actual pYIN pitch estimation")
@pytest.mark.parametrize("profile,pitches",[("vocals",[69,71]),("bass",[36,31])])
def test_real_pyin_profile_preserves_actual_audio_and_raw_frames(tmp_path,profile,pitches):
    import soundfile as sf
    sr=22050; y=np.zeros(sr*3)
    for index,pitch in enumerate(pitches):
        left=.2+index*1.3;end=left+1
        t=np.arange(sr)/sr
        f=440*2**((pitch-69)/12)
        frequency=f*2**((.55*np.sin(2*np.pi*5*t) if profile=="vocals" else 0)/12)
        phase=2*np.pi*np.cumsum(np.broadcast_to(frequency,t.shape))/sr
        envelope=np.minimum(1,t/.02)*np.minimum(1,(1-t)/.04)
        y[round(left*sr):round(end*sr)]=.3*envelope*(np.sin(phase)+.3*np.sin(2*phase))
    audio=tmp_path/"DEVELOPMENT-PITCH-FIXTURE.wav";sf.write(audio,y,sr,subtype="PCM_16")
    before=audio.read_bytes()
    result=PyinPartEngine(profile).analyze(audio,tmp_path/"analysis","test-pyin")
    assert audio.read_bytes()==before
    assert set(pitches).issubset({n["pitch"] for n in result["notes"]})
    assert len({n["id"] for n in result["notes"]})==len(result["notes"])
    assert all(n["provenance"]=="detected" and n["source_note_ids"] and n["end"]<=3 for n in result["notes"])
    with np.load(tmp_path/"analysis/raw-model-output.npz") as raw:
        assert {"f0_hz","voiced_flag","voiced_probability","rms","frame_times"}==set(raw.files)
        assert np.any(raw["voiced_flag"])
    assert json.loads((tmp_path/"analysis/raw-note-events.json").read_text())["note_events"]
    assert result["manifest"]["model_weights"] is None
    assert result["manifest"]["result_kind"]=="transcription"


@pytest.mark.skipif(os.getenv("RUN_MODEL_TESTS")!="1",reason="Opt-in actual chunked pYIN")
def test_real_pyin_sustained_note_crosses_global_chunk_boundary(tmp_path):
    import soundfile as sf
    sr=22050;y=np.zeros(sr*32);t=np.arange(round(31.2*sr))/sr
    y[round(.2*sr):round(.2*sr)+len(t)]=.25*np.sin(2*np.pi*440*t)*np.minimum(1,t/.02)*np.minimum(1,(31.2-t)/.04)
    audio=tmp_path/"DEVELOPMENT-CHUNK-FIXTURE.wav";sf.write(audio,y,sr,subtype="PCM_16")
    stages=[]
    result=PyinPartEngine("vocals").analyze(audio,tmp_path/"analysis","chunk-test",stage_callback=stages.append)
    assert len([s for s in stages if s.startswith("Tracking vocals pitch:")])==2
    assert len(result["notes"])==1
    note=result["notes"][0]
    assert note["pitch"]==69 and note["start"]<.3 and 31.3<note["end"]<31.6
