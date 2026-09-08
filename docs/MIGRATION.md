# Aktualizacja i zachowanie danych

[README](../README.md) · [Start](STARTUP.md) · [Architektura](ARCHITECTURE.md) · [Bezpieczeństwo](SECURITY.md)

Publiczne proxy, panel HTTP i MCP obsługuje Rust. Python zachowuje prywatny silnik zadań. Uruchomienie nowej wersji nie zmienia automatycznie konfiguracji innych klientów Codexa.

## Najważniejsze zmiany

| Obszar | Wcześniejsze zachowanie | Aktualny kontrakt |
| --- | --- | --- |
| Wejście do panelu Rust | Formularz tokenu | Otwarcie `/ui/` automatycznie ustanawia lokalną sesję. |
| Tożsamość zadania | `task.id` związane z dotychczasowym zadaniem | `task.id` jest trwałym czatem, a każde polecenie ma osobny `run_id`. |
| Nowy czat | Mógł istnieć tylko w widoku do pierwszego polecenia | Pusty czat zapisuje się od razu. |
| Kontynuacja | Wyniki bieżącego zadania | Rozmowa zostaje, a wcześniejsze uruchomienia pozostają w historii. |
| Statystyki zmian | Bieżący widok zmian | Baseline i wynik należą do uruchomienia; odświeżenie nie sumuje ponownie diffu. |
| Stan API | Bieżąca aktywność połączeń | Globalne rezerwacje wątków z rolami główną i pomocniczą, osobno od żądań w toku. |
| Rodzaj workspace'u | Projekt ze wskazanym katalogiem | Projekt lub rozmowa z własnym katalogiem temp i jawnym zakresem dostępu. |
| Usunięcie z panelu | Brak operacji archiwizacji | Ukrycie wpisu bez kasowania folderu i historii; ponowne dodanie ścieżki przywraca projekt. |
| Domyślne prompty | Brakujące lub niekompletne ustawienia | Brakujące wartości dostają gotowe instrukcje; zapisane stringi pozostają, także celowo puste. |
| Budżet API | Cooldown po błędzie providera | Dodatkowy budżet minutowy 1 000 000 TPM, soft 900 000, hard 950 000, osobno dla każdego API. |

Usunięcie formularza nie usuwa kontroli Host/Origin, CSRF, cookies HttpOnly/SameSite ani sekretu workera. Wewnętrzny token proxy jest przygotowywany automatycznie.

Nowe pomiary nie tworzą danych wstecz. Jeśli dawny rekord nie zawiera czasu, liczby agentów lub usage, wartość pozostaje `null` i jest pokazywana jako brak danych. Nie należy rekonstruować wyniku na podstawie liczby wybranych API lub liczby odświeżeń panelu.

## Aktualizacja istniejącej instalacji

1. Zakończ lub zatrzymaj uruchomienia i zamknij własny serwer przez **Ctrl+C**.
2. Zrób kopię lokalnego `providers.toml` oraz całego katalogu danych. Bazy, snapshoty i kopie robocze stanowią powiązany zestaw.
3. Rozpakuj wydanie do oddzielnego katalogu. Uruchom je najpierw z jego własną konfiguracją i danymi lub świadomie wskaż uzgodniony katalog przez `--data-dir`.
4. Sprawdź ścieżki projektów, modele, zapisane uprawnienia i dostępne poświadczenia przed wznowieniem pracy.
5. Uruchom `start.bat` i otwórz **http://127.0.0.1:4101/ui/**. Formularz tokenu nie jest częścią nowego startu.

Nie podmieniaj uruchomionego EXE i nie używaj równolegle tej samej bazy dla dwóch instalacji. Przy powrocie do wcześniejszej wersji użyj jej zgodnej kopii danych; nowy schemat nie jest obietnicą zgodności wstecz ze starym programem.

Usunięcie projektu z listy zachowuje jego rekordy historii i pliki. Nie jest operacją czyszczenia danych. Foldery zwykłych rozmów są zapisane w systemowym temp; aplikacja sama ich nie usuwa, lecz narzędzia systemowe mogą to zrobić. Do ważnych, długotrwałych prac wybierz istniejący katalog projektu i uwzględnij go we własnych kopiach zapasowych.

Starsza konfiguracja API bez pól minutowego budżetu dostaje wartości domyślne. Zapisane własne liczby i prompty należy zachować; odczytaj je w ustawieniach po aktualizacji. Minutowe estymaty nie zastępują historycznego raportu rzeczywistego usage.

## Porty i starsze dane Python

| Element | Starszy runtime Python | Runtime Rust |
| --- | --- | --- |
| Proxy Responses | Domyślnie `127.0.0.1:4000` | Domyślnie `127.0.0.1:4100` |
| Panel | Domyślnie `127.0.0.1:4001` | `127.0.0.1:4101/ui/` |
| Silnik zadań | Publiczna aplikacja Python | Prywatny worker uruchamiany przez Rust |
| Dane domyślne | `data/` | `data/rust/` |

Parametry portów i katalogów można ustawić jawnie w CLI. Program nie powinien przejmować portu zajętego przez inny proces. Skierowanie innego klienta na nowe `/v1` jest osobną zmianą jego konfiguracji.

Starsza historia z `data/` nie jest automatycznie importowana do nowej instalacji. Rekordy projektów mogą wskazywać bezwzględne ścieżki do oryginalnych folderów. Skopiowanie bazy bez sprawdzenia tych odwołań może skierować pracę do innego katalogu niż oczekiwany.

Przed przeniesieniem istniejącej historii sprawdź ścieżki projektów, identyfikatory rozmów, snapshoty, eksperymentalne kopie robocze oraz zapisane ustawienia uprawnień. Nowy katalog danych jest pustym, osobnym workspace'em; samo otwarcie innej przeglądarki nie kopiuje danych między serwerami.

## Szkice i preferencje przeglądarki

Wybrany projekt, czat, API i szkice są lokalnymi preferencjami przeglądarki. Rozmowy i uruchomienia są zapisane po stronie serwera. Zmiana portu, profilu przeglądarki lub usunięcie jej pamięci może zmienić preferencje, choć historia w `data/rust` nadal istnieje.

Po aktualizacji szybkie przełączanie projektów nie powinno przenosić szkicu lub wyboru API między nimi. Skrócona odpowiedź pollingu i błąd sieci nie stanowią polecenia wyczyszczenia wiadomości lub zmian.

## Poświadczenia Windows

Instalacje korzystające z tych samych identyfikatorów providerów mogą używać tych samych wpisów Menedżera poświadczeń Windows. Skopiowanie katalogu nie tworzy osobnego magazynu sekretów. Zmiana klucza może więc dotyczyć kilku instalacji.

Dla izolowanych testów używaj odrębnych identyfikatorów i fikcyjnych kluczy lokalnych mocków. Nie umieszczaj poświadczeń w repozytorium ani argumentach poleceń. `providers.example.toml` jest szablonem; rzeczywisty `providers.toml` pozostaje lokalnie.

## Kontrola po aktualizacji

Sprawdź dwa czaty jednego projektu, pracę w drugim projekcie, zachowanie szkicu i wybór API po powrocie. Wykonaj zadanie na mocku, otwórz jego historię, odśwież panel i uruchom aplikację ponownie z tym samym katalogiem danych. Zweryfikuj, że wynik oraz diff pozostały dostępne, a niedokończona praca nie otrzymała oznaczenia sukcesu.

To zalecana procedura aktualizacji, nie deklaracja jej wykonania na Twoich prywatnych danych. Wyniki konkretnej wersji znajdują się w [VERIFICATION.md](VERIFICATION.md).

## English migration notes

Opening `/ui/` now creates a local session automatically. Existing Host/Origin validation, CSRF, HttpOnly/SameSite cookies and the private worker secret remain. `task.id` identifies a persistent chat; each accepted instruction gets a separate `run_id` and change baseline. Older measurements remain unavailable when they were never recorded.

Stop the server and back up local configuration plus the whole data directory before updating. Use a separate extraction, review absolute project paths and saved permissions, and reuse data deliberately. Defaults are ports **4100 / 4101** and **`data/rust`**. Older Python history in `data/` is not automatically imported.

Browser drafts and selections are separate from server history. Copies with matching provider IDs can share Windows credentials. Startup and packaging do not modify global Codex configuration. Check [VERIFICATION.md](VERIFICATION.md) for the work actually validated; these migration instructions do not claim tests against private user data.

Removing a project now archives its entry and preserves history and files. Re-adding the same directory restores it. Standalone chats use saved system-temp directories, which survive app restarts but can be removed by system cleanup. Missing prompt settings receive defaults; existing strings, including blank ones, remain unchanged. Providers without token-budget fields inherit 1,000,000 TPM and 900,000 / 950,000 thresholds.
