---
name: payment-integration
description: Design, implement, review, or modernize secure payment integrations using provider-neutral controls and current Stripe or PayPal flows. Use for checkout, subscriptions, webhooks, refunds, disputes, reconciliation, or payment-provider migration. Do not use to initiate live charges, captures, refunds, subscriptions, or payouts without explicit user authorization.
---

# Payment Integration

Build payment systems around one shared safety model, then apply the selected provider's current API semantics. Treat provider responses and webhooks as external evidence, not as the application's source of truth.

## Route the Work

- For any integration, read [references/shared-payment-core.md](references/shared-payment-core.md).
- For Stripe, also read [references/stripe.md](references/stripe.md).
- For PayPal, also read [references/paypal.md](references/paypal.md).
- For another provider, use the shared core and its official documentation. Do not transfer Stripe or PayPal assumptions to it.
- If no provider has been selected, clarify business requirements before recommending one. Do not choose solely from implementation familiarity.

## Establish the Scope

Determine the following before changing code:

1. Payment model: one-time purchase, recurring billing, marketplace, invoicing, or payout.
2. Provider and API generation already in use.
3. Countries, currencies, payment methods, taxes, and settlement requirements.
4. Hosted checkout versus custom payment UI.
5. Required lifecycle operations: authorize, capture, cancel, refund, dispute, subscription change, or reconciliation.
6. Existing order records, payment attempts, webhook storage, queues, and accounting exports.
7. Test versus live environment and who may authorize live mutations.

When reviewing an existing system, trace one complete payment from order creation through settlement or refund before proposing changes.

## Enforce Shared Invariants

- Keep price, currency, tax, discount, merchant account, and payable amount authoritative on the server.
- Never accept raw card numbers or security codes into application logs, prompts, analytics, or general-purpose storage.
- Represent money with integer minor units or an exact decimal type plus an explicit ISO currency.
- Give each business order a stable internal ID and each provider attempt its own record.
- Use provider-supported idempotency for retried mutations and persist the key with the attempt.
- Treat timeouts and connection failures after submission as unknown outcomes; query or reconcile before retrying.
- Verify webhooks with the provider's official mechanism using the exact payload representation it requires.
- Persist verified events before processing, deduplicate them, process asynchronously, and make state transitions idempotent.
- Expect duplicate, delayed, and out-of-order events. Never rely on delivery order alone.
- Separate sandbox/test and live credentials, endpoints, data, logs, alerts, and operational permissions.
- Reconcile internal records against provider records and settlement data on a schedule.

## Implementation Workflow

1. Model the business order and allowed state transitions independently of provider objects.
2. Create a payment-attempt boundary that owns provider IDs, idempotency keys, request status, and error evidence.
3. Implement server-side create or update operations using an official maintained SDK where practical.
4. Return only client-safe tokens, session IDs, or approval URLs to the browser or mobile client.
5. Add the verified webhook ingress, durable event inbox, asynchronous processor, retry policy, and dead-letter path.
6. Apply monotonic, idempotent domain transitions based on both event data and provider queries when needed.
7. Implement refund, dispute, cancellation, and subscription paths as explicit workflows rather than flags.
8. Add reconciliation and operational tooling for replay, investigation, and manual repair with an audit trail.
9. Test in the provider's sandbox with duplicates, reordering, timeouts, retries, partial refunds, and failed payments.
10. Require a deliberate live-readiness review before enabling live credentials or executing live mutations.

## Required Output for Design or Review

Provide concrete artifacts appropriate to the task:

- Chosen provider flow and why it fits.
- Trust boundaries and server/client responsibilities.
- Domain state machine and provider-to-domain mapping.
- Data model for orders, attempts, events, refunds, and subscriptions when applicable.
- Idempotency, webhook, retry, replay, and reconciliation strategy.
- Failure-mode and test matrix.
- Credential, logging, audit, and production rollout controls.

Call out uncertainty when provider settings, account capabilities, region support, or API versions have not been verified.

## Authorization Boundary

Code generation, sandbox calls, read-only inspection, and test fixtures are allowed when within the user's request. Before any live charge, capture, refund, subscription change, transfer, or payout, obtain explicit authorization for the exact operation and target. Never infer approval from a request to design, review, or test an integration.

## Avoid These Patterns

- Trusting client-provided totals or marking an order paid from a browser redirect.
- Treating a webhook endpoint as a synchronous business processor.
- Retrying an ambiguous mutation with a new idempotency identity.
- Coding webhook verification as generic HMAC without following provider-specific rules.
- Using obsolete SDKs or legacy API families for new integrations.
- Equating provider status names directly with domain states without a mapping layer.
- Logging secrets, access tokens, complete payloads containing sensitive data, or payment-method details.
- Hiding manual payment-state edits without actor, reason, timestamp, and before/after values.
