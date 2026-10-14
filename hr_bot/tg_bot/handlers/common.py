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
from datetime import date, datetime, timedelta, timezone
from sqlalchemy.orm import Session
from aiogram.utils.formatting import Text, Bold, Italic
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from hr_bot.db.models import MailingTask 
from hr_bot.db.models import (
    TelegramUser, Statistic, Vacancy, TrackedRecruiter,
    InactiveNotificationQueue, RejectedNotificationQueue, NotificationQueue, Dialogue,
    AnalyticsEvent 
)
from sqlalchemy import func, cast, Date, Integer, and_
from hr_bot.tg_bot.keyboards import (
    user_keyboard, admin_keyboard, 
    stats_main_menu_keyboard, stats_regular_keyboard, 
    stats_followup_keyboard, back_to_stats_main_keyboard, 
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
from aiogram.utils.formatting import Bold, Italic, Text
from aiogram.utils.keyboard import InlineKeyboardBuilder
import io
import pandas as pd
from datetime import date
from sqlalchemy import cast, Date
from sqlalchemy.orm import Session
from aiogram.types import Message, BufferedInputFile
from aiogram.fsm.context import FSMContext
from hr_bot.db.models import Dialogue, InactiveNotificationQueue
from sqlalchemy import func, cast, Date, and_ 
from zoneinfo import ZoneInfo 

SPB_TIMEZONE = ZoneInfo("Europe/Moscow")

logger = logging.getLogger(__name__)
router = Router()

class ExportStates(StatesGroup):
    waiting_for_range = State()

class FollowupExportStates(StatesGroup):
    waiting_for_range = State()


class ReactivationExportStates(StatesGroup):
    waiting_for_range = State()

class MailingTaskStates(StatesGroup):
    waiting_for_vacancy = State()
    waiting_for_city = State()
    waiting_for_days = State()
    waiting_for_message = State()
    waiting_for_confirmation = State()

def get_reactivation_menu_keyboard():
    """Возвращает клавиатуру с двумя кнопками: просмотр задач / создать новую."""
    builder = InlineKeyboardBuilder()
    builder.button(text="👁️ Текущие задачи", callback_data="reactivation_view_tasks")
    builder.button(text="✨ Создать новую", callback_data="reactivation_create_new")
    builder.adjust(1)  # Кнопки в одну колонку
    return builder.as_markup()



# 2. Вход в режим реактивации — теперь показывает МЕНЮ
@router.message(F.text == "🚀 Реактивация")
async def start_mailing_task_creation(message: Message, state: FSMContext):
    # Сначала очищаем состояние, если пользователь зашёл из другого потока
    await state.clear()
    await message.answer(
        "🚀 <b>Реактивация кандидатов</b>\n\n"
        "Что вы хотите сделать?",
        reply_markup=get_reactivation_menu_keyboard(),
        parse_mode="HTML"
    )

# 5. Кнопка "Назад" в меню реактивации
@router.callback_query(F.data == "reactivation_back")
async def reactivation_back(callback: CallbackQuery):
    await callback.message.edit_text(
        "🚀 <b>Реактивация кандидатов</b>\n\n"
        "Что вы хотите сделать?",
        reply_markup=get_reactivation_menu_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

# 3. Просмотр текущих задач реактивации
@router.callback_query(F.data == "reactivation_view_tasks")
async def view_active_mailing_tasks(callback: CallbackQuery, db_session: Session):
    # Загружаем активные задачи, сортируем по времени создания (новые сверху)
    active_tasks = db_session.query(MailingTask).filter(
        MailingTask.status.in_(['pending', 'in_progress', 'paused_no_funds'])
    ).order_by(MailingTask.created_at.desc()).limit(10).all()
    
    if not active_tasks:
        await callback.message.edit_text(
            "📋 <b>Нет активных задач</b>\n\n"
            "На данный момент в очереди нет задач на реактивацию.\n"
            "Создайте новую, чтобы запустить рассылку по «молчунам».",
            reply_markup=get_reactivation_menu_keyboard(),
            parse_mode="HTML"
        )
        await callback.answer()
        return
    
    # Формируем список задач
    tasks_text = "📋 <b>Активные задачи реактивации:</b>\n\n"
    
    for task in active_tasks:
        # Статус эмодзи
        status_emoji = {
            'pending': '⏳',
            'in_progress': '🔄',
            'paused_no_funds': '💸',
            'completed': '✅',
            'failed': '❌'
        }.get(task.status, '❓')
        
        # Прогресс, если задача в работе
        progress = ""
        if task.candidates_found > 0:
            pct = round(task.processed_count / task.candidates_found * 100, 1)
            progress = f"\n   📊 Прогресс: {task.processed_count}/{task.candidates_found} ({pct}%)"
        
        tasks_text += (
            f"{status_emoji} <b>Задача #{task.id}</b>\n"
            f"   🔹 Вакансия: <code>{task.vacancy_pattern}</code>\n"
            f"   🔹 Город: <code>{task.city_pattern}</code>\n"
            f"   🔹 Молчание: {task.days_silent_min}-{task.days_silent_max} дн.\n"
            f"   🔹 Найдено: {task.candidates_found}{progress}\n"
            f"   🔹 Отправлено: {task.messages_sent} | Пропущено: {task.skipped_count}\n"
            f"   🔹 Статус: <i>{task.status}</i>\n"
            f"   🕐 Создана: {task.created_at.astimezone(SPB_TIMEZONE).strftime('%d.%m %H:%M')}\n"
            "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
        )
    
    # Кнопка "Назад" + "Создать новую"
    kb = InlineKeyboardBuilder()
    kb.button(text="✨ Создать новую", callback_data="reactivation_create_new")
    kb.button(text="⬅️ Назад", callback_data="reactivation_back")
    kb.adjust(1)
    
    await callback.message.edit_text(
        tasks_text,
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


# 4. Запуск создания новой задачи (из меню)
@router.callback_query(F.data == "reactivation_create_new")
async def start_new_mailing_task(callback: CallbackQuery, state: FSMContext):
    # 1. Сначала подтверждаем callback!
    await callback.answer()
    
    # 2. Затем удаляем меню
    await callback.message.delete()
    
    # 3. Запускаем FSM
    await state.set_state(MailingTaskStates.waiting_for_vacancy)
    await callback.message.answer(
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

# 6. Собираем Текст и показываем Превью с расчетом кандидатов
@router.message(MailingTaskStates.waiting_for_message)
async def process_mailing_text(message: Message, state: FSMContext, db_session: Session):
    await state.update_data(message_text=message.text)
    data = await state.get_data()
    
    # --- ЛОГИКА РАСЧЕТА 
    now_utc = datetime.now(timezone.utc)
    cutoff_recent = now_utc - timedelta(days=data['days_min'])
    cutoff_oldest = now_utc - timedelta(days=data['days_max'])

    query = (
        db_session.query(func.count(Dialogue.id))
        .join(Vacancy, Dialogue.vacancy_id == Vacancy.id)
        .join(TrackedRecruiter, Dialogue.recruiter_id == TrackedRecruiter.id)
        .filter(
            Dialogue.status == 'timed_out',
            Dialogue.dialogue_state.notin_(['vacancy_closed_or_resume_not_found', 'refusal']),
            Dialogue.last_message_at <= cutoff_recent,
            Dialogue.last_message_at >= cutoff_oldest,
            TrackedRecruiter.is_auth_active == True,
            TrackedRecruiter.access_token.isnot(None) 
        )
    )

    # Логика паттернов (в воркере f"%{task.vacancy_pattern}%")
    if data['vacancy_pattern'] and data['vacancy_pattern'] != '%':
        query = query.filter(Vacancy.title.ilike(f"%{data['vacancy_pattern']}%"))
    
    if data['city_pattern'] and data['city_pattern'] != '%':
        query = query.filter(Vacancy.city.ilike(f"%{data['city_pattern']}%"))
    candidates_count = query.scalar() or 0

    preview = (
        f"📋 <b>Проверка параметров рассылки:</b>\n\n"
        f"👥 Будет охвачено: <b>{candidates_count} чел.</b>\n"
        f"🔹 Вакансия: <code>{data['vacancy_pattern']}</code>\n"
        f"🔹 Город: <code>{data['city_pattern']}</code>\n"
        f"🔹 Молчание: от <b>{data['days_min']}</b> до <b>{data['days_max']}</b> дней\n\n"
        f"📝 <b>Текст сообщения:</b>\n"
        f"<i>{data['message_text']}</i>\n\n"
        f"⚠️ После запуска бот найдет подходящих кандидатов и начнет отправку. "
        f"Списание средств произойдет по тарифу Реактивации за каждое успешное сообщение."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ ЗАПУСТИТЬ", callback_data="confirm_mailing_start")
    kb.button(text="❌ ОТМЕНА", callback_data="cancel_fsm")
    kb.adjust(1)
    
    await state.set_state(MailingTaskStates.waiting_for_confirmation)
    
    # Если кандидатов 0, просто добавим предупреждение в текст, но не ломаем интерфейс
    if candidates_count == 0:
        preview += "\n\n⚠️ <b>Внимание:</b> По вашим фильтрам найдено 0 кандидатов. Если запустите, рассылка сразу завершится."

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

def _build_reactivation_7day_stats_content(db_session: Session) -> Text:
    content_parts = [Bold("🤐 Воронка Реактивации за 7 дней:"), "\n", Italic("(по дате запуска рассылки)"), "\n\n"]
    days = [date.today() - timedelta(days=i) for i in range(7)]
    has_any_data = False

    for day in days:
        # 1. Находим "базу": пары (диалог + рассылка), где отправка сообщения была именно в этот день
        # Это гарантирует, что мы анализируем результаты рассылок, запущенных в дату 'day'
        base_sent = db_session.query(
            AnalyticsEvent.dialogue_id,
            AnalyticsEvent.mailing_task_id
        ).filter(
            AnalyticsEvent.event_type == 'reactivation_sent',
            AnalyticsEvent.mode == 'reactivation',
            cast(AnalyticsEvent.created_at, Date) == day
        ).subquery()

        # 2. Собираем все флаги событий для этих пар. 
        # Группировка по (dialogue_id, mailing_task_id) делает воронку уникальной.
        # Берем события ТОЛЬКО с mode='reactivation'
        results = db_session.query(
            AnalyticsEvent.dialogue_id,
            func.max(cast(AnalyticsEvent.event_type == 'user_reply', Integer)).label('has_reply'),
            func.max(cast(AnalyticsEvent.event_type == 'interview_assigned', Integer)).label('has_interview'),
            func.max(cast(AnalyticsEvent.event_type == 'rejected_by_bot', Integer)).label('has_bot_rej'),
            func.max(cast(AnalyticsEvent.event_type == 'refusal_by_user', Integer)).label('has_user_ref'),
            func.max(cast(AnalyticsEvent.event_type == 'timed_out', Integer)).label('has_timeout')
        ).join(
            base_sent,
            and_(
                AnalyticsEvent.dialogue_id == base_sent.c.dialogue_id,
                AnalyticsEvent.mailing_task_id == base_sent.c.mailing_task_id
            )
        ).filter(
            AnalyticsEvent.mode == 'reactivation'
        ).group_by(
            AnalyticsEvent.dialogue_id,
            AnalyticsEvent.mailing_task_id
        ).all()

        sent_total = len(results)

        if sent_total > 0:
            has_any_data = True
            
            replied_count = 0
            success_count = 0      # Записаны
            refusal_bot_count = 0  # Отказ бот
            refusal_user_count = 0 # Отказ сам
            timeout_count = 0      # Снова уснули
            in_progress_count = 0  # В работе

            for d in results:
                # В статистику ответов попадают те, кто совершил 'user_reply' в режиме реактивации
                if d.has_reply:
                    replied_count += 1
                    
                # --- САМОИСКЛЮЧАЮЩАЯ ВОРОНКА (ПРИОРИТЕТЫ) ---
                # ПРИОРИТЕТ 2: Собеседование (Записаны)
                if d.has_interview:
                    success_count += 1
                # ПРИОРИТЕТ 1: Любой отказ (Самый жесткий финал)
                elif d.has_user_ref:
                    refusal_user_count += 1
                elif d.has_bot_rej:
                    refusal_bot_count += 1
                # ПРИОРИТЕТ 3: Снова уснули (Timed Out)
                elif d.has_timeout:
                    timeout_count += 1
                # ПРИОРИТЕТ 4: В работе (если был ответ)
                elif d.has_reply:
                    in_progress_count += 1

            # Конверсия из "Отправлено" в "Ответ"
            conv_reply = round((replied_count / sent_total * 100), 1) if sent_total > 0 else 0
            day_str = day.strftime('%d.%m (%a)')

            content_parts.extend([
                Bold(f"📅 Рассылка от {day_str}"), "\n",
                f"   📤 Отправлено: {sent_total}\n",
                f"   💬 Ответило: {replied_count} ", Italic(f"({conv_reply}%)"), "\n",
                f"   ✅ Записаны: {success_count}\n",
                f"   🤖 Отказ (бот): {refusal_bot_count}\n",
                f"   🙅‍♂️ Отказ (сам): {refusal_user_count}\n",
                f"   💤 Снова уснули: {timeout_count}\n",
                f"   ⏳ В работе: {in_progress_count}\n",
                "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
            ])

    if not has_any_data:
        return Text("📊 Данных по запуску рассылок за 7 дней не найдено.")
    
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
    msg_wait = await message.answer("⏳ Собираю данные по рассылкам и формирую прозрачную воронку...")

    # 1. ГЛОБАЛЬНЫЙ ЗАПРОС: Находим всех кандидатов из рассылок за период
    # и собираем флаги всех их достижений в рамках этой рассылки
    query = db.query(
        AnalyticsEvent.dialogue_id,
        AnalyticsEvent.mailing_task_id,
        # Дату рассылки берем как дату события 'reactivation_sent' в этой группе
        func.min(cast(AnalyticsEvent.created_at, Date)).label('task_date'),
        TrackedRecruiter.name.label('recruiter_name'),
        Vacancy.city.label('city'),
        Vacancy.title.label('vacancy_title'),
        # Собираем флаги (1 если событие было, 0 если нет)
        func.max(cast(AnalyticsEvent.event_type == 'reactivation_sent', Integer)).label('is_sent'),
        func.max(cast(AnalyticsEvent.event_type == 'user_reply', Integer)).label('is_reply'),
        func.max(cast(AnalyticsEvent.event_type == 'interview_assigned', Integer)).label('is_interview'),
        func.max(cast(AnalyticsEvent.event_type == 'rejected_by_bot', Integer)).label('is_bot_rej'),
        func.max(cast(AnalyticsEvent.event_type == 'refusal_by_user', Integer)).label('is_user_ref'),
        func.max(cast(AnalyticsEvent.event_type == 'timed_out', Integer)).label('is_timeout')
    ).outerjoin(
        TrackedRecruiter, AnalyticsEvent.recruiter_id == TrackedRecruiter.id
    ).outerjoin(
        Vacancy, AnalyticsEvent.vacancy_id == Vacancy.id
    ).filter(
        AnalyticsEvent.mode == 'reactivation'
    ).group_by(
        AnalyticsEvent.dialogue_id, 
        AnalyticsEvent.mailing_task_id,
        TrackedRecruiter.name, 
        Vacancy.city, 
        Vacancy.title
    ).having(
        # ОЧЕНЬ ВАЖНО: берем только те группы, где ОТПРАВКА была в наш период
        and_(
            func.max(cast(AnalyticsEvent.event_type == 'reactivation_sent', Integer)) == 1,
            cast(func.min(AnalyticsEvent.created_at), Date) >= start_date,
            cast(func.min(AnalyticsEvent.created_at), Date) <= end_date
        )
    ).all()

    if not query:
        await msg_wait.edit_text("🤷 За этот период запусков Реактивации не найдено.")
        await state.clear()
        return

    # 2. ПЕРВИЧНАЯ ГРУППИРОВКА (Сырые данные -> Сводная мапа)
    report_map = {}
    for d in query:
        dt_str = d.task_date.strftime("%d.%m.%Y")
        rec = d.recruiter_name or "Не указан"
        cit = d.city or "Не указан"
        vac = d.vacancy_title or "Не указана"
        key = (dt_str, rec, cit, vac)

        if key not in report_map:
            report_map[key] = {
                "инициировано": 0, "вступили": 0, "собес": 0, 
                "отказали_мы": 0, "отказался_кд": 0, "молчуны": 0
            }
        
        m = report_map[key]
        m["инициировано"] += 1
        
        if d.is_reply: 
            m["вступили"] += 1
        
        # ЛОГИКА ПРИОРИТЕТОВ (один КД = одна категория)
        if d.is_interview:
            m["собес"] += 1
        elif d.is_bot_rej:
            m["отказали_мы"] += 1
        elif d.is_user_ref:
            m["отказался_кд"] += 1
        elif d.is_timeout and d.is_reply:
            m["молчуны"] += 1

    # 3. ПОДГОТОВКА СТРОК ДЛЯ DATAFRAME
    rows = []
    for (dt, rec, cit, vac), m in report_map.items():
        rows.append({
            "Дата запуска": dt, "Рекрутер": rec, "Город": cit, "Вакансия": vac,
            "Инициировано": m["инициировано"],
            "Не вступили": m["инициировано"] - m["вступили"],
            "Вступили": m["вступили"],
            "Собес": m["собес"],
            "Отказался КД": m["отказался_кд"],
            "Отказали мы": m["отказали_мы"],
            "Молчуны": m["молчуны"],
            "Отказы всего": m["отказали_мы"] + m["отказался_кд"]
        })

    df_base = pd.DataFrame(rows)

    # 4. ФУНКЦИЯ ДЛЯ СВОДНЫХ ТАБЛИЦ (Группировка + Конверсии)
    def create_reactivation_summary(groupby_col):
        summary = df_base.groupby(groupby_col).agg({
            'Инициировано': 'sum', 'Вступили': 'sum', 'Не вступили': 'sum',
            'Собес': 'sum', 'Отказался КД': 'sum', 'Отказали мы': 'sum', 
            'Молчуны': 'sum', 'Отказы всего': 'sum'
        }).reset_index()

        # Расчет конверсий от Инициировано
        summary['Собес/Инициировано %'] = (summary['Собес'] / summary['Инициировано']).fillna(0)
        summary['Молчуны+Не вступили %'] = ((summary['Молчуны'] + summary['Не вступили']) / summary['Инициировано']).fillna(0)
        summary['Отказы всего %'] = (summary['Отказы всего'] / summary['Инициировано']).fillna(0)

        # Итоговая строка
        total = summary.sum(numeric_only=True)
        total[groupby_col] = 'ИТОГО'
        
        # Пересчет % для ИТОГО
        t_init = total['Инициировано'] if total['Инициировано'] > 0 else 1
        total['Собес/Инициировано %'] = total['Собес'] / t_init
        total['Молчуны+Не вступили %'] = (total['Молчуны'] + total['Не вступили']) / t_init
        total['Отказы всего %'] = total['Отказы всего'] / t_init
        
        return pd.concat([summary, pd.DataFrame([total])], ignore_index=True)

    # Генерируем срезы
    df_date = create_reactivation_summary('Дата запуска')
    df_rec = create_reactivation_summary('Рекрутер')
    df_city = create_reactivation_summary('Город')
    df_vac = create_reactivation_summary('Вакансия')

    # 5. СОХРАНЕНИЕ В EXCEL
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df_date.to_excel(writer, index=False, sheet_name='Свод по датам')
        df_rec.to_excel(writer, index=False, sheet_name='Свод по рекрутерам')
        df_city.to_excel(writer, index=False, sheet_name='Свод по городам')
        df_vac.to_excel(writer, index=False, sheet_name='Свод по вакансиям')
        df_base.to_excel(writer, index=False, sheet_name='Общий отчет')

        workbook = writer.book
        # Стили (Розовый для Реактивации)
        header_fmt = workbook.add_format({'bold': True, 'bg_color': '#F4CCCC', 'border': 1, 'align': 'center'})
        total_fmt = workbook.add_format({'bold': True, 'bg_color': '#F4CCCC', 'border': 1, 'align': 'center'})
        perc_fmt = workbook.add_format({'num_format': '0%', 'border': 1, 'align': 'center'})
        num_fmt = workbook.add_format({'border': 1, 'align': 'center'})

        for sheet_name in writer.sheets:
            ws = writer.sheets[sheet_name]
            ws.freeze_panes(1, 0)
            
            # Выбор текущего DF для настройки ширины
            if sheet_name == 'Свод по датам': curr_df = df_date
            elif sheet_name == 'Свод по рекрутерам': curr_df = df_rec
            elif sheet_name == 'Свод по городам': curr_df = df_city
            elif sheet_name == 'Свод по вакансиям': curr_df = df_vac
            else: curr_df = df_base

            for i, col in enumerate(curr_df.columns):
                column_len = max(curr_df[col].astype(str).str.len().max(), len(str(col))) + 3
                if '%' in str(col):
                    ws.set_column(i, i, 20, perc_fmt)
                else:
                    ws.set_column(i, i, column_len, num_fmt)
                ws.write(0, i, col, header_fmt)

            # Форматирование строки ИТОГО
            if sheet_name != 'Общий отчет':
                last_row = len(curr_df) - 1
                for i in range(len(curr_df.columns)):
                    val = curr_df.iloc[last_row, i]
                    fmt = total_fmt if '%' not in curr_df.columns[i] else workbook.add_format({'bold': True, 'bg_color': '#F4CCCC', 'border': 1, 'num_format': '0%', 'align': 'center'})
                    ws.write(last_row + 1, i, val, fmt)

    output.seek(0)
    await message.answer_document(
        BufferedInputFile(output.read(), filename=f"Reactivation_Funnel_{start_date}_{end_date}.xlsx"),
        caption=f"🤐 Прозрачная воронка реактивации ({start_date} - {end_date})"
    )
    await msg_wait.delete()
    await state.clear()



def _build_followup_7day_stats_content(db_session: Session) -> Text:
    content_parts = [Bold("🤖 Статистика по дням собеседований (7 дней):"), "\n\n"]
    # Берем вчерашний день и 6 дней до него (так как за сегодня данных по исходам еще мало)
    days = [date.today() - timedelta(days=i) for i in range(7)]
    has_any_data = False

    for day in days:
        # Получаем все события, привязанные к этой дате собеседования
        # Считаем уникальные диалоги для каждого события, чтобы избежать дублей
        events_query = db_session.query(
            AnalyticsEvent.event_type,
            func.count(func.distinct(AnalyticsEvent.dialogue_id))
        ).filter(
            AnalyticsEvent.interview_date == day
        ).group_by(AnalyticsEvent.event_type).all()

        stats = {e[0]: e[1] for e in events_query}

        # БАЗА: Сколько всего человек было записано на этот день
        # (событие interview_assigned могло быть создано когда угодно, но поле interview_date указывает на 'day')
        assigned = stats.get('interview_assigned', 0)

        if assigned > 0:
            has_any_data = True
            
            # 1. Вступили в дожим (ответили на FU)
            replied = stats.get('followup_reply', 0)
            
            # 2. Переназначено (из режима дожима)
            rescheduled = stats.get('followup_rescheduled', 0)
            
            # 3. Финалы: Устроились и Отказали
            success = stats.get('followup_success', 0)
            refusal = stats.get('followup_refusal', 0)
            
            # 4. Пришли (сумма успехов и отказов после встречи)
            visited = success + refusal
            
            # 5. Не пришли (подтвержденная неявка или Шаг 3 дожима)
            no_show = stats.get('followup_no_show', 0)

            # Расчет доходимости
            conv_visited = round((visited / assigned * 100), 1) if assigned > 0 else 0

            day_str = day.strftime('%d.%m (%a)')
            content_parts.extend([
                Bold(f"📅 Собеседования на {day_str}"), "\n",
                f"   Записано было: {assigned}\n",
                f"   - ✅ Пришли: {visited} ", Italic(f"({conv_visited}%)"), "\n",
                f"      └ 💼 Устроились: {success}\n",
                f"      └ ❌ Не подошли: {refusal}\n",
                f"   - 🤷‍♂️ Не пришли: {no_show}\n",
                f"   - 🔄 Перенесли: {rescheduled}\n",
                f"   - 💬 Ответили на follow up: {replied}\n",
                "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
            ])

    if not has_any_data:
        return Text("📊 Данных по запланированным собеседованиям за 7 дней не найдено.")

    return Text(*content_parts)


async def generate_followup_excel(message: Message, start_date: date, end_date: date, db: Session, state: FSMContext):
    msg_wait = await message.answer("⏳ Собираю данные по итогам собеседований за период...")
    
    # 1. ГЛУБОКИЙ СБОР ДАННЫХ ИЗ ТАБЛИЦЫ СОБЫТИЙ
    # Группируем по dialogue_id + interview_date, чтобы получить флаги всех этапов FU
    query = db.query(
        AnalyticsEvent.dialogue_id,
        AnalyticsEvent.interview_date,
        TrackedRecruiter.name.label('recruiter_name'),
        Vacancy.city.label('city'),
        Vacancy.title.label('vacancy_title'),
        # Собираем флаги по типам событий
        func.max(cast(AnalyticsEvent.event_type == 'interview_assigned', Integer)).label('is_assigned'),
        func.max(cast(AnalyticsEvent.event_type == 'followup_sent', Integer)).label('is_fu_sent'),
        func.max(cast(AnalyticsEvent.event_type == 'followup_reply', Integer)).label('is_fu_reply'),
        func.max(cast(AnalyticsEvent.event_type == 'followup_success', Integer)).label('is_success'),
        func.max(cast(AnalyticsEvent.event_type == 'followup_refusal', Integer)).label('is_refusal'),
        func.max(cast(AnalyticsEvent.event_type == 'followup_no_show', Integer)).label('is_no_show'),
        func.max(cast(AnalyticsEvent.event_type == 'followup_rescheduled', Integer)).label('is_rescheduled')
    ).outerjoin(
        TrackedRecruiter, AnalyticsEvent.recruiter_id == TrackedRecruiter.id
    ).outerjoin(
        Vacancy, AnalyticsEvent.vacancy_id == Vacancy.id
    ).filter(
        # ФИЛЬТР: по дате СОБЕСЕДОВАНИЯ
        AnalyticsEvent.interview_date >= start_date,
        AnalyticsEvent.interview_date <= end_date
    ).group_by(
        AnalyticsEvent.dialogue_id, 
        AnalyticsEvent.interview_date,
        TrackedRecruiter.name, 
        Vacancy.city, 
        Vacancy.title
    ).all()

    if not query:
        await msg_wait.edit_text("🤷 За этот период событий по собеседованиям не найдено.")
        await state.clear()
        return

    # 2. ПОДГОТОВКА СЫРЫХ ДАННЫХ ДЛЯ ЛИСТА "Сырые данные"
    rows = []
    for d in query:
        rows.append({
            "Дата собеса": d.interview_date.strftime("%d.%m.%Y"),
            "Рекрутер": d.recruiter_name or "Не указан",
            "Город": d.city or "Не указан",
            "Вакансия": d.vacancy_title or "Не указана",
            "Назначено": d.is_assigned,
            "Инициировано FU": d.is_fu_sent,
            "Ответили на FU": d.is_fu_reply,
            "Пришло": 1 if (d.is_success or d.is_refusal) else 0,
            "💼 Устроились": d.is_success,
            "❌ Не подошли": d.is_refusal,
            "🤷‍♂️ Не пришли": d.is_no_show,
            "🔄 Переназначено": d.is_rescheduled
        })

    df_base = pd.DataFrame(rows)

    # 3. ФУНКЦИЯ ДЛЯ ГЕНЕРАЦИИ СВОДНЫХ ЛИСТОВ
    def create_fu_summary(groupby_col):
        summary = df_base.groupby(groupby_col).agg({
            'Назначено': 'sum',
            'Инициировано FU': 'sum',
            'Ответили на FU': 'sum',
            'Пришло': 'sum',
            '💼 Устроились': 'sum',
            '❌ Не подошли': 'sum',
            '🤷‍♂️ Не пришли': 'sum',
            '🔄 Переназначено': 'sum'
        }).reset_index()

        # Расчет конверсий
        # Доходимость — это % Пришедших от Назначенных
        summary['Доходимость %'] = (summary['Пришло'] / summary['Назначено']).fillna(0)
        # % Устроились от пришедших
        summary['Устроились/Пришли %'] = (summary['💼 Устроились'] / summary['Пришло']).fillna(0)

        # Итоговая строка
        total = summary.sum(numeric_only=True)
        total[groupby_col] = 'ИТОГО'
        
        # Пересчет % для ИТОГО (чтобы не суммировались проценты)
        t_naz = total['Назначено'] if total['Назначено'] > 0 else 1
        t_pri = total['Пришло'] if total['Пришло'] > 0 else 1
        total['Доходимость %'] = total['Пришло'] / t_naz
        total['Устроились/Пришли %'] = total['💼 Устроились'] / t_pri
        
        return pd.concat([summary, pd.DataFrame([total])], ignore_index=True)

    # Генерируем срезы
    df_date = create_fu_summary('Дата собеса')
    df_rec = create_fu_summary('Рекрутер')
    df_city = create_fu_summary('Город')
    df_vac = create_fu_summary('Вакансия')

    # 4. СОХРАНЕНИЕ В EXCEL
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df_date.to_excel(writer, index=False, sheet_name='Свод по датам')
        df_rec.to_excel(writer, index=False, sheet_name='Свод по рекрутерам')
        df_city.to_excel(writer, index=False, sheet_name='Свод по городам')
        df_vac.to_excel(writer, index=False, sheet_name='Свод по вакансиям')
        df_base.to_excel(writer, index=False, sheet_name='Сырые данные')

        workbook = writer.book
        # Стили
        header_fmt = workbook.add_format({'bold': True, 'bg_color': '#CFE2F3', 'border': 1, 'align': 'center'})
        total_fmt = workbook.add_format({'bold': True, 'bg_color': '#F4CCCC', 'border': 1, 'align': 'center'})
        perc_fmt = workbook.add_format({'num_format': '0%', 'border': 1, 'align': 'center'})
        num_fmt = workbook.add_format({'border': 1, 'align': 'center'})

        for sheet_name in writer.sheets:
            ws = writer.sheets[sheet_name]
            ws.freeze_panes(1, 0)
            
            # Выбор текущего DF для настройки ширины
            if sheet_name == 'Свод по датам': curr_df = df_date
            elif sheet_name == 'Свод по рекрутерам': curr_df = df_rec
            elif sheet_name == 'Свод по городам': curr_df = df_city
            elif sheet_name == 'Свод по вакансиям': curr_df = df_vac
            else: curr_df = df_base

            for i, col in enumerate(curr_df.columns):
                # Авто-ширина колонок
                column_len = max(curr_df[col].astype(str).str.len().max(), len(str(col))) + 3
                
                # Применяем процентный формат, если в заголовке есть %
                if '%' in str(col):
                    ws.set_column(i, i, 18, perc_fmt)
                else:
                    ws.set_column(i, i, column_len, num_fmt)
                
                # Шапка
                ws.write(0, i, col, header_fmt)

            # Формат строки ИТОГО (кроме сырых данных)
            if sheet_name != 'Сырые данные':
                last_row = len(curr_df) - 1
                for i in range(len(curr_df.columns)):
                    val = curr_df.iloc[last_row, i]
                    # Если ячейка в итоговой строке процентная
                    is_perc = '%' in curr_df.columns[i]
                    fmt = workbook.add_format({'bold': True, 'bg_color': '#F4CCCC', 'border': 1, 'num_format': '0%' if is_perc else '', 'align': 'center'})
                    ws.write(last_row + 1, i, val, fmt)

    output.seek(0)
    await message.answer_document(
        BufferedInputFile(output.read(), filename=f"FollowUp_Report_{start_date}_{end_date}.xlsx"),
        caption=f"📈 Аналитика дожимов ({start_date} - {end_date})"
    )
    await msg_wait.delete()
    await state.clear()


def _build_7day_stats_content(db_session: Session) -> Text:
    content_parts = [Bold("📊 Статистика откликов за 7 дней:"), "\n", Italic("(по дате прихода кандидата)"), "\n\n"]
    
    days = [date.today() - timedelta(days=i) for i in range(7)]
    has_any_data = False

    for day in days:
        # 1. Получаем все диалоги, созданные в этот день в обычном режиме
        # И для каждого диалога проверяем наличие флагов из таблицы аналитики
        # Мы используем subquery или join, чтобы понять, что стало с этими диалогами
        
        query = db_session.query(
            Dialogue.id,
            # Считаем только те события, которые произошли в режиме 'standard'
            func.max(cast(and_(AnalyticsEvent.event_type == 'interview_assigned', AnalyticsEvent.mode == 'standard'), Integer)).label('is_qual'),
            func.max(cast(and_(AnalyticsEvent.event_type.in_(['rejected_by_bot', 'refusal_by_user']), AnalyticsEvent.mode == 'standard'), Integer)).label('is_rej'),
            func.max(cast(and_(AnalyticsEvent.event_type == 'timed_out', AnalyticsEvent.mode == 'standard'), Integer)).label('is_timeout')
        ).outerjoin(
            AnalyticsEvent, Dialogue.id == AnalyticsEvent.dialogue_id
        ).filter(
            cast(Dialogue.created_at, Date) == day
            # ВАЖНО: Мы убрали фильтр Dialogue.current_mode == 'standard', 
            # чтобы кандидат не исчезал из отчета, если его потом воскресили!
        ).group_by(Dialogue.id).all()

        if query:
            has_any_data = True
            
            total = len(query)       # Всего диалогов, созданных в этот день
            qual = 0                 # Собесы
            rej = 0                  # Отказы
            silent = 0               # Молчуны
            in_work = 0              # В работе

            for row in query:
                # ЛОГИКА ПРИОРИТЕТОВ (согласно твоему ТЗ):
                if row.is_qual:
                    qual += 1        # Если есть запись о собесе — это успех
                elif row.is_rej:
                    rej += 1         # Если собеса нет, но есть отказ — это отказ
                elif row.is_timeout:
                    silent += 1      # Если ни успеха, ни отказа, но был таймаут — это молчун
                else:
                    in_work += 1     # Остальное — в процессе (еще общаются или новый)

            day_str = day.strftime('%d.%m (%a)')
            content_parts.extend([
                Bold(f"📅 {day_str}"), "\n",
                f"   Откликов: ", Bold(str(total)), "\n",
                f"   - Подошло: {qual}\n",
                f"   - Отказов: {rej}\n",
                f"   - Молчуны: {silent}\n",
                f"   - В работе: {in_work}\n",
                "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
            ])

    if not has_any_data:
        return Text("📊 Данных за последние 7 дней не найдено.")

    return Text(*content_parts)







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
    msg_wait = await message.answer("⏳ Формирую детальный аналитический отчет по событиям...")
    
    # 1. ГЛУБОКИЙ СБОР ДАННЫХ
    # Мы собираем все диалоги за период и проверяем наличие событий в аналитике для каждого из них
    # Используем агрегацию (MAX/CASE), чтобы получить флаги событий для каждого диалога
    query = db.query(
        Dialogue.id,
        Dialogue.created_at,
        TrackedRecruiter.name.label('recruiter_name'),
        Vacancy.city.label('city'),
        Vacancy.title.label('vacancy_title'),
        # ЖЕСТКАЯ ИЗОЛЯЦИЯ: Учитываем только события режима 'standard'
        func.max(cast(and_(AnalyticsEvent.event_type == 'user_reply', AnalyticsEvent.mode == 'standard'), Integer)).label('has_reply'),
        func.max(cast(and_(AnalyticsEvent.event_type == 'interview_assigned', AnalyticsEvent.mode == 'standard'), Integer)).label('has_interview'),
        func.max(cast(and_(AnalyticsEvent.event_type == 'rejected_by_bot', AnalyticsEvent.mode == 'standard'), Integer)).label('has_bot_rej'),
        func.max(cast(and_(AnalyticsEvent.event_type == 'refusal_by_user', AnalyticsEvent.mode == 'standard'), Integer)).label('has_user_ref'),
        func.max(cast(and_(AnalyticsEvent.event_type == 'timed_out', AnalyticsEvent.mode == 'standard'), Integer)).label('has_timeout')
    ).outerjoin(
        AnalyticsEvent, Dialogue.id == AnalyticsEvent.dialogue_id
    ).outerjoin(
        TrackedRecruiter, Dialogue.recruiter_id == TrackedRecruiter.id
    ).outerjoin(
        Vacancy, Dialogue.vacancy_id == Vacancy.id
    ).filter(
        cast(Dialogue.created_at, Date) >= start_date,
        cast(Dialogue.created_at, Date) <= end_date
        # ВАЖНО: Убрано Dialogue.current_mode == 'standard'
    ).group_by(
        Dialogue.id, TrackedRecruiter.name, Vacancy.city, Vacancy.title
    ).all()

    if not query:
        await msg_wait.edit_text("🤷 За этот период откликов не найдено.")
        await state.clear()
        return

    # 2. ПЕРВИЧНАЯ ГРУППИРОВКА (Сырые данные -> Статистика по ключам)
    report_map = {}
    for d in query:
        dt_str = d.created_at.strftime("%d.%m.%Y")
        rec = d.recruiter_name or "Не указан"
        cit = d.city or "Не указан"
        vac = d.vacancy_title or "Не указана"
        key = (dt_str, rec, cit, vac)

        if key not in report_map:
            report_map[key] = {
                "отклики": 0, "вступили": 0, "собес": 0, 
                "отказали_мы": 0, "отказался_кд": 0, "молчуны": 0
            }
        
        m = report_map[key]
        m["отклики"] += 1
        
        if d.has_reply: 
            m["вступили"] += 1
        
        # ЛОГИКА ПРИОРИТЕТОВ (как в ТЗ для 7 дней)
        if d.has_interview:
            m["собес"] += 1
        elif d.has_bot_rej:
            m["отказали_мы"] += 1
        elif d.has_user_ref:
            m["отказался_кд"] += 1
        elif d.has_timeout and d.has_reply:
            # Молчун: замолчал + начал диалог + нет финала (собеса/отказа)
            m["молчуны"] += 1

    # 3. ПОДГОТОВКА СТРОК ДЛЯ DATAFRAME
    rows = []
    for (dt, rec, cit, vac), m in report_map.items():
        rows.append({
            "Дата": dt, "Рекрутер": rec, "Город": cit, "Вакансия": vac,
            "Отклики": m["отклики"],
            "Не вступили": m["отклики"] - m["вступили"],
            "Начали диалог": m["вступили"],
            "Собес": m["собес"],
            "Отказался КД": m["отказался_кд"],
            "Отказали мы": m["отказали_мы"],
            "Молчуны": m["молчуны"],
            "Отказы всего": m["отказали_мы"] + m["отказался_кд"]
        })

    df_base = pd.DataFrame(rows)
    # Сортировка по дате
    df_base['dt_obj'] = pd.to_datetime(df_base['Дата'], format='%d.%m.%Y')
    df_base = df_base.sort_values(['dt_obj', 'Рекрутер']).drop(columns=['dt_obj'])

    # 4. ФУНКЦИЯ ДЛЯ СВОДНЫХ ТАБЛИЦ (Группировка + Конверсии)
    def create_summary(groupby_col):
        summary = df_base.groupby(groupby_col).agg({
            'Отклики': 'sum', 'Не вступили': 'sum', 'Начали диалог': 'sum', 
            'Собес': 'sum', 'Отказался КД': 'sum', 'Отказали мы': 'sum', 
            'Молчуны': 'sum', 'Отказы всего': 'sum'
        }).reset_index()

        # Расчет конверсий
        summary['Собес/отклик %'] = (summary['Собес'] / summary['Отклики']).fillna(0)
        summary['Молчуны/Диалог %'] = (summary['Молчуны'] / summary['Начали диалог']).fillna(0)
        summary['Отказы/Диалог %'] = (summary['Отказы всего'] / summary['Начали диалог']).fillna(0)

        # Итоговая строка
        total = summary.sum(numeric_only=True)
        total[groupby_col] = 'ИТОГО'
        
        # Пересчет % для ИТОГО
        t_resp = total['Отклики'] if total['Отклики'] > 0 else 1
        t_dial = total['Начали диалог'] if total['Начали диалог'] > 0 else 1
        total['Собес/отклик %'] = total['Собес'] / t_resp
        total['Молчуны/Диалог %'] = total['Молчуны'] / t_dial
        total['Отказы/Диалог %'] = total['Отказы всего'] / t_dial
        
        return pd.concat([summary, pd.DataFrame([total])], ignore_index=True)

    # Листы
    df_date = create_summary('Дата')
    df_rec = create_summary('Рекрутер')
    df_city = create_summary('Город')
    df_vac = create_summary('Вакансия')

    # 5. СОХРАНЕНИЕ И ФОРМАТИРОВАНИЕ
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df_date.to_excel(writer, index=False, sheet_name='Свод по датам')
        df_rec.to_excel(writer, index=False, sheet_name='Свод по рекрутерам')
        df_city.to_excel(writer, index=False, sheet_name='Свод по городам')
        df_vac.to_excel(writer, index=False, sheet_name='Свод по вакансиям')
        df_base.to_excel(writer, index=False, sheet_name='Общий отчет')

        workbook = writer.book
        # Стили из вашего ТЗ
        header_fmt = workbook.add_format({'bold': True, 'bg_color': '#D9EAD3', 'border': 1, 'align': 'center'})
        total_fmt = workbook.add_format({'bold': True, 'bg_color': '#F4CCCC', 'border': 1, 'align': 'center'})
        perc_fmt = workbook.add_format({'num_format': '0%', 'border': 1, 'align': 'center'})
        num_fmt = workbook.add_format({'border': 1, 'align': 'center'})

        for sheet_name in writer.sheets:
            ws = writer.sheets[sheet_name]
            ws.freeze_panes(1, 0)
            
            # Определяем какой DF на листе
            if sheet_name == 'Свод по датам': curr_df = df_date
            elif sheet_name == 'Свод по рекрутерам': curr_df = df_rec
            elif sheet_name == 'Свод по городам': curr_df = df_city
            elif sheet_name == 'Свод по вакансиям': curr_df = df_vac
            else: curr_df = df_base

            for i, col in enumerate(curr_df.columns):
                # Авто-ширина
                column_len = max(curr_df[col].astype(str).str.len().max(), len(str(col))) + 3
                
                # Применяем форматы
                if '%' in str(col):
                    ws.set_column(i, i, 18, perc_fmt)
                else:
                    ws.set_column(i, i, column_len, num_fmt)
                
                # Формат шапки
                ws.write(0, i, col, header_fmt)

            # Формат строки ИТОГО (последняя строка)
            if sheet_name != 'Общий отчет':
                last_row = len(curr_df) - 1
                for i in range(len(curr_df.columns)):
                    val = curr_df.iloc[last_row, i]
                    fmt = total_fmt if '%' not in curr_df.columns[i] else workbook.add_format({'bold': True, 'bg_color': '#F4CCCC', 'border': 1, 'num_format': '0%', 'align': 'center'})
                    ws.write(last_row + 1, i, val, fmt)

    output.seek(0)
    file_name = f"Standard_Report_{start_date}_to_{end_date}.xlsx"
    await message.answer_document(
        BufferedInputFile(output.read(), filename=file_name),
        caption=f"📈 Аналитика откликов ({start_date} - {end_date})"
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



