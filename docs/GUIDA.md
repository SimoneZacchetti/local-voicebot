# Guida completa

Chatbot voce-a-voce che gira tutto in locale su Apple Silicon, gratis. Parli, lui risponde a voce nella tua lingua. Puoi interromperlo quando vuoi.

```
Browser (Chrome)                               Server (FastAPI, localhost)
┌────────────────────────────┐   WebSocket   ┌──────────────────────────────────────────┐
│ mic → Silero VAD (vad-web) ├──── PCM16 ───►│ Parakeet STT (MLX) → lingua (lingua-py)  │
│                            │               │ → Ollama Qwen 3.6 (streaming)            │
│ AudioWorklet player  ◄─────┼──── PCM16 ────┤ → chunker frasi → Pocket TTS (streaming) │
│ barge-in, UI               │◄──── JSON ────┤   (un modello per lingua, CPU)           │
└────────────────────────────┘               └──────────────────────────────────────────┘
```

Latenza misurata su M5 Pro: **~0,4–0,8 s** dal momento in cui il server riceve la frase al primo audio (STT ~70 ms, LLM TTFT ~50–450 ms, TTS primo chunk ~25 ms), più l'attesa di silenzio del VAD (550 ms).

## Requisiti

- Mac Apple Silicon, ≥ 32 GB di RAM (il backend MLX di Ollama lo richiede; con meno usa `gemma4:e4b-mlx`, meno preciso)
- [uv](https://docs.astral.sh/uv/), Homebrew, Google Chrome desktop

## Installazione

```bash
brew install --cask ollama                 # l'app, non la formula
launchctl setenv OLLAMA_KEEP_ALIVE -1      # tiene il modello in memoria; poi riavvia l'app Ollama
open -a Ollama
ollama pull qwen3.6:35b-a3b-nvfp4          # 23 GB, MoE con 3B parametri attivi: veloce e affidabile con gli strumenti

uv sync                                    # crea .venv con Python 3.12 e le dipendenze
cp .env.example .env                       # opzionale: tutti i valori hanno un default
uv run python scripts/check_env.py         # verifica tutto (scarica i modelli STT/TTS al primo avvio)
```

## Avvio

```bash
uv run uvicorn server.main:app --host 127.0.0.1 --port 8000
```

Apri <http://localhost:8000> in Chrome, premi "Inizia a parlare" e parla. La sfera al centro segue la voce in tempo reale: la tua quando parli tu, quella del bot quando risponde. Il colore cambia con lo stato (blu: ascolta/parla, viola: sta pensando, grigio: microfono spento). Sotto la sfera scorrono i sottotitoli; la cronologia completa è nel pannello in alto a destra. Barra spaziatrice o il pulsante microfono: silenzia/riattiva il microfono. Il primo avvio scarica i modelli (~4 GB); dopo, il server è pronto in ~6 s.

- **Interrompere**: parla sopra al bot. L'audio va in pausa subito; se è davvero una frase (non eco o rumore) la risposta viene scartata e lui ascolta. Nella cronologia resta solo la parte che hai sentito davvero.
- **Zittirlo**: tocca il cerchio mentre parla. Smette di generare e ricorda solo la parte che hai sentito.
- **Cambiare lingua**: parla direttamente nell'altra lingua, oppure chiedilo ("parliamo in inglese", "speak French").
- **Pause a metà frase**: se riprendi a parlare entro ~1,2 s da quando lui ha iniziato a rispondere, le due parti vengono unite in una sola domanda.
- **Lingue**: la lingua è rilevata dal testo trascritto. Frasi brevi ("sì", "ok") mantengono la lingua corrente. Voci locali per italiano, inglese, francese, tedesco, spagnolo, portoghese e olandese; le altre lingue europee riconosciute da Parakeet usano la voce del browser.
- **Chat scritta**: l'icona del fumetto in alto apre il pannello con un campo di testo (Invio per mandare, Maiusc+Invio per andare a capo). È la stessa conversazione della voce: con la voce attiva il bot risponde anche a voce, altrimenti solo per iscritto. Puoi scrivere senza mai premere "Inizia a parlare", e passare alla voce in qualsiasi momento senza perdere il contesto. "Termina" spegne microfono e audio ma la conversazione resta; "Nuova conversazione" la azzera. Un messaggio scritto mentre il bot risponde lo interrompe come la voce, ma non viene mai unito al messaggio precedente.
- **Altoparlanti**: con le cuffie è tutto più robusto. Senza cuffie Chrome cancella l'eco; se il bot si interrompe da solo, attiva "Modalità altoparlanti" in basso.

## Controllo della casa (dispositivi di Google Home)

Google non offre un'API gratuita per comandare da un server i dispositivi collegati a Google Home (le Home APIs sono solo per app Android/iOS; il Google Home MCP è a pagamento e solo negli USA). Per questo il voicebot passa da **Home Assistant** (gratuito, locale), che si collega agli stessi dispositivi, e lo usa tramite il suo server MCP. L'LLM sceglie lo strumento giusto ("spegni la luce della cucina" → `HassTurnOff`), Home Assistant esegue, il bot conferma a voce.

Cosa puoi fare: accendere/spegnere/regolare luci e prese, termostati, tapparelle, ventilatori, aspirapolvere; chiedere lo stato ("la luce in camera è accesa?", "che temperatura c'è?"); controllare speaker e TV (pausa, avanti, volume, cerca e riproduci se hai Music Assistant); liste (spesa); script di Home Assistant (le tue "routine"); annunci sugli speaker. Serrature, porte, cancelli, garage e allarmi chiedono sempre conferma ("Vuoi davvero sbloccare la porta?" → "sì"): la domanda la costruisce il codice dall'azione vera, e il "sì" è verificato senza LLM. Puoi rispondere a voce o con i pulsanti Sì/No.

Cosa **non** si può fare (nessuna API): avviare le routine create nell'app Google Home, timer/sveglie sugli speaker Nest, funzioni Gemini for Home. Ricrea le routine come script in Home Assistant.

### Setup (una volta)

1. **Installa Home Assistant OS** in una VM sul Mac con [UTM](https://mac.getutm.app) (rete in modalità *bridged*, così vede i dispositivi in casa), oppure su un mini-PC / Raspberry / Home Assistant Green. Non usare Docker sul Mac: senza host networking la scoperta dei dispositivi non funziona. In UTM aggiungi anche una seconda scheda di rete **Host Only**: il bridged sul Wi-Fi del Mac perde a tratti il collegamento Mac ↔ VM, la Host Only no (usa il suo IP, es. `192.168.128.2`, per browser e voicebot). Imposta la lingua in italiano.
2. **Porta i dispositivi in Home Assistant**:
   - dispositivi **Matter** già in Google Home: app Google Home → dispositivo → *Condivisione dispositivo / Collega app e servizi* → Home Assistant (serve l'app Home Assistant sul telefono);
   - altri marchi (Hue, TP-Link/Tapo, Tuya/Smart Life, Shelly, IKEA…): aggiungi la loro integrazione in *Impostazioni → Dispositivi e servizi*;
   - speaker Nest, Chromecast, Google TV: integrazione **Google Cast** (si trovano da sole).
   - Dai a dispositivi e stanze nomi italiani chiari ("Luce cucina", area "Cucina"): il bot userà quelli. Se aggiungi sinonimi (alias) a un'entità, metti tra gli alias anche il nome principale: con gli alias presenti Home Assistant riconosce solo quelli.
   - Musica (Spotify Premium): installa il componente aggiuntivo **Music Assistant**, collega Spotify dalla sua pagina e esponi al bot i suoi lettori; con `HA_DEFAULT_PLAYER=Speaker Salotto` il bot sa dove suonare quando non dici la stanza.
3. *Impostazioni → Assistenti vocali → Esponi*: scegli quali dispositivi può comandare il bot.
4. Aggiungi l'integrazione **Model Context Protocol Server** (API: Assist).
5. Crea un utente dedicato **non amministratore** (es. "voicebot"), accedi come lui e in *Profilo → Sicurezza* crea un **token di accesso a lunga durata**.
6. (Opzionale) Integrazione **Google Assistant SDK**: aggiunge lo strumento `google_assistant` per i comandi che esistono solo in Google (es. "accendi la TV del salotto") e gli annunci sugli speaker. È un'API sperimentale che Google sta dismettendo: può smettere di funzionare.
7. Nel `.env` del voicebot:

   ```dotenv
   HOME_BACKEND=ha
   HA_URL=http://192.168.128.2          # VM in UTM: usa l'IP della scheda "Host Only" (collegamento Mac-VM stabile)
   HA_TOKEN=eyJ...
   HA_ANNOUNCE_PLAYERS=media_player.cucina,media_player.salotto   # opzionale, annunci via tts.speak
   ```

8. `uv run python scripts/check_env.py` verifica connessione, strumenti, dispositivi esposti e annunci. Riavvia il server: nei log vedi `home tools: …`.

Il server ascolta solo su `127.0.0.1`: non esporlo in rete, chiunque potrebbe comandare la casa.

## Controllo della casa da fuori (Google Assistant)

Se Home Assistant gira nella VM sul Mac e porti il Mac fuori casa, i dispositivi locali (Cast, Matter, Music Assistant) non sono più raggiungibili. In quel caso il voicebot manda i comandi al **cloud di Google**, che li esegue sui dispositivi di casa: luci, prese, TV, speaker, Spotify, termostato, routine di Google Home.

1. In Home Assistant aggiungi l'integrazione **Google Assistant SDK** (*Impostazioni → Dispositivi e servizi → Aggiungi*). Serve un progetto Google Cloud con le credenziali OAuth: la procedura guidata di Home Assistant spiega i passaggi. Accedi con lo stesso account Google di Google Home.
2. Nel `.env`:

   ```dotenv
   HOME_ROUTE=auto                 # a casa: strumenti locali; fuori: Google
   HOME_PROBE=192.168.1.50:8009    # IP di un Google Home/Chromecast di casa (app Google Home → dispositivo → Impostazioni → Informazioni)
   ```

   Con `HOME_ROUTE=google` usa sempre Google, anche in casa. Con `HOME_ROUTE=ha` (default) usa solo gli strumenti locali.
3. `uv run python scripts/check_env.py` mostra la modalità attuale (`home route: home` o `away`). Riavvia il server.

Con `auto` il bot controlla ogni 30 s (`HOME_PROBE_TTL_S`) se il Google Home risponde: se sì sei a casa, altrimenti passa a Google. Il cambio avviene da solo, anche a conversazione aperta.

Limiti: Google non conferma l'esito, quindi il bot dice "comando inviato", non "fatto"; cosa suonare lo decide Google, non Music Assistant; è un'API sperimentale che Google può spegnere. Anche via Google, i comandi che citano serrature, porte, cancelli, allarmi o un dispositivo di quel tipo chiedono conferma.

## Controllo della casa da fuori con Home Assistant a casa (Tailscale)

Con [Tailscale](https://tailscale.com) (gratuito per uso personale) il voicebot comanda la casa da qualsiasi rete, come se fossi a casa. Nessuna porta aperta sul router: Mac e Home Assistant entrano in una rete privata cifrata.

**Prerequisito:** Home Assistant deve restare **a casa e sempre acceso** (Home Assistant Green, Raspberry Pi, mini-PC o un Mac fisso). Se gira in una VM sul portatile che porti con te, fuori casa non vede più i dispositivi.

1. **In Home Assistant:** *Impostazioni → Componenti aggiuntivi → Store* → **Tailscale** → Installa → Avvia → *Apri interfaccia web* e accedi con il tuo account Tailscale. Attiva *Avvio all'accensione*.
2. **Sul Mac:** `brew install --cask tailscale`, apri l'app dalla barra dei menu e accedi con **lo stesso account**.
3. Nella [console di Tailscale](https://login.tailscale.com/admin/machines) controlla che compaiano entrambi (Home Assistant di solito come `homeassistant`). In *DNS* lascia attivo **MagicDNS**. Sul dispositivo di Home Assistant disattiva *Key expiry*, così non si scollega dopo 180 giorni.
4. Nel `.env` del voicebot:

   ```dotenv
   HA_URL=http://homeassistant:8123     # nome MagicDNS; in alternativa l'IP 100.x.y.z mostrato dalla console
   ```

5. `uv run python scripts/check_env.py`: deve dire `HA_URL … via Tailscale`. Prova poi dall'hotspot del telefono.

Lo stesso indirizzo funziona a casa e fuori: in casa Tailscale si collega in diretta sulla rete locale, senza rallentamenti percepibili. Se Tailscale non è ancora connesso quando avvii il server, il bot parte senza strumenti per la casa e riprova da solo ogni `HA_RETRY_S` secondi (default 30). Musica e annunci partono comunque dagli speaker di casa: Music Assistant gira accanto a Home Assistant.

Il traffico è cifrato da Tailscale, quindi `http://` va bene. **Non** esporre Home Assistant su internet aprendo porte sul router.

## Groq (opzionale, free tier)

Per usare modelli cloud gratuiti al posto di quelli locali, crea una chiave su <https://console.groq.com> e nel `.env`:

```dotenv
GROQ_API_KEY=gsk_...
LLM_PROVIDER=groq        # qwen/qwen3.8-27b
STT_PROVIDER=groq        # whisper-large-v3-turbo
```

Se Groq risponde con errore o rate limit (free tier: ~8K token/min per l'LLM, 20 richieste/min per lo STT) il server passa automaticamente ai modelli locali per 60 s e lo segnala nella UI.

## Configurazione

Tutte le chiavi sono in [.env.example](../.env.example). Le più utili:

| Chiave | Default | Note |
|---|---|---|
| `LLM_MODEL` | `qwen3.6:35b-a3b-nvfp4` | `gemma4:26b-nvfp4`: migliore nei calcoli, ma Ollama perde ~10% delle sue chiamate agli strumenti (il bot ritenta). `scripts/bench_tools.py` confronta i modelli sui comandi di casa |
| `VAD_REDEMPTION_MS` | `550` | silenzio che chiude il turno; più basso = più reattivo ma ti interrompe nelle pause |
| `CONTINUATION_S` | `1.2` | finestra per unire una frase ripresa subito dopo l'inizio della risposta |
| `DEFAULT_LANG` | `it` | lingua iniziale |
| `VOICE_IT` … `VOICE_NL` | `giovanni`, `alba`, … | voci predefinite Pocket TTS |
| `STT_PROVIDER` | `parakeet` | `whisper-mlx` (richiede `uv sync --extra whisper`) per lingue extra-europee |

## Test

```bash
uv run pytest                                  # unit test + protocollo WebSocket con provider finti
uv run python scripts/bench_llm.py             # TTFT del modello
uv run python scripts/e2e_ws.py                # E2E senza browser: voce sintetica → risposta → trascrizione
uv run python scripts/e2e_browser.py           # E2E in Chrome con microfono finto (VAD reale)
uv run python scripts/e2e_browser.py --barge   # come sopra, con interruzione e frase spezzata
HA_TOKEN=... uv run python scripts/e2e_home.py # comandi vocali reali verso Home Assistant (server con HOME_BACKEND=ha)
```

## Struttura

| File | Ruolo |
|---|---|
| [server/session.py](../server/session.py) | protocollo WebSocket, turni (voce e chat scritta), barge-in, cronologia, pipeline STT→LLM→TTS |
| [server/stt.py](../server/stt.py), [server/llm.py](../server/llm.py), [server/tts.py](../server/tts.py) | provider con fallback automatico Groq → locale |
| [server/langid.py](../server/langid.py) | rilevamento lingua con isteresi |
| [server/chunker.py](../server/chunker.py) | spezza lo stream dell'LLM in frasi pronunciabili (primo pezzo anticipato) |
| [server/tools.py](../server/tools.py), [server/ha.py](../server/ha.py) | strumenti per la casa (MCP di Home Assistant), conferme per azioni rischiose |
| [server/textclean.py](../server/textclean.py) | pulizia markdown/emoji, filtri allucinazioni ed eco |
| [web/app.js](../web/app.js), [web/player-worklet.js](../web/player-worklet.js) | microfono, VAD, riproduzione senza buchi, interruzioni, chat scritta |

## Licenze e attribuzioni

- Modello STT [nvidia/parakeet-tdt-0.6b-v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3) (CC-BY-4.0), porting MLX [parakeet-mlx](https://github.com/senstella/parakeet-mlx).
- TTS [Kyutai Pocket TTS](https://github.com/kyutai-labs/pocket-tts) (codice MIT, pesi CC-BY-4.0; le voci hanno licenze proprie, vedi [kyutai/tts-voices](https://huggingface.co/kyutai/tts-voices)).
- LLM [Gemma 4](https://ai.google.dev/gemma) via [Ollama](https://ollama.com).
- VAD [@ricky0123/vad-web](https://github.com/ricky0123/vad) (Silero VAD, MIT) e [onnxruntime-web](https://github.com/microsoft/onnxruntime) (MIT), inclusi in `web/vendor/`.
