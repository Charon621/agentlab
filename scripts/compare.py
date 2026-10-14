"""Raw, reproducible single/multi runs; no invented objective quality scores."""
import argparse
import json
import statistics
import uuid
from datetime import datetime, timezone
from pathlib import Path
import httpx


def main():
    parser=argparse.ArgumentParser(description='AgentLab 单/多 Agent 对照；默认仅 mock 工程演示')
    parser.add_argument('--base-url',default='http://127.0.0.1:8811')
    parser.add_argument('--execution-mode',choices=['mock','live'],default='mock')
    parser.add_argument('--provider',choices=['qwen','kimi'],default='qwen')
    parser.add_argument('--tasks',type=Path,default=Path(__file__).resolve().parent.parent/'examples/research_tasks.json')
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--limit',type=int,default=15)
    parser.add_argument('--max-calls',type=int,default=12)
    parser.add_argument('--timeout-seconds',type=int,default=120)
    parser.add_argument('--repeats',type=int,default=1)
    args=parser.parse_args()
    tasks=json.loads(args.tasks.read_text(encoding='utf-8'))[:args.limit]
    experiment_id=uuid.uuid4().hex
    result={'schema_version':'1.0','experiment_id':experiment_id,'created':datetime.now(timezone.utc).isoformat(),
            'execution_mode':args.execution_mode,'provider':args.provider,'temperature':0,
            'warning':'MOCK 工程演示，不是模型质量实验' if args.execution_mode=='mock' else '真实模型运行；质量与事实准确性需要独立评审',
            'quality_scores':None,'runs':[]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    def save():args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    with httpx.Client(timeout=args.timeout_seconds+30) as client:
        client.get(args.base_url+'/api/health').raise_for_status()
        for task in tasks:
            for repeat in range(args.repeats):
                for mode in ['single','multi']:
                    payload={'task_id':f'{experiment_id}-{task["id"]}-{repeat}', 'question':task['question'],'mode':mode,
                             'execution_mode':args.execution_mode,'provider':args.provider,'temperature':0,
                             'max_calls':args.max_calls,'timeout_seconds':args.timeout_seconds,
                             'research_instructions':'说明结论边界、反例与资料来源；证据不足时明确标记。'}
                    row={'case_id':task['id'],'difficulty':task['difficulty'],'repeat':repeat,'mode':mode,'request':payload,'rubric':task['rubric']}
                    try:
                        response=client.post(args.base_url+'/api/benchmark/run',json=payload)
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
