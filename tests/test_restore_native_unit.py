"""Restoring metrics from Home Assistant in the integration's own unit.

After a restart the 1.6 handler restores a missing meter start from the HA
sensor state. That state is shown in the user's display unit: a meter start
displayed in MWh read back as "15.73269" used to be taken as kWh, so session
energy (register minus meter start) jumped from 5.49 kWh to about 15,722 kWh
and the corrupted meter start persisted.
"""

import pytest
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT

from custom_components.ocpp.enums import HAChargerSession as csess

from .test_v16_transaction_identity import (
    _meter_values,
    _mk_cp,
    _settle,
    frozen_time,  # noqa: F401
)

MS_ENTITY = "sensor.test_cpid_connector_1_energy_meter_start"
TX_ENTITY = "sensor.test_cpid_connector_1_transaction_id"
TX_ID = 4242
# Register reading after the restart: 15,738.18 kWh, 5.49 kWh into the session.
REGISTER_WH = "15738180"


async def _restart_with(hass, ms_state: str, ms_unit: str | None):
    """Seed HA as a restart leaves it and feed one MeterValues."""
    attrs = {} if ms_unit is None else {ATTR_UNIT_OF_MEASUREMENT: ms_unit}
    hass.states.async_set(MS_ENTITY, ms_state, attrs)
    hass.states.async_set(TX_ENTITY, str(TX_ID))
    cp = _mk_cp(hass)
    await _settle(hass, cp)
    cp.on_meter_values(**_meter_values(TX_ID, value=REGISTER_WH))
    await hass.async_block_till_done()
    return cp


@pytest.mark.parametrize(
    ("state", "unit"),
    [
        ("15.73269", "MWh"),
        ("15732690", "Wh"),
        ("15732.69", "kWh"),
        ("15732.69", None),
    ],
)
async def test_meter_start_is_restored_in_kwh(hass, frozen_time, state, unit):  # noqa: F811
    """Whatever unit HA displays, the meter start comes back in kWh."""
    cp = await _restart_with(hass, state, unit)

    assert cp._metrics[(1, csess.meter_start)].value == pytest.approx(15732.69)
    assert cp._metrics[(1, csess.session_energy)].value == pytest.approx(5.49)
    assert cp._active_tx[1] == TX_ID


@pytest.mark.parametrize(("state", "unit"), [("15.73269", "bogus"), ("1", "W")])
async def test_unconvertible_meter_start_is_not_restored(
    hass,
    frozen_time,  # noqa: F811
    state,
    unit,
):
    """A unit that cannot be converted to kWh is not guessed at."""
    cp = await _restart_with(hass, state, unit)

    # The restore falls back to the live register, as with no HA state.
    assert cp._metrics[(1, csess.meter_start)].value == pytest.approx(15738.18)
    assert cp._metrics[(1, csess.session_energy)].value == pytest.approx(0.0)
    assert cp.get_ha_metric(csess.meter_start, 1, unit="kWh") is None
    assert cp._active_tx[1] == TX_ID


async def test_non_numeric_state_in_other_unit_is_not_restored(hass):
    """A state that is not a number cannot be converted either."""
    hass.states.async_set(MS_ENTITY, "n/a", {ATTR_UNIT_OF_MEASUREMENT: "MWh"})
    cp = _mk_cp(hass)

    assert cp.get_ha_metric(csess.meter_start, 1, unit="kWh") is None


async def test_unitless_restore_is_unchanged(hass):
    """Without a unit the raw state is returned, as before."""
    hass.states.async_set(TX_ENTITY, str(TX_ID), {ATTR_UNIT_OF_MEASUREMENT: "x"})
    hass.states.async_set(MS_ENTITY, "15.73269", {ATTR_UNIT_OF_MEASUREMENT: "MWh"})
    cp = _mk_cp(hass)

    assert cp.get_ha_metric(csess.transaction_id, 1) == str(TX_ID)
    assert cp.get_ha_metric(csess.meter_start, 1) == "15.73269"
