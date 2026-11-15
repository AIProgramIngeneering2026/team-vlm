# Контракт Tool

Tool — одна операция оформления открытки (`frame`, `caption` и т. д.). Все Tool выглядят для Dispatcher одинаково,
поэтому новая операция добавляется без правки Dispatcher. Схема компонентов: [`architecture.md`](architecture.md).
Параметры операций в API: [`api/openapi.yaml`](api/openapi.yaml).

## 1. Интерфейс

Записан на Python-подобном псевдокоде. В коде проекта это абстрактный класс или `Protocol` в пакете ядра.

```python
class Tool(Protocol):
    name: str            # имя операции в API, например "frame"

    def validate(self, params: dict) -> None:
        """Проверяет параметры. Не смотрит на изображение, не ходит в сеть.
        Raises: InvalidParams(field, reason)."""

    def run(self, image: Image, params: dict, ctx: ToolContext) -> ToolResult:
        """При успехе возвращает ToolResult.
        При ожидаемой ошибке обработки выбрасывает ToolExecutionError."""


@dataclass
class ToolContext:
    gps: Coordinates | None   # координаты из EXIF исходника; None, если их нет или они вне диапазона
    geocoder: Geocoder        # порт ядра: city(lat, lon) -> str, при любом сбое GeocoderUnavailable
    now: datetime             # текущее время, подаётся снаружи (для штампа и тестов)


@dataclass
class ToolResult:
    image: Image       # результат при успехе
    warnings: list[Warning]   # {code, message}, например geocoder_unavailable

    @staticmethod
    def ok(image, warnings=()) -> "ToolResult": ...

class ToolExecutionError(Exception):
    """Ожидаемый отказ при выполнении Tool."""
    code: str
    message: str
    details: dict
```

`Image` — внутренний тип ядра (в реализации обёртка над изображением из библиотеки обработки), чтобы интерфейс не зависел
от конкретной библиотеки.

Registry и Dispatcher, с которыми работает Tool:

```python
class Registry:
    def register(self, tool: Tool) -> None: ...   # дубликат имени: ошибка при старте
    def get(self, name: str) -> Tool: ...         # нет такого: UnknownOperation
    def names(self) -> list[str]: ...

class Dispatcher:
    def validate_plan(self, operations: list[dict]) -> None:
        """Для каждой операции: Registry.get, затем Tool.validate.
        Raises: UnknownOperation(index, name, available) | InvalidParams(index, name, field, reason)."""

    def run(self, job_id: str) -> None:
        """Выполняет операции Job по порядку (см. ADR-0005 про проверку отмены)."""
```

## 2. Кто что проверяет

| Что проверяется | Кто | Когда | Что получает клиент |
|---|---|---|---|
| Имя операции известно | Registry (через `validate_plan`) | При создании Job, до записи в БД | `422 unknown_operation`, Job не создаётся |
| Параметры операции | `Tool.validate` (через `validate_plan`) | При создании Job, для каждой операции, до записи в БД | `422 invalid_params` с `operation_index`, `operation`, `field`, `reason` |
| Формат и размер изображения | Входной слой и JobService | При создании Job | `422 invalid_image` или `413 payload_too_large` |
| Параметры внутри `run` | Никто повторно | — | — |

Параметры проверяет **Tool, а не вызывающий**: только Tool знает свои ограничения, а Dispatcher о них не знает.
Вызывающий (JobService) лишь гарантирует, что `validate` вызван для каждой операции до сохранения Job. Поэтому `run` вправе
считать параметры корректными, а нарушение этого допущения считается программной ошибкой.
Изображение `run` тоже получает уже проверенным. Если оно не декодируется при выполнении, Tool выбрасывает `ToolExecutionError` с кодом `processing_failed`.
`Tool.validate()` — единственный исполняемый источник правил валидации параметров в коде. OpenAPI описывает те же ограничения для клиентов API, но не используется для проверки параметров во время выполнения. Тесты проверяют соответствие `Tool.validate()` ограничениям OpenAPI.

## 3. Ошибки

| Откуда | Как сообщается | Что происходит с Job |
|---|---|---|
| `validate` | исключение `InvalidParams(field, reason)` | Job не создаётся, клиент получает `422 invalid_params` |
| `run`, ожидаемый отказ | Tool выбрасывает `ToolExecutionError` с кодом `processing_failed` | Job → `failed`, ошибка записывается в `Job.error`; Dispatcher добавляет `operation_index` и `operation` |
| `run`, неожиданное исключение | перехватывает Dispatcher | Job → `failed`, `error.code = internal_error`, трассировка в логе |
| Нештатная, но не фатальная ситуация | `ToolResult.ok(image, warnings=[…])` | Job продолжается, предупреждение попадает в `warnings` Job |

Коды предупреждений: `geocoder_unavailable` (геокодер не ответил или вернул мусор, см. [ADR-0003](adr/0003-geocoder-degradation.md)),
`location_missing` (в фото нет GPS или координаты вне диапазона).

## 4. Инварианты, которые обязан соблюдать каждый Tool

1. Не изменяет входной `image`, возвращает новый.
2. Не пишет на диск и в БД, в сеть ходит только через `ctx`.
3. Не хранит состояние между вызовами и безопасен при параллельном запуске.
4. Результат не содержит метаданных исходника (EXIF, GPS): координаты не должны утечь в открытку.
5. При одинаковых `image`, `params` и `ctx` возвращает один и тот же результат.
6. Сохраняет размер изображения, если операция не изменяет его по определению (меняет размер только `resize_crop`).
7. Не прерывается снаружи: Dispatcher проверяет отмену только между операциями.

## 5. Два Tool через контракт

### `frame` — рамка

| | |
|---|---|
| **Получает** | Изображение; `params`: `style` (`thin` или `classic`, по умолчанию `classic`), `width` (целое 1–15, процент от меньшей стороны, по умолчанию 5), `color` (`#RRGGBB`, по умолчанию `#FFFFFF`) |
| **Возвращает** | Изображение того же размера: рамка рисуется по краям внутри исходных размеров. `thin` — линия, `classic` — широкое поле как у фотооткрытки |
| **Проверка в `validate`** | `style` из списка; `width` целое и в диапазоне; `color` по шаблону; неизвестные поля отклоняются (`field` = имя поля) |
| **Отказывает** | `processing_failed` при невозможности декодировать изображение. Других ожидаемых ошибок нет |
| **Предупреждения** | Нет |
| **Зависимости** | Нет (`ctx` не использует) |

Пример отказа в `validate`: `params = {"width": -5}` → `InvalidParams(field="width", reason="must be an integer from 1 to 15")`.

### `caption` — подпись

| | |
|---|---|
| **Получает** | Изображение; `params`: `text` (до 100 символов), `position` (`bottom` или `top`, по умолчанию `bottom`), `add_city` (булево, по умолчанию `false`); `ctx.gps`, `ctx.geocoder` |
| **Возвращает** | Изображение того же размера с подписью. При `add_city = true` и наличии `ctx.gps` подпись начинается с «Привет из <город>», затем `text` |
| **Проверка в `validate`** | `text` строка до 100 символов из допустимых (кириллица, латиница, цифры, пробелы, знаки препинания); `position` из списка; хотя бы одно из двух: непустой `text` или `add_city = true` (иначе `field = text`) |
| **Отказывает** | `processing_failed`, если подпись не удаётся отрисовать |
| **Предупреждения** | `geocoder_unavailable`: `ctx.geocoder.city` выбросил `GeocoderUnavailable`, подпись сделана без города. `location_missing`: `add_city = true`, но `ctx.gps = None` |
| **Зависимости** | Порт `Geocoder`. Таймаут, повторы и разбор ответа внутри адаптера, Tool видит только «город» или `GeocoderUnavailable` |

Сбой геокодера не делает Job `failed`: решение и его цена описаны в [ADR-0003](adr/0003-geocoder-degradation.md).

### Остальные Tool (кратко)

| Tool | Получает | Возвращает | Отказывает |
|---|---|---|---|
| `resize_crop` | `format` (`a6`, `a5`), `fit` (`crop`, `pad`) | Изображение нужного формата. Единственный Tool, меняющий размер | `processing_failed` |
| `filter` | `name` (`sepia`, `grayscale`), обязательно | Изображение с применённым фильтром | `processing_failed` |
| `stamp` | `style` (`stamp`, `postmark`), `position` (четыре угла); дата берётся из `ctx.now` | Изображение с маркой или штемпелем | `processing_failed` |

## 6. Как добавить новый Tool

1. Написать класс, реализующий `Tool` (`name`, `validate`, `run`) по инвариантам из раздела 4.
2. Зарегистрировать его в Registry при сборке приложения.
3. Добавить схему параметров в `openapi.yaml` (вариант в `oneOf` у `Operation`) и тесты Tool.

Dispatcher, JobService и HTTP API при этом не меняются.
