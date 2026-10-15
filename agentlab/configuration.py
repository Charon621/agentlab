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
    "kimi": ("https://api.moonshot.cn/v1", "kimi-k3", "openai"),
    "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3.8-flash", "openai"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat", "openai"),
    "glm": ("https://open.bigmodel.cn/api/paas/v4", "glm-5.3", "openai"),
    "xiaomi": ("https://api.xiaomimimo.com/v1", "mimo-v2.6-pro", "openai"),
    "openai": ("https://api.openai.com/v1", "gpt-4.1-mini", "openai"),
    "anthropic": ("https://api.anthropic.com/v1", "claude-sonnet-4-5", "anthropic"),
    "custom": ("", "", "openai"),
}

ENV_SEED_NAMES = {
    "qwen": "Qwen 3.8 Flash",
    "glm": "GLM 5.3",
    "kimi": "Kimi K3",
    "xiaomi": "MiMo 2.6 Pro",
    "deepseek": "DeepSeek Chat",
    "openai": "OpenAI",
    "anthropic": "Claude",
}

# credential_source values (A02): do NOT infer from provider name.
CREDENTIAL_SOURCES = ("web", "env_import", "legacy_unclassified")


def env_api_key(provider):
    if not provider or provider == "custom":
        return None
    return os.getenv(provider.upper() + "_API_KEY") or None


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
                CREATE TABLE IF NOT EXISTS env_claims (owner_id TEXT PRIMARY KEY, claimed_at REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS revoked_credentials (owner_id TEXT NOT NULL, credential_id TEXT NOT NULL, config_id TEXT, revoked_at REAL NOT NULL, PRIMARY KEY(owner_id, credential_id));
            """)
            self._migrate_env_claims(db)

    def _migrate_env_claims(self, db):
        """Idempotent migration: old per-provider claims → single machine owner.

        Single owner → compatible. Multi-owner conflict → keep records, block auto-select.
        """
        cols = [r[1] for r in db.execute("PRAGMA table_info(env_claims)").fetchall()]
        if "provider" not in cols:
            return  # already migrated or fresh
        rows = db.execute("SELECT provider, owner_id FROM env_claims").fetchall()
        if not rows:
            db.execute("DROP TABLE IF EXISTS env_claims")
            db.execute("CREATE TABLE IF NOT EXISTS env_claims (owner_id TEXT PRIMARY KEY, claimed_at REAL NOT NULL DEFAULT 0)")
            return
        owners = {r[1] for r in rows}
        db.execute("DROP TABLE IF EXISTS env_claims")
        db.execute("CREATE TABLE IF NOT EXISTS env_claims (owner_id TEXT PRIMARY KEY, claimed_at REAL NOT NULL DEFAULT 0)")
        if len(owners) == 1:
            db.execute("INSERT OR REPLACE INTO env_claims VALUES(?,?)", (owners.pop(), time.time()))
        else:
            # Multi-owner conflict: keep all records for audit; no auto-winner.
            # Store as a special conflict row so API can surface it.
            for owner in owners:
                db.execute("INSERT OR REPLACE INTO env_claims VALUES(?,?)", (owner, 0))

    def env_owner(self):
        """Return the single machine env owner, or None if unclaimed / conflict."""
        with self.store.connect() as db:
            rows = db.execute("SELECT owner_id, claimed_at FROM env_claims WHERE claimed_at > 0").fetchall()
        if len(rows) == 1:
            return rows[0][0]
        return None

    def env_claim_conflicts(self):
        """Return list of conflicting owner IDs (claimed_at=0 means unconfirmed)."""
        with self.store.connect() as db:
            rows = db.execute("SELECT owner_id FROM env_claims WHERE claimed_at = 0").fetchall()
        return [r[0] for r in rows]

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
        """Read-only: no model creation, no key hydration, no default changes."""
        with self.store.connect() as db:
            rows = db.execute("SELECT definition FROM model_configs WHERE owner_id=? ORDER BY rowid", (owner,)).fetchall()
            default = db.execute("SELECT config_id FROM model_defaults WHERE owner_id=?", (owner,)).fetchone()
        return {"models": [self.public(owner, json.loads(r[0])) for r in rows],
                "default_model_config_id": default[0] if default else None}

    def hydrate_env(self, owner, provider):
        """Explicit env import: create model config bound to this session. Only for the machine env owner."""
        with self.lock:
            key = env_api_key(provider)
            if not key:
                from fastapi import HTTPException
                raise HTTPException(422, "本项目环境中没有该提供商密钥")
            # Verify machine env owner.
            self.claim_env(owner, provider)
            base, model, protocol = PRESETS[provider]
            return self.save(owner, {
                "name": ENV_SEED_NAMES.get(provider, provider),
                "provider": provider,
                "protocol": protocol,
                "model": os.getenv(provider.upper() + "_MODEL", model),
                "base_url": os.getenv(provider.upper() + "_BASE_URL", base),
                "temperature": 1 if provider == "kimi" else 0,
                "max_tokens": 8192,
                "supports_temperature": True,
                "supports_tools": False,
                "tool_mode": os.getenv("AGENTLAB_TOOL_MODE", "json"),
                "input_price": None,
                "output_price": None,
                "api_key": key,
                "credential_source": "env_import",
                "env_provider": provider,
            })

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
            # credential_source: explicit only; never infer from provider name.
            if "credential_source" not in values:
                values["credential_source"] = "web" if key else (old or {}).get("credential_source", "legacy_unclassified")
            config = {**values, "id": config_id, "config_version": old["config_version"] + 1 if old else 1,
                      "credential_id": credential_id, "search": os.getenv("AGENTLAB_SEARCH", "wikipedia")}
            with self.store.connect() as db:
                db.execute("INSERT OR REPLACE INTO model_configs VALUES(?,?,?)", (config_id, owner, json.dumps(config)))
                db.execute("INSERT OR IGNORE INTO model_defaults VALUES(?,?)", (owner, config_id))
            return self.public(owner, config)

    def resolve(self, owner, snapshot):
        """Resolve credential ONLY from (owner, frozen credential_id). Never fall back to env."""
        with self.lock:
            grant_id = snapshot.get("integration_grant_id")
            if grant_id:
                grant = next((g for g in self.grants.values() if g["id"] == grant_id and g["owner_id"] == owner), None)
                if not grant:
                    from .providers import ProviderError
                    raise ProviderError("grant_revoked：联动授权已撤销，原任务已暂停。", "grant_revoked")
                if grant["expires"] <= time.time():
                    from .providers import ProviderError
                    raise ProviderError("grant_expired：联动授权已过期，原任务已暂停。", "grant_expired")
                if grant["model_config_id"] != snapshot.get("id") or grant["config_version"] != snapshot.get("config_version") or grant["credential_id"] != snapshot.get("credential_id"):
                    from .providers import ProviderError
                    raise ProviderError("grant_drift：联动授权绑定的模型版本已变化。", "grant_drift")
                from .providers import ProviderError
                try:
                    current = self.get(owner, grant["model_config_id"])
                except HTTPException:
                    raise ProviderError("grant_drift：授权绑定的模型已删除。", "grant_drift")
                if any(current.get(k) != grant.get(k) for k in ("config_version", "credential_id")):
                    raise ProviderError("grant_drift：当前模型已变化，请重新授权。", "grant_drift")
            cred_id = snapshot.get("credential_id")
            if not cred_id or cred_id == "missing":
                from .providers import ProviderError
                raise ProviderError("legacy_binding_required：该任务缺少凭证身份，需显式绑定模型后才能继续。不会自动替换原任务模型。", "legacy_binding_required")
            item = self.credentials.get((owner, cred_id))
            if not item or item["config_id"] != snapshot.get("id"):
                from .providers import ProviderError
                name = snapshot.get("provider", "model").upper()
                raise ProviderError(f"credential_missing：原任务密钥已删除或服务已重启。请在模型配置补充原凭证后继续（{name}_API_KEY / 网页密钥）。", "credential_missing")
            return item["key"]

    def delete_credentials(self, owner, config_id):
        self.get(owner, config_id)
        with self.lock:
            revoked = []
            for item in list(self.credentials):
                if item[0] == owner and self.credentials[item]["config_id"] == config_id:
                    revoked.append(item[1])
                    del self.credentials[item]
            # Record non-secret revocation state to prevent unintentional restoration.
            with self.store.connect() as db:
                for cred_id in revoked:
                    db.execute("INSERT OR REPLACE INTO revoked_credentials VALUES(?,?,?,?)", (owner, cred_id, config_id, time.time()))
        return {"revoked_credential_ids": revoked}

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
            # Explicit restore removes revocation state for this credential.
            with self.store.connect() as db:
                db.execute("DELETE FROM revoked_credentials WHERE owner_id=? AND credential_id=?", (owner, credential_id))
            self.credentials[owner, credential_id] = {"key": key, "config_id": config_id}
        return {"credential_id": credential_id, "configured": True}

    def is_revoked(self, owner, credential_id):
        with self.store.connect() as db:
            return bool(db.execute("SELECT 1 FROM revoked_credentials WHERE owner_id=? AND credential_id=?", (owner, credential_id)).fetchone())

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

    def claim_env(self, owner, provider=None):
        """Claim machine-wide env ownership. Single owner covers all providers."""
        with self.lock, self.store.connect() as db:
            existing = db.execute("SELECT owner_id, claimed_at FROM env_claims").fetchall()
            confirmed = [r for r in existing if r[1] > 0]
            if confirmed:
                if confirmed[0][0] != owner:
                    raise HTTPException(403, "本项目环境凭证已绑定另一个本地会话；请填写自己的网页密钥")
                # Same owner re-import is allowed.
                return
            if existing:
                raise HTTPException(409, "历史环境凭证归属冲突，需由原会话显式处理；普通认领不能覆盖历史记录")
            db.execute("INSERT OR REPLACE INTO env_claims VALUES(?,?)", (owner, time.time()))

    def release_env_claim(self, owner):
        """Release the machine env claim (for explicit repair / re-claim)."""
        with self.lock, self.store.connect() as db:
            db.execute("DELETE FROM env_claims WHERE owner_id=? AND claimed_at > 0", (owner,))

    def resolve_env_conflict(self, owner):
        """Explicit repair: owner takes over after reviewing multi-owner conflict."""
        with self.lock, self.store.connect() as db:
            rows = db.execute("SELECT owner_id,claimed_at FROM env_claims").fetchall()
            if not rows or any(r[1] > 0 for r in rows) or owner not in {r[0] for r in rows}:
                raise HTTPException(403, "仅冲突记录中的原会话可显式确认归属")
            db.execute("CREATE TABLE IF NOT EXISTS env_claim_audit(owner_id TEXT, resolved_by TEXT, resolved_at REAL)")
            db.executemany("INSERT INTO env_claim_audit VALUES(?,?,?)", [(r[0], owner, time.time()) for r in rows])
            db.execute("DELETE FROM env_claims")
            db.execute("INSERT OR REPLACE INTO env_claims VALUES(?,?)", (owner, time.time()))
        return {"resolved": True, "owner_id": owner}

    def unclaimed_history(self):
        with self.store.connect() as db:
            return db.execute("SELECT COUNT(*) FROM tasks WHERE json_extract(request,'$.owner_id') IS NULL").fetchone()[0]

    def claim_history(self, owner):
        with self.lock:
            with self.store.connect() as db:
                rows = db.execute("SELECT id,request FROM tasks WHERE json_extract(request,'$.owner_id') IS NULL").fetchall()
            if rows:
                self.claim_env(owner)
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
                              "temperature": frozen.get("temperature", req.get("temperature", 0)), "max_tokens": frozen.get("max_tokens", 8192),
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
        if not item:
            raise HTTPException(401, "联动授权无效或已撤销")
        if item["model_config_id"] != config_id:
            raise HTTPException(401, "联动授权不属于所选模型")
        if item["expires"] <= time.time():
            raise HTTPException(401, "联动授权已过期")
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
