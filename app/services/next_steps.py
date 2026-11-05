"""The short list of things worth doing next, shown on the dashboard."""

from __future__ import annotations

from dataclasses import dataclass

from flask import url_for
from sqlalchemy import func, select

from app.extensions import db
from app.models import Product, User
from app.services import auth, split
from app.services.split_engine import Issue, SplitResult

MOST_SHOWN = 4


@dataclass(frozen=True, slots=True)
class NextStep:
    text: str
    url: str
    action: str


def next_steps(user: User, result: SplitResult) -> list[NextStep]:
    steps: list[NextStep] = []
    user_id = user.id

    if user.onboarded_at is None:
        steps.append(
            NextStep(
                "You haven't finished setting up.", url_for("onboarding.start"), "Continue setup"
            )
        )

    product_count, unpriced = db.session.execute(
        select(
            func.count(),
            func.count().filter(Product.sell_price.is_(None)),
        ).where(Product.user_id == user_id, Product.archived_at.is_(None))
    ).one()
    if not product_count:
        steps.append(
            NextStep(
                "Add a product so you can record sales.",
                url_for("products.list_products"),
                "Add a product",
            )
        )

    if Issue.SHARES_NOT_100 in result.issues:
        steps.append(
            NextStep(
                f"Shares total {result.shares_total}%. "
                "They need to total 100% before you can settle up.",
                url_for("split.people"),
                "Fix shares",
            )
        )
    if Issue.UNASSIGNED_SALES in result.issues:
        steps.append(
            NextStep(
                "Some sale money isn't with anyone yet. Choose who usually receives it.",
                url_for("split.people"),
                "Choose someone",
            )
        )

    codes = auth.remaining_recovery_codes(user_id)
    if codes <= auth.RECOVERY_CODES_LOW:
        steps.append(
            NextStep(
                f"You have {codes} recovery code{'' if codes == 1 else 's'} left, "
                "your only way back in if you forget your password.",
                url_for("settings.security"),
                "Create new codes",
            )
        )

    if unpriced:
        steps.append(
            NextStep(
                f"{unpriced} product{' has' if unpriced == 1 else 's have'} no selling price, "
                "so the price can't fill in by itself.",
                url_for("products.list_products"),
                "See products",
            )
        )
    if not split.list_people(user_id, include_archived=True):
        steps.append(
            NextStep(
                "Sharing the profit with someone? Add them to the split.",
                url_for("split.people"),
                "Add people",
            )
        )
    return steps[:MOST_SHOWN]
