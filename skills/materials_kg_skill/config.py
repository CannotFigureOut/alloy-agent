"""集中配置与共享资源。

所有外部连接参数都从环境变量（或 .env 文件）读取。可选依赖采用延迟导入，
因此即使尚未安装 neo4j / openai，也可以安全 import 本模块并查看配置状态。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # python-dotenv 是可选依赖，缺失时直接读系统环境变量
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


@dataclass
class Settings:
    """Skill 的全部可配置项。"""

    # ---- Neo4j ----
    neo4j_uri: str = field(
        default_factory=lambda: _env("NEO4J_URI", "bolt://localhost:7687") or "bolt://localhost:7687"
    )
    neo4j_user: str = field(default_factory=lambda: _env("NEO4J_USER", "neo4j") or "neo4j")
    neo4j_password: str = field(default_factory=lambda: _env("NEO4J_PASSWORD", "") or "", repr=False)
    neo4j_database: str | None = field(default_factory=lambda: _env("NEO4J_DATABASE") or None)

    # ---- LLM（OpenAI 或任意 OpenAI 兼容接口，如 DeepSeek） ----
    llm_api_key: str = field(
        default_factory=lambda: (_env("LLM_API_KEY") or _env("OPENAI_API_KEY") or ""), repr=False
    )
    llm_base_url: str | None = field(default_factory=lambda: _env("LLM_BASE_URL") or None)
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "gpt-4o-mini") or "gpt-4o-mini")
    embedding_model: str = field(
        default_factory=lambda: _env("EMBEDDING_MODEL", "text-embedding-3-small") or "text-embedding-3-small"
    )
    embedding_base_url: str | None = field(default_factory=lambda: _env("EMBEDDING_BASE_URL") or None)
    temperature: float = field(default_factory=lambda: float(_env("LLM_TEMPERATURE", "0") or 0))

    # ---- 路径与运行参数 ----
    paper_library_dir: Path = field(
        default_factory=lambda: Path(_env("PAPER_LIBRARY_DIR", "./paper_library") or "./paper_library")
    )
    pdf2md_skill_dir: Path | None = field(
        default_factory=lambda: (Path(p) if (p := _env("PDF2MD_SKILL_DIR")) else None)
    )
    chunk_size: int = field(default_factory=lambda: int(_env("CHUNK_SIZE", "1800") or 1800))
    chunk_overlap: int = field(default_factory=lambda: int(_env("CHUNK_OVERLAP", "200") or 200))
    top_k: int = field(default_factory=lambda: int(_env("TOP_K", "5") or 5))

    # ---- PDF 解析后端 ----
    pdf_backend: str = field(
        default_factory=lambda: (_env("PDF_BACKEND", "auto") or "auto").lower()
    )
    pdf_table_strategy: str = field(
        default_factory=lambda: (_env("PDF_TABLE_STRATEGY", "lines_strict") or "lines_strict").lower()
    )
    pdf_image_dpi: int = field(default_factory=lambda: int(_env("PDF_IMAGE_DPI", "150") or 150))
    pdf_write_images: bool = field(
        default_factory=lambda: (_env("PDF_WRITE_IMAGES", "true") or "true").strip().lower()
        in {"1", "true", "yes", "y", "on"}
    )

    @property
    def neo4j_configured(self) -> bool:
        return bool(self.neo4j_password and self.neo4j_uri)

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def pdf2md_available(self) -> bool:
        """方案 A 是否可用：配置了目录且转换脚本存在。"""
        if not self.pdf2md_skill_dir:
            return False
        scripts = self.pdf2md_skill_dir / "scripts"
        return (scripts / "md_convert.py").exists() or (scripts / "md_convert_zh.py").exists()

    def public_dict(self) -> dict[str, Any]:
        """脱敏后的配置，便于在界面 / 日志中展示。"""
        return {
            "NEO4J_URI": self.neo4j_uri,
            "NEO4J_USER": self.neo4j_user,
            "NEO4J_DATABASE": self.neo4j_database or "(default)",
            "NEO4J_PASSWORD": "***" if self.neo4j_password else "(未设置)",
            "LLM_BASE_URL": self.llm_base_url or "(OpenAI 默认)",
            "LLM_MODEL": self.llm_model,
            "EMBEDDING_MODEL": self.embedding_model,
            "LLM_API_KEY": "***" if self.llm_api_key else "(未设置)",
            "PAPER_LIBRARY_DIR": str(self.paper_library_dir),
            "PDF2MD_SKILL_DIR": str(self.pdf2md_skill_dir) if self.pdf2md_skill_dir else "(未配置)",
            "CHUNK_SIZE": self.chunk_size,
            "CHUNK_OVERLAP": self.chunk_overlap,
            "PDF_BACKEND": self.pdf_backend,
            "PDF_TABLE_STRATEGY": self.pdf_table_strategy,
            "PDF_WRITE_IMAGES": self.pdf_write_images,
            "PDF_IMAGE_DPI": self.pdf_image_dpi,
        }


_settings: Settings | None = None
_driver: Any = None
_llm_client: Any = None


def get_settings(refresh: bool = False) -> Settings:
    """获取全局配置单例。refresh=True 时重新读取环境变量。"""
    global _settings
    if _settings is None or refresh:
        _settings = Settings()
    return _settings


def get_driver() -> Any:
    """获取共享的 Neo4j driver（延迟导入）。未配置时抛出 RuntimeError。"""
    global _driver
    if _driver is None:
        s = get_settings()
        if not s.neo4j_configured:
            raise RuntimeError(
                "Neo4j 尚未配置。请设置 NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD，"
                "或复制 .env.example 为 .env 后填写。"
            )
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:
            raise RuntimeError("缺少依赖 neo4j，请先运行 pip install neo4j") from exc
        _driver = GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password))
    return _driver


def get_llm_client() -> Any:
    """获取共享的 OpenAI 兼容客户端（延迟导入，支持 DeepSeek 等）。"""
    global _llm_client
    if _llm_client is None:
        s = get_settings()
        if not s.llm_configured:
            raise RuntimeError(
                "LLM 尚未配置。请设置 LLM_API_KEY（以及可选的 LLM_BASE_URL / LLM_MODEL）。"
            )
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("缺少依赖 openai，请先运行 pip install openai") from exc
        kwargs: dict[str, Any] = {"api_key": s.llm_api_key}
        if s.llm_base_url:
            kwargs["base_url"] = s.llm_base_url
        _llm_client = OpenAI(**kwargs)
    return _llm_client


def close_resources() -> None:
    """关闭共享连接（程序退出或测试收尾时调用）。"""
    global _driver, _llm_client
    if _driver is not None:
        try:
            _driver.close()
        except Exception:  # noqa: BLE001
            pass
        _driver = None
    _llm_client = None
