# Uruchomienie 3api

[README PL](../README.md) · [README EN](../README.en.md) · [MCP](MCP.md) · [Wydanie](RELEASE.md)

Folder `GitHub` zawiera eksport źródeł. Do jego uruchomienia użyj sekcji [Praca ze źródłami](#praca-ze-źródłami). Poniższy wariant dotyczy opcjonalnego wydania EXE przygotowanego osobno według [RELEASE.md](RELEASE.md).

## Opcjonalne wydanie Windows x64

1. Zainstaluj Python **3.11 lub nowszy** z dostępnym Python Launcherem (`py`) lub poleceniem `python`.
2. Do wykonywania zadań zainstaluj Codex CLI i upewnij się, że `codex --version` działa w terminalu.
3. Rozpakuj całe `3api-windows-x64.zip`. Otwórz powstały katalog `3api-windows-x64` i uruchom `start.bat`.
4. Otwórz **http://127.0.0.1:4101/ui/**. Panel ustanawia sesję automatycznie.

Release zawiera `3api.exe`, zasoby panelu, worker Python, bootstrap i szablon konfiguracji. Rust, Cargo i Node.js nie są potrzebne do jego uruchomienia. Zainstalowany Python jest potrzebny: wydanie nie zawiera interpretera Pythona.

Pierwszy start tworzy lokalne `.venv`, instaluje zależności z `requirements.txt` i przygotowuje brakującą konfigurację oraz wewnętrzny token proxy. Pobranie pakietów wymaga dostępu do ich źródła. Działające środowisko jest używane ponownie; skopiowane lub uszkodzone środowisko bootstrap może zachować w lokalnym `backups/` przed odtworzeniem. Istniejąca konfiguracja i klucze nie powinny być zastępowane przykładem.

Nie przenoś samego `3api.exe`, jeśli chcesz korzystać z panelu zadań. Samodzielne tryby `mcp` i `proxy` obsługuje Rust; `serve` uruchamia także prywatnego workera.

## Pierwsze zadanie

W panelu dodaj API z dokładnym modelem lub deploymentem, prawidłowym endpointem i kluczem. Szablon to [`providers.example.toml`](../providers.example.toml); prywatny plik `providers.toml` i klucze pozostają lokalnie. Adres bazowy nie powinien kończyć się `/responses`.

Wybierz katalog projektu, utwórz czat, zaznacz zgodne API i napisz polecenie. Pusty czat jest zapisywany od razu. Każde przyjęte polecenie tworzy osobne uruchomienie, a kolejna wiadomość zachowuje rozmowę. Historia umożliwia powrót do wcześniejszych wyników.

Zamiast projektu możesz dodać rozmowę bez wybierania istniejącego folderu. Aplikacja tworzy osobny katalog pod systemowym temp. Tryb ograniczony używa `workspace-write` i zatwierdzania; pełny dostęp wymaga jawnego wyboru i uruchamia Codexa z `danger-full-access`. W obu przypadkach katalog roboczy oraz podsumowanie zmian dotyczą folderu rozmowy. Pełny dostęp pozwala zmieniać także inne miejsca, których ten diff nie obejmuje.

Ustawienia nowej instalacji zawierają gotowe prompty głównego agenta i koordynatora. Własne zapisane wartości nie są zastępowane. Limity na jedno API wynoszą domyślnie 1 000 000 TPM, z progami 900 000 i 950 000. Są to limity lokalnego routingu, niezależne od kwoty dostępnej na koncie dostawcy.

Domyślny tryb uprawnień prosi o zatwierdzenie dodatkowych operacji. W stanie oczekiwania otwórz czat i odpowiedz na pytanie lub prośbę. Zielony ptaszek oznacza zapisane ukończenie; końcowy zapis ma osobny stan. Tryb YOLO daje Codexowi pełny dostęp bez każdorazowego zatwierdzania.

## Zatrzymanie, restart i dane

Pozostaw terminal serwera otwarty. **Ctrl+C** zatrzymuje jego instancję. Ponowne `start.bat` korzysta z tego samego lokalnego katalogu danych. Uruchomienia przerwane zamknięciem procesu nie powinny być prezentowane jako ukończone.

| Co jest zapamiętane | Gdzie |
| --- | --- |
| Projekty, czaty, uruchomienia, wiadomości i zmiany | Lokalny katalog `data/rust/` |
| Telemetria API | `data/rust/telemetry.sqlite3` |
| Szkice, wybór czatu/API osobno dla projektu, język | Pamięć lokalna tej przeglądarki |
| Konfiguracja połączeń | Lokalny `providers.toml` |
| Klucze providerów na Windows | Menedżer poświadczeń lub skonfigurowane zmienne środowiskowe |

Usunięcie danych przeglądarki usuwa lokalne szkice i preferencje. Otwarcie innego profilu przeglądarki może używać innego zestawu tych preferencji. Historia serwera zależy od katalogu `--data-dir`; wskazanie nowego katalogu otwiera oddzielną historię.

Kopię zapasową danych wykonuj po zatrzymaniu aplikacji. Zachowaj cały katalog danych wraz ze snapshotami, nie tylko jeden plik SQLite. Kopia folderu aplikacji nie izoluje poświadczeń Windows używanych przez te same identyfikatory providerów. [Migracja](MIGRATION.md).

Usuwanie projektu z aplikacji archiwizuje pozycję i jej czaty; nie usuwa folderu ani zapisanej historii. Aktywne lub odzyskiwane uruchomienie blokuje tę operację. Ponowne dodanie tej samej ścieżki przywraca projekt. Foldery rozmów w temp nie są automatycznie kasowane przez panel; jeśli usunie je zewnętrzne czyszczenie, historia zostaje, a rozpoczęcie pracy zgłasza brak katalogu.

## Praca ze źródłami

Na Windows potrzebujesz Rust stable, MSVC Build Tools i Python 3.11+. W katalogu źródeł:

```powershell
.\start.bat
```

Launcher buduje brakującą lub starszą od źródeł binarkę `target/release/3api.exe`. Przy następnym starcie uwzględnia zmiany `src/*.rs`, `Cargo.toml`, `Cargo.lock` i `rust-toolchain.toml`. Już uruchomiony serwer używa dotychczasowego kodu do restartu; najpierw zakończ jego aktywne zadania. Możesz także wymusić przebudowę:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start.ps1 -Build
```

Ręczny start pozwala jawnie ustawić ścieżki i porty. Najpierw przygotuj worker przez `bootstrap.bat` oraz konfigurację przez `manage.py init`:

```powershell
.\bootstrap.bat
.\.venv\Scripts\python.exe manage.py init
cargo build --locked --release
.\target\release\3api.exe check --config providers.toml
.\target\release\3api.exe serve --config providers.toml --data-dir data/rust --project-dir .
```

`check` waliduje konfigurację bez połączeń z providerami; nie sprawdza dostępu do konta ani jego limitów. Dla rozpakowanego wydania używaj `./3api.exe` zamiast ścieżki w `target/release`.

Node.js jest potrzebny do testów przeglądarkowych. Przygotowanie: `npm ci --ignore-scripts`, `npx playwright install chromium`; uruchomienie: `npm run test:ui`. Pełny zestaw kontroli znajduje się w README.

Na Linux/macOS dostępna jest ścieżka budowania ze źródeł: `cargo build --locked --release`, środowisko `.venv`, `pip install -r requirements.txt`, lokalna kopia przykładowej konfiguracji i `target/release/3api serve`. Launchery Windows i Menedżer poświadczeń Windows nie są przenośne; użyj wskazanych w konfiguracji zmiennych środowiskowych. Zakres rzeczywistych prób na platformach opisuje [VERIFICATION.md](VERIFICATION.md).

## Demo bez zewnętrznego API

W katalogu źródeł uruchom:

```powershell
python scripts/serve_demo.py
```

Otwórz **http://127.0.0.1:44101/ui/**. To osobny serwer demonstracyjny ze sztucznymi projektami i API; działa na bibliotece standardowej Pythona i nie wysyła żądań do modeli. Pozwala obejrzeć interfejs oraz wykonać zrzuty bez prywatnej historii.

Polecenia zawierające `[demo:completed]`, `[demo:failed]`, `[demo:interrupted]`, `[demo:awaiting_input]` albo `[demo:finalizing]` wybierają deterministyczny scenariusz. Opcjonalne `--data-dir` służy do zachowania fikcyjnych danych demo. Działające demo sprawdza zachowanie interfejsu; nie potwierdza działania właściwego gatewaya Rust, workera ani rzeczywistego API.

## Rozwiązywanie problemów

| Objaw | Sprawdź |
| --- | --- |
| Nie ma Pythona | `py -3 --version` lub `python --version`; wymagane 3.11+. |
| Bootstrap nie pobiera zależności | Dostęp do źródła pakietów i wynik `bootstrap.bat`; nie kopiuj `.venv` z innego komputera. |
| Port 4100 lub 4101 jest zajęty | Czy działa Twoja poprzednia instancja. Zatrzymaj ją w jej terminalu; launcher nie powinien zabijać obcych procesów. |
| Połączenie z panelem wygasło | Otwórz ponownie `/ui/`, aby ustanowić lokalną sesję. Zachowany szkic pozwala wrócić do wiadomości. |
| Brak projektów po restarcie | Ścieżkę `--data-dir` oraz katalog, z którego uruchamiasz aplikację. |
| Zadanie nie startuje | `codex --version`, wybór zgodnego API, klucz oraz wynik pokazany w panelu. |
| API jest dostępne, lecz czeka | Globalne przypisania, cooldown i limity oczekiwania. Rezerwacja wątku pozostaje aktywna pomiędzy żądaniami. |
| Stary formularz tokenu | Czy uruchomiona jest binarka z nowego wydania. Zatrzymaj starszą instancję przed podmianą EXE. |

## English quick start

The supplied GitHub folder is a source export. Install Python 3.11+, Rust stable and MSVC Build Tools, and make Codex CLI available in `PATH` for coding tasks. Run `start.bat` in the source directory and open **http://127.0.0.1:4101/ui/**. The session is automatic. Startup prepares Python dependencies in a local `.venv` and rebuilds a missing or outdated Rust executable. Finish active work and restart a running instance to use changed backend code. An optionally packaged Windows release does not require Rust or Node.js.

Keep the whole release together because the task dashboard requires its private Python worker. Stop the server with **Ctrl+C**. Reuse the same `data/rust` directory to retain chats, runs and changes. Drafts and UI preferences belong to the current browser profile. Check [README.en.md](../README.en.md) for source builds and tests, and [VERIFICATION.md](VERIFICATION.md) for checks actually performed.

For an offline UI demonstration from source, run `python scripts/serve_demo.py` and open `http://127.0.0.1:44101/ui/`. This uses synthetic data and deterministic `[demo:...]` scenarios; it is separate from the production Rust/Python runtime and does not contact model providers.

Chats without an existing project get a persistent folder under system temp. Isolated mode requests approval and uses workspace-write; full access requires an explicit choice. The displayed diff covers the chat folder, even when full access allows work elsewhere. Removing a project only archives its application entry and history; adding the same path restores it. New installations show default prompts and per-API limits of 1,000,000 TPM with 900,000 / 950,000 thresholds. Saved custom prompts are preserved.
