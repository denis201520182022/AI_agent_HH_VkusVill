import logging
import io
import pandas as pd
from datetime import date, datetime, timedelta
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import func, cast, Date
from sqlalchemy.orm import Session
from aiogram.utils.formatting import Text, Bold, Italic
from aiogram.utils.keyboard import InlineKeyboardBuilder # <-- Добавить
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton # <-- Добавить
from hr_bot.db.models import MailingTask # <--- Добавь в импорты сверху
from hr_bot.db.models import (
    TelegramUser, Statistic, Vacancy, TrackedRecruiter,
    InactiveNotificationQueue, RejectedNotificationQueue, NotificationQueue, Dialogue,
    AnalyticsEvent # <--- ОБЯЗАТЕЛЬНО ДОБАВИТЬ ЭТО
)

from hr_bot.tg_bot.keyboards import (
    user_keyboard, admin_keyboard, 
    stats_main_menu_keyboard, stats_regular_keyboard,      # Добавлено
    stats_followup_keyboard, back_to_stats_main_keyboard,  # Добавлено
    export_date_options_keyboard, 
    cancel_fsm_keyboard, create_stats_export_keyboard
)

from hr_bot.db.models import (
    TelegramUser, Statistic, Vacancy, TrackedRecruiter,
    InactiveNotificationQueue, RejectedNotificationQueue, NotificationQueue, Dialogue
)
from hr_bot.tg_bot.keyboards import (
    user_keyboard, admin_keyboard, 
    stats_main_menu_keyboard, export_date_options_keyboard, 
    cancel_fsm_keyboard, create_stats_export_keyboard
)

from sqlalchemy import cast, Date, func
from datetime import date, timedelta
# Предполагается, что Bold, Italic, Text импортированы из aiogram.utils.formatting или аналогичной библиотеки
from aiogram.utils.formatting import Bold, Italic, Text

import io
import pandas as pd
from datetime import date
from sqlalchemy import cast, Date
from sqlalchemy.orm import Session
from aiogram.types import Message, BufferedInputFile
from aiogram.fsm.context import FSMContext

from hr_bot.db.models import Dialogue, InactiveNotificationQueue

logger = logging.getLogger(__name__)
router = Router()

class ExportStates(StatesGroup):
    waiting_for_range = State()

class FollowupExportStates(StatesGroup):
    waiting_for_range = State()


# --- FSM для экспорта молчунов ---
class ReactivationExportStates(StatesGroup):
    waiting_for_range = State()

# 1. Определяем состояния FSM
class MailingTaskStates(StatesGroup):
    waiting_for_vacancy = State()
    waiting_for_city = State()
    waiting_for_days = State()
    waiting_for_message = State()
    waiting_for_confirmation = State()

# 2. Вход в режим создания рассылки
@router.message(F.text == "🚀 Реактивация")
async def start_mailing_task_creation(message: Message, state: FSMContext):
    await state.set_state(MailingTaskStates.waiting_for_vacancy)
    await message.answer(
        "🚀 <b>Создание задачи на реактивацию</b>\n\n"
        "Шаг 1/4: Введите название вакансии (или часть названия).\n"
        "<i>Например: Сборщик</i>\n"
        "Если хотите запустить по ВСЕМ вакансиям, введите: <code>%</code>",
        reply_markup=cancel_fsm_keyboard,
        parse_mode="HTML"
    )

# 3. Собираем Вакансию
@router.message(MailingTaskStates.waiting_for_vacancy)
async def process_mailing_vacancy(message: Message, state: FSMContext):
    await state.update_data(vacancy_pattern=message.text.strip())
    await state.set_state(MailingTaskStates.waiting_for_city)
    await message.answer(
        "Шаг 2/4: Введите город.\n"
        "<i>Например: Санкт-Петербург</i>\n"
        "По всем городам — введите: <code>%</code>",
        reply_markup=cancel_fsm_keyboard,
        parse_mode="HTML"
    )

# 4. Собираем Город
@router.message(MailingTaskStates.waiting_for_city)
async def process_mailing_city(message: Message, state: FSMContext):
    await state.update_data(city_pattern=message.text.strip())
    await state.set_state(MailingTaskStates.waiting_for_days)
    await message.answer(
        "Шаг 3/4: Введите диапазон молчания в днях (от и до).\n"
        "<i>Например: 10-20</i>\n\n"
        "Бот найдет тех, кто не отвечал в этом промежутке.",
        reply_markup=cancel_fsm_keyboard,
        parse_mode="HTML"
    )

# 5. Собираем Дни (с валидацией)
@router.message(MailingTaskStates.waiting_for_days)
async def process_mailing_days(message: Message, state: FSMContext):
    text = message.text.replace(" ", "")
    try:
        if "-" not in text:
            raise ValueError
        parts = text.split("-")
        d_min = int(parts[0])
        d_max = int(parts[1])
        if d_min > d_max:
            d_min, d_max = d_max, d_min # На случай если перепутали местами
            
        await state.update_data(days_min=d_min, days_max=d_max)
        await state.set_state(MailingTaskStates.waiting_for_message)
        await message.answer(
            "Шаг 4/4: Введите ТЕКСТ сообщения, который получит кандидат.",
            reply_markup=cancel_fsm_keyboard
        )
    except:
        await message.answer("❌ Неверный формат. Введите два числа через дефис, например: <code>7-30</code>", parse_mode="HTML")

# 6. Собираем Текст и показываем Превью
@router.message(MailingTaskStates.waiting_for_message)
async def process_mailing_text(message: Message, state: FSMContext):
    await state.update_data(message_text=message.text)
    data = await state.get_data()
    
    preview = (
        f"📋 <b>Проверка параметров рассылки:</b>\n\n"
        f"🔹 Вакансия: <code>{data['vacancy_pattern']}</code>\n"
        f"🔹 Город: <code>{data['city_pattern']}</code>\n"
        f"🔹 Молчание: от <b>{data['days_min']}</b> до <b>{data['days_max']}</b> дней\n\n"
        f"📝 <b>Текст сообщения:</b>\n"
        f"<i>{data['message_text']}</i>\n\n"
        f"⚠️ После запуска бот найдет подходящих кандидатов и начнет отправку. "
        f"Списание средств произойдет по тарифу Реактивации за каждое успешное сообщение."
    )
    
    # Создаем кнопку подтверждения
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ ЗАПУСТИТЬ", callback_data="confirm_mailing_start")
    kb.button(text="❌ ОТМЕНА", callback_data="cancel_fsm")
    kb.adjust(1)
    
    await state.set_state(MailingTaskStates.waiting_for_confirmation)
    await message.answer(preview, reply_markup=kb.as_markup(), parse_mode="HTML")

# 7. Финал: Сохранение в БД
@router.callback_query(MailingTaskStates.waiting_for_confirmation, F.data == "confirm_mailing_start")
async def confirm_mailing_task(callback: CallbackQuery, state: FSMContext, db_session: Session):
    data = await state.get_data()
    
    # 1. Сначала найдем ID пользователя в нашей БД (не telegram_id, а внутренний id)
    user = db_session.query(TelegramUser).filter_by(telegram_id=str(callback.from_user.id)).first()
    user_db_id = user.id if user else None

    # 2. Создаем запись с привязкой к создателю
    new_task = MailingTask(
        vacancy_pattern=data['vacancy_pattern'],
        city_pattern=data['city_pattern'],
        days_silent_min=data['days_min'],
        days_silent_max=data['days_max'],
        message_text=data['message_text'],
        status='pending',
        creator_id=user_db_id  # <--- ТЕПЕРЬ СОХРАНЯЕМ КТО ЗАПУСТИЛ
    )
    
    db_session.add(new_task)
    db_session.commit()
    
    await state.clear()
    await callback.message.edit_text(
        "✅ <b>Задача успешно создана и добавлена в очередь!</b>\n\n"
        "Воркер приступит к выполнению в ближайшее время. "
        "Результаты можно будет увидеть в разделе статистики (Молчуны).",
        parse_mode="HTML"
    )
    await callback.answer()

# --- Локальная клавиатура (чтобы не менять keyboards.py) ---
reactivation_stats_menu = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="👁️ Посмотреть за 7 дней (текст)", callback_data="reactivation_7days_stats")],
        [InlineKeyboardButton(text="📥 Выгрузить в Excel", callback_data="reactivation_excel_start")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="stats_back_to_main")]
    ]
)

# 1. ВХОД В МЕНЮ "МОЛЧУНЫ"
@router.callback_query(F.data == "stats_silent")
async def stats_silent_handler(callback: CallbackQuery):
    await callback.message.edit_text(
        "🤐 <b>Статистика Реактивации (Молчуны)</b>\n"
        "Здесь отображаются результаты массовых рассылок и буста старых кандидатов.",
        reply_markup=reactivation_stats_menu,
        parse_mode="HTML"
    )
    await callback.answer()

# 2. ТЕКСТОВАЯ СВОДКА ЗА 7 ДНЕЙ
def _build_reactivation_7day_stats_content(db_session: Session) -> Text:
    content_parts = [Bold("🤐 Воронка Реактивации (7 дней):"), "\n\n"]
    days = [date.today() - timedelta(days=i) for i in range(7)]
    has_data = False

    for day in days:
        # Запрашиваем события только режима 'reactivation' за конкретный день
        events = db_session.query(AnalyticsEvent.event_type, func.count(AnalyticsEvent.id)).filter(
            AnalyticsEvent.mode == 'reactivation',
            cast(AnalyticsEvent.created_at, Date) == day
        ).group_by(AnalyticsEvent.event_type).all()

        # Преобразуем список кортежей в словарь для удобства
        stats = {e[0]: e[1] for e in events}

        sent = stats.get('reactivation_sent', 0)
        
        # Если были отправки или какая-то активность
        if sent > 0 or stats:
            has_data = True
            
            awakened = stats.get('user_reply', 0)
            success = stats.get('interview_assigned', 0)
            rejected_bot = stats.get('rejected_by_bot', 0)
            refusal_user = stats.get('refusal_by_user', 0)
            timeout = stats.get('timed_out', 0)
            
            # Конверсия в ответ (если отправляли)
            conv_reply = round((awakened / sent * 100), 1) if sent > 0 else 0

            day_str = day.strftime('%d.%m (%a)')
            content_parts.extend([
                Bold(f"📅 {day_str}"), "\n",
                f"   📤 Разослано: {sent}\n",
                f"   💬 Проснулись: {awakened} ", Italic(f"({conv_reply}%)"), "\n",
                f"   ✅ Записаны: {success}\n",
                f"   🤖 Отказ (бот): {rejected_bot}\n",
                f"   🙅‍♂️ Отказ (сам): {refusal_user}\n",
                f"   💤 Снова уснули: {timeout}\n",
                "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
            ])

    if not has_data:
        return Text("📊 Данных по Реактивации за 7 дней не найдено.")
    return Text(*content_parts)

@router.callback_query(F.data == "reactivation_7days_stats")
async def view_reactivation_text_stats(callback: CallbackQuery, db_session: Session):
    content = _build_reactivation_7day_stats_content(db_session)
    await callback.message.edit_text(**content.as_kwargs(), reply_markup=back_to_stats_main_keyboard)
    await callback.answer()

# 3. ЭКСПОРТ В EXCEL
@router.callback_query(F.data == "reactivation_excel_start")
async def reactivation_export_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ReactivationExportStates.waiting_for_range)
    await callback.message.answer(
        "🤐 [РЕАКТИВАЦИЯ] За какой период выгрузить Excel?\n\n"
        "Выберите кнопку или пришлите диапазон:\n<code>01.12.2025 - 15.12.2025</code>",
        reply_markup=export_date_options_keyboard,
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(ReactivationExportStates.waiting_for_range, F.data.startswith("export_range_"))
async def reactivation_export_quick(callback: CallbackQuery, state: FSMContext, db_session: Session):
    days_count = int(callback.data.split("_")[-1])
    start_date = date.today() - timedelta(days=days_count-1)
    await generate_reactivation_excel(callback.message, start_date, date.today(), db_session, state)
    await callback.answer()

@router.message(ReactivationExportStates.waiting_for_range)
async def reactivation_export_manual(message: Message, state: FSMContext, db_session: Session):
    try:
        parts = message.text.split("-")
        start_date = datetime.strptime(parts[0].strip(), "%d.%m.%Y").date()
        end_date = datetime.strptime(parts[1].strip(), "%d.%m.%Y").date()
        await generate_reactivation_excel(message, start_date, end_date, db_session, state)
    except Exception:
        await message.answer("❌ Формат: 01.01.2024 - 10.01.2024")

async def generate_reactivation_excel(message: Message, start_date: date, end_date: date, db: Session, state: FSMContext):
    msg_wait = await message.answer("⏳ Собираю события Реактивации и формирую воронку...")

    # 1. ЗАПРОС К БД
    results = db.query(
        AnalyticsEvent.created_at,
        AnalyticsEvent.event_type,
        AnalyticsEvent.city,
        TrackedRecruiter.name,
        Vacancy.title
    ).outerjoin(
        TrackedRecruiter, AnalyticsEvent.recruiter_id == TrackedRecruiter.id
    ).outerjoin(
        Vacancy, AnalyticsEvent.vacancy_id == Vacancy.id
    ).filter(
        AnalyticsEvent.mode == 'reactivation',
        cast(AnalyticsEvent.created_at, Date) >= start_date,
        cast(AnalyticsEvent.created_at, Date) <= end_date
    ).all()

    if not results:
        await msg_wait.edit_text("🤷 За этот период событий по Реактивации не найдено.")
        await state.clear()
        return

    # 2. ПОДГОТОВКА СЫРЫХ ДАННЫХ
    rows = []
    for r in results:
        rows.append({
            "Дата": r[0].strftime("%d.%m.%Y"),
            "Тип события": r[1],
            "Город": r[2] if r[2] else "Не указан",
            "Рекрутер": r[3] if r[3] else "Не указан",
            "Вакансия": r[4] if r[4] else "Не указана"
        })
    
    df_raw = pd.DataFrame(rows)

    # 3. ФУНКЦИЯ ДЛЯ ГЕНЕРАЦИИ СВОДНЫХ ТАБЛИЦ
    def create_reactivation_summary(groupby_col):
        # Агрегируем типы событий в колонки
        pivot = pd.pivot_table(
            df_raw, 
            index=groupby_col, 
            columns='Тип события', 
            aggfunc='size', 
            fill_value=0
        ).reset_index()

        # Гарантируем наличие всех типов событий для расчетов
        for col in ['reactivation_sent', 'user_reply', 'interview_assigned', 'refusal_by_user', 'rejected_by_bot', 'timed_out']:
            if col not in pivot.columns:
                pivot[col] = 0

        # ВЫЧИСЛЕНИЯ ПО ТВОИМ ПРАВИЛАМ
        s = pivot
        s['Инициировано'] = s['reactivation_sent']
        s['Вступили'] = s['user_reply']
        s['Не вступили'] = s['Инициировано'] - s['Вступили']
        s['Собес'] = s['interview_assigned']
        s['Отказался КД'] = s['refusal_by_user']
        s['Отказали мы'] = s['rejected_by_bot']
        s['Молчуны'] = s['timed_out'] # Которые начали, но снова уснули
        s['Отказы всего'] = s['Отказался КД'] + s['Отказали мы']

        # КОНВЕРСИИ (от Инициировано)
        s['Собес/Инициировано %'] = (s['Собес'] / s['Инициировано']).fillna(0)
        s['Молчуны (молчуны + не вступили)/Инициировано %'] = ((s['Молчуны'] + s['Не вступили']) / s['Инициировано']).fillna(0)
        s['Отказы всего / инициировано %'] = (s['Отказы всего'] / s['Инициировано']).fillna(0)

        # Финальный список и порядок столбцов
        final_cols = [
            groupby_col, 'Инициировано', 'Вступили', 'Не вступили', 'Собес', 
            'Отказался КД', 'Отказали мы', 'Молчуны', 'Отказы всего',
            'Собес/Инициировано %', 'Молчуны (молчуны + не вступили)/Инициировано %', 'Отказы всего / инициировано %'
        ]
        
        res = s[final_cols]

        # ИТОГОВАЯ СТРОКА
        total = res.sum(numeric_only=True)
        total[groupby_col] = 'ИТОГО'
        
        # Пересчет % для ИТОГО (чтобы не суммировать проценты)
        t_init = total['Инициировано'] if total['Инициировано'] > 0 else 1
        total['Собес/Инициировано %'] = total['Собес'] / t_init
        total['Молчуны (молчуны + не вступили)/Инициировано %'] = (total['Молчуны'] + total['Не вступили']) / t_init
        total['Отказы всего / инициировано %'] = total['Отказы всего'] / t_init
        
        return pd.concat([res, pd.DataFrame([total])], ignore_index=True)

    # Генерируем листы
    df_date = create_reactivation_summary('Дата')
    df_rec = create_reactivation_summary('Рекрутер')
    df_city = create_reactivation_summary('Город')
    df_vac = create_reactivation_summary('Вакансия')

    # 4. СОХРАНЕНИЕ
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df_date.to_excel(writer, index=False, sheet_name='Свод по датам')
        df_rec.to_excel(writer, index=False, sheet_name='Свод по рекрутерам')
        df_city.to_excel(writer, index=False, sheet_name='Свод по городам')
        df_vac.to_excel(writer, index=False, sheet_name='Свод по вакансиям')
        
        # Лист с общим отчетом (сырые данные переименованные)
        df_raw.to_excel(writer, index=False, sheet_name='Общий отчет')

        workbook = writer.book
        perc_fmt = workbook.add_format({'num_format': '0%', 'border': 1, 'align': 'center'})
        num_fmt = workbook.add_format({'border': 1, 'align': 'center'})
        header_fmt = workbook.add_format({'bold': True, 'bg_color': '#F4CCCC', 'border': 1}) # Розоватый заголовок

        for sheet_name in writer.sheets:
            ws = writer.sheets[sheet_name]
            ws.freeze_panes(1, 0)
            
            # Определяем какой DF использовать для расчёта ширины
            if sheet_name == 'Свод по датам': curr_df = df_date
            elif sheet_name == 'Свод по рекрутерам': curr_df = df_rec
            elif sheet_name == 'Свод по городам': curr_df = df_city
            elif sheet_name == 'Свод по вакансиям': curr_df = df_vac
            else: curr_df = df_raw

            for i, col in enumerate(curr_df.columns):
                # Авто-ширина: по длине контента или заголовка
                column_len = max(curr_df[col].astype(str).str.len().max(), len(str(col))) + 4
                is_perc = '%' in str(col)
                ws.set_column(i, i, column_len, perc_fmt if is_perc else num_fmt)
            
            # Применяем формат шапки
            ws.set_row(0, None, header_fmt)

    output.seek(0)
    await message.answer_document(
        BufferedInputFile(output.read(), filename=f"Reactivation_Report_{start_date}_{end_date}.xlsx"),
        caption=f"🤐 Аналитика Реактивации ({start_date} - {end_date})"
    )
    await msg_wait.delete()
    await state.clear()
    



def _build_followup_7day_stats_content(db_session: Session) -> Text:
    content_parts = [Bold("🤖 Статистика Follow-up (7 дней):"), "\n\n"]
    days = [date.today() - timedelta(days=i) for i in range(7)]
    has_data = False

    for day in days:
        # Назначено (любые диалоги, где есть дата собеса на этот день создания)
        base_query = db_session.query(Dialogue).filter(cast(Dialogue.created_at, Date) == day, Dialogue.interview_datetime_utc.is_not(None))
        total_sched = base_query.count()

        if total_sched > 0:
            has_data = True
            # Вступили в дожим
            fu_entered = base_query.filter(Dialogue.was_in_followup == True).count()
            # Переназначено (был в FU и есть переносы)
            resched = base_query.filter(Dialogue.was_in_followup == True, Dialogue.reschedule_count > 0).count()
            # Пришли (успех или отказ)
            visited = base_query.filter(Dialogue.interview_result.in_(['success', 'refusal', 'self_refusal'])).count()
            # Не пришли (есть категория причины)
            no_shows = base_query.filter(Dialogue.no_show_reason.is_not(None)).count()

            day_str = day.strftime('%d.%m (%a)')
            content_parts.extend([
                Bold(f"📅 {day_str}"), "\n",
                f"   Назначено: {total_sched}\n",
                f"   - Вступили в дожим: {fu_entered}\n",
                f"   - Переназначено: {resched}\n",
                f"   - ✅ Пришли: {visited}\n",
                f"   - ❌ Не пришли: {no_shows}\n",
                "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
            ])

    if not has_data:
        return Text("📊 Данных по Follow-up за 7 дней не найдено.")
    return Text(*content_parts)


async def generate_followup_excel(message: Message, start_date: date, end_date: date, db: Session, state: FSMContext):
    msg_wait = await message.answer("⏳ Формирую детальную аналитику по Follow-up...")
    
    # 1. СБОР ДАННЫХ
    query = db.query(Dialogue).filter(
        cast(Dialogue.created_at, Date) >= start_date,
        cast(Dialogue.created_at, Date) <= end_date,
        Dialogue.interview_datetime_utc.is_not(None)
    )
    dialogues = query.all()

    if not dialogues:
        await msg_wait.edit_text("🤷 За этот период назначенных собеседований не найдено.")
        await state.clear()
        return

    rows = []
    for d in dialogues:
        # Логика определения "Вступил в диалог" (кандидат ответил на дожим)
        # Считаем ответившим, если был дожим И бот смог записать результат или причину неявки
        has_reply = 1 if (d.was_in_followup and (d.interview_result or d.no_show_reason)) else 0
        
        rows.append({
            "Дата": d.created_at.strftime("%d.%m.%Y"),
            "Рекрутер": d.recruiter.name if d.recruiter else "Не указан",
            "Город": d.vacancy.city if d.vacancy else "Не указан",
            "Вакансия": d.vacancy.title if d.vacancy else "Не указана",
            "Назначено": 1,
            "Инициировано follow up": 1 if d.was_in_followup else 0,
            "Прошли follow up": has_reply,
            "Пришло": 1 if d.interview_result in ['success', 'refusal', 'self_refusal'] else 0,
            "Взяли_число": 1 if d.interview_result == 'success' else 0,
            "Отказали_число": 1 if d.interview_result == 'refusal' else 0,
            "Сами отказались_число": 1 if d.interview_result == 'self_refusal' else 0,
            "Не пришло": 1 if (d.no_show_reason or d.no_show_reason) else 0,
            "Причина неявки": d.no_show_reason or "-"
        })

    df_base = pd.DataFrame(rows)

    # 2. ФУНКЦИЯ ДЛЯ СВОДНЫХ ТАБЛИЦ
    def create_fu_summary(groupby_col):
        # Группируем и суммируем все показатели
        summary = df_base.groupby(groupby_col).agg({
            'Назначено': 'sum',
            'Инициировано follow up': 'sum',
            'Прошли follow up': 'sum',
            'Пришло': 'sum',
            'Взяли_число': 'sum',
            'Отказали_число': 'sum',
            'Сами отказались_число': 'sum',
            'Не пришло': 'sum'
        }).reset_index()

        # Расчет процентов
        # Взяли/Отказали/Сами — это % от тех, кто ПРИШЕЛ
        # Используем .div с fillna, чтобы не было ошибок при делении на 0
        summary['Взяли'] = (summary['Взяли_count' if False else 'Взяли_число'] / summary['Пришло']).fillna(0)
        summary['Отказали'] = (summary['Отказали_число'] / summary['Пришло']).fillna(0)
        summary['Сами отказались'] = (summary['Сами отказались_число'] / summary['Пришло']).fillna(0)
        
        # Доходимость — это % Пришедших от Назначенных
        summary['Доходимость'] = (summary['Пришло'] / summary['Назначено']).fillna(0)

        # Переименовываем для красоты (согласно ТЗ)
        summary = summary.rename(columns={
            'Взяли': 'Взяли (% от пришло)',
            'Отказали': 'Отказали (% от пришло)',
            'Сами отказались': 'Сами отказались (% от пришло)',
            'Доходимость': 'Доходимость % (пришли от назначено)'
        })

        # Финальный порядок столбцов
        final_cols = [
            groupby_col, 'Назначено', 'Инициировано follow up', 'Прошли follow up', 
            'Пришло', 'Взяли (% от пришло)', 'Отказали (% от пришло)', 
            'Сами отказались (% от пришло)', 'Не пришло', 'Доходимость % (пришли от назначено)'
        ]
        
        res = summary[final_cols]

        # Итоговая строка
        total = res.sum(numeric_only=True)
        total[groupby_col] = 'ИТОГО'
        
        # Пересчет процентов для ИТОГО (чтобы не суммировались проценты)
        t_pri = total['Пришло'] if total['Пришло'] > 0 else 1
        t_naz = total['Назначено'] if total['Назначено'] > 0 else 1
        
        total['Взяли (% от пришло)'] = df_base['Взяли_число'].sum() / t_pri
        total['Отказали (% от пришло)'] = df_base['Отказали_число'].sum() / t_pri
        total['Сами отказались (% от пришло)'] = df_base['Сами отказались_число'].sum() / t_pri
        total['Доходимость % (пришли от назначено)'] = total['Пришло'] / t_naz

        return pd.concat([res, pd.DataFrame([total])], ignore_index=True)

    # Листы
    df_date = create_fu_summary('Дата')
    df_rec = create_fu_summary('Рекрутер')
    df_city = create_fu_summary('Город')
    df_vac = create_fu_summary('Вакансия')

    # 3. СОХРАНЕНИЕ
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df_date.to_excel(writer, index=False, sheet_name='Свод по датам')
        df_rec.to_excel(writer, index=False, sheet_name='Свод по рекрутерам')
        df_city.to_excel(writer, index=False, sheet_name='Свод по городам')
        df_vac.to_excel(writer, index=False, sheet_name='Свод по вакансиям')
        
        # Для общего списка просто выведем сырые данные (без расчетов процентов на каждой строке)
        df_base.to_excel(writer, index=False, sheet_name='Сырые данные')

        workbook = writer.book
        perc_fmt = workbook.add_format({'num_format': '0%', 'border': 1, 'align': 'center'})
        num_fmt = workbook.add_format({'border': 1, 'align': 'center'})
        header_fmt = workbook.add_format({'bold': True, 'bg_color': '#CFE2F3', 'border': 1})

        for sheet_name in writer.sheets:
            ws = writer.sheets[sheet_name]
            
            # А) ЗАКРЕПЛЯЕМ ШАПКУ (первая строка всегда видна при прокрутке)
            ws.freeze_panes(1, 0)
            
            # Б) АВТОМАТИЧЕСКАЯ ШИРИНА СТОЛБЦОВ
            # Берем данные из соответствующего DataFrame для этого листа
            if sheet_name == 'Свод по датам': current_df = df_date
            elif sheet_name == 'Свод по рекрутерам': current_df = df_rec
            elif sheet_name == 'Свод по городам': current_df = df_city
            elif sheet_name == 'Свод по вакансиям': current_df = df_vac
            else: current_df = df_base # 'Общий список'

            for i, col in enumerate(current_df.columns):
                # Вычисляем максимальную длину: либо название колонки, либо самое длинное значение в ней
                column_len = max(
                    current_df[col].astype(str).str.len().max(), # Самое длинное значение
                    len(str(col))                                # Длина заголовка
                ) + 3 # Добавляем небольшой запас
                
                # Устанавливаем ширину (если заголовок с процентами, делаем чуть шире)
                ws.set_column(i, i, column_len, num_fmt if '%' not in col else perc_fmt)

    output.seek(0)
    await message.answer_document(
        BufferedInputFile(output.read(), filename=f"FU_Report_{start_date}_{end_date}.xlsx"),
        caption=f"📈 Аналитика дожимов ({start_date} - {end_date})"
    )
    await msg_wait.delete()
    await state.clear()


def _build_7day_stats_content(db_session: Session) -> Text:
    content_parts = [Bold("📊 Статистика за последние 7 дней:"), "\n", Italic("(взаимоисключающие категории)"), "\n\n"]
    
    days = [date.today() - timedelta(days=i) for i in range(7)]
    has_any_data = False

    for day in days:
        # 1. ОТКЛИКИ (Всего)
        res = db_session.query(func.count(Dialogue.id)).filter(
            cast(Dialogue.created_at, Date) == day
        ).scalar() or 0

        # 2. ПОДОШЛО (Только qualified)
        qual = db_session.query(func.count(Dialogue.id)).filter(
            cast(Dialogue.created_at, Date) == day,
            Dialogue.status == 'qualified'
        ).scalar() or 0

        # 3. ОТКАЗОВ (Только rejected)
        rej = db_session.query(func.count(Dialogue.id)).filter(
            cast(Dialogue.created_at, Date) == day,
            Dialogue.status == 'rejected'
        ).scalar() or 0

        # 4. МОЛЧУНЫ (Есть в таблице молчунов И статус НЕ qualified И НЕ rejected)
        sil = db_session.query(func.count(Dialogue.id)).join(
            InactiveNotificationQueue, Dialogue.id == InactiveNotificationQueue.dialogue_id
        ).filter(
            cast(Dialogue.created_at, Date) == day,
            Dialogue.status.notin_(['qualified', 'rejected']) # <--- ИСКЛЮЧАЕМ ТЕХ, КТО ЗАВЕРШЕН
        ).scalar() or 0

        # 5. В ПРОЦЕССЕ (Остальные: новые или в работе, кто еще не молчит 2 часа)
        # Это поможет вам увидеть, сколько реально людей еще "живы" в боте
        in_progress = res - (qual + rej + sil)

        if res > 0:
            has_any_data = True
            day_str = day.strftime('%d.%m (%a)')
            content_parts.extend([
                Bold(f"📅 {day_str}"), "\n",
                "   Откликов: ", Bold(str(res)), "\n",
                "   - Подошло: ", Bold(str(qual)), "\n",
                "   - Отказов: ", Bold(str(rej)), "\n",
                "   - Молчунов: ", Bold(str(sil)), "\n",
                "   - В работе: ", Bold(str(in_progress)), "\n",
                "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
            ])

    if not has_any_data:
        return Text("📊 Данных за последние 7 дней не найдено.")







    return Text(*content_parts)
@router.message(CommandStart())
async def handle_start(message: Message, db_session: Session):
    user = db_session.query(TelegramUser).filter(TelegramUser.telegram_id == str(message.from_user.id)).first()
    if not user:
        await message.answer("❌ Нет доступа.")
        return
    kb = admin_keyboard if user.role == 'admin' else user_keyboard
    await message.answer(f"👋 Привет, {message.from_user.first_name or 'HR'}!", reply_markup=kb)

@router.message(F.text == "📊 Статистика")
async def stats_main_menu(message: Message):
    await message.answer("Выберите режим работы со статистикой:", reply_markup=stats_main_menu_keyboard)

@router.callback_query(F.data == "view_stats_7days")
async def view_text_stats(callback: CallbackQuery, db_session: Session):
    content = _build_7day_stats_content(db_session)
    await callback.message.edit_text(**content.as_kwargs())
    await callback.answer()

@router.callback_query(F.data == "export_excel_start")
async def export_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ExportStates.waiting_for_range)
    await callback.message.answer(
        "За какой период выгрузить данные?\n\n"
        "Выберите кнопку или пришлите диапазон:\n<code>01.12.2025 - 15.12.2025</code>",
        reply_markup=export_date_options_keyboard,
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(ExportStates.waiting_for_range, F.data.startswith("export_range_"))
async def export_range_quick(callback: CallbackQuery, state: FSMContext, db_session: Session):
    days_count = int(callback.data.split("_")[-1])
    end_date = date.today()
    start_date = end_date - timedelta(days=days_count-1)
    await generate_and_send_excel(callback.message, start_date, end_date, db_session, state)
    await callback.answer()

@router.message(ExportStates.waiting_for_range)
async def export_range_manual(message: Message, state: FSMContext, db_session: Session):
    try:
        parts = message.text.split("-")
        start_date = datetime.strptime(parts[0].strip(), "%d.%m.%Y").date()
        end_date = datetime.strptime(parts[1].strip(), "%d.%m.%Y").date()
        if (end_date - start_date).days > 30:
            await message.answer("❌ Ошибка: период не может превышать 30 дней.")
            return
        await generate_and_send_excel(message, start_date, end_date, db_session, state)
    except Exception:
        await message.answer("❌ Неверный формат. Пример: 01.12.2025 - 10.12.2025")

async def generate_and_send_excel(message: Message, start_date: date, end_date: date, db: Session, state: FSMContext):
    msg_wait = await message.answer("⏳ Собираю данные и формирую детальный отчет по новым правилам...")
    
    # 1. СБОР ДАННЫХ ИЗ БД
    # Используем created_at для фильтрации по дате создания диалога в системе
    query = db.query(Dialogue).filter(
        cast(Dialogue.created_at, Date) >= start_date,
        cast(Dialogue.created_at, Date) <= end_date
    )
    dialogues = query.all()

    if not dialogues:
        await msg_wait.edit_text("🤷 За этот период откликов не найдено.")
        await state.clear()
        return

    report_map = {}
    
    for d in dialogues:
        # Группировка: Дата | Рекрутер | Город | Вакансия
        dt = d.created_at.strftime("%d.%m.%Y")
        rec = d.recruiter.name if d.recruiter else "Не указан"
        cit = d.vacancy.city if d.vacancy else "Не указан"
        vac = d.vacancy.title if d.vacancy else "Не указана"
        key = (dt, rec, cit, vac)

        if key not in report_map:
            report_map[key] = {
                "отклики_всего": 0,
                "начали_диалог_счетчик": 0,
                "собес_счетчик": 0,
                "отказался_кд_счетчик": 0,
                "отказали_мы_счетчик": 0,
                "молчуны_в_очереди_счетчик": 0
            }

        m = report_map[key]
        
        # А) Отклики (считаем всё)
        m["отклики_всего"] += 1
        
        # Б) Проверка: Начал ли пользователь диалог?
        # Ищем хотя бы одно сообщение от 'user', которое не является системной командой
        history = d.history or []
        user_started = False
        for h in history:
            if isinstance(h, dict) and h.get('role') == 'user':
                content = h.get('content', '')
                if content and not content.startswith("[SYSTEM COMMAND]"):
                    user_started = True
                    break
        
        if user_started:
            m["начали_диалог_счетчик"] += 1

        # В) Собеседования (Status Qualified)
        if d.status == 'qualified':
            m["собес_счетчик"] += 1
            
        # Г) Отказался КД (Status Rejected + State Declined)
        if d.status == 'rejected' and d.dialogue_state == 'declined_vacancy':
            m["отказался_кд_счетчик"] += 1
            
        # Д) Отказали мы (State Qualification Failed)
        if d.dialogue_state == 'qualification_failed':
            m["отказали_мы_счетчик"] += 1
            
        # Е) Молчуны (есть в табл. молчунов + НЕ завершен + НАЧАЛ диалог)
        # Мы используем флаг user_started, который вы определили чуть выше в этом же цикле
        if d.inactive_alerts and d.status not in ['qualified', 'rejected'] and user_started:
            m["молчуны_в_очереди_счетчик"] += 1

    # 2. ФОРМИРОВАНИЕ СТРОК ДЛЯ EXCEL С ВЫЧИСЛЕНИЯМИ
    rows = []
    for (dt, rec, cit, vac), m in report_map.items():
        # Математика по твоим правилам:
        отклики = m["отклики_всего"]
        начали_диалог = m["начали_диалог_счетчик"]
        не_вступили = отклики - начали_диалог
        
        собес = m["собес_счетчик"]
        отказался_кд = m["отказался_кд_счетчик"]
        отказали_мы = m["отказали_мы_счетчик"]
        отказы_всего = отказался_кд + отказали_мы
        
        # Теперь это "чистые" молчуны (кто начал общение, но не закончил и замолчал)
        молчуны = m["молчуны_в_очереди_счетчик"]

        rows.append({
            "Дата": dt, 
            "Рекрутер": rec, 
            "Город": cit, 
            "Вакансия": vac,
            "Отклики": отклики, 
            "Не вступили": не_вступили,
            "Начали диалог": начали_диалог, 
            "Собес": собес,
            "Отказался КД": отказался_кд, 
            "Отказали мы": отказали_мы, 
            "Молчуны": молчуны,
            "Отказы всего": отказы_всего
        })

    # Создание DataFrame
    df_base = pd.DataFrame(rows)
    # Сортировка по дате (внутренняя)
    df_base['dt_obj'] = pd.to_datetime(df_base['Дата'], format='%d.%m.%Y')
    df_base = df_base.sort_values(['dt_obj', 'Рекрутер']).drop(columns=['dt_obj'])

    # 3. ФУНКЦИЯ ДЛЯ СВОДНЫХ ТАБЛИЦ (с расчетом конверсий)
    def create_summary_df(groupby_col):
        summary = df_base.groupby(groupby_col).agg({
            'Отклики': 'sum', 'Не вступили': 'sum', 'Начали диалог': 'sum', 'Собес': 'sum',
            'Отказался КД': 'sum', 'Отказали мы': 'sum', 'Молчуны': 'sum', 'Отказы всего': 'sum'
        }).reset_index()

        s = summary
        # Конверсии
        s['Собес/отклик %'] = (s['Собес'] / s['Отклики']).fillna(0)
        s['Молчуны/Диалог %'] = (s['Молчуны'] / s['Начали диалог']).fillna(0)
        s['Отказы/Диалог %'] = (s['Отказы всего'] / s['Начали диалог']).fillna(0)

        # Итоговая строка
        total = s.sum(numeric_only=True)
        total[groupby_col] = 'ИТОГО'
        
        # Пересчет конверсий для строки ИТОГО
        t_resp = total['Отклики'] if total['Отклики'] > 0 else 1
        t_dial = total['Начали диалог'] if total['Начали диалог'] > 0 else 1
        total['Собес/отклик %'] = total['Собес'] / t_resp
        total['Молчуны/Диалог %'] = total['Молчуны'] / t_dial
        total['Отказы/Диалог %'] = total['Отказы всего'] / t_dial
        
        return pd.concat([s, pd.DataFrame([total])], ignore_index=True)

    # Генерация листов
    df_date = create_summary_df('Дата')
    df_rec = create_summary_df('Рекрутер')
    df_city = create_summary_df('Город')
    df_vac = create_summary_df('Вакансия')

    # 4. СОХРАНЕНИЕ В EXCEL (с форматированием)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df_date.to_excel(writer, index=False, sheet_name='Свод по датам')
        df_rec.to_excel(writer, index=False, sheet_name='Свод по рекрутерам')
        df_city.to_excel(writer, index=False, sheet_name='Свод по городам')
        df_vac.to_excel(writer, index=False, sheet_name='Свод по вакансиям')
        df_base.to_excel(writer, index=False, sheet_name='Общий отчет')

        workbook = writer.book
        # Форматы
        header_fmt = workbook.add_format({'bold': True, 'bg_color': '#D9EAD3', 'border': 1})
        perc_fmt = workbook.add_format({'num_format': '0%', 'border': 1})
        num_fmt = workbook.add_format({'border': 1})
        total_fmt = workbook.add_format({'bold': True, 'bg_color': '#F4CCCC', 'border': 1})

        for sheet_name in writer.sheets:
            ws = writer.sheets[sheet_name]
            ws.set_column('A:Z', 15, num_fmt)
            # Применяем процентный формат к колонкам с % в названии
            current_df = df_date if sheet_name == 'Свод по датам' else df_base # упрощенно
            for i, col in enumerate(df_date.columns):
                if '%' in col:
                    ws.set_column(i, i, 18, perc_fmt)

    output.seek(0)
    await message.answer_document(
        BufferedInputFile(output.read(), filename=f"Detail_Report_{start_date} - {end_date}.xlsx"),
        caption=f"📊 Расширенный отчет ({start_date} - {end_date})"
    )
    await msg_wait.delete()
    await state.clear()





# Кнопка "Статистика за 7 дней" в меню Follow-up
@router.callback_query(F.data == "followup_7days_stats")
async def view_followup_text_stats(callback: CallbackQuery, db_session: Session):
    content = _build_followup_7day_stats_content(db_session)
    # Добавили reply_markup, чтобы можно было вернуться в меню
    await callback.message.edit_text(**content.as_kwargs(), reply_markup=back_to_stats_main_keyboard)
    await callback.answer()

# Кнопка "Экспорт в Excel" в меню Follow-up
@router.callback_query(F.data == "followup_excel_start")
async def followup_export_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(FollowupExportStates.waiting_for_range)
    # ИСПРАВЛЕНО: Текст объединен в один аргумент, добавлен parse_mode="HTML"
    await callback.message.answer(
        "📊 [FOLLOW-UP] За какой период выгрузить Excel?\n\n"
        "Выберите кнопку или пришлите диапазон:\n<code>01.12.2025 - 15.12.2025</code>",
        reply_markup=export_date_options_keyboard,
        parse_mode="HTML"
    )
    await callback.answer()

# Обработка выбора диапазона (быстрые кнопки)
@router.callback_query(FollowupExportStates.waiting_for_range, F.data.startswith("export_range_"))
async def followup_export_quick(callback: CallbackQuery, state: FSMContext, db_session: Session):
    days_count = int(callback.data.split("_")[-1])
    start_date = date.today() - timedelta(days=days_count-1)
    await generate_followup_excel(callback.message, start_date, date.today(), db_session, state)
    await callback.answer()

# Обработка ручного ввода даты
@router.message(FollowupExportStates.waiting_for_range)
async def followup_export_manual(message: Message, state: FSMContext, db_session: Session):
    try:
        parts = message.text.split("-")
        start_date = datetime.strptime(parts[0].strip(), "%d.%m.%Y").date()
        end_date = datetime.strptime(parts[1].strip(), "%d.%m.%Y").date()
        await generate_followup_excel(message, start_date, end_date, db_session, state)
    except Exception:
        await message.answer("❌ Формат: 01.01.2024 - 10.01.2024")

# Обработка выбора "Обычная статистика"
@router.callback_query(F.data == "stats_regular")
async def stats_regular_choice(callback: CallbackQuery):
    await callback.message.edit_text("Выбрана обычная статистика. Выберите формат:", reply_markup=stats_regular_keyboard)
    await callback.answer()

# Обработка выбора "Follow up"
@router.callback_query(F.data == "stats_followup")
async def stats_followup_choice(callback: CallbackQuery):
    await callback.message.edit_text("Выбрана статистика Follow up. Выберите формат:", reply_markup=stats_followup_keyboard)
    await callback.answer()

# Возврат в главное меню статистики
@router.callback_query(F.data == "stats_back_to_main")
async def stats_back_to_main(callback: CallbackQuery):
    await callback.message.edit_text("Выберите режим работы со статистикой:", reply_markup=stats_main_menu_keyboard)
    await callback.answer()



