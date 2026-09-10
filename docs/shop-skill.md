# Shop skill: UCP setup and local web guide

The `shop` skill helps a buyer find and compare products across Shopify
merchants, then supplies merchant links for human checkout. It collects budget,
country, and category-specific requirements and returns a small numbered
shortlist. It does not create carts, log into customer accounts, or place orders.

## 1. Prepare the host

Use a working Enoch checkout and configured chat/runtime providers as described
in the [README](../README.md#quick-start). Install the tools below under the same
OS account that runs Enoch so its runtime can find both `ucp` and the profile.

Check Node.js and npm:

```bash
node --version
npm --version
```

Shopify documents Node.js 18 or higher as its minimum; use a currently supported
Node.js LTS release. If Node.js is missing, install it from
[Node.js](https://nodejs.org/en/download), reopen the terminal, and repeat the
checks. These tool installation commands can run from any directory.

## 2. Install UCP and initialize a profile

```bash
npm install -g @shopify/ucp-cli
command -v ucp
ucp profile init --name enoch
ucp doctor
```

Initialization creates and activates a local agent profile under `~/.ucp/`.
Run it once for this host account; an existing healthy profile can be reused.
Keep profile contents and tokens private. No merchant Storefront token or Admin
API credentials are required by Enoch's buyer flow.

These commands follow the [Shopify UCP quickstart](https://shopify.dev/docs/agents/get-started/quickstart),
checked on 2026-09-10. Enoch already includes its own shop skill, which invokes
the CLI directly. Its implementation does not require an additional Shopify
editor plugin.

## 3. Verify catalog discovery

Run a read-only search in your terminal:

```bash
ucp catalog search \
  --set /query='wireless earbuds ANC USB-C case under $100' \
  --set /context/address_country=US \
  --view :compact \
  --format md
```

Use your shipping country and actual requirements. Keep the query in single
quotes so the shell preserves the dollar sign. Omitting `--business` searches
the Global Catalog. The skill uses `--business https://<merchant-domain>` only
when the buyer explicitly chooses a store.

Confirm that the response contains real catalog results or an explicit empty
result, rather than a setup or authentication error. Results are observations,
not guarantees of current stock, shipping, or final price. The skill follows
promising results with `ucp catalog get_product` to check variant details.

## 4. Start Enoch and request a shortlist

From the checkout, restart an already configured daemon so it loads this branch:

```bash
cd /path/to/enoch
bin/enoch-daemon restart
```

Use `bin/enoch-daemon start` for an instance that is not running, or
`bin/enoch-agent` for foreground operation with configured providers. Ensure
the service's environment can find the same Node.js and UCP executables as your
terminal; a service may have a different `PATH`.

In your configured chat, send `/skills` and confirm `shop` is listed. Then send:

```text
/do Use the shop skill to find wireless earbuds under USD 100, shipping to the US, with ANC and a USB-C charging case. Compare up to four options and give merchant links. Do not purchase anything.
```

The response should contain numbered product cards with price, merchant,
variant/specifications, fit to your needs, uncertainties, and a merchant URL.
Telegram splits cards into separate messages so each can have a link preview;
the merchant's metadata determines whether a thumbnail is available.

## 5. Open the local shop page

After a completed task produces a recognizable product shortlist, open its
**Latest shop page (tN)** link, or obtain the latest link with `/status`.
Use the browser on the computer running Enoch: `127.0.0.1` on a phone refers to
the phone, not the Enoch host.

The default address is `http://127.0.0.1:36624/shop/tN`, with an access token
in the generated link. Open the full link, including `?token=...`; the bare URL
returns an authentication error. Treat the link as private because it grants
access to the local shopping and conversation interface.

The page offers product tabs, cached preview images when available, merchant
links, and recent conversation history. Ask a follow-up such as "Compare this
one with option 2" to include the current shortlist and selected tab in the
agent's context. Browser replies are also delivered to the configured chat.
Merchant links open the handoff; merchant pages are not embedded for checkout.

## 6. Optional local web configuration

Merge this section into the existing private `.enoch/config.yaml`, preserving
your other settings:

```yaml
local_web:
  enabled: true
  bind: 127.0.0.1
  port: 36624
```

These are the defaults. Set `enabled: false` to disable the web listener, or
choose another unused fixed port if necessary. Restart Enoch after editing.
Use `127.0.0.1` for this setup; non-loopback bindings are rejected.

| Local data | Location |
| --- | --- |
| UCP profile | `~/.ucp/` outside the checkout |
| Local web access token | `.enoch/local_web.json` |
| Saved shortlists and latest index | `.enoch/artifacts/shop/shortlists/` |
| Cached product thumbnails | `.enoch/artifacts/shop/thumbs/` |

These are private host/instance data and must stay out of commits and PRs.
Shortlists can also be reconstructed from matching completed task history when
the web server starts. Thumbnail retrieval reads merchant preview metadata;
it is separate from UCP catalog discovery.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `node`, `npm`, or `ucp` not found | Reopen the terminal after installation; check `command -v node` and `command -v ucp`. Ensure the daemon's `PATH` includes their directories. |
| npm reports a global-install permissions error | Use a user-managed Node.js installation or a writable npm global prefix, then retry installation. |
| `ucp doctor` fails | Resolve the reported profile, authentication, or network issue under the daemon's OS account before asking Enoch to search. Do not paste profile contents or tokens into chat. |
| Terminal UCP works, but read-only chat cannot access `~/.ucp` | Retry the shopping request with `/do`, as the shop skill directs. If it still fails, inspect the runtime permissions and host-account setup. |
| Local page says `token required` | Open the complete latest link from the task result or `/status`. |
| Browser cannot connect | Confirm Enoch is running on this computer, local web is enabled, and the configured port is free. Check daemon output for a local web startup error. |
| No shortlist yet | Complete a `/do` search that returns product cards with merchant URLs. A conversation alone or a failed catalog search does not create a shortlist. |
| Missing image | A merchant may not provide usable metadata or may block fetching; use the merchant link to inspect the product. |
| Browser says Enoch is not locked to one chat | Finish configuring the allowed conversation using the README's chat setup steps. |

See the [shop procedure](../src/enoch/skills/shop/SKILL.md) for response rules
and the [shopping capability research](shopping-capability.md) for the broader
architecture assessment. The research document describes recommendations;
this guide describes the implementation on this branch.
