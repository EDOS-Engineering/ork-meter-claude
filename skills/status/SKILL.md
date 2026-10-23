---
name: status
description: Check the saved connection, automatic receiver and hosted token-export receipts. No reference ID is needed.
disable-model-invocation: true
allowed-tools: Bash(bash "${CLAUDE_PLUGIN_ROOT}/scripts/runtime.sh" --plugin-data "${CLAUDE_PLUGIN_DATA}" status)
---

# Ork Meter status

Run:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/scripts/runtime.sh" --plugin-data "${CLAUDE_PLUGIN_DATA}" status
```

The runtime remembers the connection. Never ask for a reference, inspect the
credential store or read private settings. This command does not start capture,
change configuration, retry delivery or initiate pairing.

Report `state`, `authorization`, `receiver` and `next_step` (quote
`next_step` as given: it is the runtime's own recovery text), any
`latest_export` capture/receipt times and sample counts, and these fields when
present:

- `authorization_expires_at` and `authorization_expiry`: when the computer's
  credential expires. The runtime renews it in the background once fewer than
  seven days remain, while Claude Code is in use (at most every 15 minutes);
  `authorization_expiring` means that has not happened yet. Its `next_step`
  says to disconnect and connect again to renew before the date: quote it,
  then say that with days left the person can keep using Claude Code and
  check status later, because background renewal usually clears it, and that
  disconnecting deletes observed usage still waiting. If it is still
  expiring close to the date (renewal refused, failing, or capped by the
  account's access), reconnect before then as `next_step` says.
- `last_export`: the receiver's last export state, e.g. `export_acknowledged`,
  `delivery_unacknowledged`, `delivery_refused` or `unsupported_export`
  (Claude's export had a shape the runtime does not count).
- `observed_totals`: `counting`, or `unavailable` when the local tracker file
  could not be read or written (snapshot capture continues).
- `last_delivery`: the receiver's last delivery answer, one of `acknowledged`,
  `unacknowledged` (network or server trouble; retried), `rate_limited`
  (Ork Meter asked it to slow down; retried later), `rejected`, `invalid` or
  `refused`.
- `observed_pending`: observed token events waiting for delivery;
  `observed_set_aside`: events Ork Meter rejected, kept and re-sent later;
  `observed_rejected`: events refused for at least a day while others were
  accepted, kept locally and still re-sent every 12 hours, but not counted
  until accepted.
- `observed_delivery`: `held` while Ork Meter rejects every event (nothing is
  dropped; delivery retries with growing pauses), `parked` once delivery
  stopped because the credential was refused.

States:

- `setup_pending`: finish browser setup.
- `connected_waiting_for_usage`: authorized, without recent current-receiver
  delivery evidence. Missing receipt is not zero usage.
- `receiving_usage`: a recent export was acknowledged after this receiver
  started; not proof of complete history or future delivery.
- `authorization_unverified`: Ork Meter could not be reached or answered
  with an error. Check network/account access. Do not assume revocation or
  tell the person to re-pair blindly.
- `authorization_refused`: Ork Meter refused this computer's credential
  (HTTP 401/403) and delivery has stopped. Usually revoked or expired, but a
  proxy, firewall or an account that lost access answers the same way, so
  those come first.
- `authorization_expiring` is reported in `authorization_expiry` with the
  date; `authorization_expired`: the credential expired and usage is no
  longer sent. Reconnecting (`/ork-meter:disconnect`, then
  `/ork-meter:connect`) issues a new one.
- `connection_unverified`: the OS credential store is locked or unreadable,
  or Ork Meter's receipt did not match the authorization stored on this
  computer.

Before suggesting `/ork-meter:disconnect`, say plainly that disconnecting
deletes any observed usage still waiting on this computer
(`observed_pending`, `observed_set_aside` and `observed_rejected`), which can
then never be sent; `next_step` gives the count when there is one.

Cumulative samples are non-additive and excluded from billed spend. Do not add
them into totals, costs or savings. Never call `headers`, accept credentials or
run the legacy `ork-meter` executable.
