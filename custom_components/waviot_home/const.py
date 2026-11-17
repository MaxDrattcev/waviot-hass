"""Константы интеграции Waviot."""
from __future__ import annotations

DOMAIN = "waviot_home"

CONF_LOGIN = "login"
CONF_JWT = "jwt"
CONF_REFRESH_TOKEN = "refresh_token"

API_BASE_URL = "https://api.waviot-home.ru"

# Пассивный опрос статусов всех устройств — единый интервал для всех координаторов.
# Раньше протечка и зарядка опрашивались чаще (20/30 сек) для быстрой реакции; теперь
# это делает events_client.py мгновенными событиями напрямую с гостевого AMQP-брокера,
# а поллинг остался только подстраховкой и полным обновлением состояния.
DEFAULT_UPDATE_INTERVAL = 900
LEAK_UPDATE_INTERVAL = DEFAULT_UPDATE_INTERVAL

# Раньше wbox опрашивался адаптивно (быстро во время зарядки) — теперь не нужно по той же
# причине, интервал всегда как у остальных.
CHARGER_CHARGING_UPDATE_INTERVAL = DEFAULT_UPDATE_INTERVAL
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

EVSE_ERROR_STATES = {
    5: "Состояние D",
    6: "Ошибка УЗО",
    7: "Потеряно заземление",
    8: "Перегрев",
    9: "Приостановлено",
    10: "Отключено",
}


def describe_evse_state(state: int) -> str:
    """Человекочитаемое описание состояния EVSE.

    Коды, не входящие в перечисленные, отдаются как "Неизвестно" — API их
    не документирует, и официальное приложение показывает их так же.
    """
    if state == EVSE_STATE_READY_TO_START:
        return "Свободна"
    if state == EVSE_STATE_WAITING_START:
        return "Ожидает старта"
    if state == EVSE_STATE_CHARGING:
        return "Заряжается"
    if state in EVSE_ERROR_STATES:
        return EVSE_ERROR_STATES[state]
    return "Неизвестно"


# Границы регулировки лимита мощности зарядки (number-сущность). Верхняя граница
# фиксированная: поле max_power_limit со стороны API меняется само по себе и как
# потолок диапазона не годится.
CHARGER_MIN_POWER_KW = 1.2
CHARGER_FALLBACK_MAX_POWER_KW = 10.0

# У wbox своя эвристика офлайна, отличная от остальных устройств: rssi==50 -> 20 мин,
# иначе -> 120 мин; без common/time_saved считаем офлайном.
CHARGER_ONLINE_SECONDS_RSSI_50 = 20 * 60
CHARGER_ONLINE_SECONDS_DEFAULT = 120 * 60


def detect_hw_type(hw_type: str) -> str | None:
    """Определить тип устройства Waviot по строке hw_type.

    Регистронезависимое совпадение по подстроке: API возвращает hw_type с
    суффиксами вида "wbox_nvt_wa_stm", точного равенства ждать нельзя.
    """
    lowered = hw_type.lower()
    for kind in ALL_HW_TYPES:
        if kind in lowered:
            return kind
    return None
