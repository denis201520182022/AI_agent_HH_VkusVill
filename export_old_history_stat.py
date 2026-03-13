import os
import pandas as pd
from datetime import date, timedelta
import calendar
from sqlalchemy import cast, Date
from hr_bot.db.models import Dialogue, SyncSessionLocal
from sqlalchemy import cast, Date
from sqlalchemy.orm import Session  # <--- ДОБАВИТЬ ЭТУ СТРОКУ

def generate_regular_report(db: Session, start_date: date, end_date: date, filepath: str):
    """Генерация обычной статистики с сохранением в файл"""
    print(f"  [Обычная] Сбор данных с {start_date} по {end_date}...")
    
    query = db.query(Dialogue).filter(
        cast(Dialogue.created_at, Date) >= start_date,
        cast(Dialogue.created_at, Date) <= end_date
    )
    dialogues = query.all()

    if not dialogues:
        print("  -> Нет данных за этот период. Пропуск.")
        return

    report_map = {}
    for d in dialogues:
        dt = d.created_at.strftime("%d.%m.%Y")
        rec = d.recruiter.name if d.recruiter else "Не указан"
        cit = d.vacancy.city if d.vacancy else "Не указан"
        vac = d.vacancy.title if d.vacancy else "Не указана"
        key = (dt, rec, cit, vac)

        if key not in report_map:
            report_map[key] = {
                "отклики_всего": 0, "начали_диалог_счетчик": 0, "собес_счетчик": 0,
                "отказался_кд_счетчик": 0, "отказали_мы_счетчик": 0, "молчуны_в_очереди_счетчик": 0
            }

        m = report_map[key]
        m["отклики_всего"] += 1
        
        history = d.history or []
        user_started = any(
            isinstance(h, dict) and h.get('role') == 'user' and not h.get('content', '').startswith("[SYSTEM COMMAND]")
            for h in history
        )
        
        if user_started: m["начали_диалог_счетчик"] += 1
        if d.status == 'qualified': m["собес_счетчик"] += 1
        if d.status == 'rejected' and d.dialogue_state == 'declined_vacancy': m["отказался_кд_счетчик"] += 1
        if d.dialogue_state == 'qualification_failed': m["отказали_мы_счетчик"] += 1
        if d.inactive_alerts and d.status not in ['qualified', 'rejected'] and user_started:
            m["молчуны_в_очереди_счетчик"] += 1

    rows = []
    for (dt, rec, cit, vac), m in report_map.items():
        отклики = m["отклики_всего"]
        начали_диалог = m["начали_диалог_счетчик"]
        не_вступили = отклики - начали_диалог
        собес = m["собес_счетчик"]
        отказался_кд = m["отказался_кд_счетчик"]
        отказали_мы = m["отказали_мы_счетчик"]
        отказы_всего = отказался_кд + отказали_мы
        молчуны = m["молчуны_в_очереди_счетчик"]

        rows.append({
            "Дата": dt, "Рекрутер": rec, "Город": cit, "Вакансия": vac,
            "Отклики": отклики, "Не вступили": не_вступили, "Начали диалог": начали_диалог, 
            "Собес": собес, "Отказался КД": отказался_кд, "Отказали мы": отказали_мы, 
            "Молчуны": молчуны, "Отказы всего": отказы_всего
        })

    df_base = pd.DataFrame(rows)
    df_base['dt_obj'] = pd.to_datetime(df_base['Дата'], format='%d.%m.%Y')
    df_base = df_base.sort_values(['dt_obj', 'Рекрутер']).drop(columns=['dt_obj'])

    def create_summary_df(groupby_col):
        s = df_base.groupby(groupby_col).agg({
            'Отклики': 'sum', 'Не вступили': 'sum', 'Начали диалог': 'sum', 'Собес': 'sum',
            'Отказался КД': 'sum', 'Отказали мы': 'sum', 'Молчуны': 'sum', 'Отказы всего': 'sum'
        }).reset_index()

        s['Собес/отклик %'] = (s['Собес'] / s['Отклики']).fillna(0)
        s['Молчуны/Диалог %'] = (s['Молчуны'] / s['Начали диалог']).fillna(0)
        s['Отказы/Диалог %'] = (s['Отказы всего'] / s['Начали диалог']).fillna(0)

        total = s.sum(numeric_only=True)
        total[groupby_col] = 'ИТОГО'
        
        t_resp = total['Отклики'] if total['Отклики'] > 0 else 1
        t_dial = total['Начали диалог'] if total['Начали диалог'] > 0 else 1
        total['Собес/отклик %'] = total['Собес'] / t_resp
        total['Молчуны/Диалог %'] = total['Молчуны'] / t_dial
        total['Отказы/Диалог %'] = total['Отказы всего'] / t_dial
        
        return pd.concat([s, pd.DataFrame([total])], ignore_index=True)

    # СОЗДАЕМ ЛИСТЫ ОДИН РАЗ КАК В БОТЕ
    df_date = create_summary_df('Дата')
    # Сортировка по дате
    mask = df_date['Дата'] != 'ИТОГО'
    df_temp = df_date[mask].copy()
    df_temp['date_obj'] = pd.to_datetime(df_temp['Дата'], format='%d.%m.%Y')
    df_temp = df_temp.sort_values('date_obj').drop(columns=['date_obj'])
    df_date = pd.concat([df_temp, df_date[~mask]], ignore_index=True)
    
    df_rec = create_summary_df('Рекрутер')
    df_city = create_summary_df('Город')
    df_vac = create_summary_df('Вакансия')

    with pd.ExcelWriter(filepath, engine='xlsxwriter') as writer:
        df_date.to_excel(writer, index=False, sheet_name='Свод по датам')
        df_rec.to_excel(writer, index=False, sheet_name='Свод по рекрутерам')
        df_city.to_excel(writer, index=False, sheet_name='Свод по городам')
        df_vac.to_excel(writer, index=False, sheet_name='Свод по вакансиям')
        df_base.to_excel(writer, index=False, sheet_name='Общий отчет')

        workbook = writer.book
        header_fmt = workbook.add_format({'bold': True, 'bg_color': '#D9EAD3', 'border': 1})
        perc_fmt = workbook.add_format({'num_format': '0%', 'border': 1})
        num_fmt = workbook.add_format({'border': 1})
        total_fmt = workbook.add_format({'bold': True, 'bg_color': '#F4CCCC', 'border': 1})

        for sheet_name in writer.sheets:
            ws = writer.sheets[sheet_name]
            ws.set_column('A:Z', 15, num_fmt)
            
            # Подбираем правильный df для листа
            if sheet_name == 'Свод по датам': current_df = df_date
            elif sheet_name == 'Свод по рекрутерам': current_df = df_rec
            elif sheet_name == 'Свод по городам': current_df = df_city
            elif sheet_name == 'Свод по вакансиям': current_df = df_vac
            else: current_df = df_base

            for i, col in enumerate(current_df.columns):
                if '%' in col: 
                    ws.set_column(i, i, 18, perc_fmt)
                    
    print(f"  -> Сохранено: {filepath}")


def generate_followup_report(db: Session, start_date: date, end_date: date, filepath: str):
    """Генерация статистики Follow-up с сохранением в файл"""
    print(f"  [Follow-up] Сбор данных с {start_date} по {end_date}...")
    
    query = db.query(Dialogue).filter(
        cast(Dialogue.created_at, Date) >= start_date,
        cast(Dialogue.created_at, Date) <= end_date,
        Dialogue.interview_datetime_utc.is_not(None)
    )
    dialogues = query.all()

    if not dialogues:
        print("  -> Нет данных по Follow-up за этот период. Пропуск.")
        return

    rows = []
    for d in dialogues:
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
            "Не пришло": 1 if d.no_show_reason else 0,
            "Причина неявки": d.no_show_reason or "-"
        })

    df_base = pd.DataFrame(rows)

    def create_fu_summary(groupby_col):
        summary = df_base.groupby(groupby_col).agg({
            'Назначено': 'sum', 'Инициировано follow up': 'sum', 'Прошли follow up': 'sum',
            'Пришло': 'sum', 'Взяли_число': 'sum', 'Отказали_число': 'sum',
            'Сами отказались_число': 'sum', 'Не пришло': 'sum'
        }).reset_index()

        summary['Взяли'] = (summary['Взяли_число'] / summary['Пришло']).fillna(0)
        summary['Отказали'] = (summary['Отказали_число'] / summary['Пришло']).fillna(0)
        summary['Сами отказались'] = (summary['Сами отказались_число'] / summary['Пришло']).fillna(0)
        summary['Доходимость'] = (summary['Пришло'] / summary['Назначено']).fillna(0)

        summary = summary.rename(columns={
            'Взяли': 'Взяли (% от пришло)', 'Отказали': 'Отказали (% от пришло)',
            'Сами отказались': 'Сами отказались (% от пришло)', 'Доходимость': 'Доходимость % (пришли от назначено)'
        })

        final_cols = [
            groupby_col, 'Назначено', 'Инициировано follow up', 'Прошли follow up', 
            'Пришло', 'Взяли (% от пришло)', 'Отказали (% от пришло)', 
            'Сами отказались (% от пришло)', 'Не пришло', 'Доходимость % (пришли от назначено)'
        ]
        res = summary[final_cols]

        total = res.sum(numeric_only=True)
        total[groupby_col] = 'ИТОГО'
        
        t_pri = total['Пришло'] if total['Пришло'] > 0 else 1
        t_naz = total['Назначено'] if total['Назначено'] > 0 else 1
        
        total['Взяли (% от пришло)'] = df_base['Взяли_число'].sum() / t_pri
        total['Отказали (% от пришло)'] = df_base['Отказали_число'].sum() / t_pri
        total['Сами отказались (% от пришло)'] = df_base['Сами отказались_число'].sum() / t_pri
        total['Доходимость % (пришли от назначено)'] = total['Пришло'] / t_naz

        return pd.concat([res, pd.DataFrame([total])], ignore_index=True)

    # СОЗДАЕМ ЛИСТЫ ОДИН РАЗ КАК В БОТЕ
    df_date = create_fu_summary('Дата')
    # Сортировка по дате
    mask = df_date['Дата'] != 'ИТОГО'
    df_temp = df_date[mask].copy()
    df_temp['date_obj'] = pd.to_datetime(df_temp['Дата'], format='%d.%m.%Y')
    df_temp = df_temp.sort_values('date_obj').drop(columns=['date_obj'])
    df_date = pd.concat([df_temp, df_date[~mask]], ignore_index=True)
    
    df_rec = create_fu_summary('Рекрутер')
    df_city = create_fu_summary('Город')
    df_vac = create_fu_summary('Вакансия')

    with pd.ExcelWriter(filepath, engine='xlsxwriter') as writer:
        df_date.to_excel(writer, index=False, sheet_name='Свод по датам')
        df_rec.to_excel(writer, index=False, sheet_name='Свод по рекрутерам')
        df_city.to_excel(writer, index=False, sheet_name='Свод по городам')
        df_vac.to_excel(writer, index=False, sheet_name='Свод по вакансиям')
        df_base.to_excel(writer, index=False, sheet_name='Сырые данные')

        workbook = writer.book
        perc_fmt = workbook.add_format({'num_format': '0%', 'border': 1, 'align': 'center'})
        num_fmt = workbook.add_format({'border': 1, 'align': 'center'})
        header_fmt = workbook.add_format({'bold': True, 'bg_color': '#CFE2F3', 'border': 1})

        for sheet_name in writer.sheets:
            ws = writer.sheets[sheet_name]
            
            # А) ЗАКРЕПЛЯЕМ ШАПКУ (как в боте)
            ws.freeze_panes(1, 0)
            
            # Б) АВТОМАТИЧЕСКАЯ ШИРИНА СТОЛБЦОВ (как в боте)
            if sheet_name == 'Свод по датам': current_df = df_date
            elif sheet_name == 'Свод по рекрутерам': current_df = df_rec
            elif sheet_name == 'Свод по городам': current_df = df_city
            elif sheet_name == 'Свод по вакансиям': current_df = df_vac
            else: current_df = df_base

            for i, col in enumerate(current_df.columns):
                column_len = max(
                    current_df[col].astype(str).str.len().max(), 
                    len(str(col))
                ) + 3
                
                ws.set_column(i, i, column_len, num_fmt if '%' not in col else perc_fmt)
                    
    print(f"  -> Сохранено: {filepath}")


def get_month_ranges(year, month):
    """Возвращает первый и последний день месяца"""
    start_date = date(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    end_date = date(year, month, last_day)
    return start_date, end_date

def main():
    # Создаем папку для отчетов
    output_dir = "historical_reports"
    os.makedirs(output_dir, exist_ok=True)

    # Задаем периоды для отчетов
    all_time_start = date(2023, 1, 1) # Можно поменять на год старта проекта
    all_time_end = date.today()
    
    ranges = [
        ("За_все_время", all_time_start, all_time_end),
        ("Март_2026", *get_month_ranges(2026, 3)),
        ("Февраль_2026", *get_month_ranges(2026, 2)),
        ("Январь_2026", *get_month_ranges(2026, 1)),
        ("Декабрь_2025", *get_month_ranges(2025, 12)),
        ("Ноябрь_2025", *get_month_ranges(2025, 11)),
        ("Октябрь_2025", *get_month_ranges(2025, 10)),
    ]

    print(f"🚀 Запуск генерации исторических отчетов в папку '{output_dir}/'...\n")

    # Открываем синхронную сессию БД
    db = SyncSessionLocal()
    
    try:
        for period_name, start_d, end_d in ranges:
            print(f"--- Генерация для периода: {period_name.replace('_', ' ')} ---")
            
            # Файлы для обычной статы
            reg_filepath = os.path.join(output_dir, f"Regular_{period_name}.xlsx")
            generate_regular_report(db, start_d, end_d, reg_filepath)
            
            # Файлы для Follow-up
            fu_filepath = os.path.join(output_dir, f"FollowUp_{period_name}.xlsx")
            generate_followup_report(db, start_d, end_d, fu_filepath)
            print()

        print("✅ Все отчеты успешно сформированы!")
        
    except Exception as e:
        print(f"❌ Произошла ошибка при выгрузке: {e}")
        
    finally:
        # Обязательно закрываем сессию
        db.close()

if __name__ == "__main__":
    main()