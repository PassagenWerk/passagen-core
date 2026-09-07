"""Domain results returned by the conversation service."""

from __future__ import annotations

from dataclasses import dataclass

from passagen.assistant.schemas import Conversation, Message, QaRecord


@dataclass(frozen=True, slots=True)
class ConversationDetail:
    conversation: Conversation
    messages: tuple[Message, ...]


@dataclass(frozen=True, slots=True)
class AssistantTurn:
    conversation_id: str
    run_id: str
    question_message: Message
    answer_message: Message
    qa_record: QaRecord
