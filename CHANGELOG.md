# Changelog

## 0.2.7 - 2026-07-23

- Split AstrBot LLM replies at ordinary and full-width spaces, tabs, line breaks, blank lines, and existing sentence-ending punctuation.
- Enforce a strict maximum of 15 characters per emitted text segment while preserving punctuation runs and decimal points.
- Remove whitespace boundaries after splitting so consecutive whitespace and blank lines cannot create empty outgoing messages.

### Automated verification

- Ordinary spaces, tabs, blank lines, punctuation runs, decimal points, and the strict 15-character cap are covered by initialization regression tests.

## 0.2.6 - 2026-07-23

- Keep the Bridge lifecycle active while WeFlow is still starting and retry the local API every two seconds instead of deactivating after the first connection refusal.
- Start a fresh AstrBot OneBot client thread for every Bridge lifecycle generation, removing the stale boolean that prevented reconnection after a failed first generation.
- Preserve immediate stop behavior, treat an invalid WeFlow token as terminal, and rate-limit readiness warnings.

### Automated verification

- Delayed WeFlow readiness, invalid-token shutdown, stop-during-retry, and OneBot thread recreation after restart are covered by lifecycle regression tests.

## 0.2.5 - 2026-07-23

- Show recent structured inbound and outbound chat records in the local Web panel with complete contact, group member, direction, status, and message body fields.
- Include the complete target contact in the live outbound review preview.
- Keep the chat endpoint separate from the 500 ms status poll, bound backward log scanning, and reject non-loopback Host or cross-origin write requests.
- Render chat data through DOM text nodes and escape configuration values before inserting the settings form HTML.

### Automated verification

- Chat history parsing, long Unicode and multiline bodies, bounded limits and scanning, fixed read errors, loopback request checks, safe DOM rendering, and contact-aware send previews are covered by regression tests.

## 0.2.4 - 2026-07-23

- Pre-confirm the installer-owned AstrBot data directory before running `astrbot init`, removing the interactive confirmation that rejected PowerShell's BOM-prefixed standard input on clean computers.
- Keep native environment output out of the installer return object so a completed calibration-required install exits successfully instead of printing a false `E_INSTALL_FAILED`.
- Preserve fresh-install ownership checks, rollback cleanup, generated dashboard credentials, and repeat-install behavior.

### Automated verification

- Fresh initialization now verifies that AstrBot's directory marker exists before the initializer runs.
- Installer regression coverage verifies that native environment output cannot escape into the final structured install result.

## 0.2.3 - 2026-07-23

- Wait for the aggregate service health check to become ready after installation instead of failing on the first startup probe.
- Stop starting new readiness probes after a 90-second monotonic retry deadline, wait for the current probe to finish, and retry at two-second intervals.
- Record readiness start, completion, failure, and attempt counts in `install.log` while preserving the one-shot behavior of the standalone health launcher.

### Automated verification

- Immediate readiness, delayed readiness, deadline exhaustion, retry order, attempt logging, and the preserved installed state after a health timeout are covered by installer-layout regression tests.

## 0.2.2 - 2026-07-23

- Preserve the latest structured chat logging and resumable pre-send review queue from `0.2.1`.
- Fix calibration, start, stop, and health batch launchers so their source-relative install root does not acquire a trailing literal quote during Windows PowerShell native argument parsing.
- Add an installer-layout regression gate covering all four installed launchers.

### Automated verification

- Launcher argument transport is checked before the full installer, calibration, lifecycle, Python, and release-hygiene suites run.

## 0.2.1 - 2026-07-23

- Record structured single-line bridge audit entries containing private contacts, group names and members, full inbound message bodies, and full Bot outbound bodies with sent/failed status.
- Keep configured credentials, credential-shaped values, and local filesystem paths redacted from logs.
- Stop returning raw `bridge.log` content from the unauthenticated Web status endpoint and remove its cross-origin response header.
- Document that `bridge.log` contains sensitive local chat data and must be manually redacted before sharing.
- Enable punctuation-first AstrBot segmented replies with a 45-character fallback and human-like random intervals.
- Add a FIFO text review queue with a one-second pre-paste preview, a ten-second post-paste hold, exact per-item cancellation, and pause/resume that preserves the remaining timer.
- Preserve existing nested UIA calibration data while adding safe defaults for the new review delays.

### Automated verification

- Private and group inbound records, text/image/face outbound records, multiline Unicode bodies, failed sends, secret/path redaction, and Web status log isolation are covered by the bridge runtime tests.
- Preview visibility, exact cancellation, pause/resume, FIFO isolation, stop/restart generation safety, clipboard ownership, and segmented configuration upgrades are covered by regression tests.

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
