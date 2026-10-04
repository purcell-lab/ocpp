"""OCPP 1.6 measurand list must survive a charger that cannot report it.

Some chargers (seen on a Sigenergy EVDC) answer GetConfiguration for
MeterValuesSampledData with the key as unknown and reject ChangeConfiguration
with NotSupported. get_supported_measurands() then returns "", and
post_connect used to store that "" as the charger's monitored_variables:

- a list the user had picked by hand was discarded;
- on the next reload sensor.py built no measurand sensors, although the
  charger kept sending those measurands in MeterValues;
- the write edited entry.data in place (a shallow copy), so
  async_update_entry saw no change and the blank list sat in memory unsaved
  until something else saved the entry.

These tests drive get_supported_measurands and post_connect directly against
a fake charger, without a websocket.
"""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from ocpp.v16 import call
from ocpp.v16.enums import ConfigurationStatus
from pytest_homeassistant_custom_component.common import MockConfigEntry
from websockets.protocol import State

from custom_components.ocpp.const import (
    CONF_CPIDS,
    CONF_MONITORED_VARIABLES,
    CONF_MONITORED_VARIABLES_AUTOCONFIG,
    CONF_NUM_CONNECTORS,
    DEFAULT_MONITORED_VARIABLES,
    DOMAIN,
    CentralSystemSettings,
    ChargerSystemSettings,
)
from custom_components.ocpp.enums import ConfigurationKey as ckey
from custom_components.ocpp.ocppv16 import ChargePoint

from .test_charge_point_core import _mk_entry_data

CP_ID = "CP_measurands"
MANUAL = "Power.Active.Import,SoC"
SAMPLED = ckey.meter_values_sampled_data.value


class FakeCharger:
    """Answers the integration's calls the way a 1.6 charger would.

    With supports_measurands False it behaves like the charger in the bug:
    every configuration key is unknown and every change is NotSupported.
    """

    def __init__(self, *, supports_measurands: bool, current: str = ""):
        """Start with the given MeterValuesSampledData value."""
        self.supports_measurands = supports_measurands
        self.current = current
        self.changes: list[tuple[str, str]] = []

    async def __call__(self, req):
        """Reply to one request."""
        if isinstance(req, call.GetConfiguration):
            key = (req.key or [""])[0]
            if key == SAMPLED and self.supports_measurands:
                return SimpleNamespace(
                    configuration_key=[
                        {"key": key, "value": self.current, "readonly": False}
                    ],
                    unknown_key=None,
                )
            return SimpleNamespace(configuration_key=[], unknown_key=[key])
        if isinstance(req, call.ChangeConfiguration):
            self.changes.append((req.key, req.value))
            if req.key == SAMPLED and self.supports_measurands:
                self.current = req.value
                return SimpleNamespace(status=ConfigurationStatus.accepted)
            return SimpleNamespace(status=ConfigurationStatus.not_supported)
        # Anything else post_connect sends is best effort; a bare reply makes
        # those steps fail quietly, as they would on a minimal charger.
        return SimpleNamespace()


def _charger_settings(measurands, autoconfig: bool) -> dict:
    settings = {"cpid": "test_cpid", CONF_NUM_CONNECTORS: 1}
    if measurands is not None:
        settings[CONF_MONITORED_VARIABLES] = measurands
    settings[CONF_MONITORED_VARIABLES_AUTOCONFIG] = autoconfig
    return settings


def _mk_cp(
    hass: HomeAssistant,
    charger: FakeCharger,
    *,
    measurands: str | None,
    autoconfig: bool,
) -> ChargePoint:
    data = _mk_entry_data()
    data[CONF_CPIDS] = [{CP_ID: _charger_settings(measurands, autoconfig)}]
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})
    centr = CentralSystemSettings(**entry.data)
    chg = ChargerSystemSettings(
        cpid="test_cpid",
        max_current=32,
        idle_interval=60,
        meter_interval=60,
        monitored_variables=measurands or "",
        monitored_variables_autoconfig=autoconfig,
        skip_schema_validation=False,
        force_smart_charging=False,
    )
    conn = SimpleNamespace(state=State.CLOSED, close=lambda: asyncio.sleep(0))
    cp = ChargePoint(CP_ID, conn, hass, entry, centr, chg)
    cp.num_connectors = 1
    cp._init_connector_slots(1)
    cp.call = charger
    return cp


def _stored(cp: ChargePoint) -> dict:
    return cp.entry.data[CONF_CPIDS][0][CP_ID]


async def _detect_and_store(cp: ChargePoint) -> str:
    """Run the measurand step of post_connect."""
    accepted = await cp.get_supported_measurands()
    cp._store_detected_settings(accepted)
    return accepted


# --------------------------------------------------------------------------
# Charger that cannot report or accept measurands
# --------------------------------------------------------------------------


@pytest.mark.parametrize("autoconfig", [False, True])
async def test_configured_list_is_kept(hass: HomeAssistant, autoconfig: bool):
    """The stored list survives an unknown key / NotSupported charger."""
    cp = _mk_cp(
        hass,
        FakeCharger(supports_measurands=False),
        measurands=MANUAL,
        autoconfig=autoconfig,
    )
    before = copy.deepcopy(dict(cp.entry.data))

    with patch.object(hass.config_entries, "async_update_entry") as update:
        accepted = await _detect_and_store(cp)

    assert accepted == ""  # the charger really did give nothing usable
    update.assert_not_called()  # nothing changed, so nothing to save or reload
    assert dict(cp.entry.data) == before
    assert _stored(cp)[CONF_MONITORED_VARIABLES] == MANUAL


@pytest.mark.parametrize("autoconfig", [False, True])
@pytest.mark.parametrize("measurands", [None, ""])
async def test_nothing_configured_falls_back_to_all_measurands(
    hass: HomeAssistant, autoconfig: bool, measurands
):
    """With no list stored (or one already blanked), monitor everything.

    "" covers entries already damaged by the old behaviour: they recover
    instead of staying without measurand sensors.
    """
    cp = _mk_cp(
        hass,
        FakeCharger(supports_measurands=False),
        measurands=measurands,
        autoconfig=autoconfig,
    )

    await _detect_and_store(cp)

    assert _stored(cp)[CONF_MONITORED_VARIABLES] == DEFAULT_MONITORED_VARIABLES
    assert _stored(cp)[CONF_MONITORED_VARIABLES_AUTOCONFIG] is autoconfig


async def test_full_post_connect_keeps_the_manual_list(hass: HomeAssistant):
    """End to end: post_connect against the bug's charger keeps the list."""
    cp = _mk_cp(
        hass,
        FakeCharger(supports_measurands=False),
        measurands=MANUAL,
        autoconfig=False,
    )
    # post_connect can reach the remote-trigger paths; keep them out.
    cp._attr_supported_features = 0

    await cp.post_connect()

    assert cp.post_connect_success is True
    assert _stored(cp)[CONF_MONITORED_VARIABLES] == MANUAL


# --------------------------------------------------------------------------
# Entry handling
# --------------------------------------------------------------------------


async def test_entry_data_is_replaced_not_mutated(hass: HomeAssistant):
    """A real change produces new containers, so it is saved and reloaded."""
    cp = _mk_cp(
        hass,
        FakeCharger(supports_measurands=True),
        measurands=MANUAL,
        autoconfig=False,
    )
    old_data = cp.entry.data
    old_cpids = old_data[CONF_CPIDS]
    old_snapshot = copy.deepcopy(dict(old_data))
    cp.num_connectors = 2  # the charger reports another connector

    with patch.object(
        hass.config_entries,
        "async_update_entry",
        wraps=hass.config_entries.async_update_entry,
    ) as update:
        await _detect_and_store(cp)

    update.assert_called_once()
    # The old containers are untouched: had they been edited in place, the
    # update would have compared equal and been dropped.
    assert dict(old_data) == old_snapshot
    assert old_cpids[0][CP_ID][CONF_NUM_CONNECTORS] == 1
    assert _stored(cp)[CONF_NUM_CONNECTORS] == 2
    assert _stored(cp)[CONF_MONITORED_VARIABLES] == MANUAL
    # Unrelated settings ride along.
    assert _stored(cp)["cpid"] == "test_cpid"
    assert cp.entry.data["csid"] == old_data["csid"]


async def test_unknown_charger_id_leaves_entry_alone(hass: HomeAssistant):
    """A charger not in the entry is not written anywhere."""
    cp = _mk_cp(
        hass,
        FakeCharger(supports_measurands=False),
        measurands=MANUAL,
        autoconfig=False,
    )
    cp.id = "someone_else"

    with patch.object(hass.config_entries, "async_update_entry") as update:
        cp._store_detected_settings("")

    update.assert_not_called()


# --------------------------------------------------------------------------
# Configurable charger: unchanged behaviour
# --------------------------------------------------------------------------


async def test_manual_list_accepted_is_stored_and_set(hass: HomeAssistant):
    """A charger that accepts the list gets it, and the entry keeps it."""
    charger = FakeCharger(supports_measurands=True, current="Voltage")
    cp = _mk_cp(hass, charger, measurands=MANUAL, autoconfig=False)

    with patch.object(hass.config_entries, "async_update_entry") as update:
        accepted = await _detect_and_store(cp)

    assert accepted == MANUAL
    assert (SAMPLED, MANUAL) in charger.changes
    update.assert_not_called()  # the stored list already matched


async def test_autoconfig_stores_what_the_charger_accepts(hass: HomeAssistant):
    """Autoconfig replaces the stored list with the charger's answer."""
    charger = FakeCharger(supports_measurands=True, current="Voltage")
    cp = _mk_cp(
        hass,
        charger,
        measurands=DEFAULT_MONITORED_VARIABLES,
        autoconfig=True,
    )

    # The charger refuses the full list but reports what it does sample.
    async def refuse_change(req):
        if isinstance(req, call.ChangeConfiguration):
            return SimpleNamespace(status=ConfigurationStatus.rejected)
        return await charger(req)

    cp.call = refuse_change

    accepted = await _detect_and_store(cp)

    assert accepted == "Voltage"
    assert _stored(cp)[CONF_MONITORED_VARIABLES] == "Voltage"


async def test_reordered_list_is_not_a_change(hass: HomeAssistant):
    """The same measurands in another order do not rewrite or reload."""
    cp = _mk_cp(
        hass,
        FakeCharger(supports_measurands=False),
        measurands=MANUAL,
        autoconfig=False,
    )

    with patch.object(hass.config_entries, "async_update_entry") as update:
        cp._store_detected_settings("SoC,Power.Active.Import")

    update.assert_not_called()
    assert _stored(cp)[CONF_MONITORED_VARIABLES] == MANUAL
