#!/usr/bin/env python3
"""Run actual separation/part transcription on reproducible original multitrack audio."""
from __future__ import annotations
import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import numpy as np
import soundfile as sf
from backend.app.config import Settings
from backend.app.engine import atomic_json
from scripts.generate_multitrack_fixture import generate
from scripts.evaluate_transcription import metrics


def si_sdr(reference,estimate):
    reference=np.asarray(reference,dtype=np.float64).ravel();estimate=np.asarray(estimate,dtype=np.float64).ravel()
    reference=reference-reference.mean();estimate=estimate-estimate.mean()
    energy=float(reference@reference)
    if energy<1e-12 or float(estimate@estimate)<1e-12:return None
    target=reference*float((estimate@reference)/energy)
    residual=estimate-target
    return float(10*np.log10(max(float(target@target),1e-12)/max(float(residual@residual),1e-12)))


def separation_metrics(references,estimates,mixture):
    categories=list(references)
    basis=np.stack([references[c].ravel() for c in categories],axis=1).astype(np.float64)
    energies=np.sum(basis*basis,axis=0)
    rows={}
    for i,category in enumerate(categories):
        estimate=estimates[category].ravel().astype(np.float64)
        coefficients=np.linalg.lstsq(basis,estimate,rcond=None)[0]
        component_energies=coefficients**2*energies
        target=float(component_energies[i]);interference=float(component_energies.sum()-target)
        projection=basis@coefficients
        residual=estimate-projection
        separated=si_sdr(references[category],estimates[category]);baseline=si_sdr(references[category],mixture)
        rows[category]={"si_sdr_db":separated,"mixture_si_sdr_db":baseline,
            "si_sdr_improvement_db":separated-baseline if separated is not None and baseline is not None else None,
            "projection_target_to_interference_db":float(10*np.log10(max(target,1e-12)/max(interference,1e-12))),
            "projection_leakage_fraction":interference/max(target+interference,1e-12),
            "projection_coefficients":{c:float(v) for c,v in zip(categories,coefficients)},
            "unexplained_residual_energy_fraction":float(residual@residual)/max(float(estimate@estimate),1e-12)}
    return rows


def run_adapter_task(args):
    """One native inference runtime per process, as in production job attempts."""
    settings=Settings.from_env()
    args.output.mkdir(parents=True,exist_ok=True)
    if args.adapter_task=="separate":
        from backend.app.separation import SeparationEngine
        started=time.perf_counter()
        result=SeparationEngine(settings).separate(args.input_audio,args.output,
            stage_callback=lambda stage:print(stage,flush=True))
        payload={"result":result,"elapsed_seconds":time.perf_counter()-started}
    else:
        from backend.app.transcription_profiles import get_transcription_engine
        import librosa
        audio,rate=sf.read(args.input_audio,dtype="float64",always_2d=True)
        mono=librosa.resample(audio.mean(axis=1),orig_sr=rate,target_sr=22050)
        path=args.output/"normalized.wav"
        sf.write(path,mono,22050,subtype="PCM_16")
        engine=get_transcription_engine(settings,{"transcription_profile":args.category})
        started=time.perf_counter()
        result=engine.analyze(path,args.output,f"mixed-evaluation:{args.category}:{args.source_name}")
        payload={"result":result,"engine":engine.name,"elapsed_seconds":time.perf_counter()-started}
    atomic_json(args.result_file,payload)


def run_isolated_adapter(task,input_audio,output,result_file,settings,*,category=None,source_name=None):
    command=[sys.executable,"-X","faulthandler",str(Path(__file__).resolve()),
             "--adapter-task",task,"--input-audio",str(input_audio.resolve()),
             "--output",str(output.resolve()),"--result-file",str(result_file.resolve())]
    if category is not None:
        command += ["--category",category,"--source-name",source_name]
    timeout=settings.separation_timeout_seconds if task=="separate" else settings.job_timeout_seconds
    # A child may write its result and subsequently fail during native teardown.
    # Accept it only after normal process exit; never hide a crash with os._exit.
    subprocess.run(command,check=True,timeout=timeout)
    return json.loads(result_file.read_text())


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=ROOT/"evaluation/generated/mixed-evaluation")
    parser.add_argument("--summary",type=Path,default=ROOT/"evaluation/mixed-results.json")
    parser.add_argument("--report",type=Path,default=ROOT/"docs/MIXED_EVALUATION.md")
    parser.add_argument("--adapter-task",choices=("separate","transcribe"),help=argparse.SUPPRESS)
    parser.add_argument("--input-audio",type=Path,help=argparse.SUPPRESS)
    parser.add_argument("--result-file",type=Path,help=argparse.SUPPRESS)
    parser.add_argument("--category",choices=("vocals","bass","other"),help=argparse.SUPPRESS)
    parser.add_argument("--source-name",choices=("reference","separated"),help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.adapter_task:
        if args.input_audio is None or args.result_file is None:
            parser.error("Adapter tasks require --input-audio and --result-file")
        if args.adapter_task=="transcribe" and (args.category is None or args.source_name is None):
            parser.error("Transcription adapter tasks require --category and --source-name")
        run_adapter_task(args)
        return
    fixture=generate(args.output)
    mixture_path=args.output/fixture["files"]["mixture"]["filename"]
    settings=Settings.from_env()
    separation_task=run_isolated_adapter("separate",mixture_path,args.output/"separated",
        args.output/"separation-task.json",settings)
    separated=separation_task["result"]
    wall=separation_task["elapsed_seconds"]
    atomic_json(args.output/"separation-result.json",separated)
    refs={name:sf.read(args.output/fixture["files"][name]["filename"],dtype="float64",always_2d=True)[0] for name in ("vocals","bass","other","drums")}
    mix=sf.read(mixture_path,dtype="float64",always_2d=True)[0]
    outputs={stem["category"]:sf.read(args.output/"separated"/stem["filename"],dtype="float64",always_2d=True)[0] for stem in separated["stems"]}
    assert all(value.shape==mix.shape for value in outputs.values())
    leakage=separation_metrics(refs,outputs,mix)
    rows={}
    for category in ("vocals","bass","other"):
        rows[category]={}
        for source_name in ("reference","separated"):
            directory=args.output/f"transcription-{category}-{source_name}";directory.mkdir(exist_ok=True)
            # Same decoded rate/mono format as production, with no reference data
            # passed to either pitch adapter. Reference transcription is a control.
            path=(args.output/fixture["files"][category]["filename"] if source_name=="reference"
                  else args.output/"separated"/next(stem["filename"] for stem in separated["stems"] if stem["category"]==category))
            transcription_task=run_isolated_adapter("transcribe",path,directory,directory/"adapter-result.json",
                settings,category=category,source_name=source_name)
            result=transcription_task["result"]
            raw=json.loads((directory/"raw-note-events.json").read_text())["note_events"]
            raw_notes=[{"start":e[0],"end":e[1],"pitch":e[2]} for e in raw]
            rows[category][source_name]={"engine":transcription_task["engine"],"elapsed_seconds":transcription_task["elapsed_seconds"],
                "cleaned":metrics(fixture["expected_notes"][category],result["notes"],.1),
                "raw":metrics(fixture["expected_notes"][category],raw_notes,.1),
                "predicted_notes":result["notes"],"manifest":result["manifest"]}
            print(category,source_name,rows[category][source_name]["cleaned"],flush=True)
    report={"run_at_utc":datetime.now(timezone.utc).isoformat(),"platform":platform.platform(),
            "python":platform.python_version(),"fixture":fixture,"separation_wall_seconds":wall,
            "separation_manifest":separated["manifest"],"aligned_frames":len(mix),
            "separation_metrics":leakage,"transcription":rows,"onset_tolerance_seconds":.1,
            "generator_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "execution":"Each separation/transcription adapter runs in a fresh checked subprocess; nonzero exits and timeouts fail evaluation before report publication."}
    atomic_json(args.summary,report)
    fmt=lambda x:"—" if x is None else f"{x:.3f}"
    lines=["# Mixed-recording evaluation", "", f"Actual run: {report['run_at_utc']}. Original procedural CC0 audio; no commercial recordings or injected detections.","",
           "## Method", "", "The 12-second stereo fixture combines separately known synthetic vowel-like vibrato, bass, struck chords and seeded-noise percussion. Production Open-Unmix UMX-HQ separates the mixture without seeing the references. Its 10-second windows with 25% overlap are assembled on original sample positions. All four outputs have exactly 529,200 stereo frames at 44,100 Hz; no alignment search or delay correction is used in these measurements.","",
           "SI-SDR compares each estimate with its known target up to one scalar gain; higher is better. Improvement is relative to the unseparated mixture. A separate least-squares projection onto all four known sources estimates a leakage proxy: energy attributable to non-target source projections divided by summed projection energy. Sources are not orthogonal and nonlinear separation artifacts remain in the residual, so this is not a calibrated isolation or accuracy percentage. The unexplained residual fraction reports estimate energy not captured by that linear projection. Reference waveform shapes, not only note pitches, enter these metrics.","",
           "| Category | SI-SDR dB | Improvement dB | Projection target/interference dB | Leakage proxy (fraction) | Unexplained residual (fraction) |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for category,m in leakage.items():
        lines.append(f"| {category} | {fmt(m['si_sdr_db'])} | {fmt(m['si_sdr_improvement_db'])} | {fmt(m['projection_target_to_interference_db'])} | {m['projection_leakage_fraction']:.6f} | {m['unexplained_residual_energy_fraction']:.6f} |")
    lines += ["", "## Actual detected notes", "", "Exact MIDI pitch and onset within 100 ms define a one-to-one match. Precision, recall and F1 count notes; missed and extra notes remain errors. Onset/offset MAE uses only matched notes. Offset is not a matching criterion. The comparison with transcription of each clean reference helps separate pitch-estimator limitations from separation damage; references never affect production inference. Timing remains unquantized performance seconds.","",
              "| Part / input | Expected / predicted / matched | Precision | Recall | F1 | Onset MAE ms | Offset MAE ms | Pitch MAE semitones (150 ms pairs) |", "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for category,inputs in rows.items():
        for name,row in inputs.items():
            m=row["cleaned"]
            lines.append(f"| {category} / {name} | {m['expected_notes']} / {m['predicted_notes']} / {m['matched_exact_pitch_and_onset']} | {fmt(m['precision'])} | {fmt(m['recall'])} | {fmt(m['f1'])} | {fmt(m['onset_mae_seconds']*1000 if m['onset_mae_seconds'] is not None else None)} | {fmt(m['offset_mae_seconds']*1000 if m['offset_mae_seconds'] is not None else None)} | {fmt(m['pitch_mae_semitones_within_150ms'])} |")
    lines += ["", "## Limits and reproducibility", "", f"Measured separation wall time including weight verification/loading: {wall:.3f} seconds on {report['platform']}; this is a measurement, not a user-facing completion estimate.","",
              "Vocal and bass parts use one-pitch-at-a-time pYIN, with preserved raw F0/voicing frames and separate vibrato/fragment cleanup. Other uses Basic Pitch and is experimental. There is no percussion transcription and no generated accompaniment. A vocal stem can contain several singers; one estimated line does not recover every harmony. Synthetic timbres and a single short mixture do not establish quality on real vocals, physical instruments, mastered songs or long recordings. Leakage, harmonic errors, missing notes and timing shifts need review.","",
              "Every engine version, checksum, inference setting, original note and measured result is retained in `evaluation/mixed-results.json` and the ignored generated artifact folder. All sources have the same sample timeline. Each adapter runs in a fresh subprocess, as production jobs do; the evaluator requires a successful process exit before accepting its output. This avoids sharing native separation and transcription runtime lifetimes, and propagates crashes or timeouts instead of publishing a successful report. Rerun:","", "```sh", ".venv/bin/python backend/install_separation.py", ".venv/bin/python scripts/evaluate_mixed.py", "```", ""]
    args.report.write_text("\n".join(lines));print(f"Saved {args.summary} and {args.report}",flush=True)

if __name__=="__main__":main()
