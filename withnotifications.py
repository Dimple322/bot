import sqlite3
import asyncio
import re
from datetime import datetime, time
import logging
import pandas as pd
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
import threading
from contextlib import contextmanager

# Блокировка для конкурентного доступа к БД
db_lock = threading.Lock()

# Контекстный менеджер для безопасного доступа к БД
@contextmanager
def db_connection():
    with db_lock:
        conn = sqlite3.connect("risks.db", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


PROBABILITY_MAPPING = {
    1: 10,  # <10%
    2: 15,  # 10-20%
    3: 27,  # 20-35%
    4: 47,  # 35-60%
    5: 60  # >60%
}

# Функции преобразования в 5-балльную шкалу
def get_cost_score(cost_impact: int) -> int:
    """Преобразует влияние на стоимость в 5-балльную шкалу"""
    if cost_impact < 100:
        return 1
    elif cost_impact < 200:
        return 2
    elif cost_impact < 500:
        return 3
    elif cost_impact < 1000:
        return 4
    else:
        return 5

def get_schedule_score(schedule_impact: int) -> int:
    """Преобразует влияние на сроки в 5-балльную шкалу"""
    if schedule_impact < 14:
        return 1
    elif schedule_impact < 45:
        return 2
    elif schedule_impact < 90:
        return 3
    elif schedule_impact < 150:
        return 4
    else:
        return 5

def get_probability_score(probability: int) -> int:
    """Преобразует вероятность в 5-балльную шкалу"""
    if probability < 10:
        return 1
    elif probability < 20:
        return 2
    elif probability < 50:
        return 3
    elif probability < 75:
        return 4
    else:
        return 5

# Включаем логирование для отладки
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# === Настройка базы данных ===
def init_db():
    with db_connection() as conn:
        cursor = conn.cursor()

        # Создаём таблицу risks с кириллическими именами (в кавычках)
        cursor.execute("""
                    CREATE TABLE IF NOT EXISTS risks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        username TEXT,
                        phase TEXT,
                        object TEXT,
                        category TEXT,
                        description TEXT,
                        impact_schedule_min INTEGER,
                        impact_schedule_most_likely INTEGER,
                        impact_schedule_max INTEGER,
                        impact_cost_min INTEGER,
                        impact_cost_most_likely INTEGER,
                        impact_cost_max INTEGER,
                        impact_schedule_min_re INTEGER,
                        impact_schedule_most_likely_re INTEGER,
                        impact_schedule_max_re INTEGER,
                        impact_cost_min_re INTEGER,
                        impact_cost_most_likely_re INTEGER,
                        impact_cost_max_re INTEGER,
                        probability INTEGER,
                        probability_re INTEGER,
                        risk_score INTEGER,
                        timeline TEXT,
                        mitigation TEXT,
                        expected_result TEXT,
                        reevaluation_needed INTEGER,
                        needs_schedule_evaluation INTEGER,
                        needs_cost_evaluation INTEGER,
                        needs_schedule_evaluation_re INTEGER,
                        needs_cost_evaluation_re INTEGER,
                        timestamp TEXT
                    )
                """)

        # Проверяем структуру таблицы и добавляем недостающие колонки
        cursor.execute("PRAGMA table_info(risks)")
        columns = [column[1] for column in cursor.fetchall()]

        required_columns = {
            'impact_schedule_min': 'INTEGER',
            'impact_schedule_most_likely': 'INTEGER',
            'impact_schedule_max': 'INTEGER',
            'impact_cost_min': 'INTEGER',
            'impact_cost_most_likely': 'INTEGER',
            'impact_cost_max': 'INTEGER',
            'impact_schedule_min_re': 'INTEGER',
            'impact_schedule_most_likely_re': 'INTEGER',
            'impact_schedule_max_re': 'INTEGER',
            'impact_cost_min_re': 'INTEGER',
            'impact_cost_most_likely_re': 'INTEGER',
            'impact_cost_max_re': 'INTEGER',
            'probability': 'INTEGER',
            'probability_re': 'INTEGER',
            'risk_score': 'INTEGER',
            'timeline': 'TEXT',
            'mitigation': 'TEXT',
            'expected_result': 'TEXT',
            'reevaluation_needed': 'INTEGER',
            'needs_schedule_evaluation': 'INTEGER',
            'needs_cost_evaluation': 'INTEGER',
            'needs_schedule_evaluation_re': 'INTEGER',
            'needs_cost_evaluation_re': 'INTEGER'
        }

        for column_name, column_type in required_columns.items():
            if column_name not in columns:
                try:
                    cursor.execute(f'ALTER TABLE risks ADD COLUMN "{column_name}" {column_type}')
                except sqlite3.OperationalError as e:
                    logger.warning(f"Не удалось добавить колонку {column_name}: {e}")

                # Создаем таблицу для дополнительных мероприятий
            cursor.execute("""
                  CREATE TABLE IF NOT EXISTS additional_mitigations (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      risk_id INTEGER,
                      user_id INTEGER,
                      username TEXT,
                      mitigation TEXT,
                      expected_result TEXT,
                      timestamp TEXT,
                      FOREIGN KEY (risk_id) REFERENCES risks(id)
                  )
              """)
            conn.commit()

        # Создаём таблицу подписок
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER PRIMARY KEY,
                is_subscribed INTEGER DEFAULT 1,
                last_notification DATE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()



# === Конфигурация проекта ===
PHASES = ["1", "2", "3", "4", "5", "Песцовое", "КПРA"]
OBJECTS = ["Скважины", "Кустовые площадки", "Площадочные объекты", "Линейная часть", "Другое"]
CATEGORIES = ["Проектное управление", "Стоимостной инжиниринг", "ГиР", "Операционная деятельность", "Строительство",
              "Закупки", "Инжиниринг", "Бурение", "ПБ"]


# === Функция создания клавиатуры с кнопкой "Назад" ===
def create_back_keyboard(back_data="back_to_menu"):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Вернуться на предыдущий шаг", callback_data=back_data)
    ]])

# === Функции работы с подписками ===
def subscribe_user(user_id: int):
    """Подписывает пользователя на рассылку"""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO subscriptions (user_id, is_subscribed, last_notification)
            VALUES (?, 1, ?)
        """, (user_id, datetime.now().strftime("%Y-%m-%d")))
        conn.commit()

def unsubscribe_user(user_id: int):
    """Отписывает пользователя от рассылки"""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE subscriptions SET is_subscribed = 0 WHERE user_id = ?
        """, (user_id,))
        conn.commit()

def is_subscribed(user_id: int) -> bool:
    """Проверяет, подписан ли пользователь на рассылку"""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT is_subscribed FROM subscriptions WHERE user_id = ? AND is_subscribed = 1
        """, (user_id,))
        return cursor.fetchone() is not None

def get_subscribed_users():
    """Возвращает список ID пользователей, которые подписаны на рассылку"""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id FROM subscriptions WHERE is_subscribed = 1
        """)
        return [row[0] for row in cursor.fetchall()]

# === Функция экспорта в Excel ===
def export_to_excel():
    try:
        with db_connection() as conn:
            # SQL-запрос с выборкой всех данных, включая дополнительные мероприятия
            df = pd.read_sql_query("""
                SELECT 
                    r.id,
                    r.username,
                    r.phase,
                    r.object,
                    r.category,
                    r.description,
                    r.impact_schedule_min,
                    r.impact_schedule_most_likely,
                    r.impact_schedule_max,
                    r.impact_cost_min,
                    r.impact_cost_most_likely,
                    r.impact_cost_max,
                    r.impact_schedule_min_re,
                    r.impact_schedule_most_likely_re,
                    r.impact_schedule_max_re,
                    r.impact_cost_min_re,
                    r.impact_cost_most_likely_re,
                    r.impact_cost_max_re,
                    r.probability,
                    r.probability_re,
                    r.risk_score,
                    r.timeline,
                    r.mitigation,
                    r.expected_result,
                    r.reevaluation_needed,
                    r.needs_schedule_evaluation,
                    r.needs_cost_evaluation,
                    r.needs_schedule_evaluation_re,
                    r.needs_cost_evaluation_re,
                    r.timestamp,
                    GROUP_CONCAT(m.mitigation, '\n\n') as additional_mitigations,
                    GROUP_CONCAT(m.expected_result, '\n\n') as additional_expected_results,
                    GROUP_CONCAT(m.username, '\n\n') as mitigation_usernames,
                    GROUP_CONCAT(m.timestamp, '\n\n') as mitigation_timestamps
                FROM risks r
                LEFT JOIN additional_mitigations m ON r.id = m.risk_id
                GROUP BY r.id
                ORDER BY r.risk_score DESC
            """, conn)

        # Словарь для красивых названий колонок
        column_names = {
            "id": "Номер риска",
            "username": "От кого риск",
            "phase": "Фаза/Опция",
            "object": "Объект",
            "category": "Функциональное направление риска",
            "description": "Описание риска",
            "impact_schedule_min": "Влияние на сроки до мероприятий (мин)",
            "impact_schedule_most_likely": "Влияние на сроки до мероприятий (ожидаемое)",
            "impact_schedule_max": "Влияние на сроки до мероприятий (макс)",
            "impact_cost_min": "Влияние на стоимость до мероприятий (мин)",
            "impact_cost_most_likely": "Влияние на стоимость до мероприятий (ожидаемое)",
            "impact_cost_max": "Влияние на стоимость до мероприятий (макс)",
            "impact_schedule_min_re": "Влияние на сроки после мероприятий (мин)",
            "impact_schedule_most_likely_re": "Влияние на сроки после мероприятий (ожидаемое)",
            "impact_schedule_max_re": "Влияние на сроки после мероприятий (макс)",
            "impact_cost_min_re": "Влияние на стоимость после мероприятий (мин)",
            "impact_cost_most_likely_re": "Влияние на стоимость после мероприятий (ожидаемое)",
            "impact_cost_max_re": "Влияние на стоимость после мероприятий (макс)",
            "probability": "Вероятность до мероприятий",
            "probability_re": "Вероятность после мероприятий",
            "risk_score": "Рейтинг риска",
            "timeline": "Срок реализации риска",
            "mitigation": "Мероприятие по митигации",
            "expected_result": "Ожидаемый результат",
            "reevaluation_needed": "Нужна переоценка",
            "needs_schedule_evaluation": "Нужна оценка сроков",
            "needs_cost_evaluation": "Нужна оценка стоимости",
            "needs_schedule_evaluation_re": "Нужна переоценка сроков",
            "needs_cost_evaluation_re": "Нужна переоценка стоимости",
            "timestamp": "Дата внесения",
            "additional_mitigations": "Дополнительные мероприятия",
            "additional_expected_results": "Ожидаемые результаты дополнительных мероприятий",
            "mitigation_usernames": "Добавлено пользователями",
            "mitigation_timestamps": "Дата добавления мероприятий"
        }

        # Переименовываем колонки
        df = df.rename(columns=column_names)

        # Заменяем 1/0 на "Да"/"Нет" в булевых полях
        yes_no_columns = [
            "Нужна переоценка", "Нужна оценка сроков", "Нужна оценка стоимости",
            "Нужна переоценка сроков", "Нужна переоценка стоимости"
        ]
        for col in yes_no_columns:
            if col in df.columns:
                df[col] = df[col].map({1: "Да", 0: "Нет"})

        # Сохраняем в Excel
        filename = f"Риски_{datetime.now().strftime('%d%m%Y_%H%M')}.xlsx"
        df.to_excel(filename, index=False, sheet_name="Риски", engine='openpyxl')
        return filename
    except Exception as e:
        logger.error(f"Ошибка при экспорте в Excel: {e}")
        return None

# === Вспомогательная функция для парсинга трех значений ===
def parse_three_values(text: str) -> tuple:
    """Парсит строку и возвращает кортез из трех целых чисел."""
    # Используем регулярное выражение для поиска всех чисел в строке
    numbers = re.findall(r'-?\d+', text)
    if len(numbers) >= 3:
        return int(numbers[0]), int(numbers[1]), int(numbers[2])
    else:
        raise ValueError("Неверный формат. Введите три числа.")


def create_risk_selection_keyboard(risks):
    """
    Создаёт клавиатуру с кнопками для выбора рисков.
    Располагает кнопки в несколько рядов по 4 штуки.
    """
    keyboard = []
    row = []
    buttons_per_row = 4  # Количество кнопок в одном ряду

    for risk in risks:
        risk_id = risk['id']
        button = InlineKeyboardButton(f"#{risk_id}", callback_data=f"view_risk_{risk_id}")
        row.append(button)

        # Если в ряду накопилось достаточно кнопок, добавляем ряд в клавиатуру и начинаем новый
        if len(row) == buttons_per_row:
            keyboard.append(row)
            row = []

    # Добавляем последний неполный ряд, если он есть
    if row:
        keyboard.append(row)

    return keyboard

# === Обработчики ===
# === Обработчики команд для управления подпиской ===
async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подписывает пользователя на ежедневные напоминания"""
    user_id = update.effective_user.id
    subscribe_user(user_id)
    message = (
        "✅ Вы успешно подписались на ежедневные напоминания о необходимости ведения рисков!\n\n"
        "Каждый день в 14:30 вы будете получать напоминание о заполнении рисков."
    )
    await update.message.reply_text(message)

async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отписывает пользователя от ежедневных напоминаний"""
    user_id = update.effective_user.id
    unsubscribe_user(user_id)
    message = "✅ Вы отписаны от напоминаний."
    await update.message.reply_text(message)


async def select_report_phase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Создаем кнопки для выбора фазы
    keyboard = []
    for phase in PHASES:
        keyboard.append([InlineKeyboardButton(phase, callback_data=f"report_phase_{phase}")])

    # Добавляем кнопку "Все фазы"
    keyboard.append([InlineKeyboardButton("Все фазы", callback_data="report_phase_all")])
    keyboard.append([InlineKeyboardButton("🔙 Вернуться в меню", callback_data="back_to_menu")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("Выберите фазу для отчета:", reply_markup=reply_markup)

# === Функция ежедневной рассылки ===
async def send_daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Отправляет ежедневное напоминание всем подписанным пользователям"""
    subscribed_users = get_subscribed_users()
    current_date = datetime.now().strftime("%d.%m.%Y")

    message = (
        f"📌 Ежедневное напоминание от {current_date}\n\n"
        "Не забудьте заполнить информацию о рисках за сегодня!\n\n"
        "Чтобы сообщить о новом риске, нажмите кнопку 'Сообщить риск' в меню бота.\n\n"
        "Ваши риски помогают проекту быть более управляемым и предсказуемым!"
    )

    for user_id in subscribed_users:
        try:
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT last_notification FROM subscriptions 
                    WHERE user_id = ? AND last_notification = ?
                """, (user_id, datetime.now().strftime("%Y-%m-%d")))
                already_notified = cursor.fetchone() is not None

            if not already_notified:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📢 Сообщить риск", callback_data="submit_risk")
                    ]])
                )
                with db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        UPDATE subscriptions SET last_notification = ? WHERE user_id = ?
                    """, (datetime.now().strftime("%Y-%m-%d"), user_id))
                    conn.commit()
                logger.info(f"✅ Напоминание отправлено пользователю {user_id}")
        except Exception as e:
            logger.error(f"❌ Ошибка при отправке напоминания пользователю {user_id}: {e}")

# === Обработчик переключения подписки ===
async def toggle_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    current_status = is_subscribed(user_id)

    if current_status:
        unsubscribe_user(user_id)
        message = "🔕 Вы отписаны от напоминаний."
    else:
        subscribe_user(user_id)
        message = "🔔 Вы подписаны на ежедневные напоминания!"

    await query.edit_message_text(message)
    await start(update, context)


async def view_risks_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отображает список рисков в текстовом сообщении и кнопки для выбора по номеру.
       Принимает фазу из callback_data или использует 'all' по умолчанию."""
    query = update.callback_query
    await query.answer()

    # Определяем фазу из callback_data
    # Формат callback_data: view_phase_<phase_name> или view_phase_all
    if query.data.startswith("view_phase_"):
        selected_phase = query.data.split("_", 2)[2]  # Разделяем максимум 2 раза
        context.user_data['selected_view_phase'] = selected_phase
    else:
        # Если вызвана напрямую (например, через кнопку "Назад" из деталей риска)
        # используем сохраненную фазу или "all"
        selected_phase = context.user_data.get('selected_view_phase', 'all')

    # Определяем максимальное количество рисков для отображения в одном сообщении
    MAX_RISKS_PER_MESSAGE = 30  # Можно настроить
    page = context.user_data.get('view_risks_page', 1)

    try:
        with db_connection() as conn:
            cursor = conn.cursor()

            # Формируем SQL-запрос в зависимости от выбранной фазы
            if selected_phase == "all":
                # Сначала получаем общее количество рисков
                cursor.execute("SELECT COUNT(*) as total FROM risks")
                total_risks = cursor.fetchone()['total']

                # Затем получаем риски для текущей страницы
                offset = (page - 1) * MAX_RISKS_PER_MESSAGE
                cursor.execute("""
                    SELECT id, description, risk_score, phase, object 
                    FROM risks 
                    ORDER BY risk_score DESC 
                    LIMIT ? OFFSET ?
                """, (MAX_RISKS_PER_MESSAGE, offset))
                risks = cursor.fetchall()
                phase_text = "всех фазах"
            else:
                # Сначала получаем общее количество рисков для фазы
                cursor.execute("SELECT COUNT(*) as total FROM risks WHERE phase = ?", (selected_phase,))
                total_risks = cursor.fetchone()['total']

                # Затем получаем риски для текущей страницы
                offset = (page - 1) * MAX_RISKS_PER_MESSAGE
                cursor.execute("""
                    SELECT id, description, risk_score, phase, object 
                    FROM risks 
                    WHERE phase = ?
                    ORDER BY risk_score DESC 
                    LIMIT ? OFFSET ?
                """, (selected_phase, MAX_RISKS_PER_MESSAGE, offset))
                risks = cursor.fetchall()
                phase_text = f"фазе '{selected_phase}'"

        if not risks:
            message = f"📭 Нет доступных рисков для {phase_text}."
            await query.edit_message_text(
                message,
                reply_markup=create_back_keyboard("back_to_view_phase_selection")
            )
            return

        # Вычисляем количество страниц
        total_pages = (total_risks + MAX_RISKS_PER_MESSAGE - 1) // MAX_RISKS_PER_MESSAGE

        # Формируем текстовое сообщение со списком рисков для текущей страницы
        if total_pages > 1:
            message_lines = [f"📋 **Список рисков ({phase_text}, страница {page}/{total_pages}):**\n"]
        else:
            message_lines = [f"📋 **Список всех рисков ({phase_text}):**\n"]
        message_lines.append(f"📈 Всего рисков: {total_risks}\n")

        # Добавляем риски в сообщение
        for risk in risks:
            # Форматируем строку списка
            message_lines.append(
                f"**#{risk['id']}** (Рейтинг: {risk['risk_score']})\n"
                f"   Фаза: {risk['phase']}\n"
                f"   Объект: {risk['object']}\n"
                f"   Описание: {risk['description'][:150]}{'...' if len(risk['description']) > 150 else ''}\n"
            )

        message = "\n".join(message_lines)

        # Создаем клавиатуру с кнопками для выбора риска по ID
        keyboard = create_risk_selection_keyboard(risks)

        # Добавляем кнопки навигации, если есть несколько страниц
        navigation_buttons = []
        if total_pages > 1:
            if page > 1:
                navigation_buttons.append(
                    InlineKeyboardButton("⬅️ Назад", callback_data=f"view_risks_page_{page - 1}_{selected_phase}"))
            if page < total_pages:
                navigation_buttons.append(
                    InlineKeyboardButton("➡️ Далее", callback_data=f"view_risks_page_{page + 1}_{selected_phase}"))

        if navigation_buttons:
            keyboard.append(navigation_buttons)

        # Добавляем кнопки управления внизу
        control_buttons = [
            [InlineKeyboardButton("🔄 Другая фаза", callback_data="view_risks_list")],  # Вернуться к выбору фазы
            [InlineKeyboardButton("🔙 Вернуться в меню", callback_data="back_to_menu")]
        ]
        keyboard.extend(control_buttons)

        reply_markup = InlineKeyboardMarkup(keyboard)
        # Используем parse_mode='Markdown' для форматирования
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"❌ Ошибка при получении списка рисков: {e}")
        await query.edit_message_text(
            "❌ Произошла ошибка при получении списка рисков. Пожалуйста, попробуйте позже.",
            reply_markup=create_back_keyboard("back_to_menu")
        )


async def view_risk_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает детали выбранного риска и позволяет добавить мероприятие"""
    query = update.callback_query
    await query.answer()
    risk_id = int(query.data.split("_")[2])

    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            # Получаем основную информацию о риске
            cursor.execute("SELECT * FROM risks WHERE id = ?", (risk_id,))
            risk = cursor.fetchone()

            if not risk:
                await query.edit_message_text(
                    "Риск не найден.",
                    reply_markup=create_back_keyboard("back_to_menu")
                )
                return

            # Получаем дополнительные мероприятия
            cursor.execute("""
                SELECT * FROM additional_mitigations 
                WHERE risk_id = ? 
                ORDER BY timestamp DESC
            """, (risk_id,))
            additional_mitigations = cursor.fetchall()

        # Формируем сообщение с деталями риска
        message = f"📋 Детали риска #{risk['id']} (рейтинг: {risk['risk_score']})\n\n"
        message += f"• Фаза: {risk['phase']}\n"
        message += f"• Объект: {risk['object']}\n"
        message += f"• Категория: {risk['category']}\n"
        message += f"• Описание: {risk['description']}\n\n"

        # Основное мероприятие (если есть)
        if risk['mitigation']:
            message += "📌 Основное мероприятие:\n"
            message += f"• {risk['mitigation']}\n"
            if risk['expected_result']:
                message += f"  Ожидаемый результат: {risk['expected_result']}\n\n"

        # Дополнительные мероприятия
        if additional_mitigations:
            message += "📌 Дополнительные мероприятия:\n"
            for i, mitigation in enumerate(additional_mitigations, 1):
                message += f"{i}. {mitigation['mitigation']}\n"
                if mitigation['expected_result']:
                    message += f"   Ожидаемый результат: {mitigation['expected_result']}\n"
                message += f"   Добавлено: {mitigation['username']} ({mitigation['timestamp'][:10]})\n\n"

        keyboard = [
            [InlineKeyboardButton("Добавить мероприятие", callback_data=f"add_mitigation_{risk_id}")],
            [InlineKeyboardButton("🔙 Вернуться к списку", callback_data="view_risks_list")],
            [InlineKeyboardButton("🏠 В главное меню", callback_data="back_to_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(message, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Ошибка при получении деталей риска: {e}")
        await query.edit_message_text(
            "Произошла ошибка при получении деталей риска.",
            reply_markup=create_back_keyboard("back_to_menu")
        )


async def add_mitigation_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начинает процесс добавления нового мероприятия к существующему риску"""
    query = update.callback_query
    await query.answer()
    risk_id = int(query.data.split("_")[2])

    context.user_data['adding_mitigation_to_risk'] = risk_id
    await query.edit_message_text(
        "Введите мероприятие по митигации риска:",
        reply_markup=create_back_keyboard("back_to_risk_details")
    )
    context.user_data['awaiting_mitigation_for_existing_risk'] = True


async def handle_view_risks_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает переход между страницами списка рисков"""
    query = update.callback_query
    await query.answer()

    # Извлекаем номер страницы и фазу из callback_data
    # Формат: view_risks_page_<page_number>_<phase> или view_risks_page_<page_number>_all
    parts = query.data.split("_", 3)  # Разделяем максимум 3 раза
    if len(parts) >= 4:
        page = int(parts[3])
        selected_phase = parts[4] if len(parts) > 4 else 'all'
    else:
        # На случай, если формат изменится, используем значения по умолчанию
        page = 1
        selected_phase = context.user_data.get('selected_view_phase', 'all')

    context.user_data['view_risks_page'] = page
    context.user_data['selected_view_phase'] = selected_phase

    # Вызываем функцию отображения списка рисков
    # Передаем update и context напрямую, так как view_risks_list ожидает callback_query
    # и сама определит фазу из context.user_data
    await view_risks_list(update, context)

async def handle_new_mitigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ввод нового мероприятия"""
    if not context.user_data.get('awaiting_mitigation_for_existing_risk'):
        return

    mitigation = update.message.text
    risk_id = context.user_data.get('adding_mitigation_to_risk')

    context.user_data['new_mitigation'] = mitigation
    context.user_data['awaiting_mitigation_for_existing_risk'] = False

    await update.message.reply_text(
        "Ожидаемый результат от мероприятия (опишите текстом):",
        reply_markup=create_back_keyboard("back_to_risk_details")
    )
    context.user_data['awaiting_expected_result_for_mitigation'] = True


async def handle_expected_result_for_mitigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ввод ожидаемого результата и сохраняет мероприятие"""
    if not context.user_data.get('awaiting_expected_result_for_mitigation'):
        return

    expected_result = update.message.text
    risk_id = context.user_data.get('adding_mitigation_to_risk')
    mitigation = context.user_data.get('new_mitigation')

    # Сохраняем в новую таблицу
    try:
        user_id = update.effective_user.id
        username = update.effective_user.username or update.effective_user.full_name
        timestamp = datetime.now().isoformat()

        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO additional_mitigations (
                    risk_id, user_id, username, mitigation, expected_result, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (risk_id, user_id, username, mitigation, expected_result, timestamp))
            conn.commit()

        # Очищаем данные
        context.user_data.pop('adding_mitigation_to_risk', None)
        context.user_data.pop('new_mitigation', None)
        context.user_data.pop('awaiting_expected_result_for_mitigation', None)

        # Возвращаемся к просмотру риска
        await update.message.reply_text(
            "✅ Мероприятие успешно добавлено!",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Посмотреть обновленный риск", callback_data=f"view_risk_{risk_id}")
            ]])
        )
    except Exception as e:
        logger.error(f"Ошибка при добавлении мероприятия: {e}")
        await update.message.reply_text(
            "Произошла ошибка при добавлении мероприятия.",
            reply_markup=create_back_keyboard("back_to_menu")
        )


# Добавляем функцию для возврата к деталям риска
async def back_to_risk_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    risk_id = context.user_data.get('adding_mitigation_to_risk', 0)

    if risk_id:
        await query.edit_message_text(
            "Введите мероприятие по митигации риска:",
            reply_markup=create_back_keyboard("back_to_risk_details")
        )
        context.user_data['awaiting_mitigation_for_existing_risk'] = True
    else:
        await start(update, context)

# === Главное меню ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO subscriptions (user_id, is_subscribed)
            VALUES (?, 1)
        """, (user_id,))
        conn.commit()
    is_subscribed_status = is_subscribed(user_id)
    subscription_text = "🔕 Отписаться" if is_subscribed_status else "🔔 Подписаться"

    # Добавляем пункт "Просмотреть риски"
    keyboard = [
        [InlineKeyboardButton("📢 Сообщить риск", callback_data="submit_risk")],
        [InlineKeyboardButton("🔍 Просмотреть риски/мероприятия", callback_data="view_risks_list")],
        [InlineKeyboardButton("📊 Отчёт по рискам", callback_data="report")],
        [InlineKeyboardButton("💾 Экспорт в Excel", callback_data="export_excel")],
        [InlineKeyboardButton(subscription_text, callback_data="toggle_subscription")]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.message:
        await update.message.reply_text("Добро пожаловать в Ежедневник рисков проекта!", reply_markup=reply_markup)
    else:
        await update.callback_query.edit_message_text("Добро пожалов в Ежедневник рисков проекта!",
                                                      reply_markup=reply_markup)


async def submit_risk_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton(phase, callback_data=f"phase_{phase}")] for phase in PHASES]
    keyboard.append([InlineKeyboardButton("📝 Ввести свою фазу", callback_data="custom_phase")])
    keyboard.append([InlineKeyboardButton("🔙 Вернуться в меню", callback_data="back_to_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("Выберите фазу проекта или введите свою:", reply_markup=reply_markup)


async def custom_phase_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Введите название фазы проекта:",
        reply_markup=create_back_keyboard("back_to_phase_selection")
    )
    context.user_data['awaiting_custom_phase'] = True


async def handle_custom_phase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_custom_phase'):
        return

    phase = update.message.text
    context.user_data['phase'] = phase
    context.user_data['awaiting_custom_phase'] = False

    keyboard = [[InlineKeyboardButton(obj, callback_data=f"object_{obj}")] for obj in OBJECTS]
    keyboard.append([InlineKeyboardButton("📝 Ввести свой объект", callback_data="custom_object")])
    keyboard.append([InlineKeyboardButton("🔙 Вернуться к фазе", callback_data="back_to_phase_selection")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(f"Фаза: {phase}\nВыберите объект или введите свой:", reply_markup=reply_markup)


async def choose_phase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    phase = query.data.split("_", 1)[1]
    context.user_data['phase'] = phase

    keyboard = [[InlineKeyboardButton(obj, callback_data=f"object_{obj}")] for obj in OBJECTS]
    keyboard.append([InlineKeyboardButton("📝 Ввести свой объект", callback_data="custom_object")])
    keyboard.append([InlineKeyboardButton("🔙 Вернуться к фазе", callback_data="back_to_phase_selection")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(f"Фаза: {phase}\nВыберите объект или введите свой:", reply_markup=reply_markup)


async def custom_object_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Введите название объекта:",
        reply_markup=create_back_keyboard("back_to_object_selection")
    )
    context.user_data['awaiting_custom_object'] = True


async def handle_custom_object(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_custom_object'):
        return

    obj = update.message.text
    context.user_data['object'] = obj
    context.user_data['awaiting_custom_object'] = False

    keyboard = [[InlineKeyboardButton(cat, callback_data=f"cat_{cat}")] for cat in CATEGORIES]
    keyboard.append(
        [InlineKeyboardButton("📝 Ввести свое функциональное направление риска", callback_data="custom_category")])
    keyboard.append([InlineKeyboardButton("🔙 Вернуться к объекту", callback_data="back_to_object_selection")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(f"Объект: {obj}\nВыберите функциональное направление риска или введите свою:",
                                    reply_markup=reply_markup)


async def choose_object(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    obj = query.data.split("_", 1)[1]
    context.user_data['object'] = obj

    keyboard = [[InlineKeyboardButton(cat, callback_data=f"cat_{cat}")] for cat in CATEGORIES]
    keyboard.append(
        [InlineKeyboardButton("📝 Ввести свое функциональное направление риска", callback_data="custom_category")])
    keyboard.append([InlineKeyboardButton("🔙 Вернуться к объекту", callback_data="back_to_object_selection")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(f"Объект: {obj}\nВыберите функциональное направление риска или введите свой:",
                                  reply_markup=reply_markup)


async def custom_category_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Введите функциональное направление риска:",
        reply_markup=create_back_keyboard("back_to_category_selection")
    )
    context.user_data['awaiting_custom_category'] = True


async def handle_custom_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_custom_category'):
        return

    cat = update.message.text
    context.user_data['category'] = cat
    context.user_data['awaiting_custom_category'] = False

    await update.message.reply_text(
        f"Категория: {cat}\n"
        "Опишите риск:",
        reply_markup=create_back_keyboard("back_to_category_selection")
    )
    context.user_data['awaiting_description'] = True


async def choose_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cat = query.data.split("_", 1)[1]
    context.user_data['category'] = cat

    await query.edit_message_text(
        f"Категория: {cat}\n"
        "Опишите риск:",
        reply_markup=create_back_keyboard("back_to_category_selection")
    )
    context.user_data['awaiting_description'] = True


async def get_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_description'):
        return

    description = update.message.text
    context.user_data['description'] = description
    context.user_data['awaiting_description'] = False

    # Начинаем оценку влияния на сроки
    keyboard = [
        [InlineKeyboardButton("Не влияет на сроки", callback_data="no_impact_schedule")],
        [InlineKeyboardButton("Оценить влияние на сроки", callback_data="evaluate_schedule_impact")],
        [InlineKeyboardButton("Запросить оценку (Повещенко С., Гиренко В.)",
                              callback_data="request_schedule_evaluation")],
        [InlineKeyboardButton("🔙 Вернуться к описанию", callback_data="back_to_description")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Влияние на сроки (оценка без мероприятий, в днях):\nВыберите вариант:",
        reply_markup=reply_markup
    )


async def no_impact_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Устанавливаем нулевые значения для влияния на сроки
    context.user_data['impact_schedule_min'] = 0
    context.user_data['impact_schedule_most_likely'] = 0
    context.user_data['impact_schedule_max'] = 0
    context.user_data['needs_schedule_evaluation'] = 0  # Не нужна оценка

    # Переходим к оценке влияния на стоимость
    keyboard = [
        [InlineKeyboardButton("Не влияет на стоимость", callback_data="no_impact_cost")],
        [InlineKeyboardButton("Оценить влияние на стоимость", callback_data="evaluate_cost_impact")],
        [InlineKeyboardButton("Запросить оценку (Давыдова Е., Борисов А.)", callback_data="request_cost_evaluation")],
        [InlineKeyboardButton("🔙 Вернуться к срокам", callback_data="back_to_schedule_impact")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "Влияние на стоимость (оценка без мероприятий, в млн.руб.):\nВыберите вариант:",
        reply_markup=reply_markup
    )


async def request_schedule_evaluation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Помечаем, что нужна оценка сроков
    context.user_data['needs_schedule_evaluation'] = 1
    # Устанавливаем нулевые значения для влияния на сроки (пока нет оценки)
    context.user_data['impact_schedule_min'] = 0
    context.user_data['impact_schedule_most_likely'] = 0
    context.user_data['impact_schedule_max'] = 0

    # Переходим к оценке влияния на стоимость
    keyboard = [
        [InlineKeyboardButton("Не влияет на стоимость", callback_data="no_impact_cost")],
        [InlineKeyboardButton("Оценить влияние на стоимость", callback_data="evaluate_cost_impact")],
        [InlineKeyboardButton("Запросить оценку (Давыдова Е., Борисов А.)", callback_data="request_cost_evaluation")],
        [InlineKeyboardButton("🔙 Вернуться к срокам", callback_data="back_to_schedule_impact")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "Влияние на стоимость (оценка без мероприятий, в млн.руб.):\nВыберите вариант:",
        reply_markup=reply_markup
    )


async def evaluate_schedule_impact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Помечаем, что оценка сроков не требуется (пользователь сам вводит)
    context.user_data['needs_schedule_evaluation'] = 0

    await query.edit_message_text(
        "Влияние на сроки (оценка без мероприятий):\nВведите минимальное, наиболее вероятное и максимальное влияние на сроки (в днях), разделенные пробелом, запятой или новой строкой:",
        reply_markup=create_back_keyboard("back_to_schedule_impact")
    )
    context.user_data['awaiting_schedule_values'] = True


# Обработчик для ввода всех трех значений влияния на сроки
async def handle_schedule_values(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_schedule_values'):
        return

    try:
        text = update.message.text
        min_val, most_likely_val, max_val = parse_three_values(text)

        # Проверка логики: min <= most_likely <= max
        if not (min_val <= most_likely_val <= max_val):
            await update.message.reply_text(
                "Ошибка: Значения должны быть в порядке возрастания (мин <= вероятно <= макс).\n"
                "Пожалуйста, введите значения снова:",
                reply_markup=create_back_keyboard("back_to_schedule_impact")
            )
            return

        context.user_data['impact_schedule_min'] = min_val
        context.user_data['impact_schedule_most_likely'] = most_likely_val
        context.user_data['impact_schedule_max'] = max_val
        context.user_data['awaiting_schedule_values'] = False

        # Переходим к оценке влияния на стоимость
        keyboard = [
            [InlineKeyboardButton("Не влияет на стоимость", callback_data="no_impact_cost")],
            [InlineKeyboardButton("Оценить влияние на стоимость", callback_data="evaluate_cost_impact")],
            [InlineKeyboardButton("Запросить оценку (Давыдова Е., Борисов А.)",
                                  callback_data="request_cost_evaluation")],
            [InlineKeyboardButton("🔙 Вернуться к срокам", callback_data="back_to_schedule_impact")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "Влияние на стоимость (оценка без мероприятий):\nВыберите вариант:",
            reply_markup=reply_markup
        )
    except ValueError as e:
        await update.message.reply_text(
            f"{str(e)}\n"
            "Пожалуйста, введите три числа, разделенные пробелом, запятой или новой строкой:",
            reply_markup=create_back_keyboard("back_to_schedule_impact")
        )


async def no_impact_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Устанавливаем нулевые значения для влияния на стоимость
    context.user_data['impact_cost_min'] = 0
    context.user_data['impact_cost_most_likely'] = 0
    context.user_data['impact_cost_max'] = 0
    context.user_data['needs_cost_evaluation'] = 0  # Не нужна оценка

    # Переходим к оценке вероятности
    keyboard = [
        [InlineKeyboardButton("<10%", callback_data="prob_1")],
        [InlineKeyboardButton("10-20%", callback_data="prob_2")],
        [InlineKeyboardButton("20-50%", callback_data="prob_3")],
        [InlineKeyboardButton("50-75%", callback_data="prob_4")],
        [InlineKeyboardButton(">75%", callback_data="prob_5")],
        [InlineKeyboardButton("📝 Ввести свою вероятность", callback_data="custom_probability")],
        [InlineKeyboardButton("🔙 Вернуться к стоимости", callback_data="back_to_cost_impact")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text("Оцените вероятность возникновения риска или введите свою:",
                                  reply_markup=reply_markup)


async def request_cost_evaluation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Помечаем, что нужна оценка стоимости
    context.user_data['needs_cost_evaluation'] = 1
    # Устанавливаем нулевые значения для влияния на стоимость (пока нет оценки)
    context.user_data['impact_cost_min'] = 0
    context.user_data['impact_cost_most_likely'] = 0
    context.user_data['impact_cost_max'] = 0

    # Переходим к оценке вероятности
    keyboard = [
        [InlineKeyboardButton("<10%", callback_data="prob_1")],
        [InlineKeyboardButton("10-20%", callback_data="prob_2")],
        [InlineKeyboardButton("20-50%", callback_data="prob_3")],
        [InlineKeyboardButton("50-75%", callback_data="prob_4")],
        [InlineKeyboardButton(">75%", callback_data="prob_5")],
        [InlineKeyboardButton("📝 Ввести свою вероятность", callback_data="custom_probability")],
        [InlineKeyboardButton("🔙 Вернуться к стоимости", callback_data="back_to_cost_impact")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text("Оцените вероятность возникновения риска или введите свою:",
                                  reply_markup=reply_markup)


async def evaluate_cost_impact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Помечаем, что оценка стоимости не требуется (пользователь сам вводит)
    context.user_data['needs_cost_evaluation'] = 0

    await query.edit_message_text(
        "Влияние на стоимость (оценка без мероприятий):\nВведите минимальное, наиболее вероятное и максимальное влияние на стоимость (в млн. руб.), разделенные пробелом, запятой или новой строкой:",
        reply_markup=create_back_keyboard("back_to_cost_impact")
    )
    context.user_data['awaiting_cost_values'] = True


# Обработчик для ввода всех трех значений влияния на стоимость
async def handle_cost_values(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_cost_values'):
        return

    try:
        text = update.message.text
        min_val, most_likely_val, max_val = parse_three_values(text)

        # Проверка логики: min <= most_likely <= max
        if not (min_val <= most_likely_val <= max_val):
            await update.message.reply_text(
                "Ошибка: Значения должны быть в порядке возрастания (мин <= вероятно <= макс).\n"
                "Пожалуйста, введите значения снова:",
                reply_markup=create_back_keyboard("back_to_cost_impact")
            )
            return

        context.user_data['impact_cost_min'] = min_val
        context.user_data['impact_cost_most_likely'] = most_likely_val
        context.user_data['impact_cost_max'] = max_val
        context.user_data['awaiting_cost_values'] = False

        # Переходим к оценке вероятности
        keyboard = [
            [InlineKeyboardButton("<10%", callback_data="prob_1")],
            [InlineKeyboardButton("10-20%", callback_data="prob_2")],
            [InlineKeyboardButton("20-50%", callback_data="prob_3")],
            [InlineKeyboardButton("50-75%", callback_data="prob_4")],
            [InlineKeyboardButton(">75%", callback_data="prob_5")],
            [InlineKeyboardButton("📝 Ввести свою вероятность", callback_data="custom_probability")],
            [InlineKeyboardButton("🔙 Вернуться к стоимости", callback_data="back_to_cost_impact")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text("Оцените вероятность возникновения риска или введите свою:",
                                        reply_markup=reply_markup)
    except ValueError as e:
        await update.message.reply_text(
            f"{str(e)}\n"
            "Пожалуйста, введите три числа, разделенные пробелом, запятой или новой строкой:",
            reply_markup=create_back_keyboard("back_to_cost_impact")
        )


async def custom_probability_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "Введите оценку вероятности (в процентах, от 0 до 100):",
        reply_markup=create_back_keyboard("back_to_probability")
    )
    context.user_data['awaiting_custom_probability'] = True


async def handle_custom_probability(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_custom_probability'):
        return

    try:
        prob = int(update.message.text)
        if prob < 0 or prob > 100:
            await update.message.reply_text(
                "Пожалуйста, введите число от 0 до 100:",
                reply_markup=create_back_keyboard("back_to_probability")
            )
            return

        context.user_data['probability'] = prob
        context.user_data['awaiting_custom_probability'] = False

        await update.message.reply_text(
            "Укажите примерный период реализации риска:",
            reply_markup=create_back_keyboard("back_to_probability")
        )
        context.user_data['awaiting_timeline'] = True
    except ValueError:
        await update.message.reply_text(
            "Пожалуйста, введите число от 0 до 100:",
            reply_markup=create_back_keyboard("back_to_probability")
        )


async def choose_probability(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("prob_re_"):
        return  # Пусть обработает choose_probability_re

    try:
        prob_level = int(query.data.split("_")[1])  # <- Строка 1109, должна быть с отступом
        if prob_level not in PROBABILITY_MAPPING:
            await query.answer("Неверный уровень вероятности", show_alert=True)
            return
        context.user_data['probability'] = PROBABILITY_MAPPING[prob_level]
        await query.edit_message_text(
            "Укажите примерный период реализации риска:",
            reply_markup=create_back_keyboard("back_to_probability")
        )
        context.user_data['awaiting_timeline'] = True
    except (IndexError, ValueError):
        await query.answer("Ошибка обработки данных", show_alert=True)
        return


async def handle_timeline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_timeline'):
        return

    timeline = update.message.text
    context.user_data['timeline'] = timeline
    context.user_data['awaiting_timeline'] = False

    # Переходим к выбору действия по митигации
    keyboard = [
        [InlineKeyboardButton("Добавить мероприятие по митигации и внести оценку",
                              callback_data="add_mitigation_with_evaluation")],
        [InlineKeyboardButton("Заполнить мероприятие по митигации без переоценки",
                              callback_data="add_mitigation_without_evaluation")],
        [InlineKeyboardButton("🔙 Вернуться к сроку", callback_data="back_to_timeline")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Выберите действие:",
        reply_markup=reply_markup
    )


# === Обработчики митигации ===

async def add_mitigation_with_evaluation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Помечаем, что нужна переоценка
    context.user_data['reevaluation_needed'] = 1

    await query.edit_message_text(
        "Введите мероприятие по митигации риска:",
        reply_markup=create_back_keyboard("back_to_mitigation_choice")
    )
    context.user_data['awaiting_mitigation'] = True


async def add_mitigation_without_evaluation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Помечаем, что переоценка не нужна
    context.user_data['reevaluation_needed'] = 0

    await query.edit_message_text(
        "Введите мероприятие по митигации риска:",
        reply_markup=create_back_keyboard("back_to_mitigation_choice")
    )
    context.user_data['awaiting_mitigation'] = True


async def handle_mitigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_mitigation'):
        return

    mitigation = update.message.text
    context.user_data['mitigation'] = mitigation
    context.user_data['awaiting_mitigation'] = False

    await update.message.reply_text(
        "Ожидаемый результат от мероприятия (опишите текстом):",
        reply_markup=create_back_keyboard("back_to_mitigation")
    )
    context.user_data['awaiting_expected_result'] = True


async def handle_expected_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_expected_result'):
        return

    expected_result = update.message.text
    context.user_data['expected_result'] = expected_result
    context.user_data['awaiting_expected_result'] = False

    # Проверяем, нужна ли переоценка
    if context.user_data.get('reevaluation_needed', 0) == 1:
        # Начинаем переоценку влияния на сроки (с мероприятиями)
        keyboard = [
            [InlineKeyboardButton("Не влияет на сроки", callback_data="no_impact_schedule_re")],
            [InlineKeyboardButton("Оценить влияние на сроки", callback_data="evaluate_schedule_impact_re")],
            [InlineKeyboardButton("Запросить оценку (Повещенко С., Гиренко В.)",
                                  callback_data="request_schedule_evaluation_re")],
            [InlineKeyboardButton("🔙 Вернуться к выбору", callback_data="back_to_mitigation_result")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "Влияние на сроки (оценка с мероприятиями, в днях):\nВыберите вариант:",
            reply_markup=reply_markup
        )
    else:
        # Если переоценка не нужна, сразу сохраняем риск
        await save_risk(update, context)


# === Обработчики переоценки (с мероприятиями) ===

async def no_impact_schedule_re(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Устанавливаем нулевые значения для влияния на сроки при переоценке
    context.user_data['impact_schedule_min_re'] = 0
    context.user_data['impact_schedule_most_likely_re'] = 0
    context.user_data['impact_schedule_max_re'] = 0
    context.user_data['needs_schedule_evaluation_re'] = 0

    # Переходим к оценке влияния на стоимость
    keyboard = [
        [InlineKeyboardButton("Не влияет на стоимость", callback_data="no_impact_cost_re")],
        [InlineKeyboardButton("Оценить влияние на стоимость", callback_data="evaluate_cost_impact_re")],
        [InlineKeyboardButton("Запросить оценку (Давыдова Е., Борисов А.)",
                              callback_data="request_cost_evaluation_re")],
        [InlineKeyboardButton("🔙 Вернуться к срокам", callback_data="back_to_schedule_impact_re")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "Влияние на стоимость (оценка с мероприятиями):\nВыберите вариант:",
        reply_markup=reply_markup
    )


async def request_schedule_evaluation_re(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Помечаем, что нужна оценка сроков при переоценке
    context.user_data['needs_schedule_evaluation_re'] = 1
    # Устанавливаем нулевые значения для влияния на сроки (пока нет оценки)
    context.user_data['impact_schedule_min_re'] = 0
    context.user_data['impact_schedule_most_likely_re'] = 0
    context.user_data['impact_schedule_max_re'] = 0

    # Переходим к оценке влияния на стоимость
    keyboard = [
        [InlineKeyboardButton("Не влияет на стоимость", callback_data="no_impact_cost_re")],
        [InlineKeyboardButton("Оценить влияние на стоимость", callback_data="evaluate_cost_impact_re")],
        [InlineKeyboardButton("Запросить оценку (Давыдова Е., Борисов А.)",
                              callback_data="request_cost_evaluation_re")],
        [InlineKeyboardButton("🔙 Вернуться к срокам", callback_data="back_to_schedule_impact_re")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "Влияние на стоимость (оценка с мероприятиями):\nВыберите вариант:",
        reply_markup=reply_markup
    )


async def evaluate_schedule_impact_re(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Помечаем, что оценка сроков не требуется (пользователь сам вводит)
    context.user_data['needs_schedule_evaluation_re'] = 0

    await query.edit_message_text(
        "Влияние на сроки (оценка с мероприятиями):\nВведите минимальное, наиболее вероятное и максимальное влияние на сроки (в днях), разделенные пробелом, запятой или новой строкой:",
        reply_markup=create_back_keyboard("back_to_schedule_impact_re")
    )
    context.user_data['awaiting_schedule_values_re'] = True


async def handle_schedule_values_re(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_schedule_values_re'):
        return

    try:
        text = update.message.text
        min_val, most_likely_val, max_val = parse_three_values(text)

        # Проверка логики: min <= most_likely <= max
        if not (min_val <= most_likely_val <= max_val):
            await update.message.reply_text(
                "Ошибка: Значения должны быть в порядке возрастания (мин <= вероятно <= макс).\n"
                "Пожалуйста, введите значения снова:",
                reply_markup=create_back_keyboard("back_to_schedule_impact_re")
            )
            return

        context.user_data['impact_schedule_min_re'] = min_val
        context.user_data['impact_schedule_most_likely_re'] = most_likely_val
        context.user_data['impact_schedule_max_re'] = max_val
        context.user_data['awaiting_schedule_values_re'] = False

        # Переходим к оценке влияния на стоимость
        keyboard = [
            [InlineKeyboardButton("Не влияет на стоимость", callback_data="no_impact_cost_re")],
            [InlineKeyboardButton("Оценить влияние на стоимость", callback_data="evaluate_cost_impact_re")],
            [InlineKeyboardButton("Запросить оценку (Давыдова Е., Борисов А.)",
                                  callback_data="request_cost_evaluation_re")],
            [InlineKeyboardButton("🔙 Вернуться к срокам", callback_data="back_to_schedule_impact_re")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "Влияние на стоимость (оценка с мероприятиями):\nВыберите вариант:",
            reply_markup=reply_markup
        )
    except ValueError as e:
        await update.message.reply_text(
            f"{str(e)}\n"
            "Пожалуйста, введите три числа, разделенные пробелом, запятой или новой строкой:",
            reply_markup=create_back_keyboard("back_to_schedule_impact_re")
        )


async def no_impact_cost_re(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Устанавливаем нулевые значения для влияния на стоимость при переоценке
    context.user_data['impact_cost_min_re'] = 0
    context.user_data['impact_cost_most_likely_re'] = 0
    context.user_data['impact_cost_max_re'] = 0
    context.user_data['needs_cost_evaluation_re'] = 0

    # Переходим к оценке вероятности после мероприятий
    keyboard = [
        [InlineKeyboardButton("<10%", callback_data="prob_re_1")],
        [InlineKeyboardButton("10-20%", callback_data="prob_re_2")],
        [InlineKeyboardButton("20-50%", callback_data="prob_re_3")],
        [InlineKeyboardButton("50-75%", callback_data="prob_re_4")],
        [InlineKeyboardButton(">75%", callback_data="prob_re_5")],
        [InlineKeyboardButton("📝 Ввести свою вероятность", callback_data="custom_probability_re")],
        [InlineKeyboardButton("🔙 Вернуться к стоимости", callback_data="back_to_cost_impact_re")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text("Оцените вероятность возникновения риска после мероприятий или введите свою:",
                                  reply_markup=reply_markup)


async def request_cost_evaluation_re(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Помечаем, что нужна оценка стоимости при переоценке
    context.user_data['needs_cost_evaluation_re'] = 1
    # Устанавливаем нулевые значения для влияния на стоимость (пока нет оценки)
    context.user_data['impact_cost_min_re'] = 0
    context.user_data['impact_cost_most_likely_re'] = 0
    context.user_data['impact_cost_max_re'] = 0

    # Переходим к оценке вероятности после мероприятий
    keyboard = [
        [InlineKeyboardButton("<10%", callback_data="prob_re_1")],
        [InlineKeyboardButton("10-20%", callback_data="prob_re_2")],
        [InlineKeyboardButton("20-50%", callback_data="prob_re_3")],
        [InlineKeyboardButton("50-75%", callback_data="prob_re_4")],
        [InlineKeyboardButton(">75%", callback_data="prob_re_5")],
        [InlineKeyboardButton("📝 Ввести свою вероятность", callback_data="custom_probability_re")],
        [InlineKeyboardButton("🔙 Вернуться к стоимости", callback_data="back_to_cost_impact_re")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text("Оцените вероятность возникновения риска после мероприятий или введите свою:",
                                  reply_markup=reply_markup)


async def evaluate_cost_impact_re(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Помечаем, что оценка стоимости не требуется (пользователь сам вводит)
    context.user_data['needs_cost_evaluation_re'] = 0

    await query.edit_message_text(
        "Влияние на стоимость (оценка с мероприятиями):\nВведите минимальное, наиболее вероятное и максимальное влияние на стоимость (в млн. руб.), разделенные пробелом, запятой или новой строкой:",
        reply_markup=create_back_keyboard("back_to_cost_impact_re")
    )
    context.user_data['awaiting_cost_values_re'] = True


async def handle_cost_values_re(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_cost_values_re'):
        return

    try:
        text = update.message.text
        min_val, most_likely_val, max_val = parse_three_values(text)

        # Проверка логики: min <= most_likely <= max
        if not (min_val <= most_likely_val <= max_val):
            await update.message.reply_text(
                "Ошибка: Значения должны быть в порядке возрастания (мин <= вероятно <= макс).\n"
                "Пожалуйста, введите значения снова:",
                reply_markup=create_back_keyboard("back_to_cost_impact_re")
            )
            return

        context.user_data['impact_cost_min_re'] = min_val
        context.user_data['impact_cost_most_likely_re'] = most_likely_val
        context.user_data['impact_cost_max_re'] = max_val
        context.user_data['awaiting_cost_values_re'] = False

        # Переходим к оценке вероятности после мероприятий
        keyboard = [
            [InlineKeyboardButton("<10%", callback_data="prob_re_1")],
            [InlineKeyboardButton("10-20%", callback_data="prob_re_2")],
            [InlineKeyboardButton("20-50%", callback_data="prob_re_3")],
            [InlineKeyboardButton("50-75%", callback_data="prob_re_4")],
            [InlineKeyboardButton(">75%", callback_data="prob_re_5")],
            [InlineKeyboardButton("📝 Ввести свою вероятность", callback_data="custom_probability_re")],
            [InlineKeyboardButton("🔙 Вернуться к стоимости", callback_data="back_to_cost_impact_re")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text("Оцените вероятность возникновения риска после мероприятий или введите свою:",
                                        reply_markup=reply_markup)
    except ValueError as e:
        await update.message.reply_text(
            f"{str(e)}\n"
            "Пожалуйста, введите три числа, разделенные пробелом, запятой или новой строкой:",
            reply_markup=create_back_keyboard("back_to_cost_impact_re")
        )


async def custom_probability_re_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "Введите оценку вероятности после мероприятий (в процентах, от 0 до 100):",
        reply_markup=create_back_keyboard("back_to_probability_re")
    )
    context.user_data['awaiting_custom_probability_re'] = True


async def select_view_phase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Позволяет пользователю выбрать фазу для просмотра рисков"""
    query = update.callback_query
    await query.answer()

    # Создаем кнопки для выбора фазы
    keyboard = []
    # Добавляем фазы из конфигурации
    for phase in PHASES:
        keyboard.append([InlineKeyboardButton(phase, callback_data=f"view_phase_{phase}")])

    # Добавляем опцию "Все фазы"
    keyboard.append([InlineKeyboardButton("Все фазы", callback_data="view_phase_all")])
    # Добавляем кнопку "Назад" в главное меню
    keyboard.append([InlineKeyboardButton("🔙 Вернуться в меню", callback_data="back_to_menu")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("Выберите фазу для просмотра рисков:", reply_markup=reply_markup)

async def handle_custom_probability_re(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_custom_probability_re'):
        return

    try:
        prob = int(update.message.text)
        if prob < 0 or prob > 100:
            await update.message.reply_text(
                "Пожалуйста, введите число от 0 до 100:",
                reply_markup=create_back_keyboard("back_to_probability_re")
            )
            return

        context.user_data['probability_re'] = prob
        context.user_data['awaiting_custom_probability_re'] = False

        # Сохраняем риск после завершения переоценки
        await save_risk(update, context)
    except ValueError:
        await update.message.reply_text(
            "Пожалуйста, введите число от 0 до 100:",
            reply_markup=create_back_keyboard("back_to_probability_re")
        )


async def choose_probability_re(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        # Формат: prob_re_1 → split → ['prob', 're', '1']
        parts = query.data.split("_")
        if len(parts) != 3 or parts[0] != "prob" or parts[1] != "re":
            await query.answer("Неверный формат данных", show_alert=True)
            return
        prob_level = int(parts[2])
        if prob_level not in PROBABILITY_MAPPING:
            await query.answer("Неверный уровень вероятности", show_alert=True)
            return
        context.user_data['probability_re'] = PROBABILITY_MAPPING[prob_level]
        await save_risk(update, context)
    except (IndexError, ValueError):
        await query.answer("Ошибка обработки данных", show_alert=True)
        return


# === Функция сохранения риска ===
async def save_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Получаем данные для расчета
    probability = context.user_data.get('probability', 0)
    impact_cost_most_likely = context.user_data.get('impact_cost_most_likely', 0)
    impact_schedule_most_likely = context.user_data.get('impact_schedule_most_likely', 0)

    # Преобразуем в 5-балльную шкалу
    cost_score = get_cost_score(impact_cost_most_likely)
    schedule_score = get_schedule_score(impact_schedule_most_likely)
    probability_score = get_probability_score(probability)

    # Расчет риска по новой формуле
    risk_score = cost_score + schedule_score + probability_score
    context.user_data['risk_score'] = risk_score

    # Получаем данные пользователя
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.full_name

    # Сохраняем в базу данных
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO risks (
                    user_id, username, phase, object, category, description,
                    impact_schedule_min, impact_schedule_most_likely, impact_schedule_max,
                    impact_cost_min, impact_cost_most_likely, impact_cost_max,
                    impact_schedule_min_re, impact_schedule_most_likely_re, impact_schedule_max_re,
                    impact_cost_min_re, impact_cost_most_likely_re, impact_cost_max_re,
                    probability, probability_re, risk_score, timeline, mitigation, expected_result,
                    reevaluation_needed, needs_schedule_evaluation, needs_cost_evaluation,
                    needs_schedule_evaluation_re, needs_cost_evaluation_re, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id, username,
                context.user_data.get('phase'),
                context.user_data.get('object'),
                context.user_data.get('category'),
                context.user_data.get('description'),
                context.user_data.get('impact_schedule_min', 0),
                context.user_data.get('impact_schedule_most_likely', 0),
                context.user_data.get('impact_schedule_max', 0),
                context.user_data.get('impact_cost_min', 0),
                context.user_data.get('impact_cost_most_likely', 0),
                context.user_data.get('impact_cost_max', 0),
                context.user_data.get('impact_schedule_min_re', 0),
                context.user_data.get('impact_schedule_most_likely_re', 0),
                context.user_data.get('impact_schedule_max_re', 0),
                context.user_data.get('impact_cost_min_re', 0),
                context.user_data.get('impact_cost_most_likely_re', 0),
                context.user_data.get('impact_cost_max_re', 0),
                context.user_data.get('probability', 0),
                context.user_data.get('probability_re', 0),
                risk_score,
                context.user_data.get('timeline'),
                context.user_data.get('mitigation'),
                context.user_data.get('expected_result'),
                context.user_data.get('reevaluation_needed', 0),
                context.user_data.get('needs_schedule_evaluation', 0),
                context.user_data.get('needs_cost_evaluation', 0),
                context.user_data.get('needs_schedule_evaluation_re', 0),
                context.user_data.get('needs_cost_evaluation_re', 0),
                datetime.now().isoformat()
            ))
            conn.commit()
    except Exception as e:
        logger.error(f"Ошибка при сохранении риска: {e}")
        await update.message.reply_text(
            "Произошла ошибка при сохранении риска. Пожалуйста, попробуйте снова.",
            reply_markup=create_back_keyboard("back_to_menu")
        )
        return

    # Формируем сообщение с результатом
    message = "✅ Риск успешно сохранен!\n\n"
    message += f"📊 Рейтинг риска: {risk_score}\n\n"
    message += f"📋 Детали:\n"
    message += f"• Фаза: {context.user_data.get('phase')}\n"
    message += f"• Объект: {context.user_data.get('object')}\n"
    message += f"• Категория: {context.user_data.get('category')}\n"
    message += f"• Описание: {context.user_data.get('description')}\n"
    message += f"• Влияние на сроки: {context.user_data.get('impact_schedule_min', 0)} / {context.user_data.get('impact_schedule_most_likely', 0)} / {context.user_data.get('impact_schedule_max', 0)} дней\n"
    message += f"• Влияние на стоимость: {context.user_data.get('impact_cost_min', 0)} / {context.user_data.get('impact_cost_most_likely', 0)} / {context.user_data.get('impact_cost_max', 0)} млн. руб.\n"
    message += f"• Вероятность: {probability}%\n"
    message += f"• Срок реализации: {context.user_data.get('timeline')}\n"
    message += f"• Мероприятие: {context.user_data.get('mitigation')}\n"
    message += f"• Ожидаемый результат: {context.user_data.get('expected_result')}\n"

    if context.user_data.get('reevaluation_needed', 0) == 1:
        message += f"\n📈 После мероприятий:\n"
        message += f"• Влияние на сроки: {context.user_data.get('impact_schedule_min_re', 0)} / {context.user_data.get('impact_schedule_most_likely_re', 0)} / {context.user_data.get('impact_schedule_max_re', 0)} дней\n"
        message += f"• Влияние на стоимость: {context.user_data.get('impact_cost_min_re', 0)} / {context.user_data.get('impact_cost_most_likely_re', 0)} / {context.user_data.get('impact_cost_max_re', 0)} млн. руб.\n"
        message += f"• Вероятность: {context.user_data.get('probability_re', 0)}%\n"

    keyboard = [[InlineKeyboardButton("🏠 В главное меню", callback_data="back_to_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(message, reply_markup=reply_markup)
    else:
        await update.message.reply_text(message, reply_markup=reply_markup)


# === Обработчики отчетов ===
async def show_phase_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    phase_data = query.data

    # Извлекаем фазу из callback_data
    if phase_data == "report_phase_all":
        phase = None
    else:
        phase = phase_data.replace("report_phase_", "")

    try:
        with db_connection() as conn:
            cursor = conn.cursor()

            # Формируем условие для SQL-запроса
            where_clause = ""
            params = []
            if phase:
                where_clause = "WHERE phase = ?"
                params = [phase]

            # Получаем общее количество рисков
            cursor.execute(f"SELECT COUNT(*) as total FROM risks {where_clause}", params)
            total_risks = cursor.fetchone()['total']

            # Получаем количество рисков по категории
            if where_clause:
                cursor.execute(f"""
                    SELECT category, COUNT(*) as count 
                    FROM risks 
                    {where_clause}
                    GROUP BY category 
                    ORDER BY count DESC
                """, params)
            else:
                cursor.execute("""
                    SELECT category, COUNT(*) as count 
                    FROM risks 
                    GROUP BY category 
                    ORDER BY count DESC
                """)
            categories = cursor.fetchall()

            # Получаем топ-5 рисков по рейтингу
            if where_clause:
                cursor.execute(f"""
                    SELECT description, risk_score 
                    FROM risks 
                    {where_clause}
                    ORDER BY risk_score DESC 
                    LIMIT 5
                """, params)
            else:
                cursor.execute("""
                    SELECT description, risk_score 
                    FROM risks 
                    ORDER BY risk_score DESC 
                    LIMIT 5
                """)
            top_risks = cursor.fetchall()

            # Формируем заголовок отчета в зависимости от фазы
            if phase:
                message = f"📊 Отчет по рискам (фаза: {phase})\n"
            else:
                message = "📊 Отчет по рискам (все фазы)\n"

            message += f"📈 Всего рисков: {total_risks}\n"
            message += "🏷️ Распределение по категориям:\n"
            for category in categories:
                message += f"• {category['category']}: {category['count']}\n"
            message += "\n🔥 Топ-5 рисков по рейтингу:\n"
            for i, risk in enumerate(top_risks, 1):
                # Обрезаем описание до 30 символов, чтобы не было слишком длинным
                desc = risk['description'][:200] + "..." if len(risk['description']) > 200 else risk['description']
                message += f"{i}. {desc} - {risk['risk_score']}\n"

            keyboard = [
                [InlineKeyboardButton("🔄 Выбрать другую фазу", callback_data="select_report_phase")],
                [InlineKeyboardButton("🔙 Вернуться в меню", callback_data="back_to_menu")],
                [InlineKeyboardButton("💾 Экспорт в Excel", callback_data="export_excel")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(message, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Ошибка при формировании отчета: {e}")
        await query.edit_message_text(
            "Произошла ошибка при формировании отчета. Пожалуйста, попробуйте позже.",
            reply_markup=create_back_keyboard("back_to_menu")
        )


async def export_excel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    message_to_delete = await query.edit_message_text("🔄 Создание Excel файла...")

    filename = export_to_excel()
    if filename:
        try:
            with open(filename, 'rb') as file:
                await context.bot.send_document(
                    chat_id=query.message.chat_id,
                    document=file,
                    caption="📊 Экспорт данных по рискам"
                )
        except Exception as e:
            logger.error(f"Ошибка при отправке файла: {e}")
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="Произошла ошибка при отправке файла.",
                reply_markup=create_back_keyboard("back_to_menu")
            )
    else:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Произошла ошибка при создании файла.",
            reply_markup=create_back_keyboard("back_to_menu")
        )

    # Удаляем временное сообщение
    try:
        await context.bot.delete_message(
            chat_id=query.message.chat_id,
            message_id=message_to_delete.message_id
        )
    except Exception as e:
        logger.warning(f"Не удалось удалить сообщение: {e}")

    # Отправляем главное меню как новое сообщение
    await start(update, context)


# === Обработчики кнопки "Назад" ===
async def handle_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    back_data = query.data

    if back_data == "back_to_menu":
        await start(update, context)

    elif back_data == "back_to_phase_selection":
        # Очищаем данные о фазе и возвращаемся к выбору фазы
        context.user_data.pop('phase', None)
        context.user_data.pop('awaiting_custom_phase', None)
        await submit_risk_start(update, context)

    elif back_data == "toggle_subscription":
        await toggle_subscription(update, context)

    elif back_data == "back_to_object_selection":
        # Очищаем данные об объекте и возвращаемся к выбору объекта
        context.user_data.pop('object', None)
        context.user_data.pop('awaiting_custom_object', None)
        phase = context.user_data.get('phase')

        keyboard = [[InlineKeyboardButton(obj, callback_data=f"object_{obj}")] for obj in OBJECTS]
        keyboard.append([InlineKeyboardButton("📝 Ввести свой объект", callback_data="custom_object")])
        keyboard.append([InlineKeyboardButton("🔙 Вернуться к фазе", callback_data="back_to_phase_selection")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(f"Фаза: {phase}\nВыберите объект или введите свой:", reply_markup=reply_markup)

    elif back_data == "back_to_view_phase_selection":
        # Очищаем выбранные данные и возвращаемся к выбору фазы
        context.user_data.pop('selected_view_phase', None)
        context.user_data.pop('view_risks_page', None)
        await select_view_phase(update, context)

    elif back_data == "back_to_category_selection":
        # Очищаем данные о категории и возвращаемся к выбору категории
        context.user_data.pop('category', None)
        context.user_data.pop('awaiting_custom_category', None)
        obj = context.user_data.get('object')

        keyboard = [[InlineKeyboardButton(cat, callback_data=f"cat_{cat}")] for cat in CATEGORIES]
        keyboard.append(
            [InlineKeyboardButton("📝 Ввести свое функциональное направление риска", callback_data="custom_category")])
        keyboard.append([InlineKeyboardButton("🔙 Вернуться к объекту", callback_data="back_to_object_selection")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(f"Объект: {obj}\nВыберите функциональное направление риска или введите свой:",
                                      reply_markup=reply_markup)

    elif back_data == "back_to_description":
        # Очищаем описание и возвращаемся к вводу описания
        context.user_data.pop('description', None)
        context.user_data.pop('awaiting_description', None)
        cat = context.user_data.get('category')

        await query.edit_message_text(
            f"Категория: {cat}\n"
            "Опишите риск:",
            reply_markup=create_back_keyboard("back_to_category_selection")
        )
        context.user_data['awaiting_description'] = True

    elif back_data == "back_to_schedule_impact":
        # Очищаем данные о влиянии на сроки и возвращаемся к выбору
        context.user_data.pop('impact_schedule_min', None)
        context.user_data.pop('impact_schedule_most_likely', None)
        context.user_data.pop('impact_schedule_max', None)
        context.user_data.pop('needs_schedule_evaluation', None)
        context.user_data.pop('awaiting_schedule_values', None)

        keyboard = [
            [InlineKeyboardButton("Не влияет на сроки", callback_data="no_impact_schedule")],
            [InlineKeyboardButton("Оценить влияние на сроки", callback_data="evaluate_schedule_impact")],
            [InlineKeyboardButton("Запросить оценку (Повещенко С., Гиренко В.)",
                                  callback_data="request_schedule_evaluation")],
            [InlineKeyboardButton("🔙 Вернуться к описанию", callback_data="back_to_description")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            "Влияние на сроки (оценка без мероприятий):\nВыберите вариант:",
            reply_markup=reply_markup
        )

    elif back_data == "back_to_cost_impact":
        # Очищаем данные о влиянии на стоимость и возвращаемся к выбору
        context.user_data.pop('impact_cost_min', None)
        context.user_data.pop('impact_cost_most_likely', None)
        context.user_data.pop('impact_cost_max', None)
        context.user_data.pop('needs_cost_evaluation', None)
        context.user_data.pop('awaiting_cost_values', None)

        keyboard = [
            [InlineKeyboardButton("Не влияет на стоимость", callback_data="no_impact_cost")],
            [InlineKeyboardButton("Оценить влияние на стоимость", callback_data="evaluate_cost_impact")],
            [InlineKeyboardButton("Запросить оценку (Давыдова Е., Борисов А.)",
                                  callback_data="request_cost_evaluation")],
            [InlineKeyboardButton("🔙 Вернуться к срокам", callback_data="back_to_schedule_impact")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            "Влияние на стоимость (оценка без мероприятий):\nВыберите вариант:",
            reply_markup=reply_markup
        )

    elif back_data == "back_to_probability":
        # Очищаем данные о вероятности и возвращаемся к выбору
        context.user_data.pop('probability', None)
        context.user_data.pop('awaiting_custom_probability', None)

        keyboard = [
            [InlineKeyboardButton("<10%", callback_data="prob_1")],
            [InlineKeyboardButton("10-20%", callback_data="prob_2")],
            [InlineKeyboardButton("20-50%", callback_data="prob_3")],
            [InlineKeyboardButton("60-75%", callback_data="prob_4")],
            [InlineKeyboardButton(">75%", callback_data="prob_5")],
            [InlineKeyboardButton("📝 Ввести свою вероятность", callback_data="custom_probability")],
            [InlineKeyboardButton("🔙 Вернуться к стоимости", callback_data="back_to_cost_impact")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text("Оцените вероятность возникновения риска или введите свою:",
                                      reply_markup=reply_markup)

    elif back_data == "back_to_timeline":
        # Очищаем данные о сроке и возвращаемся к вводу
        context.user_data.pop('timeline', None)
        context.user_data.pop('awaiting_timeline', None)

        await query.edit_message_text(
            "Укажите примерный период реализации риска:",
            reply_markup=create_back_keyboard("back_to_probability")
        )
        context.user_data['awaiting_timeline'] = True

    elif back_data == "back_to_mitigation_choice":
        # Очищаем данные о митигации и возвращаемся к выбору
        context.user_data.pop('reevaluation_needed', None)
        context.user_data.pop('awaiting_mitigation', None)

        keyboard = [
            [InlineKeyboardButton("Добавить мероприятие по митигации и внести оценку",
                                  callback_data="add_mitigation_with_evaluation")],
            [InlineKeyboardButton("Заполнить мероприятие по митигации без переоценки",
                                  callback_data="add_mitigation_without_evaluation")],
            [InlineKeyboardButton("🔙 Вернуться к сроку", callback_data="back_to_timeline")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            "Выберите действие:",
            reply_markup=reply_markup
        )

    elif back_data == "back_to_mitigation":
        # Очищаем данные о митигации и возвращаемся к вводу
        context.user_data.pop('mitigation', None)
        context.user_data.pop('awaiting_mitigation', None)

        await query.edit_message_text(
            "Введите мероприятие по митигации риска:",
            reply_markup=create_back_keyboard("back_to_mitigation_choice")
        )
        context.user_data['awaiting_mitigation'] = True

    elif back_data == "back_to_mitigation_result":
        # Очищаем данные об ожидаемом результате и возвращаемся к вводу
        context.user_data.pop('expected_result', None)
        context.user_data.pop('awaiting_expected_result', None)

        await query.edit_message_text(
            "Ожидаемый результат от мероприятия (опишите текстом):",
            reply_markup=create_back_keyboard("back_to_mitigation")
        )
        context.user_data['awaiting_expected_result'] = True

    elif back_data == "back_to_schedule_impact_re":
        # Очищаем данные о влиянии на сроки (переоценка) и возвращаемся к выбору
        context.user_data.pop('impact_schedule_min_re', None)
        context.user_data.pop('impact_schedule_most_likely_re', None)
        context.user_data.pop('impact_schedule_max_re', None)
        context.user_data.pop('needs_schedule_evaluation_re', None)
        context.user_data.pop('awaiting_schedule_values_re', None)

        keyboard = [
            [InlineKeyboardButton("Не влияет на сроки", callback_data="no_impact_schedule_re")],
            [InlineKeyboardButton("Оценить влияние на сроки", callback_data="evaluate_schedule_impact_re")],
            [InlineKeyboardButton("Запросить оценку (Повещенко С., Гиренко В.)",
                                  callback_data="request_schedule_evaluation_re")],
            [InlineKeyboardButton("🔙 Вернуться к выбору", callback_data="back_to_mitigation_result")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            "Влияние на сроки (оценка с мероприятиями):\nВыберите вариант:",
            reply_markup=reply_markup
        )

    elif back_data == "back_to_cost_impact_re":
        # Очищаем данные о влиянии на стоимость (переоценка) и возвращаемся к выбору
        context.user_data.pop('impact_cost_min_re', None)
        context.user_data.pop('impact_cost_most_likely_re', None)
        context.user_data.pop('impact_cost_max_re', None)
        context.user_data.pop('needs_cost_evaluation_re', None)
        context.user_data.pop('awaiting_cost_values_re', None)

        keyboard = [
            [InlineKeyboardButton("Не влияет на стоимость", callback_data="no_impact_cost_re")],
            [InlineKeyboardButton("Оценить влияние на стоимость", callback_data="evaluate_cost_impact_re")],
            [InlineKeyboardButton("Запросить оценку (Давыдова Е., Борисов А.)",
                                  callback_data="request_cost_evaluation_re")],
            [InlineKeyboardButton("🔙 Вернуться к срокам", callback_data="back_to_schedule_impact_re")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            "Влияние на стоимость (оценка с мероприятиями):\nВыберите вариант:",
            reply_markup=reply_markup
        )

    elif back_data == "back_to_probability_re":
        # Очищаем данные о вероятности (переоценка) и возвращаемся к выбору
        context.user_data.pop('probability_re', None)
        context.user_data.pop('awaiting_custom_probability_re', None)

        keyboard = [
            [InlineKeyboardButton("<10%", callback_data="prob_re_1")],
            [InlineKeyboardButton("10-20%", callback_data="prob_re_2")],
            [InlineKeyboardButton("20-50%", callback_data="prob_re_3")],
            [InlineKeyboardButton("50-75%", callback_data="prob_re_4")],
            [InlineKeyboardButton(">75%", callback_data="prob_re_5")],
            [InlineKeyboardButton("📝 Ввести свою вероятность", callback_data="custom_probability_re")],
            [InlineKeyboardButton("🔙 Вернуться к стоимости", callback_data="back_to_cost_impact_re")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text("Оцените вероятность возникновения риска после мероприятий или введите свою:",
                                      reply_markup=reply_markup)


# === Обработка текстовых сообщений ===
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Проверяем, какое именно текстовое сообщение ожидается
    if context.user_data.get('awaiting_custom_phase'):
        await handle_custom_phase(update, context)
    elif context.user_data.get('awaiting_custom_object'):
        await handle_custom_object(update, context)
    elif context.user_data.get('awaiting_custom_category'):
        await handle_custom_category(update, context)
    elif context.user_data.get('awaiting_description'):
        await get_description(update, context)
    elif context.user_data.get('awaiting_schedule_values'):
        await handle_schedule_values(update, context)
    elif context.user_data.get('awaiting_cost_values'):
        await handle_cost_values(update, context)
    elif context.user_data.get('awaiting_custom_probability'):
        await handle_custom_probability(update, context)
    elif context.user_data.get('awaiting_timeline'):
        await handle_timeline(update, context)
    elif context.user_data.get('awaiting_mitigation'):
        await handle_mitigation(update, context)
    elif context.user_data.get('awaiting_expected_result'):
        await handle_expected_result(update, context)
    elif context.user_data.get('awaiting_schedule_values_re'):
        await handle_schedule_values_re(update, context)
    elif context.user_data.get('awaiting_cost_values_re'):
        await handle_cost_values_re(update, context)
    elif context.user_data.get('awaiting_custom_probability_re'):
        await handle_custom_probability_re(update, context)
    # Новые обработчики для дополнительных мероприятий
    elif context.user_data.get('awaiting_mitigation_for_existing_risk'):
        await handle_new_mitigation(update, context)
    elif context.user_data.get('awaiting_expected_result_for_mitigation'):
        await handle_expected_result_for_mitigation(update, context)
    else:
        # Если сообщение не распознано, предлагаем вернуться в меню
        keyboard = [[InlineKeyboardButton("🏠 В главное меню", callback_data="back_to_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "Не понимаю ваше сообщение. Выберите действие из меню:",
            reply_markup=reply_markup
        )

# === Основная функция ===
def main():
    init_db()

    application = Application.builder().token("8226676788:AAEddKIZuMR1b5Mv4dD_JrGCf0a6oWAw2ic").build()


    # 📅 Ежедневное уведомление в 11:30
    application.job_queue.run_daily(
        send_daily_reminder,
        time=time(hour=11, minute=30),
        name="daily_reminder"
    )

    # Добавление обработчиков команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("subscribe", subscribe))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe))

    # Добавление обработчика для переключения подписки
    application.add_handler(CallbackQueryHandler(toggle_subscription, pattern="^toggle_subscription$"))

    # Добавление обработчиков callback-запросов
    application.add_handler(CallbackQueryHandler(submit_risk_start, pattern="^submit_risk$"))
    application.add_handler(CallbackQueryHandler(select_report_phase, pattern="^report$"))
    application.add_handler(CallbackQueryHandler(select_view_phase, pattern="^view_risks_list$"))
    application.add_handler(CallbackQueryHandler(show_phase_report, pattern="^report_phase_"))
    application.add_handler(CallbackQueryHandler(select_report_phase, pattern="^select_report_phase$"))
    application.add_handler(CallbackQueryHandler(export_excel_handler, pattern="^export_excel$"))
    application.add_handler(CallbackQueryHandler(custom_phase_request, pattern="^custom_phase$"))
    application.add_handler(CallbackQueryHandler(choose_phase, pattern="^phase_"))
    application.add_handler(CallbackQueryHandler(view_risks_list, pattern="^view_phase_"))
    application.add_handler(CallbackQueryHandler(custom_object_request, pattern="^custom_object$"))
    application.add_handler(CallbackQueryHandler(choose_object, pattern="^object_"))
    application.add_handler(CallbackQueryHandler(custom_category_request, pattern="^custom_category$"))
    application.add_handler(CallbackQueryHandler(choose_category, pattern="^cat_"))
    application.add_handler(CallbackQueryHandler(no_impact_schedule, pattern="^no_impact_schedule$"))
    application.add_handler(CallbackQueryHandler(request_schedule_evaluation, pattern="^request_schedule_evaluation$"))
    application.add_handler(CallbackQueryHandler(evaluate_schedule_impact, pattern="^evaluate_schedule_impact$"))
    application.add_handler(CallbackQueryHandler(no_impact_cost, pattern="^no_impact_cost$"))
    application.add_handler(CallbackQueryHandler(request_cost_evaluation, pattern="^request_cost_evaluation$"))
    application.add_handler(CallbackQueryHandler(evaluate_cost_impact, pattern="^evaluate_cost_impact$"))
    application.add_handler(CallbackQueryHandler(custom_probability_request, pattern="^custom_probability$"))
    application.add_handler(CallbackQueryHandler(choose_probability_re, pattern="^prob_re_\\d+$"))
    application.add_handler(CallbackQueryHandler(view_risks_list, pattern="^view_risks_list$"))
    application.add_handler(CallbackQueryHandler(view_risk_details, pattern="^view_risk_\\d+$"))
    application.add_handler(CallbackQueryHandler(add_mitigation_start, pattern="^add_mitigation_\\d+$"))
    application.add_handler(CallbackQueryHandler(back_to_risk_details, pattern="^back_to_risk_details$"))
    application.add_handler(
        CallbackQueryHandler(add_mitigation_with_evaluation, pattern="^add_mitigation_with_evaluation$"))
    application.add_handler(
        CallbackQueryHandler(add_mitigation_without_evaluation, pattern="^add_mitigation_without_evaluation$"))
    application.add_handler(CallbackQueryHandler(no_impact_schedule_re, pattern="^no_impact_schedule_re$"))
    application.add_handler(
        CallbackQueryHandler(request_schedule_evaluation_re, pattern="^request_schedule_evaluation_re$"))
    application.add_handler(CallbackQueryHandler(evaluate_schedule_impact_re, pattern="^evaluate_schedule_impact_re$"))
    application.add_handler(CallbackQueryHandler(no_impact_cost_re, pattern="^no_impact_cost_re$"))
    application.add_handler(CallbackQueryHandler(request_cost_evaluation_re, pattern="^request_cost_evaluation_re$"))
    application.add_handler(CallbackQueryHandler(evaluate_cost_impact_re, pattern="^evaluate_cost_impact_re$"))
    application.add_handler(CallbackQueryHandler(custom_probability_re_request, pattern="^custom_probability_re$"))
    application.add_handler(CallbackQueryHandler(choose_probability, pattern="^prob_\\d+$"))
    application.add_handler(CallbackQueryHandler(handle_back, pattern="^back_to_"))
    application.add_handler(CallbackQueryHandler(handle_view_risks_page, pattern="^view_risks_page_"))

    # Добавление обработчика текстовых сообщений
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Запуск бота
    print("Бот запущен...")
    application.run_polling()


if __name__ == "__main__":
    main()