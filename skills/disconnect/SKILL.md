---
name: disconnect
description: Stop automatic capture, remove owned Claude settings and local credentials, and open the saved connection's remote revocation review.
disable-model-invocation: true
allowed-tools: Bash(bash "${CLAUDE_PLUGIN_ROOT}/scripts/runtime.sh" --plugin-data "${CLAUDE_PLUGIN_DATA}" disconnect)
---

# Disconnect Ork Meter

The person explicitly invoked disconnect. Run:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/scripts/runtime.sh" --plugin-data "${CLAUDE_PLUGIN_DATA}" disconnect
```

This stops the internal receiver, removes only unchanged settings the plugin
installed and deletes this local connection's credentials. It also deletes any
observed usage still waiting on this computer for delivery (what
`/ork-meter:status` reports as `observed_pending`, `observed_set_aside` or
`observed_rejected`); that usage can never be sent afterwards. Say so in the
report. Unrelated settings
and later edits are preserved. No reference is required. The person confirms
remote revocation in the browser page it opens.

Report `local_disconnected` accurately. `remote_revocation: "not_checked"`
means remote revocation has not been verified; tell the person to complete
the browser review. Local removal does not prove remote revocation or erase
received usage. Report fixed failures honestly. Never wipe configuration,
accept secrets or run legacy `ork-meter`.
