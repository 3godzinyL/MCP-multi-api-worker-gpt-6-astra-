# Referencja kontraktu interfejsu i backendu

Ten plik opisuje bieżący kontrakt aplikacji. Rust obsługuje publiczne HTTP, proxy Responses i MCP; Python pozostaje prywatnym workerem. Nazwa pliku jest zachowana dla istniejących odnośników. Przykłady w [ui-contract.json](ui-contract.json) są syntetyczne, a wyniki faktycznych kontroli znajdują się wyłącznie w [VERIFICATION.md](../docs/VERIFICATION.md).

## Sesja i uprawnienia HTTP

`GET /ui/` ustanawia lub odnawia lokalną sesję bez formularza tokenu. UI wysyła cookies przez `credentials:same-origin` i pobiera CSRF z `meta[name=csrf-token]`. Operacje zapisujące stan wymagają poprawnego Origin i `X-Panel-CSRF`. Gateway przekazuje także metodę DELETE do prywatnego workera. Sekret workera nie jest tokenem przeznaczonym dla przeglądarki.

## Workspace, czat i uruchomienie

`project_id` wskazuje workspace: projekt z istniejącym katalogiem albo osobną rozmowę. `task.id` jest trwałym czatem wewnątrz tego workspace'u. Każde przyjęte polecenie ma osobny `run_id`.

| Operacja | Wynik |
| --- | --- |
| `POST /ui/api/projects` z `kind=project`, `path`, opcjonalnym `name` | HTTP 200; zapis istniejącego folderu, `access_mode=project`. |
| `POST /ui/api/projects` z `kind=chat`, `access_mode=isolated` | HTTP 200; własny katalog temp, wymuszone `approval/workspace-write`. |
| Ten sam endpoint z `kind=chat`, `access_mode=full`, `full_access_confirmed=true` | HTTP 200; własny katalog temp i jawny pełny dostęp `yolo/danger-full-access`. |
| `POST /ui/api/chats` z `project_id` i opcjonalnym `title` | HTTP 201; od razu zapisuje również pusty czat. |
| `POST /ui/api/tasks` z `continue_task` | HTTP 202; kontynuuje czat i zwraca `{id, run_id, state}`. |
| `DELETE /ui/api/projects/{id}` | HTTP 200 z `{id, removed:true}`; archiwizuje wpis. Aktywna lub odzyskiwana praca: HTTP 409. |

Folder rozmowy powstaje pod systemowym temp w `3api-chats/<hash-katalogu-danych>/chat-<losowy-id>`. Ścieżka jest generowana przez serwer, nie z nazwy użytkownika. Przeżywa restart aplikacji. Zewnętrzne usunięcie folderu nie usuwa historii, ale blokuje dalszy start z czytelnym błędem. Pełny dostęp nie rozszerza automatycznie skanu zmian poza ten katalog. Sam temp nie izoluje wszystkich odczytów komputera.

Archiwizacja zachowuje folder, historię i zakres dostępu. Ponowne świadome dodanie tej samej ścieżki przywraca ten sam identyfikator i historię. Zarchiwizowane czaty nie pojawiają się w zwykłych listach aplikacji.

## Idempotencja tworzenia

`POST /projects` i `POST /chats` przyjmują opcjonalne `client_request_id`: 1–128 znaków ASCII, litery, cyfry, podkreślenie, myślnik i kropkę. Interfejs generuje ID raz dla jednej operacji i zachowuje je wraz z body po niepewnym wyniku sieciowym.

SQLite zapisuje zasób i rekord `creation_requests` w jednej transakcji. Kluczem jest para operacja/ID; fingerprint obejmuje wartości body poza samym `client_request_id`, niezależnie od kolejności kluczy JSON. Replay jest sprawdzany przed przydzieleniem następnego katalogu temp.

- To samo body i ID zwracają istniejący zasób, także po restarcie.
- Zmienione wartości body pod użytym ID zwracają `409` z `code=request_conflict`.
- Retry do zarchiwizowanej pozycji zwraca `409`; nie przywraca jej po cichu.
- Nowa operacja lub świadome odtworzenie ścieżki dostają nowe ID.
- Błąd zajętej bazy jest przejściowym `503` z `Retry-After`; klient zachowuje dane do ponowienia.

`POST /tasks` również wykorzystuje `client_request_id` do odnalezienia uruchomienia po utracie odpowiedzi. Nowe polecenie wymaga nowego ID. `continue_task` wskazuje zapisany czat, więc błąd startu nie jest poleceniem usunięcia rozmowy.

## Odczyt, historia i łączenie odpowiedzi

`GET /ui/api/state` zachowuje `schema_version:2`, aktywny `project_id/task_id/run_id` i osobne `requested_project_id/requested_task_id/requested_run_id`. Wersja formatu HTTP jest niezależna od wersji schematu SQLite. Brak `messages` lub `changes` w skróconej odpowiedzi oznacza pominięcie, a nie pustą listę. Zaznaczony czat może zostać zwrócony również poza oknem ostatnich czatów.

Listy `/projects/{id}/chats` i `/projects/{id}/history` przyjmują `cursor` oraz `limit` od 1 do 200. Historia przyjmuje także `task_id`. Odpowiedź ma `{items,next_cursor}`.

Szczegóły `/tasks/{task_id}/runs/{run_id}` zawierają metadane i kolekcje `messages`, `changes`, `change_history`, `api_events`. Każda ma własne `{items,next_cursor}`; kolejne strony wskazują `messages_cursor`, `changes_cursor`, `change_history_cursor` i `api_events_cursor`. Kursor należy traktować jako nieprzezroczysty tekst.

`files/added/removed` opisują różnicę od początku wybranego uruchomienia. `touched_files` i `change_history` zachowują zaobserwowane edycje później cofnięte. Sam polling nie zwiększa liczników. Czasy to sekundy Unix; brak dawnych pomiarów to `null`. Liczba agentów pochodzi z rzeczywistych wątków, a nie liczby zaznaczonych API.

Zielony sukces przysługuje tylko `completed` po zapisaniu wyniku. `finalizing` oznacza trwający końcowy zapis. Błąd końcowego skanu nie jest ukończeniem. Projekt najpierw pokazuje aktywną pracę; pusty czat `idle` nie powinien zasłaniać ostatniego wyniku ukończonej pracy.

## Role i routing API

`POST /admin/routes` przyjmuje `project_id`, `task_id`, `run_id`, `thread_id` i `role`. Proxy rozpoznaje zweryfikowaną parę trasa + rzeczywisty nagłówek `thread-id`; kolejność żądań i wspólny `session-id` nie wyznaczają roli. Niezarejestrowany wątek ma ograniczony czas oczekiwania na przypisanie, potem otrzymuje konflikt.

Rezerwacje ról są globalne pomiędzy projektami i pozostają aktywne pomiędzy żądaniami. `main_count` i `auxiliary_count` opisują wątki, a `in_flight` aktualne żądania. `assignments` zawiera identyfikatory i rolę. Zakończenie lub przerwanie uruchomienia zwalnia jego przypisania przez `POST /admin/routes/release` z `{run_id}`.

Main preferuje API wolne, potem zajęte tylko pomocniczo i dalej najmniej obciążone. Auxiliary preferuje współdzielenie pomocnicze, potem wolne i najmniej obciążone. Przydział jest atomowy, ograniczony do wybranych zgodnych providerów i uwzględnia cooldown oraz budżet TPM.

## Minutowy budżet każdego API

Konfiguracja i `POST /ui/api/providers` używają `tokens_per_minute`, `soft_tokens_per_minute` i `hard_tokens_per_minute`. Domyślne wartości to **1 000 000 / 900 000 / 950 000**. Wymagane liczby całkowite: `0 < soft < hard <= limit <= 1000000000`. Zmiana nie nadpisuje pozostałych ustawień providera; edycja API wykorzystywanego przez aktywne zadanie jest blokowana.

`provider.token_budget` zawiera `limit`, `soft_limit`, `hard_limit`, `used_tokens`, `reserved_tokens`, `estimated_tokens`, `total_tokens`, `soft_reached`, `hard_reached`, `retry_after_seconds` i `window_seconds`.

`total_tokens` w tym obiekcie to lokalne obciążenie budżetu: raportowane użycie + rezerwacje aktywnych żądań + estymaty niepotwierdzonego usage. Nie jest raportem rozliczeniowym. Raportowane wpisy obejmują ostatnie 60 sekund. Aktywne rezerwacje nie wygasają w połowie strumienia. SQLite zachowuje okno; przerwane rezerwacje po restarcie stają się czasową estymatą, a kolejne restarty nie przedłużają jej bez końca.

Próg miękki preferuje inne zgodne API. Przy braku takiej alternatywy można pracować poniżej progu twardego. Nowe żądanie, którego estymata przekroczyłaby hard, czeka w granicach skonfigurowanego czasu albo otrzymuje `429`/`Retry-After`. Odpowiedź w toku nie jest z tego powodu urywana. Ruch poza tym proxy nie jest liczony.

## Ustawienia i tryb eksperymentalny

`main_prompt` i `coordinator_prompt` są lokalnymi ustawieniami panelu. Brakujące lub `null` wartości dostają gotowe instrukcje; istniejące stringi, również puste, są zachowane. Frontend zachowuje niewysłaną edycję przy pollingu i odświeżeniu.

Koordynator na Ultra przygotowuje plan tylko do odczytu i dokładnie dwa pakiety o rozłącznej własności plików. Dwa pozostałe API prowadzą wykonawców na Ultra w oddzielnych kopiach, a API koordynatora jest rezerwą. Puste `owned_paths` wymuszają recenzję tylko do odczytu. Kontrola scalenia chroni własność i zmiany użytkownika; testy integracji wymagające obu pakietów muszą zostać wykonane na połączonym wyniku.

## Dokumentacja i eksport

[ARCHITECTURE.md](../docs/ARCHITECTURE.md) opisuje komponenty, [SECURITY.md](../docs/SECURITY.md) ich granice, a [RELEASE.md](../docs/RELEASE.md) zwykły folder `./GitHub` i opcjonalny release Windows. Eksport źródeł nie zawiera prywatnych ustawień, baz, środowisk, debugów ani `node_modules`. Nie zmienia indeksu Git ani globalnego configu Codexa.

## English

This is the current API reference, not a pending handoff or an old test report. Workspaces have `kind=project|chat` and a persisted access mode. Creating projects and chats supports atomic, operation-scoped `client_request_id` replay; changed payloads or retries to archived resources return 409. Archival preserves files and history. Runs retain their own baselines and actual agent measurements.

Global provider roles are separate from in-flight requests. Per-API defaults are 1,000,000 TPM with 900,000 soft and 950,000 hard thresholds. Reported usage, reservations and unreported estimates remain distinguishable and the token window survives restart. Default prompts preserve existing custom strings; the experimental mode uses a read-only Ultra planner and exactly two workers. See [ui-contract.json](ui-contract.json) for synthetic requests and [VERIFICATION.md](../docs/VERIFICATION.md) for actual checks.
