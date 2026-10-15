from typing import Literal
from pydantic import BaseModel, Field, model_validator, field_validator

class ReplayRequest(BaseModel):
    search_snapshot: dict | None = None

    @field_validator('search_snapshot')
    @classmethod
    def valid_retrieval_snapshot(cls,value):
        if value is not None:
            import json
            from .search_replay import validate
            if len(json.dumps(value))>500000:raise ValueError('检索快照最多 500KB')
            validate(value)
        return value

Provider = Literal["qwen", "kimi", "deepseek", "glm", "xiaomi", "openai", "anthropic", "custom"]


class TaskRequest(ReplayRequest):
    question: str = Field(min_length=3, max_length=4000)
    execution_mode: Literal["mock", "live"] = "mock"
    provider: Provider = "qwen"
    model_config_id: str | None = Field(default=None, max_length=100)
    mode: Literal["single", "multi"] = "multi"
    review_plan: bool = True
    max_iterations: int = Field(default=5, ge=1, le=8)
    max_calls: int = Field(default=24, ge=1, le=40)
    timeout_seconds: int = Field(default=300, ge=5, le=1800)
    parent_id: str | None = None
    research_instructions: str = Field(default="", max_length=12000)
    temperature: float = Field(default=0, ge=0, le=2)
    max_tokens: int = Field(default=8192, ge=1, le=65536)
    mock_scenario: Literal["standard", "revise", "search_more", "clarify"] = "standard"


class ResumeRequest(BaseModel):
    direction: str = Field(default="", max_length=4000)
    plan: list[str] | None = Field(default=None, min_length=1, max_length=8)


class BenchmarkRequest(ReplayRequest):
    task_id: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=3, max_length=4000)
    mode: Literal["single", "multi"] = "multi"
    execution_mode: Literal["mock", "live"] = "mock"
    provider: Provider = "qwen"
    model_config_id: str | None = Field(default=None, max_length=100)
    expected_config_version: int | None = Field(default=None, ge=1)
    max_calls: int = Field(default=24, ge=1, le=40)
    timeout_seconds: int = Field(default=300, ge=5, le=1800)
    research_instructions: str = Field(default="", max_length=12000)
    temperature: float = Field(default=0, ge=0, le=2)
    max_tokens: int = Field(default=8192, ge=1, le=65536)


class ModelConfigRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: Provider
    protocol: Literal["openai", "anthropic"] = "openai"
    model: str = Field(min_length=1, max_length=200, pattern=r"^[^\s]+$")
    base_url: str = Field(min_length=1, max_length=2000)
    api_key: str | None = Field(default=None, min_length=1, max_length=4096)
    temperature: float = Field(default=0, ge=0, le=2)
    max_tokens: int = Field(default=8192, ge=1, le=65536)
    supports_tools: bool = False
    supports_temperature: bool = True
    tool_mode: Literal["json", "native"] = "json"
    input_price: float | None = Field(default=None, ge=0)
    output_price: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def capabilities(self):
        if self.provider == "anthropic" and self.protocol != "anthropic":
            raise ValueError("Anthropic 提供商需使用 Anthropic messages 协议")
        if self.provider not in {"anthropic", "custom"} and self.protocol != "openai":
            raise ValueError("当前提供商需使用 OpenAI 兼容协议")
        if self.protocol == "anthropic" and self.temperature > 1:
            raise ValueError("Anthropic temperature 范围为 0–1")
        if self.tool_mode == "native" and not self.supports_tools:
            raise ValueError("原生工具模式需声明模型支持工具调用")
        if self.api_key and ("\n" in self.api_key or "\r" in self.api_key):
            raise ValueError("密钥不得包含换行")
        return self


class GrantRequest(BaseModel):
    model_config_id: str = Field(min_length=1, max_length=100)


class CredentialRequest(BaseModel):
    credential_id: str = Field(min_length=1, max_length=100)
    api_key: str = Field(min_length=1, max_length=4096)
