---
name: shop
description: Help the human shop as a buyer across Shopify merchants. Identify the product category, collect budget plus category-specific constraints, search the Global Catalog, compare a few purchasable options, and hand off merchant URLs. Do not use a merchant Storefront token or complete checkout.
---

# Shop

## Outcome

Act as the human's shopping assistant, not as a store's private catalog client.
Search Shopify's buyer-facing Global Catalog across merchants for whatever
product category they named, recommend two to four suitable purchasable
options, and give merchant URLs so they can review details and complete
checkout themselves.

## Who this is for

This skill is for a **customer / buyer**. It must not require
`shopify.store_domain` or `shopify.storefront_token`. Those credentials belong
to a merchant app and must not be requested, stored, or used for this flow.

If the human already named one merchant they want to stay inside, scope the
catalog search to that storefront. That is still a buyer query, not a merchant
admin integration.

## Procedure

1. Identify the **product category** from the request (for example running
   shoes, wireless earbuds, a jacket). If the category is unclear, ask once
   before searching. Do not assume footwear.
2. Collect missing **shared** constraints before searching:
   - budget and currency
   - country or shipping region
   - quantity and timing when they affect the buy
   - deal-breakers the human already stated
3. Also collect missing **category-specific** constraints. Ask only for
   attributes that change the catalog query or whether an option is usable.
   Do not ask for shoe size when the human wants earbuds.

   | Category | Ask when missing (besides budget and country) |
   | --- | --- |
   | Running / athletic shoes | Size and sizing system; intended use (road, trail, racing, daily trainer); width or stability only if they mention fit issues |
   | Other apparel / footwear | Size and sizing system; gender or fit range if the catalog is split that way; material or weather use if relevant |
   | Wireless earbuds / headphones | Fit (in-ear, over-ear); must-haves such as ANC, transparency, waterproofing, USB-C or wireless charging case, microphone / call quality, codec or jack needs |
   | Phones / laptops / headphones-adjacent electronics | Storage or generation if it changes the SKU; must-have ports or features; condition (new vs refurbished) |
   | Other goods | The 1–3 attributes that distinguish a usable SKU in that category (size, compatibility, dietary/material constraint, condition). Skip lifestyle questions that do not change the search. |

   Separate facts (size, budget, compatibility, seller) from preferences
   (style, brand vibe). Ask about health, accessibility, or safety only when
   the category makes them material.
4. Discover products with Shopify's Universal Commerce Protocol catalog, not
   a merchant Storefront Admin/custom-app token.
   - Prefer the `ucp` CLI from `@shopify/ucp-cli` when it is on PATH.
   - Omit `--business` to search the Global Catalog across merchants.
   - Pass `--business https://<merchant-domain>` only when the human named a
     specific store.
   - Include buyer context such as `--set /context/address_country=US` (or the
     country the human gave). Optional override:
     `ENOCH_SHOPIFY_ADDRESS_COUNTRY` or `shopify.address_country` in
     `.enoch/config.yaml`.
   - Put the category and collected constraints into the query. Example:

     ```bash
     ucp catalog search \
       --set /query='wireless earbuds ANC USB-C case under $100' \
       --set /context/address_country=US \
       --view :compact \
       --format md
     ```

   - When a promising product appears, use `ucp catalog get_product` to confirm
     the relevant variant attributes, price, seller domain, and the merchant
     buy / product URL.
5. If `ucp` is missing or `ucp doctor` fails, stop. Tell the human to install
   the buyer tooling from https://shopify.dev/docs/agents/get-started/quickstart
   (`npm install -g @shopify/ucp-cli`, then `ucp profile init --name enoch` and
   `ucp doctor`). If this is a read-only chat turn and the failure is only that
   the sandbox cannot read `~/.ucp`, tell them to rerun the same request with
   `/do` rather than inventing catalog rows. Do not scrape storefront HTML as
   a hidden connector. Do not ask for a Storefront access token.
6. Filter to variants that match the stated category constraints and look
   currently purchasable. For shoes, require a matching size when one was
   given. For electronics, require the stated must-have features when they
   can be verified. Treat inferred catalog fields, ambiguous labels, missing
   prices, and absent availability as uncertainty. Prefer results that include
   a merchant domain and a concrete product or permalink URL.
7. Compare two to four strong options within budget when the catalog permits,
   preferably from more than one merchant when the query was not store-scoped.
   Do not dump raw `ucp` tables and do not use a markdown table. Telegram
   cannot render tables, and one message only unfurls the first URL. If a
   table with several `/products/` URLs is emitted anyway, Telegram still
   splits it into one message per product so each can show a thumbnail.
   After a search, the last chat message is a **Latest shop page (tN)**
   link to `http://127.0.0.1:36624/shop/tN` on this Mac: product tabs plus
   the same chat. `/status` also shows that latest link. Do not iframe
   merchant pages or complete checkout there.

   Write **one card per product**. Put `<!-- telegram:break -->` on its own
   line between cards. Telegram splits that marker into separate messages:
   three products become three messages, and each message gets its own link
   preview thumbnail. Do not put two product URLs in the same card. Do not
   add a header-only message that has no product URL.

   Each card must contain exactly one merchant `http` or `https` URL, as a
   bare URL on the last line (not only a markdown link). Number the cards so
   the human can say "more on 2" or "drop 1."

   ```text
   1. <name> — <price> <currency>
      Need: <category> · <budget> · <country>
      Store: <merchant>
      Variant: <exact variant or key specs>
      Why: <fit to stated use>
      Uncertain: <missing or inferred fields>
      https://<merchant-product-url>

   <!-- telegram:break -->

   2. <name> — <price> <currency>
      Need: <category> · <budget> · <country>
      Store: <merchant>
      Variant: <exact variant or key specs>
      Why: <fit to stated use>
      Uncertain: <missing or inferred fields>
      https://<merchant-product-url>
   ```
8. If fewer than two verified matches exist, return the verified matches and
   say what prevented a fuller comparison (over budget, missing size, no URL).
   Never invent products, variants, prices, availability, merchants, or URLs.

## Credential Handling

- Do not read, request, or send `shopify.storefront_token`,
  `ENOCH_SHOPIFY_STOREFRONT_TOKEN`, Admin API tokens, or app client secrets.
- A UCP agent profile created by `ucp profile init` identifies this agent to
  Shopify. Do not print the profile file or any tokens from `~/.ucp/`.
- Keep API and CLI errors sanitized. Report a concise cause without echoing
  secrets, headers, or profile contents.

## Commerce Boundary

This skill is buyer discovery and handoff only. It must not create or mutate a
cart, convert a cart to checkout, call `ucp checkout complete`, log in to a
customer account, reserve inventory, place an order, or handle payment
details. If the catalog or CLI returns a `continue_url` or buy permalink, give
that URL to the human and stop. The human owns every purchase decision and
completes checkout independently.
