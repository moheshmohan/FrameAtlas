"""Validate a completed owned Studio group; normal indexing uses the UI/MCP queue."""
from pathlib import Path
from frameatlas.multipart import analyze

def analyze_pilot(store,record):
    """Use the same production package analysis on a completed, owned inspection source."""
    import json
    from frameatlas.camera_parts import source_paths,validated_parts
    from frameatlas.native import Native,cli,CLI
    from frameatlas.studio_stage import owned_work,matching_mount,MOUNT
    import subprocess
    import uuid
    work=owned_work(store.root,Path(record['work']).name)
    mount=work/MOUNT
    if Path(record['work']).resolve()!=work.resolve() or Path(record['destination']).resolve()!=mount.resolve() or not matching_mount(work):
        raise ValueError('Inspection source is not a verified owned volume')
    asset=store.get(record['asset_id'])
    paths=source_paths(mount,asset['filename'])
    if not paths:raise ValueError('All camera parts must finish downloading first')
    records=cli('exec','insta360.list',json.dumps({'folders':[str(mount)]})).get('recordings',[])
    parts=validated_parts(mount,paths,records)
    if len(parts)!=len(paths):raise ValueError('Every native camera trailer must be complete')
    asset['locator']['recording_parts']=parts
    scenes,speech,provenance=analyze(asset,paths[0],work,{},lambda label:print(label,flush=True),lambda:False,Native)
    provenance['acquisition']='Temporary owned Studio group inspection; originals removed after validation'
    preview=provenance.pop('_preview_file','');thumb=asset.get('thumbnail','')
    if preview:
        folder=store.root/'thumbnails';folder.mkdir(exist_ok=True)
        target=folder/(asset['id']+'.jpg')
        temporary=folder/(asset['id']+'.next-'+uuid.uuid4().hex+'.jpg')
        try:
            p=subprocess.run([CLI,'frames',str(work/preview),'--at','0','--out',str(temporary)],capture_output=True,timeout=90)
            if p.returncode==0 and temporary.is_file() and 0<temporary.stat().st_size<=2*1024**2:
                temporary.replace(target);thumb=target.name
        finally:temporary.unlink(missing_ok=True)
    store.analysis(asset['id'],scenes,speech,provenance,thumb)
    result={'asset_id':asset['id'],'parts':[{'filename':p['filename'],'bytes':p['bytes'],'duration':p['duration'],'offset':p['offset']} for p in provenance['parts']],
            'scene_records':len(scenes),'directional':provenance['directional_analysis'],'source_bytes':provenance['source_bytes'],'transcript_chars':len(speech['text'])}
    (store.root/'pilot/split-analysis.json').write_text(json.dumps(result,indent=2))
    return result


if __name__=='__main__':
    import argparse,json,subprocess
    from frameatlas.store import Store,DATA
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',default=str(DATA));parser.add_argument('--record',required=True)
    args=parser.parse_args()
    try:
        path=Path(args.record)
        if not path.is_file() or path.stat().st_size>16384:raise ValueError('Use a bounded source-inspection record')
        record=json.loads(path.read_text())
        if not isinstance(record,dict):raise ValueError('Source-inspection record must be an object')
        print(json.dumps(analyze_pilot(Store(Path(args.data)),record)))
    except (ValueError,TypeError,KeyError,OSError,RuntimeError,subprocess.SubprocessError) as exc:
        raise SystemExit('FrameAtlas validation: '+str(exc)[:1000])
