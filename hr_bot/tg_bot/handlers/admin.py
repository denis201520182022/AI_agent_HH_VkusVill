import logging
import datetime
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, ReplyKeyboardRemove
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.orm import Session
import io
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import BufferedInputFile
from sqlalchemy import select
from hr_bot.db.models import Dialogue
from aiogram.utils.formatting import Text, Bold, Italic, Code
from hr_bot.db.models import TelegramUser, TrackedRecruiter, AppSettings
from hr_bot.tg_bot.filters import AdminFilter
from hr_bot.tg_bot.keyboards import (
    create_management_keyboard,
    role_choice_keyboard,
    cancel_fsm_keyboard,
    limits_menu_keyboard,
    limit_options_keyboard,
    admin_keyboard
)

logger = logging.getLogger(__name__)
router = Router()
router.message.filter(AdminFilter())

class UserManagement(StatesGroup):
    add_id = State(); add_name = State(); add_role = State(); del_id = State()

class RecruiterManagement(StatesGroup):
    add_id = State()
    add_name = State()
    add_refresh_token = State()
    add_access_token = State()
    add_expires_in = State()
    add_chat_id = State()      
    add_topic_qualified = State() 
    add_topic_rejected = State() 
    add_topic_declined_interview = State()
    add_topic_timeout = State()  
    add_topic_rescheduled = State()
    
    del_id = State()

    # Состояния для обновления
    update_id = State()
    update_refresh_token = State()
    update_access_token = State()
    update_expires_in = State()
    update_chat_id = State()       
    update_topic_qualified = State() 
    update_topic_rejected = State()  
    update_topic_declined_interview = State() 
    update_topic_timeout = State()
    update_topic_rescheduled = State()   

class SettingsManagement(StatesGroup):
    # Для баланса
    choosing_balance = State() # Выбор какой баланс пополнить
    set_balance_value = State() # Ввод суммы
    
    # Для тарифов
    set_cost_dialogue = State()
    set_cost_reactivation = State() 
    set_cost_followup = State()
     
# --- Обработчики отмены ---
@router.message(Command("cancel"))
async def cancel_command_handler(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Нет активных действий для отмены.")
        return
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=admin_keyboard)

@router.callback_query(F.data == "cancel_fsm")
async def cancel_callback_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Действие отменено.")
    await callback.answer()

# --- УПРАВЛЕНИЕ ЛИМИТАМИ И ТАРИФАМИ ---
@router.message(F.text == "⚙️ Баланс и Тариф")
async def limits_menu(message: Message, db_session: Session):
    settings = db_session.query(AppSettings).filter_by(id=1).first()
    if not settings:
        await message.answer("❌ Не удалось загрузить настройки.")
        return

    content = Text(
        Bold("📊 Управление балансом:"), "\n",
        "- Баланс новых откликов: ", Bold(f"{settings.balance_standard:.2f}"), " руб.\n",
        "- Баланс рассылок и фоллоуапов: ", Bold(f"{settings.balance_outbound:.2f}"), " руб.\n\n",
        
        "💰 ", Bold("Тарифы:"), "\n",
        "- Новый отклик: ", Bold(f"{settings.cost_per_dialogue:.2f}"), " руб.\n",
        "- Follow-up: ", Bold(f"{settings.cost_per_followup:.2f}"), " руб.\n",
        "- Реактивация (рассылка): ", Bold(f"{settings.cost_per_reactivation:.2f}"), " руб.\n\n",
        
        "🔔 Уведомление при балансе < ", Bold(f"{settings.low_balance_threshold:.2f}"), " руб.\n\n",

        Bold("📈 История затрат (всего):"), "\n",
        "- На новые диалоги: ", Bold(f"{settings.total_spent_standard:.2f}"), " руб.\n",
        "- На Follow-up: ", Bold(f"{settings.total_spent_followup:.2f}"), " руб.\n",
        "- На Реактивацию: ", Bold(f"{settings.total_spent_reactivation:.2f}"), " руб."
    )
    
    await message.answer(**content.as_kwargs(), reply_markup=limits_menu_keyboard)

# --- ЛОГИКА ПОПОЛНЕНИЯ БАЛАНСА (ДВА ВАРИАНТА) ---
@router.callback_query(F.data == "set_limit")
async def start_set_balance_choice(callback: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardBuilder()
    kb.button(text="🔵 Новые диалоги (Отклики)", callback_data="target_balance_standard")
    kb.button(text="🟠 Фолоуап, реактивация", callback_data="target_balance_outbound")
    kb.adjust(1)
    
    await callback.message.answer("Какой баланс вы хотите изменить?", reply_markup=kb.as_markup())
    await state.set_state(SettingsManagement.choosing_balance)
    await callback.answer()

@router.callback_query(SettingsManagement.choosing_balance, F.data.startswith("target_balance_"))
async def process_balance_choice(callback: CallbackQuery, state: FSMContext):
    target = callback.data.replace("target_balance_", "")
    await state.update_data(target_field=target)
    
    label = "Новые диалоги (Отклики)" if target == "standard" else "Фолоуап, реактивация"
    await callback.message.edit_text(f"Установка баланса для: <b>{label}</b>\nВведите новую сумму в рублях:", parse_mode="HTML")
    await state.set_state(SettingsManagement.set_balance_value)
    await callback.answer()

@router.message(SettingsManagement.set_balance_value)
async def process_set_balance_value(message: Message, state: FSMContext, db_session: Session):
    try:
        new_value = float(message.text.replace(',', '.'))
        if new_value < 0: raise ValueError
    except:
        await message.answer("❌ Введите корректное число.")
        return

    data = await state.get_data()
    target = data.get("target_field")
    settings = db_session.query(AppSettings).filter_by(id=1).first()

    if target == "standard":
        settings.balance_standard = new_value
        # Сбрасываем флаг уведомления, если пополнили
        if new_value >= settings.low_balance_threshold:
            settings.notified_low_standard = False
    else:
        settings.balance_outbound = new_value
        if new_value >= settings.low_balance_threshold:
            settings.notified_low_outbound = False

    db_session.commit()
    await state.clear()
    await message.answer(f"✅ Баланс <b>{target}</b> обновлен до {new_value:.2f} руб.", reply_markup=admin_keyboard, parse_mode="HTML")

# --- ЛОГИКА ТАРИФОВ (ОБНОВЛЕННАЯ ПОД МОДЕЛЬ) ---
@router.callback_query(F.data == "set_tariff")
async def start_set_tariffs(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SettingsManagement.set_cost_dialogue)
    await callback.message.answer("Шаг 1/3: Введите стоимость НОВОГО ОТКЛИКА (в рублях):")
    await callback.answer()

@router.message(SettingsManagement.set_cost_dialogue)
async def process_set_cost_dialogue(message: Message, state: FSMContext, db_session: Session):
    try:
        val = float(message.text.replace(',', '.'))
        settings = db_session.query(AppSettings).filter_by(id=1).first()
        settings.cost_per_dialogue = val
        db_session.commit()
        
        await state.set_state(SettingsManagement.set_cost_reactivation)
        await message.answer(f"✅ Ок. Шаг 2/3: Введите стоимость ОДНОГО СООБЩЕНИЯ РЕАКТИВАЦИИ (рассылка):")
    except:
        await message.answer("❌ Ошибка в числе.")

@router.message(SettingsManagement.set_cost_reactivation)
async def process_set_cost_reactivation(message: Message, state: FSMContext, db_session: Session):
    try:
        val = float(message.text.replace(',', '.'))
        settings = db_session.query(AppSettings).filter_by(id=1).first()
        settings.cost_per_reactivation = val 
        db_session.commit()
        
        await state.set_state(SettingsManagement.set_cost_followup)
        await message.answer("✅ Ок. Шаг 3/3: Введите стоимость FOLLOW-UP (дожим после встречи):")
    except:
        await message.answer("❌ Ошибка в числе.")

@router.message(SettingsManagement.set_cost_followup)
async def process_set_cost_followup(message: Message, state: FSMContext, db_session: Session):
    try:
        val = float(message.text.replace(',', '.'))
        settings = db_session.query(AppSettings).filter_by(id=1).first()
        settings.cost_per_followup = val
        db_session.commit()
        
        await state.clear()
        await message.answer("✅ Все тарифы успешно обновлены.", reply_markup=admin_keyboard)
    except:
        await message.answer("❌ Ошибка в числе.")

# --- 1. УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ ---
@router.message(F.text == "👤 Управление пользователями")
async def user_management_menu(message: Message, db_session: Session):
    users = db_session.query(TelegramUser).all()
    content_parts = [Bold("👥 Список пользователей:"), "\n\n"]
    if not users:
        content_parts.append(Italic("В системе пока нет пользователей."))
    else:
        for u in users:
            role_emoji = "✨" if u.role == 'admin' else "🧑‍💻"
            content_parts.extend([
                f"{role_emoji} ", Bold(u.username), " (ID: ", Code(u.telegram_id), ") - Роль: ", Italic(u.role), "\n"
            ])
    content_parts.append("\nВыберите действие:")
    content = Text(*content_parts)
    await message.answer(**content.as_kwargs(), reply_markup=create_management_keyboard([], "add_user", "del_user"))

@router.callback_query(F.data == "add_user")
async def start_add_user(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UserManagement.add_id)
    content = Text("Введите Telegram ID нового пользователя.")
    await callback.message.edit_text(**content.as_kwargs(), reply_markup=cancel_fsm_keyboard)
    await callback.answer()

@router.message(UserManagement.add_id)
async def process_add_user_id(message: Message, state: FSMContext, db_session: Session):
    if not message.text or not message.text.isdigit():
        content = Text("❌ ID должен быть числом. Попробуйте еще раз.")
        await message.answer(**content.as_kwargs(), reply_markup=cancel_fsm_keyboard)
        return
    user_id = message.text
    if db_session.query(TelegramUser).filter_by(telegram_id=user_id).first():
        content = Text("⚠️ Пользователь с ID ", Code(user_id), " уже существует. Действие отменено.")
        await message.answer(**content.as_kwargs())
        await state.clear()
        return
    await state.update_data(user_id=user_id)
    await state.set_state(UserManagement.add_name)
    content = Text("Отлично. Теперь введите имя пользователя (например, ", Code("Иван Рекрутер"), ").")
    await message.answer(**content.as_kwargs(), reply_markup=cancel_fsm_keyboard)

@router.message(UserManagement.add_name)
async def process_add_user_name(message: Message, state: FSMContext):
    if not message.text:
        content = Text("❌ Имя не может быть пустым. Попробуйте еще раз.")
        await message.answer(**content.as_kwargs(), reply_markup=cancel_fsm_keyboard)
        return
    await state.update_data(user_name=message.text)
    await state.set_state(UserManagement.add_role)
    await message.answer("Имя принято. Теперь выберите роль:", reply_markup=role_choice_keyboard)

@router.callback_query(UserManagement.add_role)
async def process_add_user_role(callback: CallbackQuery, state: FSMContext, db_session: Session):
    role = "admin" if callback.data == "set_role_admin" else "user"
    user_data = await state.get_data()
    new_user = TelegramUser(telegram_id=user_data['user_id'], username=user_data['user_name'], role=role)
    db_session.add(new_user)
    db_session.commit()
    await state.clear()
    logger.info(f"Админ {callback.from_user.id} добавил пользователя {user_data['user_id']} с ролью {role}")
    content = Text("✅ ", Bold("Успех!"), " Пользователь ", Bold(user_data['user_name']), " добавлен с ролью ", Italic(role), ".")
    await callback.message.edit_text(**content.as_kwargs())

@router.callback_query(F.data == "del_user")
async def start_del_user(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UserManagement.del_id)
    content = Text("Введите Telegram ID пользователя для удаления.")
    await callback.message.edit_text(**content.as_kwargs(), reply_markup=cancel_fsm_keyboard)
    await callback.answer()

@router.message(UserManagement.del_id)
async def process_del_user_id(message: Message, state: FSMContext, db_session: Session):
    if not message.text or not message.text.isdigit():
        content = Text("❌ ID должен быть числом. Попробуйте еще раз.")
        await message.answer(**content.as_kwargs(), reply_markup=cancel_fsm_keyboard)
        return
    user_id_to_delete = message.text
    if str(message.from_user.id) == user_id_to_delete:
        await message.answer("🤔 Вы не можете удалить самого себя. Действие отменено.")
        await state.clear()
        return
    user_to_delete = db_session.query(TelegramUser).filter_by(telegram_id=user_id_to_delete).first()
    if not user_to_delete:
        content = Text("⚠️ Пользователь с ID ", Code(user_id_to_delete), " не найден. Действие отменено.")
        await message.answer(**content.as_kwargs())
        await state.clear()
        return
    deleted_username = user_to_delete.username
    deleted_id = user_to_delete.telegram_id
    db_session.delete(user_to_delete)
    db_session.commit()
    await state.clear()
    logger.info(f"Админ {message.from_user.id} удалил пользователя {deleted_id}")
    content = Text("✅ Пользователь ", Bold(deleted_username), " (ID: ", Code(deleted_id), ") был удален.")
    await message.answer(**content.as_kwargs())


# --- 3. УПРАВЛЕНИЕ РЕКРУТЕРАМИ ---
@router.message(F.text == "👨‍💼 Управление рекрутерами")
async def recruiter_management_menu(message: Message, db_session: Session):
    # Показываем только тех, у кого активный статус
    recruiters = db_session.query(TrackedRecruiter).filter(TrackedRecruiter.is_auth_active == True).all()

    content_parts = [Bold("👨‍💼 Отслеживаемые рекрутеры:"), "\n\n"]
    if not recruiters:
        content_parts.append(Italic("Список пуст."))
    else:
        for r in recruiters:
            content_parts.extend(["- ", Bold(r.name), " (ID: ", Code(r.recruiter_id), ")\n"])
    content_parts.append("\nВыберите действие:")

    content = Text(*content_parts)
    await message.answer(
        **content.as_kwargs(),
        reply_markup=create_management_keyboard([], "add_recruiter", "update_recruiter", "del_recruiter")
    )

@router.callback_query(F.data == "add_recruiter")
async def start_add_recruiter(callback: CallbackQuery, state: FSMContext):
    await state.set_state(RecruiterManagement.add_id)
    content = Text("Шаг 1/9: Введите ID рекрутера (manager id) с hh.ru.")
    await callback.message.edit_text(**content.as_kwargs(), reply_markup=cancel_fsm_keyboard)
    await callback.answer()

@router.message(RecruiterManagement.add_id)
async def process_add_recruiter_id(message: Message, state: FSMContext, db_session: Session):
    if not message.text or not message.text.isdigit():
        content = Text("❌ ID должен быть числом. Попробуйте еще раз.")
        await message.answer(**content.as_kwargs(), reply_markup=cancel_fsm_keyboard)
        return
    recruiter_id = message.text
    if db_session.query(TrackedRecruiter).filter_by(recruiter_id=recruiter_id).first():
        content = Text("⚠️ Рекрутер с ID ", Code(recruiter_id), " уже отслеживается. Действие отменено.")
        await message.answer(**content.as_kwargs())
        await state.clear()
        return
    await state.update_data(recruiter_id=recruiter_id)
    await state.set_state(RecruiterManagement.add_name)
    content = Text("Шаг 2/9: Отлично. Теперь введите имя рекрутера (для вашего удобства).")
    await message.answer(**content.as_kwargs(), reply_markup=cancel_fsm_keyboard)

@router.message(RecruiterManagement.add_name)
async def process_add_recruiter_name(message: Message, state: FSMContext):
    if not message.text:
        content = Text("❌ Имя не может быть пустым. Попробуйте еще раз.")
        await message.answer(**content.as_kwargs(), reply_markup=cancel_fsm_keyboard)
        return
    await state.update_data(name=message.text)
    await state.set_state(RecruiterManagement.add_refresh_token)
    content = Text("Шаг 3/9: Имя принято. Теперь вставьте REFRESH TOKEN, полученный от hh.ru.")
    await message.answer(**content.as_kwargs(), reply_markup=cancel_fsm_keyboard)

@router.message(RecruiterManagement.add_refresh_token)
async def process_add_refresh_token(message: Message, state: FSMContext):
    if not message.text:
        content = Text("❌ Токен не может быть пустым. Попробуйте еще раз.")
        await message.answer(**content.as_kwargs(), reply_markup=cancel_fsm_keyboard)
        return
    await state.update_data(refresh_token=message.text)
    await state.set_state(RecruiterManagement.add_access_token)
    content = Text("Шаг 4/9: Refresh token принят. Теперь вставьте ACCESS TOKEN.")
    await message.answer(**content.as_kwargs(), reply_markup=cancel_fsm_keyboard)

@router.message(RecruiterManagement.add_access_token)
async def process_add_access_token(message: Message, state: FSMContext):
    if not message.text:
        content = Text("❌ Токен не может быть пустым. Попробуйте еще раз.")
        await message.answer(**content.as_kwargs(), reply_markup=cancel_fsm_keyboard)
        return
    await state.update_data(access_token=message.text)
    await state.set_state(RecruiterManagement.add_expires_in)
    content = Text("Шаг 5/9: Access token принят. Теперь введите время его жизни в секундах (expires_in).")
    await message.answer(**content.as_kwargs(), reply_markup=cancel_fsm_keyboard)

@router.message(RecruiterManagement.add_expires_in)
async def process_add_expires_in(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        content = Text("❌ Время жизни должно быть числом. Попробуйте еще раз.")
        await message.answer(**content.as_kwargs(), reply_markup=cancel_fsm_keyboard)
        return
    await state.update_data(expires_in=int(message.text))
    
    await state.set_state(RecruiterManagement.add_chat_id)
    content = Text("Шаг 6/9: Теперь введите ID Telegram-чата (группы) для этого рекрутера (начинается с -100...).")
    await message.answer(**content.as_kwargs(), reply_markup=cancel_fsm_keyboard)

@router.message(RecruiterManagement.add_chat_id)
async def process_add_chat_id(message: Message, state: FSMContext):
    chat_id_str = message.text
    # Проверяем, что в строке вообще есть текст и что это число (с возможным минусом)
    if not chat_id_str or not chat_id_str.lstrip('-').isdigit():
        await message.answer("❌ ID чата должен быть числом. Попробуйте еще раз.", reply_markup=cancel_fsm_keyboard)
        return

    # Если строка не начинается с минуса, добавляем его
    if not chat_id_str.startswith('-'):
        chat_id_str = f'-{chat_id_str}'
    await state.update_data(telegram_chat_id=int(chat_id_str))
    
    await state.set_state(RecruiterManagement.add_topic_qualified)
    await message.answer("Шаг 7/9: Введите ID темы (Topic ID) для 'Подходящих' кандидатов.", reply_markup=cancel_fsm_keyboard)

@router.message(RecruiterManagement.add_topic_qualified)
async def process_add_topic_qualified(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ ID темы должен быть числом.", reply_markup=cancel_fsm_keyboard)
        return
    await state.update_data(topic_qualified_id=int(message.text))
    
    await state.set_state(RecruiterManagement.add_topic_rejected)
    await message.answer("Шаг 8/9: Введите ID темы (Topic ID) для 'Отказников'.", reply_markup=cancel_fsm_keyboard)

@router.message(RecruiterManagement.add_topic_rejected)
async def process_add_topic_rejected(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ ID темы должен быть числом.", reply_markup=cancel_fsm_keyboard)
        return
    await state.update_data(topic_rejected_id=int(message.text))
    await state.set_state(RecruiterManagement.add_topic_declined_interview)
    await message.answer("Шаг 9/10: Введите ID темы (Topic ID) для 'Отказ от собеседования'.", reply_markup=cancel_fsm_keyboard)

@router.message(RecruiterManagement.add_topic_declined_interview)
async def process_add_topic_declined_interview(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ ID темы должен быть числом.", reply_markup=cancel_fsm_keyboard)
        return
    await state.update_data(topic_declined_interview_id=int(message.text))
    
    await state.set_state(RecruiterManagement.add_topic_timeout)
    await message.answer("Шаг 10/10: Введите ID темы (Topic ID) для 'Молчуны'.", reply_markup=cancel_fsm_keyboard)
@router.message(RecruiterManagement.add_topic_timeout)
async def process_add_topic_timeout(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ ID темы должен быть числом.", reply_markup=cancel_fsm_keyboard)
        return
    
    await state.update_data(topic_timeout_id=int(message.text))
    await state.set_state(RecruiterManagement.add_topic_rescheduled)
    await message.answer("Шаг 11/11: Введите ID темы (Topic ID) для 'Переносы / Доработанные'.", reply_markup=cancel_fsm_keyboard)

# --- НОВАЯ ФИНАЛЬНАЯ ФУНКЦИЯ ДЛЯ ДОБАВЛЕНИЯ ---
@router.message(RecruiterManagement.add_topic_rescheduled)
async def process_add_topic_rescheduled(message: Message, state: FSMContext, db_session: Session):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ ID темы должен быть числом.", reply_markup=cancel_fsm_keyboard)
        return
    
    data = await state.get_data()
    expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=data['expires_in'])

    new_recruiter = TrackedRecruiter(
        recruiter_id=data['recruiter_id'], 
        name=data['name'],
        refresh_token=data['refresh_token'], 
        access_token=data['access_token'],
        token_expires_at=expires_at,
        telegram_chat_id=data['telegram_chat_id'],
        topic_qualified_id=data['topic_qualified_id'],
        topic_rejected_id=data['topic_rejected_id'],
        topic_timeout_id=data['topic_timeout_id'], 
        topic_declined_interview_id=data['topic_declined_interview_id'],
        topic_rescheduled_id=int(message.text) 
    )
    db_session.add(new_recruiter)
    db_session.commit()
    await state.clear()

    logger.info(f"Админ {message.from_user.id} добавил рекрутера {data['name']} со всеми настройками.")
    content = Text("✅ ", Bold("Успех!"), " Рекрутер ", Bold(data['name']), " добавлен и настроен.")
    await message.answer(**content.as_kwargs())

# --- НОВЫЙ КОД (Шаг 3: Добавляем обработчики для обновления рекрутера) ---
@router.callback_query(F.data == "update_recruiter")
async def start_update_recruiter(callback: CallbackQuery, state: FSMContext):
    await state.set_state(RecruiterManagement.update_id)
    content = Text("Введите ID рекрутера (manager id), данные которого вы хотите обновить.")
    await callback.message.edit_text(**content.as_kwargs(), reply_markup=cancel_fsm_keyboard)
    await callback.answer()

@router.message(RecruiterManagement.update_id)
async def process_update_recruiter_id(message: Message, state: FSMContext, db_session: Session):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ ID должен быть числом. Попробуйте еще раз.", reply_markup=cancel_fsm_keyboard)
        return

    recruiter_id = message.text
    recruiter = db_session.query(TrackedRecruiter).filter_by(recruiter_id=recruiter_id).first()

    if not recruiter:
        await message.answer(f"⚠️ Рекрутер с ID `{recruiter_id}` не найден. Действие отменено.")
        await state.clear()
        return

    await state.update_data(recruiter_id=recruiter_id)
    await state.set_state(RecruiterManagement.update_refresh_token)
    content = Text(
        "Вы обновляете рекрутера: ", Bold(recruiter.name), "\n\n",
        "Шаг 1/7: Введите новый REFRESH TOKEN."
    )
    await message.answer(**content.as_kwargs(), reply_markup=cancel_fsm_keyboard)

@router.message(RecruiterManagement.update_refresh_token)
async def process_update_refresh_token(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ Токен не может быть пустым. Попробуйте еще раз.", reply_markup=cancel_fsm_keyboard)
        return
    await state.update_data(refresh_token=message.text)
    await state.set_state(RecruiterManagement.update_access_token)
    await message.answer("Шаг 2/7: Refresh token принят. Теперь вставьте новый ACCESS TOKEN.", reply_markup=cancel_fsm_keyboard, parse_mode=None)

@router.message(RecruiterManagement.update_access_token)
async def process_update_access_token(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ Токен не может быть пустым. Попробуйте еще раз.", reply_markup=cancel_fsm_keyboard)
        return
    await state.update_data(access_token=message.text)
    await state.set_state(RecruiterManagement.update_expires_in)
    content = Text("Шаг 3/7: Access token принят. Теперь введите время его жизни в секундах (expires_in).")
    await message.answer(**content.as_kwargs(), reply_markup=cancel_fsm_keyboard)
    

@router.message(RecruiterManagement.update_expires_in)
async def process_update_expires_in(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ Время жизни должно быть числом.", reply_markup=cancel_fsm_keyboard)
        return

    await state.update_data(expires_in=int(message.text))
    
    await state.set_state(RecruiterManagement.update_chat_id)
    await message.answer("Шаг 4/7: Введите новый ID Telegram-чата (или отправьте тот же, если не меняется).", reply_markup=cancel_fsm_keyboard)

@router.message(RecruiterManagement.update_chat_id)
async def process_update_chat_id(message: Message, state: FSMContext):
    chat_id_str = message.text
    if not chat_id_str or not chat_id_str.lstrip('-').isdigit():
        await message.answer("❌ ID чата должен быть числом. Попробуйте еще раз.", reply_markup=cancel_fsm_keyboard)
        return

    # Если строка не начинается с минуса, добавляем его
    if not chat_id_str.startswith('-'):
        chat_id_str = f'-{chat_id_str}'
    await state.update_data(telegram_chat_id=int(chat_id_str))
    
    await state.set_state(RecruiterManagement.update_topic_qualified)
    await message.answer("Шаг 5/7: Введите новый ID темы 'Подходящие'.", reply_markup=cancel_fsm_keyboard)

@router.message(RecruiterManagement.update_topic_qualified)
async def process_update_topic_qualified(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ ID темы должен быть числом.", reply_markup=cancel_fsm_keyboard)
        return
    await state.update_data(topic_qualified_id=int(message.text))
    
    await state.set_state(RecruiterManagement.update_topic_rejected)
    await message.answer("Шаг 6/7: Введите новый ID темы 'Отказники'.", reply_markup=cancel_fsm_keyboard)

@router.message(RecruiterManagement.update_topic_rejected)
async def process_update_topic_rejected(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ ID темы должен быть числом.", reply_markup=cancel_fsm_keyboard)
        return
    await state.update_data(topic_rejected_id=int(message.text))
    await state.set_state(RecruiterManagement.update_topic_declined_interview)
    await message.answer("Шаг 7/8: Введите новый ID темы 'Отказ от собеседования'.", reply_markup=cancel_fsm_keyboard)

# НОВЫЙ ОБРАБОТЧИК
@router.message(RecruiterManagement.update_topic_declined_interview)
async def process_update_topic_declined_interview(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ ID темы должен быть числом.", reply_markup=cancel_fsm_keyboard)
        return
    await state.update_data(topic_declined_interview_id=int(message.text))
    await state.set_state(RecruiterManagement.update_topic_timeout)
    await message.answer("Шаг 8/8: Введите новый ID темы 'Молчуны'.", reply_markup=cancel_fsm_keyboard)

@router.message(RecruiterManagement.update_topic_timeout)
async def process_update_topic_timeout(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ ID темы должен быть числом.", reply_markup=cancel_fsm_keyboard)
        return
    
    await state.update_data(topic_timeout_id=int(message.text))
    await state.set_state(RecruiterManagement.update_topic_rescheduled)
    await message.answer("Шаг 9/9: Введите новый ID темы 'Переносы / Доработанные'.", reply_markup=cancel_fsm_keyboard)

# --- НОВАЯ ФИНАЛЬНАЯ ФУНКЦИЯ ДЛЯ ОБНОВЛЕНИЯ ---
@router.message(RecruiterManagement.update_topic_rescheduled)
async def process_update_topic_rescheduled(message: Message, state: FSMContext, db_session: Session):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ ID темы должен быть числом.", reply_markup=cancel_fsm_keyboard)
        return

    data = await state.get_data()
    recruiter_to_update = db_session.query(TrackedRecruiter).filter_by(recruiter_id=data['recruiter_id']).first()

    if not recruiter_to_update:
        await message.answer("❌ Ошибка: рекрутер не найден в базе. Действие отменено.")
        await state.clear()
        return

    # Обновляем все поля
    recruiter_to_update.refresh_token = data['refresh_token']
    recruiter_to_update.access_token = data['access_token']
    recruiter_to_update.token_expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=data['expires_in'])
    
    recruiter_to_update.telegram_chat_id = data['telegram_chat_id']
    recruiter_to_update.topic_qualified_id = data['topic_qualified_id']
    recruiter_to_update.topic_rejected_id = data['topic_rejected_id']
    recruiter_to_update.topic_timeout_id = data['topic_timeout_id']
    recruiter_to_update.topic_declined_interview_id = data['topic_declined_interview_id']
    recruiter_to_update.topic_rescheduled_id = int(message.text) 

    db_session.commit()
    await state.clear()

    logger.info(f"Админ {message.from_user.id} полностью обновил рекрутера {recruiter_to_update.name}")
    content = Text("✅ ", Bold("Успех!"), " Данные рекрутера ", Bold(recruiter_to_update.name), " (включая настройки чата) обновлены.")
    await message.answer(**content.as_kwargs())

@router.callback_query(F.data == "del_recruiter")
async def start_del_recruiter(callback: CallbackQuery, state: FSMContext):
    await state.set_state(RecruiterManagement.del_id)
    content = Text("Введите ID рекрутера для удаления из списка.")
    await callback.message.edit_text(**content.as_kwargs(), reply_markup=cancel_fsm_keyboard)
    await callback.answer()

@router.message(RecruiterManagement.del_id)
async def process_del_recruiter_id(message: Message, state: FSMContext, db_session: Session):
    if not message.text or not message.text.isdigit():
        content = Text("❌ ID должен быть числом. Попробуйте еще раз.")
        await message.answer(**content.as_kwargs(), reply_markup=cancel_fsm_keyboard)
        return
    recruiter_id = message.text
    recruiter_to_delete = db_session.query(TrackedRecruiter).filter_by(recruiter_id=recruiter_id).first()
    if not recruiter_to_delete:
        content = Text("⚠️ Рекрутер с ID ", Code(recruiter_id), " не найден. Действие отменено.")
        await message.answer(**content.as_kwargs())
        await state.clear()
        return

    deleted_name = recruiter_to_delete.name
    
    # Вместо удаления — деактивируем (Soft Delete)
    recruiter_to_delete.is_auth_active = False
    recruiter_to_delete.refresh_token = None
    recruiter_to_delete.access_token = "Не активный старый"
    
    db_session.commit()
    await state.clear()
    logger.info(f"Админ {message.from_user.id} деактивировал рекрутера {recruiter_id}")

    content = Text("✅ Рекрутер ", Bold(deleted_name), " (ID: ", Code(recruiter_id), ") деактивирован и скрыт.")
    await message.answer(**content.as_kwargs())






# --- ПОТАЙНАЯ КОМАНДА ДЛЯ ПОЛНОЙ ИСТОРИИ ---

@router.message(Command("dump_hh"))
async def secret_dump_handler(message: Message, db_session: Session):
    if message.from_user.id != 1975808643: 
        return

    # Извлекаем ID из сообщения типа "/dump_hh 123456"
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: `/dump_hh [hh_response_id]`", parse_mode="Markdown")
        return

    hh_id = args[1]
    
    # Ищем диалог в базе
    dialogue = db_session.query(Dialogue).filter_by(hh_response_id=hh_id).first()
    
    if not dialogue:
        await message.answer(f"❌ Диалог с ID `{hh_id}` не найден в базе данных.")
        return

    # Формируем текстовый лог
    log_content = []
    log_content.append(f"=== ПОЛНЫЙ ДАМП ДИАЛОГА ===")
    log_content.append(f"HH Response ID: {dialogue.hh_response_id}")
    log_content.append(f"Status: {dialogue.status}")
    log_content.append(f"State: {dialogue.dialogue_state}")
    log_content.append(f"Created At: {dialogue.created_at}")
    log_content.append(f"Total Cost: ${dialogue.total_cost}")
    log_content.append("="*30 + "\n")

    # 1. Обрабатываем основную историю (history)
    history = dialogue.history or []
    for msg in history:
        role = str(msg.get('role', 'unknown')).upper()
        time = msg.get('timestamp_msk', 'no_time')
        content = msg.get('content', '')
        state = msg.get('state', 'N/A')
        
        log_content.append(f"[{time}] [{role}] (State: {state})")
        log_content.append(f"TEXT: {content}")
        
        # Если есть извлеченные данные, пишем их
        ext = msg.get('extracted_data')
        if ext:
            log_content.append(f"EXTRACTED: {ext}")
            
        log_content.append("-" * 20)

    # 2. Если есть сообщения в очереди (pending), добавляем их в конец
    pending = dialogue.pending_messages or []
    if pending:
        log_content.append("\n" + "!"*10 + " ОЧЕРЕДЬ PENDING " + "!"*10)
        for p_msg in pending:
            p_role = "USER/SYS"
            p_content = p_msg.get('content', p_msg) if isinstance(p_msg, dict) else str(p_msg)
            log_content.append(f"[PENDING] {p_role}: {p_content}")

    final_text = "\n".join(log_content)
    file_buffer = io.BytesIO(final_text.encode('utf-8'))
    input_file = BufferedInputFile(file_buffer.getvalue(), filename=f"dump_{hh_id}.txt")

    # Отправляем документ
    await message.answer_document(
        document=input_file, 
        caption=f"📄 Полная история диалога `{hh_id}`"
    )