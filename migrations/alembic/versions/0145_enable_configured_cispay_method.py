"""Enable the CisPay payment-method row when CisPay is configured in .env.

До 5.1.0 бот проверял CisPay только по .env, а запись в payment_method_configs
влияла лишь на кабинет. Запись создавалась с is_enabled = «настроен ли провайдер
в момент создания», поэтому у тех, кто включил CisPay в .env позже, она осталась
выключенной. С 5.1.0 оплату проверяет и эта запись: кнопки в боте видны, а оплата
отвечает «Способ оплаты недоступен». Один раз приводим запись к .env; дальше
выключатель в «Способах оплаты» снова решает сам.

Revision ID: 0145
Revises: 0144
"""

import sqlalchemy as sa
from alembic import op


revision = '0145'
down_revision = '0144'
branch_labels = None
depends_on = None


def upgrade() -> None:
    from app.config import settings

    if not settings.is_cispay_enabled():
        return
    if 'payment_method_configs' not in sa.inspect(op.get_bind()).get_table_names():
        return
    op.execute(
        sa.text("UPDATE payment_method_configs SET is_enabled = true WHERE method_id = 'cispay' AND NOT is_enabled")
    )


def downgrade() -> None:
    # Прежнее «выключено» было случайным следствием порядка настройки, а не
    # решением админа — восстанавливать его нечего.
    pass
