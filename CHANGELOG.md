# Changelog

## 0.3.5 - 2026-07-26

- Select the exact OCR-matched contact/result instead of clicking the calibrated
  fixed first-result coordinate. This prevents WeChat's similarly placed chat
  history result from opening a modal and aborting the body-paste stage.
- Keep the existing per-segment FIFO and cancellable review behavior unchanged:
  text and image items across all contacts share one strict queue, and each
  successful paste waits a newly sampled three to five seconds before its own
  send action.
- If another window briefly takes the foreground at the final submit boundary,
  retain the current FIFO head and send it after WeChat returns instead of
  dropping it and advancing to the next item.

### Automated verification

- Text and image sends use the exact OCR contact selector.
- Consecutive segments and different-contact sends remain serialized and
  receive their own three-to-five-second post-paste delay.
- A transient foreground loss at the send-button boundary cannot skip the
  current queue item.

## 0.3.4 - 2026-07-26

- Remove the redundant contact-title OCR check between focusing the message input and injecting `Ctrl+V`; the selected contact is still verified immediately after selection and again before final submission.
- Keep bot-owned text on the clipboard for 500 ms after `Ctrl+V` before conditionally clearing it, allowing slower WeChat message editors to consume the paste without overwriting newer user clipboard content.
- Preserve the existing cancellable review window: one second before paste and ten seconds after paste by default, with pause, resume, cancellation, and FIFO behavior unchanged.

### Automated verification

- The send-order regression test requires no OCR between message-input focus and body paste while retaining both safety checks.
- Deterministic clipboard-delay coverage verifies that a delayed target reads the body before the 500 ms conditional cleanup.

## 0.3.3 - 2026-07-25

- Restore the calibrated first-result step after entering a contact name, while requiring the clicked result surface to belong to the same WeChat process and retaining contact-title verification before message input and final submission.
- Make `校准.bat` use the same tokenized Bridge PID resolver as start and stop, including Windows virtual environments whose live PID belongs to the base Python interpreter.
- Raise the punctuation-first reply fallback from a strict 15-character maximum to 25 characters.

### Automated verification

- The fixed four-point send sequence, same-process popup boundary, wrong-process rejection, title-verification failure, base-interpreter calibration lockout, and 25-character segmentation cap are covered by focused regression tests.
- The unified Windows release gate continues to verify installer preservation, lifecycle safety, calibration, release allowlists, and the complete Python suite.

## 0.3.2 - 2026-07-25

- Recover automatically when a previous Bridge process has exited but left `data\state\bridge.pid` behind, including the PID-reuse case observed after an update.
- Refuse to overwrite a live or unverifiable Bridge owner, create the Python PID record exclusively, and remove it only when the current process still owns the exact record.
- Let `停止.bat` verify and stop an orphan Bridge even when `processes.json` is empty, while retaining the existing executable, command-line, and creation-time identity checks.
- Extend the Bridge startup stability check beyond the previous 300 ms window and write a bounded, credential- and path-redacted `data\logs\bridge-startup.log` when imports or startup fail.
- Declare Win32 process APIs with pointer-safe signatures so PID creation-time checks remain reliable on 64-bit Windows.
- Remove the unintended chat pseudonym/metadata-only behavior: local structured `CHAT` records and the loopback Web panel again retain the complete contact, group sender, and message body. API keys, tokens, and configured runtime paths remain filtered.

### Automated verification

- Stale PID recovery, live orphan refusal and stop, delayed startup exit rollback, tokenized PID fail-closed behavior, exclusive PID ownership, ownership-safe cleanup, and complete local chat records are covered by focused regression tests.
- The unified Windows release gate continues to verify installer preservation, lifecycle readiness, plugin deployment, release allowlists, credential filtering, and the complete Python suite.

## 0.3.1 - 2026-07-25

- Preserve image and video descriptions in a dedicated SQLite semantic field while retaining WeFlow's authoritative raw media marker, so Qwen session rebuilds no longer lose visual context.
- Add inbound WeChat video understanding through the configured OpenAI-compatible visual provider. Video export is matched to the exact WeFlow `serverId`, downloaded only from the configured local WeFlow origin with Bearer authentication, limited to 6 MiB for Base64 API compatibility, and deleted after transcription.
- Keep failed media descriptions as ordinary media markers instead of durable semantic memory, and use enriched media text consistently for token estimates, relevant-history retrieval, and rebuilt Qwen context.
- Upgrade existing contact-memory databases in place from schema 5 to schema 6 without changing confirmed message rows or copying API credentials.
- Prevent stale `bridge.pid` files from blocking startup after Windows reuses a PID by binding ownership to the process creation time; legacy PID-only records are verified through the bridge status endpoint before reuse.
- Normalize legacy string-valued Web panel ports, allow safe loopback socket reuse, and retry a temporarily unavailable port before emitting the fixed `E_BRIDGE_WEB_BIND` diagnostic.

### Automated verification

- Media confirmation, pre-existing authoritative rows, schema migration, rebuilt context, exact video matching, local credential boundaries, and Qwen `video_url` request construction are covered by focused tests.
- The unified Windows release gate continues to verify installer preservation, lifecycle readiness, plugin deployment, privacy allowlists, and the complete Python suite.

## 0.3.0 - 2026-07-24

- Add an installer-managed AstrBot 4.26.6 plugin that permanently archives private WeChat history in local SQLite and isolates memory by the stable `(WeChat account, sessionId)` identity rather than nickname.
- Give each private contact an independent Qwen Conversations session while reusing one existing AstrBot Provider and API key. Switching A → B → A resumes A's cloud conversation without re-seeding its complete history.
- Seed new or expired Qwen sessions from at most 150,000 estimated tokens of relevant older records and recent confirmed history; rotate at a 700,000-token soft limit or after the provider's seven-day retention window.
- Import the latest 2,000 WeFlow records on first contact use, fall back to 500 within the foreground budget, and continue an uncapped local backfill in the background.
- Persist collision-safe OneBot identities in `data\state\bridge_identity.sqlite3`, reject private events without a stable `sessionId`, and carry per-source message references through buffered OneBot events.
- Require a concrete bot `wxid` for private identity isolation, store only salted HMAC identity mappings, and reject ambiguous or unverifiable nickname routes before UIA can select the first result.
- Deploy plugin code only after AstrBot initialization with exact-file validation, atomic replacement, rollback, and preservation of plugin data. Existing installs start in `shadow` mode.
- Protect the local contact HMAC key with Windows DPAPI, encrypt raw account/session identifiers, keep API credentials in the existing AstrBot Provider, and require explicit confirmation before forgetting a contact.

### Automated verification

- Stable contact identity, same-nickname isolation, A → B → A cloud-session reuse, generated-output reconciliation, and local forget behavior are covered by focused plugin tests.
- Bridge identity persistence, missing-session fail-closed behavior, buffered source references, installer atomicity, update preservation, rollback, and release allowlists are covered by the existing unified Windows test gate.

## 0.2.8 - 2026-07-23

- Probe the WeFlow sessions endpoint during startup so current WeFlow versions do not reject readiness checks that omit the required message `talker`.

### Automated verification

- The delayed-readiness lifecycle regression now verifies the compatible sessions endpoint while preserving token, retry, and timeout behavior.

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
