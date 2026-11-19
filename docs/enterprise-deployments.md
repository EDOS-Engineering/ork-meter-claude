# Ork Meter Enterprise deployments: administrator guide

An organization that runs on an Ork Meter Enterprise deployment points its
computers at that deployment with one setting. This page is for the
administrator who sets it. The contract behind it is EDOS-Engineering/ork-meter
`docs/deployment-attestation.md`.

## The setting

Set `ORK_METER_PLUGIN_ORIGIN` to the deployment's exact origin in the `env`
block of Claude Code's managed settings:

```json
{
  "env": {
    "ORK_METER_PLUGIN_ORIGIN": "https://ork.example-customer.com"
  }
}
```

| System | Managed settings file |
| --- | --- |
| macOS | `/Library/Application Support/ClaudeCode/managed-settings.json` |
| Linux | `/etc/claude-code/managed-settings.json` |
| Windows | `C:\Program Files\ClaudeCode\managed-settings.json` |

Drop-in files in `managed-settings.d/` beside it are read too, in name order;
the last one that names the setting wins. A person without managed settings
can put the same line in their user settings (`~/.claude/settings.json`).
Managed settings win over user settings.

The value must be exactly `https://` followed by a lower-case DNS name: no IP
address, port, path or trailing slash.

The plugin reads this setting from those files only. It does not read it from
the process environment or from a project's `.claude/settings.json`, so a
repository can neither choose where usage is sent nor undo your choice.

## What people see

- `/ork-meter:connect` (or the first session after install) connects to your
  deployment. The command's answer and `/ork-meter:status` name the origin and
  the organization EDOS attested for it, for example
  `https://ork.example-customer.com (Example Customer, attested by EDOS)`. The
  local setup page names the origin only.
- People sign in to your deployment and approve the pairing there.
- If savings are not turned on for their account, the session-start line says
  to ask their administrator. It never mentions Ork Meter Plus or billing.

## What makes a deployment acceptable

The plugin connects to an origin outside EDOS's public cells only while that
origin serves, at `/.well-known/ork-meter-deployment`, an attestation that
EDOS signed for exactly that origin and that is valid now. The runtime
fetches it without any credential before pairing and again once a day. The
runtime and the hooks each verify it for themselves, against root keys
compiled into the plugin. The deployment's lever policy is accepted only when
signed by the policy keys that attestation names.

An attestation is valid for at most 90 days and EDOS renews it. If none is
current, nothing is sent to the deployment and the session-start line says
so. Sending resumes by itself once the deployment serves a current one;
nobody has to pair again.

## Changing or removing the setting

The setting decides where a **new** connection goes. It never moves an
existing one:

- A computer already connected somewhere else (Ork Meter, or another
  deployment) stops sending as soon as the setting names your deployment.
  The session-start line names both destinations and tells the person to run
  `/ork-meter:disconnect` and then `/ork-meter:connect`.
- Removing the setting does not disconnect anyone. A connection to your
  deployment keeps working until the person disconnects.
- A value that is not an exact `https://` origin, or a managed settings file
  that cannot be read, stops all sending until it is corrected. A rule that
  cannot be read is not treated as a rule that was lifted.

## What goes where

With the setting in place, token counters and usage events go to your
deployment and nowhere else. Nothing is sent to `https://orkmeter.com`.
What is captured is the same as for Ork Meter; the README's "Runtime and
data" section lists it and what stays on the computer.

## Checking a computer

- `/ork-meter:status` shows the destination. `origin_conflict` means the
  computer is connected to one destination and configured for another, the
  setting cannot be used, or the deployment has no current attestation; its
  next step says which.
- The saved connection is `connection.json` in the plugin's data directory;
  the attestation on file is `attestations/<host>.json` beside it. Neither
  is a secret.
