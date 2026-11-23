#!/usr/bin/env python3
"""Validate this specification pack only. Does not run or certify VideoMind."""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path
from fractions import Fraction
from urllib.parse import unquote, urlparse

try:
    import yaml
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError as exc:
    raise SystemExit('Install requirements-docs.txt with your Python environment first: '+str(exc))

ROOT=Path(__file__).resolve().parents[1]
ERRORS=[]
COUNTS={}
def fail(message):ERRORS.append(message)
def read(path):
    p=path if isinstance(path,Path) else ROOT/path
    return yaml.safe_load(p.read_text(encoding='utf-8')) if p.suffix in {'.yaml','.yml'} else json.loads(p.read_text(encoding='utf-8'))
def refs(node):
    if isinstance(node,dict):
        if '$ref' in node:yield node['$ref']
        for value in node.values():yield from refs(value)
    elif isinstance(node,list):
        for value in node:yield from refs(value)
def resolve(reference,base,document=None):
    path,_,fragment=reference.partition('#')
    if urlparse(path).scheme:
        raise ValueError('Unexpected network ref: '+reference)
    target=(base.parent/path).resolve() if path else base.resolve()
    if not target.is_relative_to(ROOT):raise ValueError('Ref escapes pack: '+reference)
    data=document if not path and document is not None else read(target)
    if fragment:
        if not fragment.startswith('/'):raise ValueError('Only JSON pointers supported: '+reference)
        for token in fragment[1:].split('/'):
            token=unquote(token).replace('~1','/').replace('~0','~')
            data=data[int(token)] if isinstance(data,list) else data[token]
    return data,target
def bundle(node,base,document=None,chain=()):
    if isinstance(node,list):return [bundle(x,base,document,chain) for x in node]
    if not isinstance(node,dict):return node
    if '$ref' in node:
        key=(str(base),node['$ref'])
        if key in chain:raise ValueError('Schema cycle in provider bundle: '+str(key))
        target,new_base=resolve(node['$ref'],base,document)
        resolved=bundle(target,new_base,read(new_base),chain+(key,))
        siblings={k:v for k,v in node.items() if k!='$ref'}
        if siblings:resolved={'allOf':[resolved,bundle(siblings,base,document,chain)]}
        return resolved
    return {k:bundle(v,base,document,chain) for k,v in node.items()}

def check_required_files():
    required=['README.md','AGENTS.md','.specify/memory/constitution.md','requirements-docs.txt','prompts/START_HERE.txt','prompts/CONTINUE.txt','prompts/REVIEW.txt','contracts/openapi.yaml','contracts/schemas/domain.schema.json','contracts/tool-registry.json','traceability/requirements.json','traceability/task-index.json','traceability/acceptance-matrix.json','status/PROGRESS.md']
    required += [f'docs/{x}' for x in ['00_SPEC_WORKFLOW.md','01_PRD.md','02_UX_FLOWS.md','03_ARCHITECTURE.md','04_MEDIA_RETRIEVAL.md','05_HARNESS_EDITING.md','06_DATA_API.md','07_CODEX_EXECUTION.md','08_VERIFICATION_RELEASE.md','09_DECISIONS_AND_SOURCES.md']]
    for f in required:
        if not (ROOT/f).is_file():fail('Missing file: '+f)
    for folder in (ROOT/'specs').iterdir():
        if folder.is_dir():
            for name in ['spec.md','plan.md','tasks.md']:
                if not (folder/name).is_file():fail('Missing feature file: '+str(folder/name))

def check_docs():
    files=list(ROOT.rglob('*.md'))
    for file in files:
        content=file.read_text(encoding='utf-8')
        if len(re.findall(r'^\s*```',content,re.M))%2:fail('Unclosed fence: '+str(file.relative_to(ROOT)))
        for link in re.findall(r'\[[^\]]*\]\(([^)]+)\)',content):
            target=link.split('#')[0].split(' "')[0]
            if not target or urlparse(target).scheme or target.startswith('mailto:'):continue
            if not (file.parent/unquote(target)).exists():fail(f'Broken local link in {file.relative_to(ROOT)}: {target}')
    COUNTS['markdown_files']=len(files)

def check_traceability():
    reqs=read('traceability/requirements.json')['requirements']
    tasks=read('traceability/task-index.json')['tasks']
    cases=read('traceability/acceptance-matrix.json')['cases']
    req={x['id']:x for x in reqs};task={x['id']:x for x in tasks};case={x['id']:x for x in cases}
    for name,rows,mapping in [('requirements',reqs,req),('tasks',tasks,task),('acceptance',cases,case)]:
        if len(rows)!=len(mapping):fail('Duplicate IDs: '+name)
    definitions={}
    for file in (ROOT/'specs').glob('*/tasks.md'):
        for tid,title in re.findall(r'^### (T\d{2}-\d{3}) ([^\n]+)$',file.read_text(encoding='utf-8'),re.M):
            if tid in definitions:fail('Duplicate task heading: '+tid)
            definitions[tid]=file.relative_to(ROOT).as_posix()
    if set(definitions)!=set(task):fail('task-index differs from task headings')
    for r in reqs:
        if not r['task_ids'] or not r['acceptance_ids']:fail('Uncovered requirement: '+r['id'])
        for tid in r['task_ids']:
            if tid not in task or r['id'] not in task[tid]['requirement_ids']:fail('Broken requirement/task edge: '+r['id']+' '+tid)
        for aid in r['acceptance_ids']:
            if aid not in case or r['id'] not in case[aid]['requirement_ids']:fail('Broken requirement/acceptance edge: '+r['id']+' '+aid)
    for t in tasks:
        if not t['requirement_ids'] or not t['acceptance'] or not t['deliverables']:fail('Incomplete task: '+t['id'])
        if definitions.get(t['id'])!=t['file']:fail('Task source mismatch: '+t['id'])
        for rid in t['requirement_ids']:
            if rid not in req:fail('Unknown requirement in task: '+rid)
        for dep in t['depends_on']:
            if dep not in task:fail('Unknown task dependency: '+t['id']+' -> '+dep)
    for c in cases:
        if not c['given'] or not c['when'] or not c['then']:fail('Incomplete AT: '+c['id'])
        for rid in c['requirement_ids']:
            if rid not in req:fail('Unknown requirement in AT: '+rid)
    done=set();active=[]
    def visit(tid):
        if tid in active:fail('Task dependency cycle: '+' -> '.join(active+[tid]));return
        if tid in done or tid not in task:return
        active.append(tid)
        for dep in task[tid]['depends_on']:visit(dep)
        active.pop();done.add(tid)
    for tid in task:visit(tid)
    prd=(ROOT/'docs/01_PRD.md').read_text(encoding='utf-8')
    for rid in req:
        if rid not in prd:fail('PRD misses requirement: '+rid)
    for file in list((ROOT/'docs').glob('*.md'))+list((ROOT/'specs').glob('*/*.md')):
        for rid in set(re.findall(r'REQ-[A-Z]+-\d{3}',file.read_text(encoding='utf-8'))):
            if rid not in req:fail(f'Unknown REQ {rid} in {file.relative_to(ROOT)}')
    COUNTS.update(requirements=len(req),tasks=len(task),acceptance_cases=len(case),feature_packages=len(list((ROOT/'specs').iterdir())))

def business_errors(instance,typename):
    e=[]
    if typename in {'TimeRange','TimelineClip','ClipDraft'}:
        a,b=('start_us','end_us') if typename=='TimeRange' else ('source_start_us','source_end_us')
        if instance[b]<=instance[a]:e.append('end must exceed start')
    if typename=='TimelineRevision':
        total=sum(c['source_end_us']-c['source_start_us'] for c in instance['clips'])
        if instance['total_duration_us']!=total:e.append('Fixture nominal duration mismatch')
    if typename=='FrameArtifact':
        origin=instance['source_origin']
        actual=round((Fraction(instance['actual_pts']*instance['time_base_num'],instance['time_base_den'])-Fraction(origin['num'],origin['den']))*1000000)
        if abs(instance['actual_us']-actual)>1:e.append('Fixture actual_us/PTS mismatch')
    return e

def check_contracts():
    schema_path=ROOT/'contracts/schemas/domain.schema.json'
    schema=read(schema_path)
    Draft202012Validator.check_schema(schema)
    for key,value in schema['$defs'].items():
        Draft202012Validator.check_schema(bundle(value,schema_path,schema))
    api_path=ROOT/'contracts/openapi.yaml';api=read(api_path)
    if api.get('openapi')!='3.1.0':fail('Unexpected OpenAPI version')
    operations=[]
    for path,item in api['paths'].items():
        for method,op in item.items():
            if method not in {'get','post','put','patch','delete'}:continue
            operations.append(op['operationId'])
            expected=set(re.findall(r'{([^}]+)}',path))
            actual={p['name'] for p in op.get('parameters',[]) if p['in']=='path'}
            if expected!=actual:fail('Path parameter mismatch: '+path)
            if not op.get('responses'):fail('Missing responses: '+path)
    if len(operations)!=len(set(operations)):fail('Duplicate operationId')
    registry_path=ROOT/'contracts/tool-registry.json';registry=read(registry_path)
    names=[];wire=[]
    for tool in registry['tools']:
        names.append(tool['name']);wire.append(tool['provider_name'])
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',tool['provider_name']):fail('Invalid provider tool name: '+tool['name'])
        for key in ['input_schema','output_schema']:
            result=bundle(tool[key],registry_path,registry)
            Draft202012Validator.check_schema(result)
            if list(refs(result)):fail('Unresolved refs after provider bundle: '+tool['name'])
        if tool['effect']!='READ' and not tool['idempotency_required']:fail('Effect tool lacks idempotency: '+tool['name'])
    if len(names)!=len(set(names)) or len(wire)!=len(set(wire)):fail('Duplicate tool names')
    for path,document in [(schema_path,schema),(api_path,api),(registry_path,registry)]:
        for reference in refs(document):
            try:resolve(reference,path,document)
            except Exception as exc:fail(f'Broken contract ref {path.name} {reference}: {exc}')
    manifest=read('contracts/examples/manifest.json')
    for example in manifest:
        instance=read(ROOT/'contracts'/example['file'])
        target,base=resolve(example['schema'],ROOT/'contracts/manifest-root.json')
        resolved=bundle(target,base,read(base))
        errors=list(Draft202012Validator(resolved,format_checker=FormatChecker()).iter_errors(instance))
        kind=example['schema'].split('/')[-1]
        logic=business_errors(instance,kind) if not errors else []
        expected=example['expected']
        if expected=='valid' and (errors or logic):fail('Valid fixture failed '+example['file']+': '+str([e.message for e in errors]+logic))
        if expected=='invalid' and not errors:fail('Invalid structure accepted: '+example['file'])
        if expected=='business_invalid' and (errors or not logic):fail('Business-invalid fixture not detected at intended layer: '+example['file'])
    COUNTS.update(domain_schemas=len(schema['$defs']),api_operations=len(operations),agent_tools=len(names),contract_examples=len(manifest))

def main():
    for check in [check_required_files,check_docs,check_traceability,check_contracts]:
        try:check()
        except Exception as exc:fail(check.__name__+': '+type(exc).__name__+': '+str(exc))
    report={'spec_validation':'PASS' if not ERRORS else 'FAIL','application_validation':'NOT_RUN','counts':COUNTS,'errors':ERRORS,'scope':'Static specification, refs, DAG and fixture checks only. No VideoMind runtime, API provider, retrieval quality or rendered media has been certified.'}
    (ROOT/'status/SPEC_VALIDATION.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 1 if ERRORS else 0

if __name__=='__main__':sys.exit(main())
