# Zgłaszanie problemów bezpieczeństwa

Nie zamieszczaj kluczy API, tokenów, historii rozmów, plików `providers.toml`, baz SQLite ani surowych logów w publicznych zgłoszeniach.

Jeśli repozytorium ma włączone GitHub Private Vulnerability Reporting, użyj zakładki **Security → Report a vulnerability**. W przeciwnym razie najpierw uzgodnij z opiekunem prywatny kanał kontaktu. Opisz wersję, warunki odtworzenia i wpływ, używając fikcyjnych danych.

Model zagrożeń, zastosowane zabezpieczenia i ograniczenia: [docs/SECURITY.md](docs/SECURITY.md).

Panel tworzy sesję automatycznie na lokalnym `/ui/`. Zgłoszenia dotyczące omijania Host/Origin, CSRF, ochrony cookies lub granicy prywatnego workera powinny zawierać minimalny przykład z fikcyjnymi danymi. Nie dołączaj własnych cookies ani tokenu proxy.

## English

Do not post API keys, tokens, cookies, conversation history, `providers.toml`, SQLite databases or raw logs in public issues. Use **Security → Report a vulnerability** if private reporting is enabled; otherwise agree on a private contact channel with the maintainer first. Include the affected version, a synthetic reproduction and the impact.

Automatic local sessions retain Host/Origin validation, CSRF and a separate private worker secret. See the [security model](docs/SECURITY.md) for boundaries and limitations.
