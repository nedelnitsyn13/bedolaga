"""
Тесты для утилит ценообразования и форматирования цен.

Этот модуль тестирует функции из app/utils/pricing_utils.py и app/localization/texts.py,
особенно функции отображения цен со скидками на кнопках подписки.
"""

from unittest.mock import MagicMock, patch

from app.localization.texts import _build_dynamic_values
from app.utils.pricing_utils import (
    calculate_price_per_month,
    tariff_period_extrapolated_price_kopeks,
    tariff_period_intrinsic_discount_percent,
)


# DEPRECATED: format_period_option_label tests removed - function replaced with unified price_display system


class TestCalculatePricePerMonth:
    """Тесты для calculate_price_per_month из pricing_utils.py."""

    def test_whole_months_divide_evenly(self) -> None:
        """Периоды, кратные 30 дням, делятся на целые месяцы."""
        assert calculate_price_per_month(16030, 30) == 16030
        assert calculate_price_per_month(30000, 90) == 10000
        assert calculate_price_per_month(60000, 180) == 10000

    def test_short_period_is_extrapolated(self) -> None:
        """Для периода короче месяца ставка экстраполируется, а не равна цене периода."""
        assert calculate_price_per_month(4830, 7) == 20700
        assert calculate_price_per_month(9900, 14) == 21214

    def test_period_not_multiple_of_month_is_prorated(self) -> None:
        """Некратные 30 дням периоды считаются пропорцией, а не делением на округлённые месяцы."""
        assert calculate_price_per_month(15000, 45) == 10000
        assert calculate_price_per_month(100000, 365) == 8219

    def test_non_positive_input_falls_back_to_price(self) -> None:
        """Нулевые и отрицательные значения не роняют расчёт."""
        assert calculate_price_per_month(0, 90) == 0
        assert calculate_price_per_month(10000, 0) == 10000
        assert calculate_price_per_month(-100, 30) == 0


class TestBuildDynamicValues:
    """
    Тесты для функции _build_dynamic_values из texts.py.

    NOTE: PERIOD_*_DAYS константы были удалены из _build_dynamic_values,
    так как теперь кнопки периодов генерируются динамически в get_subscription_period_keyboard()
    с учетом персональных скидок пользователя.
    """

    @patch('app.localization.texts.settings')
    def test_returns_empty_dict_for_unknown_language(self, mock_settings: MagicMock) -> None:
        """Неизвестный язык должен возвращать пустой словарь."""
        result = _build_dynamic_values('fr-FR')  # Французский не поддерживается
        assert result == {}

    @patch('app.localization.texts.settings')
    def test_traffic_keys_also_generated(self, mock_settings: MagicMock) -> None:
        """Должны генерироваться ключи трафика и другие динамические значения."""
        # Настройка моков для traffic цен
        mock_settings.format_price = lambda x: f'{x // 100} ₽'
        mock_settings.PRICE_TRAFFIC_5GB = 10000
        mock_settings.PRICE_TRAFFIC_10GB = 20000
        mock_settings.PRICE_TRAFFIC_25GB = 30000
        mock_settings.PRICE_TRAFFIC_50GB = 40000
        mock_settings.PRICE_TRAFFIC_100GB = 50000
        mock_settings.PRICE_TRAFFIC_250GB = 60000
        mock_settings.PRICE_TRAFFIC_UNLIMITED = 70000

        result = _build_dynamic_values('ru-RU')

        # Проверяем наличие ключей трафика
        assert 'TRAFFIC_5GB' in result
        assert 'TRAFFIC_10GB' in result
        assert 'TRAFFIC_UNLIMITED' in result
        assert 'SUPPORT_INFO' in result


class TestTariffPeriodIntrinsicDiscount:
    """Собственная скидка тарифа за длинный период — из period_prices, без промогруппы."""

    STANDARD_PRICES = {'30': 29000, '90': 78000, '180': 148000, '360': 278000}

    def test_shortest_period_has_no_discount(self) -> None:
        assert tariff_period_intrinsic_discount_percent(self.STANDARD_PRICES, 30) == 0

    def test_longer_period_computed_relative_to_shortest(self) -> None:
        # 90 дней по цене 30-дневного периода: 29000 * 3 = 87000, факт 78000 → ~10%
        assert tariff_period_intrinsic_discount_percent(self.STANDARD_PRICES, 90) == 10
        # 360 дней: 29000 * 12 = 348000, факт 278000 → ~20%
        assert tariff_period_intrinsic_discount_percent(self.STANDARD_PRICES, 360) == 20

    def test_period_not_in_prices_returns_zero(self) -> None:
        assert tariff_period_intrinsic_discount_percent(self.STANDARD_PRICES, 60) == 0

    def test_empty_or_missing_prices_return_zero(self) -> None:
        assert tariff_period_intrinsic_discount_percent({}, 90) == 0
        assert tariff_period_intrinsic_discount_percent(None, 90) == 0

    def test_negative_price_period_is_treated_as_disabled(self) -> None:
        """Отрицательная цена — отключённый период (как в get_price_for_period), не участвует в базе."""
        prices = {'30': -1, '90': 78000, '360': 278000}
        # 30 дней отключён -> базой становится 90 дней, для него самого скидки нет
        assert tariff_period_intrinsic_discount_percent(prices, 90) == 0


class TestTariffPeriodExtrapolatedPrice:
    STANDARD_PRICES = {'30': 29000, '90': 78000, '360': 278000}

    def test_extrapolates_from_shortest_period(self) -> None:
        assert tariff_period_extrapolated_price_kopeks(self.STANDARD_PRICES, 90) == 87000
        assert tariff_period_extrapolated_price_kopeks(self.STANDARD_PRICES, 360) == 348000

    def test_shortest_period_itself_returns_zero(self) -> None:
        assert tariff_period_extrapolated_price_kopeks(self.STANDARD_PRICES, 30) == 0
