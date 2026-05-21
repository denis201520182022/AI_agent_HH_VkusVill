        # =====================================================================
        # [START] УНИВЕРСАЛЬНАЯ ВАЛИДАЦИЯ ДАТЫ И ВРЕМЕНИ (РЕФАКТОРИНГ V2)
        # =====================================================================
        DATE_CRITICAL_STATES = [
            'init_scheduling_spb', 'scheduling_spb_day', 'scheduling_spb_time', 'interview_scheduled_spb',
            'init_scheduling_sbor', 'scheduling_sbor_day', 'scheduling_sbor_time', 'interview_scheduled_sbor'
        ]
        
        if new_state in DATE_CRITICAL_STATES:
            interview_date = extracted_data.get("interview_date")
            interview_time = extracted_data.get("interview_time")

            TIME_KEYWORDS = [
                "сегодня", "завтра", "послезавтра", "понедельник", "вторник", "сред", "четверг", 
                "пятниц", "суббот", "воскресен", "январ", "феврал", "март", "апрел", "май", "июн", 
                "июл", "август", "сентябр", "октябр", "ноябр", "декабр", "вчера", "число", "время", "числа",
                "01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12", "13", "14", "15", 
                "16", "17", "18", "19", "20", "21", "22", "23", "24", "25", "26", "27", "28", "29", "30", "31",
                "первого", "второго", "третьего", "четвертого", "пятого", "шестого", "седьмого", "восьмого", "девятого", "десятого",
                "одиннадцатого", "двенадцатого", "тринадцатого", "четырнадцатого", "пятнадцатого", "шестнадцатого", "семнадцатого", 
                "восемнадцатого", "девятнадцатого", "двадцатого", "тридцатого"
            ]
            bot_text_low = (bot_response_text or "").lower()
            user_text_low = combined_masked_message.lower()
            has_time_keywords = any(kw in bot_text_low or kw in user_text_low for kw in TIME_KEYWORDS)

            if interview_date or has_time_keywords:
                # --- 1. ПРОВЕРКА ИСПОЛНЕНИЯ КОМАНД (is_obeyed) ---
                is_obeyed = False
                last_msg = dialogue.history[-1] if dialogue.history else {}
                last_content = str(last_msg.get("content", ""))

                if last_msg.get("role") == "user" and "[SYSTEM COMMAND]" in last_content:
                    import re
                    # Ищем: ОБЯЗАТЕЛЬНО установи 'interview_date' в JSON на 'YYYY-MM-DD'
                    target_date_match = re.search(r"interview_date' в JSON на '(\d{4}-\d{2}-\d{2})'", last_content)
                    target_null = "interview_date' в JSON на null" in last_content

                    if target_date_match and interview_date == target_date_match.group(1):
                        is_obeyed = True
                        ctx_logger.info(f"[{dialogue.hh_response_id}] ✅ Бот исполнил: установил дату {interview_date}")
                    elif target_null and (interview_date is None or interview_date == ""):
                        is_obeyed = True
                        ctx_logger.info(f"[{dialogue.hh_response_id}] ✅ Бот исполнил: занулил дату")

                # --- 2. ОБЪЕДИНЕННЫЙ ЦЕНТР: АУДИТ + СЛОТЫ ---
                if not is_obeyed:
                    already_stored_date_str = None
                    if dialogue.interview_datetime_utc:
                        already_stored_date_str = dialogue.interview_datetime_utc.astimezone(SPB_TIMEZONE).strftime('%Y-%m-%d')
                    
                    # Умный пропуск аудита (экономим токены gpt-4o)
                    run_audit = True
                    verified_date = "none"
                    audit_reason = "Ожидание аудита"

                    if already_stored_date_str == interview_date and not has_time_keywords:
                        ctx_logger.info(f"[{dialogue.hh_response_id}] Дата совпадает с БД и нет новых триггеров. Пропуск аудита.")
                        run_audit = False
                        verified_date = interview_date
                        audit_reason = "Совпадение с БД"
                    
                    if run_audit:
                        ctx_logger.info(f"[{dialogue.hh_response_id}] Запуск аудитора (State: {new_state}): {interview_date}")
                        cal_context = _generate_calendar_context_2()
                        # Очищаем историю от системных команд, чтобы не путать аудитора
                        clean_hist_for_audit = [m for m in (dialogue.history or []) if not str(m.get("content", "")).startswith("[SYSTEM")]
                        full_hist_for_audit = clean_hist_for_audit + user_entries_to_history
                        
                        verified_date_res = await _verify_date_audit(db, dialogue, recruiter, pending_messages, interview_date, full_hist_for_audit, cal_context, log_context)
                        if verified_date_res[0] is None: return 
                        verified_date, audit_reason = verified_date_res
                        ctx_logger.info(f"[{dialogue.hh_response_id}] Результат аудита: {verified_date} | Причина: {audit_reason}")

                    if verified_date and verified_date != "none":
                        # Получаем РЕАЛЬНЫЕ слоты (Бизнес-логика VkusVill)
                        now_msk = datetime.datetime.now(SPB_TIMEZONE)
                        today_str = now_msk.strftime('%Y-%m-%d')
                        v_date_obj = datetime.datetime.strptime(verified_date, '%Y-%m-%d')
                        weekday_idx = v_date_obj.weekday()
                        weekdays_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
                        v_weekday = weekdays_ru[weekday_idx]
                        holidays = [(2, 23), (5, 1), (5, 2), (5, 3), (5, 9), (5, 10)]
                        is_holiday = (v_date_obj.month, v_date_obj.day) in holidays

                        # Запрос слотов
                        is_warehouse = is_collector_vacancy(dialogue.vacancy.title)
                        slots = []
                        if is_warehouse:
                            target_address = dialogue.selected_address or (dialogue.vacancy.extracted_info or {}).get('full_address')
                            target_city = dialogue.vacancy.city or "Санкт-Петербург"
                            slots = await sbor_google_service.get_available_slots(verified_date, target_city, target_address)
                        else:
                            if not (weekday_idx == 6 or is_holiday):
                                if weekday_idx == 5: slots = ["14:00"]
                                else:
                                    current_city_low = (dialogue.vacancy.city or "").lower()
                                    is_spb = any(city in current_city_low for city in ['санкт-петербург', 'мурино', 'кудрово'])
                                    slots = ["10:00", "12:00", "15:00", "16:00"] if is_spb else ["10:00", "12:00", "16:00"]
                        
                        if verified_date == today_str:
                            slots = [s for s in slots if int(s.split(':')[0]) > now_msk.hour]

                        # Формируем вердикт
                        is_day_off = (weekday_idx == 6 or is_holiday)
                        is_full = not slots and not is_day_off
                        is_hallucination = (verified_date != interview_date)
                        
                        cmd_content = None
                        if is_day_off:
                            reason = "воскресенье" if weekday_idx == 6 else "праздничный день"
                            cmd_content = (
                                f"[SYSTEM COMMAND] Внимание!!! {verified_date} ({v_weekday}) — это {reason}, когда собеседования НЕ проводятся. "
                                f"1. ОБЯЗАТЕЛЬНО установи 'interview_date' в JSON на null. "
                                f"2. Вежливо объясни это кандидату и предложи выбрать другой день."
                            )
                        elif is_full:
                            cmd_content = (
                                f"[SYSTEM COMMAND] Внимание!!! На {verified_date} ({v_weekday}) нет доступных слотов для записи. "
                                f"Предложи кандидату выбрать другой день. ОБЯЗАТЕЛЬНО установи 'interview_date' в JSON на null."
                            )
                        elif is_hallucination:
                            slots_str = ", ".join(slots) if slots else "другой день"
                            cmd_content = (
                                f"[SYSTEM COMMAND] Ты ошиблась: пользователь выбрал {v_weekday} ({verified_date}). "
                                f"На этот день доступны слоты: {slots_str}. "
                                f"Подтверди ПРАВИЛЬНУЮ дату и ОБЯЗАТЕЛЬНО установи 'interview_date' в JSON на '{verified_date}'."
                            )
                            await send_date_conflict_alert(dialogue.id, dialogue.hh_response_id, user_text_low, interview_date, f"{verified_date} ({v_weekday})", "History", audit_reason)
                        
                        # Если всё ВЕРНО, но бот еще не знает время (Даем подсказку, как в zz.md)
                        elif not interview_time and slots:
                            slots_str = ", ".join(slots)
                            cmd_content = (
                                f"[SYSTEM COMMAND] Внимание!!! {verified_date} это {v_weekday}!!! "
                                f"В этот день строго разрешены только следующие слоты: {slots_str}. "
                                f"Ты ОБЯЗАНА перечислить ВСЕ эти варианты ({slots_str}) в своем ответе, чтобы кандидат мог выбрать."
                            )

                        if cmd_content:
                            # Антидубль (проверка последних 3-х сообщений)
                            history_to_check = [str(m.get('content', "")) for m in (dialogue.history or [])[-3:]]
                            if cmd_content not in history_to_check:
                                sys_cmd = {'message_id': f'sys_date_res_{time.time()}', 'role': 'user', 'content': cmd_content}
                                dialogue.history = (dialogue.history or []) + user_entries_to_history + [sys_cmd]
                                dialogue.pending_messages = [sys_cmd] # Форсируем ретрай
                                await db.commit()
                                ctx_logger.info(f"[{dialogue.hh_response_id}] Отправлено на исправление/хинт: {verified_date}")
                                return 
                            else:
                                ctx_logger.warning(f"[{dialogue.hh_response_id}] Команда уже в истории, бот её игнорирует. Пропуск во избежание цикла.")

                # --- 3. ЖЕСТКАЯ ВАЛИДАЦИЯ ВРЕМЕНИ (Если дата уже согласована) ---
                if (is_obeyed or not run_audit) and interview_date and interview_time:
                    # Бизнес-логика получения свежих слотов для финальной проверки
                    is_warehouse = is_collector_vacancy(dialogue.vacancy.title)
                    now_msk = datetime.datetime.now(SPB_TIMEZONE)
                    allowed_slots = []
                    if is_warehouse:
                        allowed_slots = await sbor_google_service.get_available_slots(interview_date, dialogue.vacancy.city or "Санкт-Петербург", dialogue.selected_address or (dialogue.vacancy.extracted_info or {}).get('full_address'))
                    else:
                        v_date_obj = datetime.datetime.strptime(interview_date, '%Y-%m-%d')
                        holidays = [(2, 23), (5, 1), (5, 2), (5, 3), (5, 9), (5, 10)]
                        if not (v_date_obj.weekday() == 6 or (v_date_obj.month, v_date_obj.day) in holidays):
                            if v_date_obj.weekday() == 5: allowed_slots = ["14:00"]
                            else:
                                is_spb = any(c in (dialogue.vacancy.city or "").lower() for c in ['санкт-петербург', 'мурино', 'кудрово'])
                                allowed_slots = ["10:00", "12:00", "15:00", "16:00"] if is_spb else ["10:00", "12:00", "16:00"]
                    
                    if interview_date == now_msk.strftime('%Y-%m-%d'):
                        allowed_slots = [s for s in allowed_slots if int(s.split(':')[0]) > now_msk.hour]

                    if interview_time.strip() not in allowed_slots:
                        msg = f"На {interview_date} доступно время: {', '.join(allowed_slots) if allowed_slots else 'другой день'}. Слот {interview_time} недоступен."
                        time_cmd = {
                            'message_id': f'sys_time_fix_{time.time()}', 'role': 'user',
                            'content': f"[SYSTEM COMMAND] {msg} Предложи выбрать из доступных. ОБЯЗАТЕЛЬНО установи 'interview_time' в JSON на null."
                        }
                        # Антидубль для времени
                        if time_cmd['content'] not in [str(m.get('content', "")) for m in (dialogue.history or [])[-2:]]:
                            dialogue.history = (dialogue.history or []) + user_entries_to_history + [time_cmd]
                            dialogue.pending_messages = [time_cmd]
                            await db.commit()
                            return 

        # =====================================================================
        # [END] УНИВЕРСАЛЬНАЯ ВАЛИДАЦИЯ ДАТЫ И ВРЕМЕНИ
        # =====================================================================