from prometheus_client import Counter, Histogram, Gauge, start_http_server
import logging

logger = logging.getLogger(__name__)


# Ошибки (с разбивкой по типу ошибки и месту возникновения)
ERRORS_TOTAL = Counter(
    'hh_bot_errors_total', 
    'Total count of exceptions', 
    ['type', 'module']
)

# Токены LLM (считаем input и output)
LLM_TOKENS_TOTAL = Counter(
    'hh_bot_llm_tokens_total', 
    'Total LLM tokens used', 
    ['model', 'type'] # type = prompt (input) или completion (output)
)

# Стоимость (в долларах, будет дробное число)
LLM_COST_TOTAL = Counter(
    'hh_bot_llm_cost_total', 
    'Total estimated cost in USD'
)

# Бизнес-метрика: Количество отправленных ответов кандидатам
MESSAGES_SENT_TOTAL = Counter(
    'hh_bot_messages_sent_total',
    'Total messages sent to candidates'
)

# Длительность запроса к OpenAI
LLM_REQUEST_DURATION = Histogram(
    'hh_bot_llm_request_duration_seconds', 
    'Time spent waiting for OpenAI response',
    buckets=[1, 2, 5, 10, 20, 30, 60] 
)

# Задержка ответа (сколько времени прошло с момента написания сообщения кандидатом до нашего ответа)
E2E_RESPONSE_LATENCY = Histogram(
    'hh_bot_e2e_response_latency_seconds',
    'Time from candidate message created_at to bot reply',
    buckets=[60, 300, 600, 1800, 3600, 7200, 18000] # 1мин, 5мин, 10мин, 30мин, 1ч, 2ч, 5ч
)


# Очередь диалогов (сколько сейчас необработанных)
PENDING_DIALOGUES_GAUGE = Gauge(
    'hh_bot_pending_dialogues_count',
    'Number of dialogues waiting for processing'
)

# Загруженность семафоров Redis (значение от 0 до Limit)
REDIS_SEMAPHORE_USAGE = Gauge(
    'hh_bot_redis_semaphore_usage',
    'Current usage of distributed semaphores',
    ['semaphore_name']
)

def start_metrics_server(port=8000):
    """Запускает маленький HTTP сервер внутри воркера, чтобы отдавать метрики Прометею"""
    try:
        start_http_server(port)
        logger.info(f"📈 Metrics server started on port {port}")
    except Exception as e:
        logger.warning(f"Failed to start metrics server on {port} (maybe running multiple workers locally?): {e}")