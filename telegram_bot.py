import os
import json
import logging
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from anthropic import Anthropic

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

# Папка для сгенерированных договоров внутри постоянного хранилища
CONTRACTS_DIR = os.path.join(DATA_DIR, 'contracts')
os.makedirs(CONTRACTS_DIR, exist_ok=True)

# Файл с авторизованными пользователями (в постоянном хранилище)
AUTHORIZED_USERS_FILE = os.path.join(DATA_DIR, 'authorized_users.json')

logger.info(f"✅ DATA_DIR: {DATA_DIR}")

# Состояния для ConversationHandler
WAITING_FOR_CLIENT_DATA = 1
WAITING_FOR_CONTRACT_TYPE = 2
WAITING_FOR_CONTRACT_PARAMS = 3
GENERATING_CONTRACT = 4

# Инициализируем Anthropic клиент
anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

# ============ Управление авторизацией ============

def load_authorized_users() -> dict:
    """Загружает список авторизованных пользователей"""
    if os.path.exists(AUTHORIZED_USERS_FILE):
        with open(AUTHORIZED_USERS_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_authorized_users(users: dict):
    """Сохраняет список авторизованных пользователей"""
    with open(AUTHORIZED_USERS_FILE, 'w') as f:
        json.dump(users, f, indent=2, ensure_ascii=False)

def is_authorized(user_id: int) -> bool:
    """Проверяет, авторизован ли пользователь"""
    users = load_authorized_users()
    return str(user_id) in users and users[str(user_id)]['active']

def is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором"""
    return user_id == ADMIN_ID

# ============ Обработчики команд ============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    user_id = update.effective_user.id
    user_name = update.effective_user.full_name
    
    if not is_authorized(user_id) and not is_admin(user_id):
        await update.message.reply_text(
            f"❌ Доступ запрещён. Свяжитесь с администратором.\n"
            f"Ваш ID: {user_id}"
        )
        return ConversationHandler.END
    
    welcome_text = f"👋 Добро пожаловать, {user_name}!\n\n"
    welcome_text += "Я помогу вам генерировать договоры на основе данных клиента.\n\n"
    welcome_text += "Доступные команды:\n"
    welcome_text += "/new_contract - Создать новый договор\n"
    welcome_text += "/help - Справка\n"
    
    if is_admin(user_id):
        welcome_text += "/auth_users - Управление пользователями (админ)\n"
    
    await update.message.reply_text(welcome_text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /help"""
    user_id = update.effective_user.id
    
    if not is_authorized(user_id) and not is_admin(user_id):
        await update.message.reply_text("❌ Доступ запрещён")
        return
    
    help_text = """
📋 **Как использовать бота:**

1. Отправьте команду /new_contract
2. Введите данные клиента в следующем формате:
   - ФИО клиента
   - Должность
   - Организация
   - Реквизиты (если нужны)

3. Выберите тип договора
4. Укажите дополнительные параметры

Бот автоматически сгенерирует договор с использованием AI.

💡 **Примеры типов договоров:**
- Договор купли-продажи
- Договор оказания услуг
- Договор аренды
- NDA (Соглашение о конфиденциальности)
- Договор подряда

❓ При вопросах обращайтесь к администратору.
"""
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def auth_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /auth_users - управление авторизованными пользователями (только для админа)"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ Только администратор может управлять пользователями")
        return
    
    users = load_authorized_users()
    
    if not users:
        await update.message.reply_text("📋 Авторизованные пользователи отсутствуют")
        return
    
    text = "📋 **Авторизованные пользователи:**\n\n"
    for user_id_str, user_info in users.items():
        status = "✅ Активен" if user_info['active'] else "❌ Заблокирован"
        text += f"• {user_info['name']} (ID: {user_id_str}) - {status}\n"
        text += f"  Добавлен: {user_info['added_date']}\n\n"
    
    text += "\n/add_user <ID> <Имя> - добавить пользователя\n"
    text += "/remove_user <ID> - удалить пользователя\n"
    
    await update.message.reply_text(text, parse_mode='Markdown')

async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /add_user - добавить авторизованного пользователя"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ Только администратор может добавлять пользователей")
        return
    
    if len(context.args) < 2:
        await update.message.reply_text(
            "Используйте: /add_user <ID> <Имя>\n"
            "Пример: /add_user 123456789 Иван"
        )
        return
    
    new_user_id = context.args[0]
    new_user_name = ' '.join(context.args[1:])
    
    users = load_authorized_users()
    users[new_user_id] = {
        'name': new_user_name,
        'active': True,
        'added_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    save_authorized_users(users)
    
    await update.message.reply_text(f"✅ Пользователь {new_user_name} (ID: {new_user_id}) добавлен")

async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /remove_user - удалить авторизованного пользователя"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ Только администратор может удалять пользователей")
        return
    
    if len(context.args) < 1:
        await update.message.reply_text("Используйте: /remove_user <ID>")
        return
    
    remove_user_id = context.args[0]
    users = load_authorized_users()
    
    if remove_user_id in users:
        removed_name = users[remove_user_id]['name']
        del users[remove_user_id]
        save_authorized_users(users)
        await update.message.reply_text(f"✅ Пользователь {removed_name} удалён")
    else:
        await update.message.reply_text("❌ Пользователь не найден")

# ============ Диалог создания договора ============

async def new_contract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало создания нового договора"""
    user_id = update.effective_user.id
    
    if not is_authorized(user_id) and not is_admin(user_id):
        await update.message.reply_text("❌ Доступ запрещён")
        return ConversationHandler.END
    
    await update.message.reply_text(
        "📝 Создание нового договора\n\n"
        "Введите данные клиента в следующем формате:\n\n"
        "ФИО: [ФИО клиента]\n"
        "Должность: [должность]\n"
        "Организация: [название организации]\n"
        "Email: [email] (опционально)\n"
        "Телефон: [телефон] (опционально)\n\n"
        "Пример:\n"
        "ФИО: Иван Петров\n"
        "Должность: Директор\n"
        "Организация: ООО Рога и Копыта"
    )
    
    return WAITING_FOR_CLIENT_DATA

async def receive_client_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение данных клиента"""
    user_id = update.effective_user.id
    
    if not is_authorized(user_id) and not is_admin(user_id):
        await update.message.reply_text("❌ Доступ запрещён")
        return ConversationHandler.END
    
    # Сохраняем данные клиента в контексте
    context.user_data['client_data'] = update.message.text
    
    # Показываем кнопки выбора типа договора
    keyboard = [
        [InlineKeyboardButton("Купли-продажи", callback_data="contract_sale")],
        [InlineKeyboardButton("Оказания услуг", callback_data="contract_services")],
        [InlineKeyboardButton("Аренды", callback_data="contract_rent")],
        [InlineKeyboardButton("NDA", callback_data="contract_nda")],
        [InlineKeyboardButton("Подряда", callback_data="contract_work")],
        [InlineKeyboardButton("Другое", callback_data="contract_other")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "✅ Данные получены\n\n"
        "Выберите тип договора:",
        reply_markup=reply_markup
    )
    
    return WAITING_FOR_CONTRACT_TYPE

async def contract_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора типа договора"""
    query = update.callback_query
    await query.answer()
    
    contract_types = {
        "contract_sale": "Договор купли-продажи",
        "contract_services": "Договор оказания услуг",
        "contract_rent": "Договор аренды",
        "contract_nda": "NDA (Соглашение о конфиденциальности)",
        "contract_work": "Договор подряда",
        "contract_other": "Другой тип"
    }
    
    context.user_data['contract_type'] = contract_types[query.data]
    
    await query.edit_message_text(
        text=f"Выбран тип: **{context.user_data['contract_type']}**\n\n"
        "Введите дополнительные параметры договора (сумма, сроки, условия и т.д.):\n\n"
        "Или напишите 'готово' если параметров нет",
        parse_mode='Markdown'
    )
    
    return WAITING_FOR_CONTRACT_PARAMS

async def receive_contract_params(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение параметров договора"""
    user_id = update.effective_user.id
    
    if not is_authorized(user_id) and not is_admin(user_id):
        await update.message.reply_text("❌ Доступ запрещён")
        return ConversationHandler.END
    
    if update.message.text.lower() != 'готово':
        context.user_data['contract_params'] = update.message.text
    else:
        context.user_data['contract_params'] = "Стандартные условия"
    
    # Начинаем генерацию договора
    await update.message.reply_text("⏳ Генерирую договор с помощью Claude AI...")
    
    # Генерируем договор
    contract_text = await generate_contract(
        context.user_data['client_data'],
        context.user_data['contract_type'],
        context.user_data['contract_params']
    )
    
    # Сохраняем договор в файл (в постоянном хранилище)
    filename = f"contract_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    filepath = os.path.join(CONTRACTS_DIR, filename)
    context.user_data['contract_filename'] = filename
    context.user_data['contract_filepath'] = filepath

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(contract_text)
    
    # Отправляем договор
    await update.message.reply_text(
        f"✅ Договор успешно сгенерирован!\n\n"
        f"Тип: {context.user_data['contract_type']}\n"
        f"Файл: {filename}"
    )
    
    # Отправляем текст договора (разбиваем на части, если очень большой)
    if len(contract_text) > 4096:
        # Telegram лимит на сообщение - 4096 символов
        for i in range(0, len(contract_text), 4096):
            await update.message.reply_text(contract_text[i:i+4096])
    else:
        await update.message.reply_text(contract_text)
    
    # Предлагаем дополнительные действия
    keyboard = [
        [InlineKeyboardButton("📝 Создать новый договор", callback_data="new_contract_again")],
        [InlineKeyboardButton("💾 Скачать договор", callback_data="download_contract")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "Что дальше?",
        reply_markup=reply_markup
    )
    
    return ConversationHandler.END

async def generate_contract(client_data: str, contract_type: str, params: str) -> str:
    """Генерирует договор с помощью Claude API"""
    prompt = f"""
Ты опытный юрист, специалист по составлению договоров. 
Сгенерируй профессиональный договор на основе следующих данных:

ТИП ДОГОВОРА: {contract_type}

ДАННЫЕ КЛИЕНТА:
{client_data}

ПАРАМЕТРЫ И УСЛОВИЯ:
{params}

Требования:
1. Договор должен быть полным и готовым к подписанию
2. Используй профессиональную юридическую терминологию
3. Включи все необходимые разделы (стороны, предмет, условия, права и обязанности, сроки, стоимость и т.д.)
4. Договор должен соответствовать российскому законодательству
5. Текст должен быть четким и понятным

Выведи только текст договора без предисловий и комментариев.
"""
    
    message = anthropic_client.messages.create(
        model="claude-opus-4-1-20250805",
        max_tokens=4000,
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ]
    )
    
    return message.content[0].text

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена операции"""
    await update.message.reply_text("❌ Операция отменена")
    return ConversationHandler.END

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопок"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "new_contract_again":
        return await new_contract(update, context)
    elif query.data == "download_contract":
        # Отправляем файл договора
        filename = context.user_data.get('contract_filename')
        filepath = context.user_data.get('contract_filepath')
        if filepath and os.path.exists(filepath):
            await query.edit_message_text(
                text="💾 Договор готов к скачиванию\n"
                f"Файл: {filename}\n\n"
                "Используйте /start для новой операции"
            )
        else:
            await query.edit_message_text(text="❌ Файл не найден")

# ============ Основная функция ============

def main():
    """Запуск бота"""
    if not TELEGRAM_TOKEN or not ANTHROPIC_API_KEY:
        logger.error("Не установлены переменные окружения TELEGRAM_BOT_TOKEN или ANTHROPIC_API_KEY")
        return
    
    # Создаем приложение
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Инициализируем файл с пользователями, если его нет
    if not os.path.exists(AUTHORIZED_USERS_FILE):
        save_authorized_users({str(ADMIN_ID): {
            'name': 'Администратор',
            'active': True,
            'added_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }})
    
    # Создаем обработчик диалога
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("new_contract", new_contract)],
        states={
            WAITING_FOR_CLIENT_DATA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_client_data)
            ],
            WAITING_FOR_CONTRACT_TYPE: [
                CallbackQueryHandler(contract_type_selected, pattern=r"^contract_"),
                CommandHandler("cancel", cancel),
            ],
            WAITING_FOR_CONTRACT_PARAMS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_contract_params)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    
    # Добавляем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("auth_users", auth_users))
    application.add_handler(CommandHandler("add_user", add_user))
    application.add_handler(CommandHandler("remove_user", remove_user))
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Запускаем бота
    logger.info("🚀 Бот запущен")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
