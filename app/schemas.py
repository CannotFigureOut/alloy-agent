from typing import Literal
from pydantic import BaseModel, Field, field_validator

class Goals(BaseModel):
    tensile: float = Field(320, gt=0, le=2000, description='抗拉强度 MPa，严格大于')
    yield_strength: float = Field(180, gt=0, le=2000, description='屈服强度 MPa，严格大于')
    elongation: float = Field(12, gt=0, le=100, description='伸长率 %，严格大于')

class RunInput(BaseModel):
    session_id: str = Field(min_length=1, max_length=100, pattern=r'^[a-zA-Z0-9_-]+$')
    message: str = Field(min_length=1, max_length=6000)
    scenario: Literal['防撞梁', '电池壳体', '车身结构件'] | None = None
    goals: Goals | None = None
    rounds: int = Field(2, ge=2, le=3)

class NewSession(BaseModel):
    title: str = Field('铝合金设计会话', min_length=1, max_length=100)

class PlanConfirmation(BaseModel):
    candidate_id: str = Field(min_length=1,max_length=100)

class ModelConfiguration(BaseModel):
    base_url: str = Field(min_length=1,max_length=2000)
    api_key: str = Field('',max_length=4096)
    model_name: str = Field(min_length=1,max_length=200)

    @field_validator('base_url')
    @classmethod
    def validate_url(cls,value):
        from urllib.parse import urlsplit
        value=value.strip().rstrip('/')
        url=urlsplit(value)
        if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('请输入 http/https API 基础地址，不含凭证、查询参数或片段')
        return value

    @field_validator('model_name','api_key')
    @classmethod
    def validate_text(cls,value):
        value=value.strip()
        if any(c in value for c in ('\r','\n','\x00')):raise ValueError('配置不能包含换行或空字符')
        return value
