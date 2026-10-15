---
name: allow-config
description: The operator allows changes to instructions, skills, agent definitions and settings in this session for 15 minutes. User-only.
disable-model-invocation: true
---

The operator typed `/allow-config` and has allowed changes to instructions (`CLAUDE.md`), skills, agent definitions,
commands, rules and settings in this session for 15 minutes, if the grant was recorded: the hooks log it when the message
is sent, and the config gate only opens once that record exists. The grant expires after 15 minutes or when the session
ends, and only the main session can use it.

You may now apply the exact change you already showed the operator, and nothing beyond it. Do not run this command
yourself, do not try to invoke it from a shell, and do not schedule it or send it to another session: only the operator
can allow such a change, by typing it.
