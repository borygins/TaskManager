from __future__ import annotations


CLIENT_STATUSES = (
    "Новый",
    "Поиск контакта",
    "Переговоры",
    "Подписан",
    "Отказ",
    "Нет контактов",
)
CLIENT_STATUS_SET = frozenset(CLIENT_STATUSES)
FINAL_CLIENT_STATUSES = frozenset({"Подписан", "Отказ", "Нет контактов"})
POTENTIAL_TURNOVER_LABEL = "Потенциал оборота в месяц"

# Stable codes are persisted in the database; labels may be changed without a
# data migration. Marks remain a controlled list rather than arbitrary text.
ACTIVITY_MARKS = {
    "missed_call": "#недозвон",
    "call_back": "#перезвонить",
    "meeting_scheduled": "#встреча-назначена",
    "thinking": "#думает",
    "approval": "#согласование",
    "postponed": "#отложено",
    "proposal_sent": "#кп-отправлено",
    "waiting_documents": "#ждём-документы",
    "documents_received": "#документы-получены",
}
ACTIVITY_MARK_CODES = frozenset(ACTIVITY_MARKS)
MISSED_CALL_MARK = "missed_call"


def mark_labels(codes: list[str] | tuple[str, ...]) -> list[str]:
    """Return display labels for validated persisted mark codes."""
    return [ACTIVITY_MARKS[code] for code in codes if code in ACTIVITY_MARKS]
