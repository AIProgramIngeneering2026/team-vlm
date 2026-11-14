# Архитектура Postcard Service

Документ описывает систему на трёх схемах: контекст, компоненты и жизненный цикл Job.
Контракты: [`api/openapi.yaml`](api/openapi.yaml), [`tool-contract.md`](tool-contract.md).
Принятые решения: [`adr/`](adr/). Проверка контракта колодой: [`contract-check.md`](contract-check.md).

---

## 1. Контекст

```mermaid
flowchart LR
    user["Конечный пользователь"]
    integrator["Разработчик-интегратор"]
    app["Клиентское приложение или бот"]
    subgraph sys["Граница системы"]
        service["Postcard Service: API, обработка изображений, хранение"]
    end
    geo["Nominatim: внешний геокодер"]

    user -->|"пользуется"| app
    app -->|"HTTP API: создать открытку, узнать статус, забрать результат"| service
    integrator -->|"HTTP API напрямую"| service
    service -->|"HTTPS: определить город по координатам"| geo
```

**Что показывают стрелки:** направление вызова. Стрелка идёт от того, кто обращается, к тому, к кому обращаются;
подпись говорит, по какому протоколу и зачем.

| Участник | Роль |
|---|---|
| Конечный пользователь | Получает открытки через приложение или бот. С сервисом напрямую не работает. |
| Клиентское приложение или бот | Вызывает наш API от имени пользователя. Вне границы проекта. |
| Разработчик-интегратор | Вызывает API напрямую, строит свои приложения поверх него. |
| Postcard Service | Наша система. Внутри неё PostgreSQL (история заданий) и файловый том (изображения): наружу они не открыты и на схеме контекста не показаны, их видно на схеме компонентов. |
| Nominatim | Внешний геокодер: по GPS из фото возвращает название города для подписи. Вызывается только при `add_city: true`. |

---

## 2. Компоненты

```mermaid
flowchart TB
    subgraph input["Входной слой"]
        api["HTTP API"]
    end
    subgraph appl["Прикладной слой"]
        svc["JobService"]
        worker["Worker"]
        cleanup["Cleanup"]
    end
    subgraph core["Ядро: не знает ни фреймворка, ни БД"]
        disp["Dispatcher"]
        reg["Registry"]
        tool["Tool: интерфейс"]
        job["Job и состояния"]
        repoport["JobRepository: порт"]
        fileport["FileStorage: порт"]
        geoport["Geocoder: порт"]
    end
    subgraph tools["Реализации Tool"]
        t1["resize_crop"]
        t2["frame"]
        t3["caption"]
        t4["filter"]
        t5["stamp"]
    end
    subgraph adapters["Адаптеры"]
        sql["SqlJobRepository: PostgreSQL или SQLite"]
        vol["VolumeFileStorage: том"]
        nom["NominatimGeocoder: timeout, retry, разбор ответа"]
    end

    api --> svc
    svc --> job
    svc --> disp
    svc --> repoport
    svc --> fileport
    worker --> disp
    worker --> repoport
    cleanup --> repoport
    cleanup --> fileport
    disp --> reg
    disp --> job
    disp --> repoport
    disp --> fileport
    reg --> tool
    t1 --> tool
    t2 --> tool
    t3 --> tool
    t4 --> tool
    t5 --> tool
    t3 --> geoport
    sql --> repoport
    vol --> fileport
    nom --> geoport
```

**Что показывают стрелки:** зависимости кода, а не вызовы по сети. Стрелка `A --> B` означает «код A зависит от B:
импортирует его или использует его интерфейс». Ядро ничего не импортирует из входного слоя, Tool-реализаций и адаптеров.
Наоборот, Tool и адаптеры зависят от интерфейсов ядра (инверсия зависимостей), поэтому ядро тестируется без HTTP и БД.

| Компонент | За что отвечает | Что скрывает от соседей |
|---|---|---|
| HTTP API | Принимает HTTP-запросы, проверяет форму запроса (заголовки, размер, multipart), переводит ошибки в единый формат | От ядра: всё про HTTP, коды ответов и фреймворк |
| JobService | Сценарии «создать, получить, отменить Job»: идемпотентность, проверка плана операций, постановка в очередь | От API: порядок работы с репозиторием, хранилищем и очередью |
| Worker | Берёт Job из очереди и запускает Dispatcher, при старте восстанавливает «осиротевшие» Job | От API: когда и в каком потоке выполняется Job |
| Cleanup | Удаляет файлы с истёкшим сроком хранения и просроченные ключи идемпотентности | От остальных: расписание и способ удаления |
| Dispatcher | Выполняет операции Job по порядку, проверяет отмену между операциями, превращает `ToolResult` в статусы Job | От остальных: какие Tool существуют (знает только Registry) |
| Registry | Находит Tool по имени операции | От Dispatcher: способ регистрации и конкретные классы |
| Tool (интерфейс) | Единый контракт операции: проверить параметры, применить к изображению | От вызывающего: алгоритм обработки и схему своих параметров |
| Job и состояния | Хранит состояние задания и разрешает только допустимые переходы | От остальных: способ хранения |
| JobRepository (порт) | Сохранение и чтение Job, атомарная смена статуса | От ядра: SQL, тип БД, миграции |
| FileStorage (порт) | Запись, чтение, удаление файлов исходников и результатов | От ядра: том это или S3, структуру путей |
| Geocoder (порт) | «Координаты → название города» | От Tool: Nominatim, HTTP, повторы, формат ответа |
| Реализации Tool | Пять операций оформления | Друг от друга и от Dispatcher |
| Адаптеры | Подключают порты к PostgreSQL, тому и Nominatim | От ядра: конкретные технологии |

---

## 3. Диаграмма состояний Job

```mermaid
stateDiagram-v2
    [*] --> created : POST jobs
    created --> running : Worker взял Job
    created --> cancelled : отмена до запуска
    running --> done : все операции выполнены, результат сохранён
    running --> failed : ошибка операции, хранилища или перезапуск сервиса
    running --> cancelled : отмена запрошена, проверка между операциями
    done --> [*]
    failed --> [*]
    cancelled --> [*]
```

Состояния `done`, `failed`, `cancelled` конечные: из них переходов нет.

| Состояние | Смысл |
|---|---|
| `created` | Job принят и сохранён, ждёт Worker |
| `running` | Dispatcher выполняет операции. Поле `cancel_requested` показывает, что клиент запросил отмену |
| `done` | Результат сохранён и доступен до `result_expires_at` |
| `failed` | Ошибка в `error.code`: `processing_failed`, `storage_error`, `interrupted` или `internal_error` |
| `cancelled` | Отменён клиентом, промежуточные данные удалены |

### Разрешённые переходы

`created → running`, `created → cancelled`, `running → done`, `running → failed`, `running → cancelled`.

Каждый переход выполняется атомарно: `UPDATE … WHERE status = <ожидаемый>`. Если условие не выполнилось, переход не состоялся
и Job не изменился. Переход `running → done` выполняется ещё и с условием `cancel_requested = false` (см. [ADR-0005](adr/0005-cooperative-cancellation.md)).

### Запрещённые переходы

| Попытка | Откуда может прийти | Что ответит система |
|---|---|---|
| `done → cancelled`, `failed → cancelled`, `cancelled → cancelled` | Клиент: `POST /jobs/{id}/cancel` | `409 invalid_transition`, в `details` поля `from` и `to`; статус не меняется |
| `done → running`, `failed → running`, `cancelled → running` («воскрешение») | Эндпоинта для этого нет. Повторить можно только новым `POST /jobs` с новым ключом идемпотентности | Через API недостижимо. В ядре: `InvalidTransition`, Job не меняется, ошибка в логе |
| `created → done`, `created → failed` (пропуск `running`) | Только баг в коде | `InvalidTransition` в ядре, транзакция откатывается, в логе уровень ERROR; если баг всплыл в запросе, клиент получает `500 internal_error` |
| `running → created` (возврат) | Только баг в коде | То же |
