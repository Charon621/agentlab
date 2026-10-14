"""Owner-scoped model definitions; credentials and bridge grants stay in RAM."""
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from fastapi import HTTPException

PRESETS = {
    "kimi": ("https://api.moonshot.cn/v1", "moonshot-v1-8k", "openai"),
    "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus", "openai"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat", "openai"),
    "glm": ("https://open.bigmodel.cn/api/paas/v4", "glm-4-plus", "openai"),
    "openai": ("https://api.openai.com/v1", "gpt-4.1-mini", "openai"),
    "anthropic": ("https://api.anthropic.com/v1", "claude-sonnet-4-5", "anthropic"),
    "custom": ("", "", "openai"),
}


class ModelRegistry:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.credentials = {}
        self.grants = {}
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, owner_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS model_configs (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, definition TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS model_defaults (owner_id TEXT PRIMARY KEY, config_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS env_claims (provider TEXT PRIMARY KEY, owner_id TEXT NOT NULL);
            """)

    def session(self, token):
        if token and len(token) == 64:
            digest = hashlib.sha256(token.encode()).hexdigest()
            with self.store.connect() as db:
                row = db.execute("SELECT owner_id FROM sessions WHERE token_hash=?", (digest,)).fetchone()
            if row:
                return row[0], None
        token, owner = secrets.token_hex(32), uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("INSERT INTO sessions VALUES(?,?)", (hashlib.sha256(token.encode()).hexdigest(), owner))
        return owner, token

    def get(self, owner, config_id):
        with self.store.connect() as db:
            row = db.execute("SELECT definition FROM model_configs WHERE id=? AND owner_id=?", (config_id, owner)).fetchone()
        if not row:
            raise HTTPException(404, "模型配置不存在")
        return json.loads(row[0])

    def public(self, owner, config):
        with self.lock:
            configured = (owner, config.get("credential_id")) in self.credentials
        return {**config, "configured": configured}

    def freeze(self, owner, config_id):
        return {k: v for k, v in self.get(owner, config_id).items() if k != "last_test"}

    def record_test(self, owner, config_id, version, result):
        with self.lock:
            current = self.get(owner, config_id)
            if current["config_version"] == version:
                current["last_test"] = result
                with self.store.connect() as db:
                    db.execute("UPDATE model_configs SET definition=? WHERE id=? AND owner_id=?", (json.dumps(current), config_id, owner))

    def list(self, owner):
        with self.store.connect() as db:
            rows = db.execute("SELECT definition FROM model_configs WHERE owner_id=? ORDER BY rowid", (owner,)).fetchall()
            default = db.execute("SELECT config_id FROM model_defaults WHERE owner_id=?", (owner,)).fetchone()
        return {"models": [self.public(owner, json.loads(r[0])) for r in rows],
                "default_model_config_id": default[0] if default else None}

    def save(self, owner, values, config_id=None):
        with self.lock:
            old = self.get(owner, config_id) if config_id else None
            config_id = config_id or uuid.uuid4().hex
            key = values.pop("api_key", None)
            credential_id = old["credential_id"] if old else uuid.uuid4().hex
            # Restoring after a restart rehydrates the current ID. Changing a live key
            # allocates a new ID, so earlier tasks retain their original credential.
            if key:
                if (owner, credential_id) in self.credentials:
                    credential_id = uuid.uuid4().hex
                self.credentials[owner, credential_id] = {"key": key, "config_id": config_id}
            config = {**values, "id": config_id, "config_version": old["config_version"] + 1 if old else 1,
                      "credential_id": credential_id, "search": os.getenv("AGENTLAB_SEARCH", "wikipedia")}
            with self.store.connect() as db:
                db.execute("INSERT OR REPLACE INTO model_configs VALUES(?,?,?)", (config_id, owner, json.dumps(config)))
                db.execute("INSERT OR IGNORE INTO model_defaults VALUES(?,?)", (owner, config_id))
            return self.public(owner, config)

    def resolve(self, owner, snapshot):
        with self.lock:
            if snapshot.get("integration_grant_id") and not any(g["id"] == snapshot["integration_grant_id"] and g["owner_id"] == owner and g["model_config_id"] == snapshot.get("id") and g["config_version"] == snapshot.get("config_version") and g["credential_id"] == snapshot.get("credential_id") and g["expires"] > time.time() for g in self.grants.values()):
                from .providers import ProviderError
                raise ProviderError("联动授权已撤销或过期，原任务已暂停。", "grant_missing")
            item = self.credentials.get((owner, snapshot.get("credential_id")))
            if not item or item["config_id"] != snapshot.get("id"):
                from .providers import ProviderError
                name = snapshot.get("provider", "model").upper()
                raise ProviderError(f"credential_missing：原任务密钥已删除或服务已重启。请在模型配置补充原凭证后继续（{name}_API_KEY / 网页密钥）。", "credential_missing")
            return item["key"]

    def delete_credentials(self, owner, config_id):
        self.get(owner, config_id)
        with self.lock:
            for item in list(self.credentials):
                if item[0] == owner and self.credentials[item]["config_id"] == config_id:
                    del self.credentials[item]

    def restore_credential(self, owner, config_id, credential_id, key):
        config = self.get(owner, config_id)
        # IDs must appear in this owner's current definition or a frozen task.
        allowed = credential_id == config["credential_id"]
        if not allowed:
            with self.store.connect() as db:
                allowed = bool(db.execute("SELECT 1 FROM tasks WHERE json_extract(request,'$.owner_id')=? AND json_extract(request,'$.provider_config.id')=? AND json_extract(request,'$.provider_config.credential_id')=? LIMIT 1", (owner, config_id, credential_id)).fetchone())
        if not allowed:
            raise HTTPException(404, "凭证标识不存在")
        with self.lock:
            if (owner, credential_id) in self.credentials:
                raise HTTPException(409, "凭证仍存在；修改密钥请保存新配置版本")
            self.credentials[owner, credential_id] = {"key": key, "config_id": config_id}
        return {"credential_id": credential_id, "configured": True}

    def delete(self, owner, config_id):
        self.delete_credentials(owner, config_id)
        with self.lock:
            for token, grant in list(self.grants.items()):
                if grant["owner_id"] == owner and grant["model_config_id"] == config_id:
                    del self.grants[token]
            with self.store.connect() as db:
                db.execute("DELETE FROM model_configs WHERE id=? AND owner_id=?", (config_id, owner))
                db.execute("DELETE FROM model_defaults WHERE owner_id=? AND config_id=?", (owner, config_id))

    def set_default(self, owner, config_id):
        self.get(owner, config_id)
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO model_defaults VALUES(?,?)", (owner, config_id))
        return {"default_model_config_id": config_id}

    def claim_env(self, owner, provider):
        with self.lock, self.store.connect() as db:
            row = db.execute("SELECT owner_id FROM env_claims WHERE provider='local-env'").fetchone()
            if row and row[0] != owner:
                raise HTTPException(403, "本项目环境凭证已绑定另一个本地会话；请填写自己的网页密钥")
            db.execute("INSERT OR IGNORE INTO env_claims VALUES('local-env',?)", (owner,))

    def unclaimed_history(self):
        with self.store.connect() as db:
            return db.execute("SELECT COUNT(*) FROM tasks WHERE json_extract(request,'$.owner_id') IS NULL").fetchone()[0]

    def claim_history(self, owner):
        with self.lock:
            with self.store.connect() as db:
                rows = db.execute("SELECT id,request FROM tasks WHERE json_extract(request,'$.owner_id') IS NULL").fetchall()
            if rows:
                self.claim_env(owner, "legacy")
            configs = {}
            for row in rows:
                req = json.loads(row["request"])
                req["owner_id"] = owner
                if req.get("execution_mode") == "live":
                    frozen = dict(req.get("provider_config") or {})
                    provider = frozen.get("provider", req.get("provider", "qwen"))
                    preset = PRESETS.get(provider, PRESETS["qwen"])
                    values = {"name": "历史任务 / " + provider, "provider": provider, "protocol": frozen.get("protocol", preset[2]),
                              "model": frozen.get("model", preset[1]), "base_url": frozen.get("base_url", preset[0]),
                              "temperature": frozen.get("temperature", req.get("temperature", 0)), "max_tokens": frozen.get("max_tokens", 2600),
                              "supports_tools": frozen.get("tool_mode") == "native", "supports_temperature": True,
                              "tool_mode": frozen.get("tool_mode", "json"), "input_price": frozen.get("input_price") or None,
                              "output_price": frozen.get("output_price") or None}
                    signature = json.dumps(values, sort_keys=True)
                    if signature not in configs:
                        configs[signature] = self.save(owner, values)
                    config = configs[signature]
                    req["provider_config"] = {**{k: v for k, v in config.items() if k != "configured"}, **frozen,
                                              "id": config["id"], "config_version": config["config_version"], "credential_id": config["credential_id"]}
                    req["model_config_id"] = config["id"]
                with self.store.connect() as db:
                    db.execute("UPDATE tasks SET request=? WHERE id=? AND json_extract(request,'$.owner_id') IS NULL", (json.dumps(req, ensure_ascii=False), row["id"]))
            return {"claimed": len(rows)}

    def grant(self, owner, config_id):
        config = self.get(owner, config_id)
        self.resolve(owner, config)
        token = secrets.token_urlsafe(32)
        item = {"id": uuid.uuid4().hex, "owner_id": owner, "model_config_id": config_id, "config_version": config["config_version"],
                "credential_id": config["credential_id"], "created": time.time(), "expires": time.time() + 86400}
        with self.lock:
            self.grants[hashlib.sha256(token.encode()).hexdigest()] = item
        return {"id": item["id"], "token": token, "model_config": self.public(owner, config)}

    def authorize(self, authorization, config_id):
        token = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
        with self.lock:
            item = self.grants.get(hashlib.sha256(token.encode()).hexdigest())
        if not item or item["model_config_id"] != config_id or item["expires"] <= time.time():
            raise HTTPException(401, "联动授权无效、已撤销或不属于所选模型")
        current = self.get(item["owner_id"], config_id)
        if current["config_version"] != item["config_version"] or current["credential_id"] != item["credential_id"]:
            raise HTTPException(409, "联动授权绑定的模型版本已变化。请重新创建授权并导入。")
        return dict(item)

    def list_grants(self, owner):
        with self.lock:
            return {"grants": [{k: v for k, v in g.items() if k != "owner_id"} for g in self.grants.values() if g["owner_id"] == owner]}

    def revoke(self, owner, grant_id):
        with self.lock:
            for token, item in list(self.grants.items()):
                if item["id"] == grant_id and item["owner_id"] == owner:
                    del self.grants[token]
                    return {"revoked": True}
        raise HTTPException(404, "联动授权不存在")
