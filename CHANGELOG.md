# Changelog

## 0.2.1 - 2026-07-23

- Record structured single-line bridge audit entries containing private contacts, group names and members, full inbound message bodies, and full Bot outbound bodies with sent/failed status.
- Keep configured credentials, credential-shaped values, and local filesystem paths redacted from logs.
- Stop returning raw `bridge.log` content from the unauthenticated Web status endpoint and remove its cross-origin response header.
- Document that `bridge.log` contains sensitive local chat data and must be manually redacted before sharing.

### Automated verification

- Private and group inbound records, text/image/face outbound records, multiline Unicode bodies, failed sends, secret/path redaction, and Web status log isolation are covered by the bridge runtime tests.

## 0.2.0 - 2026-07-17

- Keep one fixed-point UIA sending path and remove the two superseded sender modules.
- Add strict four-point calibration schema validation, Windows input capture, atomic calibration persistence, and start-time calibration gates.
- Add the installed `校准.bat` entry while keeping desktop shortcuts limited to start, stop, and health check.
- Allow a clean uncalibrated install to finish without starting services; preserve an existing valid calibration during update.
- Expose only calibration readiness and the fixed sender mode in bridge status, without exposing stored calibration details.
- Update release hygiene checks for the exact 50-file public snapshot and the four direct bridge dependencies.

### Automated verification

- Calibration schema, capture ordering, cancellation, atomic persistence, sender behavior, start gating, installer rollback, public payload layout, dependency allowlist, and secret/path hygiene are covered by the repository test suite.
- Live WeChat interaction and real multi-monitor DPI behavior are not claimed by automated tests.

## 0.1.0 - 2026-07-17

- Prepare the first clean public bridge installer release.
- Isolate bridge and AstrBot Python environments.
- Add local WeFlow installer selection, safe configuration, and health checks.

### Release verification

- Clean isolated install: PASS on Python 3.12.10 x64 using an existing WeFlow executable.
- Repeat install: PASS; AstrBot data and the bridge token were preserved and a backup was created.
- Bridge and AstrBot `pip check`: PASS (exit code 0).
- AstrBot version: 4.26.6.
- Existing WeFlow configuration remained unchanged during isolated verification.
- Live four-service health check: NOT RUN in isolated verification.
- Manual WeChat round trip: NOT RUN; it requires the user's WeChat login and model-provider credentials.
