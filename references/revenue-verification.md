# Revenue verification

The first-revenue milestone is deliberately strict.

## Acceptable proof

Use either:

- a live payment-provider event inspected through a read-only trusted connector; or
- a redacted payment record whose amount, currency, settled status, settlement timestamp, payer distinction, and offer relationship can be inspected.

The proof type is `provider_event` or `redacted_payment_record`. `user_assertion` alone is not sufficient.

## Required facts

- Amount meets the contract's `revenue_goal_amount`, default 1.00 in its selected currency.
- Mode is `live` and status is `settled`.
- `settled_at` is a timezone-aware timestamp within the current campaign timeline.
- Payer relation is `external_customer`.
- Payment names the current campaign and selected offer.
- The payment is not refunded, disputed, a self-purchase, circular, a founder transfer, a coupon-only event, or a test fixture; each exclusion must be explicitly false.
- A real fulfillment obligation exists and is recorded.

Pass these facts to `verify_revenue`. If any field is unknown, record evidence and remain in `operate` or pause for verification. Never infer a payment from a checkout visit, intent, invoice, screenshot without inspectable fields, or an analytics conversion.

## Handoff

After verification, report the exact amount and evidence type, what the customer bought, fulfillment due, active automations, remaining budget, and any authorization that should be paused or revoked. One payment proves one payment, not repeatable demand or future earnings.
