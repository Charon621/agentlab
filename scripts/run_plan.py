"""Execute every frozen plan unit with real LangGraph/checkpoints, offline by default."""
import argparse, json, sys, os, uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from agentlab.search_replay import freeze, validate, digest
from agentlab.storage import Store
from agentlab.engine import Engine, benchmark_result
from agentlab.models import TaskRequest

def main():
    p=argparse.ArgumentParser(description='连续完成全部计划；Mock 不调用模型或联网检索')
    p.add_argument('--plan',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--search-snapshot',type=Path);p.add_argument('--resume',action='store_true')
    a=p.parse_args();plan=json.loads(a.plan.read_text(encoding='utf8'));a.output.mkdir(parents=True,exist_ok=True)
    cachefile=a.output/'search-snapshot.json'
    if a.search_snapshot:cache=validate(json.loads(a.search_snapshot.read_text(encoding='utf8')))
    elif cachefile.exists():cache=validate(json.loads(cachefile.read_text(encoding='utf8')))
    else:
        queries={u['question'][:250] for u in plan['units']}
        cache=freeze({q:[{'title':'离线研究方法夹具','url':'https://example.org/research','excerpt':'合成固定证据：比较研究需控制模型、预算、检索信息和采样顺序。','retrieved':False,'retrieved_at':'2026-10-15T00:00:00Z'}] for q in queries})
    cachefile.write_text(json.dumps(cache,ensure_ascii=False,indent=2),encoding='utf8')
    resultfile=a.output/'results.json'
    source_root=Path(__file__).resolve().parent.parent
    source_hashes={str(f.relative_to(source_root)):__import__('hashlib').sha256(f.read_bytes()).hexdigest() for f in sorted((source_root/'agentlab').glob('*.py'))}
    signature=digest({'plan':plan,'search_sha256':cache['sha256'],'execution_mode':'mock','source_hashes':source_hashes})
    if resultfile.exists():
        if not a.resume:p.error('输出已存在；使用 --resume 保留并继续原计划')
        results=json.loads(resultfile.read_text(encoding='utf8'))
        if results['signature']!=signature:p.error('计划或检索快照漂移，必须新建输出目录')
    else:results={'signature':signature,'execution_mode':'mock','quality_scores':None,'source_hashes':source_hashes,'search_snapshot_sha256':cache['sha256'],'runs':[]}
    store=Store(a.output/'runtime');done={r['unit_id'] for r in results['runs']}
    for unit in sorted(plan['units'],key=lambda u:u['order']):
        if unit['unit_id'] in done:continue
        task_id=uuid.uuid5(uuid.NAMESPACE_URL,signature+unit['unit_id']).hex
        req=TaskRequest(question=unit['question'],mode=unit['mode'],review_plan=False,max_calls=plan['max_calls'],timeout_seconds=plan['timeout_seconds'],research_instructions=plan['research_instructions']).model_dump()
        req['provider_config']={**Engine.initial(task_id,req)['provider_config'],'search_snapshot':cache,'tool_mode':'json'}
        if not store.get(task_id):store.create(task_id,req)
        Engine(store).run(task_id)
        task=store.get(task_id);result=benchmark_result(task,task['result'],store)
        results['runs'].append({'unit_id':unit['unit_id'],'order':unit['order'],'case_id':unit['case_id'],'difficulty':unit['difficulty'],'mode':unit['mode'],'repeat':unit['repeat'],'result':result,'quality_review':'not_reviewed'})
        temp=resultfile.with_suffix('.tmp');temp.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf8');temp.replace(resultfile)
        print(unit['unit_id'],task['status'],flush=True)
    print(f"Completed {len(results['runs'])}/{len(plan['units'])}; engineering fixtures only")
if __name__=='__main__':main()
