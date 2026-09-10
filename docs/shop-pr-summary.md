# Add UCP shopping discovery, local shortlist UI, and Telegram product previews

## Summary

Buyers can ask Enoch to compare products across Shopify merchants, receive
individual product cards in Telegram, and continue comparing the saved shortlist
in a local browser with the same conversation. The shop skill uses Shopify's
UCP catalog and hands off merchant URLs for the human to complete checkout.

## Changes

- Add and register the `shop` skill with category-specific questions, budget and
  country constraints, Global Catalog discovery, product-detail verification,
  and two-to-four-option comparisons. Its declared commerce permissions and
  instructions exclude cart changes, checkout, customer login, and orders.
- Add `enoch.local_web`: an authenticated loopback HTTP server, product tabs,
  merchant links, conversation polling, asynchronous chat submission, and
  thumbnail discovery/cache with bounded downloads.
- Save shortlists from completed task output, maintain a latest-shortlist index,
  and backfill matching task history. Include the selected shortlist and product
  tab in browser follow-up context.
- Integrate web-server startup/shutdown with the application, serialize chat
  dispatch, record browser chat receipts and conversation turns, and deliver
  browser replies through the configured chat provider. Append the latest shop
  page link to task results and `/status`.
- Extend Telegram rendering with explicit card breaks and automatic splitting
  of product comparisons, including supported Markdown tables, so each product
  message can receive its own link preview.
- Add regression coverage for skill discovery, local web authentication and
  routes, conversation handling, shortlist context, thumbnails, and Telegram
  presentation/delivery.
- Add shopping capability architecture research and the detailed
  [UCP/local web setup guide](shoop-skill.md); link the shop skill and guide from
  the README.

## Configuration and private state

Local web is enabled by default at `127.0.0.1:36624`; configure or disable it
through `local_web` in private `.enoch/config.yaml`. Generated page links include
an access token stored in `.enoch/local_web.json`. Shortlists and thumbnails
live under `.enoch/artifacts/shop/`; UCP profiles remain in `~/.ucp/`.
Credentials and user shopping data are not part of the versioned body.

## Validation

- `python3.13 -m unittest discover -s libraries/telegram/tests`: **29 passed**.
- `python3.13 -m unittest tests.test_enoch_skills tests.test_enoch_local_web`:
  **21 passed, 3 errors** because the sandbox denied localhost socket binding.
  Permission to rerun outside the sandbox was declined; those three checks
  remain unverified in this session.
- `git diff --check`: passed.
- The initial `python3` attempts used the host's Python 3.9 and failed imports;
  validation was repeated with supported Python 3.13.
- Full core suite and live UCP/Telegram/browser end-to-end validation were not
  run. UCP installation/profile/search instructions were checked against
  [Shopify's official quickstart](https://shopify.dev/docs/agents/get-started/quickstart)
  on 2026-09-10.

## Review considerations

The shop skill defines the commerce boundary through instructions and metadata;
this change does not add a dedicated transaction-enforcement layer. The local
page can access the configured conversation and dispatch chat commands, so its
token-bearing links must remain private. Thumbnail fetching reads merchant
metadata separately from UCP search, and images may be unavailable.

The Telegram changes are in the checked-in provider source; the existing
published reference dependency pin is unchanged. Deployments using that pinned
package need a provider release/pin update or an explicit installation of the
updated local provider to receive the new rendering behavior.
