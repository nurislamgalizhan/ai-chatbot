"""A manager must see the chat when the customer asks for a person or
wants to book — recognised from words, since the model has no tools."""

from __future__ import annotations

import pytest

from app.services.escalation import BOOKING, HUMAN_REQUESTED, escalation_reason


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Позовите администратора, пожалуйста", HUMAN_REQUESTED),
        ("нужен менеджер", HUMAN_REQUESTED),
        ("Соедините с оператором", HUMAN_REQUESTED),
        ("Хочу поговорить с живым человеком", HUMAN_REQUESTED),
        ("Менеджер керек", HUMAN_REQUESTED),
        ("Әкімшіні шақырыңыз", HUMAN_REQUESTED),
        ("I want to talk to a human", HUMAN_REQUESTED),
        ("Хочу забронировать игровой зал на субботу", BOOKING),
        ("Можно бронь на 18:00?", BOOKING),
        ("Запишите меня на бокс", BOOKING),
        ("Оставлю заявку на аренду поля", BOOKING),
        ("Брондау жасағым келеді", BOOKING),
        ("I want to book the football field", BOOKING),
        ("Please sign me up for volleyball", BOOKING),
    ],
)
def test_requests_for_a_person_or_a_booking_are_recognised(text: str, reason: str) -> None:
    assert escalation_reason(text) == reason


@pytest.mark.parametrize(
    "text",
    [
        "Сколько стоит абонемент в зал?",
        "Зал на 20 человек подойдёт для тренировки?",
        "Подготовка сотрудников охраны — есть ли курс?",
        "Есть ли у вас парковка?",
        "I lost my notebook at the gym",
        "",
        "👍",
        # Whole words, not prefixes: everyday words of a guard-training
        # centre with a sports hall must not summon a manager.
        "Есть ли у вас бронежилеты для тренировок?",
        "Где административное здание?",
        "Взял бронзовую медаль на соревнованиях",
        "Резервуар для воды в зале есть?",
        "Можно записывать лекции на телефон?",
        "Оператор связи не ловит в зале",
    ],
)
def test_ordinary_questions_are_not_escalated(text: str) -> None:
    assert escalation_reason(text) is None
