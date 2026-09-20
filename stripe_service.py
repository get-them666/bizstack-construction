import os

import stripe

from datetime import datetime


DEFAULT_PRICES = {
    "Turnover Cleaning": 16000,
    "Deep Cleaning": 27500,
    "Linen Restock": 7000,
    "Inspection": 9500,
}

ENV_PRICE_MAP = {
    "Turnover Cleaning": "STRIPE_PRICE_TURNOVER",
    "Deep Cleaning": "STRIPE_PRICE_DEEP",
    "Linen Restock": "STRIPE_PRICE_LINEN",
    "Inspection": "STRIPE_PRICE_INSPECTION",
}


def default_deposit_cents() -> int:
    """Deposit to reserve a project / book a site survey, in cents."""
    raw = os.getenv("STRIPE_DEPOSIT_PRICE", "500.00")
    try:
        return int(float(raw) * 100)
    except (TypeError, ValueError):
        return 50000


class StripeService:
    def __init__(self):
        self.api_key = os.getenv("STRIPE_SECRET_KEY", "")
        if self.api_key:
            stripe.api_key = self.api_key
        self.webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def get_price(self, service_type: str) -> int:
        """Return the price in cents for a service type, or 0 if unknown."""
        env_var = ENV_PRICE_MAP.get(service_type)
        if env_var:
            env_price = os.getenv(env_var)
            if env_price:
                try:
                    return int(float(env_price) * 100)
                except ValueError:
                    pass
        return DEFAULT_PRICES.get(service_type, 0)

    def create_checkout_session(
        self,
        event_id: int,
        customer_name: str,
        customer_email: str,
        service_type: str,
        start_time: datetime,
        discount_coupon: str = None,
    ) -> str:
        """Create a Stripe Checkout Session for a single booking. Returns the checkout URL."""
        if not self.is_configured():
            raise RuntimeError("STRIPE_SECRET_KEY is not configured.")

        amount_cents = self.get_price(service_type)
        if amount_cents <= 0:
            raise ValueError(f"No price configured for service type: {service_type}")

        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": amount_cents,
                        "product_data": {
                            "name": f"Broom Service – {service_type}",
                            "description": (
                                f"Booking #{event_id} · {customer_name} · "
                                f"{start_time.strftime('%B %d, %Y at %I:%M %p')}"
                            ),
                        },
                    },
                    "quantity": 1,
                }
            ],
            metadata={
                "event_id": str(event_id),
                "service_type": service_type,
                "customer_name": customer_name,
                "scheduled_start": start_time.isoformat(),
            },
            discounts=[{"coupon": discount_coupon}] if discount_coupon else None,
            customer_email=customer_email or None,
            success_url=self._base_url() + "/payments/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=self._base_url() + "/payments/cancel",
        )
        return session.url

    def construct_webhook_event(self, payload: bytes, signature: str):
        """Validate an incoming webhook and return the Stripe event object."""
        if not self.webhook_secret:
            raise RuntimeError("STRIPE_WEBHOOK_SECRET is not configured.")
        return stripe.Webhook.construct_event(payload, signature, self.webhook_secret)

    def create_deposit_session(
        self,
        lead_id: int,
        customer_name: str,
        customer_email: str,
        project_type: str,
        amount_cents: int = 0,
        base_url: str = "",
    ) -> str:
        """Create a Stripe Checkout Session for a project deposit. Returns the URL."""
        if not self.is_configured():
            raise RuntimeError("STRIPE_SECRET_KEY is not configured.")

        amount_cents = int(amount_cents or default_deposit_cents())
        if amount_cents <= 0:
            raise ValueError("No deposit amount configured.")

        base = (base_url or self._base_url()).rstrip("/")

        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": amount_cents,
                        "product_data": {
                            "name": "Buildstack Construction — Project Deposit",
                            "description": (
                                f"Lead #{lead_id if lead_id else '—'} · {customer_name} · "
                                f"{project_type or 'Renovation project'}"
                            ),
                        },
                    },
                    "quantity": 1,
                }
            ],
            metadata={
                "lead_id": str(lead_id) if lead_id else "",
                "project_type": project_type or "",
                "customer_name": customer_name,
                "kind": "deposit",
            },
            customer_email=customer_email or None,
            success_url=base + "/payments/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=base + "/payments/cancel",
        )
        return session.url

    def create_worker_account(self, name: str, email: str = "", phone: str = "") -> str:
        """Create a Stripe Connect Express account for a crew member. Returns the account id."""
        if not self.is_configured():
            raise RuntimeError("STRIPE_SECRET_KEY is not configured.")
        parts = (name or "").strip().split()
        first = parts[0] if parts else None
        last = " ".join(parts[1:]) if len(parts) > 1 else None
        account = stripe.Account.create(
            type="express",
            country="US",
            email=email or None,
            capabilities={"transfers": {"requested": True}},
            business_type="individual",
            business_profile={
                "product_description": "Independent contractor with Buildstack Construction",
                "mcc": "1521",
            },
            individual={"first_name": first, "last_name": last or name or "Crew Member"},
            metadata={"platform": "buildstack", "crew_name": name or ""},
        )
        return account.id

    def account_link(self, account_id: str, redirect_base: str = "") -> str:
        """Create a Stripe hosted onboarding link for a Connect account. Returns the URL."""
        if not self.is_configured():
            return ""
        base = redirect_base or self._base_url()
        return_url = base.rstrip("/") + "/crew/pay"
        try:
            link = stripe.AccountLink.create(
                account=account_id,
                refresh_url=return_url,
                return_url=return_url,
                type="account_onboarding",
            )
            return link.url
        except Exception:
            return ""

    def transfer_to_worker(self, stripe_account_id: str, amount_cents: int, memo: str = "") -> bool:
        """Transfer money to a crew member's connected account. Returns True on success."""
        if not self.is_configured() or not stripe_account_id:
            return False
        try:
            stripe.Transfer.create(
                amount=int(amount_cents),
                currency="usd",
                destination=stripe_account_id,
                transfer_group=memo or "",
            )
            return True
        except Exception:
            return False

    def construct_connect_event(self, payload: bytes, signature: str):
        """Validate a Connect webhook. In dev (no secret configured) returns a harmless no-op."""
        wh = os.getenv("STRIPE_CONNECT_WEBHOOK_SECRET", "")
        if not wh:
            return {"type": "account.updated", "data": {"object": {}}}
        return stripe.Webhook.construct_event(payload, signature, wh)

    def _base_url(self) -> str:
        return (os.getenv("APP_BASE_URL", "https://bizstackperks.com") or "").rstrip("/")