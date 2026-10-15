"""Immutable retrieval snapshots: explicit hits and symmetric offline misses."""
import hashlib
import json

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def freeze(entries, provenance="synthetic engineering fixture"):
    body={"schema_version":"1.0","miss_policy":"empty_offline","provenance":provenance,"entries":entries}
    return {**body,"sha256":digest(body)}

def validate(snapshot):
    body={k:v for k,v in snapshot.items() if k!='sha256'}
    if snapshot.get('miss_policy')!='empty_offline' or digest(body)!=snapshot.get('sha256'):
        raise ValueError('检索快照哈希或 miss_policy 无效')
    if not isinstance(snapshot.get('entries'),dict):raise ValueError('检索快照 entries 无效')
    for query,items in snapshot['entries'].items():
        if not isinstance(query,str) or not isinstance(items,list):raise ValueError('检索快照结构无效')
        for item in items:
            if not all(isinstance(item.get(k),str) for k in ('title','url','excerpt')):raise ValueError('检索条目无效')
    return snapshot

def replay(snapshot,query):
    validate(snapshot)
    return [dict(item,query=query,cache_snapshot_sha256=snapshot['sha256'],cache_hit=True) for item in snapshot['entries'].get(query,[])]
