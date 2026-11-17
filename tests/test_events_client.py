"""Тесты разбора событий гостевого AMQP — без реального брокера, только чистая логика.

Тела и routing key — те же, что реально приходили при живой проверке через events-gw
(см. events-gw/HANDOFF.md), чтобы не разойтись с уже проверенным форматом.
"""
import base64
import json

from custom_components.waviot_home.events_client import (
    _mask_queue_name,
    api_key_from_jwt,
    parse_delivery,
)


def _fake_jwt(api_keys: list[str] | None) -> str:
    def enc(v: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(v).encode()).rstrip(b"=").decode()

    header = enc({"alg": "none"})
    payload = {"userId": "u1"}
    if api_keys is not None:
        payload["apiKeys"] = api_keys
    return f"{header}.{enc(payload)}.sig"


def test_api_key_from_jwt_reads_first_key():
    jwt = _fake_jwt(["ak-1", "ak-2"])
    assert api_key_from_jwt(jwt) == "ak-1"


def test_api_key_from_jwt_returns_none_without_claim():
    jwt = _fake_jwt(None)
    assert api_key_from_jwt(jwt) is None


def test_api_key_from_jwt_returns_none_for_malformed_token():
    assert api_key_from_jwt("not-a-jwt") is None


def test_parse_ladoga_leak_detected():
    rk = "APIKEY.0.ul_eav.ladoga_stm.event.1.12055965"
    body = json.dumps(
        {"event_code": 1, "message": "leakage_detected", "modem_id": 12055965, "timestamp": 1794578508}
    ).encode()

    event = parse_delivery(rk, body)

    assert event == {"kind": "leak", "modem_id": "12055965", "bool": True}


def test_parse_ladoga_leak_fixed_by_message_fallback():
    """Реальный прибор прислал code=2, а не "сухой" 0 из дефолтных правил — разбор
    должен упасть на запасной путь по Message, а не промолчать."""
    rk = "APIKEY.0.ul_eav.ladoga_stm.event.2.12055965"
    body = json.dumps({"event_code": 2, "message": "leakage_fixed", "modem_id": 12055965}).encode()

    event = parse_delivery(rk, body)

    assert event == {"kind": "leak", "modem_id": "12055965", "bool": False}


def test_parse_phobos_relay_on_event_code():
    rk = "APIKEY.0.ul_eav.phobos3L_dualcore_stm.event.519.11773968"
    body = json.dumps({"event_code": 519, "modem_id": 11773968, "timestamp": 1794578523}).encode()

    event = parse_delivery(rk, body)

    assert event == {"kind": "relay", "modem_id": "11773968", "bool": True}


def test_parse_phobos_relay_off_event_code():
    rk = "APIKEY.0.ul_eav.phobos3L_dualcore_stm.event.518.11773968"
    body = json.dumps({"event_code": 518, "modem_id": 11773968}).encode()

    event = parse_delivery(rk, body)

    assert event == {"kind": "relay", "modem_id": "11773968", "bool": False}


def test_parse_phobos_timer0_action_off_is_not_any_nonzero():
    """Живой баг: timer0_action — код действия (1=выкл, 2=вкл), не булево 0/1.
    Раньше "любое ненулевое = включено" ломало состояние после нескольких быстрых
    переключений подряд (реле залипало на "включено")."""
    rk = "APIKEY.0.ul_eav.phobos3L_dualcore_stm.electro5.setting.timer0_action.11773968"
    body = json.dumps({"modem_id": 11773968, "obis_code": "timer0_action", "value": 1}).encode()

    event = parse_delivery(rk, body)

    assert event == {"kind": "relay", "modem_id": "11773968", "bool": False}


def test_parse_phobos_timer0_action_on():
    rk = "APIKEY.0.ul_eav.phobos3L_dualcore_stm.electro5.setting.timer0_action.11773968"
    body = json.dumps({"modem_id": 11773968, "obis_code": "timer0_action", "value": 2}).encode()

    event = parse_delivery(rk, body)

    assert event == {"kind": "relay", "modem_id": "11773968", "bool": True}


def test_parse_phobos_timer0_action_unknown_value_is_skipped():
    rk = "APIKEY.0.ul_eav.phobos3L_dualcore_stm.electro5.setting.timer0_action.11773968"
    body = json.dumps({"modem_id": 11773968, "obis_code": "timer0_action", "value": 0}).encode()

    assert parse_delivery(rk, body) is None


def test_parse_phobos_relay_state_eav_is_boolean_not_action_code():
    rk = "APIKEY.0.ul_eav.phobos3L_dualcore_stm.electro5.query.0_0_96_3_10_255_2.11773968"
    body = json.dumps({"modem_id": 11773968, "obis_code": "0_0_96_3_10_255_2", "value": 1}).encode()

    event = parse_delivery(rk, body)

    assert event == {"kind": "relay", "modem_id": "11773968", "bool": True}


def test_parse_charger_inst_power():
    rk = "APIKEY.0.ul_eav.wbox_nvt_wa_stm.ev.tag.CHARGER_DEVICE_EVSE_SHORT_NEW_CMD_inst_power.8456524"
    body = json.dumps(
        {"modem_id": 8456524, "obis_code": "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD_inst_power", "value": 3500}
    ).encode()

    event = parse_delivery(rk, body)

    assert event == {
        "kind": "charger_power",
        "modem_id": "8456524",
        "obis_code": "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD_inst_power",
        "value": 3500,
    }


def test_parse_charger_max_power_limit_is_distinct_from_max_power():
    """max_power_limit содержит подстроку "max_power" — раньше substring-матчинг
    (и в Go, и тут) ловил обоими одной веткой и затирал max_power значением потолка."""
    rk = "APIKEY.0.ul_eav.wbox_nvt_wa_stm.ev.tag.CHARGER_DEVICE_EVSE_SHORT_NEW_CMD_max_power_limit.8456524"
    body = json.dumps(
        {"modem_id": 8456524, "obis_code": "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD_max_power_limit", "value": 11000}
    ).encode()

    event = parse_delivery(rk, body)

    assert event["kind"] == "charger_power"
    assert event["obis_code"] == "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD_max_power_limit"
    assert event["value"] == 11000


def test_parse_charger_state():
    rk = "APIKEY.0.ul_eav.wbox_nvt_wa_stm.ev.tag.CHARGER_DEVICE_EVSE_SHORT_NEW_CMD_state.8456524"
    body = json.dumps(
        {"modem_id": 8456524, "obis_code": "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD_state", "value": 4}
    ).encode()

    event = parse_delivery(rk, body)

    assert event == {"kind": "charger_state", "modem_id": "8456524", "value": 4}


def test_parse_charger_state_with_nonzero_n_segment():
    """Живой баг: у зарядки второй сегмент routing key — "4", а не "0", как у Ладоги/
    Фобоса. bind был жёстко "0.ul_eav.#" и события зарядки вообще не долетали до
    очереди. Разбор от этого сегмента не зависит — фиксируем это явно."""
    rk = "APIKEY.4.ul_eav.wbox_nvt_wa_stm.electro5.setting.CHARGER_DEVICE_EVSE_SHORT_NEW_CMD_state.11968025"
    body = json.dumps(
        {"modem_id": 11968025, "obis_code": "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD_state", "value": 3}
    ).encode()

    event = parse_delivery(rk, body)

    assert event == {"kind": "charger_state", "modem_id": "11968025", "value": 3}


def test_parse_charger_relay():
    rk = "APIKEY.0.ul_eav.wbox_nvt_wa_stm.ev.tag.CHARGER_DEVICE_EVSE_SHORT_NEW_CMD_relay.8456524"
    body = json.dumps(
        {"modem_id": 8456524, "obis_code": "CHARGER_DEVICE_EVSE_SHORT_NEW_CMD_relay", "value": 1}
    ).encode()

    event = parse_delivery(rk, body)

    assert event == {"kind": "charger_relay", "modem_id": "8456524", "bool": True}


def test_parse_unknown_eav_obis_is_skipped():
    rk = "APIKEY.0.ul_eav.wallhub_stm.electro5.setting.SSERIAL_CMD_TEMPERATURE_humidity.12442918"
    body = json.dumps(
        {"modem_id": 12442918, "obis_code": "SSERIAL_CMD_TEMPERATURE_humidity", "value": 55}
    ).encode()

    assert parse_delivery(rk, body) is None


def test_parse_invalid_json_is_skipped():
    assert parse_delivery("APIKEY.0.ul_eav.x.event.1.1", b"not json") is None


def test_parse_unknown_body_shape_is_skipped():
    assert parse_delivery("APIKEY.0.ul_eav.x.y.1", json.dumps({"foo": "bar"}).encode()) is None


def test_mask_queue_name_hides_api_key():
    masked = _mask_queue_name("guest.pj8apk52san13utrx96nty7qtpkp7rh85iesog8ynqlip40ik4rpa5rv1beujg0c.78c965ec308b")

    assert "pj8apk52san13utrx96nty7qtpkp7rh85iesog8ynqlip40ik4rpa5rv1beujg0c" not in masked
    assert masked == "guest.***jg0c.78c965ec308b"


def test_mask_queue_name_passthrough_for_unexpected_shape():
    assert _mask_queue_name("not-a-guest-queue") == "not-a-guest-queue"
