import re

_MESSAGES = {
    "en": {
        "Требуется действительный токен доступа": "A valid access token is required",
        "Требуются права администратора": "Administrator access is required",
        "Пользователь с таким email уже зарегистрирован": "A user with this email is already registered",
        "Неверный email или пароль": "Invalid email or password",
        "Сад с таким названием уже существует": "A garden with this name already exists",
        "Сад не найден": "Garden not found", "Растение не найдено": "Plant not found", "Диагностика не найдена": "Diagnosis not found",
        "Фотография не найдена": "Photo not found", "Файл фотографии не найден": "Photo file not found",
        "Запись ухода не найдена": "Care entry not found", "Напоминание не найдено": "Reminder not found",
        "Укажите местоположение сада или регион профиля": "Specify the garden location or profile region",
        "Единица измерения требует количества": "A unit requires an amount",
        "Период календаря должен включать часовой пояс": "The calendar range must include a time zone",
        "Начало периода должно быть раньше окончания": "The start must be before the end",
        "Период календаря не может превышать 366 дней": "The calendar range cannot exceed 366 days",
        "Погодный сервис временно недоступен": "The weather service is temporarily unavailable",
        "Загрузите от 1 до 5 фотографий": "Upload between 1 and 5 photos",
        "Поддерживаются только JPEG, PNG и WebP": "Only JPEG, PNG, and WebP are supported",
        "Некоторые фотографии не принадлежат этому растению": "Some photos do not belong to this plant",
        "Очередь диагностики временно недоступна": "The diagnosis queue is temporarily unavailable",
        "Задание диагностики не найдено": "Diagnosis job not found",
        "Месячный лимит диагностик исчерпан": "The monthly diagnosis limit has been reached",
        "Лимит повторных анализов для этой диагностики исчерпан": "The reanalysis limit for this diagnosis has been reached",
        "Один вопрос нельзя передать дважды": "The same question cannot be submitted twice",
        "Некоторые вопросы не принадлежат текущей версии диагностики": "Some questions do not belong to the current diagnosis revision",
        "Нет доступа к кабинету партнёра": "Partner dashboard access is not available",
        "Недостаточно прав для управления товарами": "You do not have permission to manage products",
        "Товар не найден": "Product not found", "Активный партнёр не найден": "Active partner not found",
        "Пользователь с таким email не найден": "No user with this email was found",
        "Пользователь уже привязан к партнёру": "The user is already assigned to a partner",
        "Сотрудник партнёра не найден": "Partner staff member not found", "Партнёр не найден": "Partner not found",
        "Партнёр с таким названием уже существует": "A partner with this name already exists",
        "Нельзя перенести товар в другую организацию": "A product cannot be moved to another organisation",
        "Оплата пока не подключена": "Payments are not configured",
        "Подписка уже активна; используйте управление подпиской": "The subscription is already active; use subscription management",
        "Платёжный профиль не найден": "Billing profile not found", "Webhook не настроен": "Webhook is not configured",
        "Webhook слишком большой": "Webhook payload is too large", "Telegram-бот пока не подключён": "The Telegram bot is not configured",
        "Telegram webhook не настроен": "Telegram webhook is not configured",
        "Некорректный секрет Telegram webhook": "Invalid Telegram webhook secret",
        "Telegram webhook слишком большой": "Telegram webhook payload is too large",
        "Некорректное обновление Telegram": "Invalid Telegram update",
        "Уведомление не найдено": "Notification not found",
        "Источник с таким URL уже существует": "A source with this URL already exists",
    },
    "lv": {
        "Требуется действительный токен доступа": "Nepieciešams derīgs piekļuves marķieris",
        "Требуются права администратора": "Nepieciešamas administratora tiesības",
        "Пользователь с таким email уже зарегистрирован": "Lietotājs ar šo e-pastu jau ir reģistrēts",
        "Неверный email или пароль": "Nepareizs e-pasts vai parole",
        "Сад с таким названием уже существует": "Dārzs ar šādu nosaukumu jau pastāv",
        "Сад не найден": "Dārzs nav atrasts", "Растение не найдено": "Augs nav atrasts", "Диагностика не найдена": "Diagnostika nav atrasta",
        "Фотография не найдена": "Fotoattēls nav atrasts", "Файл фотографии не найден": "Fotoattēla fails nav atrasts",
        "Запись ухода не найдена": "Kopšanas ieraksts nav atrasts", "Напоминание не найдено": "Atgādinājums nav atrasts",
        "Укажите местоположение сада или регион профиля": "Norādiet dārza atrašanās vietu vai profila reģionu",
        "Единица измерения требует количества": "Mērvienībai nepieciešams daudzums",
        "Период календаря должен включать часовой пояс": "Kalendāra periodam jāietver laika josla",
        "Начало периода должно быть раньше окончания": "Sākumam jābūt pirms beigām",
        "Период календаря не может превышать 366 дней": "Kalendāra periods nedrīkst pārsniegt 366 dienas",
        "Погодный сервис временно недоступен": "Laikapstākļu pakalpojums īslaicīgi nav pieejams",
        "Загрузите от 1 до 5 фотографий": "Augšupielādējiet no 1 līdz 5 fotoattēliem",
        "Поддерживаются только JPEG, PNG и WebP": "Tiek atbalstīti tikai JPEG, PNG un WebP",
        "Некоторые фотографии не принадлежат этому растению": "Daži fotoattēli nepieder šim augam",
        "Очередь диагностики временно недоступна": "Diagnostikas rinda īslaicīgi nav pieejama",
        "Задание диагностики не найдено": "Diagnostikas uzdevums nav atrasts",
        "Месячный лимит диагностик исчерпан": "Mēneša diagnostiku limits ir sasniegts",
        "Лимит повторных анализов для этой диагностики исчерпан": "Šīs diagnostikas atkārtoto analīžu limits ir sasniegts",
        "Один вопрос нельзя передать дважды": "Vienu jautājumu nevar iesniegt divreiz",
        "Некоторые вопросы не принадлежат текущей версии диагностики": "Daži jautājumi nepieder pašreizējai diagnostikas versijai",
        "Нет доступа к кабинету партнёра": "Nav piekļuves partnera kabinetam",
        "Недостаточно прав для управления товарами": "Nepietiek tiesību preču pārvaldībai",
        "Товар не найден": "Prece nav atrasta", "Активный партнёр не найден": "Aktīvs partneris nav atrasts",
        "Пользователь с таким email не найден": "Lietotājs ar šo e-pastu nav atrasts",
        "Пользователь уже привязан к партнёру": "Lietotājs jau ir piesaistīts partnerim",
        "Сотрудник партнёра не найден": "Partnera darbinieks nav atrasts", "Партнёр не найден": "Partneris nav atrasts",
        "Партнёр с таким названием уже существует": "Partneris ar šādu nosaukumu jau pastāv",
        "Нельзя перенести товар в другую организацию": "Preci nevar pārvietot uz citu organizāciju",
        "Оплата пока не подключена": "Apmaksa vēl nav konfigurēta",
        "Подписка уже активна; используйте управление подпиской": "Abonements jau ir aktīvs; izmantojiet abonementa pārvaldību",
        "Платёжный профиль не найден": "Maksājumu profils nav atrasts", "Webhook не настроен": "Webhook nav konfigurēts",
        "Webhook слишком большой": "Webhook saturs ir pārāk liels", "Telegram-бот пока не подключён": "Telegram bots vēl nav konfigurēts",
        "Telegram webhook не настроен": "Telegram webhook nav konfigurēts",
        "Некорректный секрет Telegram webhook": "Nederīgs Telegram webhook noslēpums",
        "Telegram webhook слишком большой": "Telegram webhook saturs ir pārāk liels",
        "Некорректное обновление Telegram": "Nederīgs Telegram atjauninājums",
        "Уведомление не найдено": "Paziņojums nav atrasts",
        "Источник с таким URL уже существует": "Avots ar šādu URL jau pastāv",
    },
}

_GENERIC_HTTP_MESSAGES = {
    "en": {
        400: "The request is invalid", 401: "Authentication is required",
        403: "Access is forbidden", 404: "The requested resource was not found",
        409: "The request conflicts with the current state",
        413: "The request is too large", 415: "The media type is not supported",
        422: "The submitted data is invalid", 429: "Too many requests",
        503: "The service is temporarily unavailable",
    },
    "lv": {
        400: "Pieprasījums nav derīgs", 401: "Nepieciešama autentifikācija",
        403: "Piekļuve ir liegta", 404: "Pieprasītais resurss nav atrasts",
        409: "Pieprasījums konfliktē ar pašreizējo stāvokli",
        413: "Pieprasījums ir pārāk liels", 415: "Multivides tips netiek atbalstīts",
        422: "Iesniegtie dati nav derīgi", 429: "Pārāk daudz pieprasījumu",
        503: "Pakalpojums īslaicīgi nav pieejams",
    },
}


def normalize_language(value: str | None) -> str:
    language = (value or "ru").split(",", 1)[0].split("-", 1)[0].strip().lower()
    return language if language in {"ru", "lv", "en"} else "ru"


def translate(message: str, language: str) -> str:
    normalized = normalize_language(language)
    translated = _MESSAGES.get(normalized, {}).get(message)
    if translated:
        return translated
    size = re.fullmatch(r"Размер файла превышает (\d+) МБ", message)
    if size and normalized == "en":
        return f"The file exceeds {size.group(1)} MB"
    if size and normalized == "lv":
        return f"Fails pārsniedz {size.group(1)} MB"
    return message


def translate_http_error(message: str, language: str, status_code: int) -> str:
    """Never leak an untranslated Russian API error to LV/EN clients."""
    normalized = normalize_language(language)
    translated = translate(message, normalized)
    if normalized == "ru" or translated != message or not re.search(r"[А-Яа-яЁё]", message):
        return translated
    return _GENERIC_HTTP_MESSAGES[normalized].get(
        status_code, "Request could not be completed" if normalized == "en"
        else "Pieprasījumu neizdevās izpildīt",
    )
