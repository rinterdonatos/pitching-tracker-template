#!/usr/bin/env python3
"""One-time setup script: creates the Stripe products/prices Mound HQ's
billing needs (Starter/Growth/Pro, monthly + annual, plus a per-player
overage price for Starter and Growth), then prints the env vars to paste
into your deployment (Render -> your service -> Environment).

Run this ONCE per Stripe account/mode. Stripe test mode and live mode are
completely separate - run it again with a live secret key when you're
ready to go live, and use the new price IDs it prints for your live env
vars.

Usage:
    pip install stripe
    STRIPE_SECRET_KEY=sk_test_xxx python3 stripe_setup.py

This only ever creates objects - it's safe to re-run, though re-running
will create a second copy of everything under a new product. If you need
to change a price later, don't edit it in Stripe (prices are immutable) -
create a new one and update the env var instead.
"""
import os
import sys

try:
    import stripe
except ImportError:
    sys.exit("Run 'pip install stripe' first, then re-run this script.")

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
if not STRIPE_SECRET_KEY:
    sys.exit("Set STRIPE_SECRET_KEY first, e.g.:\n  STRIPE_SECRET_KEY=sk_test_xxx python3 stripe_setup.py")

stripe.api_key = STRIPE_SECRET_KEY

# (key, label, player cap, monthly $, annual $, overage $/player or None)
TIERS = [
    ("starter", "Mound HQ - Starter", 20, 80, 799, 6),
    ("growth", "Mound HQ - Growth", 40, 150, 1499, 5),
    ("pro", "Mound HQ - Pro", None, 295, 2999, None),
]


def main():
    print(f"Using Stripe account in {'LIVE' if STRIPE_SECRET_KEY.startswith('sk_live') else 'TEST'} mode.\n")
    env_lines = []

    for key, label, cap, monthly, annual, overage in TIERS:
        product = stripe.Product.create(name=label)
        print(f"Created product: {label} ({product.id})")

        monthly_price = stripe.Price.create(
            product=product.id,
            unit_amount=monthly * 100,
            currency="usd",
            recurring={"interval": "month"},
            nickname=f"{label} monthly",
        )
        annual_price = stripe.Price.create(
            product=product.id,
            unit_amount=annual * 100,
            currency="usd",
            recurring={"interval": "year"},
            nickname=f"{label} annual",
        )
        print(f"  monthly ${monthly}  -> {monthly_price.id}")
        print(f"  annual  ${annual}  -> {annual_price.id}")

        env_lines.append(f"STRIPE_PRICE_{key.upper()}_MONTHLY={monthly_price.id}")
        env_lines.append(f"STRIPE_PRICE_{key.upper()}_ANNUAL={annual_price.id}")

        if overage:
            # Metered/licensed per-unit price for players over the plan's
            # cap. "licensed" usage type means the app tells Stripe the
            # current overage quantity directly (see sync_overage_quantity
            # in app.py) rather than Stripe metering usage records itself -
            # simpler, and it's just a headcount, not a usage event stream.
            overage_price = stripe.Price.create(
                product=product.id,
                unit_amount=overage * 100,
                currency="usd",
                recurring={"interval": "month", "usage_type": "licensed"},
                nickname=f"{label} overage (per player over {cap})",
            )
            print(f"  overage ${overage}/player over {cap} -> {overage_price.id}")
            env_lines.append(f"STRIPE_PRICE_{key.upper()}_OVERAGE={overage_price.id}")

        print()

    print("=" * 70)
    print("Paste these into your environment variables (Render: Environment tab):")
    print("=" * 70)
    for line in env_lines:
        print(line)
    print()
    print("Also make sure these are set (see the comment block above")
    print("STRIPE_SECRET_KEY in app.py for details):")
    print("  STRIPE_SECRET_KEY=" + STRIPE_SECRET_KEY)
    print("  STRIPE_PUBLISHABLE_KEY=<from the Stripe dashboard, API keys page>")
    print("  STRIPE_WEBHOOK_SECRET=<from the webhook you create in the Stripe dashboard>")
    print()
    print("Next: in the Stripe dashboard, add a webhook endpoint pointed at")
    print("  https://<your-domain>/webhooks/stripe")
    print("listening for: checkout.session.completed, customer.subscription.created,")
    print("customer.subscription.updated, customer.subscription.deleted")


if __name__ == "__main__":
    main()
