# Our Ark Claude runtime provider

`our-ark-claude` connects an Our Ark agent body to the locally installed
Claude Code CLI. It implements the versioned runtime contract from
`our-ark-provider-kit` and is discovered through the `runtime.claude` entry
point.

The provider runs Claude non-interactively with structured streaming output,
keeps logical Enoch sessions mapped to Claude session IDs, reports token usage
and progress, and honors runtime cancellation and timeout controls. Read-only
conversation turns and workspace-writing task turns receive separate bounded
tool policies. The provider never enables Claude's bypass-permissions mode.

From an Enoch source checkout, the local provider is discovered automatically.
For an installed body, install this package separately, authenticate Claude,
and select it:

```bash
python -m pip install ./libraries/claude
claude auth login
bin/enoch config provider runtime claude
```

Provider-specific settings are available through:

```text
/config runtime claude
/config runtime claude executable <path|auto>
/config runtime claude max-budget <usd|off>
```

The generic `/config model` and `/config reasoning-effort` commands configure
the selected Claude model and effort level.
