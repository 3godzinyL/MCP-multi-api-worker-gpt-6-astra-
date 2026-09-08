# Materiał porównawczy

`start.bat` zachowuje poprzednie menu wyłącznie do testów zgodności historycznego mechanizmu zatrzymywania. Nie jest kompletnym launcherem do samodzielnego uruchomienia z tego katalogu.

Używaj `start.bat` w głównym katalogu projektu. Dawne wejścia `run_proxy.py`, `run_gui.py`, `start_gui.py` i `start_background.py` w głównym katalogu kierują już do programu Rust. Moduły `proxy/*.py` pozostają materiałem odniesienia dla migracji oraz zależnościami prywatnego silnika i jego testów.
