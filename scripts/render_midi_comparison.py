"""Render supplied MIDI with the app's local licensed samples for A/B listening.

No generative timing, pedal, accompaniment, or loudness mastering. A single gain
is shared across all MIDI files, so source-relative note dynamics stay comparable.
"""
from __future__ import annotations
import argparse,json,math,subprocess
from pathlib import Path
import numpy as np
import pretty_midi
import soundfile as sf


def render(midi_path, bank, sample_rate=22050, origin=0.,duration=None):
    if not isinstance(sample_rate, int) or isinstance(sample_rate, bool) or not 1 <= sample_rate <= 192000:
        raise ValueError('Use a positive bounded integer output sample rate')
    if not math.isfinite(origin): raise ValueError('Score origin must be finite')
    if not bank: raise ValueError('No local piano samples are available')
    for pitch, (data, rate) in bank.items():
        if (not isinstance(pitch, int) or not 0 <= pitch <= 127 or not math.isfinite(rate) or rate <= 0
                or not isinstance(data, np.ndarray) or data.ndim != 2 or data.shape[0] < 2 or data.shape[1] < 1
                or not np.isfinite(data).all()):
            raise ValueError('Each piano sample needs a MIDI root, finite PCM channels, and positive sample rate')
    midi=pretty_midi.PrettyMIDI(str(midi_path))
    length=duration if duration is not None else (midi.get_end_time()+origin+.25)
    if not math.isfinite(length) or not 0 < length <= 3601:
        raise ValueError('Render duration must be positive and no longer than 60 minutes plus a one-second tail')
    out=np.zeros((math.ceil(length*sample_rate),2),dtype=np.float32)
    roots=sorted(bank)
    for instrument in midi.instruments:
        if instrument.is_drum:raise ValueError('Pitched piano comparison cannot render percussion')
        if instrument.control_changes:raise ValueError('This bounded comparison renderer does not emulate MIDI controllers')
        for note in instrument.notes:
            root=min(roots,key=lambda r:(abs(r-note.pitch),r))
            data,rate=bank[root]
            raw_start=round((note.start+origin)*sample_rate)
            note_frames=round((note.end-note.start)*sample_rate)
            start=max(0,raw_start)
            length=min(raw_start+note_frames,len(out))-start
            if length<=0:continue
            step=rate/sample_rate*2**((note.pitch-root)/12)
            offset=start-raw_start
            length=min(length,max(0,math.floor((len(data)-1)/step)-offset+1))
            if length<=0:continue
            frames=np.arange(offset,offset+length)
            x=frames*step
            envelope=np.ones(length,dtype=np.float32)
            attack=min(round(.008*sample_rate),note_frames//3)
            release=min(round(.02*sample_rate),note_frames//3)
            # Cropping a leading pickup or the requested render duration must
            # not restart the sample/attack or insert a fake early note release.
            if attack>1:envelope*=np.clip(frames/(attack-1),0,1)
            if release>1:envelope*=np.clip((note_frames-1-frames)/(release-1),0,1)
            for channel in range(2):
                out[start:start+length,channel]+=np.interp(x,np.arange(len(data)),data[:,min(channel,data.shape[1]-1)]).astype(np.float32)*envelope*(note.velocity/127)
    return out


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--midi',action='append',required=True,help='label=path')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--origin',type=float,default=0)
    parser.add_argument('--duration',type=float,default=None)
    parser.add_argument('--ffmpeg',default='.runtime/bin/ffmpeg')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    roots={}
    pitches={'C':0,'Ds':3,'Fs':6,'A':9}
    for path in Path('frontend/public/samples/salamander').glob('*.mp3'):
        name=path.stem;pitch=(int(name[-1])+1)*12+pitches[name[:-1]]
        roots[pitch]=sf.read(path,dtype='float32',always_2d=True)
    rendered={}
    for value in args.midi:
        label,path=value.split('=',1)
        if Path(label).name!=label:raise ValueError('Use a plain output label')
        rendered[label]=render(Path(path),roots,origin=args.origin,duration=args.duration)
    peak=max(float(np.max(np.abs(value))) for value in rendered.values())
    gain=.90/max(peak,1e-8)
    for label,data in rendered.items():
        wav=args.output/(label+'.wav');mp3=wav.with_suffix('.mp3')
        sf.write(wav,data*gain,22050,subtype='PCM_16')
        subprocess.run([args.ffmpeg,'-nostdin','-v','error','-y','-i',str(wav),'-c:a','libmp3lame','-b:a','192k',str(mp3)],check=True,timeout=60)
        print(str(mp3),flush=True)
    (args.output/'rendering.private.json').write_text(json.dumps({'shared_gain':gain,'origin_seconds':args.origin,
        'sample_rate':22050,'source':'Local Salamander single-velocity MP3 samples by Alexander Holm, CC BY 3.0',
        'listening_review':False,'timing':'Exact supplied MIDI, plus common score origin; no expressive changes'},indent=2))
if __name__=='__main__':main()
