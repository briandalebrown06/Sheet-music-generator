"""Bounded local repairs for audio-reviewed note candidates.

The first pass only assigns hands. The optional second pass changes the octave
or release of existing unreviewed secondary material, with an explicit log.
Audio-reviewed and melodic pitches/timing/dynamics and all note IDs survive.
"""
from __future__ import annotations
import copy
from collections import defaultdict, deque
from fractions import Fraction
from backend.app.piano import _timeline, _settings, _locked, assess_playability


def _record_edit(note, edit):
    previous = note.get('arrangement_provenance') or {}
    note['arrangement_provenance'] = {**previous,
        'kind': 'derived_accompaniment',
        'changes': list(previous.get('changes', [])) + [
            {key: value for key, value in edit.items() if key != 'note_id'}]}
    note['provenance'], note['locked'] = 'edited', False


def repair_audio_hands(notes, score_settings, piano_settings=None, *, max_audits=128, max_edits=48):
    piano,score=_settings(piano_settings,score_settings)
    if piano.left_reach != piano.right_reach:
        raise ValueError('Hand-only interval coloring currently requires equal hand reach limits.')
    result=copy.deepcopy(notes)
    byid={n['id']:n for n in result}
    timeline,_=_timeline(result,score)
    timeline=[n for n in timeline if n.get('role')!='vocal_cue']
    graph=defaultdict(set)
    active=[]
    for n in timeline:
        active=[a for a in active if a['_release']>n['_start']+1e-8]
        for a in active:
            if abs(n['_pitch']-a['_pitch'])>piano.left_reach:
                graph[n['id']].add(a['id']);graph[a['id']].add(n['id'])
        active.append(n)
    before=assess_playability(result,piano,score)
    report=before
    changed=[];unresolved=[];seen=set();audits=0
    def rank(r,changes=0):
        density=r['density']['over_target_frames']
        excess=sum(max(0,x.get('span_semitones',0)-x.get('reach_limit',0)) for x in r['issues'] if x['severity']=='error')
        return (r['summary']['error_count'],excess,density,r['summary']['warning_count'],changes)
    for seed in sorted(graph):
        if audits >= max_audits or len(changed) >= max_edits:break
        if seed in seen:continue
        color={seed:0};queue=deque([seed]);conflicts=[]
        while queue:
            nid=queue.popleft();seen.add(nid)
            for neighbour in graph[nid]:
                if neighbour not in color:
                    color[neighbour]=1-color[nid];queue.append(neighbour)
                elif color[neighbour]==color[nid]:conflicts.append((nid,neighbour))
        ids=set(color)
        if not any(x['severity']=='error' and ids.intersection(x['note_ids']) for x in report['issues']):continue
        if conflicts:
            # The component cannot fit either hand coloring without changing
            # pitches/timing or relaxing the requested reach limit.
            unresolved.append({'kind':'not_two_colorable','note_ids':sorted(ids),'conflict_edges':sorted(set(tuple(sorted(x)) for x in conflicts)),
                'events':[{k:byid[n].get(k) for k in ('id','pitch','start','end','hand','role','audio_review')} for n in sorted(ids)],
                'reason':'An odd cycle of mutually incompatible overlapping hand spans prevents hand-only repair.'})
            continue
        best=None
        for flip in (0,1):
            if audits >= max_audits:break
            assignments={nid:('right' if value^flip else 'left') for nid,value in color.items()}
            if any((_locked(byid[nid]) or byid[nid].get('fingering') is not None) and byid[nid].get('hand')!=hand for nid,hand in assignments.items()):continue
            candidate=copy.deepcopy(result)
            edit_ids=[]
            for n in candidate:
                if n['id'] in assignments and n.get('hand')!=assignments[n['id']]:
                    n['hand']=assignments[n['id']];edit_ids.append(n['id'])
            if not edit_ids or len(changed) + len(edit_ids) > max_edits:continue
            checked=assess_playability(candidate,piano,score);audits+=1
            # Never create a denser chord merely to satisfy reach, or increase
            # hard error count. Movement warnings are retained explicitly.
            if checked['density']['over_target_frames']>report['density']['over_target_frames']:continue
            if rank(checked,len(edit_ids))>=rank(report):continue
            proposal=(rank(checked,len(edit_ids)),candidate,checked,assignments,edit_ids)
            if best is None or proposal[0]<best[0]:best=proposal
        if best:
            _,candidate,report,assignments,edit_ids=best
            candidates_by_id = {n['id']: n for n in candidate}
            for nid in edit_ids:
                edit = {'note_id':nid,'field':'hand','before':byid[nid].get('hand'),'after':assignments[nid],
                    'reason':'Local interval-graph hand allocation preserves every audio-reviewed pitch, time and dynamic.'}
                changed.append(edit)
                _record_edit(candidates_by_id[nid], edit)
            result=candidate;byid={n['id']:n for n in result}
    return {'notes':result,'changes':changed,'before':before,'after':report,'unresolved':unresolved,'audits':audits}


def repair_audio_local(notes, score_settings, piano_settings=None, *, max_audits=384, max_edits=48):
    """Repair local reaches while keeping reviewed/lead notes acoustically exact.

    Only hand reassignment is allowed for reviewed events or the melody. Existing
    unlocked inferred bass/secondary harmony may move one octave or release at a
    conflicting attack; onsets, velocities, pitch classes and source timing stay.
    The bounded search never omits notes, inserts pedal or relaxes the profile.
    """
    if not isinstance(max_audits, int) or not 0 <= max_audits <= 4096:
        raise ValueError('Audit budget must be an integer from 0 to 4096.')
    if not isinstance(max_edits, int) or not 0 <= max_edits <= 128:
        raise ValueError('Edit budget must be an integer from 0 to 128.')
    first=repair_audio_hands(notes,score_settings,piano_settings,
                             max_audits=max_audits,max_edits=max_edits)
    result=first['notes']; changes=list(first['changes']); before=first['before']; report=first['after']
    piano,score=_settings(piano_settings,score_settings)
    audits=first['audits'];attempted=set()
    def rank(r,penalty=0):
        excess=sum(max(0,x.get('span_semitones',0)-x.get('reach_limit',0)) for x in r['issues'] if x['severity']=='error')
        return (r['summary']['error_count'],excess,r['density']['over_target_frames'],r['summary']['warning_count'],penalty)
    while report['summary']['error_count'] and audits<max_audits and len(changes)<max_edits:
        timeline,bpm=_timeline(result,score);bytime={n['id']:n for n in timeline};byid={n['id']:n for n in result}
        origin=float(score.first_downbeat_seconds)-float(Fraction(score.pickup_beats))*60/bpm
        best=None
        for issue in sorted((i for i in report['issues'] if i['severity']=='error'),key=lambda x:(x['measure'],x['code'])):
            moment=max(bytime[nid]['_start'] for nid in issue['note_ids'])
            for nid in issue['note_ids']:
                n=byid[nid];t=bytime[nid]
                if _locked(n) or n.get('fingering') is not None:continue
                protected=bool(n.get('audio_review')) or n.get('role') in {'melody','vocal_cue','riff','countermelody'}
                inferred_bass=(n.get('role')=='bass' and n.get('arrangement_provenance',{}).get('inferred_role')=='bass')
                editable=not protected and (n.get('role') in {'harmony','texture'} or inferred_bass)
                proposals=[{'hand':'right' if n.get('hand')=='left' else 'left'}]
                if editable:
                    for shift in (-12,12):
                        if 21<=n['pitch']+shift<=108:
                            for hand in ('left','right'):proposals.append({'pitch':n['pitch']+shift,'hand':hand})
                    release=origin+moment
                    if t['_start']<moment<t['_release'] and release-n['start']>=.08:
                        proposals.append({'end':release})
                for fields in proposals:
                    if audits >= max_audits:break
                    # A shorter sounding end cannot retain a later physical
                    # key-release timestamp. Preserve existing pedal intent.
                    if 'end' in fields and n.get('key_release_seconds') is not None and n['key_release_seconds'] > fields['end']:
                        fields = {**fields, 'key_release_seconds': fields['end']}
                    if len(changes) + sum(n.get(key) != value for key, value in fields.items()) > max_edits:continue
                    signature=(nid,tuple(sorted(fields.items())),tuple(sorted((m['id'],m.get('pitch'),m.get('end'),m.get('hand')) for m in result if m['id'] in issue['note_ids'])) )
                    if signature in attempted:continue
                    attempted.add(signature)
                    candidate=copy.deepcopy(result)
                    for item in candidate:
                        if item['id']==nid:item.update(fields);break
                    checked=assess_playability(candidate,piano,score);audits+=1
                    if checked['density']['over_target_frames']>report['density']['over_target_frames']:continue
                    penalty=(3 if 'pitch' in fields else 1 if 'end' in fields else 0)
                    if rank(checked,penalty)>=rank(report):continue
                    proposal=(rank(checked,penalty),candidate,checked,{nid: fields})
                    if best is None or proposal[0]<best[0]:best=proposal
                    if audits>=max_audits:break
                if audits>=max_audits:break
            # Moving one tone into an occupied hand may temporarily exceed the
            # density target although an opposite-hand exchange is feasible.
            # Search only overlapping local pairs; do not globally reassign
            # phrases or alter the reviewed pitches/timing to escape the load.
            if len(changes) + 2 <= max_edits:
                for nid in issue['note_ids']:
                    if audits >= max_audits:break
                    n, t = byid[nid], bytime[nid]
                    if _locked(n) or n.get('fingering') is not None or n.get('hand') not in ('left','right'):continue
                    partners = [p for p in timeline if p['id'] != nid and p.get('role') != 'vocal_cue'
                        and p.get('hand') in ('left','right') and p['hand'] != n['hand']
                        and not _locked(byid[p['id']]) and byid[p['id']].get('fingering') is None
                        and min(t['_release'],p['_release']) > max(t['_start'],p['_start']) + 1e-8]
                    partners.sort(key=lambda p:(abs(t['_start']-p['_start']),abs(t['_pitch']-p['_pitch']),p['id']))
                    for partner in partners[:12]:
                        if audits >= max_audits:break
                        pid = partner['id']
                        assignments = {nid:{'hand':partner['hand']},pid:{'hand':n['hand']}}
                        signature = ('pair',tuple(sorted((key,value['hand']) for key,value in assignments.items())),
                                     tuple(sorted((m['id'],m.get('pitch'),m.get('end'),m.get('hand')) for m in result if m['id'] in issue['note_ids'])))
                        if signature in attempted:continue
                        attempted.add(signature)
                        candidate = copy.deepcopy(result)
                        for item in candidate:
                            if item['id'] in assignments:item.update(assignments[item['id']])
                        checked=assess_playability(candidate,piano,score);audits+=1
                        if checked['density']['over_target_frames']>report['density']['over_target_frames']:continue
                        if rank(checked,0)>=rank(report):continue
                        proposal=(rank(checked,0),candidate,checked,assignments)
                        if best is None or proposal[0]<best[0]:best=proposal
            if best is not None or audits>=max_audits:break
        if best is None:break
        _,candidate,report,assignments=best
        edited_notes = {n['id']:n for n in candidate}
        for nid,fields in assignments.items():
            for field,value in fields.items():
                if byid[nid].get(field)!=value:
                    edit = {'note_id':nid,'field':field,'before':byid[nid].get(field),'after':value,
                        'reason':('Local hand allocation preserves every audible event.' if field=='hand' else 'One-octave placement of existing inferred secondary material; audio-reviewed events and lead unchanged.' if field=='pitch' else 'Shorten an existing unreviewed secondary release at a conflicting attack; retain its original timing and onset, without inventing pedal.')}
                    changes.append(edit)
                    _record_edit(edited_notes[nid], edit)
        result=candidate
    return {'notes':result,'changes':changes,'before':before,'after':report,'audits':audits,'audit_limit':max_audits,
            'budget_exhausted':audits>=max_audits,'initial_hand_only_unresolved':first['unresolved']}
