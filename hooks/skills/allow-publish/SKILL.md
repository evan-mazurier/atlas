---
name: allow-publish
description: The operator allows one publish (a push, release or deploy) in this session. User-only.
disable-model-invocation: true
argument-hint: "[n]"
---

The operator typed `/allow-publish` and has allowed $ARGUMENTS publish command(s) in this session (one if no number was
given, at most 5), if the grant was recorded: the hooks log it when the message is sent, and the publish gate only opens
once that record exists. The grant expires after 30 minutes or when the session ends, and only the main session can use it.

You may now run the publish command the operator asked for (a `git push`, a release, a deploy, an upload). Each such
command uses up one allowance; the next one is refused again until the operator types the command again. Do not run
this command yourself, do not try to invoke it from a shell, and do not schedule it or send it to another session:
only the operator can allow a publish, by typing it.
