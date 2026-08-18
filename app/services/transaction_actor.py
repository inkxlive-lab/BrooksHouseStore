"""Request-local inventory transaction actor information."""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True)
class TransactionActor:
    user_id: int | None = None
    display_name: str | None = None
    role: str | None = None


_actor: ContextVar[TransactionActor | None] = ContextVar(
    "brookshouse_transaction_actor",
    default=None,
)


def set_transaction_actor(user) -> Token:
    actor = None
    if user is not None:
        actor = TransactionActor(
            user_id=int(getattr(user, "user_id", 0) or 0) or None,
            display_name=(
                str(getattr(user, "display_name", "") or "").strip()
                or str(getattr(user, "username", "") or "").strip()
                or None
            ),
            role=str(getattr(user, "role", "") or "").strip() or None,
        )
    return _actor.set(actor)


def reset_transaction_actor(token: Token) -> None:
    _actor.reset(token)


def transaction_user_id() -> int | None:
    actor = _actor.get()
    return actor.user_id if actor else None


def transaction_user_name() -> str:
    actor = _actor.get()
    return actor.display_name if actor and actor.display_name else "System"


def transaction_user_role() -> str | None:
    actor = _actor.get()
    return actor.role if actor else None
