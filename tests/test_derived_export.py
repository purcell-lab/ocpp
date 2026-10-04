"""Export sensors derived from negative import (derive_export_from_negative_import).

Some bidirectional chargers report V2G discharge only as a negative
Power.Active.Import / Current.Import and send no export measurand at all.
With the option on, the 1.6 charge point splits those signed readings into
import and export flows and integrates export power into a persisted
Energy.Active.Export.Register. These tests drive the MeterValues handler
directly, as test_v16_transaction_identity does.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant
from ocpp.v16.enums import Measurand
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from websockets.protocol import State

from custom_components.ocpp.const import (
    DOMAIN,
    CentralSystemSettings,
    ChargerSystemSettings,
)
from custom_components.ocpp.derived_export import (
    DERIVED_SOURCE,
    MIN_MAX_GAP_S,
    DerivedExportRegister,
    max_sample_gap,
    parse_sample_timestamp,
    split_signed,
)
from custom_components.ocpp.enums import OcppMisc as om
from custom_components.ocpp.ocppv16 import ChargePoint

from .test_charge_point_core import _mk_entry_data

CP_ID = "CP_derived_export"
T0 = datetime(2026, 10, 4, 1, 0, 0, tzinfo=UTC)

PAI = Measurand.power_active_import.value
PAE = Measurand.power_active_export.value
CUR = Measurand.current_import.value
CEX = Measurand.current_export.value
EAIR = Measurand.energy_active_import_register.value
EAER = Measurand.energy_active_export_register.value


def _mk_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=_mk_entry_data())
    entry.add_to_hass(hass)
    return entry


def _mk_cp(
    hass: HomeAssistant,
    *,
    derive: bool = True,
    entry: MockConfigEntry | None = None,
    connectors: int = 1,
) -> ChargePoint:
    entry = entry or _mk_entry(hass)
    hass.data.setdefault(DOMAIN, {})
    centr = CentralSystemSettings(**entry.data)
    chg = ChargerSystemSettings(
        cpid="test_cpid",
        max_current=32,
        idle_interval=60,
        meter_interval=60,
        monitored_variables="",
        monitored_variables_autoconfig=False,
        skip_schema_validation=False,
        force_smart_charging=False,
        derive_export_from_negative_import=derive,
    )
    conn = SimpleNamespace(state=State.CLOSED, close=lambda: asyncio.sleep(0))
    cp = ChargePoint(CP_ID, conn, hass, entry, centr, chg)
    cp.num_connectors = connectors
    for c in range(1, connectors + 1):
        cp._init_connector_slots(c)
    return cp


async def _settle(hass: HomeAssistant, cp: ChargePoint) -> None:
    cp._ensure_tx_store_loaded()
    await cp._tx_store_load
    await hass.async_block_till_done()


def _sv(measurand: str, value, unit: str, phase: str | None = None) -> dict:
    sv = {
        "measurand": measurand,
        "value": str(value),
        "unit": unit,
        "context": "Sample.Periodic",
    }
    if phase is not None:
        sv["phase"] = phase
    return sv


def _send(cp: ChargePoint, ts: datetime | str | None, *samples: dict) -> None:
    bucket = {"sampled_value": list(samples)}
    if ts is not None:
        bucket["timestamp"] = ts if isinstance(ts, str) else ts.isoformat()
    cp.on_meter_values(connector_id=1, meter_value=[bucket])


def _sigen(power_w: float, current_a: float) -> tuple[dict, dict]:
    """Return the two signed flow readings the Sigenergy DC charger sends."""
    return _sv(PAI, power_w, "W"), _sv(CUR, current_a, "A")


def _m(cp: ChargePoint, measurand: str):
    return cp._metrics[(1, measurand)]


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def test_split_signed():
    """A signed reading becomes two non-negative flows."""
    assert split_signed(7.5) == (7.5, 0.0)
    assert split_signed(-7.5) == (0.0, 7.5)
    assert split_signed(0.0) == (0.0, 0.0)


def test_max_sample_gap_has_a_floor():
    """Short meter intervals still allow MIN_MAX_GAP_S; long ones scale."""
    assert max_sample_gap(10) == MIN_MAX_GAP_S
    assert max_sample_gap(None) == MIN_MAX_GAP_S
    assert max_sample_gap("junk") == MIN_MAX_GAP_S
    assert max_sample_gap(300) == 900


def test_parse_sample_timestamp():
    """OCPP 'Z' timestamps parse as UTC; garbage parses as None."""
    assert parse_sample_timestamp("2026-10-04T01:00:00Z") == T0
    assert parse_sample_timestamp("2026-10-04T11:00:00+10:00") == T0
    assert parse_sample_timestamp("2026-10-04T01:00:00") == T0
    assert parse_sample_timestamp("not a time") is None
    assert parse_sample_timestamp(None) is None


def test_register_integrates_trapezoids_and_never_bridges():
    """Energy accrues between close samples only, and only forwards in time."""
    reg = DerivedExportRegister()
    assert reg.add_sample(T0, 10.0, 180) == 0.0  # baseline only
    assert reg.add_sample(T0 + timedelta(seconds=60), 10.0, 180) == pytest.approx(
        10 / 60
    )
    # Ramp down to zero: the trapezoid counts half the interval at 10 kW.
    assert reg.add_sample(T0 + timedelta(seconds=120), 0.0, 180) == pytest.approx(
        5 / 60
    )
    total = reg.energy_kwh
    # Duplicate and out-of-order samples change nothing, not even the baseline.
    assert reg.add_sample(T0 + timedelta(seconds=120), 50.0, 180) == 0.0
    assert reg.add_sample(T0 + timedelta(seconds=30), 50.0, 180) == 0.0
    assert reg.last_ts == T0 + timedelta(seconds=120)
    # A gap longer than the limit sets a new baseline instead of bridging.
    assert reg.add_sample(T0 + timedelta(seconds=600), 10.0, 180) == 0.0
    assert reg.energy_kwh == pytest.approx(total)
    # Nonsense power is ignored.
    assert reg.add_sample(T0 + timedelta(seconds=660), float("nan"), 180) == 0.0
    assert reg.add_sample(T0 + timedelta(seconds=660), -1.0, 180) == 0.0


def test_register_round_trip_drops_the_baseline():
    """Only the energy persists, so a restart can never bridge the downtime."""
    reg = DerivedExportRegister(
        energy_kwh=1.25,
        energy_low_kwh=1.0,
        energy_high_kwh=1.5,
        step_intervals=2,
        last_ts=T0,
        last_kw=3.0,
    )
    restored = DerivedExportRegister.from_dict(reg.to_dict())
    assert restored == DerivedExportRegister(
        energy_kwh=1.25, energy_low_kwh=1.0, energy_high_kwh=1.5, step_intervals=2
    )
    # A total persisted before the bounds existed restores as exact.
    legacy = DerivedExportRegister.from_dict({"energy_kwh": 0.5})
    assert (legacy.energy_low_kwh, legacy.energy_high_kwh) == (0.5, 0.5)
    # Inconsistent bounds are discarded rather than trusted.
    bad = DerivedExportRegister.from_dict(
        {"energy_kwh": 0.5, "energy_low_kwh": 0.9, "energy_high_kwh": 0.1}
    )
    assert (bad.energy_low_kwh, bad.energy_high_kwh) == (0.5, 0.5)
    for bad in (None, {}, {"energy_kwh": "x"}, {"energy_kwh": -1}, [1]):
        assert DerivedExportRegister.from_dict(bad) is None
    assert DerivedExportRegister.from_dict({"energy_kwh": float("inf")}) is None


# --------------------------------------------------------------------------
# MeterValues handling
# --------------------------------------------------------------------------


async def test_discharge_is_split_into_export_flows(hass):
    """Negative import becomes zero import plus positive, labelled export."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)

    _send(cp, T0, *_sigen(-22267, -60.2))

    assert _m(cp, PAI).value == 0.0
    assert _m(cp, PAE).value == pytest.approx(22.267)
    assert _m(cp, PAE).unit == "kW"
    assert _m(cp, PAE).extra_attr["source"] == DERIVED_SOURCE
    assert _m(cp, PAE).extra_attr[om.context] == "Sample.Periodic"
    assert _m(cp, CUR).value == 0.0
    assert _m(cp, CEX).value == pytest.approx(60.2)
    assert _m(cp, CEX).unit == "A"
    # A single sample is only a baseline: no energy yet, but the register
    # exists and says it is an estimate.
    assert _m(cp, EAER).value == 0.0
    assert _m(cp, EAER).unit == "kWh"
    assert _m(cp, EAER).extra_attr["estimated"] is True


async def test_charging_reports_zero_export(hass):
    """Positive import is untouched and export reads a measured zero."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)

    _send(cp, T0, *_sigen(22267, 60.2))

    assert _m(cp, PAI).value == pytest.approx(22.267)
    assert _m(cp, PAE).value == 0.0
    assert _m(cp, CUR).value == pytest.approx(60.2)
    assert _m(cp, CEX).value == 0.0


async def test_export_energy_follows_the_chargers_clock(hass):
    """Energy integrates over sample timestamps, through a sign change."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)

    _send(cp, T0, *_sigen(-12000, -30))
    _send(cp, T0 + timedelta(seconds=60), *_sigen(-12000, -30))
    _send(cp, T0 + timedelta(seconds=120), *_sigen(-12000, -30))
    assert _m(cp, EAER).value == pytest.approx(0.4)  # 12 kW for 2 minutes

    # Switching back to charging: the ramp from 12 kW export to none counts
    # half an interval, and charging itself adds nothing.
    _send(cp, T0 + timedelta(seconds=180), *_sigen(7000, 18))
    _send(cp, T0 + timedelta(seconds=240), *_sigen(7000, 18))
    assert _m(cp, EAER).value == pytest.approx(0.5)
    # The import register is never touched by the derivation.
    assert _m(cp, EAIR).value is None


async def test_gaps_and_untimed_samples_add_no_energy(hass):
    """A long silence or a sample without a timestamp breaks the chain."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)

    _send(cp, T0, *_sigen(-12000, -30))
    _send(cp, T0 + timedelta(seconds=60), *_sigen(-12000, -30))
    assert _m(cp, EAER).value == pytest.approx(0.2)

    # Ten minutes of silence: the next sample is only a new baseline.
    _send(cp, T0 + timedelta(seconds=660), *_sigen(-12000, -30))
    assert _m(cp, EAER).value == pytest.approx(0.2)

    # An untimed sample still splits the flows but resets the baseline.
    _send(cp, None, *_sigen(-12000, -30))
    assert _m(cp, PAE).value == pytest.approx(12.0)
    _send(cp, T0 + timedelta(seconds=720), *_sigen(-12000, -30))
    assert _m(cp, EAER).value == pytest.approx(0.2)


async def test_per_phase_discharge_is_summed_before_splitting(hass):
    """Per-phase power is summed by process_phases, then split."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)

    def phases(w):
        return [_sv(PAI, w, "W", p) for p in ("L1", "L2", "L3")]

    _send(cp, T0, *phases(-4000))
    _send(cp, T0 + timedelta(seconds=60), *phases(-4000))

    assert _m(cp, PAI).value == 0.0
    assert _m(cp, PAE).value == pytest.approx(12.0)
    assert _m(cp, EAER).value == pytest.approx(0.2)


async def test_power_without_a_unit_is_not_integrated(hass):
    """An ambiguous power unit must not become energy."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)

    _send(cp, T0, {"measurand": PAI, "value": "-5000", "context": "Sample.Periodic"})

    assert _m(cp, PAI).value == -5000
    assert _m(cp, PAE).value is None
    assert _m(cp, EAER).value is None


async def test_option_off_leaves_readings_alone(hass):
    """Control: without the option nothing is split or derived."""
    cp = _mk_cp(hass, derive=False)
    await _settle(hass, cp)

    _send(cp, T0, *_sigen(-22267, -60.2))
    _send(cp, T0 + timedelta(seconds=60), *_sigen(-22267, -60.2))

    assert _m(cp, PAI).value == pytest.approx(-22.267)
    assert _m(cp, CUR).value == pytest.approx(-60.2)
    assert _m(cp, PAE).value is None
    assert _m(cp, EAER).value is None
    assert cp._derived_export == {}


async def test_native_export_wins_and_stops_derivation(hass, caplog):
    """Once the charger meters export itself, its figures are left as sent."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)

    _send(cp, T0, *_sigen(-10000, -25))
    _send(
        cp,
        T0 + timedelta(seconds=60),
        _sv(PAI, 0, "W"),
        _sv(PAE, 9000, "W"),
        _sv(EAER, 123456, "Wh"),
    )

    assert cp._native_export_seen is True
    assert "no longer deriving export" in caplog.text
    assert _m(cp, PAE).value == pytest.approx(9.0)
    assert _m(cp, EAER).value == pytest.approx(123.456)

    # Later negative import is no longer reinterpreted.
    _send(cp, T0 + timedelta(seconds=120), *_sigen(-10000, -25))
    assert _m(cp, PAI).value == pytest.approx(-10.0)
    assert _m(cp, EAER).value == pytest.approx(123.456)


async def test_register_survives_a_restart_without_bridging(hass, hass_storage):
    """The total is persisted and restored; the downtime adds nothing."""
    entry = _mk_entry(hass)
    cp = _mk_cp(hass, entry=entry)
    await _settle(hass, cp)

    _send(cp, T0, *_sigen(-12000, -30))
    _send(cp, T0 + timedelta(seconds=60), *_sigen(-12000, -30))
    async_fire_time_changed(hass, datetime.now(tz=UTC).replace(year=2100))
    await hass.async_block_till_done()
    stored = hass_storage[cp._tx_store.key]["data"]["derived_export"]
    assert stored["1"]["energy_kwh"] == pytest.approx(0.2)

    # A new process: same entry, same storage.
    cp2 = _mk_cp(hass, entry=entry)
    await _settle(hass, cp2)
    assert _m(cp2, EAER).value == pytest.approx(0.2)

    # The first sample after the restart is a baseline, even though it is
    # within the gap limit of the last one before it.
    _send(cp2, T0 + timedelta(seconds=120), *_sigen(-12000, -30))
    assert _m(cp2, EAER).value == pytest.approx(0.2)
    _send(cp2, T0 + timedelta(seconds=180), *_sigen(-12000, -30))
    assert _m(cp2, EAER).value == pytest.approx(0.4)


async def test_nothing_is_persisted_for_chargers_not_deriving(hass, hass_storage):
    """The stored layout is unchanged when the option is off."""
    cp = _mk_cp(hass, derive=False)
    await _settle(hass, cp)
    _send(cp, T0, *_sigen(-12000, -30))
    assert "derived_export" not in cp._tx_store_snapshot()


# --------------------------------------------------------------------------
# Entities and configuration
# --------------------------------------------------------------------------


@pytest.fixture(name="bypass_websockets")
def bypass_websockets_fixture():
    """Stub only the websocket server, as test_sensor_stale_cleanup does."""
    import websockets.asyncio.server
    from unittest.mock import patch

    future = asyncio.Future()
    future.set_result(websockets.asyncio.server.Server)
    with (
        patch("websockets.asyncio.server.serve", return_value=future),
        patch("websockets.asyncio.server.Server.close"),
        patch("websockets.asyncio.server.Server.wait_closed"),
    ):
        yield


@pytest.mark.parametrize("derive", [True, False])
async def test_export_sensors_exist_only_when_deriving(hass, bypass_websockets, derive):
    """The option creates export sensors even with no export measurand listed.

    The Sigenergy charger rejects MeterValuesSampledData, so its configured
    measurand list never contains export measurands; without this the
    derived flows would have no entity to land on.
    """
    from homeassistant.helpers import entity_registry as er

    from custom_components.ocpp.const import (
        CONF_CPIDS,
        CONF_DERIVE_EXPORT_FROM_NEGATIVE_IMPORT,
        CONF_MONITORED_VARIABLES,
        sensor_unique_id,
    )

    from .const import MOCK_CONFIG_CP_APPEND, MOCK_CONFIG_DATA

    cp_cfg = {
        **MOCK_CONFIG_CP_APPEND,
        CONF_MONITORED_VARIABLES: f"{PAI},{CUR},{EAIR}",
        CONF_DERIVE_EXPORT_FROM_NEGATIVE_IMPORT: derive,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**MOCK_CONFIG_DATA, CONF_CPIDS: [{"CP_derive": cp_cfg}]},
        entry_id=f"test_derive_{derive}",
        title="test_derive",
        version=2,
        minor_version=0,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    cpid = cp_cfg["cpid"]
    for measurand in (PAE, CEX, EAER, "Flow.Direction", "Energy.Session.Export"):
        found = registry.async_get_entity_id(
            "sensor", DOMAIN, sensor_unique_id(cpid, measurand, None)
        )
        assert (found is not None) is derive, measurand
    # The configured import sensors are there either way.
    assert registry.async_get_entity_id(
        "sensor", DOMAIN, sensor_unique_id(cpid, PAI, None)
    )


async def test_disabled_option_keeps_but_does_not_publish_the_total(hass, hass_storage):
    """Switching the option off hides the total; switching it on continues it."""
    entry = _mk_entry(hass)
    cp = _mk_cp(hass, entry=entry)
    await _settle(hass, cp)
    _send(cp, T0, *_sigen(-12000, -30))
    _send(cp, T0 + timedelta(seconds=60), *_sigen(-12000, -30))
    async_fire_time_changed(hass, datetime.now(tz=UTC).replace(year=2100))
    await hass.async_block_till_done()

    off = _mk_cp(hass, entry=entry, derive=False)
    await _settle(hass, off)
    assert _m(off, EAER).value is None
    assert off._tx_store_snapshot()["derived_export"]["1"][
        "energy_kwh"
    ] == pytest.approx(0.2)

    on = _mk_cp(hass, entry=entry)
    await _settle(hass, on)
    assert _m(on, EAER).value == pytest.approx(0.2)


# --------------------------------------------------------------------------
# Current.Export from power and voltage
# --------------------------------------------------------------------------


def _sigen_v2g(power_w: float, volts: float | None = 392.1) -> list[dict]:
    """Return a discharge reading as the Sigenergy EVDC really sends it.

    Observed live: negative Power.Active.Import, but Current.Import 0.00 A.
    """
    samples = [_sv(PAI, power_w, "W"), _sv(CUR, "0.00", "A")]
    if volts is not None:
        samples.append(_sv(Measurand.voltage.value, volts, "V"))
    return samples


async def test_export_current_comes_from_power_when_current_is_unsigned(hass):
    """A 0 A current during discharge is replaced by power over voltage."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)

    _send(cp, T0, *_sigen_v2g(-7269))

    assert _m(cp, CUR).value == 0.0
    assert _m(cp, CEX).value == pytest.approx(7269 / 392.1, abs=0.01)
    assert _m(cp, CEX).extra_attr["estimated"] is True
    assert _m(cp, CEX).extra_attr["method"] == "power_divided_by_voltage"


async def test_dc_pack_voltage_above_ac_range_is_used(hass):
    """An 800 V-class pack is a real supply voltage, not an implausible one."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)

    _send(cp, T0, *_sigen_v2g(-16000, volts=800))

    assert _m(cp, CEX).value == pytest.approx(20.0)


async def test_export_current_unknown_without_a_voltage(hass):
    """No voltage in the reading: export current is unknown, not zero."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)

    _send(cp, T0, *_sigen_v2g(-7269, volts=None))

    assert _m(cp, PAE).value == pytest.approx(7.269)
    assert _m(cp, CEX).value is None


async def test_charging_after_discharge_clears_the_estimate(hass):
    """Back to charging, export current is a plain zero with no method."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)

    _send(cp, T0, *_sigen_v2g(-7269))
    _send(cp, T0 + timedelta(seconds=60), *_sigen(16527, 41.0))

    assert _m(cp, CEX).value == 0.0
    assert _m(cp, CEX).extra_attr["estimated"] is False
    assert "method" not in _m(cp, CEX).extra_attr


async def test_signed_current_from_the_charger_is_preferred(hass):
    """When the charger signs its current, that measured value wins."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)

    _send(cp, T0, *_sigen(-22267, -60.2), _sv(Measurand.voltage.value, 380, "V"))

    assert _m(cp, CEX).value == pytest.approx(60.2)
    assert _m(cp, CEX).extra_attr["estimated"] is False


# --------------------------------------------------------------------------
# Flow direction
# --------------------------------------------------------------------------

FLOW = "Flow.Direction"


@pytest.mark.parametrize(
    ("power_w", "expected"),
    [
        (16527, "import"),
        (-7269, "export"),
        (-249, "export"),  # smallest real discharge observed
        (-58, "idle"),  # session-start noise with 0 A
        (-7, "idle"),  # residue after the discharge stopped
        (50, "idle"),
    ],
)
async def test_flow_direction_follows_power_sign(hass, power_w, expected):
    """Direction comes from the power sign, with a small deadband."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)

    _send(cp, T0, *_sigen_v2g(power_w))

    assert cp._metrics[(1, FLOW)].value == expected
    assert cp._metrics[(1, FLOW)].extra_attr["deadband_kw"] == 0.1


async def test_flow_direction_goes_idle_when_the_session_closes(hass):
    """Clearing flow readings at a stop also clears the direction."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)

    _send(cp, T0, *_sigen_v2g(-7269))
    cp._zero_flow_measurands(1)

    assert cp._metrics[(1, FLOW)].value == "idle"


async def test_flow_direction_not_published_without_the_option(hass):
    """Control: without the option there is no direction metric."""
    cp = _mk_cp(hass, derive=False)
    await _settle(hass, cp)

    _send(cp, T0, *_sigen_v2g(-7269))

    assert cp._metrics[(1, FLOW)].value is None


# --------------------------------------------------------------------------
# Per-session export energy
# --------------------------------------------------------------------------

SESSION_EXPORT = "Energy.Session.Export"


def _send_tx(cp: ChargePoint, tx: int, ts: datetime, *samples: dict) -> None:
    cp.on_meter_values(
        connector_id=1,
        meter_value=[{"timestamp": ts.isoformat(), "sampled_value": list(samples)}],
        transaction_id=tx,
    )


async def test_session_export_counts_only_this_transaction(hass):
    """Export before a session starts is not billed to it; a new one resets."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)

    # Outside any transaction: the lifetime register moves, no session does.
    _send(cp, T0, *_sigen_v2g(-12000))
    _send(cp, T0 + timedelta(seconds=60), *_sigen_v2g(-12000))
    assert _m(cp, EAER).value == pytest.approx(0.2)

    tx = cp.on_start_transaction(1, "tag", 15_000_000).transaction_id
    assert _m(cp, SESSION_EXPORT).value == 0.0

    _send_tx(cp, tx, T0 + timedelta(seconds=120), *_sigen_v2g(-12000))
    _send_tx(cp, tx, T0 + timedelta(seconds=180), *_sigen_v2g(-12000))
    assert _m(cp, SESSION_EXPORT).value == pytest.approx(0.4)
    assert _m(cp, SESSION_EXPORT).extra_attr["estimated"] is True
    assert _m(cp, EAER).value == pytest.approx(0.6)

    second = cp.on_start_transaction(1, "tag", 15_000_000).transaction_id
    assert second != tx
    assert _m(cp, SESSION_EXPORT).value == 0.0


async def test_session_export_survives_a_restart_of_the_same_session(
    hass, hass_storage
):
    """A running session keeps its total across a restart; downtime adds none."""
    entry = _mk_entry(hass)
    cp = _mk_cp(hass, entry=entry)
    await _settle(hass, cp)
    tx = cp.on_start_transaction(1, "tag", 15_000_000).transaction_id
    async_fire_time_changed(hass, datetime.now(tz=UTC).replace(year=2100))
    await hass.async_block_till_done()
    _send_tx(cp, tx, T0, *_sigen_v2g(-12000))
    _send_tx(cp, tx, T0 + timedelta(seconds=60), *_sigen_v2g(-12000))
    async_fire_time_changed(hass, datetime.now(tz=UTC).replace(year=2101))
    await hass.async_block_till_done()
    stored = hass_storage[cp._tx_store.key]["data"]["derived_session_export"]
    assert stored == {"1": {"tx_id": tx, "energy_kwh": pytest.approx(0.2)}}

    cp2 = _mk_cp(hass, entry=entry)
    await _settle(hass, cp2)
    assert _m(cp2, SESSION_EXPORT).value == pytest.approx(0.2)


async def test_session_export_of_another_transaction_is_not_restored(
    hass, hass_storage
):
    """A total recorded for a different transaction never carries over."""
    entry = _mk_entry(hass)
    key = ChargePoint(
        CP_ID,
        SimpleNamespace(state=State.CLOSED, close=lambda: asyncio.sleep(0)),
        hass,
        entry,
        CentralSystemSettings(**entry.data),
        ChargerSystemSettings(
            cpid="test_cpid",
            max_current=32,
            idle_interval=60,
            meter_interval=60,
            monitored_variables="",
            monitored_variables_autoconfig=False,
            skip_schema_validation=False,
            force_smart_charging=False,
        ),
    )._tx_store.key
    hass_storage[key] = {
        "version": 1,
        "key": key,
        "data": {
            "last_tx_id": 4242,
            "connectors": {"1": {"tx_id": 4242, "started_at": 1_800_000_000.0}},
            "derived_session_export": {"1": {"tx_id": 1111, "energy_kwh": 3.5}},
        },
    }
    cp = _mk_cp(hass, entry=entry)
    await _settle(hass, cp)

    assert _m(cp, SESSION_EXPORT).value is None


# --------------------------------------------------------------------------
# Uncertainty
# --------------------------------------------------------------------------


async def test_bounds_and_steps_replay_the_live_v2g_run(hass):
    """Replay the observed 4 October discharge and check the published bounds.

    The independent inverter counter recorded 1.250 kWh. The point estimate
    is high because export started and stopped mid-interval; the bounds
    bracket the true value and the two step intervals are counted.
    """
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    readings = [  # (seconds after T0, signed W) as received from the charger
        (0, 16527),
        (60, -249),
        (120, -269),
        (180, -359),
        (240, -229),
        (300, -7048),
        (361, -7269),
        (421, -7267),
        (481, -7332),
        (541, -7601),
        (601, -18819),
        (661, -21839),
        (721, -7),
    ]
    for offset, watts in readings:
        _send(cp, T0 + timedelta(seconds=offset), *_sigen_v2g(watts))

    reg = _m(cp, EAER)
    assert reg.value == pytest.approx(1.3067, abs=0.001)
    low = reg.extra_attr["energy_lower_bound_kwh"]
    high = reg.extra_attr["energy_upper_bound_kwh"]
    assert low < 1.250 < high
    assert low <= reg.value <= high
    # Export starting, the 0.2 -> 7 kW jump, the 7.6 -> 18.8 kW jump, the
    # 18.8 -> 21.8 kW change and export stopping.
    assert reg.extra_attr["step_intervals"] == 5
    assert reg.extra_attr["last_interval_s"] == 60


async def test_steady_export_has_tight_bounds(hass):
    """Constant power: the bounds collapse onto the estimate, no steps."""
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    for i in range(4):
        _send(cp, T0 + timedelta(seconds=60 * i), *_sigen_v2g(-7300))

    reg = _m(cp, EAER)
    assert reg.extra_attr["energy_lower_bound_kwh"] == pytest.approx(reg.value)
    assert reg.extra_attr["energy_upper_bound_kwh"] == pytest.approx(reg.value)
    assert reg.extra_attr["step_intervals"] == 0
