# purcell-lab/ocpp fork: branches and upstreaming order

This fork carries fixes and a V2G export feature found while running a
Sigenergy EVDC (OCPP 1.6J) against `lbbrhzn/ocpp`. Every change lives on its
own branch, based on upstream `main` at `a33616a`, so each can be proposed
upstream separately. The install branch `ccr-1174808c-ul1kmg` merges them all
for HACS and is the only branch that carries fork-only changes.

## Independent branches (each based on upstream `main`)

| Branch | What it fixes or adds | Upstream reference |
|---|---|---|
| `fix/v16-preserve-measurand-list` | A charger that cannot report measurands (`MeterValuesSampledData` unknown) no longer blanks the configured list; `post_connect` no longer mutates `entry.data` in place. Note: genuine changes are now saved, so a mismatched entry reloads once. | lbbrhzn/ocpp#1760 |
| `fix/restore-metrics-in-native-unit` | Meter Start restored after a restart is converted from the HA display unit (e.g. MWh) instead of being read as kWh. | new issue |
| `fix/v16-stop-after-finishing` | A session lost across an HA restart is still matched to its StopTransaction from the persisted transaction store; connector 0 no longer shadows connector 1 on the flat Transaction Id sensor. The live failure that prompted it is not fully explained yet: confirm with debug logging on the next remote stop. | new issue |
| `feat/measurand-context-source` | `context_source: charger / defaulted` next to every published `context`. | new issue |

## Stacked V2G export series (merge in this order)

Each branch builds on the one above it.

1. `feat/derived-export-from-negative-import`: opt-in option; negative `Power.Active.Import` / `Current.Import` split into export flows; trapezoidal `Energy.Active.Export.Register`, persisted, never bridging gaps or restarts.
2. `feat/derived-export-current`: `Current.Export` from power and voltage when the charger reports 0 A while discharging.
3. `feat/flow-direction-sensor`: `Flow.Direction` (import / export / idle, 0.1 kW deadband).
4. `feat/session-export-energy`: `Energy.Session.Export` per transaction.
5. `feat/derived-export-uncertainty`: lower and upper bounds, step-interval count and last interval on the register.
6. `feat/adaptive-export-sample-interval`: fast `MeterValueSampleInterval` (default 10 s) while exporting, released afterwards.
7. `feat/export-reference-divergence`: optional reference entity, divergence attributes only.

## Fork-only changes (install branch only, never upstream)

- `hacs.json` without `zip_release`, so HACS installs from branch contents.
  The same change is on the fork's `main`, because HACS reads `hacs.json` from
  the default branch.
- `Carry context_source onto the derived export flows`: an integration commit
  that only makes sense once both `feat/measurand-context-source` and the
  export series are present. Fold it into the export series if both land
  upstream.

## Evidence

Live observations, the V2G run and the accuracy analysis behind these
changes are recorded in
[purcell-lab/ha-bsv-settlement#61](https://github.com/purcell-lab/ha-bsv-settlement/issues/61).
