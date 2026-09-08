---
name: shop
description: Find and compare running shoes available from Enoch's configured Shopify store when the human wants product recommendations and direct product URLs, not purchasing or checkout.
---

# Shop

## Outcome

Recommend two to four suitable, currently purchasable running-shoe variants from
the configured Shopify store. Give the human direct product URLs so they can
review the products and complete checkout themselves.

## Procedure

1. Before searching, ask for any missing essentials: shoe size and sizing
   system, budget and currency, and intended use such as road, trail, racing, or
   daily training. Ask about width or other constraints only when they affect
   the search.
2. Read the store connection from environment variables
   `ENOCH_SHOPIFY_STORE_DOMAIN` and `ENOCH_SHOPIFY_STOREFRONT_TOKEN`, falling
   back to the `shopify.store_domain` and `shopify.storefront_token` values in
   `.enoch/config.yaml`. Environment variables take precedence. Normalize the
   domain to a hostname, rejecting any path or unexpected scheme.
3. If either setting is missing, explain which setting is needed. Never ask the
   human to paste a token into conversation.
4. Query only that store's Shopify Storefront GraphQL API. Search product title,
   description, type, tags, and variants using the intended use and constraints.
   Fetch enough variant data to verify size, availability, price, currency,
   product handle, and `onlineStoreUrl`. Do not infer availability from the
   product-level status alone.
5. Filter to variants that match the requested size and are available for sale.
   Treat ambiguous size labels, missing prices, stale-looking data, and absent
   availability as uncertainty rather than silently assuming a match.
6. Compare two to four strong options within budget when the catalog permits.
   For each, report product name, exact variant (including size and width when
   present), price with currency, direct product URL, a brief fit-for-use reason,
   and any uncertainty. Use `onlineStoreUrl` when present; otherwise construct
   `https://<store-domain>/products/<handle>` from returned Storefront data.
7. If fewer than two verified matches exist, return the verified matches and
   clearly say what prevented a fuller comparison. Never invent products,
   variants, prices, availability, or URLs.

## Credential Handling

- Use the Storefront token only as the `X-Shopify-Storefront-Access-Token`
  request header.
- Never print, log, quote, persist, commit, or include the token in a URL, shell
  argument, error message, recommendation, or transcript.
- Keep API errors sanitized. Report HTTP status and a concise cause without
  echoing request headers or configuration contents.

## Commerce Boundary

This skill is product discovery only. It must not create or mutate a cart,
initiate checkout, log in to a customer account, reserve inventory, place an
order, or handle payment details. Stop after providing product URLs; the human
owns every purchase decision and completes checkout independently.
