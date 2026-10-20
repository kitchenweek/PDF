import asyncio
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import time

BOT_TOKEN = 8657302251:AAGr96NR-agMuRuJhUog4YuysVaRxDHpwn4
MAX_INPUT = 19 * 1024 * 1024
MAX_OUTPUT = 49 * 1024 * 1024
MAX_PAGES = 100
DPI = 150
TIMEOUT = 120
BUTTON_TTL = 1800
# Необязательно: разрешённые Telegram user ID. Пустое множество — для всех.
ALLOWED_USERS = set()

NOTICE = ('Очистка пересоздаёт страницы как изображения. Поиск и копирование текста, '
          'интерактивность и цифровые подписи исчезнут. Видимые ФИО, адреса, QR-коды '
          'и водяные знаки остаются. Комментарии, пометки и поля также могут быть '
          'видны на изображении: проверьте результат перед отправкой.')


def brief(value, limit=150):
    # Не допускаем управляющие символы из недоверенного PDF в отчёт.
    return ''.join(c if c.isprintable() else ' ' for c in str(value))[:limit]


def inspect_pdf(doc):
    findings = []
    def add(s):
        if len(findings) < 55:
            findings.append(s)
    labels = {'author': 'Автор', 'title': 'Название', 'subject': 'Тема',
              'keywords': 'Ключевые слова', 'creator': 'Приложение',
              'producer': 'Производитель PDF', 'creationDate': 'Создан',
              'modDate': 'Изменён'}
    for key, val in doc.metadata.items():
        if val and key not in ('format', 'encryption'):
            add(f'{labels.get(key, key)}: {brief(val)}')
    if doc.get_xml_metadata():
        add('XMP: расширенные метаданные (могут содержать имя, историю и идентификаторы).')
    if doc.xref_get_key(-1, 'ID')[0] != 'null':
        add('ID документа: может позволять сопоставлять копии; сам по себе не раскрывает личность.')
    if doc.embfile_count():
        add(f'Встроенные файлы: {doc.embfile_count()}.')
    if doc.get_toc():
        add('Закладки: могут содержать дополнительные сведения.')
    counts = {'Комментарии/пометки': 0, 'Поля формы': 0, 'Ссылки': 0}
    hidden = False
    for page in doc:
        for ann in page.annots() or []:
            counts['Комментарии/пометки'] += 1
            if ann.info.get('title'):
                add('Автор комментария: ' + brief(ann.info['title']))
        counts['Поля формы'] += sum(1 for _ in page.widgets() or [])
        counts['Ссылки'] += len(page.get_links())
        # type=3: невидимый текст; это не полный детектор скрытого содержимого.
        hidden |= any(s.get('type') == 3 for s in page.get_texttrace())
    for label, count in counts.items():
        if count:
            add(f'{label}: {count}.')
    if hidden:
        add('Найден невидимый текстовый слой (в том числе возможный OCR).')
    markers = {
        '/JavaScript': 'JavaScript', '/JS': 'JavaScript',
        '/Launch': 'запуск внешнего файла', '/URI': 'внешние адреса',
        '/EmbeddedFile': 'вложения', '/Filespec': 'ссылки на файлы',
        '/Metadata': 'метаданные объектов', '/PieceInfo': 'данные приложений',
        '/OCProperties': 'слои', '/Sig': 'цифровые подписи',
        '/StructTreeRoot': 'структура/дополнительный текст',
        '/Thumb': 'миниатюры',
    }
    found = set()
    # Проверяем также нестандартные поля словаря Info.
    typ, info = doc.xref_get_key(-1, 'Info')
    if typ == 'xref':
        ref = int(info.split()[0])
        for key in doc.xref_get_keys(ref):
            if key not in ('Author', 'Title', 'Subject', 'Keywords', 'Creator', 'Producer', 'CreationDate', 'ModDate', 'Trapped'):
                add(f'Дополнительное свойство {brief(key)}: {brief(doc.xref_get_key(ref, key)[1])}')
    if doc.xref_length() > 100000:
        raise ValueError('Слишком сложный PDF: больше 100000 объектов.')
    for ref in range(1, doc.xref_length()):
        obj = doc.xref_object(ref, compressed=True)
        if len(obj) > 2_000_000:
            raise ValueError('Слишком большой объект PDF.')
        for marker, label in markers.items():
            if re.search(re.escape(marker) + r'(?=[\s/<\[>()]|$)', obj):
                found.add(label)
    if found:
        add('Дополнительные структуры: ' + ', '.join(sorted(found)) + '.')
    if doc.is_repaired:
        add('Структура повреждена и восстановлена парсером; проверка может быть неполной.')
    return findings


def rebuild(doc, output):
    import pymupdf as fitz
    with fitz.open() as clean:
        for page in doc:
            rect = page.rect
            if rect.width <= 0 or rect.height <= 0 or rect.width * rect.height * (DPI/72)**2 > 18_000_000:
                raise ValueError('Размер страницы превышает безопасный лимит рендеринга.')
            # annots=True сохраняет видимые пометки, включая закрывающие текст.
            pix = page.get_pixmap(dpi=DPI, colorspace=fitz.csRGB, alpha=False, annots=True)
            new = clean.new_page(width=rect.width, height=rect.height)
            new.insert_image(new.rect, stream=pix.tobytes('jpeg', jpg_quality=90))
        clean.set_metadata({})
        clean.del_xml_metadata()
        clean.save(output, garbage=4, deflate=True, no_new_id=True)
    if Path(output).stat().st_size > MAX_OUTPUT:
        Path(output).unlink()
        raise ValueError('Новая копия больше 49 МБ. Разделите исходник на части.')
    with fitz.open(output) as check:
        if check.page_count != doc.page_count or check.get_xml_metadata() or check.embfile_count():
            raise ValueError('Проверка новой копии не пройдена.')
        if any(v for k, v in check.metadata.items() if k not in ('format', 'encryption')):
            raise ValueError('В новой копии обнаружены метаданные.')
        if check.xref_get_key(-1, 'ID')[0] != 'null':
            raise ValueError('В новой копии обнаружен ID.')
        for page in check:
            if page.get_text().strip() or page.get_links() or list(page.annots() or []) or list(page.widgets() or []):
                raise ValueError('В новой копии обнаружены лишние структуры.')


def worker(mode, source, output, result):
    try:
        if sys.platform.startswith('linux'):
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (1536*1024**2, 1536*1024**2))
            resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT, TIMEOUT))
            resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT + 1024**2, MAX_OUTPUT + 1024**2))
        import pymupdf as fitz
        fitz.TOOLS.mupdf_display_errors(False)
        fitz.TOOLS.mupdf_display_warnings(False)
        with fitz.open(source) as doc:
            if not doc.is_pdf:
                raise ValueError('Файл не является PDF.')
            if doc.needs_pass:
                raise ValueError('PDF защищён паролем. Сначала разблокируйте его локально.')
            if not 1 <= doc.page_count <= MAX_PAGES:
                raise ValueError(f'Допускается от 1 до {MAX_PAGES} страниц.')
            if mode == 'clean':
                rebuild(doc, output)
                data = {'pages': doc.page_count}
            else:
                data = {'pages': doc.page_count, 'findings': inspect_pdf(doc)}
    except ValueError as exc:
        data = {'error': brief(exc, 400)}
    except Exception:
        data = {'error': 'Не удалось обработать PDF: файл повреждён, слишком сложен или превышен лимит ресурсов.'}
    Path(result).write_text(json.dumps(data, ensure_ascii=False))


async def run_worker(mode, source, directory):
    output, result = Path(directory)/'clean.pdf', Path(directory)/'result.json'
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).resolve()), '--worker', mode,
        str(source), str(output), str(result),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(proc.wait(), TIMEOUT)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if proc.returncode != 0 or not result.exists():
        raise ValueError('Обработка остановлена: превышен лимит ресурсов или повреждён PDF.')
    data = json.loads(result.read_text())
    if data.get('error'):
        raise ValueError(data['error'])
    return data, output


# В памяти храним только file_id Telegram; исходник на диске удаляем после операции.
PENDING = {}
BUSY = set()


def prune():
    now = time.monotonic()
    for key in list(PENDING):
        if now - PENDING[key]['time'] > BUTTON_TTL:
            del PENDING[key]


def allowed(update):
    return update.effective_user and (not ALLOWED_USERS or update.effective_user.id in ALLOWED_USERS)


async def start(update, context):
    if not allowed(update):
        return
    await update.effective_message.reply_text(
        'Отправьте PDF как документ — я проверю потенциально идентифицирующие сведения '
        'и предложу кнопку «Перевыпустить». Лимиты: 19 МБ, 100 страниц; кнопка действует 30 минут.\n\n'
        + NOTICE + '\n\nИсходник и результат проходят через Telegram и сервер владельца бота. '
        'Локальные временные файлы удаляются после операции; сообщения в Telegram остаются. '
        'В группе отчёт видят участники. /forget — отключить свои кнопки очистки.')


async def forget(update, context):
    if not allowed(update):
        return
    for key in list(PENDING):
        if PENDING[key]['user'] == update.effective_user.id:
            del PENDING[key]
    await update.effective_message.reply_text('Кнопки отключены. Сообщения и файлы из Telegram не удалены.')


async def process(update, context, mode, file_id, topic_message):
    uid = update.effective_user.id
    if uid in BUSY or len(BUSY) >= 2:
        await topic_message.reply_text('Уже идёт обработка. Попробуйте чуть позже.')
        return
    BUSY.add(uid)
    try:
        await topic_message.reply_text('Проверяю PDF…' if mode == 'scan' else 'Создаю новую копию…')
        with tempfile.TemporaryDirectory(prefix='pdf_privacy_') as directory:
            source = Path(directory)/'source.pdf'
            tg_file = await context.bot.get_file(file_id)
            if tg_file.file_size and tg_file.file_size > MAX_INPUT:
                raise ValueError('Лимит исходного PDF — 19 МБ.')
            await tg_file.download_to_drive(source)
            if source.stat().st_size > MAX_INPUT:
                raise ValueError('Лимит исходного PDF — 19 МБ.')
            data, output = await run_worker(mode, source, directory)
            if mode == 'clean':
                with output.open('rb') as document:
                    await topic_message.reply_document(document, filename='document_clean.pdf',
                        caption='Новая копия: изображения страниц, без исходных метаданных и скрытого текстового слоя.\n' + NOTICE,
                        read_timeout=120, write_timeout=120)
            else:
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                findings = data['findings']
                title = ('Обнаружены потенциально идентифицирующие сведения/структуры:' if findings else
                         'Проверяемые признаки не обнаружены. Это не гарантия анонимности.')
                text = f'Страниц: {data["pages"]}\n{title}\n' + '\n'.join('• '+x for x in findings)
                text += '\n\nПроверка эвристическая: история правок, метаданные изображений и видимые персональные данные полностью не анализируются.\n\n' + NOTICE
                for offset in range(0, len(text), 3500):
                    await topic_message.reply_text(text[offset:offset+3500])
                prune()
                if len(PENDING) >= 1000:
                    del PENDING[next(iter(PENDING))]
                key = secrets.token_hex(12)
                PENDING[key] = {'user': uid, 'chat': update.effective_chat.id,
                                'file': file_id, 'time': time.monotonic()}
                await topic_message.reply_text('Создать новую копию?', reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton('Перевыпустить PDF', callback_data='clean:'+key)],
                    [InlineKeyboardButton('Забыть файл', callback_data='forget:'+key)]]))
    except asyncio.TimeoutError:
        await topic_message.reply_text('Обработка заняла больше 120 секунд и остановлена.')
    except ValueError as exc:
        await topic_message.reply_text(str(exc))
    except Exception:
        # Не записываем токены, метаданные, file_id и URL загрузки в логи.
        await topic_message.reply_text('Ошибка загрузки, обработки или отправки. Попробуйте ещё раз.')
    finally:
        BUSY.discard(uid)


async def document(update, context):
    if not allowed(update):
        return
    msg = update.effective_message
    if not msg.document:
        return
    doc = msg.document
    if not ((doc.file_name or '').lower().endswith('.pdf') or doc.mime_type == 'application/pdf'):
        await msg.reply_text('Отправьте файл PDF.')
        return
    if doc.file_size and doc.file_size > MAX_INPUT:
        await msg.reply_text('Лимит исходного PDF — 19 МБ.')
        return
    await process(update, context, 'scan', doc.file_id, msg)


async def callback(update, context):
    query = update.callback_query
    if not allowed(update) or not query.message:
        await query.answer('Недоступно.', show_alert=True)
        return
    prune()
    action, key = query.data.split(':', 1)
    item = PENDING.get(key)
    if not item or item['user'] != update.effective_user.id or item['chat'] != update.effective_chat.id:
        await query.answer('Кнопка устарела или принадлежит другому пользователю.', show_alert=True)
        return
    await query.answer()
    if action == 'forget':
        PENDING.pop(key, None)
        await query.edit_message_text('Кнопка отключена. Файл в Telegram не удалён.')
    else:
        await process(update, context, 'clean', item['file'], query.message)


async def error_handler(update, context):
    logging.getLogger('pdf_bot').warning('Ошибка обработчика: %s', type(context.error).__name__)


def main():
    from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
    if not BOT_TOKEN or BOT_TOKEN == 'ВСТАВЬТЕ_ТОКЕН_БОТА':
        raise SystemExit('Укажите BOT_TOKEN в начале main.py или в переменной окружения BOT_TOKEN.')
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger('httpx').setLevel(logging.CRITICAL)
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(8).build()
    app.add_handler(CommandHandler(['start', 'help'], start))
    app.add_handler(CommandHandler('forget', forget))
    app.add_handler(MessageHandler(filters.Document.ALL, document))
    app.add_handler(CallbackQueryHandler(callback, pattern=r'^(clean|forget):[0-9a-f]{24}$'))
    app.add_error_handler(error_handler)
    app.run_polling(allowed_updates=['message', 'callback_query'])


if __name__ == '__main__':
    if len(sys.argv) == 6 and sys.argv[1] == '--worker':
        worker(*sys.argv[2:])
    else:
        main()