# Vicegerent notifier

This directory contains the source for Vicegerent's macOS notification helper. Host-package reconciliation compiles it into a signed application bundle under `~/.vicegerent/notifier` and registers that bundle with LaunchServices. The executable remains a normal CLI: direct invocations return meaningful exit codes and write authorization, delivery, and removal failures to stderr.

The helper uses Apple's supported `UserNotifications` framework. Each MCP or credential notification group is the native request identifier, so an unhealthy poll leaves an existing notification alone, recreates one the user dismissed, and a healthy poll removes the exact delivered notification. Reconciliation clears the helper's delivered records after rebuilding it so the watcher recreates active failures with the current copy and notification settings. Vicegerent requires **System Settings → Notifications → Vicegerent → Alert Style → Persistent**, not **Temporary**, because health failures must remain onscreen until recovery; the helper's `status`, `authorize`, and `post` commands report the temporary style as unavailable.

Run the installed executable directly when diagnosing notification delivery; unlike `open`, it remains attached to the terminal and reports the actual failure:

```bash
"$HOME/.vicegerent/notifier/Vicegerent Notifier.app/Contents/MacOS/vicegerent-notifier" status
"$HOME/.vicegerent/notifier/Vicegerent Notifier.app/Contents/MacOS/vicegerent-notifier" post vicegerent-test "Vicegerent notification test" "Remove this test by identifier."
"$HOME/.vicegerent/notifier/Vicegerent Notifier.app/Contents/MacOS/vicegerent-notifier" list
"$HOME/.vicegerent/notifier/Vicegerent Notifier.app/Contents/MacOS/vicegerent-notifier" remove vicegerent-test
```
