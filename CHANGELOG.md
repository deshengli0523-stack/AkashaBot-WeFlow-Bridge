# Changelog

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
