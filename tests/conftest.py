"""
Конфигурация pytest — заглушки для модулей с внешними зависимостями.
"""
import sys
from unittest.mock import MagicMock, AsyncMock


# ── asyncpg ────────────────────────────────────────────────────────────────────
sys.modules.setdefault("asyncpg", MagicMock())

# ── db.py заглушка ─────────────────────────────────────────────────────────────
db_mock = MagicMock()
db_mock.ok.return_value = False
db_mock.subscription_get = AsyncMock(return_value=None)
sys.modules["db"] = db_mock

# ── aiogram — нужны реальные классы State/StatesGroup для декораторов ──────────
# Вместо полного мока — ставим заглушки только на то что не тестируем
class _State:
    def __set_name__(self, owner, name): self._name = name
    def __get__(self, obj, t): return self
    def __call__(self, *a, **kw): return self

class _StatesGroup:
    def __init_subclass__(cls, **kw):
        for k, v in list(cls.__dict__.items()):
            if isinstance(v, _State):
                setattr(cls, k, v)

_Router = MagicMock
_InlineKeyboardMarkup = MagicMock
_InlineKeyboardButton = MagicMock
_WebAppInfo = MagicMock
_Message = MagicMock
_CallbackQuery = MagicMock
_Bot = MagicMock

# aiogram.fsm.state
fsm_state_mock = MagicMock()
fsm_state_mock.State = _State
fsm_state_mock.StatesGroup = _StatesGroup
sys.modules["aiogram.fsm.state"] = fsm_state_mock

# aiogram.fsm.context
sys.modules["aiogram.fsm.context"] = MagicMock()

# aiogram.fsm.storage.memory
sys.modules["aiogram.fsm.storage.memory"] = MagicMock()

# aiogram.filters
sys.modules["aiogram.filters"] = MagicMock()

# aiogram.types — нужен InlineKeyboardMarkup настоящий для тестов меню
# Импортируем реальный aiogram если установлен, иначе мок
try:
    import aiogram.types as _real_types
    sys.modules.setdefault("aiogram.types", _real_types)
    import aiogram as _real_aiogram
    sys.modules.setdefault("aiogram", _real_aiogram)
except ImportError:
    sys.modules.setdefault("aiogram", MagicMock())
    sys.modules.setdefault("aiogram.types", MagicMock())
