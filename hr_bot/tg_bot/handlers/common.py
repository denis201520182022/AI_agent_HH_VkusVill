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
            "Вступили в follow up": has_reply,
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
            'Вступили в follow up': 'sum',
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
            'Доходимость': 'Доходимость (пришли от назначено)'
        })

        # Финальный порядок столбцов
        final_cols = [
            groupby_col, 'Назначено', 'Инициировано follow up', 'Вступили в follow up', 
            'Пришло', 'Взяли (% от пришло)', 'Отказали (% от пришло)', 
            'Сами отказались (% от пришло)', 'Не пришло', 'Доходимость (пришли от назначено)'
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
        total['Доходимость (пришли от назначено)'] = total['Пришло'] / t_naz

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


@router.callback_query(F.data == "stats_silent")
async def stats_silent_handler(callback: CallbackQuery):
    await callback.answer("Раздел 'Молчуны' в разработке", show_alert=True)