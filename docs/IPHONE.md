# Voicebot dall'iPhone

Usa il voicebot dal telefono, a casa e fuori:

- **App "Casa"** sulla schermata Home: voce e chat, come sul Mac.
- **Siri, widget, Centro di Controllo e tasto Azione**: un comando alla volta, anche a telefono bloccato.

Il Mac resta il "cervello": deve essere acceso e sveglio. Il telefono lo raggiunge attraverso **Tailscale**, una rete privata cifrata. Non ci sono porte aperte sul router e niente è esposto su internet.

```
iPhone ──Tailscale──► https://<mac>.<tailnet>.ts.net:9443 ──► Mac 127.0.0.1:8001 (porta "telefono")
                                                              Mac 127.0.0.1:8000 (browser del Mac)
```

## 1. Configurazione (una volta)

### Tailscale

1. **Mac**: installa la versione *Standalone* da <https://pkgs.tailscale.com/stable/#macos>, fai login e attiva la CLI (icona nella barra dei menu → *Settings* → *CLI integration*).
2. **iPhone**: installa l'app [Tailscale](https://apps.apple.com/app/tailscale/id1470499037), fai login con **lo stesso account** e attiva **VPN On Demand**.
3. Dai al Mac un nome neutro, solo dentro Tailscale. Il nome finisce nel registro pubblico dei certificati HTTPS. Il nome del Mac in macOS non cambia.

   ```bash
   tailscale set --hostname=voicebot-mac
   ```

4. Nella [console DNS](https://login.tailscale.com/admin/dns) attiva **MagicDNS** e **HTTPS Certificates**.

### Server

1. Genera un token per ogni dispositivo. Lo script stampa i token: copiali nel `.env` e non condividerli.

   ```bash
   uv run python scripts/gen_secrets.py iphone
   ```

2. Nel `.env`:

   ```dotenv
   REMOTE_ENABLED=true
   REMOTE_PORT=8001
   PUBLIC_HOST=voicebot-mac.tailXXXX.ts.net:9443   # nome da `tailscale status`; :9443 vedi sotto
   TS_ALLOWED_LOGINS=tuo.account@gmail.com         # il login Tailscale ammesso
   API_TOKENS=<token generato>
   ```

   Poi esegui `chmod 600 .env`.

3. Avvia il server con le due porte:

   ```bash
   uv run python -m server
   ```

4. Pubblica **solo** la porta per il telefono:

   ```bash
   tailscale serve --bg --https=9443 8001
   ```

   Si usa la 9443 perché sul Mac la 443 può essere occupata (per esempio da Traefik di Docker). Se la tua 443 è libera, usa `tailscale serve --bg 8001` e togli `:9443` da `PUBLIC_HOST`.

5. Lancia `uv run python scripts/check_env.py`. Deve risultare tutto `OK`: `.env` non tracciato e con permessi 600, `tailscale serve` che pubblica solo `127.0.0.1:8001`, funnel spento.

> **Mai** `tailscale funnel`, mai `HOST=0.0.0.0`, mai port forwarding sul router: il server comanda la casa.

## 2. App "Casa" (voce e chat)

1. Apri Safari sull'iPhone e vai su `https://voicebot-mac.tailXXXX.ts.net:9443`.
2. Tocca **Condividi** → **Aggiungi alla schermata Home**.
3. Apri l'icona **Casa** e tocca **Inizia a parlare**. Concedi il microfono: in iOS l'app installata può chiederlo di nuovo a ogni apertura.

Cosa aspettarsi:

- **Voce**: funziona solo con l'app aperta e lo schermo acceso. Durante la conversazione lo schermo resta acceso da solo.
- **Blocco schermo o cambio di rete**: l'app si riconnette da sola ("Riconnessione…"). Il bot ricorda la conversazione per 15 minuti (`CLIENT_HISTORY_TTL_S`).
- **Chiamate e Siri**: interrompono l'audio. Compare **"tocca per riprendere"**.
- **Chat**: l'icona del fumetto in alto apre la chat, che funziona senza microfono.
- **Eco**: con l'altoparlante del telefono a volume alto parte della voce del bot può tornare nel microfono. Il bot la riconosce quasi sempre. Con AirPods o cuffie il problema sparisce.
- **Mac spento o Tailscale disattivato**: l'app mostra "Mac non raggiungibile".

## 3. Siri e Comandi Rapidi

I comandi rapidi chiamano `POST /api/command`. Il bot esegue la richiesta con lo stesso modello, gli stessi strumenti e la stessa sicurezza della voce, e risponde con un testo che Siri legge ad alta voce.

### "Assistente casa" (voce, a mani libere)

Nell'app **Comandi Rapidi** tocca **+** e aggiungi queste azioni:

1. **Detta testo**: lingua *Italiano*, *Interrompi ascolto: Dopo una pausa*.
2. **Ottieni contenuto dell'URL**:
   - URL: `https://voicebot-mac.tailXXXX.ts.net:9443/api/command`
   - Metodo: **POST**
   - Intestazioni:
     - `Authorization` = `Bearer <il tuo token>`
     - `Content-Type` = `application/json`
   - Corpo della richiesta: **JSON**
     - `text` (Testo) = variabile *Testo dettato*
     - `session_id` (Testo) = `phone`
3. **Ottieni valore del dizionario**: chiave `reply` da *Contenuto dell'URL*.
4. **Pronuncia testo**: *Valore del dizionario*.

Chiamalo **"Assistente casa"**. Evita "Casa": si confonde con l'app Casa di Apple. Poi prova: *"Ehi Siri, Assistente casa"* → *"spegni la luce in cucina"*.

`session_id: "phone"` mantiene la conversazione per 10 minuti (`PHONE_SESSION_TTL_S`): dopo *"accendi la luce in cucina"*, *"e in camera?"* funziona.

### "Scrivi a casa" (testo)

È uguale a "Assistente casa", ma al posto di **Detta testo** usa **Chiedi input** (tipo Testo). Serve per scrivere in silenzio.

### Pulsanti fissi

Un comando rapido senza dettatura, con `text` fisso, per esempio `spegni tutte le luci` o `accendi la TV del salotto`. Basta un tocco.

### Dove metterli

- **Widget**: tieni premuto sulla schermata Home → **Modifica** → **Aggiungi widget** → **Comandi Rapidi**.
- **Centro di Controllo** e **schermata di blocco** (iOS 18): **Modifica** → **Aggiungi un controllo** → **Comandi Rapidi** → scegli il comando.
- **Tasto Azione** (iPhone 15 Pro e successivi): **Impostazioni** → **Tasto Azione** → **Comando rapido**.

### Risposte possibili

| `status` | Significato |
|---|---|
| `done` | Fatto: `reply` è la risposta. Fuori casa, con i comandi via Google, `reply` dice "inviato". |
| `running` | Il bot ci mette più di `API_TIMEOUT_S` (20 s). Il comando non viene annullato. |
| `blocked` | Serrature, porte o allarmi: da Siri sono **bloccati di default**. Usa l'app. |
| `needs_confirmation` | Solo con `API_ALLOW_RISKY=true`: serve una conferma (vedi sotto). |
| `error` | Messaggio vuoto o errore del server. |

Se il Mac non è raggiungibile, **Ottieni contenuto dell'URL** fallisce e Comandi Rapidi mostra un errore.

### Serrature e porte da Siri (sconsigliato)

Di default sono rifiutate. Per abilitarle:

1. Nel `.env` imposta `API_ALLOW_RISKY=true` e un `API_CONFIRM_PIN=<pin>`.
2. Nel comando rapido, dopo **Ottieni contenuto dell'URL** aggiungi:
   1. **Se** `status` è `needs_confirmation`:
      - **Pronuncia testo** `question`;
      - **Scegli dal menu**: Sì / No;
      - **Chiedi input** (PIN);
      - **Ottieni contenuto dell'URL**: `POST /api/confirm` con JSON `{"session_id": "phone", "yes": true|false, "pin": "<input>"}` e le stesse intestazioni.
   2. Hai **60 secondi** (`API_CONFIRM_TTL_S`). Una conferma scaduta risponde `expired` e non esegue nulla.
   3. Un "sì" mandato come comando normale non conferma mai niente.

**Mai** collegare comandi per serrature, porte o allarmi alla schermata di blocco o al Centro di Controllo.

## 4. Sicurezza

- **Chi può collegarsi**: solo i dispositivi della tua tailnet con un login in `TS_ALLOWED_LOGINS`. Tutti gli altri ricevono 403. Il login lo inserisce Tailscale e non si può falsificare dal telefono.
- **Origine dell'app**: l'app web deve arrivare esattamente da `https://PUBLIC_HOST`. Siri invece usa il token.
- **Token**: `API_TOKENS` contiene un token per dispositivo, così puoi revocarne uno solo.
  - Il token è scritto in chiaro dentro il comando rapido e si sincronizza via iCloud: **non condividere** quel comando rapido.
  - Il server non scrive mai i token nei log.
- **Browser del Mac**: sulla porta locale `/ws` accetta solo le pagine di `127.0.0.1` e `localhost`, quindi un sito qualsiasi aperto in Chrome non può comandare la casa.

### Telefono perso

1. Nella [console Tailscale](https://login.tailscale.com/admin/machines) rimuovi il dispositivo.
2. Togli il suo token da `API_TOKENS` nel `.env` e riavvia il server.

## 5. Problemi comuni

| Sintomo | Causa probabile | Cosa fare |
|---|---|---|
| "Mac non raggiungibile" | Mac in stop, server fermo o Tailscale spento | Sveglia il Mac, `uv run python -m server`, controlla Tailscale su Mac e iPhone. |
| La pagina non si apre | `tailscale serve` non attivo | `tailscale serve status`. Se è vuoto, `tailscale serve --bg --https=9443 8001`. |
| 403 | Login non ammesso o `PUBLIC_HOST` sbagliato | Confronta `TS_ALLOWED_LOGINS` e `PUBLIC_HOST` con `tailscale status`. Il log del server dice il motivo (`refused … login/host/origin`). |
| Certificato non valido | La 443 del Mac è occupata da un altro programma | Usa `--https=9443` e `PUBLIC_HOST=…:9443`. |
| Siri dice errore | Token sbagliato (401) o Mac irraggiungibile | Controlla l'intestazione `Authorization: Bearer …`. |
| Prima risposta lenta | Dopo inattività, macOS ha spostato il modello sul disco | Il bot lo ricarica quando il telefono si collega. Liberare RAM (Docker, app pesanti) aiuta. |
| Il bot si interrompe da solo | Eco dell'altoparlante | Abbassa il volume o usa le cuffie. |
| Comandi di casa ignorati fuori casa | La rotta Google non è attiva | `HOME_ROUTE=auto` con `HOME_PROBE`, vedi [GUIDA.md](GUIDA.md). |
