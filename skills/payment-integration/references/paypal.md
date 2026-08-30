# PayPal Integration

Apply the shared payment core first, then use these PayPal-specific decisions. Verify current behavior against PayPal's official documentation because product availability, regional support, and API behavior can change.

## Modern Baseline

- Use PayPal REST APIs for new integrations.
- Use Orders v2 for standard checkout and server-side create/capture or authorize/capture flows.
- Use the Subscriptions API for recurring plans and subscriptions.
- Use REST webhooks for event delivery.
- Treat IPN, NVP/SOAP APIs, and old SDKs such as `paypalrestsdk` as legacy migration surfaces, not defaults for new code.
- Use an official or currently supported PayPal SDK where it covers the required API; otherwise use a small, well-tested REST client against official endpoints.

## Select the PayPal Flow

| Need | Preferred starting point | Key boundary |
|---|---|---|
| Immediate checkout | Orders v2 with `CAPTURE` intent | Server creates and captures the order |
| Separate authorization and capture | Orders v2 with `AUTHORIZE` intent | Capture timing, expiry, and partial capture are explicit business workflows |
| Recurring billing | Products, Plans, and Subscriptions APIs | Subscription state is distinct from individual payment/sale evidence |
| Refund | Refund the relevant captured payment | Track full/partial refund lifecycle independently |
| Marketplace payout | Payouts or the applicable PayPal platform product | Recipient validation, fees, reversals, and live authorization are explicit |

## OAuth and Environment

Obtain OAuth 2.0 access tokens server-side using the client ID and secret. Cache tokens within their validity window and refresh safely; never expose the client secret or access token to the client.

Keep sandbox and live base URLs, credentials, webhook IDs, merchant accounts, and data strictly separated. Validate that the intended merchant account owns or may access the referenced PayPal object.

## Orders v2 Checkout

1. Receive an internal order/cart identifier and load authoritative purchase data on the server.
2. Persist the internal order and payment attempt.
3. Create the PayPal order with the correct intent, currency, amount breakdown, payee, and internal correlation data.
4. Send a stable `PayPal-Request-Id` for retried supported POST operations and persist it with the attempt.
5. Return the approval link or client-safe order ID.
6. After buyer approval, capture or authorize from the server using a stable retry identity.
7. Confirm fulfillment from capture/provider evidence and verified webhooks, not from the browser redirect alone.

Validate that the approved/captured order belongs to the expected merchant and internal order and that currency and amount match trusted server data.

## REST Webhooks

PayPal REST webhook verification is provider-specific. Do not describe it as a generic shared-secret HMAC operation.

- Preserve the request body and required PayPal transmission headers.
- Verify authenticity using PayPal's supported verification method and the configured webhook ID. Depending on the integration, use the official verification API or a correctly implemented certificate/signature flow per current documentation.
- Do not trust certificate URLs, algorithms, or header values without validation according to PayPal's documentation.
- Persist the verified event under a unique PayPal event ID, acknowledge promptly, and process asynchronously.
- Expect duplicates and non-sequential delivery. Query the current resource when event evidence is insufficient or stale.
- Map capture, authorization, refund, dispute, and subscription events to explicit domain transitions.

The webhook ID is environment- and endpoint-specific. Do not mix sandbox and live webhook configuration.

## Subscriptions

Model PayPal product, plan, subscription, and individual payment events separately. Cover:

- Plan/version changes and which subscribers they affect.
- Approval and activation.
- Trial and billing-cycle transitions.
- Payment completed, failed, or reversed.
- Suspension, reactivation, cancellation, and expiration.
- Product access during grace or delinquency periods.
- Refunds and disputes on individual subscription payments.

Do not grant indefinite access from subscription approval alone. Apply the product's entitlement policy to verified subscription and payment evidence.

## Refunds and Disputes

Refund the exact capture intended by the business workflow. Persist PayPal refund ID, requested and completed amounts, currency, reason, request ID, state, and evidence. Reconcile partial and multiple refunds against the captured amount.

Track disputes separately from refunds. Preserve case IDs, deadlines, messages/evidence, disputed amount, outcome, and balance consequences. Treat live case actions as consequential operations requiring explicit authorization.

## Payouts

Payouts move funds and require a stronger authorization boundary. Validate recipient identity, currency, amount, duplicate prevention, compliance status, and business approval before creating a batch. Persist sender batch IDs and item IDs, then reconcile asynchronous item results. Never use a payout as a substitute for modeling marketplace charge ownership or refund liability.

## Legacy Migration

When existing code uses IPN, NVP/SOAP, or an obsolete SDK:

1. Inventory the messages, operations, and business transitions actually used.
2. Add tests that capture current observable behavior.
3. Map each legacy operation to a supported REST API and each IPN message to relevant REST webhook/resource evidence.
4. Run old and new observation paths in parallel where feasible, without duplicating financial mutations.
5. Compare events and reconciled outcomes before cutover.
6. Remove legacy credentials and endpoints only after rollback and accounting requirements are satisfied.

## PayPal Test Checklist

- Buyer approval, cancellation, capture success, and capture failure.
- Authorization, delayed capture, expiry, and partial capture if used.
- Duplicate POST requests and timeout retry with the same `PayPal-Request-Id`.
- Invalid, duplicate, delayed, and out-of-order REST webhooks.
- Full and partial refunds, reversals, and disputes.
- Subscription approval, renewal, payment failure, suspension, and cancellation.
- Sandbox/live isolation and merchant-account ownership checks.
- Payout duplicate prevention and asynchronous item failures when applicable.

## Official References

- [PayPal Orders v2 API](https://developer.paypal.com/docs/api/orders/v2/)
- [PayPal checkout standard integration](https://developer.paypal.com/docs/checkout/standard/integrate/)
- [PayPal REST authentication](https://developer.paypal.com/api/rest/authentication/)
- [PayPal REST idempotency](https://developer.paypal.com/api/rest/reference/idempotency/)
- [PayPal webhooks](https://developer.paypal.com/api/rest/webhooks/)
- [Verify webhook signature](https://developer.paypal.com/docs/api/webhooks/v1/#verify-webhook-signature_post)
- [PayPal Subscriptions](https://developer.paypal.com/docs/subscriptions/)
- [Refund captured payments](https://developer.paypal.com/docs/api/payments/v2/#captures_refund)
- [PayPal Payouts](https://developer.paypal.com/docs/payouts/)
- [Legacy APIs overview](https://developer.paypal.com/api/nvp-soap/)
