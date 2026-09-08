# Praca nad 3api

[README PL](README.md) · [README EN](README.en.md) · [Architektura](docs/ARCHITECTURE.md) · [Wydanie](docs/RELEASE.md)

Przed zmianami przeczytaj `AGENTS.md`. Publiczne HTTP i MCP należą do Rust (`src/`). Python jest prywatnym silnikiem zadań; zachowuj jego protokół, zapis historii oraz kontrolę operacji na plikach.

## Zasady zmian

- Pracuj w uzgodnionej kopii i przydzielonych plikach. Przy pracy równoległej uzgodnij rozłączne zakresy, szczególnie manifesty, lockfile i launchery.
- Zachowuj niezacommitowane pliki oraz stan indeksu użytkownika. Przy snapshotach bez pierwszego commita podstawą jest bieżąca zawartość, nie `HEAD`.
- Zachowuj `task.id` jako trwały czat, osobne `run_id` dla poleceń i idempotencję `client_request_id`.
- Nie zamieniaj pominiętych szczegółów odpowiedzi w puste listy. Polling ma odrzucać spóźnione odpowiedzi i zachowywać pobrane dane po błędzie sieci.
- Status `completed` publikuj po zapisaniu końcowych wyników. Zielone potwierdzenie nie może oznaczać oczekiwania, finalizacji, błędu ani przerwania.
- Przydział API wynika ze zweryfikowanego wątku i roli. Rezerwacje pozostają globalne, a ich liczba jest czymś innym niż liczba żądań w toku.
- Utrzymuj spójne teksty PL/EN. Nie tłumacz treści użytkownika, kodu ani nazw projektów.

## Dane i testy

Używaj lokalnych mocków, fikcyjnych kluczy i osobnych katalogów danych. Nie testuj na normalnym `data/`, nie utrwalaj sekretów w fixtures i nie loguj payloadów użytkownika. Nie zmieniaj globalnej konfiguracji Codexa w ramach testów.

Uruchom kontrole odpowiednie dla zmiany z README. Dla interfejsu przygotuj zależności przez `npm ci --ignore-scripts` i uruchom `npm run test:ui`. Dla pakowania użyj `.\.venv\Scripts\python.exe -m pytest -q tests/test_package_source.py`. Zmiany transportu i streamingu sprawdzaj na rzeczywistym HTTP/TCP, również przy anulowaniu klienta.

Przeglądarka powinna objąć wiele czatów i projektów, szybkie przełączanie przy opóźnionym pollingu, reload, ponowne otwarcie, szkice, historię, rozwinięte diffy, role API oraz wszystkie stany zakończenia. Sprawdź PL/EN, widok mobilny i konsolę JavaScript.

W opisie zmiany podaj problem, wynikające zachowanie, wykonane kontrole oraz ograniczenia. Nie deklaruj wyników niewykonanych testów. `docs/VERIFICATION.md` jest zapisem faktów, nie listą planowanych prób.

## Dokumentacja i wydanie

UI używa lokalnych zasobów bez CDN. Lockfile Rust i narzędzi przeglądarkowych należy zachowywać; zależności aktualizuj z kontrolą zgodności oraz audytem właściwym dla zmiany.

README PL/EN powinny zawierać zgodne instrukcje. Zrzuty wykonuj na uruchomionym demo ze sztucznymi danymi, a potem je obejrzyj. Sprawdź lokalne odnośniki po spakowaniu zarówno źródeł, jak i runtime.

Pakuj według [docs/RELEASE.md](docs/RELEASE.md): finalny snapshot, allowlist, ZIP, sumy SHA-256 i weryfikacja `--source-only`. Folder `./GitHub` wewnątrz checkoutu jest ignorowany i nie trafia ponownie do eksportu. Opcjonalny release wymaga osobnego buildu, pełnej weryfikacji i próby świeżo rozpakowanego wydania. Do folderu `GitHub` nie należą prywatne ustawienia, dane, logi, kopie, środowiska, cache, `node_modules` ani pliki debugowania.

## English contribution notes

Read `AGENTS.md`, work in the agreed files, and preserve uncommitted user work. Rust owns public HTTP and MCP; Python remains the private task engine. Keep persistent chats separate from runs, request submission idempotent, provider roles verified, and terminal status dependent on saved results.

Use isolated mock providers and synthetic credentials. Never change global Codex configuration or test against private runtime data. Run checks appropriate to the change, validate PL/EN and mobile UI behavior, and report only results actually obtained. Source packaging uses final files, an explicit allowlist and `--source-only` verification. Optional binary releases also require a freshly tested extraction; see [docs/RELEASE.md](docs/RELEASE.md).
