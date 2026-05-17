import os
import io
import json
import logging
from datetime import datetime, timedelta
from typing import Optional
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from anthropic import Anthropic

# Чтение .docx (для пользовательских шаблонов и присланных договоров)
try:
    from docx import Document as DocxDocument
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False

# Загружаем переменные окружения
load_dotenv()

# Логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Константы - ИСПРАВЛЕННОЕ ЧТЕНИЕ
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN') or os.getenv('TELEGRAM_BOT_TOKEN')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY') or os.getenv('ANTHROPIC_API_KEY')
ADMIN_ID_STR = os.environ.get('ADMIN_ID') or os.getenv('ADMIN_ID', '0')

# Проверка что ключи есть
if not TELEGRAM_TOKEN:
    logger.error("❌ TELEGRAM_BOT_TOKEN не установлен!")
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

if not ANTHROPIC_API_KEY:
    logger.error("❌ ANTHROPIC_API_KEY не установлен!")
    raise ValueError("ANTHROPIC_API_KEY environment variable is required")

try:
    ADMIN_ID = int(ADMIN_ID_STR)
except (ValueError, TypeError):
    logger.error(f"❌ ADMIN_ID должен быть числом, получено: {ADMIN_ID_STR}")
    ADMIN_ID = 0

logger.info(f"✅ Переменные загружены успешно")
logger.info(f"✅ TELEGRAM_TOKEN: {TELEGRAM_TOKEN[:10]}...")
logger.info(f"✅ ANTHROPIC_API_KEY: {ANTHROPIC_API_KEY[:10]}...")
logger.info(f"✅ ADMIN_ID: {ADMIN_ID}")

# Директория для постоянного хранения данных.
# На Railway сюда монтируется Volume через переменную DATA_DIR (например /data).
# Локально, если переменная не задана, используется текущая папка.
DATA_DIR = os.environ.get('DATA_DIR', '.')
os.makedirs(DATA_DIR, exist_ok=True)

# Файл с пользовательскими шаблонами договоров (общие для всех)
TEMPLATES_FILE = os.path.join(DATA_DIR, 'custom_templates.json')

logger.info(f"✅ DATA_DIR: {DATA_DIR}")

# ============ Настройки модели и диалога ============

# Используемая модель Claude
CLAUDE_MODEL = "claude-sonnet-4-6"

# Сколько последних сообщений диалога хранить (user + assistant суммарно)
MAX_HISTORY_MESSAGES = 20

# Через сколько часов простоя контекст диалога обнуляется
CONTEXT_RESET_HOURS = 24

# Максимум токенов в ответе
MAX_TOKENS = 4000

# Дисклеймер, добавляется к юридическим результатам
DISCLAIMER = (
    "\n\n———\n"
    "⚠️ Это черновик, подготовленный AI. Перед подписанием проверьте документ "
    "у юриста — особенно для сделок с недвижимостью."
)

# Инициализируем Anthropic клиент
anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

# ============ Управление шаблонами ============

def load_templates() -> dict:
    """Загружает пользовательские шаблоны договоров"""
    if os.path.exists(TEMPLATES_FILE):
        with open(TEMPLATES_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_templates(templates: dict):
    """Сохраняет пользовательские шаблоны договоров"""
    with open(TEMPLATES_FILE, 'w', encoding='utf-8') as f:
        json.dump(templates, f, indent=2, ensure_ascii=False)

def is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором"""
    return user_id == ADMIN_ID

# ============ Системный промпт ============

def build_system_prompt() -> str:
    """Собирает системный промпт с ролью и шаблонами договоров"""
    prompt = (
        "Ты — юридический ассистент в Telegram-боте, специалист по сделкам "
        "с недвижимостью в России: купля-продажа, аренда, займ, расписки, "
        "договоры подряда и сопутствующие документы.\n\n"
        "Твоя задача — помогать пользователю в свободной беседе:\n"
        "• составлять договоры по описанию задачи (в свободной форме);\n"
        "• анализировать присланные договоры — указывать риски, слабые места "
        "и предлагать недостающие юридические пункты;\n"
        "• отвечать на вопросы по договорам и сделкам с недвижимостью.\n\n"
        "Правила:\n"
        "• Пиши на русском языке, понятно и по делу.\n"
        "• Если пользователь просит составить договор — выдавай полный, "
        "структурированный, готовый к подписанию документ с нумерацией "
        "разделов (1.1, 1.2 и т.д.). Недостающие данные оставляй как "
        "[placeholder] для заполнения.\n"
        "• Опирайся на законодательство РФ (ГК РФ и профильные нормы).\n"
        "• Если данных не хватает — задай уточняющие вопросы, прежде чем "
        "составлять документ.\n"
        "• Если для нужного вида договора есть подходящий шаблон ниже — "
        "бери его за основу, адаптируя под запрос пользователя.\n"
    )

    templates = load_templates()
    if templates:
        prompt += "\n=== ШАБЛОНЫ ДОГОВОРОВ ===\n"
        for name, body in templates.items():
            prompt += f"\n--- ШАБЛОН: {name} ---\n{body}\n--- КОНЕЦ ШАБЛОНА ---\n"
    else:
        prompt += "\n(Пользовательские шаблоны пока не добавлены.)\n"

    return prompt

# ============ Вспомогательные функции ============

def extract_text_from_docx(file_bytes: bytes) -> str:
    """Извлекает текст из .docx файла"""
    if not HAS_DOCX:
        return ""
    doc = DocxDocument(io.BytesIO(file_bytes))
    return "\n".join(p.text for p in doc.paragraphs)

async def read_document_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    """Считывает текст из присланного документа (.txt или .docx). None если не получилось."""
    document = update.message.document
    if not document:
        return None

    file_name = (document.file_name or "").lower()
    tg_file = await context.bot.get_file(document.file_id)
    file_bytes = bytes(await tg_file.download_as_bytearray())

    if file_name.endswith('.docx'):
        if not HAS_DOCX:
            return None
        return extract_text_from_docx(file_bytes)
    elif file_name.endswith('.txt'):
        try:
            return file_bytes.decode('utf-8')
        except UnicodeDecodeError:
            return file_bytes.decode('cp1251', errors='ignore')
    return None

async def send_long_message(update: Update, text: str):
    """Отправляет длинный текст, разбивая на части по лимиту Telegram (4096)"""
    if len(text) <= 4096:
        await update.message.reply_text(text)
    else:
        for i in range(0, len(text), 4096):
            await update.message.reply_text(text[i:i + 4096])

# ============ Управление историей диалога ============

def get_history(context: ContextTypes.DEFAULT_TYPE) -> list:
    """Возвращает историю диалога, обнуляя её при простое больше CONTEXT_RESET_HOURS."""
    started_at = context.user_data.get('chat_started_at')
    if started_at:
        try:
            started_dt = datetime.fromisoformat(started_at)
            if datetime.now() - started_dt > timedelta(hours=CONTEXT_RESET_HOURS):
                # Контекст устарел — обнуляем
                context.user_data['history'] = []
                context.user_data['chat_started_at'] = datetime.now().isoformat()
                logger.info("Контекст диалога обнулён по истечении суток")
        except ValueError:
            context.user_data['chat_started_at'] = datetime.now().isoformat()
    else:
        context.user_data['chat_started_at'] = datetime.now().isoformat()

    return context.user_data.setdefault('history', [])

def append_history(context: ContextTypes.DEFAULT_TYPE, role: str, content: str):
    """Добавляет сообщение в историю и обрезает её до MAX_HISTORY_MESSAGES."""
    history = context.user_data.setdefault('history', [])
    history.append({"role": role, "content": content})
    # Оставляем только последние MAX_HISTORY_MESSAGES сообщений
    if len(history) > MAX_HISTORY_MESSAGES:
        del history[:len(history) - MAX_HISTORY_MESSAGES]

# ============ Запрос к Claude ============

def ask_claude(history: list) -> str:
    """Отправляет диалог в Claude API с кэшированием системного промпта."""
    system_prompt = build_system_prompt()
    message = anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                # Кэширование: системный промпт (роль + шаблоны) повторяется
                # в каждом запросе — платим за него 10% после первого раза.
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=history,
    )
    # Логируем использование кэша для контроля экономии
    usage = message.usage
    logger.info(
        f"Tokens — in:{usage.input_tokens} "
        f"cache_write:{getattr(usage, 'cache_creation_input_tokens', 0)} "
        f"cache_read:{getattr(usage, 'cache_read_input_tokens', 0)} "
        f"out:{usage.output_tokens}"
    )
    # Собираем текст из всех текстовых блоков ответа
    parts = [block.text for block in message.content if block.type == "text"]
    return "\n".join(parts).strip()

# ============ Обработчики команд ============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    user_name = update.effective_user.full_name

    # Новый /start — начинаем диалог заново
    context.user_data['history'] = []
    context.user_data['chat_started_at'] = datetime.now().isoformat()

    welcome_text = f"👋 Здравствуйте, {user_name}!\n\n"
    welcome_text += (
        "Я — юридический ассистент по недвижимости. Помогаю составлять "
        "и проверять договоры: купля-продажа, аренда, займ, расписки и другие.\n\n"
        "Просто пишите мне обычным текстом — как живому человеку. Например:\n"
        "• «Составь договор аренды квартиры на 11 месяцев, 35 000 руб/мес»\n"
        "• «Проверь этот договор» (и пришлите текст или файл .txt / .docx)\n"
        "• «Какие пункты обязательно нужны в договоре купли-продажи?»\n\n"
        "Если данных не хватит — я уточню.\n\n"
        "Команды:\n"
        "/reset — начать диалог заново\n"
        "/help — справка\n"
    )
    if is_admin(update.effective_user.id):
        welcome_text += (
            "\nАдмин-команды:\n"
            "/templates — список шаблонов\n"
            "/add_template <название> — добавить шаблон (затем пришлите текст/файл)\n"
            "/del_template <название> — удалить шаблон\n"
        )

    await update.message.reply_text(welcome_text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /help"""
    help_text = (
        "📋 Как пользоваться ботом\n\n"
        "Просто пишите свободным текстом — бот сам поймёт, что нужно:\n"
        "• составить договор — опишите задачу (вид договора, стороны, "
        "объект, сумма, сроки, условия);\n"
        "• проверить договор — напишите об этом и пришлите текст "
        "или файл (.txt / .docx);\n"
        "• задать вопрос по сделкам с недвижимостью.\n\n"
        "Бот помнит ход беседы, поэтому можно уточнять и дополнять "
        "по ходу разговора.\n\n"
        "/reset — забыть текущий диалог и начать заново.\n"
        f"Контекст также автоматически обнуляется после "
        f"{CONTEXT_RESET_HOURS} часов простоя.\n\n"
        "🎙 Голосовые сообщения пока не поддерживаются — пишите текстом.\n\n"
        "❓ Вопросы — к администратору."
    )
    await update.message.reply_text(help_text)

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /reset — очистка контекста диалога"""
    context.user_data['history'] = []
    context.user_data['chat_started_at'] = datetime.now().isoformat()
    await update.message.reply_text(
        "🔄 Диалог сброшен. Можете начать новый разговор."
    )

# ============ Шаблоны (только админ) ============

async def templates_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /templates — показать список шаблонов"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Команда доступна только администратору.")
        return

    templates = load_templates()
    if not templates:
        await update.message.reply_text("📂 Шаблоны пока не добавлены.")
        return

    text = "📂 Доступные шаблоны:\n\n"
    for name in templates:
        text += f"• {name}\n"
    await update.message.reply_text(text)

async def add_template_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /add_template <название> — задаёт название и ждёт текст/файл шаблона"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Добавлять шаблоны может только администратор.")
        return

    if not context.args:
        await update.message.reply_text(
            "Используйте: /add_template <название>\n"
            "Например: /add_template Договор купли-продажи квартиры\n\n"
            "После этого пришлите текст шаблона сообщением или файлом (.txt / .docx)."
        )
        return

    name = ' '.join(context.args)
    context.user_data['awaiting_template_name'] = name
    await update.message.reply_text(
        f"📝 Название шаблона: «{name}».\n"
        "Теперь пришлите сам текст шаблона — сообщением или файлом (.txt / .docx)."
    )

async def del_template_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /del_template <название> — удалить шаблон"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Удалять шаблоны может только администратор.")
        return

    templates = load_templates()
    if not templates:
        await update.message.reply_text("📂 Шаблонов нет.")
        return

    if not context.args:
        text = "Используйте: /del_template <название>\n\nДоступные шаблоны:\n"
        for name in templates:
            text += f"• {name}\n"
        await update.message.reply_text(text)
        return

    name = ' '.join(context.args)
    if name in templates:
        del templates[name]
        save_templates(templates)
        await update.message.reply_text(f"✅ Шаблон «{name}» удалён.")
    else:
        await update.message.reply_text(f"❌ Шаблон «{name}» не найден.")

# ============ Основной обработчик сообщений ============

async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Голосовые сообщения пока не поддерживаются"""
    await update.message.reply_text(
        "🎙 Голосовые сообщения пока не поддерживаются.\n"
        "Опишите задачу текстом, и я помогу с договором."
    )

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает любое текстовое сообщение или документ — свободный диалог."""
    user_id = update.effective_user.id

    # Извлекаем текст из сообщения или присланного файла
    incoming_text = None
    if update.message.document:
        incoming_text = await read_document_text(update, context)
        if incoming_text is None:
            await update.message.reply_text(
                "❌ Не удалось прочитать файл. Поддерживаются .txt и .docx. "
                "Можно также прислать текст сообщением."
            )
            return
    elif update.message.text:
        incoming_text = update.message.text

    if not incoming_text or not incoming_text.strip():
        await update.message.reply_text("Пришлите текст — я помогу с договором.")
        return

    # Режим добавления шаблона (админ ранее вызвал /add_template <название>)
    if is_admin(user_id) and context.user_data.get('awaiting_template_name'):
        name = context.user_data.pop('awaiting_template_name')
        templates = load_templates()
        templates[name] = incoming_text.strip()
        save_templates(templates)
        await update.message.reply_text(
            f"✅ Шаблон «{name}» сохранён и доступен всем пользователям."
        )
        return

    # Свободный диалог с Claude
    history = get_history(context)
    append_history(context, "user", incoming_text)

    await update.message.chat.send_action("typing")

    try:
        answer = ask_claude(context.user_data['history'])
    except Exception as e:
        logger.error(f"Ошибка запроса к Claude: {e}")
        # Откатываем последнее сообщение пользователя, чтобы можно было повторить
        if context.user_data.get('history'):
            context.user_data['history'].pop()
        await update.message.reply_text(
            "❌ Не удалось получить ответ. Попробуйте ещё раз чуть позже."
        )
        return

    if not answer:
        answer = "Извините, не удалось сформировать ответ. Попробуйте переформулировать."

    append_history(context, "assistant", answer)
    await send_long_message(update, answer + DISCLAIMER)

# ============ Основная функция ============

def main():
    """Запуск бота"""
    if not TELEGRAM_TOKEN or not ANTHROPIC_API_KEY:
        logger.error("Не установлены переменные окружения TELEGRAM_BOT_TOKEN или ANTHROPIC_API_KEY")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CommandHandler("templates", templates_command))
    application.add_handler(CommandHandler("add_template", add_template_command))
    application.add_handler(CommandHandler("del_template", del_template_command))

    # Голосовые — пока не поддерживаются
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, voice_handler))

    # Любой текст или документ вне команд — свободный диалог
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.Document.ALL) & ~filters.COMMAND,
            message_handler
        )
    )

    logger.info("🚀 Бот запущен")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
