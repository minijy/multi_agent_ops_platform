# Stripe Integration

Apply the shared payment core first, then use these Stripe-specific decisions. Verify current behavior against Stripe's official documentation before implementation because API versions and account capabilities can change.

## Select the Stripe Flow

| Need | Preferred starting point | Use a lower-level flow when |
|---|---|---|
| Most one-time or subscription checkouts | Checkout Sessions | The product requires a fully custom payment lifecycle or UI behavior Checkout cannot support |
| Custom payment UI and lifecycle | Payment Intents with Stripe Elements or supported mobile SDKs | Never collect raw card data in the application server |
| Save a payment method without an immediate payment | Setup Intents | Do not simulate setup with an uncaptured charge |
| Recurring billing | Checkout Sessions or Stripe Billing APIs | Model subscription and invoice lifecycles explicitly |
| Customer self-service | Billing Portal | Build custom changes only when product requirements demand it |
| Marketplace or platform payments | Connect | Account ownership, charge type, fees, transfers, refunds, and liability are explicitly designed |

Stripe recommends Checkout Sessions for most integrations. Choose Payment Intents when the application genuinely needs lower-level control, and document why.

## Server-Side Checkout

1. Receive an internal order or cart identifier, not a trusted total.
2. Load authoritative products, prices, currency, discounts, tax behavior, and merchant account on the server.
3. Persist the order and payment attempt before creating the Stripe object.
4. Create the Checkout Session or Payment Intent with a persisted idempotency key.
5. Put non-sensitive internal correlation IDs in metadata; do not place secrets or sensitive personal data there.
6. Return only Stripe values intended for the client.
7. Treat the success URL as user experience only. Confirm fulfillment from verified webhook/provider evidence.

For Payment Intents, use automatic payment methods unless requirements call for explicit control. Let the Stripe SDK drive required next actions rather than inventing a client-side status protocol.

## Stripe Webhooks

- Preserve the raw request body before JSON parsing.
- Use an official Stripe SDK to construct and verify the event from the payload, `Stripe-Signature` header, and endpoint secret.
- Configure a reasonable timestamp tolerance consistent with Stripe's guidance.
- Deduplicate using the Stripe event ID, but also make downstream operations idempotent because distinct events can refer to the same object.
- Return success after durable inbox insertion; do expensive work asynchronously.
- Retrieve the current Stripe object when an event is thin, stale, or insufficient to justify the domain transition.
- Handle event schema/API versions deliberately.

Do not implement Stripe verification as a hand-written generic HMAC check unless maintaining a constrained legacy system with a separately reviewed requirement.

## Subscriptions

Model customer, subscription, invoice, and Payment Intent relationships separately. Access should normally follow paid/collectible invoice evidence and the product's grace-period policy, not merely `customer.subscription.updated`.

Handle at least:

- Trial start and end.
- Initial payment requiring customer action.
- Renewal success and failure.
- Plan, quantity, and proration changes.
- Cancellation now versus at period end.
- Pauses, delinquency, and grace periods if supported by the product.
- Credit notes and refunds without corrupting invoice history.

Use Stripe test clocks when time-dependent Billing behavior is important.

## Refunds and Disputes

Create refunds on the server against a known charge or Payment Intent and store the refund as its own lifecycle record. Use a stable idempotency key for a retry of the same refund request. Do not mark the order refunded until provider evidence supports the amount and state.

Disputes are not ordinary refunds. Store dispute IDs, deadlines, evidence status, disputed amount, outcome, and resulting balance impact. Restrict evidence submission and other consequential live operations to explicitly authorized users.

## Connect Boundary

Before using Connect, decide and document:

- Connected account type and onboarding responsibility.
- Direct, destination, or separate charges and transfers.
- Platform fee behavior and merchant of record assumptions.
- Refund and dispute liability.
- Negative-balance and failed-transfer handling.
- Which account owns each Stripe object and webhook endpoint.

Always make the target Stripe account explicit on server calls. Include account identity in idempotency and reconciliation design.

## Stripe Test Checklist

- Successful and declined payments across selected payment methods.
- 3DS or other customer action, including abandoned action.
- Asynchronous method completion after the client has left.
- Duplicate requests, network timeouts, and retries with the same idempotency key.
- Invalid, duplicate, delayed, and out-of-order webhook events.
- Full and partial refunds, refund failure, and disputes.
- Subscription renewal, dunning, proration, cancellation, and test-clock advancement.
- Connect account isolation and liability paths when applicable.

## Official References

- [Stripe payment integration options](https://docs.stripe.com/payments/payment-element/migration-ewcs)
- [Checkout quickstart](https://docs.stripe.com/checkout/quickstart)
- [Payment Intents](https://docs.stripe.com/payments/payment-intents)
- [Setup Intents](https://docs.stripe.com/payments/setup-intents)
- [Webhooks](https://docs.stripe.com/webhooks)
- [Idempotent requests](https://docs.stripe.com/api/idempotent_requests)
- [Billing subscriptions](https://docs.stripe.com/billing/subscriptions/overview)
- [Connect](https://docs.stripe.com/connect)
- [Refunds](https://docs.stripe.com/refunds)
- [Disputes](https://docs.stripe.com/disputes)
