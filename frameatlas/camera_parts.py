"""Resolve Studio's explicitly named split groups without guessing missing recordings."""
import datetime
import os
from pathlib import Path

from .studio_inventory import NAME


def names(filename):
    m=NAME.fullmatch(filename) if isinstance(filename,str) else None
    if not m:raise ValueError('A complete camera filename is required')
    counters=m[5].split('_')
    if len({int(n) for n in counters})!=len(counters):raise ValueError('Recording group repeats a part identity')
    return m,counters


def files(root):
    count=0;visited=0
    for directory,dirs,entries in os.walk(root,followlinks=False):
        visited+=1
        if visited>256:raise ValueError('Recording source has too many directories')
        dirs[:]=[d for d in dirs if not (Path(directory)/d).is_symlink() and not d.startswith('.')]
        if any(os.path.ismount(Path(directory)/d) for d in dirs):raise ValueError('Unexpected mounted disk inside recording source')
        count+=len(entries)
        if count>4096:raise ValueError('Recording source has too many files')
        for name in entries:
            path=Path(directory)/name
            if not path.is_symlink() and path.is_file():yield path


def source_paths(root,filename):
    root=Path(root);m,counters=names(filename)
    if len(counters)==1:
        found=[p for p in files(root) if p.name.casefold()==filename.casefold()]
        if len(found)>1:raise ValueError('Several files match this recording; choose no source automatically')
        return found
    # Studio places a split group in this exact folder, with separate camera files.
    expected=Path(filename).stem.casefold()+'-original';folders=[];visited=0
    for directory,dirs,entries in os.walk(root,followlinks=False):
        visited+=1
        if visited>256 or len(dirs)>256:raise ValueError('Recording group has too many directories')
        if len(Path(directory).relative_to(root).parts)>4:
            dirs[:]=[];continue
        dirs[:]=[d for d in dirs if not (Path(directory)/d).is_symlink() and not d.startswith('.')]
        for d in dirs:
            if os.path.ismount(Path(directory)/d):raise ValueError('Unexpected mounted disk inside recording source')
            if d.casefold()==expected:folders.append(Path(directory)/d)
        if len(folders)>1:raise ValueError('Ambiguous recording group folders')
    if not folders:return []
    found={};first=datetime.datetime.strptime(m[2]+m[3],'%Y%m%d%H%M%S')
    for p in files(folders[0]):
        candidate=NAME.fullmatch(p.name)
        if not candidate or candidate[1].casefold()!=m[1].casefold() or candidate[4]!=m[4] or candidate[6].casefold()!=m[6].casefold():continue
        if '_' in candidate[5] or candidate[5] not in counters:continue
        at=datetime.datetime.strptime(candidate[2]+candidate[3],'%Y%m%d%H%M%S')
        if not 0<=(at-first).total_seconds()<=48*3600:raise ValueError('Camera part time is outside this recording group')
        if candidate[5]==counters[0] and (candidate[2],candidate[3])!=(m[2],m[3]):raise ValueError('First recording part does not match the group timestamp')
        if candidate[5] in found:raise ValueError('Several files match one recording part')
        found[candidate[5]]=(p,at)
    if any(n not in found for n in counters):return []
    ordered=[found[n] for n in counters]
    if any(ordered[i][1]>ordered[i+1][1] for i in range(len(ordered)-1)):raise ValueError('Camera part timestamps are not in group order')
    return [p for p,at in ordered]


def validated_parts(mount,paths,records):
    """Require every part's camera trailer and keep all dependencies within the owned volume."""
    result=[];mount=Path(mount).resolve()
    for path in paths:
        record=next((r for r in records if Path(r.get('path','')).resolve()==path.resolve()),None)
        if not record or not record.get('complete'):return []
        part={'path':str(path),'filename':path.name}
        for key in ('proxy','second_lens','secondLens'):
            value=record.get(key)
            if value:
                dep=Path(value)
                if dep.is_symlink() or not dep.is_file() or mount not in dep.resolve().parents:raise ValueError('Camera dependency is outside the owned source volume')
                part[key]=str(dep)
        result.append(part)
    return result
