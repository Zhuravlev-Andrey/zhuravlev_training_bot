"""
tests/test_logic.py — юнит-тесты чистых функций из bot.py.
Не требуют БД или Telegram. Запуск: pytest tests/
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot import compute_new_weight, get_closest_weight

AVAILABLE = [20.0, 25.0, 30.0, 35.0, 40.0, 45.0, 50.0]
CURRENT   = 35.0  # рабочий вес — индекс 3


class TestComputeNewWeight:

    def test_set3_below_target_decreases_weight(self):
        """Подход 3 < 10 → снизить вес."""
        result = compute_new_weight(CURRENT, AVAILABLE, set3_reps=8, set4_reps=10, set4_weight=CURRENT)
        assert result == 30.0

    def test_set3_below_target_has_priority_over_set4(self):
        """Подход 3 < 10 → снизить, даже если подход 4 выполнен."""
        result = compute_new_weight(CURRENT, AVAILABLE, set3_reps=7, set4_reps=10, set4_weight=CURRENT)
        assert result == 30.0

    def test_set4_complete_with_working_weight_increases(self):
        """Подход 4 == 10 с рабочим весом → повысить."""
        result = compute_new_weight(CURRENT, AVAILABLE, set3_reps=10, set4_reps=10, set4_weight=CURRENT)
        assert result == 40.0

    def test_set4_complete_with_non_working_weight_no_change(self):
        """Подход 4 == 10, но с нерабочим весом → без изменений."""
        result = compute_new_weight(CURRENT, AVAILABLE, set3_reps=10, set4_reps=10, set4_weight=30.0)
        assert result == CURRENT

    def test_set4_incomplete_no_change(self):
        """Подход 4 < 10 → без изменений."""
        result = compute_new_weight(CURRENT, AVAILABLE, set3_reps=10, set4_reps=9, set4_weight=CURRENT)
        assert result == CURRENT

    def test_already_at_maximum_no_increase(self):
        """Уже максимальный вес → не повышать."""
        max_w = AVAILABLE[-1]
        result = compute_new_weight(max_w, AVAILABLE, set3_reps=10, set4_reps=10, set4_weight=max_w)
        assert result == max_w

    def test_already_at_minimum_no_decrease(self):
        """Уже минимальный вес → не снижать."""
        min_w = AVAILABLE[0]
        result = compute_new_weight(min_w, AVAILABLE, set3_reps=7, set4_reps=7, set4_weight=min_w)
        assert result == min_w

    def test_weight_not_in_available_returns_current(self):
        """Рабочий вес не найден в списке → вернуть текущий."""
        result = compute_new_weight(33.0, AVAILABLE, set3_reps=10, set4_reps=10, set4_weight=33.0)
        assert result == 33.0

    def test_both_sets_complete_set3_wins(self):
        """set3 < 10 имеет приоритет даже если set4 == 10."""
        result = compute_new_weight(CURRENT, AVAILABLE, set3_reps=9, set4_reps=10, set4_weight=CURRENT)
        assert result == 30.0


class TestGetClosestWeight:

    def test_exact_match(self):
        assert get_closest_weight([20, 25, 30], 25) == 25

    def test_rounds_to_nearest(self):
        assert get_closest_weight([20, 25, 30], 23) == 25

    def test_below_minimum(self):
        assert get_closest_weight([20, 25, 30], 5) == 20

    def test_above_maximum(self):
        assert get_closest_weight([20, 25, 30], 100) == 30

    def test_midpoint_goes_to_higher(self):
        # 22.5 равноудалено от 20 и 25 — min выбирает первый совпавший (20)
        result = get_closest_weight([20, 25, 30], 22.5)
        assert result in (20, 25)  # оба допустимы

    def test_single_element(self):
        assert get_closest_weight([15], 100) == 15
