"""Raw, reproducible single/multi runs; no invented objective quality scores."""
import argparse
import json
import os
import statistics
import uuid
from datetime import datetime, timezone
from pathlib import Path
import httpx
import hashlib
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from agentlab.search_replay import validate


def main():
    parser=argparse.ArgumentParser(description='AgentLab 单/多 Agent 对照；默认仅 mock 工程演示')
    parser.add_argument('--base-url',default='http://127.0.0.1:8811')
    parser.add_argument('--execution-mode',choices=['mock','live'],default='mock')
    parser.add_argument('--provider',choices=['qwen','kimi','deepseek','glm','openai','anthropic','custom'],default='qwen')
    parser.add_argument('--model-config-id',help='网页所选模型配置 ID；live 联动时必填')
    parser.add_argument('--config-version',type=int,help='导入时的配置版本，避免静默漂移')
    parser.add_argument('--tasks',type=Path,default=Path(__file__).resolve().parent.parent/'examples/research_tasks.json')
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--limit',type=int,default=15)
    parser.add_argument('--max-calls',type=int,default=12)
    parser.add_argument('--timeout-seconds',type=int,default=120)
    parser.add_argument('--repeats',type=int,default=1)
    parser.add_argument('--plan',type=Path,help='执行完整冻结计划，保留记录顺序')
    parser.add_argument('--search-snapshot',type=Path,help='固定检索快照，未命中绝不联网回退')
    parser.add_argument('--resume',action='store_true')
    args=parser.parse_args()
    token=os.getenv('AGENTLAB_GRANT_TOKEN','')
    if args.execution_mode=='live' and (not args.model_config_id or not token):
        parser.error('live 需要 --model-config-id 和环境变量 AGENTLAB_GRANT_TOKEN；在网页模型配置创建联动授权。')
    tasks=json.loads(args.tasks.read_text(encoding='utf-8'))[:args.limit]
    plan=json.loads(args.plan.read_text(encoding='utf8')) if args.plan else None
    cache=validate(json.loads(args.search_snapshot.read_text(encoding='utf8'))) if args.search_snapshot else None
    if plan and cache is None:parser.error('冻结计划必须提供 --search-snapshot，防止单/多架构实时检索漂移')
    experiment_id=plan['experiment_id'] if plan else uuid.uuid4().hex
    units=sorted(plan['units'],key=lambda u:u['order']) if plan else [{**task,'case_id':task['id'],'mode':mode,'repeat':repeat,'unit_id':f"{task['id']}-{mode}-r{repeat}"} for task in tasks for repeat in range(args.repeats) for mode in (['single','multi'] if repeat%2==0 else ['multi','single'])]
    signature=hashlib.sha256(json.dumps({'units':units,'cache_sha':cache['sha256'] if cache else None,'model_config_id':args.model_config_id,'config_version':args.config_version,'execution_mode':args.execution_mode},sort_keys=True).encode()).hexdigest()
    result={'schema_version':'1.0','experiment_id':experiment_id,'created':datetime.now(timezone.utc).isoformat(),
            'execution_mode':args.execution_mode,'provider':args.provider,'temperature':0,
            'warning':'MOCK 工程演示，不是模型质量实验' if args.execution_mode=='mock' else '真实模型运行；质量与事实准确性需要独立评审',
            'quality_scores':None,'runs':[],'signature':signature,'search_snapshot_sha256':cache['sha256'] if cache else None}
    if args.output.exists():
        if not args.resume:parser.error('输出已存在，请使用新路径或 --resume')
        previous=json.loads(args.output.read_text(encoding='utf8'))
        if previous.get('signature')!=signature:parser.error('冻结计划或配置漂移，不能续写原实验')
        result=previous
        experiment_id=result['experiment_id']
    args.output.parent.mkdir(parents=True,exist_ok=True)
    def save():args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    with httpx.Client(timeout=(plan['timeout_seconds'] if plan else args.timeout_seconds)+30,trust_env=False) as client:
        client.get(args.base_url+'/api/health').raise_for_status()
        done={row.get('unit_id') for row in result['runs']}
        for unit in units:
            if unit['unit_id'] in done:continue
            task={**unit,'id':unit['case_id']};mode=unit['mode'];repeat=unit['repeat']
            payload={'task_id':f'{experiment_id}-{task["id"]}-{repeat}-{mode}', 'question':task['question'],'mode':mode,
                     'execution_mode':args.execution_mode,'provider':args.provider,'temperature':0,
                     'max_calls':plan['max_calls'] if plan else args.max_calls,'timeout_seconds':plan['timeout_seconds'] if plan else args.timeout_seconds,
                     'research_instructions':plan['research_instructions'] if plan else '说明结论边界、反例与资料来源；证据不足时明确标记。'}
            if cache:payload['search_snapshot']=cache
            if args.model_config_id:payload['model_config_id']=args.model_config_id
            if args.config_version:payload['expected_config_version']=args.config_version
            row={'unit_id':unit['unit_id'],'case_id':task['id'],'difficulty':task['difficulty'],'repeat':repeat,'mode':mode,'request':payload,'rubric':task['rubric']}
            try:
                response=client.post(args.base_url+'/api/benchmark/run',json=payload,headers={'Authorization':'Bearer '+token} if token else {})
                response.raise_for_status();row['result']=response.json()
                print(task['id'],mode,row['result']['status'],flush=True)
            except httpx.HTTPError as exc:
                row['transport_error']=type(exc).__name__
                print(task['id'],mode,'transport error; not automatically retried',flush=True)
            result['runs'].append(row);save()
    result['summary']=[]
    for difficulty in ['simple','complex']:
        for mode in ['single','multi']:
            rows=[r for r in result['runs'] if r['difficulty']==difficulty and r['mode']==mode]
            metrics=[r['result']['metrics'] for r in rows if 'result' in r]
            if not rows:continue
            result['summary'].append({'difficulty':difficulty,'mode':mode,'attempted':len(rows),
                'engineering_success_rate':sum(m['success'] for m in metrics)/len(rows),
                'mean_latency_ms':statistics.mean(m['latency_ms'] for m in metrics) if metrics else None,
                'mean_actual_model_calls':statistics.mean(m['model_calls'] for m in metrics) if metrics else None,
                'mean_cost_usd':statistics.mean(m['cost_usd'] for m in metrics) if metrics and all(m['cost_usd'] is not None for m in metrics) else None,
                'quality_accuracy_and_citation_support':'not independently scored'})
    save()
    print('Saved',args.output)


if __name__=='__main__':main()
