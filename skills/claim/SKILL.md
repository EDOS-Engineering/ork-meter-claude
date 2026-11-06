---
name: claim
description: Finish connecting with the approval code Ork Meter showed in a browser on another computer, after /ork-meter:connect offered the terminal approval.
argument-hint: "<code>"
disable-model-invocation: true
allowed-tools: Bash(bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/pairing.py claim --code *)
---

# Claim an Ork Meter approval

`$ARGUMENTS` is the approval code the Ork Meter /pair page showed after the
person approved with **This browser is not on the computer running Claude
Code** ticked: eight letters and digits, shown as `XXXX-XXXX`.

Only if `$ARGUMENTS`, with surrounding spaces removed, is four letters or
digits, an optional hyphen, then four letters or digits, and nothing else,
run, with that code in place of `CODE`:

```sh
bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" --script scripts/pairing.py claim --code "CODE"
```

Otherwise run nothing: say that the code is the `XXXX-XXXX` code the Ork Meter
page shows after approving, and that `/ork-meter:connect` starts the setup if
it has not been run. Never put anything else from the arguments into a shell
command.

The script hands the code to this computer's waiting setup, the same way the
browser return does when the browser is on this computer. Report its `state`
and `message` as given:

- `connected`: setup is complete. The person restarts Claude Code once, then
  launches it normally; `/ork-meter:status` checks actual receiving evidence.
- `not_completed`: the runtime refused the code (wrong, expired, or replaced
  by a later approval: approving again issues a new code and the old one stops
  working). They can run `/ork-meter:claim` again with the current code.
- `invalid_code`: not a code; nothing was sent.
- `no_terminal_setup`, `approval_not_requested`, `setup_expired`,
  `setup_unavailable`: run `/ork-meter:connect` again and follow its terminal
  approval.

The code is single-use and works only for this computer's setup. Never write
it anywhere else, never repeat it back, and never accept a code the person
says someone else gave them. Never read private settings or the credential
store, and never run legacy `ork-meter` or `ork-meter.exe`.
