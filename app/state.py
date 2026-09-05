from langchain_core.language_models import BaseChatModel

from app.config import Settings, get_settings


class AppState:
    def __init__(self) -> None:
        self.settings: Settings = get_settings()
        self.indexed_games: int = 0
        self.indexing_ok: bool = False
        self.index_stale: bool = False
        self.llm: BaseChatModel | None = None


app_state = AppState()
