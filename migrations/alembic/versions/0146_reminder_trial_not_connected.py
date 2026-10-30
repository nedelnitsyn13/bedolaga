"""встроенное напоминание «триал без подключения»

Revision ID: 0146
Revises: 0145
Create Date: 2026-10-30

Взял триал, а к VPN так и не подключился — самый частый способ потерять клиента
ещё до оплаты. Условие «ни разу не подключался» проверяется по панели, поэтому
появилось только вместе с ним. Поставляется ВЫКЛЮЧЕННЫМ, как и прочие встроенные:
текст, частоту и кнопку админ правит в разделе «Напоминания» и включает сам.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0146'
down_revision: Union[str, None] = '0145'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


BUILTIN_TRIAL_NOT_CONNECTED = {
    'name': 'Триал без подключения',
    'is_active': False,
    'builtin_key': 'trial_not_connected',
    'channels': 'both',
    'category': 'service',
    # Сутки на то, чтобы человек разобрался сам: напоминание сразу после выдачи
    # триала раздражает больше, чем помогает.
    'conditions': {'subscription': {'segment': 'trial'}, 'never_connected': True, 'registered_days_min': 1},
    'repeat_every_days': 1,
    'max_sends': 2,
    'button_kind': 'cabinet',
    'button_target': '/subscription',
    'texts': {
        'ru': {
            'title': 'Вы ещё не подключились',
            'body': (
                'Пробный период уже идёт, а VPN так и не включён. Нажмите кнопку ниже — '
                'подключение займёт пару минут. Если что-то не получается, напишите в поддержку.'
            ),
            'button': 'Подключиться',
        },
        'en': {
            'title': "You haven't connected yet",
            'body': (
                "Your trial is already running, but the VPN hasn't been turned on. Tap the button "
                "below — it takes a couple of minutes. If something doesn't work, contact support."
            ),
            'button': 'Connect',
        },
        'ua': {
            'title': 'Ви ще не підключилися',
            'body': (
                'Пробний період уже йде, а VPN так і не ввімкнено. Натисніть кнопку нижче — '
                'підключення займе кілька хвилин. Якщо щось не виходить, напишіть у підтримку.'
            ),
            'button': 'Підключитися',
        },
        'zh': {
            'title': '您还没有连接',
            'body': '试用期已经开始，但 VPN 还没有开启。点击下方按钮，只需几分钟即可连接。如果遇到问题，请联系客服。',
            'button': '连接',
        },
        'fa': {
            'title': 'هنوز متصل نشده‌اید',
            'body': (
                'دوره آزمایشی شما شروع شده، اما VPN هنوز روشن نشده است. روی دکمه زیر بزنید — '
                'اتصال چند دقیقه طول می‌کشد. اگر مشکلی بود، به پشتیبانی پیام دهید.'
            ),
            'button': 'اتصال',
        },
    },
}


def upgrade() -> None:
    bind = op.get_bind()
    if 'user_reminders' not in sa.inspect(bind).get_table_names():
        return
    exists = bind.execute(
        sa.text('SELECT 1 FROM user_reminders WHERE builtin_key = :key'),
        {'key': BUILTIN_TRIAL_NOT_CONNECTED['builtin_key']},
    ).first()
    if exists is not None:
        return
    # JSON-колонки объявлены с типом: иначе bulk_insert не сериализует dict.
    table = sa.table(
        'user_reminders',
        sa.column('name', sa.String()),
        sa.column('is_active', sa.Boolean()),
        sa.column('builtin_key', sa.String()),
        sa.column('channels', sa.String()),
        sa.column('category', sa.String()),
        sa.column('conditions', sa.JSON()),
        sa.column('repeat_every_days', sa.Integer()),
        sa.column('max_sends', sa.Integer()),
        sa.column('button_kind', sa.String()),
        sa.column('button_target', sa.String()),
        sa.column('texts', sa.JSON()),
    )
    op.bulk_insert(table, [BUILTIN_TRIAL_NOT_CONNECTED])


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM user_reminders WHERE builtin_key = 'trial_not_connected'"))
