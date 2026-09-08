"""Константы интеграции Waviot."""
from __future__ import annotations

DOMAIN = "waviot"

CONF_LOGIN = "login"
CONF_JWT = "jwt"
CONF_REFRESH_TOKEN = "refresh_token"

API_BASE_URL = "https://api.waviot-home.ru"

# Основной опрос статусов всех устройств.
DEFAULT_UPDATE_INTERVAL = 600
# Отдельный, более частый опрос протечки ladoga — временное значение,
# пользователь попросил 20 сек на старте, увеличим позже.
LEAK_UPDATE_INTERVAL = 20

# Опрос wbox: адаптивный интервал — быстрый во время зарядки, обычный (как DEFAULT_UPDATE_INTERVAL)
# в режиме ожидания/не подключено.
CHARGER_CHARGING_UPDATE_INTERVAL = 30
CHARGER_IDLE_UPDATE_INTERVAL = DEFAULT_UPDATE_INTERVAL

HW_TYPE_PHOBOS = "phobos"
HW_TYPE_AQUA = "aqua"
HW_TYPE_GEFEST = "gefest"
HW_TYPE_BETAR = "betar"
HW_TYPE_LADOGA = "ladoga"
HW_TYPE_WALLHUB = "wallhub"
HW_TYPE_WBOX = "wbox"

ALL_HW_TYPES = (
    HW_TYPE_PHOBOS,
    HW_TYPE_AQUA,
    HW_TYPE_GEFEST,
    HW_TYPE_BETAR,
    HW_TYPE_LADOGA,
    HW_TYPE_WALLHUB,
    HW_TYPE_WBOX,
)

SUPPORTED_HW_TYPES = (
    HW_TYPE_PHOBOS,
    HW_TYPE_AQUA,
    HW_TYPE_GEFEST,
    HW_TYPE_BETAR,
    HW_TYPE_LADOGA,
    HW_TYPE_WALLHUB,
    HW_TYPE_WBOX,
)

# Состояния EVSE wbox (CHARGER_DEVICE_EVSE_SHORT_NEW_CMD.state).
EVSE_STATE_READY_TO_START = 1  # "Свободна" — разъём не подключён
EVSE_STATE_WAITING_START = 3  # подключено, готово к запуску
EVSE_STATE_CHARGING = 4  # идёт зарядка

# Состояния, в которых пользователю разрешено включать/выключать зарядку.
EVSE_TOGGLABLE_STATES = (EVSE_STATE_WAITING_START, EVSE_STATE_CHARGING)

_EVSE_ERROR_STATES = {
    5: "Состояние D",
    6: "Ошибка УЗО",
    7: "Потеряно заземление",
    8: "Перегрев",
    9: "Приостановлено",
    10: "Отключено",
}


def describe_evse_state(state: int) -> str:
    """Человекочитаемое описание состояния EVSE."""
    if state == EVSE_STATE_READY_TO_START:
        return "Свободна"
    if state == EVSE_STATE_WAITING_START:
        return "Ожидает старта"
    if state == EVSE_STATE_CHARGING:
        return "Заряжается"
    if state in _EVSE_ERROR_STATES:
        return _EVSE_ERROR_STATES[state]
    return "Неизвестно"


# Лимит мощности зарядки (number-сущность).
CHARGER_MIN_POWER_KW = 1.2
CHARGER_FALLBACK_MAX_POWER_KW = 10.0

# Charger (wbox) — своя эвристика офлайна, отличная от остальных устройств: rssi==50 -> 20 мин,
# иначе -> 120 мин; без common/time_saved -> offline.
CHARGER_ONLINE_SECONDS_RSSI_50 = 20 * 60
CHARGER_ONLINE_SECONDS_DEFAULT = 120 * 60


def detect_hw_type(hw_type: str) -> str | None:
    """Определить тип устройства Waviot по строке hw_type.

    Регистронезависимое совпадение по подстроке.
    """
    lowered = hw_type.lower()
    for kind in ALL_HW_TYPES:
        if kind in lowered:
            return kind
    return None
