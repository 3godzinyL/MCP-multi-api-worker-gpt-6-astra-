# Model bezpieczeństwa

[README](../README.md) · [Architektura](ARCHITECTURE.md) · [Migracja](MIGRATION.md)

3api jest lokalnym narzędziem dla zaufanego użytkownika komputera. Rust wzmacnia obsługę ruchu i walidację wejścia, ale nie daje gwarancji całkowitego bezpieczeństwa ani izolacji wszystkich operacji Codexa.

## Granice zaufania

| Granica | Kontrola |
| --- | --- |
| Sieć → usługi lokalne | Nasłuch wyłącznie na IPv4 loopback; kontrola oczekiwanego Host, zgodności Origin i żądań cross-site |
| Przeglądarka → panel | Automatyczne ustanowienie lokalnej sesji przy wejściu na `/ui/`; losowe cookie HttpOnly i SameSite Strict; kontrola Host/Origin |
| Formularze → operacje | Wewnętrzna sesja panelu oraz token CSRF dla operacji zapisujących dane |
| Gateway → worker Python | Prywatny port loopback i osobny losowy nonce przekazywany w nagłówku; nonce nie pochodzi z żądania użytkownika |
| Klient Responses → proxy | Lokalny token Bearer; ograniczenia rozmiaru, równoległości i czasu |
| Proxy → upstream | HTTPS, walidacja URL i adresów DNS, brak redirectów i systemowego proxy |
| Klient MCP → dane lokalne | Jawna lista narzędzi i pól, ograniczone parametry, SQLite tylko do odczytu, brak dowolnych ścieżek i poleceń |

Publiczne porty nowej kopii to domyślnie `4100` i `4101`. Nie wystawiaj ich przez tunel, reverse proxy ani przekierowanie routera, traktując lokalne zabezpieczenia jako kompletny system autoryzacji wieloużytkownikowej.

Panel nie wymaga ręcznego wpisywania tokenu. Otwarcie `/ui/` z dozwolonego lokalnego kontekstu ustanawia sesję; żądania cross-site nadal podlegają kontroli Host/Origin i metadanych przeglądarki. Operacje zapisujące dane wymagają CSRF. Cookie gatewaya zawiera losowy identyfikator sesji, nie wewnętrzny token proxy. Sekrety nie są zapisywane jako preferencje w localStorage.

Dostęp odbywa się przez HTTP loopback, dlatego cookie nie jest przedstawiane jako cookie transportu HTTPS. Sesje wygasają lub kończą się po restarcie usługi; ponowne otwarcie `/ui/` ustanawia sesję lokalną. Ten model ufa użytkownikowi komputera i procesom działającym z jego uprawnieniami. Automatyczna sesja nie jest logowaniem wieloużytkownikowej usługi publicznej.

## Poświadczenia

Klucze providerów są odczytywane po stronie serwera ze zmiennych środowiskowych lub Menedżera poświadczeń Windows. Nie są częścią TOML, odpowiedzi API panelu ani wyników MCP. Odczyt magazynu Windows zachowuje kompatybilność z wcześniejszą instalacją; te same identyfikatory oznaczają współdzielone wpisy.

Wewnętrzny token proxy jest osobnym poświadczeniem, przygotowywanym automatycznie. Nie trzeba go odczytywać ani wpisywać, aby otworzyć panel. Polecenie diagnostyczne `3api token`, jeśli zostanie użyte, celowo wyświetla sekret; jego wyniku nie należy publikować. Nonce prywatnego workera jest generowany przy uruchomieniu i nie jest poświadczeniem przeznaczonym dla użytkownika lub klienta MCP.

Proces Codex otrzymuje potrzebną delegację do lokalnego proxy, ale nie powinien dziedziczyć nonce workera ani zmiennych z kluczami providerów. Nie oznacza to ochrony przed dowolnym programem działającym z pełnymi uprawnieniami tego samego użytkownika Windows. Taki program może mieć dostęp do jego plików, procesów i magazynu poświadczeń.

## Dostęp upstream i SSRF

Domyślnie `allow_loopback_upstreams = false`. Włączony provider wymaga publicznego endpointu HTTPS bez danych logowania w URL, query, fragmentu i końcowego `/responses`. Proxy dopisuje właściwą ścieżkę protokołu i przekazuje klucz tylko do skonfigurowanego endpointu.

Resolver kontroluje adresy faktycznie przekazane do połączenia. Odrzuca odpowiedzi DNS zawierające adresy prywatne lub specjalne, także gdy obok nich występuje adres publiczny. Kontrola obejmuje IPv4, IPv6 i istotne postacie przejściowe. Redirecty i automatyczny systemowy proxy są wyłączone.

`allow_loopback_upstreams = true` jest świadomą opcją dla lokalnych atrap testowych. Pozwala na endpoint loopback, w tym lokalny HTTP; nie otwiera dostępu do dowolnych prywatnych adresów LAN ani endpointów metadanych chmurowych. Pozostaw ją wyłączoną w zwykłej konfiguracji.

Ta kontrola ogranicza SSRF, ale nie ocenia uczciwości publicznego providera. Skonfigurowane API otrzymuje prompty i inne dane Responses potrzebne do realizacji zadania.

## Limity i błędy

Gateway ogranicza body panelu podczas odbioru i ma timeout. Proxy ma konfigurowalne limity żądań, odpowiedzi, strumieni, czasu i równoległych połączeń. Parser SSE ogranicza pamięć również dla bardzo długiej linii i nie wymaga zgromadzenia całej odpowiedzi. Niepoprawny JSON lub typ danych nie powinien przerywać procesu.

Klasyfikacja retry korzysta ze strukturalnych błędów protokołu. Zwykła wypowiedź modelu o limitach nie uruchamia failoveru. Retry-After pochodzące ze zdarzeń jest ograniczone i sprawdzane pod kątem znaków sterujących. Przerwanie już rozpoczętej odpowiedzi nie uprawnia proxy do ponownego wykonania całego zadania ani sklejenia wyniku innego providera.

Komunikaty diagnostyczne i telemetria powinny używać statusów oraz krótkich identyfikatorów, nie surowych requestów, odpowiedzi ani komunikatów z sekretami. Awaria księgowania nie powinna zmieniać prawidłowej odpowiedzi upstream.

## Uprawnienia zadań i pliki

Nowe zadania domyślnie używają `approval`: `workspace-write`, `on-request` oraz ograniczeń sieci właściwych dla tego trybu. Rzeczywiste egzekwowanie tych zasad należy do zainstalowanego Codexa i mechanizmów systemu operacyjnego. Panel pokazuje prośby o uprawnienia i pytania wymagające odpowiedzi.

YOLO jest jawną opcją `danger-full-access` i `approval_policy=never`. W tym trybie Codex może wykonywać polecenia oraz zmieniać pliki bez każdorazowego zatwierdzania. Ani Rust, ani przydział ścieżek w eksperymencie nie są dodatkowym sandboxem dla takich poleceń.

Rozmowa utworzona bez projektu ma osobny katalog w systemowym temp. Wariant ograniczony wymusza `workspace-write` i zatwierdzanie; pełny dostęp wymaga jawnego potwierdzenia. Sam katalog temp nie jest sandboxem systemu operacyjnego. Widoczny diff dotyczy tego katalogu i nie obejmuje automatycznie zmian w innych miejscach dostępnych dla YOLO.

Eksperymentalne kopie robocze pomagają rozdzielić pracę wykonawców. Scalanie sprawdza własność plików, dowiązania i zmiany użytkownika oraz zachowuje kopie poprzednich wersji. Kontrola scalania nie zastępuje izolacji OS podczas wykonywania narzędzi. Kopie powinny pozostawać dostępne do ręcznego sprawdzenia po konflikcie lub przerwaniu.

## MCP tylko do odczytu

MCP działa przez stdio. Nie oferuje narzędzi do uruchamiania zadań, wysyłania promptów, wykonywania poleceń, zmiany providerów ani zapisu plików. Parametry narzędzi są ograniczone do obsługiwanych filtrów i limitów. Żądanie nie może wskazać dowolnej bazy, pliku lub URL do pobrania.

Dozwolone bazy to `dashboard.sqlite3` i `telemetry.sqlite3` we wskazanym przy starcie katalogu danych, z kontrolą położenia plików oraz trybem SQLite read-only/query-only. Zapytania mają ograniczone wyniki. Tytuły zadań, będące fragmentami promptów, wiadomości, diffy, polecenia, ścieżki projektów, endpointy i klucze są pomijane. Nazwa projektu i identyfikatory nadal mogą mieć znaczenie prywatne; udostępniaj serwer MCP wyłącznie zaufanemu klientowi.

Źródła danych mogą zawierać treści nieufne. Oznaczenie narzędzia jako read-only nie zmienia takich treści w zaufane instrukcje dla agenta.

## Dane lokalne i publikacja

`providers.toml`, `data/`, logi, kopie zapasowe, lokalne środowiska i artefakty testów nie należą do publicznego repozytorium. Pakowanie źródeł i wydania korzysta z jawnej listy dozwolonych plików. Telemetria nie przechowuje promptów, lecz baza panelu, snapshoty i kopie robocze mogą zawierać treści rozmów lub plików. Bazy SQLite nie są szyfrowane przez samą aplikację; chronią je uprawnienia użytkownika i zabezpieczenia dysku.

Przeglądarka przechowuje szkice i preferencje wyboru projektu, czatu oraz API. Są to dane lokalnego profilu przeglądarki i mogą zawierać niewysłany tekst użytkownika. Usunięcie sesyjnego cookie nie jest równoznaczne z usunięciem szkiców lub zapisanej historii serwera.

Usunięcie projektu z listy jest archiwizacją, nie kasowaniem prywatnej treści. Folder, historia i snapshoty pozostają lokalnie; ponowne dodanie tej samej ścieżki może je przywrócić. Katalogi rozmów pod systemowym temp mogą zostać usunięte przez zewnętrzne narzędzia czyszczące.

Publikuj szablon konfiguracji oraz zrzuty ekranu z danych demonstracyjnych. Przed pierwszym pushem sprawdź rzeczywistą listę plików Git, a nie tylko `.gitignore`. Ignorowanie nie usuwa sekretu już zapisanego w historii repozytorium. Ujawniony klucz wymaga unieważnienia lub rotacji u wystawcy.

## Weryfikacja i zgłoszenia

Testy z lokalnymi atrapami sprawdzają konkretne scenariusze, w tym autoryzację, granice wejścia, strumienie i zachowanie plików. Nie stanowią dowodu bezpieczeństwa wszystkich zależności, systemu operacyjnego ani dostępności rzeczywistego API. Wyniki aktualnego wydania i zakres rzeczywistych prób należy podawać osobno w README lub raporcie walidacji.

Zgłaszając podatność, opisz wersję, komponent, sposób odtworzenia i wpływ przy użyciu sztucznych danych. Nie dołączaj kluczy, cookies, prywatnej bazy, promptów ani logów z sekretami. Jeśli repozytorium udostępnia prywatne zgłoszenia bezpieczeństwa GitHub, użyj tej drogi; w przeciwnym razie skontaktuj się prywatnie z właścicielem przed publikacją szczegółów umożliwiających wykorzystanie błędu.

## English security summary

This is a local, single-user tool. Rust handles the public HTTP boundary, but does not sandbox Codex or guarantee complete security. Default ports **4100 / 4101** bind to loopback. Opening `/ui/` establishes a session automatically, without token entry. Host/Origin validation, HttpOnly/SameSite Strict cookies, CSRF and the private worker's per-start secret remain in place. The internal proxy token is prepared automatically. This session model trusts the local user; it is not public-service authentication.

Provider requests require public HTTPS with validated connection DNS addresses, no redirects and no ambient system proxy. **`allow_loopback_upstreams` defaults to false**; enabling it is intended for local test servers and does not permit private LAN destinations. Windows vault entries remain shared with installations using the same provider IDs.

New tasks default to approval mode. Opt-in YOLO gives Codex full access and disables per-operation approval; experimental worktrees and merge checks are not an OS sandbox. MCP is read-only and excludes prompts, task titles, messages, diffs, commands, project paths, endpoints and credentials.

Standalone chats use a temporary working directory with an explicit isolated/full access choice. Temp alone is not an OS sandbox, and the displayed diff covers that directory rather than every location available in full-access mode. Removing a project archives its entry; it does not erase private files or history.

Local history and snapshots may contain sensitive content and are not encrypted by this application. Browser storage also keeps drafts and selection preferences. Never commit runtime data, keys or real-account screenshots. Source and release packaging use an explicit allowlist. Report vulnerabilities privately with synthetic reproductions. Passing tests does not prove every deployment or workload is secure.
