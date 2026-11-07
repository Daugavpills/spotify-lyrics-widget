# Spotify Glass — текст песни (виджет Rainmeter) / Lyrics widget for Rainmeter

**RU:** Локальный мост: **Spotify → текст с синхронизацией (Spicy Lyrics / LRCLIB) → PNG с подсветкой слова → виджет LyricsGlass в Rainmeter.** Виджет работает на рабочем столе, ниже открытых окон, обновляется ~33 кадра/с, подсветка плывёт по словам синхронно со звуком.

**EN:** Local bridge: **Spotify → synced lyrics (Spicy Lyrics / LRCLIB) → PNG with word highlight → Rainmeter LyricsGlass widget.** Widget runs on the desktop below open windows, updates ~33 fps, highlight flows word-by-word in sync with audio.

---

## 🚀 Быстрый старт / Quick start

### 1. Установи зависимости / Install dependencies

```powershell
pip install winsdk requests pillow
```

**RU:** Нужен **Python 3.11** (или новее). Rainmeter должен быть установлен и запущен.  
**EN:** You need **Python 3.11** (or newer). Rainmeter must be installed and running.

### 2. Настрой конфиг / Set up config

Скопируй шаблон / Copy the template:

```powershell
copy config.example.json config.json
```

Открой `config.json` и вставь свой ключ Spicy Lyrics / Open `config.json` and paste your Spicy Lyrics key:

```json
{
  "spicy_key": "sl_sk_твой_ключ_здесь",
  "port": 8765,
  "widget_width": 390,
  "lag_offset_ms": 0
}
```

**RU:**  
- `spicy_key` — ключ из [Spicy Lyrics](https://spicyyrics.com) (бесплатный, регистрация на сайте).  
- `lag_offset_ms` — сдвиг подсветки относительно звука, мс. `+` = текст раньше звука, `−` = текст позже. По умолчанию `0`.

**EN:**  
- `spicy_key` — key from [Spicy Lyrics](https://spicyyrics.com) (free, register on the site).  
- `lag_offset_ms` — highlight offset relative to audio, ms. `+` = text before sound, `−` = text after sound. Default `0`.

### 3. Запусти мост / Start the bridge

```powershell
run.bat
```

Или вручную / Or manually:

```powershell
pythonw bridge.py
```

### 4. Включи песню в Spotify / Play a song in Spotify

**RU:** Виджет **LyricsGlass** в Rainmeter покажет текст с подсветкой текущего слова. Позицию виджета можно перетаскивать мышкой — мост её не сбросит.  
**EN:** The **LyricsGlass** widget in Rainmeter will show lyrics with the current word highlighted. You can drag the widget with the mouse — the bridge won't reset its position.

---

## 📦 Что в репозитории / Repository contents

| Файл / File | Что делает / What it does |
|---|---|
| `bridge.py` | Мост: SMTC (позиция Spotify) → API текста → рисует PNG ~33 кадра/с / Bridge: SMTC (Spotify position) → lyrics API → draws PNG ~33 fps |
| `config.json` | Конфиг (твой `spicy_key`) / Config (your `spicy_key`) |
| `config.example.json` | Шаблон конфига с placeholder-ключом / Config template with placeholder key |
| `run.bat` | Лаунчер моста (`pythonw bridge.py`) / Bridge launcher (`pythonw bridge.py`) |
| `.gitignore` | Исключает кэш, логи, отрисованные кадры / Excludes cache, logs, rendered frames |
| `static/index.html` | Старое HTML-окно (опция `--window`) / Old HTML window (option `--window`) |

---

## 🛠 Настройка Rainmeter / Rainmeter setup

Скин `LyricsGlass` должен лежать в / The `LyricsGlass` skin should be at:

```
Documents\Rainmeter\Skins\LyricsGlass\LyricsGlass.ini
```

В скине уже прописано / The skin already has:

- `Update=16` — 60 Гц перерисовка / 60 Hz redraw  
- `DynamicVariables=1` — перечитывание PNG при каждом кадре / re-read PNG every frame  
- `Measure=Script` + `@Resources\frame.lua` — пинг-понг кадров (анти-мерцание) / ping-pong frames (anti-flicker)

Если виджет не появился / If the widget doesn't appear — обнови скин через Rainmeter / refresh the skin via Rainmeter: `!Refresh "LyricsGlass"`.

---

## ⚙️ Настройка синхронизации / Sync tuning

Если подсветка опережает или отстаёт от звука / If highlight leads or lags audio, крути `lag_offset_ms` в `config.json` / adjust `lag_offset_ms` in `config.json`:

- Видео/текст **опережает** звук → уменьшай `lag_offset_ms` (например, `-50`) / Text **leads** audio → decrease `lag_offset_ms` (e.g. `-50`)  
- Видео/текст **отстаёт** от звука → увеличивай `lag_offset_ms` (например, `+50`) / Text **lags** audio → increase `lag_offset_ms` (e.g. `+50`)

После изменения перезапусти мост (`run.bat`). / After changing, restart the bridge (`run.bat`).

---

## 🎯 Режимы работы / Modes

```
python bridge.py            # обычный (виджет + HTTP API) / normal (widget + HTTP API)
python bridge.py --demo     # демо-трек без Spotify / demo track without Spotify
python bridge.py --window   # + HTML-окно в браузере для отладки / + HTML window in browser for debugging
```

---

## 🔒 Анти-мерцание / Anti-flicker

Два файла кадров (`lyrics_a.png` / `lyrics_b.png`) пишутся по очереди. Мост пишет только в файл, который Rainmeter **не показывает** в данный момент. Скрипт `frame.lua` переключает `ImageName` и подтверждает показ в `ack.txt` — следующий кадр пишется только после подтверждения. Читатель и писатель никогда не работают с одним файлом одновременно, поэтому виджет не мигает.

Two frame files (`lyrics_a.png` / `lyrics_b.png`) are written in turn. The bridge writes only to the file Rainmeter is **not showing** at the moment. Script `frame.lua` switches `ImageName` and confirms display in `ack.txt` — the next frame is written only after confirmation. Reader and writer never work on the same file simultaneously, so the widget does not flicker.

---

## 📄 Атрибуция / Attribution

**RU:** Spicy Lyrics по условиям использования требует указывать источник текста. Строка атрибуции в виджете отключена по запросу пользователя. Если провайдер потребует её вернуть — это один блок в `build_widget_image` (`_credit_text`).

**EN:** Spicy Lyrics terms of use require attributing the text source. The attribution line in the widget is disabled at user request. If the provider requires it back — it's one block in `build_widget_image` (`_credit_text`).

---

## 🐛 Если что-то не работает / Troubleshooting

| Симптом / Symptom | Решение / Solution |
|---|---|
| Виджет пуст / Widget empty | Проверь, что Spotify играет и `spicy_key` верный / Check that Spotify is playing and `spicy_key` is correct |
| Текст отстаёт от звука / Text lags audio | Измени `lag_offset_ms` в `config.json` / Change `lag_offset_ms` in `config.json` |
| Виджет мигает / Widget flickers | Убедись, что в Rainmeter скин обновлён (`!Refresh "LyricsGlass"`) / Make sure Rainmeter skin is refreshed (`!Refresh "LyricsGlass"`) |
| Ошибка в логе / Error in log | `Rainmeter.log` в `%APPDATA%\Rainmeter\` / `Rainmeter.log` in `%APPDATA%\Rainmeter\` |

---

Разработано для Rainmeter 4.5+, Windows 10/11. / Developed for Rainmeter 4.5+, Windows 10/11.
