# Shared Payment Core

Use this reference for every provider. Provider-specific references refine these rules but do not replace them.

## Trust Boundary

The client may express intent, such as an order ID, selected plan, or approved payment method. The server must resolve the actual product, merchant, amount, currency, tax, discounts, and capture policy from trusted data.

Keep secret keys and provider access tokens on the server. Send the client only narrowly scoped values intended for client use, such as a checkout session ID, client secret, or approval URL.

Prefer hosted payment surfaces or provider-controlled components to reduce exposure to regulated payment data. Never persist card verification values.

## Provider-Neutral Data Model

Use names appropriate to the codebase, but preserve these responsibilities:

### `orders`

- Internal immutable order identifier.
- Customer, merchant, currency, and authoritative total.
- Business state such as `pending`, `paid`, `partially_refunded`, `refunded`, `disputed`, or `cancelled`.
- Version or transition guard for concurrent updates.

### `payment_attempts`

- Internal attempt identifier and order identifier.
- Provider, provider account, provider object IDs, and API generation.
- Requested operation, amount, currency, and capture mode.
- Idempotency key and a fingerprint of the intended request.
- Local status, provider status, last error category, and timestamps.

Create a new attempt when the business intent materially changes. Reuse the same attempt and idempotency identity when retrying the same intent.

### `provider_events`

- Provider and provider event ID under a unique constraint.
- Arrival time, verification result, event type, relevant object IDs, and payload location.
- Processing status, attempt count, next retry time, processed time, and last error.
- API or event schema version where available.

Store sensitive payloads only when necessary, encrypted with restricted access and a retention policy. A hash plus selected normalized fields may be sufficient for some systems.

### `refunds`, `subscriptions`, and `disputes`

Use separate records for independently changing lifecycles. Persist provider IDs, amounts, currency, status, reason, and links to the originating attempt. Avoid compressing multi-step provider behavior into one boolean on the order.

## Idempotency and Unknown Outcomes

Generate idempotency identities on the server and persist them before the provider call. Scope them to one intended mutation, not to a user session or an unlimited time range.

If the request times out after it may have reached the provider:

1. Mark the attempt outcome unknown.
2. Retry with the same provider idempotency identity when the provider supports it.
3. Query by the persisted provider ID or metadata when available.
4. Let webhook evidence and reconciliation converge the state.
5. Do not create a replacement charge merely because the client did not receive a response.

## Webhook Boundary

Use a thin ingress handler:

1. Preserve the raw request representation required by the provider.
2. Verify authenticity using the provider's official method and current documentation.
3. Validate timestamp or replay protections where the provider defines them.
4. Insert the event into a durable inbox under a unique provider/event key.
5. Acknowledge promptly after durable acceptance.
6. Process asynchronously with bounded retries and a dead-letter or investigation state.

The worker must be safe to rerun. Load current domain state, resolve the relevant provider object, apply only valid transitions, record evidence, and commit atomically. For ambiguous or stale events, query the provider before changing business state.

## State Mapping

Define an explicit mapping from provider objects and statuses to domain transitions. A conservative example is:

```text
pending -> authorized -> paid -> partially_refunded -> refunded
    |          |          |
    +-------> failed      +-> disputed
    +-------> cancelled
```

Adapt it to the business. Require evidence for each transition and reject regressions caused by older events. Some transitions, such as dispute resolution or asynchronous payment-method completion, need provider-specific branches.

## Reconciliation

Run scheduled reconciliation over recent and unresolved attempts:

- Compare local provider IDs, amount, currency, capture/refund totals, and status with provider records.
- Compare provider balance or settlement exports with accounting records when settlement matters.
- Classify mismatches instead of silently overwriting state.
- Repair automatically only when the transition is unambiguous and audited.
- Create an operator-visible case for unresolved, duplicated, or financially inconsistent records.

## Test Matrix

At minimum, cover:

- Success, decline, cancellation, asynchronous completion, and abandoned checkout.
- Duplicate create/capture/refund requests with the same idempotency identity.
- Timeout before response, webhook before response, and response before webhook.
- Duplicate, delayed, malformed, invalidly signed, and out-of-order webhooks.
- Worker crash before and after state commit, retry exhaustion, and replay.
- Full and partial refunds, refund failure, disputes, and chargebacks where supported.
- Currency precision and zero-decimal currency behavior.
- Sandbox/live credential mix-ups and least-privilege failure.
- Reconciliation detecting and safely handling a mismatch.

Use provider test clocks, test cards/accounts, simulators, and webhook tools when available. Never run destructive scenarios against live customer transactions as routine tests.

## Operational Controls

- Store secrets in a managed secret store and rotate them with a documented procedure.
- Redact tokens, signatures, payment details, and sensitive personal data from logs.
- Include order ID, attempt ID, provider object ID, event ID, and trace ID as structured correlation fields.
- Alert on verification failures, inbox backlog, dead letters, unknown outcomes, reconciliation mismatches, and elevated payment failures.
- Restrict manual repair tools by role and record actor, reason, evidence, and before/after state.
- Pin or deliberately manage provider API versions and test upgrades before rollout.
