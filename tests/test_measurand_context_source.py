"""Measurand sensors say whether their reading context came from the charger.

OCPP makes sampledValue.context optional and defines Sample.Periodic as its
default. The integration fills that default in and publishes it as the
sensor's `context` attribute, which made a value the charger never sent look
like charger evidence (seen live on a Sigenergy EVDC that omits context
entirely). `context_source` now sits next to every published `context`:
"charger" when the charger sent it, "defaulted" when the integration did.
"""

from datetime import UTC, datetime

from custom_components.ocpp.enums import (
    OcppMisc as om,
    ReadingContextSource as ctxsrc,
)

from .test_v16_transaction_identity import _mk_cp as _mk_cp_v16, _settle
from .test_v201_connector_charge_state import _mk_cp as _mk_cp_v201


def _sv(measurand: str, value: str, unit: str, **extra) -> dict:
    """Build a sampled value as the handler sees it (library snake_case)."""
    return {"measurand": measurand, "value": value, "unit": unit, **extra}


def _send_v16(cp, sampled_values: list[dict], connector_id: int = 1) -> None:
    cp.on_meter_values(
        connector_id=connector_id,
        meter_value=[
            {
                "timestamp": datetime.now(tz=UTC).isoformat(),
                "sampled_value": sampled_values,
            }
        ],
    )


def _attrs(cp, measurand: str, connector_id: int = 1) -> dict:
    return cp._metrics[(connector_id, measurand)].extra_attr


async def test_v16_missing_context_is_marked_defaulted(hass):
    """No context from the charger: the default stays, and says it is one."""
    cp = _mk_cp_v16(hass, connectors=1)
    await _settle(hass, cp)

    _send_v16(
        cp,
        [
            _sv("Energy.Active.Import.Register", "1000", "Wh"),
            _sv("Power.Active.Import", "7000", "W"),
        ],
    )

    for measurand in ("Energy.Active.Import.Register", "Power.Active.Import"):
        attrs = _attrs(cp, measurand)
        # Unchanged for existing consumers.
        assert attrs[om.context] == "Sample.Periodic"
        assert attrs[om.context_source] == ctxsrc.defaulted
        assert attrs[om.context_source] == "defaulted"


async def test_v16_explicit_context_is_marked_charger(hass):
    """A context the charger sent, including Sample.Periodic itself."""
    cp = _mk_cp_v16(hass, connectors=1)
    await _settle(hass, cp)

    _send_v16(
        cp,
        [
            _sv("Energy.Active.Import.Register", "1000", "Wh", context="Sample.Clock"),
            _sv("Power.Active.Import", "7000", "W", context="Sample.Periodic"),
        ],
    )

    energy = _attrs(cp, "Energy.Active.Import.Register")
    assert energy[om.context] == "Sample.Clock"
    assert energy[om.context_source] == "charger"
    power = _attrs(cp, "Power.Active.Import")
    assert power[om.context] == "Sample.Periodic"
    assert power[om.context_source] == "charger"


async def test_v16_source_follows_the_latest_reading(hass):
    """The attribute is per reading, not sticky from an earlier sample."""
    cp = _mk_cp_v16(hass, connectors=1)
    await _settle(hass, cp)

    _send_v16(cp, [_sv("Power.Active.Import", "7000", "W", context="Sample.Clock")])
    assert _attrs(cp, "Power.Active.Import")[om.context_source] == "charger"

    _send_v16(cp, [_sv("Power.Active.Import", "6000", "W")])
    attrs = _attrs(cp, "Power.Active.Import")
    assert attrs[om.context] == "Sample.Periodic"
    assert attrs[om.context_source] == "defaulted"


async def test_v16_per_phase_with_context_is_marked_charger(hass):
    """Per-phase readings publish context only when sent, so always charger."""
    cp = _mk_cp_v16(hass, connectors=1)
    await _settle(hass, cp)

    _send_v16(
        cp,
        [
            _sv("Current.Import", "10", "A", phase=p, context="Sample.Periodic")
            for p in ("L1", "L2", "L3")
        ],
    )

    attrs = _attrs(cp, "Current.Import")
    assert attrs[om.context] == "Sample.Periodic"
    assert attrs[om.context_source] == "charger"


async def test_v16_per_phase_without_context_publishes_neither(hass):
    """No default is invented on the per-phase path, so nothing to label."""
    cp = _mk_cp_v16(hass, connectors=1)
    await _settle(hass, cp)

    _send_v16(cp, [_sv("Current.Import", "10", "A", phase=p) for p in ("L1", "L2")])

    attrs = _attrs(cp, "Current.Import")
    assert om.context not in attrs
    assert om.context_source not in attrs


def _tx_event_with_meter(cp, sampled_values: list[dict], seq_no: int) -> None:
    cp.on_transaction_event(
        "Updated",
        "2026-01-01T00:00:00Z",
        "MeterValuePeriodic",
        seq_no,
        {"transaction_id": "tx-ctx"},
        evse={"id": 1, "connector_id": 1},
        meter_value=[
            {"timestamp": "2026-01-01T00:00:00Z", "sampled_value": sampled_values}
        ],
    )


def _sv201(measurand: str, value: float, unit: str, **extra) -> dict:
    return {
        "measurand": measurand,
        "value": value,
        "unit_of_measure": {"unit": unit},
        **extra,
    }


async def test_v201_transaction_event_context_source(hass):
    """OCPP 2.0.1 TransactionEvent meter values get the same attribute."""
    cp = _mk_cp_v201(hass)

    _tx_event_with_meter(
        cp,
        [
            _sv201("Power.Active.Import", 7000, "W"),
            _sv201("Current.Offered", 16, "A", context="Sample.Clock"),
        ],
        seq_no=1,
    )

    power = _attrs(cp, "Power.Active.Import")
    assert power[om.context] == "Sample.Periodic"
    assert power[om.context_source] == "defaulted"
    offered = _attrs(cp, "Current.Offered")
    assert offered[om.context] == "Sample.Clock"
    assert offered[om.context_source] == "charger"
