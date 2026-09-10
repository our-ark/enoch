# Shopping capability research

Status: architecture recommendation, researched 2026-09-07. This document does
not authorize an integration, credential enrollment, affiliate relationship, or
purchase.

## Decision

Start shopping as a **skill for research, comparison, and human handoff**. Add
an optional **shopping extension** only when the agent needs durable lists,
price watches, recurring work, or deterministic retailer API calls. Keep
retailer clients inside that extension package behind a small connector
contract; they are domain integrations, not new Enoch providers.

The first useful release should stop at a merchant-hosted product, list, or
cart URL. The human chooses the actual item and merchant, reviews the current
price, shipping, tax, substitutions, return terms, and seller, then completes
checkout. Autonomous ordering is a separate capability with a substantially
larger authority and security design; it must not arrive as an incidental
extension of product search.

This split follows Enoch's existing boundaries:

| Concern | Home | Reason |
| --- | --- | --- |
| Ask for needs, turn them into constraints, search allowed sources, compare evidence, disclose uncertainty, and present a shortlist | Skill | This is reusable reasoning and operating procedure without owned state or credentials. |
| Saved lists, preferences explicitly supplied for shopping, price-watch state, commands, schedules, audit references, and retailer/API adapters | Extension | These are durable, namespaced domain behavior that can be enabled or removed independently. |
| HTTP, OAuth/API-key handling, rate limiting, response normalization, retailer terms, and affiliate-link construction | Connector internal to the extension | Retailer sources are many-to-one inputs to shopping, not interchangeable execution infrastructure for Enoch itself. |
| Chat, runtime, VCS, forge, and service-manager infrastructure | Existing providers | Shopping does not justify widening the stable provider contract. |
| Credentials and user-specific merchant tokens | Private instance state or an OS credential store, referenced by the extension | They must never enter the software body, task prompt, artifact, memory, or Git history. |

Do not extract a shared shopping library initially. Extract only after at least
two agents or implementations prove a stable, agent-neutral connector/result
contract. A premature library would freeze vendor churn into Enoch's lineage.

## Minimum skill

A `shop` skill can be useful without adding code or credentials. It should:

1. Elicit intended use, hard constraints, location/currency, quantity, timing,
   budget, acceptable condition, and deal-breakers. Ask about health,
   accessibility, compatibility, or sustainability only when relevant.
2. Separate facts from preferences. Treat model numbers, sizes, compatibility,
   ingredients, seller identity, delivery dates, and total cost as facts that
   need source evidence; treat style and trade-offs as user choices.
3. Prefer first-party manufacturer and merchant pages. Use independent testing
   when it adds evidence, and label affiliate or sponsored sources.
4. Compare the same variant and condition. Show item price separately from
   estimated shipping, tax, membership discounts, coupons, subscriptions, and
   rebates. Attach an observed-at timestamp because offers change.
5. Return a small shortlist with source URLs, exclusion reasons, uncertainty,
   and a clear final-check list. Do not claim inventory, delivery, or price is
   guaranteed.
6. Hand the human to the merchant. Never sign in, accept terms, add a payment
   method, redeem stored value, or place an order as part of this skill.

The skill must not silently persist inferred preferences. A user can explicitly
ask a future extension to save a preference; sensitive attributes and one-off
gift context should remain ephemeral by default.

## When an extension earns its place

Create an `our_ark.extensions` package named `shopping` when at least one of
these is required:

- named, durable shopping lists shared across conversations;
- watch rules such as “notify me below $300,” with a schedule and bounded
  notification behavior;
- deterministic access to a retailer catalog or list-link API;
- normalized offer snapshots and provenance needed for later comparison;
- commands such as `/shop list`, `/shop watch`, `/shop unwatch`, and
  `/shop sources`.

The extension should use `.enoch/extensions/shopping/` for private preferences,
watch definitions, source configuration, and rate-limit state, and
`.enoch/artifacts/extensions/shopping/` for bounded comparison reports. API
secrets should be resolved at call time from private configuration or the host
credential store and should not be copied into either location merely for
convenience.

Use an internal connector boundary rather than a core provider kind. A small
first contract is enough:

```text
search(query, constraints, locale) -> offers[]
get_offer(source_offer_id, locale) -> offer
create_handoff(items, locale) -> hosted_url       # optional
```

An offer should carry `source`, opaque source ID, canonical product URL,
merchant/seller, title, identifiers such as GTIN/MPN when licensed, exact
variant, condition, item price and currency, separately reported delivery
cost, availability as reported, observed time, and provenance URLs. Missing
fields remain unknown; connectors must not manufacture a “total” or merge
probably-similar variants.

Commands should make bounded calls with explicit connect/read deadlines,
response-size limits, allowlisted HTTPS hosts, schema validation, redacted
errors, and per-source rate limits. Watches should use Enoch's declarative
schedules and governed workflow rather than a polling thread or second queue.
Stable watch/list IDs should be used as idempotency keys, and notifications
should link to the stored snapshot that triggered them.

## API assessment

No single reviewed API is a neutral, comprehensive consumer-shopping catalog.
Each useful source is narrower than “shopping,” and access terms are part of
the connector design.

| Source | Suitable role | Important constraint | Recommendation |
| --- | --- | --- | --- |
| Instacart Developer Platform | Grocery and household-list handoff | The create-shopping-list endpoint returns an Instacart Marketplace URL; the user selects a store, reviews matched products, and adds them to a cart. Production access uses a bearer API key obtained through Instacart. | Best first deterministic handoff. It preserves human choice and keeps checkout with Instacart. See the [shopping-list flow](https://docs.instacart.com/developer_platform_api/guide/concepts/shopping_list/) and [endpoint authentication](https://docs.instacart.com/developer_platform_api/api/products/create_shopping_list_page). |
| eBay Browse API | Broad marketplace discovery, used/collectible items, GTIN and compatibility search | Browse uses an application OAuth token. eBay says many Buy APIs are limited release and production use is intended for approved partners; checkout requires further approval. | Discovery/link-out connector only at first. Do not plan around Order API access. See [Browse API](https://www.developer.ebay.com/develop/api/buy) and [production requirements](https://developer.ebay.com/api-docs/buy/buy-requirements.html). |
| Best Buy Products API | US electronics catalog, specifications, price, and availability | Requires an API key; Best Buy describes price as near-real-time, while its Commerce API is invite-only. Some catalog uses also carry affiliate conditions. | Useful second discovery connector for electronics; link out for purchase. See the [API catalog](https://developer.bestbuy.com/apis) and [product API documentation](https://bestbuyapis.github.io/api-documentation/). |
| Amazon Creators API | Amazon catalog search and affiliate product detail | It is for Associates publishers/affiliate partners. Current prerequisites include Associates enrollment, registration, credentials, and at least 10 qualifying sales in the prior 30 days. | Optional, never a baseline dependency. Add only after the human deliberately accepts the affiliate program and content-usage terms. See the [official introduction](https://affiliate-program.amazon.com/creatorsapi/docs/en-us/introduction) and [API reference](https://affiliate-program.amazon.com/creatorsapi/docs/en-us/api-reference). |
| Open Food Facts | Barcode, ingredient, allergen, and nutrition enrichment | Community data comes without accuracy/completeness guarantees. Search is rate-limited, and the current docs recommend v3 for new integration while noting full-text search limitations. It is not an authoritative retailer price source. | Optional enrichment with visible provenance; never use alone for safety-critical dietary advice or availability. See the [official API guidance](https://openfoodfacts.github.io/openfoodfacts-server/api/). |
| Google Merchant API | A merchant's own catalog administration | The API manages products belonging to a Merchant Center account; it is not a public consumer product-search API. | Out of scope unless a descendant is itself operating a merchant catalog. See the [product lifecycle guide](https://developers.google.com/merchant/api/guides/products/add-manage). |

Retailer web pages may supplement API coverage through the runtime's normal
research tools, but HTML scraping should not become a hidden connector. It is
brittle, can violate site rules, and makes provenance, freshness, and consent
harder to govern. Prefer documented APIs, feeds, or ordinary human-facing links.

## What must stay out of core

The portable `enoch` package should not contain:

- retailer SDKs, endpoints, response schemas, affiliate tags, catalog
  taxonomies, or vendor-specific retry policy;
- API keys, OAuth refresh/access tokens, merchant cookies, account identifiers,
  addresses, payment instruments, loyalty numbers, or gift-card balances;
- a universal product catalog, offer cache, cross-user purchase history, or
  inferred consumer profile;
- browser automation for sign-in, cart mutation, CAPTCHA handling, acceptance
  of terms, or checkout;
- a new shopping provider kind, a shopping-owned scheduler/worker, or direct
  task-queue mutation;
- hidden sponsored ranking, commission-maximizing selection, or affiliate-link
  rewriting without disclosure;
- medical, allergen, compatibility, authenticity, delivery, or “lowest price”
  guarantees derived from incomplete or stale data.

Core may eventually gain a domain-neutral capability name used by
authorization, such as read-only network access, only if multiple unrelated
extensions need the same governed effect. It should not gain retailer names or
shopping semantics to enable one extension.

## Purchase boundary

“Find and compare” and “spend money” are different authorities. The proposed
skill and extension cover only the former plus hosted handoff. A future
transaction extension would require a separate human-reviewed design with, at
minimum:

- an explicit capability disabled by default and separate from catalog read;
- merchant-scoped OAuth with least privilege and a credential-vault boundary;
- an immutable preview of exact SKU/variant, seller, quantity, condition,
  address class, delivery window, return policy, and final total;
- fresh human confirmation bound to that preview, with expiry and rejection on
  any material change;
- per-order and rolling spend limits, restricted product categories, duplicate
  prevention, idempotency keys, cancellation behavior, and durable receipts;
- no purchase of regulated goods, age-restricted goods, financial products,
  subscriptions, gift cards/cash equivalents, or safety-critical goods until
  each category has its own reviewed policy.

Until that design exists, “add to cart” should also remain merchant-hosted.
Cart mutation creates side effects, can reserve scarce inventory, can leak
account identity, and is an easy place for accidental duplicate actions.

## Recommended delivery sequence

1. Write and test the read-only `shop` skill against several real requests.
   Measure whether it elicits the right constraints, compares exact variants,
   cites observations, and consistently stops at handoff.
2. If durable lists are wanted, build the smallest shopping extension with
   local list state and a hosted-link output. Instacart is the cleanest first
   API experiment for grocery/household lists; otherwise start without a
   retailer API.
3. Add one read-only discovery connector only when a concrete use case needs
   structured catalog data. Contract-test normalization, missing fields,
   stale observations, throttling, and secret redaction with fixtures.
4. Add price watches only after defining notification frequency, stale-data
   behavior, retention, and deletion. Keep schedules declarative and work
   extension-scoped.
5. Revisit a shared connector library or transaction capability only from
   accumulated evidence and as a separate reviewed change.

The success criterion for the first capability is not “the agent bought the
item.” It is that the human reaches a defensible decision faster, understands
the evidence and uncertainty, and retains control of every consequential
choice.
