"""Sequential local analysis of camera parts with reversible source-time mappings."""
import copy
import math
from pathlib import Path
import shutil
import time

from .camera_parts import names
from .fingerprint import digest_source
from .native import TICKS
from .speech import MAX_WORDS


def seconds(value):
    if isinstance(value,bool):raise ValueError('Invalid camera timestamp')
    n=float(value)
    if not math.isfinite(n) or not 0<=n<=48*3600:raise ValueError('Camera timestamp is outside its supported range')
    return n


def package(asset,source):
    parts=asset.get('locator',{}).get('recording_parts')
    if not parts:return []
    match,counters=names(asset['filename'])
    if not isinstance(parts,list) or not 2<=len(parts)<=16 or len(parts)!=len(counters):raise ValueError('Recording group is missing a part')
    result=[];parent=None
    for index,part in enumerate(parts):
        if not isinstance(part,dict) or not isinstance(part.get('path'),str):raise ValueError('Invalid recording part path')
        path=Path(part['path'])
        if path.is_symlink() or not path.is_file():raise ValueError('A recording part is missing; retrieve the complete group')
        path=path.resolve();m,ids=names(path.name)
        if len(ids)!=1 or ids[0]!=counters[index] or m[1].casefold()!=match[1].casefold() or m[4]!=match[4] or m[6].casefold()!=match[6].casefold():raise ValueError('A file does not match its recording-group identity')
        if index==0:
            parent=path.parent
            if path!=Path(source).resolve() or (m[2],m[3])!=(match[2],match[3]):raise ValueError('The first camera part does not match its source')
        if path.parent!=parent:raise ValueError('Camera parts must stay together in one source package')
        result.append({**part,'path':str(path),'filename':path.name})
    return result


def analyze(asset,source,work,options,stage,cancelled,native_factory):
    parts=package(asset,source)
    if not parts:
        with native_factory(work,cancelled) as native:return native.analyze(source,asset['kind'],options,stage)
    from .worker import Cache
    scenes=[];words=[];texts=[];reports=[];offset=0.;total_bytes=0;preview='';warnings=[];languages=set()
    cap_per_part=max(1,10000//len(parts))
    for index,part in enumerate(parts):
        if cancelled():raise RuntimeError('Job cancelled')
        path=Path(part['path']);stage('Analysing camera part '+str(index+1)+'/'+str(len(parts))+' · '+path.name)
        with Cache(work).job() as part_work:
            with native_factory(part_work,cancelled) as native:
                found,speech,report=native.analyze(path,asset['kind'],options,lambda label:stage('Part '+str(index+1)+'/'+str(len(parts))+' · '+label))
            part_bytes = path.stat().st_size
            if not (part.get("second_lens") or part.get("secondLens")):
                try:
                    fingerprint = digest_source(path, cancelled)
                except OSError:
                    fingerprint = None
                if fingerprint and fingerprint["bytes"] == part_bytes:
                    report["file_fingerprint"] = fingerprint
            duration=seconds(float(report.get('probe',{}).get('duration',0))/TICKS)
            if duration<=0:raise ValueError('Camera part has no valid duration; group was not committed')
            preview_name=report.pop('_preview_file','')
            if index==0 and isinstance(preview_name,str) and Path(preview_name).name==preview_name:
                p=part_work/preview_name
                if p.is_file() and not p.is_symlink() and 0<p.stat().st_size<=2*1024**2:
                    preview='group-preview.png';shutil.copyfile(p,Path(work)/preview)
            for original in found[:cap_per_part]:
                scene=copy.deepcopy(original)
                start,end=seconds(scene.get('start',0)),seconds(scene.get('end',0))
                if end<start or end>duration+1:raise ValueError('Scene time range is outside its camera part')
                origin={'index':index,'filename':path.name,'start':start,'end':end,'offset':offset}
                scene.update({'start':offset+start,'end':offset+end,'source_part':origin})
                if scene.get('sample_key'):
                    scene['sample_key']='part-'+str(index)+':'+str(scene['sample_key'])
                    at=seconds(scene.get('sample_time',start));origin['sample_time']=at;scene['sample_time']=offset+at
                    for view in scene.get('views',[])[:6]:view['at']=offset+seconds(view.get('at',at))
                scenes.append(scene)
            if len(found)>cap_per_part:warnings.append('Part '+str(index+1)+' scene storage limit reached; each part remains represented')
            for word in speech.get('words',[])[:200000-len(words)]:
                start,end=seconds(word.get('start',0)),seconds(word.get('end',0))
                if end<start:raise ValueError('Invalid speech time range in camera part')
                words.append({**word,'start':offset+start,'end':offset+end,'source_part':{'index':index,'filename':path.name,'start':start,'end':end,'offset':offset}})
            texts.append(str(speech.get('text','')))
            if isinstance(speech.get('language'),str):languages.add(speech['language'])
            total_bytes+=part_bytes
            reports.append({'index':index,'filename':path.name,'offset':offset,'duration':duration,'bytes':part_bytes,'analysis':report})
            offset=seconds(offset+duration)
    first=reports[0]['analysis'];statuses=[r['analysis'].get('speech_status') for r in reports]
    # Native.analyze returns speech separately; retain the joined, time-shifted words here.
    speech={'text':'\n'.join(t for t in texts if t),'words':words,'time_unit':'seconds','source':'Sequential camera parts',
            'language':next(iter(languages)) if len(languages)==1 else 'mixed'}
    speech_status='failed' if 'failed' in statuses else ('no_audio' if all(s=='no_audio' for s in statuses) else 'complete')
    directional={}
    if any(r['analysis'].get('is_360') for r in reports):
        directional={'version':1,'status':'sampled','parts':len(parts),'directions_per_window':6,
                     **{key:sum(int(r['analysis'].get('directional_analysis',{}).get(key,0)) for r in reports) for key in ('windows','frames_described','frames_missing')},
                     'time_basis':'Concatenated part time; every scene also retains its original filename and local time',
                     'source_projection':'Calibrated native camera per part; attached proxies reused internally'}
        warnings.append('360 directions are sampled separately in each camera part; events between samples may be missed')
    warnings.extend(r['analysis'].get('coverage_warning','') for r in reports if r['analysis'].get('directional_error'))
    probe={**first.get('probe',{}),'name':asset['filename'],'duration':round(offset*TICKS),'file_size':total_bytes}
    provenance={key:first.get(key) for key in ('engine','offline_inference','vision_model','cut_model','speech_model','claims','vr_properties')}
    provenance.update({'analyzed_at':time.time(),'media_kind':asset['kind'],'probe':probe,'source_bytes':total_bytes,'parts':reports,
                       'is_360':any(r['analysis'].get('is_360') for r in reports),
                       'camera_proxy_attached':all(r['analysis'].get('camera_proxy_attached') for r in reports),
                       'speech_status':speech_status,'speech_error':'; '.join(r['analysis'].get('speech_error','') for r in reports if r['analysis'].get('speech_error')),
                       'directional_analysis':directional,'coverage_warning':'; '.join(dict.fromkeys(warnings)),
                       'part_time_mapping':'Group times concatenate the full native duration of each part; source_part stores the exact file and local seconds'})
    if asset["kind"] == "video":
        positions = [r["analysis"].get("embedded_gps") for r in reports]
        original_position = first.get("embedded_gps")
        if original_position and all(position == original_position for position in positions):
            provenance.update(video_gps_status="recorded", embedded_gps=original_position)
        elif any(positions):
            provenance["video_gps_status"] = "part_specific"
        else:
            gps_statuses = {r["analysis"].get("video_gps_status", "unavailable") for r in reports}
            provenance["video_gps_status"] = (next(iter(gps_statuses)) if len(gps_statuses) == 1 else "unavailable")
    if preview:provenance['_preview_file']=preview
    return scenes,speech,provenance


def repair_speech(asset, source, model, read_part):
    """Rebuild group speech after re-reading failed parts only."""
    from .store import normalize_speech

    saved = asset["analysis"]
    saved_parts = saved["parts"]
    sources = package(asset, source)
    if len(sources) != len(saved_parts):
        raise ValueError("Retrieve the complete indexed recording group before retrying speech")
    existing = saved.get("speech", {})
    retained = [[] for _ in sources]
    for word in existing.get("words", []):
        origin = word.get("source_part", {})
        index = origin.get("index")
        if (type(index) is not int or not 0 <= index < len(sources)
                or origin.get("filename") != sources[index]["filename"]):
            raise ValueError("Existing words lack a valid part mapping. Run full analysis to repair the index.")
        retained[index].append(word)
    if existing.get("text") and not any(retained):
        raise ValueError("Existing transcript lacks timed words. Run full analysis to repair the index.")
    updated_parts, words, evidence, languages, models = [], [], [], set(), set()
    for index, (part, previous) in enumerate(zip(sources, saved_parts)):
        filename = part["filename"]
        if previous["index"] != index or previous["filename"] != filename:
            raise ValueError("Source part identity differs from the indexed recording")
        part_analysis = previous["analysis"]
        part_words = retained[index]
        language = existing.get("language", "")
        if part_analysis.get("speech_status") == "failed":
            offset = seconds(previous["offset"])
            duration = seconds(previous["duration"])
            expected = {**part_analysis, "source_bytes": previous["bytes"]}
            speech, result, verification = read_part(Path(part["path"]), expected, filename, index)
            part_analysis = {**part_analysis, "speech_status": result, "speech_model": model, "speech_error": ""}
            speech = normalize_speech(speech, part_analysis)
            if part_words and not speech.get("words"):
                raise ValueError("The retry returned no timed words. Existing partial words were kept.")
            part_words = []
            for word in speech.get("words", []):
                start, end = seconds(word["start"]), seconds(word["end"])
                if end < start or end > duration + 1:
                    raise ValueError("Recovered speech time is outside the indexed camera part")
                origin = {"index": index, "filename": filename, "start": start, "end": end, "offset": offset}
                part_words.append({**word, "start": offset + start, "end": offset + end, "source_part": origin})
            evidence.append({"index": index, "filename": filename, "verification": verification})
            language = speech.get("language", "")
        if len(words) + len(part_words) > MAX_WORDS:
            raise ValueError("Repaired recording exceeds the catalogue transcript word limit")
        if part_words:
            if language:
                languages.add(language)
            if part_analysis.get("speech_model"):
                models.add(part_analysis["speech_model"])
        words.extend(part_words)
        updated_parts.append({**previous, "analysis": part_analysis})
    statuses = [part["analysis"]["speech_status"] for part in updated_parts]
    status = "complete" if words else "no_audio" if all(s == "no_audio" for s in statuses) else "no_speech"
    speech = {**existing, "text": " ".join(word["text"] for word in words), "words": words,
              "language": next(iter(languages)) if len(languages) == 1 else ("mixed" if languages else ""),
              "source": "Sequential camera parts", "time_unit": "seconds"}
    report = {"parts": updated_parts, "speech_status": status, "speech_error": "",
              "speech_model": next(iter(models)) if len(models) == 1 else ("mixed" if models else model)}
    return speech, report, evidence
