"""Client visit chain (ТЗ 2.4): how many prior contacts this client had.

Links a client's calls/meetings into a chronological chain (by ``client_key``,
falling back to ``phone_number``) so the scorer knows whether a contact is the
1st / 2nd / 3rd and can apply repeat-visit leniency from CRM metadata rather than
trusting the LLM's guess alone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

from AtamuraOKK.db.models.call import Call

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def visit_index(session: AsyncSession, call: Call) -> int:
    """1-based position of ``call`` in its client's chronological contact chain.

    Counts the client's earlier contacts (by ``client_key``, else
    ``phone_number``) and adds one. Returns 1 when the client cannot be
    identified or the contact has no timestamp.
    """
    if call.started_at is None:
        return 1
    if call.client_key:
        client_filter = Call.client_key == call.client_key
    elif call.phone_number:
        client_filter = Call.phone_number == call.phone_number
    else:
        return 1

    prior = await session.scalar(
        select(func.count())
        .select_from(Call)
        .where(
            client_filter,
            Call.started_at < call.started_at,
            Call.id != call.id,
        ),
    )
    return int(prior or 0) + 1
