# Our Ark Slack provider

`our-ark-slack` connects an Our Ark agent to Slack through Socket Mode. It does
not require a public HTTP endpoint.

## Create the Slack app

1. Create a Slack app from [`slack-app-manifest.yaml`](slack-app-manifest.yaml).
2. Under **Basic Information**, create an app-level token with
   `connections:write`. This is the `xapp-...` token.
3. Install the app to the workspace and copy its `xoxb-...` bot token.
4. Open the app's Messages tab and send a message. Copy the DM conversation ID
   and your Slack user ID from Slack's UI.

The manifest requests only the bot scopes used by the provider: receiving DMs
and mentions, posting and editing messages, and adding the read acknowledgment
reaction.

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
commands, so this provider uses `!` as a secondary command prefix:

```text
!help
!task add investigate retry behavior
!evolve list
```

In a channel, mention the agent before the command, for example
`@Enoch !help`. The agent core retains `/help`, `/task`, and the rest of its
canonical command surface; translation happens only at the Slack boundary.

The transport persists each supported Socket Mode envelope under the agent's
private channel state before acknowledging it. Tokens and temporary Slack
response URLs are removed before that durable write.
