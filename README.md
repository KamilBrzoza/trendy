# Trendomat by Odyseo (Streamlit)

Narzędzie do analizy trendów wyszukiwań dla dowolnego klienta/marki: sprawdza dla wklejonej
listy fraz wolumen wyszukiwań aktualny, wolumen sprzed roku oraz trend Google Trends, generuje
gotowy raport PDF/Excel dla klienta, a opcjonalnie porównuje frazy brandowe konkurencji.
Dane pobierane na żywo z DataForSEO (i opcjonalnie Senuto).

## Uruchomienie lokalnie

```bash
pip install -r requirements.txt
streamlit run app.py
```

Aplikacja otworzy się w przeglądarce pod `http://localhost:8501`. Klucze API wpisujesz
w panelu bocznym — nie są zapisywane w kodzie. Tam też wpisujesz nazwę klienta/marki, która
pojawi się w tytule wygenerowanego raportu PDF/Excel oraz w nazwie pliku.

## Wdrożenie na Streamlit Community Cloud (żeby udostępnić klientowi link)

1. Załóż darmowe konto na [streamlit.io](https://streamlit.io) (logowanie przez GitHub).
2. Wrzuć pliki `app.py`, `requirements.txt` oraz folder `.streamlit/` (zawiera `config.toml`
   z kolorystyką Odyseo) do repozytorium na GitHubie (może być prywatne).
3. Na [share.streamlit.io](https://share.streamlit.io) kliknij **New app**, wskaż repo, branch
   i plik `app.py`.
4. Kliknij **Deploy**. Po chwili dostaniesz publiczny link (np. `https://trendomat.streamlit.app`),
   który możesz wysłać klientowi lub używać wewnętrznie dla wielu klientów naraz.
5. Klucze API do DataForSEO/Senuto klient (lub Ty) wpisuje bezpośrednio w panelu bocznym aplikacji
   przy każdej sesji — nie trzeba ich trzymać w Secrets ani w repo. Jeśli wolisz, żeby klucze
   były wpisane na stałe (np. Twoje własne konto API, klient nie wpisuje nic), możesz je zapisać
   w **Settings → Secrets** aplikacji na Streamlit Cloud i zmienić `app.py` tak, by czytał je
   z `st.secrets` zamiast z pól tekstowych.

## Jak to działa

- **Wolumen aktualny i sprzed roku**: endpoint DataForSEO
  `keywords_data/google_ads/search_volume/live` zwraca dla każdej frazy bieżący wolumen oraz
  tablicę `monthly_searches` (ostatnie 12 miesięcy) — z niej wyciągana jest wartość sprzed roku.
- **Google Trends**: endpoint DataForSEO `keywords_data/google_trends/explore/live`. Endpoint
  ten przyjmuje tylko jedno zadanie na request, więc każda fraza jest sprawdzana osobno —
  dokładnie tak, jak przy ręcznym wyszukaniu pojedynczej frazy na trends.google.com (bez efektu
  "porównania" kilku fraz naraz, który potrafi psuć wykres słabszym frazom w grupie). Domyślnie
  pobierane jest 5 lat historii — można zmienić w panelu bocznym na 12 miesięcy lub 90 dni. Dla
  każdej frazy liczony jest też niezależny sygnał „rosnący / malejący / stabilny” (porównanie
  średniej z pierwszej i drugiej połowy okresu), widoczny w podsumowaniu i w tabeli szczegółowej
  jako potwierdzenie danych z Google Ads. Frazy z za małą liczbą realnych punktów danych (poniżej
  4) są pomijane zamiast pokazywać mylące, puste wykresy.
- **Analiza konkurencji** (sekcja 6, opcjonalna): osobna tabela, w której wprowadzasz pary
  Konkurent + Fraza (dowolna liczba wierszy). Sprawdzany jest tylko wolumen wyszukiwań z
  DataForSEO (bez Google Trends), pogrupowany i zsumowany wg konkurenta. Wyniki trafiają jako
  dodatkowy rozdział 4. do PDF-a i dodatkowe arkusze do Excela.
- **Senuto**: integracja jest szkieletowa (funkcja `senuto_get_volumes` w `app.py`).
  Publiczna dokumentacja Senuto (docs-api.senuto.com) wymaga konta z dostępem do API, żeby
  potwierdzić dokładną nazwę endpointu i pól w odpowiedzi dla Twojego planu — dopasuj tę funkcję
  po sprawdzeniu w swojej kolekcji Postmana.

## Koszty / limity do pamiętania

- DataForSEO Google Ads (wolumen): do 1000 fraz/zapytanie, limit 12 zapytań/min na endpointach
  *live*.
- DataForSEO Google Trends: jedno zadanie na request (patrz wyżej) — dla 50 fraz to 50 zapytań
  HTTP, wysyłanych sekwencyjnie z paskiem postępu w interfejsie; dla większych list to może
  potrwać kilka minut.
- Każde zapytanie do DataForSEO jest płatne zgodnie z Twoim cennikiem konta.
