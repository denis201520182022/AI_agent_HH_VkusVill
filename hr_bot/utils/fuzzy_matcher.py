# hr_bot/utils/fuzzy_matcher.py

import logging
import difflib
from typing import List, Optional, Tuple
from hr_bot.utils.constants import METRO_STATIONS_SPB
from hr_bot.utils.system_notifier import send_system_alert

logger = logging.getLogger(__name__)

def get_similarity(s1: str, s2: str) -> float:
    """Возвращает коэффициент схожести двух строк (0.0 - 1.0)."""
    return difflib.SequenceMatcher(None, s1.lower().strip(), s2.lower().strip()).ratio()

def find_best_match(input_str: str, choices: List[str], threshold: float = 0.6) -> Tuple[Optional[str], float]:
    """
    Находит лучший вариант из списка choices для строки input_str.
    Возвращает (совпадение, коэффициент) или (None, 0.0).
    """
    if not input_str or not choices:
        return None, 0.0
    
    best_match = None
    max_score = 0.0
    
    for choice in choices:
        score = get_similarity(input_str, choice)
        if score > max_score:
            max_score = score
            best_match = choice
            
    if max_score >= threshold:
        return best_match, max_score
    
    return None, max_score

async def match_metro(metro_input: str, dialogue_id: int = None, hh_id: str = None) -> Optional[str]:
    """
    Выполняет нечеткий поиск станции метро. 
    Порог: 0.6. Логирует и алертит при исправлениях.
    """
    if not metro_input:
        return None
        
    best_match, score = find_best_match(metro_input, METRO_STATIONS_SPB, threshold=0.6)
    
    if best_match:
        if score < 1.0:
            msg = (
                f"🧩 **Fuzzy Match (Metro)**\n"
                f"Диалог: `{dialogue_id}` (HH: `{hh_id}`)\n"
                f"Ввод: `{metro_input}`\n"
                f"Исправлено на: `{best_match}`\n"
                f"Схожесть: `{score:.2f}`"
            )
            logger.info(f"Fuzzy Match (Metro): '{metro_input}' -> '{best_match}' (score: {score:.2f})")
            if score < 0.95: # Не шлем алерт на совсем мелкие опечатки типа "ё"
                await send_system_alert(msg, alert_type="admin_only")
        return best_match
    
    logger.warning(f"Fuzzy Match (Metro): Не удалось найти соответствие для '{metro_input}' (лучший score: {score:.2f})")
    return None

async def match_address(address_input: str, valid_addresses: List[str], dialogue_id: int = None, hh_id: str = None) -> Optional[str]:
    """
    Выполняет нечеткий поиск адреса из списка доступных. 
    Порог: 0.9. Логирует и алертит при исправлениях.
    """
    if not address_input or not valid_addresses:
        return None
        
    best_match, score = find_best_match(address_input, valid_addresses, threshold=0.9)
    
    if best_match:
        if score < 1.0:
            msg = (
                f"🧩 **Fuzzy Match (Address)**\n"
                f"Диалог: `{dialogue_id}` (HH: `{hh_id}`)\n"
                f"Ввод: `{address_input}`\n"
                f"Исправлено на: `{best_match}`\n"
                f"Схожесть: `{score:.2f}`"
            )
            logger.info(f"Fuzzy Match (Address): '{address_input}' -> '{best_match}' (score: {score:.2f})")
            await send_system_alert(msg, alert_type="admin_only")
        return best_match
    
    logger.warning(f"Fuzzy Match (Address): Не удалось найти соответствие для '{address_input}' (лучший score: {score:.2f})")
    return None
