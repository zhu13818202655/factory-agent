"""Ownership resolution, interaction loading, and session history rebuild."""

from collections.abc import AsyncIterator

from factory_agent.application.authorization import ResolvedAuthorization
from factory_agent.application.capability_map import CHITCHAT_CAPABILITY_ID, fr_id_for
from factory_agent.application.context import ConversationTurn
from factory_agent.application.session.base import SessionCore
from factory_agent.application.session.definitions import (
    TERMINAL_STATUSES,
    InteractionNotFoundError,
)
from factory_agent.domain import InteractionId, InteractionRecord, SessionEvent, SessionId
from factory_agent.ports import InteractionOwner, TrustedCredential


class SessionHistoryMixin(SessionCore):
    """Owner-scoped record access and bounded multi-turn context rebuild."""

    async def _resolve_owner(
        self, credential: TrustedCredential
    ) -> tuple[InteractionOwner, ResolvedAuthorization]:
        authorization = await self._authorization.authorize(credential, self._clock.now())
        context = authorization.tenant_context
        return (
            InteractionOwner(tenant_id=context.tenant_id, user_id=context.user_id),
            authorization,
        )

    async def _load(
        self, owner: InteractionOwner, interaction_id: InteractionId
    ) -> InteractionRecord:
        record = await self._store.get_interaction(owner, interaction_id)
        if record is None:
            raise InteractionNotFoundError("interaction does not exist")
        return record

    async def _replay_tail(
        self, owner: InteractionOwner, interaction_id: InteractionId, after_sequence: int
    ) -> AsyncIterator[SessionEvent]:
        for event in await self._store.list_events(owner, interaction_id, after_sequence):
            yield event

    async def _session_history(
        self,
        owner: InteractionOwner,
        session_id: SessionId,
        *,
        exclude_interaction_id: InteractionId,
        page_size: int = 50,
    ) -> tuple[ConversationTurn, ...]:
        """Rebuild bounded multi-turn context from this session's own turns.

        Reads are ownership-scoped (same tenant and user as the interaction),
        the current interaction is excluded, and only terminal turns qualify.
        Each prior turn is reduced to the caller's own question plus a
        non-sensitive capability summary — never detail rows or scope IDs.
        """
        turns: list[ConversationTurn] = []
        cursor: str | None = None
        while True:
            page = await self._store.list_interactions(owner, session_id, page_size, cursor)
            turns.extend(
                self._history_turn(record)
                for record in page.items
                if record.interaction_id != exclude_interaction_id
                and record.status in TERMINAL_STATUSES
            )
            cursor = page.next_cursor
            if cursor is None:
                break
        return tuple(turns)

    @staticmethod
    def _history_turn(record: InteractionRecord) -> ConversationTurn:
        """One compact, non-sensitive turn from a stored interaction row."""
        capability = record.capability_id
        if capability is not None and str(capability) == CHITCHAT_CAPABILITY_ID:
            assistant_text = "上一轮是闲聊回复。"
        elif capability is not None:
            assistant_text = f"已返回 {fr_id_for(str(capability))} 查询结果。"
        else:
            assistant_text = "上一轮需要补充信息或未能完成。"
        return ConversationTurn(
            user_text=record.input_text.strip(),
            assistant_text=assistant_text,
            status=record.status,
            capability_id=capability,
            result_row_count=None,
        )
