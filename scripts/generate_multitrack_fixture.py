#!/usr/bin/env python3
"""Generate original, CC0 procedural test audio with separately known sources.

These synthetic vowels, bass tones, struck chords and noise percussion are
explicit development fixtures, not real performers or substituted model output.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
import soundfile as sf

RATE = 44100
DURATION = 12.0


def generate(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    count = int(RATE * DURATION)
    sources = {name: np.zeros((count, 2), dtype=np.float64) for name in ("vocals", "bass", "other", "drums")}
    expected = {name: [] for name in sources}

    def add(name, signal, start, pan=0):
        left = round(start * RATE)
        signal = signal[:count-left]
        sources[name][left:left+len(signal), 0] += signal * np.sqrt((1-pan)/2)
        sources[name][left:left+len(signal), 1] += signal * np.sqrt((1+pan)/2)

    def note(name, pitch, start, end, amplitude, pan=0):
        t = np.arange(round((end-start)*RATE)) / RATE
        frequency = 440 * 2 ** ((pitch-69)/12)
        envelope = np.minimum(1, t/.025) * np.minimum(1, np.maximum(0, end-start-t)/.06)
        if name == "vocals":
            # Periodic glottal harmonics through broad vowel-like formants.
            instantaneous = frequency * 2 ** (.6*np.sin(2*np.pi*5.4*t)/12)
            phase = 2*np.pi*np.cumsum(instantaneous)/RATE
            signal = np.zeros_like(t)
            for harmonic in range(1, 24):
                hz = harmonic*frequency
                formant = .12 + 1.2*np.exp(-.5*((hz-700)/160)**2) + .85*np.exp(-.5*((hz-1200)/190)**2) + .5*np.exp(-.5*((hz-2500)/300)**2)
                signal += formant/harmonic * np.sin(harmonic*phase)
        elif name == "bass":
            signal = sum(weight*np.sin(2*np.pi*frequency*k*t) for k,weight in enumerate([1,.45,.2,.1],1))
            envelope *= .45+.55*np.exp(-3*t)
        else:
            signal = sum((1/k**1.3)*np.sin(2*np.pi*frequency*k*(1+.0001*k*k)*t)*np.exp(-k*t*1.8) for k in range(1,8))
        signal /= max(float(np.max(np.abs(signal))), 1e-8)
        add(name, amplitude * envelope * signal, start, pan)
        expected[name].append({"pitch":pitch,"start":start,"end":end})

    for i,pitch in enumerate([69,69,71,72,74,72,71,69]):
        note("vocals",pitch,.5+i*1.25,1.45+i*1.25,.24,0)
    for i,pitch in enumerate([36,31,33,29,36,31]):
        note("bass",pitch,.3+i*1.8,1.9+i*1.8,.25,-.1)
    for i,chord in enumerate([[60,64,67],[55,59,62],[57,60,64],[53,57,60],[60,64,67],[55,59,62]]):
        for pitch in chord:
            note("other",pitch,.35+i*1.8,1.65+i*1.8,.10,.35)
    rng=np.random.default_rng(72441)
    for i in range(22):
        start=.3+i*.5
        t=np.arange(int(.22*RATE))/RATE
        if i%2==0:
            phase=2*np.pi*(50*t+55*.035*(1-np.exp(-t/.035)))
            signal=.23*np.sin(phase)*np.exp(-t*22)
        else:
            noise=rng.standard_normal(len(t)); noise=np.r_[0,np.diff(noise)]
            signal=.075*noise*np.exp(-t*25)
        add("drums",signal,start,-.25 if i%2 else 0)
        hat=rng.standard_normal(int(.07*RATE)); hat=np.r_[0,np.diff(hat)]
        add("drums",.035*hat*np.exp(-np.arange(len(hat))/RATE*65),start+.25,.5)
    mixture=sum(sources.values())
    gain=min(1,.9/max(float(np.max(np.abs(mixture))),1e-8))
    files={}
    for name,signal in {**sources,"mixture":mixture}.items():
        destination=output/("DEVELOPMENT-MULTITRACK.wav" if name=="mixture" else f"reference-{name}.wav")
        sf.write(destination,signal*gain,RATE,subtype="PCM_16")
        files[name]={"filename":destination.name,"sha256":hashlib.sha256(destination.read_bytes()).hexdigest()}
    manifest={"purpose":"Clearly labeled generated evaluation input; references are never supplied to inference",
              "license":"CC0-1.0 for this original procedural audio", "sample_rate":RATE,"duration_seconds":DURATION,
              "generator_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),"random_seed_percussion_only":72441,
              "global_gain":gain,"files":files,"expected_notes":expected,
              "vocals":"Synthetic vowel-like excitation with ±0.6-semitone 5.4Hz vibrato; not human singing",
              "other":"Generated struck harmonic chords; category name does not imply an isolated real piano or guitar",
              "drums":"Seeded noise and swept sine percussion; no pitched-note reference or drum notation claim"}
    (output/"reference.json").write_text(json.dumps(manifest,indent=2)+"\n")
    return manifest


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=Path("evaluation/generated/multitrack"))
    args=parser.parse_args(); generate(args.output)
    print(args.output.resolve()/"DEVELOPMENT-MULTITRACK.wav")
