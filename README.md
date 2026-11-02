# Greek NL2SQL για βιομηχανική τηλεμετρία

Κώδικας μεταπτυχιακής διπλωματικής εργασίας. Ο χρήστης κάνει ερωτήσεις στα ελληνικά για δεδομένα
αισθητήρων μηχανών, ένα τοπικό LLM (Qwen2.5 Coder 14B μέσω Ollama) γράφει το SQL, το σύστημα το
εκτελεί σε PostgreSQL και επιστρέφει απάντηση στα ελληνικά και γράφημα.

## Δομή

- `nl2sql-api/`: backend (FastAPI). `domain.yaml` είναι η περιγραφή της βάσης (στήλες, συνώνυμα,
  κανόνες, παραδείγματα). Για άλλη βάση αλλάζει μόνο αυτό το αρχείο.
- `nl2sql-ui/`: web εφαρμογή συνομιλίας.
- `benchmark/`: ερωτήσεις αξιολόγησης με SQL αναφοράς και το script που τις τρέχει.
- `benchmark/tb/`, `benchmark/lang/`: δύο επιπλέον πειράματα (ονόματα στηλών τύπου SCADA,
  ερωτήσεις στα αγγλικά).

## Εκτέλεση

Χρειάζονται Docker, NVIDIA GPU και τα δεδομένα στον πίνακα `factory_telemetry_fixed` της βάσης
`iot_staging` (δεν περιλαμβάνονται στο repo).

```bash
cp .env.example .env                  # και βάλτε κωδικό
docker network create shared-services
docker compose up -d
docker exec ollama ollama pull qwen2.5-coder:14b
```

UI: http://localhost:5174 · API: http://localhost:8001

## Αξιολόγηση

```bash
pip install requests pyyaml psycopg2-binary
python3 benchmark/runner.py --questions benchmark/questions.yaml
```
