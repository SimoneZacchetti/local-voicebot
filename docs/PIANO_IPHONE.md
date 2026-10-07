# Piano: voicebot e controllo casa dall'iPhone (branch `feat/iphone`)

## Contesto

Oggi il voicebot si usa solo da Chrome sul Mac (`127.0.0.1:8000`). L'obiettivo è usarlo dall'iPhone in quattro modi:
1. **voce-voce**;
2. **chat scritta**;
3. **comandi rapidi** senza aprire l'app (Siri, widget, Centro di Controllo, tasto Azione);
4. **notifiche** dalla casa.

Vincoli:
- Il Mac resta il "cervello" (STT, LLM, TTS, VM di Home Assistant). È sempre acceso e sveglio, ma viaggia con l'utente.
- Rete privata: **Tailscale** con `tailscale serve`, che dà HTTPS valido su `*.ts.net`. Mai `funnel`, mai porte aperte sul router.
- Tutto il lavoro avviene sul nuovo branch **`feat/iphone`**, con un commit per milestone.
- Nessun trailer o footer di attribuzione in commit e PR.

**Problema di sicurezza attuale, da chiudere prima di esporre qualsiasi cosa.** `/ws` non ha né autenticazione né controllo Origin ([server/main.py](server/main.py) `ws_endpoint`). Qualsiasi pagina web aperta nel Chrome del Mac potrebbe già comandare la casa (cross-site WebSocket hijacking).

**Limite iOS non aggirabile.** La voce dalla PWA funziona solo con l'app in primo piano e lo schermo acceso. A telefono bloccato l'unica via a mani libere è Siri Shortcuts, un comando alla volta.

## Architettura

```
iPhone (Tailscale, VPN On Demand)
 ├─ PWA "Casa" (Home Screen) ─────────┐  voce + chat: wss /ws
 ├─ Siri Shortcuts / widget / CC ─────┤  POST /api/command, /api/confirm (Bearer)
 └─ Web Push (APNs) ◄─────────────────┼─────────────────────────────┐
                                      ▼                             │
     https://voicebot-mac.<tailnet>.ts.net  (tailscale serve :443 → 127.0.0.1:8001)
Mac ─ uvicorn: un solo server, due socket loopback                  │
      ├─ 127.0.0.1:8000 LOCALE  (browser del Mac, Origin allowlist) │
      └─ 127.0.0.1:8001 REMOTO  (login Tailscale obbligatorio, fail-closed)
         /ws → Session · /api/* → PhoneSessions(HeadlessSession) · push.py ───┘
         ha_events.py: polling di input_text.voicebot_notify (HA_TOKEN non-admin)
```

Il server decide se una richiesta è locale o remota dalla **porta d'ascolto** (`scope["server"][1]`), mai da header falsificabili come Host o X-Forwarded.

## Milestone

### M0: branch e Tailscale
- **Io:** `git switch -c feat/iphone`; controllo che `uv run pytest -q` sia verde.
- **Utente:**
  - installare Tailscale **Standalone** sul Mac (.pkg) e attivare la CLI;
  - nella admin console: rinominare il Mac `voicebot-mac` (il nome finisce nei log pubblici di Certificate Transparency), attivare MagicDNS e HTTPS Certificates;
  - scrivere una regola ACL che permetta di raggiungere il Mac su `:443` solo dai propri dispositivi;
  - installare Tailscale sull'iPhone con VPN On Demand e annotare la versione di iOS.
- **Verifica:** `tailscale status` mostra Mac e iPhone.

### M1: sicurezza, listener remoto, primo test sull'iPhone
- **[server/config.py](server/config.py)**, nuove chiavi:
  - `REMOTE_ENABLED`, `REMOTE_PORT` (8001), `PUBLIC_HOST`;
  - `TS_ALLOWED_LOGINS`;
  - `API_TOKENS` (un token per dispositivo, così se ne revoca uno solo);
  - `LOCAL_ORIGINS`.
- **`server/__main__.py`** (nuovo, si avvia con `uv run python -m server`):
  - un solo `uvicorn.Server` avviato con `serve(sockets=[8000, 8001])`, cioè un solo lifespan;
  - esce se `HOST` non è loopback;
  - con `REMOTE_ENABLED` esce anche se mancano token (almeno 32 caratteri), allowlist dei login o `PUBLIC_HOST`.
- **`server/auth.py`** (nuovo):
  - middleware `RemoteGate`. Sulla porta remota: `Tailscale-User-Login` nella allowlist, altrimenti 403; `Host == PUBLIC_HOST`; Origin esatto oppure Bearer valido su upgrade WS e POST.
  - Sulla porta locale: Host deve essere `127.0.0.1` o `localhost`; Origin su `/ws` deve stare nell'allowlist. Questo chiude il CSWSH.
  - `require_token`: confronto Bearer con `hmac.compare_digest`; 503 se non è configurato nessun token, 401 se è sbagliato.
  - `/healthz` resta libero.
  - I log riportano login, path ed esito, mai i token.
- **[server/main.py](server/main.py):** aggiunge il middleware; in `ws_endpoint` il controllo avviene prima di `accept()`, con `close(1008)` se fallisce.
- **[server/session.py](server/session.py):** un JSON malformato non chiude più la sessione (try/except, helper `_int`); limite di circa 30 s sui frame audio.
- **Altri file:**
  - `scripts/gen_secrets.py`: stampa token e chiavi VAPID su stdout;
  - [scripts/check_env.py](scripts/check_env.py): `.env` non tracciato e con permessi 600, `serve` punta solo alla 8001, nessun funnel attivo;
  - `.env.example`;
  - `.gitignore`: aggiunge `data/`.
- **Test:**
  - le fixture comuni (FakeHome, ToolLlm, GoogleHome) si spostano in `tests/conftest.py`;
  - `tests/test_auth.py`: 403 senza login, Origin estraneo chiuso con 1008, Host sbagliato, CSWSH locale bloccato, JSON malformato, `/healthz`.
- **Utente:** `tailscale serve --bg 8001` (**non** 8000).
- **Verifica:**
  - `curl 127.0.0.1:8001/` dà 403;
  - dall'iPhone in rete cellulare, `https://voicebot-mac…ts.net` si carica e la chat scritta risponde (prova che il WS passa attraverso `serve`);
  - con la VPN spenta il dominio non risponde.

### M2: voce affidabile su Safari iOS
- **[web/app.js](web/app.js):**
  - nel tap su Start, prima di ogni `await`: creare **un solo** `AudioContext` alla frequenza hardware (togliendo `sampleRate: 24000`), chiamare `resume()`, impostare `navigator.audioSession.type='play-and-record'` se esiste, sbloccare `speechSynthesis`;
  - passare `audioContext` a `MicVAD.new`;
  - mute con `track.enabled` al posto di `vad.pause()`;
  - se il microfono termina (`track.onended`), riacquisirlo;
  - overlay "Tocca per riprendere" quando lo stato è `interrupted`;
  - su iOS nascondere "Modalità altoparlanti";
  - barge-in con `speechSynthesis.cancel()`.
- **[web/player-worklet.js](web/player-worklet.js):** ricampionamento lineare 24 kHz → frequenza del contesto; le posizioni restano in campioni sorgente, quindi `heard_seq` lato server non cambia.
- **[web/style.css](web/style.css) e [web/index.html](web/index.html):**
  - textarea a 16px (evita lo zoom), `:hover` solo dentro `@media (hover:hover)`, `svh`, target da almeno 44px;
  - `enterkeyhint="send"`;
  - pulsante "Scrivi" che apre la chat senza microfono.
- **Verifica:**
  - sul Mac, `scripts/e2e_browser.py` e `e2e_ws.py` senza regressioni;
  - sull'iPhone: saluto, domanda e risposta parlata, barge-in in circa 300 ms, mute e unmute, una chiamata durante la sessione fa comparire l'overlay.

### M3: ciclo di vita e PWA installabile
- **[web/app.js](web/app.js):**
  - riconnessione WS con backoff da 0,5 a 10 s, con pill "Riconnessione…" o "Mac non raggiungibile";
  - gestione di `visibilitychange` e `pageshow` (riprende contesto, microfono e WS);
  - `wakeLock` mentre la voce è attiva;
  - `client_id` in `localStorage`, inviato in `hello`;
  - registrazione di `sw.js`.
- **Server:** registro `app.state.histories` (`client_id` → history, TTL 900 s). Dopo una riconnessione la history viene ripresa, il pending (conferma in sospeso) **no**.
- **Nuovi file:**
  - `web/manifest.webmanifest` ("Casa", standalone);
  - `web/icons/` generate da `favicon.svg` (sfondo opaco), meta Apple in `index.html`;
  - `web/sw.js`: cache-first per `/vendor/*` (versionata), network-first per la shell con pagina offline "Mac non raggiungibile", mai in cache `/ws` e `/api`.
- **Verifica:**
  - test pytest: dopo una riconnessione con lo stesso `client_id` la history c'è;
  - sull'iPhone: blocco e sblocco con memoria conservata; modalità aereo e recupero; server spento mostra la pagina offline; il wasm da 11 MB non viene riscaricato.

### M4: API per comandi rapidi
- **[server/session.py](server/session.py):**
  - estrarre `run_text(text)` da `handle_text`, senza lock (lo prende il chiamante);
  - nuova `HeadlessSession(Session)` con `ws=None`, che raccoglie gli eventi per turno.
- **`server/phone.py`** (nuovo): `PhoneSessions`.
  - Lock per sessione, TTL 600 s, al massimo 8 sessioni.
  - `command()` azzera il pending (un "sì" mandato a `/command` non esegue mai azioni rischiose). Attende il turno fino a `API_TIMEOUT_S` (20 s); oltre risponde `running` senza annullare il turno, e la risposta arriva poi via push.
  - Azioni rischiose: con `API_ALLOW_RISKY=false` (default) la risposta è `blocked` ("usa l'app"). Con `true` è `needs_confirmation`, e poi `/api/confirm` con PIN entro 60 s.
  - Un confirm scaduto o sconosciuto dà 410 e **non arriva mai all'LLM**.
  - Si riusano senza modifiche `_resolve_pending` e `yes_no` di [server/tools.py](server/tools.py).
- **`server/api.py`** (nuovo):
  - `POST /api/command`;
  - `POST /api/confirm`;
  - `GET /api/status` (rotta, tool, provider, warnings);
  - router incluso prima del mount statico.
- **`tests/test_api.py`:** 401/503, `done` con chiamata in FakeHome, `blocked`, conferma con PIN, 410 senza LLM, `running`, rotta Google che risponde "inviato", nessuna regressione sulla chat WS.
- **Verifica:** `curl` con Bearer verso `https://$PUBLIC_HOST/api/command`, latenza sotto 8 s.

### M5: Siri, widget, Centro di Controllo (documentazione)
- **`docs/IPHONE.md`** (nuovo):
  - Shortcut **"Assistente casa"**: Detta testo, POST `/api/command`, ramo per `needs_confirmation`, Pronuncia la risposta, ramo di errore "Mac non raggiungibile";
  - "Scrivi a casa";
  - pulsanti fissi ("Spegni tutte le luci");
  - come aggiungerli a widget, Centro di Controllo e tasto Azione;
  - **mai** serrature o porte da Lock Screen;
  - non condividere gli Shortcuts (il token è in chiaro);
  - procedura per telefono perso.
- Aggiornare [README.md](README.md) e [docs/GUIDA.md](docs/GUIDA.md) con il rimando.
- **Verifica:** "Ehi Siri, Assistente casa" → "spegni la luce" riceve risposta letta; un comando rischioso risponde `blocked`; con il server spento si sente "non raggiungibile".

### M6: notifiche (Web Push)
- **Dipendenza:** `pywebpush` in `pyproject.toml` e `uv.lock`.
- **Config:** `VAPID_*` (subject `mailto:` obbligatorio), `PUSH_STORE=data/push_subscriptions.json`, `HA_NOTIFY_ENTITY`.
- **`server/push.py`** (nuovo): `PushHub`.
  - File con permessi 0600, al massimo 10 iscrizioni, solo host `web.push.apple.com` (anti-SSRF).
  - Le iscrizioni che rispondono 404 o 410 vengono rimosse.
  - Manda anche un toast alle sessioni WS aperte.
- **`server/ha_events.py`** (nuovo): polling di `input_text.voicebot_notify` ogni 10 s con `HA_TOKEN` non-admin, push quando cambia, nessun replay all'avvio.
- **[server/ha.py](server/ha.py):** metodo `state(entity_id)`.
- **[server/tools.py](server/tools.py):** tool `notify_phone` ("invia subito una notifica"), sul modello di `ANNOUNCE_TOOL`, attivo in home e in away.
- **Endpoint:** `/api/push/{key,subscribe,unsubscribe,test}` (login più Origin) e `/api/notify` (Bearer).
- **Push di stato:** quando HA va giù o torna su, con debounce di 120 s.
- **Web:** handler `push` e `notificationclick` in `sw.js`; pulsante "Notifiche sul telefono" (solo in standalone) con Prova e Disattiva; banner per i warnings.
- **`tests/test_push.py`:** `webpush` sostituito da un mock.
- **Utente:** in HA creare l'helper `input_text.voicebot_notify` e le automazioni che lo scrivono.
- **Verifica:** `curl /api/notify` arriva a iPhone bloccato; un cambio dell'helper arriva in circa 15 s; "mandami sul telefono: compra il latte" produce un push.

### M7: Mac sempre raggiungibile
- **`scripts/serve.sh`:** `caffeinate -is uv run python -m server`, con un avviso se `serve` non punta alla 8001.
- **LaunchAgent** facoltativo, come esempio in `docs/launchd/`.
- **[scripts/check_env.py](scripts/check_env.py):** controlla Tailscale Running, `pmset`, `/healthz` raggiungibile su ts.net, chiavi VAPID.
- **`docs/IPHONE.md`:**
  - sezione "Mac sempre acceso": coperchio aperto con alimentazione, oppure clamshell; avviso su `disablesleep`;
  - tabella dei guasti.

## Sicurezza (sintesi)
- **Esposizione:** solo loopback; solo la 8001 via `serve`; ACL di Tailscale; `check_env` fallisce se c'è un funnel o se `serve` punta alla 8000.
- **Autenticazione:** login Tailscale fail-closed; Origin e Host controllati; Bearer per dispositivo per `/api`; la PWA non contiene segreti.
- **Azioni rischiose:** la conferma deterministica resta com'è; dall'API sono disattivate di default.
- **Segreti:** in `.env` (permessi 600) e `data/` (gitignored). Il server non usa `HA_ADMIN_TOKEN`.

## Rischi e limiti
- La voce non funziona a schermo bloccato, solo Siri. Il comportamento di Siri a telefono bloccato e il suo timeout vanno misurati.
- Il WebSocket attraverso `serve` non è documentato ufficialmente: si verifica in M1.
- Se il Mac dorme, non funziona nulla, notifiche comprese.
- Fuori casa la rotta Google conferma solo l'invio. Gli eventi HA arrivano solo dalle integrazioni cloud.
- Wake lock in standalone richiede iOS 18.4 o successivo. Web Push richiede iOS 16.4 o successivo e l'app installata sulla Home.
- LLM e TTS sono condivisi tra telefono e desktop: va usata una sola sessione vocale alla volta.

## Verifica end-to-end
1. `uv run pytest -q` è tutto verde e `check_env.py` non mostra FAIL. Sul Mac, `e2e_browser.py` e `e2e_ws.py` passano.
2. **Sicurezza:** VPN spenta → irraggiungibile; login non ammesso → 403; `/ws` da origine estranea → 1008; `/api` senza Bearer → 401.
3. **iPhone in rete cellulare, fuori casa:**
   - voce dalla PWA (barge-in, blocco schermo);
   - chat;
   - Siri;
   - comando rischioso → `blocked`;
   - widget;
   - push (curl, tool `notify_phone`, automazione HA);
   - server spento → "Mac non raggiungibile".
4. `git diff main --stat`, poi PR da `feat/iphone`, senza attribuzioni.
