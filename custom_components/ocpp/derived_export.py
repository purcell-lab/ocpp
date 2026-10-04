"""Export metering derived from negative import, for chargers that send no export.

Some bidirectional chargers (e.g. the Sigenergy DC charger over OCPP 1.6)
report discharge only as a negative ``Power.Active.Import`` /
``Current.Import`` and never send any ``*.Export.*`` measurand, so Home
Assistant has no export energy at all. When the per-charger
``derive_export_from_negative_import`` option is on, the charge point splits
the signed reading into non-negative import and export flows and integrates
the export power over the charger's own sample timestamps into a lifetime
``Energy.Active.Export.Register``.

The register is an estimate from instantaneous samples, not a metered
counter, and every derived metric says so in its attributes. The rules are
deliberately conservative: energy is only accumulated between two consecutive
samples that are close enough together, so a gap, a restart, a duplicate or an
out-of-order sample never bridges or invents energy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import math

# Attribute values published on every derived metric.
DERIVED_SOURCE = "derived_from_negative_import"
DERIVED_METHOD = "trapezoidal_integration"
DERIVED_CURRENT_METHOD = "power_divided_by_voltage"

# Attribute keys.
ATTR_SOURCE = "source"
ATTR_METHOD = "method"
ATTR_ESTIMATED = "estimated"
ATTR_MAX_GAP = "max_sample_gap_s"
ATTR_LAST_SAMPLE = "last_sample_timestamp"
ATTR_LOWER = "energy_lower_bound_kwh"
ATTR_UPPER = "energy_upper_bound_kwh"
ATTR_STEPS = "step_intervals"
ATTR_LAST_INTERVAL = "last_interval_s"
ATTR_REFERENCE_ENTITY = "reference_entity"
ATTR_REFERENCE_STATUS = "reference_status"
ATTR_REFERENCE_DELTA = "reference_delta_kwh"
ATTR_DERIVED_DELTA = "derived_delta_kwh"
ATTR_DIVERGENCE = "divergence_kwh"
ATTR_DIVERGENCE_PCT = "divergence_pct"

# An interval is a "step" when export starts or stops inside it, or power
# moves by more than this between its two samples. Live V2G data showed the
# whole error against an independent counter coming from such intervals: the
# true change happened at an unknown point between samples 60 s apart.
STEP_KW = 2.0

# Never integrate across more than this many seconds, whatever the configured
# meter interval: a longer silence means the charger was offline or idle and
# its power over the gap is unknown.
MIN_MAX_GAP_S = 180


# Energy flow direction, published because OCPP 1.6 connector status stays
# Charging while a bidirectional charger discharges.
FLOW_IMPORT = "import"
FLOW_EXPORT = "export"
FLOW_IDLE = "idle"
# Readings smaller than this are treated as no flow. Observed live: -58 W at a
# session start and -7 W after a discharge stopped, both with 0 A, while the
# smallest genuine discharge seen (229 W) did move the inverter's counter.
FLOW_DEADBAND_KW = 0.1
ATTR_DEADBAND = "deadband_kw"


def flow_direction(signed_kw: float) -> str:
    """Classify a signed import reading as import, export or idle."""
    if signed_kw >= FLOW_DEADBAND_KW:
        return FLOW_IMPORT
    if signed_kw <= -FLOW_DEADBAND_KW:
        return FLOW_EXPORT
    return FLOW_IDLE


def max_sample_gap(meter_interval: int | float | None) -> float:
    """Return the longest gap two samples may span and still be integrated."""
    try:
        interval = float(meter_interval or 0)
    except (TypeError, ValueError):
        interval = 0.0
    if not math.isfinite(interval) or interval < 0:
        interval = 0.0
    return max(float(MIN_MAX_GAP_S), 3.0 * interval)


def parse_sample_timestamp(raw) -> datetime | None:
    """Parse an OCPP sample timestamp, returning an aware UTC datetime or None."""
    if isinstance(raw, datetime):
        ts = raw
    elif isinstance(raw, str) and raw:
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def split_signed(value: float) -> tuple[float, float]:
    """Split a signed import reading into (import, export), both non-negative."""
    if value < 0:
        return 0.0, -value
    return value, 0.0


@dataclass
class DerivedExportRegister:
    """Lifetime export energy (kWh) integrated from export power samples."""

    energy_kwh: float = 0.0
    # Bounds from holding the lower / higher sample of each interval: the
    # true energy lies between them whenever power changed monotonically.
    energy_low_kwh: float = 0.0
    energy_high_kwh: float = 0.0
    step_intervals: int = 0
    last_interval_s: float | None = None
    last_ts: datetime | None = None
    last_kw: float | None = None

    def add_sample(self, ts: datetime, export_kw: float, max_gap_s: float) -> float:
        """Integrate one export power sample; return the kWh added.

        The first sample, and the first one after a gap longer than
        ``max_gap_s``, only sets a new baseline. Duplicate or out-of-order
        samples are ignored entirely so they cannot shift the baseline back.
        """
        if not math.isfinite(export_kw) or export_kw < 0:
            return 0.0
        if self.last_ts is not None and ts <= self.last_ts:
            return 0.0

        added = 0.0
        if self.last_ts is not None and self.last_kw is not None:
            dt = (ts - self.last_ts).total_seconds()
            if dt <= max_gap_s:
                hours = dt / 3600.0
                added = (self.last_kw + export_kw) / 2.0 * hours
                self.energy_kwh += added
                self.energy_low_kwh += min(self.last_kw, export_kw) * hours
                self.energy_high_kwh += max(self.last_kw, export_kw) * hours
                if (self.last_kw > 0) != (export_kw > 0) or abs(
                    export_kw - self.last_kw
                ) > STEP_KW:
                    self.step_intervals += 1
                self.last_interval_s = dt

        self.last_ts = ts
        self.last_kw = export_kw
        return added

    def reset_baseline(self) -> None:
        """Forget the previous sample, so the next one cannot bridge to it."""
        self.last_ts = None
        self.last_kw = None

    def to_dict(self) -> dict:
        """Serialise the persistent part: totals only, never the baseline."""
        return {
            "energy_kwh": float(self.energy_kwh),
            "energy_low_kwh": float(self.energy_low_kwh),
            "energy_high_kwh": float(self.energy_high_kwh),
            "step_intervals": int(self.step_intervals),
        }

    def uncertainty_attributes(self) -> dict:
        """Return the attributes that let a consumer grade the estimate."""
        return {
            ATTR_LOWER: round(self.energy_low_kwh, 6),
            ATTR_UPPER: round(self.energy_high_kwh, 6),
            ATTR_STEPS: self.step_intervals,
            ATTR_LAST_INTERVAL: self.last_interval_s,
        }

    @classmethod
    def from_dict(cls, data) -> DerivedExportRegister | None:
        """Restore a register, or None if the data is unusable."""
        if not isinstance(data, dict):
            return None
        try:
            energy = float(data["energy_kwh"])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(energy) or energy < 0:
            return None
        # Totals persisted before the bounds existed restore as exact.
        try:
            low = float(data.get("energy_low_kwh", energy))
            high = float(data.get("energy_high_kwh", energy))
            steps = int(data.get("step_intervals", 0))
        except (TypeError, ValueError):
            low, high, steps = energy, energy, 0
        if not (math.isfinite(low) and math.isfinite(high)) or not (
            0 <= low <= energy <= high
        ):
            low, high = energy, energy
        return cls(
            energy_kwh=energy,
            energy_low_kwh=low,
            energy_high_kwh=high,
            step_intervals=max(steps, 0),
        )
