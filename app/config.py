import logging
from functools import lru_cache
from typing import Literal

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from pydantic_settings import BaseSettings, SettingsConfigDict

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    llm_provider: Literal["openrouter", "openai", "ollama"] = "openrouter"
    embedding_provider: Literal["openrouter", "openai", "ollama"] = "openrouter"

    openrouter_api_key: str | None = None
    openrouter_model: str = "openai/gpt-4o-mini"
    openrouter_embedding_model: str = "openai/text-embedding-3-small"

    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    openai_embedding_model: str = "text-embedding-3-small"

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3"
    ollama_embedding_model: str = "nomic-embed-text"

    games_csv_path: str = "./data/games.csv"
    chroma_persist_dir: str = "./data/chroma"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_llm(settings: Settings | None = None) -> BaseChatModel:
    settings = settings or get_settings()
    provider = settings.llm_provider

    if provider == "openrouter":
        logger.info("LLM provider=openrouter model=%s", settings.openrouter_model)
        return ChatOpenAI(
            model=settings.openrouter_model,
            api_key=settings.openrouter_api_key,
            base_url=OPENROUTER_BASE_URL,
            temperature=0,
        )
    if provider == "openai":
        logger.info("LLM provider=openai model=%s", settings.openai_model)
        return ChatOpenAI(
            model=settings.openai_model,
            api_key=settings.openai_api_key,
            temperature=0,
        )
    logger.info("LLM provider=ollama model=%s base_url=%s", settings.ollama_model, settings.ollama_base_url)
    return ChatOpenAI(
        model=settings.ollama_model,
        api_key="ollama",
        base_url=f"{settings.ollama_base_url.rstrip('/')}/v1",
        temperature=0,
    )


def get_embeddings(settings: Settings | None = None) -> Embeddings:
    settings = settings or get_settings()
    provider = settings.embedding_provider

    if provider == "openrouter":
        logger.info(
            "Embedding provider=openrouter model=%s", settings.openrouter_embedding_model
        )
        return OpenAIEmbeddings(
            model=settings.openrouter_embedding_model,
            api_key=settings.openrouter_api_key,
            base_url=OPENROUTER_BASE_URL,
            check_embedding_ctx_length=False,
            model_kwargs={"encoding_format": "float"},
        )
    if provider == "openai":
        logger.info("Embedding provider=openai model=%s", settings.openai_embedding_model)
        return OpenAIEmbeddings(
            model=settings.openai_embedding_model,
            api_key=settings.openai_api_key,
        )
    logger.info(
        "Embedding provider=ollama model=%s base_url=%s",
        settings.ollama_embedding_model,
        settings.ollama_base_url,
    )
    return OpenAIEmbeddings(
        model=settings.ollama_embedding_model,
        api_key="ollama",
        base_url=f"{settings.ollama_base_url.rstrip('/')}/v1",
        check_embedding_ctx_length=False,
        model_kwargs={"encoding_format": "float"},
    )
