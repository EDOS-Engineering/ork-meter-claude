# Failure behaviour (#45)

What Claude Code, the plugin's hooks and usage capture do when Ork Meter, the
network, the credential or the runtime fails, what `/ork-meter:status` shows,
and which test proves it. The design rule is that the plugin never blocks
Claude: every hook exits 0 within its timeout (`hooks/hooks.json`), the Python
hooks never use the network (tests/no_egress_regressions.py), and only the
bundled runtime (ork-meter's `cmd/claude-plugin-runtime`) talks to Ork Meter.

The rows describe the behaviour of the runtime built from ork-meter cb4b2b9
(ork-meter #357 typed failures and the observed-queue set-aside and breaker,
#358 expiry signals, #359 background delivery, #362 renewal), which closed the
four gaps first recorded here (#353 no renewal or expiry signal, #354 status
could not name revocation or expiry, #355 a permanent rejection blocked the
queue and nothing backed off, #356 a slow Ork Meter held Claude's export).
`tests/native_failure_connected_qualification.py` asserts each fix; it is
pinned to the runtime's SHA-256 (`PINNED_RUNTIME_SHA256`) and must be re-run
and re-pinned whenever `runtime/` changes.

Where tests live:

- **plugin, not connected**: `tests/native_failure_qualification.py` (the
  real CLI and the shipped runtime against the localhost fixture; every
  platform's CI, through `.github/scripts/check-native-host.py`).
- **plugin, connected**: `tests/native_failure_connected_qualification.py`
  (Linux CI only, the `connected-failure` job: a real pairing, driven through
  an `xdg-open` shim, against a fake Ork Meter (`tests/fake_platform.py`)
  behind a TLS-terminating proxy, with a throwaway CA trusted on that runner
  only and gnome-keyring in a D-Bus session private to the job; every request
  body is scanned for the canaries of #44).
- **ork-meter**: Go tests in EDOS-Engineering/ork-meter, cited as
  `path:TestName`.

Status states are those of `/ork-meter:status` (skills/status/SKILL.md):
`not_connected`, `setup_pending`, `connection_unverified`,
`connected_waiting_for_usage`, `receiving_usage`, `authorization_unverified`,
`authorization_refused` and `authorization_expired`, with
`authorization_expiry`, `last_delivery`, `last_export` (the receiver's last
export state, e.g. `unsupported_export`), `observed_totals` (`counting` or
`unavailable`), `observed_pending`, `observed_delivery`, `observed_set_aside`
and `observed_rejected` alongside.

| Failure | Effect on Claude and hooks | Effect on capture | What status shows | Verified by |
|---|---|---|---|---|
| Ork Meter down (connection refused, 5xx) | None. The SessionStart hook is bounded (the runtime caps itself at 2 s, inside the hook's 5 s timeout) and prints at most one line; the other hooks never use the network; the header callback answers locally. Claude's export is acknowledged at once and delivered in the background (ork-meter #359), so a session ends as fast as with Ork Meter up. | The latest filtered snapshot waits in memory and is retried 5, 15 and 45 s after a failure with the same observation ID; the next export carries newer cumulative values. Observed-usage events stay in `observed-usage-<id>.json` and are retried with the same IDs, backing off from 1 minute up to 30 minutes (#357). | `authorization_unverified` ("check network and account access"); a failed check never claims revocation. `last_delivery: unacknowledged`. | plugin, not connected: `tests/native_failure_qualification.py:test_refused`, `test_5xx`. plugin, connected: `tests/native_failure_connected_qualification.py:test_down`, `test_5xx` (and recovery once Ork Meter is back). ork-meter: `internal/localcapture/background_test.go:TestHangingDeliveryIsRetriedWithSameSnapshot`, `internal/pluginruntime/runtime_test.go:TestStatusDoesNotConvertNetworkFailureIntoRevocation` |
| Ork Meter slow | None: hooks and SessionStart are unaffected and Claude's export is acknowledged at once while delivery runs in the background (ork-meter #359; before it, a slow Ork Meter held each export up to 15 s). | Delivered late, or retried as for "down" when the attempt times out. | `authorization_unverified` once its check times out (10 s). | plugin, not connected: `tests/native_failure_qualification.py:test_slow`. plugin, connected: `tests/native_failure_connected_qualification.py:test_slow_ork_meter_does_not_hold_the_export`. ork-meter: `internal/localcapture/background_test.go:TestPluginExportReturnsWhileDeliveryHangs` |
| DNS failure or offline | As "down". | As "down". | `authorization_unverified`. | plugin, not connected: `tests/native_failure_qualification.py:test_offline`. plugin, connected: `tests/native_failure_connected_qualification.py:test_down` (a refused CONNECT is the same path for the runtime). |
| 429 rate limited | As "down". | Retried no sooner than the platform's `Retry-After` (capped at an hour), snapshots and observed events alike, with the same IDs (#357). | `last_delivery: rate_limited` in both cases below. While the status check itself is answered 429 (as the connected suite's fake answers every route), `authorization_unverified` with the "check network and account access" next step, or a receiver-side observed-capture problem followed by "The platform could not be reached to verify authorization." When the status check succeeds and only deliveries are rate limited, `authorization: verified` and, with observed events waiting (`observed_pending` above 0), a next step saying Ork Meter asked this computer to slow down and the runtime retries later with the same events. The two never appear together: the slow-down step is only on the verified path. | plugin, connected: `tests/native_failure_connected_qualification.py:test_429_retry_after_is_honoured`. ork-meter: `internal/localcapture/typed_failure_test.go:TestSnapshotPathHandlesTypedPlatformFailures` |
| Credential revoked (401/403) | None. `entitlement.json` is removed at the next entitlement refresh (at most every 15 minutes), so savings levers then turn off; capture is not gated. | Delivery stops: the snapshot is not retried automatically and observed delivery is `parked`, durably across restarts, until the computer is paired again (#357). | `authorization_refused` (`authorization: refused`). The next step names revocation or expiry as the usual cause, says a proxy, firewall or lost account access answers the same way, and warns that disconnecting deletes the observed usage still waiting, with its count. | plugin, connected: `tests/native_failure_connected_qualification.py:test_revoked_403`. ork-meter: `internal/pluginruntime/entitlement_test.go:TestEntitlementOfflineKeepsLastAnswerAndRefusedCredentialClearsIt`, `internal/pluginruntime/setaside_test.go:TestObservedStateIsReportedPerReference` |
| Credential near expiry | SessionStart prints one line, once per state, with the expiry date (#358). The first session after an install or update does not reach the runtime (row below), so the notice then appears at the next session. | Once fewer than seven days remain, the detached entitlement refresh renews the credential to 30 days ahead (`POST /v1/collectors/renew`; the platform extends a credential at most once a day, counted from its claim or latest renewal, so never in its first day). A failed renewal backs off from 30 minutes to 6 hours, honouring `Retry-After`; a refused renewal is recorded and never retried until the computer is paired again. Renewals show for the account owner in Ork Meter (#362). | `authorization_expiry: authorization_expiring` with `authorization_expires_at`, and a next step with the date and days left that says to disconnect and connect again to renew before then. Background renewal runs in the same seven-day window, so the state usually clears on its own while Claude Code is in use; it persists when renewal is refused, keeps failing or cannot extend (an account whose access ends sooner). | plugin, connected: `tests/native_failure_connected_qualification.py:test_expiring_then_expired`, `test_renewal_extends_a_credential_near_expiry`, `test_refused_renewal_is_never_retried`. ork-meter: `internal/pluginruntime/renewal_test.go:TestRenewalExtendsWithinTheWindowAndStoresTheReceipt`, `TestRefusedRenewalStaysStoppedUntilRepaired`, `internal/pluginruntime/expiry_test.go:TestSessionNoticeIsShownOncePerState` |
| Credential expired | SessionStart prints one line, once, saying it expired (#358); as for near expiry, at the next session when the first one after an install or update set the runtime up. | Deliveries stop once the expiry plus a 5-minute clock-skew margin has passed; an expired credential is never renewed (#358, #362). | `authorization_expired` (`authorization: expired`), decided locally without asking Ork Meter, with the date and the reconnect step. | plugin, connected: `tests/native_failure_connected_qualification.py:test_expiring_then_expired`. ork-meter: `internal/pluginruntime/expiry_test.go:TestStatusReportsAnExpiredAuthorizationWithoutAskingThePlatform`, `TestDeliveriesStopOnlyAfterTheClockSkewMargin` |
| OS credential store locked or missing | None. SessionStart says to run `/ork-meter:connect` once, the same notice as not connected. | Nothing is captured: the receiver cannot read its credential. There is no plaintext fallback. | `connection_unverified`, "Unlock the OS credential store and retry". | plugin, not connected: every case of `tests/native_failure_qualification.py` on Linux CI (no Secret Service reachable). |
| First session after an install or update (no installed runtime for this SHA yet) | None. `hooks/session-start.sh` does not install in the hook: it starts `scripts/runtime.sh --install` detached and at once prints one line, "setting up its runtime in the background; it is ready from your next session", and exits 0. The install (copy, digests, one `--version` run so that a first-exec scan such as Windows Defender's happens there) finishes on its own after the hook and the CLI exit; a lock makes concurrent sessions install once, and a staging file or lock left by a killed install is cleared after 5 or 10 minutes. Without this, a cold Windows hook took over 4.3 s more than a warm one and could be cancelled at its 5 s timeout (#140). | That session's SessionStart does not reach the runtime (no receiver check, no entitlement refresh). A connected install keeps capturing through the header callback, which still names the previous runtime's copy; the next session's hook moves it to the new one. | Unchanged: `/ork-meter:status` installs in the foreground if it runs first. | plugin: `tests/session-start.test.sh` (cold returns at once, detached install completes, next session warm, concurrent sessions install once, survives its process group being killed, no session secrets reach the installer), `tests/runtime.test.sh` (`--install`, lock, stale staging files), `tests/native_runtime_install_qualification.py` (cold hook under the hook timeout and a real cold `claude` session, every platform's CI). |
| Runtime binary missing or corrupt | None. `hooks/session-start.sh` prints one line, "automatic capture is unavailable", and exits 0. `scripts/runtime.sh` refuses a package whose SHA-256 does not match `SHA256`. A damaged installed copy is replaced from the verified package: in the background, as for a first session, when SessionStart finds it, in the foreground when a skill does. | None while the binary is unusable. | The status skill fails with the runtime's integrity message. | plugin: `tests/runtime.test.sh` (tampered, damaged, missing, stamp mismatch), `tests/session-start.test.sh` ("missing runtime stays non-blocking", "a damaged install is replaced in the background"). |
| Lost reply after commit | None. | The snapshot is retried with the same observation ID and capture time; observed events with the same event ID and body. The server's idempotent receipt makes the replay a no-op. | `receiving_usage` once the replay is acknowledged. | plugin, connected: `tests/native_failure_connected_qualification.py:test_lost_reply_replay` (the same observation ID and event ID are replayed; the fake keeps one copy). ork-meter: `internal/pluginruntime/observed_test.go:TestObservedRestartPersistenceAndLostReply` |
| Duplicate export | None. | A point not newer than the stored one adds nothing to observed usage; snapshots are cumulative and non-additive by design. | Unchanged. | ork-meter: `internal/pluginruntime/observed_test.go:TestObservedDeltasResetsDuplicatesAndGaps` |
| Permanent rejection (400, 409, 422) | None. | A rejected snapshot is dropped locally (the next export makes a new one). A rejected observed event is set aside, never dropped: later events continue, and it is re-sent after other events are accepted and then every 1 to 12 hours. Only after repeated rejections over at least a day while others were accepted is it final, and even then it is kept and reported. If every event is rejected (a clock ahead of the platform, a platform change), delivery is `held` with backoff instead (#357). | `observed_set_aside`, `observed_rejected` or `observed_delivery: held`, each with its next step; `last_delivery: rejected`. | plugin, connected: `tests/native_failure_connected_qualification.py:test_permanent_rejection_does_not_block_the_queue`. ork-meter: `internal/pluginruntime/setaside_test.go:TestObservedQueueAtLimitContinuesPastARejectedHead`, `TestObservedRefusedEventIsFinalOnlyOnRepeatedEvidence`, `TestP1ShortPlatformWideOutageLosesNothing` |
| Receiver or Claude restart during an outage | None. | On a graceful stop the pending snapshot gets one bounded delivery attempt (#359); otherwise it is lost and the next cumulative export carries its values. Observed events, their backoff and any hold survive in `observed-usage-<id>.json` and are sent after the restart with their original IDs. | As for the outage, then `receiving_usage`. | ork-meter: `internal/localcapture/background_test.go:TestPluginFlushOnStopSendsThePendingSnapshotOnce`, `internal/pluginruntime/setaside_test.go:TestObservedBackoffSurvivesARestart`, `internal/pluginruntime/observed_test.go:TestObservedRestartPersistenceAndLostReply` |
| Re-pair with queued events | None. | Events queued under the old pairing are discarded, not sent to the new one; their usage is not recovered. | `connected_waiting_for_usage` for the new pairing. | ork-meter: `internal/pluginruntime/observed_test.go:TestObservedRepairingDiscardsOldReferenceEvents` |
| Queue full (2,000 events) | None. | No new observed event is queued; the series are left unchanged, so the next cumulative value carries the usage once delivery resumes. Snapshots continue. | `observed_pending` at 2,000 and a next step about delivery. | ork-meter: `internal/pluginruntime/observed_status_test.go:TestStatusReportsObservedEventsWaitingForDelivery` |

## Not connected

Without a connection the runtime has no credential and sends nothing, in any
network condition: `tests/native_failure_qualification.py` asserts the proxy
sees no request in all four modes, and that a one-turn `claude --print` is
within 2 s of the baseline. Local measurements are in its log (macOS, Claude
Code 2.1.302: 0.56 s against a 0.61-0.65 s baseline, SessionStart hook
0.16 s, status 0.14 s, in every mode).

## Content

Whatever fails, no prompt, completion, tool output, path or repository name
leaves the machine through the plugin: tests/native_canary_qualification.py
(#44) and tests/no_egress_regressions.py. With the runtime's telemetry flags
Claude Code's metrics export carries none of them; a repository's project
settings can turn attribute flags back on (Claude Code then sends the
repository owner, name and URL as metric attributes), and ork-meter's receiver
filter drops them before upload. The per-release record is described in
[release.md](release.md#per-release-canary-record-44).
