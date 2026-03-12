import os
import io
import pandas as pd
import logging
from datetime import date
from sqlalchemy import cast, Date
# ВАЖНО: используем SyncSessionLocal вместо SessionLocal
from hr_bot.db.models import SyncSessionLocal, Dialogue, Vacancy, TrackedRecruiter
from dotenv import load_dotenv

# Настройка логов
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def run_manual_report():
    # Загружаем переменные окружения ДО открытия сессии
    load_dotenv()
    
    # 1. УСТАНОВКА ДАТ (Февраль 2026)
    start_date = date(2026, 2, 1)
    end_date = date(2026, 2, 28)
    
    logger.info(f"🚀 Запуск генерации отчета за период: {start_date} - {end_date}")
    
    # Используем СИНХРОННУЮ сессию, так как она поддерживает метод .query()
    db = SyncSessionLocal()
    try:
        # --- ОРИГИНАЛЬНАЯ ЛОГИКА СБОРА ДАННЫХ (БЕЗ ИЗМЕНЕНИЙ) ---
        query = db.query(Dialogue).filter(
            cast(Dialogue.created_at, Date) >= start_date,
            cast(Dialogue.created_at, Date) <= end_date
        )
        dialogues = query.all()

        if not dialogues:
            logger.warning("🤷 За этот период откликов не найдено.")
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
                    "отклики_всего": 0,
                    "начали_диалог_счетчик": 0,
                    "собес_счетчик": 0,
                    "отказался_кд_счетчик": 0,
                    "отказали_мы_счетчик": 0,
                    "молчуны_в_очереди_счетчик": 0
                }

            m = report_map[key]
            m["отклики_всего"] += 1
            
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

            if d.status == 'qualified':
                m["собес_счетчик"] += 1
            if d.status == 'rejected' and d.dialogue_state == 'declined_vacancy':
                m["отказался_кд_счетчик"] += 1
            if d.dialogue_state == 'qualification_failed':
                m["отказали_мы_счетчик"] += 1
            if d.inactive_alerts and d.status not in ['qualified', 'rejected'] and user_started:
                m["молчуны_в_очереди_счетчик"] += 1

        # --- ОРИГИНАЛЬНАЯ ЛОГИКА ФОРМИРОВАНИЯ СТРОК (БЕЗ ИЗМЕНЕНИЙ) ---
        rows = []
        for (dt, rec, cit, vac), m in report_map.items():
            отклики = m["отклики_всего"]
            начали_диалог = m["начали_диалог_счетчик"]
            rows.append({
                "Дата": dt, 
                "Рекрутер": rec, 
                "Город": cit, 
                "Вакансия": vac,
                "Отклики": отклики, 
                "Не вступили": отклики - начали_диалог,
                "Начали диалог": начали_диалог, 
                "Собес": m["собес_счетчик"],
                "Отказался КД": m["отказался_кд_счетчик"], 
                "Отказали мы": m["отказали_мы_счетчик"], 
                "Молчуны": m["молчуны_в_очереди_счетчик"],
                "Отказы всего": m["отказался_кд_счетчик"] + m["отказали_мы_счетчик"]
            })

        df_base = pd.DataFrame(rows)
        df_base['dt_obj'] = pd.to_datetime(df_base['Дата'], format='%d.%m.%Y')
        df_base = df_base.sort_values(['dt_obj', 'Рекрутер']).drop(columns=['dt_obj'])

        # --- ОРИГИНАЛЬНАЯ ЛОГИКА СВОДНЫХ ТАБЛИЦ (БЕЗ ИЗМЕНЕНИЙ) ---
        def create_summary_df(groupby_col):
            summary = df_base.groupby(groupby_col).agg({
                'Отклики': 'sum', 'Не вступили': 'sum', 'Начали диалог': 'sum', 'Собес': 'sum',
                'Отказался КД': 'sum', 'Отказали мы': 'sum', 'Молчуны': 'sum', 'Отказы всего': 'sum'
            }).reset_index()
            s = summary
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

        df_date = create_summary_df('Дата')
        df_rec = create_summary_df('Рекрутер')
        df_city = create_summary_df('Город')
        df_vac = create_summary_df('Вакансия')

        # --- ОРИГИНАЛЬНАЯ ЛОГИКА СОХРАНЕНИЯ В EXCEL (БЕЗ ИЗМЕНЕНИЙ) ---
        file_name = f"Final_Report_Feb_2026.xlsx"
        
        with pd.ExcelWriter(file_name, engine='xlsxwriter') as writer:
            df_date.to_excel(writer, index=False, sheet_name='Свод по датам')
            df_rec.to_excel(writer, index=False, sheet_name='Свод по рекрутерам')
            df_city.to_excel(writer, index=False, sheet_name='Свод по городам')
            df_vac.to_excel(writer, index=False, sheet_name='Свод по вакансиям')
            df_base.to_excel(writer, index=False, sheet_name='Общий отчет')

            workbook = writer.book
            num_fmt = workbook.add_format({'border': 1})
            perc_fmt = workbook.add_format({'num_format': '0%', 'border': 1})

            for sheet_name in writer.sheets:
                ws = writer.sheets[sheet_name]
                ws.set_column('A:Z', 15, num_fmt)
                for i, col in enumerate(df_date.columns):
                    if '%' in col:
                        ws.set_column(i, i, 18, perc_fmt)

        logger.info(f"✅ Готово! Файл сохранен как: {file_name}")

    except Exception as e:
        logger.error(f"❌ Ошибка: {e}", exc_info=True)
    finally:
        db.close()

if __name__ == "__main__":
    run_manual_report()