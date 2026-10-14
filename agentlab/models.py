from typing import Literal
from pydantic import BaseModel, Field


class TaskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=4000)
    execution_mode: Literal["mock", "live"] = "mock"
    provider: Literal["qwen", "kimi"] = "qwen"
    mode: Literal["single", "multi"] = "multi"
    review_plan: bool = True
    max_iterations: int = Field(default=3, ge=1, le=8)
    max_calls: int = Field(default=12, ge=1, le=40)
    timeout_seconds: int = Field(default=120, ge=5, le=600)
    parent_id: str | None = None
    research_instructions: str = Field(default="", max_length=12000)
    temperature: float = Field(default=0, ge=0, le=2)
    mock_scenario: Literal["standard", "revise", "search_more", "clarify"] = "standard"


class ResumeRequest(BaseModel):
    direction: str = Field(default="", max_length=4000)
    plan: list[str] | None = Field(default=None, min_length=1, max_length=8)


class BenchmarkRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=3, max_length=4000)
    mode: Literal["single", "multi"] = "multi"
    execution_mode: Literal["mock", "live"] = "mock"
    provider: Literal["qwen", "kimi"] = "qwen"
    max_calls: int = Field(default=12, ge=1, le=40)
    timeout_seconds: int = Field(default=120, ge=5, le=600)
    research_instructions: str = Field(default="", max_length=12000)
    temperature: float = Field(default=0, ge=0, le=2)
