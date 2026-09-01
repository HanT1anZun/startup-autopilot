# Build, launch, and operate

## Offer

Define one buyer, one painful job, one bounded deliverable, one price, one delivery standard, one payment path, and one honest risk reversal. Avoid unsupported urgency, scarcity, guarantees, bonuses, or earnings claims.

## Build

Build only what the validated offer requires. Reuse existing tools before creating infrastructure. Verify the customer-visible outcome, direct cost, privacy boundary, failure behavior, and fulfillment steps. Use `checkpoint` with `artifacts_ready: true` before entering launch.

## Launch

Choose one channel the buyer already uses. Prepare exact content and target, call `plan_action`, honor host confirmations, execute once, verify the result, and call `record_action`. Do not silently switch accounts, channels, or tools. Enter operate only after one verified launch action.

An idempotent replay returns the already planned or recorded action; it is not a new instruction to execute. Never automatically retry `message`, `publish`, `deploy`, payment, or another non-idempotent action when the external result is unknown.

## Operate

Each cycle:

1. Sense new replies, usage, costs, failures, and delivery obligations.
2. Prioritize the single action with the highest expected evidence or customer value.
3. Validate it against the authorization and remaining budgets.
4. Act through the best available permitted tool.
5. Verify the external state; uncertainty is not success.
6. Record the outcome and cost before scheduling another cycle.

Keep a weekly checkpoint containing qualified leads, real conversations, conversions, collected cash, direct fulfillment cost, failures, and the next decision. Avoid vanity metrics when a buyer or payment signal is available.

After launch, customer support and fulfillment outrank new acquisition. Stop acquiring when the offer cannot be delivered to the promised standard.
