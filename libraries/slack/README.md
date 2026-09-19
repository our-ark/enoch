# Our Ark Slack provider

`our-ark-slack` connects an Our Ark agent to Slack through Socket Mode. It does
not require a public HTTP endpoint.

Command arguments accept Slack's `<https://...>` and `<https://...|label>`
link formats. The provider restores the actual URL before dispatch, so pasting
a cross-repository PR link into `.pr show` or `.pr merge` works normally. Display
labels never determine the target. Ordinary conversation, code spans, and the
original event payload remain intact. A bare PR number still refers to the
agent's current repository; use a full link for another repository.

## Create the Slack app

1. Create a Slack app from [`slack-app-manifest.yaml`](slack-app-manifest.yaml).
2. Under **Basic Information**, create an app-level token with
   `connections:write`. This is the `xapp-...` token.
3. Install the app to the workspace and copy its `xoxb-...` bot token.
4. Open the app's Messages tab and send a message. Copy the DM conversation ID
   and your Slack user ID from Slack's UI.

The manifest requests only the bot scopes used by the provider: receiving DMs
and mentions, posting and editing messages, and adding the read acknowledgment
reaction, reading files shared with the app (`files:read`), and sending governed
outbound artifacts (`files:write`). Existing apps must add both file scopes and
reinstall to the workspace for file access and upload.

## Install and configure

From an Enoch source checkout:

```bash
python -m pip install ./libraries/slack
bin/enoch config provider chat slack
bin/enoch setup bot-token <xoxb-token>
bin/enoch setup app-token <xapp-token>
bin/enoch setup conversation <conversation-id>
bin/enoch setup user <user-id>
bin/enoch-daemon restart
```

Credentials can instead be provided through `ENOCH_SLACK_BOT_TOKEN` and
`ENOCH_SLACK_APP_TOKEN`. The provider also recognizes the agent-neutral
`OUR_ARK_SLACK_*` names and Slack's conventional `SLACK_BOT_TOKEN` and
`SLACK_APP_TOKEN` names.

Send natural language directly in the Messages tab. Slack reserves slash
commands, so this provider uses `.` as its default command prefix:

```text
.help
.task add investigate retry behavior
.evolve list
```

`!` remains supported as a compatibility prefix and fallback, for example
`!help` or `!task add investigate retry behavior`. Help and startup messages use `.`.
The prefix must immediately precede a command name at the start of the message.

In a channel, mention the agent before the command, for example
`@Enoch .help`. The agent core retains `/help`, `/task`, and the rest of its
canonical command surface; translation happens only at the Slack boundary.

The transport persists each supported Socket Mode envelope under the agent's
private channel state before acknowledging it. Tokens and temporary Slack
response URLs are removed before that durable write.

File-only messages and captioned messages both carry all Slack file references
to the agent. Downloads resolve file IDs with `files.info` and authenticate only
HTTPS requests to `files.slack.com`; file-size limits also apply while streaming.
Enoch retains documents in private channel state and supplies bounded PDF text
previews plus local paths for later research tasks. Scans, encrypted PDFs, and
failed downloads produce explicit status instead of silently dropping the file.

Outbound files use Slack's external upload sequence: `files.getUploadURLExternal`,
binary upload, then `files.completeUploadExternal`. The provider accepts only
verified `artifact://` references under the current instance artifact store,
rechecks content type, extension, size, digest, regular-file status, and symlink
containment before reading, and shares only to the configured conversation lock.
Generated images are imported into that store by Enoch before delivery. PNG,
JPEG, WebP, GIF, PDF, UTF-8 text formats, and validated Office Open XML files are
supported. `slack.max_upload_bytes` defaults to 20 MiB and may be configured up
to 100 MiB.

For interruption-safe upload reconciliation, retain both `files:write` and
`files:read`. The latter lets the provider confirm that a file returned by a
timed-out completion call was already shared instead of completing or posting it
again. Permission failures and files rejected by the safety boundary preserve
the text reply and append an actionable attachment fallback notice.
