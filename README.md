# Aura Herbals — raport fraz (Streamlit)

Aplikacja do sprawdzania dla listy fraz: wolumenu wyszukiwań aktualnego, wolumenu sprzed roku
oraz trendu Google Trends. Dane pobierane na żywo z DataForSEO (i opcjonalnie Senuto).

## Uruchomienie lokalnie

```bash
pip install -r requirements.txt
streamlit run app.py
```

Aplikacja otworzy się w przeglądarce pod `http://localhost:8501`. Klucze API wpisujesz
w panelu bocznym — nie są zapisywane w kodzie.

## Wdrożenie na Streamlit Community Cloud (żeby udostępnić klientowi link)

1. Załóż darmowe konto na [streamlit.io](https://streamlit.io) (logowanie przez GitHub).
2. Wrzuć pliki `app.py`, `requirements.txt` oraz folder `.streamlit/` (zawiera `config.toml`
   z kolorystyką Odyseo) do repozytorium na GitHubie (może być prywatne).
3. Na [share.streamlit.io](https://share.streamlit.io) kliknij **New app**, wskaż repo, branch
   i plik `app.py`.
4. Kliknij **Deploy**. Po chwili dostaniesz publiczny link (np. `https://aura-herbals-frazy.streamlit.app`),
   który możesz wysłać klientowi.
5. Klucze API do DataForSEO/Senuto klient (lub Ty) wpisuje bezpośrednio w panelu bocznym aplikacji
   przy każdej sesji — nie trzeba ich trzymać w Secrets ani w repo. Jeśli wolisz, żeby klucze
   były wpisane na stałe (np. Twoje własne konto API, klient nie wpisuje nic), możesz je zapisać
   w **Settings → Secrets** aplikacji na Streamlit Cloud i zmienić `app.py` tak, by czytał je
   z `st.secrets` zamiast z pól tekstowych.

## Jak to działa

- **Wolumen aktualny i sprzed roku**: endpoint DataForSEO
  `keywords_data/google_ads/search_volume/live` zwraca dla każdej frazy bieżący wolumen oraz
  tablicę `monthly_searches` (ostatnie 12 miesięcy) — z niej wyciągana jest wartość sprzed roku.
- **Google Trends**: endpoint DataForSEO `keywords_data/google_trends/explore/live`
  (maks. 5 fraz na zapytanie, więc 50 fraz = 10 zapytań). Zwraca szereg czasowy popularności 0–100.
  Domyślnie pobierane jest 5 lat historii (jak w trends.google.com) — można zmienić w panelu
  bocznym na 12 miesięcy lub 90 dni. Dla każdej frazy liczony jest też niezależny sygnał
  „rosnący / malejący / stabilny" (porównanie średniej z pierwszej i drugiej połowy okresu),
  widoczny w podsumowaniu i w tabeli szczegółowej jako potwierdzenie danych z Google Ads.
- **Senuto**: integracja jest szkieletowa (funkcja `senuto_get_volumes` w `app.py`).
  Publiczna dokumentacja Senuto (docs-api.senuto.com) wymaga konta z dostępem do API, żeby
  potwierdzić dokładną nazwę endpointu i pól w odpowiedzi dla Twojego planu — dopasuj tę funkcję
  po sprawdzeniu w swojej kolekcji Postmana.

## Koszty / limity do pamiętania

- DataForSEO Google Ads: do 1000 fraz/zapytanie, limit 12 zapytań/min na endpointach *live*.
- DataForSEO Google Trends: do 5 fraz/zadanie, limit odgórny 500k zapytań dziennie (współdzielony
  między wszystkich klientów DataForSEO) — dla 50 fraz to nieistotne, ale nie odpalaj raportu
  w pętli co kilka sekund.
- Każde zapytanie do DataForSEO jest płatne zgodnie z Twoim cennikiem konta.
