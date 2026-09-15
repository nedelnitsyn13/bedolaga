"""Телеграм-редактор: LIMITED squad тарифа (новая архитектура лимитного трафика).

Параллельно tariff_server_limits.py (индивидуальные лимиты по серверам) и
компаньон-версии (LIMITED_COMPANION_SQUAD_UUID) — здесь настраивается общий
лимит трафика на отдельный пул серверов на ОСНОВНОМ Remnawave user, без
второго панельного аккаунта. См. app/services/limited_squad_service.py.
"""

import html

from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.server_squad import get_all_server_squads
from app.database.crud.tariff import get_tariff_by_id, update_tariff
from app.database.models import Tariff, User
from app.localization.texts import get_texts
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler


def format_limited_squad_summary(tariff: Tariff) -> str:
    """Строка для карточки тарифа."""
    if not getattr(tariff, 'limited_traffic_enabled', False):
        return 'выключен'
    squads = getattr(tariff, 'limited_squad_uuids', None) or []
    base_gb = getattr(tariff, 'limited_base_traffic_gb', 0) or 0
    return f'{len(squads)} серв., база {base_gb} ГБ' if squads else f'нет серверов, база {base_gb} ГБ'


def render_limited_squad_screen(tariff: Tariff) -> str:
    enabled = getattr(tariff, 'limited_traffic_enabled', False)
    squads = getattr(tariff, 'limited_squad_uuids', None) or []
    base_gb = getattr(tariff, 'limited_base_traffic_gb', 0) or 0
    status = '✅ включён' if enabled else '❌ выключен'
    return (
        f'🎯 <b>LIMITED squad</b>\n\n'
        f'Тариф: <b>{html.escape(tariff.name)}</b>\n'
        f'Статус: {status}\n'
        f'Базовый лимит пула: <b>{base_gb} ГБ</b>\n'
        f'Серверов в пуле: <b>{len(squads)}</b>\n\n'
        'Общий лимит трафика делится на все выбранные ниже сервера вместе '
        '(не по отдельности на каждый). MAIN-сервера тарифа (вкладка «Серверы») '
        'сюда не входят и остаются безлимитными.'
    )


def get_limited_squad_keyboard(tariff: Tariff, squads: list, language: str) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    enabled = getattr(tariff, 'limited_traffic_enabled', False)
    selected = set(getattr(tariff, 'limited_squad_uuids', None) or [])

    buttons = [
        [
            InlineKeyboardButton(
                text='❌ Выключить' if enabled else '✅ Включить',
                callback_data=f'admin_tariff_ls_toggle:{tariff.id}',
            )
        ],
        [
            InlineKeyboardButton(
                text=f'📊 Базовый лимит: {getattr(tariff, "limited_base_traffic_gb", 0) or 0} ГБ',
                callback_data=f'admin_tariff_ls_base:{tariff.id}',
            )
        ],
    ]
    for squad in squads:
        mark = '✅' if squad.squad_uuid in selected else '▫️'
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f'{mark} {squad.display_name}',
                    callback_data=f'admin_tariff_ls_squad:{tariff.id}:{squad.squad_uuid}',
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data=f'admin_tariff_view:{tariff.id}')])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _render_screen(
    target: types.Message, tariff: Tariff, db: AsyncSession, language: str, *, edit: bool, prefix: str = ''
) -> None:
    squads, _ = await get_all_server_squads(db, limit=10000)
    text = f'{prefix}{render_limited_squad_screen(tariff)}'
    keyboard = get_limited_squad_keyboard(tariff, squads, language)
    if edit:
        await target.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
    else:
        await target.answer(text, reply_markup=keyboard, parse_mode='HTML')


@admin_required
@error_handler
async def show_limited_squad(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.clear()
    tariff = await get_tariff_by_id(db, int(callback.data.split(':')[1]))
    if not tariff:
        await callback.answer('Тариф не найден', show_alert=True)
        return
    await _render_screen(callback.message, tariff, db, db_user.language, edit=True)
    await callback.answer()


@admin_required
@error_handler
async def toggle_limited_squad_enabled(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    tariff = await get_tariff_by_id(db, int(callback.data.split(':')[1]))
    if not tariff:
        await callback.answer('Тариф не найден', show_alert=True)
        return
    new_value = not getattr(tariff, 'limited_traffic_enabled', False)
    tariff = await update_tariff(db, tariff, limited_traffic_enabled=new_value)
    confirmation = '✅ LIMITED squad включён' if new_value else '✅ LIMITED squad выключен'
    await _render_screen(callback.message, tariff, db, db_user.language, edit=True, prefix=f'{confirmation}\n\n')
    await callback.answer()


@admin_required
@error_handler
async def toggle_limited_squad_member(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    _, tariff_id, squad_uuid = callback.data.split(':', 2)
    tariff = await get_tariff_by_id(db, int(tariff_id))
    if not tariff:
        await callback.answer('Тариф не найден', show_alert=True)
        return

    # Новый список, а не правка хранимого: JSON-колонка иначе не увидит изменения.
    current = list(getattr(tariff, 'limited_squad_uuids', None) or [])
    if squad_uuid in current:
        current = [uuid for uuid in current if uuid != squad_uuid]
    else:
        current.append(squad_uuid)
    tariff = await update_tariff(db, tariff, limited_squad_uuids=current)
    await _render_screen(callback.message, tariff, db, db_user.language, edit=True)
    await callback.answer()


@admin_required
@error_handler
async def start_edit_limited_base_gb(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    tariff = await get_tariff_by_id(db, int(callback.data.split(':')[1]))
    if not tariff:
        await callback.answer('Тариф не найден', show_alert=True)
        return
    await state.set_state(AdminStates.editing_tariff_limited_base_gb)
    await state.update_data(tariff_id=tariff.id, language=db_user.language)
    texts = get_texts(db_user.language)
    current = getattr(tariff, 'limited_base_traffic_gb', 0) or 0
    await callback.message.edit_text(
        f'📊 <b>Базовый лимит LIMITED-пула</b>\n\nТариф: <b>{html.escape(tariff.name)}</b>\n'
        f'Текущее значение: <b>{current} ГБ</b>\n\n'
        'Введите базовый лимит в ГБ целым числом. <code>0</code> — безлимит.',
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=texts.CANCEL, callback_data=f'admin_tariff_edit_limited_squad:{tariff.id}')]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_limited_base_gb_input(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    data = await state.get_data()
    tariff = await get_tariff_by_id(db, data.get('tariff_id')) if data.get('tariff_id') is not None else None
    if tariff is None:
        await message.answer('Тариф не найден')
        await state.clear()
        return

    try:
        base_gb = int((message.text or '').strip())
        if base_gb < 0:
            raise ValueError
    except ValueError:
        await message.answer(
            '❌ Введите целое число гигабайт, не меньше нуля. <code>0</code> — безлимит.', parse_mode='HTML'
        )
        return

    tariff = await update_tariff(db, tariff, limited_base_traffic_gb=base_gb)
    await state.clear()
    confirmation = f'✅ Базовый лимит установлен: {base_gb} ГБ' if base_gb else '✅ Базовый лимит снят — безлимит'
    await _render_screen(message, tariff, db, db_user.language, edit=False, prefix=f'{confirmation}\n\n')


def register_limited_squad_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(show_limited_squad, F.data.startswith('admin_tariff_edit_limited_squad:'))
    dp.callback_query.register(toggle_limited_squad_enabled, F.data.startswith('admin_tariff_ls_toggle:'))
    dp.callback_query.register(toggle_limited_squad_member, F.data.startswith('admin_tariff_ls_squad:'))
    dp.callback_query.register(start_edit_limited_base_gb, F.data.startswith('admin_tariff_ls_base:'))
    dp.message.register(process_limited_base_gb_input, AdminStates.editing_tariff_limited_base_gb)
