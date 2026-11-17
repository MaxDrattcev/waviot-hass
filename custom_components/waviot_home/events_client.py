"""Клиент мгновенных событий: прямое подключение к гостевому AMQP WAVIoT.

ВРЕМЕННОЕ РЕШЕНИЕ (так решило руководство, осознанно). Подключаемся под общей
гостевой учёткой `api_guest`/`waviot` (та же, что в публичной статье KB WAVIoT
№87 "AMQP API"), создаём гостевую очередь командой `create_queue` на свой
`api_key` и слушаем её напрямую — без отдельного сервиса-посредника (`events-gw`,
см. его репозиторий, пока не используется).

ВАЖНО: `api_key` в `create_queue` сервер (`serversoft`) не сверяет с личностью
вызывающего — изоляция по пользователю на этом пути не гарантирована протоколом,
только тем, что мы сами просим именно свой `api_key`. Если это когда-то станет
проблемой — нужно либо дождаться, пока платформа включит проверку на своей
стороне, либо перейти на выдачу персональных очередей по JWT (план обсуждался,
отложен).

Без кэша и без ACK: пока соединения не было, события просто не приходят —
обычный поллинг координаторов (see coordinator.py) остаётся источником истины,
этот клиент только ускоряет обновление между опросами.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import uuid
from typing import Any

import aio_pika

from .account import WaviotAccount
from .const import (
    EVSE_ERROR_STATES,
    EVSE_STATE_CHARGING,
    EVSE_STATE_WAITING_START,
)
from .coordinator import WaviotChargingCoordinator, WaviotLeakCoordinator, WaviotMainCoordinator
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

RECONNECT_DELAY_MIN = 5  # секунд между попытками переподключения
RECONNECT_DELAY_MAX = 300  # потолок бэкоффа — не долбить брокер время простоя
_WARN_AFTER_CONSECUTIVE_FAILURES = 5  # после стольких подряд — поднять до warning

# Гостевая учётка брокера WAVIoT — те же данные, что в публичной статье KB №87
# "AMQP API" (api_guest/waviot). Это официально документированный публичный
# доступ, не внутренний секрет.
_RABBIT_GUEST_URL = "amqp://api_guest:waviot@waviot.dom-ik.ru:5672/%2Fapi"
_RABBIT_EXCHANGE = "strij.api"
_RABBIT_CMD_ROUTING_KEY = "cmd.in.serversoft"
_QUEUE_EXPIRES_SECONDS = 3600
_QUEUE_DECLARE_ATTEMPTS = 10
_QUEUE_DECLARE_RETRY_DELAY = 0.5

# Коды из журнала событий Фобоса (0.0.96.3.10.255_2 / журнал type=2).
_PHOBOS_EVENT_RELAY_OFF = 518
_PHOBOS_EVENT_RELAY_ON = 519

# timer0_action — код действия (1=выкл, 2=вкл), а не булево состояние. Важно не
# путать с "любое ненулевое = включено": 1 (выкл) тоже ненулевое.
_PHOBOS_TIMER0_OFF = 1
_PHOBOS_TIMER0_ON = 2

# Коды протечки Ладоги по умолчанию; при несовпадении — запасной разбор по Message.
_LADOGA_LEAK_CODE = 1
_LADOGA_DRY_CODE = 0

_OBIS_PHOBOS_RELAY_STATE = "0_0_96_3_10_255_2"
_OBIS_PHOBOS_TIMER0 = "timer0_action"
_OBIS_CHARGER_STATE = "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD_state"
_OBIS_CHARGER_RELAY = "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD_relay"

# OBIS -> ключ в статусе координатора зарядки (см. parser.py parse_device_status).
# Точное совпадение — "max_power_limit" содержит подстроку "max_power".
_CHARGER_POWER_FIELD_BY_OBIS: dict[str, str] = {
    "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD_inst_power": "power",
    "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD_max_power": "max_power",
    "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD_max_power_limit": "max_power_limit",
    "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD_count_power": "meter",
}


def _mask_queue_name(queue_name: str) -> str:
    """Прячет api_key из имени гостевой очереди ("guest.<api_key>.<suffix>") перед
    логированием — секрет аккаунта не должен попадать в лог целиком."""
    parts = queue_name.split(".")
    if len(parts) >= 3 and parts[0] == "guest":
        return f"guest.***{parts[1][-4:]}.{parts[2]}"
    return queue_name


def _mask_routing_key(routing_key: str) -> str:
    """Вырезает первый сегмент routing key (api_key) перед логированием."""
    i = routing_key.find(".")
    return routing_key[i:] if i >= 0 else routing_key


def _hw_type_from_routing_key(routing_key: str) -> str:
    """Достаёт сегмент сразу после "ul_eav": "<api_key>.<n>.ul_eav.<hw_type>..."."""
    parts = routing_key.split(".")
    for i, part in enumerate(parts):
        if part == "ul_eav" and i + 1 < len(parts):
            return parts[i + 1]
    _LOGGER.debug("rabbit: в routing key нет сегмента ul_eav: ...%s", _mask_routing_key(routing_key))
    return ""


def _eav_value_as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _resolve_leak(code: Any, message: Any) -> bool | None:
    if code == _LADOGA_LEAK_CODE:
        return True
    if code == _LADOGA_DRY_CODE:
        return False
    if message == "leakage_detected":
        return True
    if message == "leakage_fixed":
        return False
    return None


def _resolve_phobos_relay_event(code: Any) -> bool | None:
    if code == _PHOBOS_EVENT_RELAY_OFF:
        return False
    if code == _PHOBOS_EVENT_RELAY_ON:
        return True
    return None


def _parse_event_body(hw_type: str, raw: dict[str, Any]) -> dict[str, Any] | None:
    """Тело ASB_EVENT (журнал событий: протечка, реле Фобоса)."""
    modem_id = raw.get("modem_id")
    if modem_id is None:
        return None
    code = raw.get("event_code")
    hw = hw_type.lower()

    if "ladoga" in hw:
        leak = _resolve_leak(code, raw.get("message"))
        if leak is None:
            return None
        return {"kind": "leak", "modem_id": str(modem_id), "bool": leak}

    if "phobos" in hw:
        on = _resolve_phobos_relay_event(code)
        if on is None:
            return None
        return {"kind": "relay", "modem_id": str(modem_id), "bool": on}

    return None


def _parse_eav_body(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Тело ASB_EAV (сырые показания по OBIS: реле, зарядка)."""
    modem_id = raw.get("modem_id")
    if modem_id is None:
        return None
    obis = raw.get("obis_code") or ""
    value = raw.get("value")
    modem_id = str(modem_id)

    if obis == _OBIS_PHOBOS_RELAY_STATE:
        return {"kind": "relay", "modem_id": modem_id, "bool": _eav_value_as_float(value) != 0}

    if obis == _OBIS_PHOBOS_TIMER0:
        action = int(_eav_value_as_float(value))
        if action == _PHOBOS_TIMER0_OFF:
            return {"kind": "relay", "modem_id": modem_id, "bool": False}
        if action == _PHOBOS_TIMER0_ON:
            return {"kind": "relay", "modem_id": modem_id, "bool": True}
        return None

    if obis == _OBIS_CHARGER_STATE:
        return {"kind": "charger_state", "modem_id": modem_id, "value": value}

    if obis in _CHARGER_POWER_FIELD_BY_OBIS:
        return {"kind": "charger_power", "modem_id": modem_id, "obis_code": obis, "value": value}

    if obis == _OBIS_CHARGER_RELAY:
        return {"kind": "charger_relay", "modem_id": modem_id, "bool": _eav_value_as_float(value) != 0}

    return None


def parse_delivery(routing_key: str, body: bytes) -> dict[str, Any] | None:
    """Разбирает одно сырое сообщение с exchange strij.api в нормализованное событие.

    None — сообщение не распознано (неизвестный формат тела или устройство/код);
    это не ошибка, такие молча пропускаются.
    """
    # Bind теперь "<api_key>.#" (ловит вообще всё для ключа) — большая часть трафика
    # (ul_app/ul_roll/dl_queued/dl_inprocess/dl_success и т.п.) нам не нужна вообще.
    # Дешёвая проверка по routing key до JSON.loads, чтобы не парсить тело зря.
    if "ul_eav" not in routing_key and ".event." not in routing_key:
        return None
    try:
        raw = json.loads(body)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None

    if "event_code" in raw:
        return _parse_event_body(_hw_type_from_routing_key(routing_key), raw)
    if "obis_code" in raw:
        return _parse_eav_body(raw)
    return None


def api_key_from_jwt(jwt: str) -> str | None:
    """Достаёт свой же api_key из уже имеющегося у аккаунта JWT (claim apiKeys).

    Подпись не проверяем — токен уже наш собственный, действующий (им только что
    успешно пользовался account для обычных запросов к облаку).
    """
    parts = jwt.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    payload = payload.translate(str.maketrans("+/", "-_"))
    try:
        decoded = base64.urlsafe_b64decode(payload)
        claims = json.loads(decoded)
    except (ValueError, UnicodeDecodeError):
        return None
    keys = claims.get("apiKeys")
    if not keys:
        return None
    return keys[0]


class WaviotEventsClient:
    """Держит соединение к гостевому AMQP-брокеру и обновляет координаторы на лету."""

    def __init__(
        self,
        hass: HomeAssistant,
        account: WaviotAccount,
        main_coordinator: WaviotMainCoordinator,
        leak_coordinator: WaviotLeakCoordinator | None,
        charging_coordinator: WaviotChargingCoordinator | None,
    ) -> None:
        self._hass = hass
        self._account = account
        self._main_coordinator = main_coordinator
        self._leak_coordinator = leak_coordinator
        self._charging_coordinator = charging_coordinator
        self._task: asyncio.Task | None = None
        self._stopped = False
        self._connection: aio_pika.abc.AbstractConnection | None = None
        self._consecutive_failures = 0

    def async_start(self) -> None:
        self._stopped = False
        self._task = self._hass.loop.create_task(self._run(), name="waviot_home_events_client")

    async def async_stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._connection is not None and not self._connection.is_closed:
            await self._connection.close()

    async def _run(self) -> None:
        while not self._stopped:
            connected = await self._connect_once()
            if self._stopped:
                return

            if connected:
                # Соединение было установлено и разорвалось уже после (сеть/брокер) —
                # не штраф за постоянную проблему, сбрасываем бэкофф.
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1

            delay = min(
                RECONNECT_DELAY_MIN * (2 ** min(self._consecutive_failures, 6)),
                RECONNECT_DELAY_MAX,
            )
            if self._consecutive_failures == _WARN_AFTER_CONSECUTIVE_FAILURES:
                _LOGGER.warning(
                    "rabbit: не удаётся подключиться уже %d попыток подряд — "
                    "мгновенные события недоступны, работает только обычный опрос",
                    self._consecutive_failures,
                )
            await asyncio.sleep(delay)

    async def _connect_once(self) -> bool:
        """Возвращает True, если соединение было успешно установлено (хотя бы подключились
        и завели очередь) — используется в _run, чтобы отличить "не смогли подключиться
        вообще" (авторастущий бэкофф) от "подключились, потом порвалось" (бэкофф сбрасывается)."""
        api_key = api_key_from_jwt(self._account.jwt)
        if not api_key:
            _LOGGER.warning("rabbit: не удалось получить api_key из токена")
            return False

        connected = False
        connection: aio_pika.abc.AbstractConnection | None = None
        try:
            connection = await aio_pika.connect(_RABBIT_GUEST_URL)
            self._connection = connection
            queue_suffix = uuid.uuid4().hex[:12]
            await self._send_create_queue(connection, api_key, queue_suffix)

            queue_name = f"guest.{api_key}.{queue_suffix}"
            channel, queue = await self._declare_queue_with_retry(connection, queue_name)
            if queue is None:
                _LOGGER.debug("rabbit: гостевая очередь %s не появилась", _mask_queue_name(queue_name))
                return False
            await channel.set_qos(prefetch_count=20)

            _LOGGER.debug("rabbit: подключён, слушаю %s", _mask_queue_name(queue_name))
            connected = True
            async with queue.iterator() as messages:
                async for message in messages:
                    async with message.process():
                        self._handle_delivery(message.routing_key, message.body)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 — обрыв сети не должен ронять интеграцию
            _LOGGER.debug("rabbit: соединение прервано: %s", err)
            return connected
        finally:
            if connection is not None and not connection.is_closed:
                await connection.close()
        return connected

    @staticmethod
    async def _send_create_queue(
        connection: aio_pika.abc.AbstractConnection, api_key: str, queue_suffix: str
    ) -> None:
        channel = await connection.channel()
        try:
            exchange = await channel.declare_exchange(
                _RABBIT_EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True
            )
            body = json.dumps(
                {
                    "msg_type": "create_queue",
                    # Не завязываемся на внутреннюю форму routing key после api_key: она
                    # уже менялась незаметно (у Ладоги/Фобоса второй сегмент "0", у
                    # зарядки — "4") и может измениться снова для нового типа устройства.
                    # "#" — всё для нашего api_key; ненужное (ul_app/ul_roll/dl_*) отсеивается
                    # дёшево в parse_delivery до разбора JSON, без потери по bind на брокере.
                    "bind": "#",
                    "api_key": api_key,
                    "max_length": 1000,
                    "expires": _QUEUE_EXPIRES_SECONDS,
                    "name": queue_suffix,
                }
            ).encode()
            await exchange.publish(aio_pika.Message(body=body), routing_key=_RABBIT_CMD_ROUTING_KEY)
        finally:
            await channel.close()

    @staticmethod
    async def _declare_queue_with_retry(
        connection: aio_pika.abc.AbstractConnection, queue_name: str
    ) -> tuple[aio_pika.abc.AbstractChannel | None, aio_pika.abc.AbstractQueue | None]:
        """create_queue исполняется сервером не мгновенно — ждём, пока очередь появится.

        Каждая попытка на новом Channel: неудачный passive-declare закрывает канал
        по правилам AMQP, повторно использовать его нельзя.
        """
        for _ in range(_QUEUE_DECLARE_ATTEMPTS):
            channel = await connection.channel()
            try:
                queue = await channel.declare_queue(
                    queue_name, durable=True, auto_delete=True, passive=True
                )
                return channel, queue
            except Exception as err:  # noqa: BLE001 — любая ошибка declare = попробовать ещё раз
                _LOGGER.debug("rabbit: очередь %s ещё не готова: %s", _mask_queue_name(queue_name), err)
                await asyncio.sleep(_QUEUE_DECLARE_RETRY_DELAY)
        return None, None

    def _handle_delivery(self, routing_key: str, body: bytes) -> None:
        payload = parse_delivery(routing_key, body)
        if payload is None:
            _LOGGER.debug(
                "rabbit: не распознано rk=...%s body=%s", _mask_routing_key(routing_key), body
            )
            return
        _LOGGER.debug("rabbit: разобрано rk=...%s -> %s", _mask_routing_key(routing_key), payload)
        self._handle_event(payload)

    def _handle_event(self, payload: dict[str, Any]) -> None:
        modem_id = payload.get("modem_id")
        kind = payload.get("kind")
        if modem_id is None or kind is None:
            return

        apply = self._APPLY_BY_KIND.get(kind)
        if apply is None:
            _LOGGER.debug("rabbit: неизвестный kind=%s payload=%s", kind, payload)
            return
        apply(self, modem_id, payload)

    def _apply_leak(self, modem_id: str, payload: dict[str, Any]) -> None:
        if self._leak_coordinator is None:
            return
        value = payload.get("bool")
        if value is None:
            return
        data = dict(self._leak_coordinator.data or {})
        data[modem_id] = bool(value)
        self._leak_coordinator.async_set_updated_data(data)

    def _apply_relay(self, modem_id: str, payload: dict[str, Any]) -> None:
        value = payload.get("bool")
        if value is None:
            return
        self._update_coordinator_status(self._main_coordinator, modem_id, {"on": bool(value)})

    def _apply_charger_state(self, modem_id: str, payload: dict[str, Any]) -> None:
        if self._charging_coordinator is None:
            return
        try:
            state = int(payload.get("value"))
        except (TypeError, ValueError):
            return
        self._update_coordinator_status(
            self._charging_coordinator,
            modem_id,
            {
                "state": state,
                "on": state == EVSE_STATE_CHARGING,
                "cable_connected": state in (EVSE_STATE_WAITING_START, EVSE_STATE_CHARGING),
                "problem": state in EVSE_ERROR_STATES,
            },
        )

    def _apply_charger_power(self, modem_id: str, payload: dict[str, Any]) -> None:
        if self._charging_coordinator is None:
            return
        value = payload.get("value")
        if value is None:
            return
        field = _CHARGER_POWER_FIELD_BY_OBIS.get(payload.get("obis_code") or "")
        if field is None:
            return
        try:
            number = float(value)
        except (TypeError, ValueError):
            return
        self._update_coordinator_status(self._charging_coordinator, modem_id, {field: number})

    def _apply_charger_relay(self, modem_id: str, payload: dict[str, Any]) -> None:
        if self._charging_coordinator is None:
            return
        value = payload.get("bool")
        if value is None:
            return
        self._update_coordinator_status(self._charging_coordinator, modem_id, {"on": bool(value)})

    @staticmethod
    def _update_coordinator_status(coordinator: Any, modem_id: str, update: dict[str, Any]) -> None:
        data = dict(coordinator.data or {})
        current = dict(data.get(modem_id) or {})
        current.update(update)
        data[modem_id] = current
        coordinator.async_set_updated_data(data)

    _APPLY_BY_KIND: dict[str, Any] = {
        "leak": _apply_leak,
        "relay": _apply_relay,
        "charger_state": _apply_charger_state,
        "charger_power": _apply_charger_power,
        "charger_relay": _apply_charger_relay,
    }
