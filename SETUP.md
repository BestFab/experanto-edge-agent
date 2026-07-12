# Experanto Edge — Configurazione e avvio su Raspberry Pi

Guida operativa per mettere in **produzione** un Raspberry Pi dedicato: l'agente parte
**da solo al boot**, si **riavvia da solo** dopo un crash o un riavvio del Pi, e ri-spedisce
i dati accumulati quando la rete torna.

> ⚠️ **Pi dedicato.** `install.sh` è invasivo (utente di sistema, `/opt`, `/etc`, systemd,
> sudoers, unattended-upgrades). Usalo solo su un Pi da dedicare a questo. Per **provare**
> senza toccare il sistema (sandbox in `~/edge-test`, niente sudo) vedi `handoff_pi.md`.

---

## 0. Prerequisiti

- **Raspberry Pi 3 o superiore** (64-bit consigliato; il Pi Zero non va — manca il wheel di `cryptography`).
- Raspberry Pi OS / Debian, accesso `sudo`, **internet in uscita** (nessuna porta in ingresso richiesta).
- Il **datalogger Solar-Log Base** sulla stessa LAN, con **API getjp locale su "Open"** (impostazione lato cliente).
- Credenziali del device dalla **SPA** → *Aggiungi datalogger remoto*: `device_code`, `secret` (mostrato **una sola volta**), `station_id`.
- *(Opzionale)* una **authkey Tailscale** (reusable + ephemeral + tagged) per l'SSH remoto.

---

## 1. Installazione (parte da sola al boot)

Copia il sorgente sul Pi e lancia `install.sh`:

```bash
# dal Mac — repo privato: si copia, non si clona con token sul Pi
rsync -av --exclude '.git' --exclude '.venv*' --exclude '__pycache__' \
  ~/Developer/experanto-edge-agent/  "<user>@<pi-ip>:~/experanto-edge-agent/"

# sul Pi
cd ~/experanto-edge-agent
sudo ./install.sh \
  --code    EXP-XXXX-XXXX \
  --secret  <SECRET> \
  --station <STATION_UUID> \
  --broker  mqtt.experanto.it \
  --datalogger-ip 192.168.1.50 \
  --tailscale-authkey tskey-…          # opzionale: abilita l'SSH remoto
  # OTA (opzionale): --update-base-url https://…/releases --update-pubkey <base64>
```

Se ometti `--code`/`--secret` te li chiede interattivamente. Flag:

| Flag | Obbligatorio | Cosa fa |
|---|---|---|
| `--code` | sì | device_code (anche username MQTT) |
| `--secret` | sì | secret del device (password MQTT), mostrato una volta dalla SPA |
| `--station` | consigliato | UUID della station Experanto che il Pi alimenta |
| `--broker` | consigliato | host del broker (default `mqtt.experanto.it`) |
| `--datalogger-ip` | no | IP del Solar-Log sulla LAN; se omesso, l'agente lo cerca da solo |
| `--tailscale-authkey` | no | abilita l'SSH remoto on-demand (installa tailscale + operator mode) |
| `--tailscale-login-server` | no | URL di un Headscale self-hosted (vuoto = Tailscale SaaS) |
| `--update-base-url` / `--update-pubkey` | no | abilita gli aggiornamenti OTA firmati dalla dashboard |

L'installer (una volta): utente di sistema `experanto-edge`, venv in `/opt/experanto-edge`,
config in `/etc/experanto-edge/config.yaml`, **servizio systemd abilitato e avviato**,
`unattended-upgrades` per l'OS.

---

## 2. Avvio automatico e resistenza ai riavvii — come funziona

Il cuore è il servizio systemd `experanto-edge.service` (installato ed **enabled**):

- **`systemctl enable` + `WantedBy=multi-user.target`** → **parte a ogni boot**, senza bisogno di login.
- **`Restart=always`, `RestartSec=10`** → se il processo termina o crolla, systemd lo **riavvia entro 10 s**.
- **`After=/Wants=network-online.target`** → aspetta che la rete sia su prima di partire.
- **Sandbox** (`ProtectSystem=strict`, `ProtectHome`, `NoNewPrivileges`, `PrivateTmp`): scrive solo in `/var/lib/experanto-edge` e `/etc/experanto-edge`.
- **Store-and-forward**: le letture non inviate finiscono in un buffer **SQLite** (`/var/lib/experanto-edge/buffer.db`) che **sopravvive a riavvii e cadute di rete** e viene svuotato al primo contatto utile.

**Verifica la resistenza al riavvio:**

```bash
sudo systemctl is-enabled experanto-edge     # atteso: enabled  (=> parte al boot)
sudo reboot
# riconnettiti dopo il riavvio:
systemctl is-active experanto-edge           # atteso: active   (=> ripartito da solo)
```

---

## 3. Verifica che funzioni

```bash
systemctl status experanto-edge --no-pager
journalctl -u experanto-edge -f              # log in tempo reale (Ctrl-C per uscire)
```

Nei log ti aspetti, a ogni ciclo (default 300 s): **lettura getjp OK** (801/170 + 782) →
connessione al broker → publish `telemetry` + `status`. Se il broker è giù vedrai
*"broker non raggiungibile — bufferizzo"* (normale finché il broker non è deployato: i
dati non si perdono, restano nel buffer).

---

## 4. Configurazione (dopo l'installazione)

Il file è **`/etc/experanto-edge/config.yaml`** (owner `experanto-edge`, permessi 640). Campi principali:

| Campo | Default | Note |
|---|---|---|
| `device_code`, `secret`, `station_id` | — | identità (da enrollment) |
| `broker_host`, `broker_port`, `tls` | `mqtt.experanto.it`, `8883`, `true` | broker |
| `datalogger_ip`, `datalogger_port` | auto, `80` | Solar-Log locale |
| `interval` | `300` | secondi tra un ciclo e l'altro |
| `buffer_path`, `buffer_max_rows` | `/var/lib/experanto-edge/buffer.db`, `5000` | store-and-forward |
| `wg_interface`, `wg_address`, `wg_ssh_user` | default/vuoti | accesso remoto (WireGuard) |
| `ssh_default_ttl` | `900` | durata finestra tunnel (s) |
| `update_base_url`, `update_public_key` | vuoti | OTA firmato |

Per modificare e applicare:

```bash
sudo nano /etc/experanto-edge/config.yaml
sudo systemctl restart experanto-edge
```

---

## 5. Accesso remoto (WireGuard, on-demand o persistente)

Il Pi è dietro NAT/CGNAT (niente porte in ingresso): si unisce al TUO **hub WireGuard**
(**nessun servizio terzo**, una porta UDP che coesiste con nginx/sshd) con un IP overlay fisso;
lo raggiungi via SSH diretto a quell'IP. Setup dell'hub in **[WG_HUB.md](WG_HUB.md)**.

**All'installazione:** `--wg-endpoint <hub>:51820 --wg-hub-pubkey <KEY> --wg-address 10.8.0.5/32`
genera la keypair, scrive `/etc/wireguard/wg-experanto.conf` e stampa la **pubblica** del Pi da
registrare come peer sull'hub. Aggiungi `--wg-persistent` per tenere il link **sempre su**
(consigliato per il bring-up / quando il broker non c'è ancora).

**On-demand** (default, broker attivo): l'interfaccia è giù; sale sul comando **Apri SSH** dalla
dashboard per `ssh_default_ttl` s (default 900), poi si richiude da sola (o con **Chiudi SSH**).

**Raggiungere il Pi** (dall'hub o da un tuo peer WG):
```bash
ssh <utente_pi>@10.8.0.5
```

**Abilitarlo dopo l'installazione** (se non fatto all'install): rilancia
`install.sh … --wg-endpoint … --wg-hub-pubkey … --wg-address 10.8.0.5/32`, poi registra la
pubkey stampata come peer sull'hub (vedi WG_HUB.md).

---

## 6. Gestione

| Azione | Comando |
|---|---|
| Stato | `systemctl status experanto-edge` |
| Log dal vivo | `journalctl -u experanto-edge -f` |
| Riavvia | `sudo systemctl restart experanto-edge` |
| Ferma / avvia | `sudo systemctl stop\|start experanto-edge` |
| Non partire più al boot | `sudo systemctl disable --now experanto-edge` |
| Ri-abilita al boot | `sudo systemctl enable --now experanto-edge` |

Dalla **dashboard** (eseguiti al ciclo successivo): *Leggi ora, Diagnostica, Riscopri,
Riavvia, Aggiorna* (OTA), *Apri/Chiudi SSH*, *Revoca*.

---

## 7. Aggiornamenti

- **Agente (OTA firmato)**: dalla dashboard → *Aggiorna*. L'agente verifica firma Ed25519 +
  sha256, fa lo swap atomico e, se la nuova versione non è sana, **rollback automatico** alla
  precedente. Richiede `--update-base-url`/`--update-pubkey` all'install.
- **Sistema operativo**: `unattended-upgrades` (security) è attivo dall'installazione.

---

## 8. Disinstallazione

Non c'è un `uninstall.sh`; a mano:

```bash
sudo systemctl disable --now experanto-edge unattended-upgrades
sudo rm -rf /opt/experanto-edge /etc/experanto-edge /var/lib/experanto-edge \
            /etc/systemd/system/experanto-edge.service* /etc/sudoers.d/experanto-edge
sudo userdel experanto-edge
sudo systemctl daemon-reload
# se avevi installato tailscale solo per questo:  sudo tailscale logout && sudo systemctl disable --now tailscaled
```

---

## 9. Troubleshooting

- **Datalogger non trovato / lettura fallita** → metti il getjp del Solar-Log su **"Open"**,
  verifica `datalogger_ip`/`datalogger_port` (Solar-Log = porta **80**). Ricerca manuale:
  `sudo -u experanto-edge EXPERANTO_EDGE_CONFIG=/etc/experanto-edge/config.yaml /opt/experanto-edge/current/venv/bin/experanto-edge --discover-datalogger`
- **Non sai che datalogger c'è** (device ethernet ignoto, es. Pi a distanza) → `python3 tools/recon.py`
  sul Pi (solo stdlib, gira via SSH senza venv): scopre gli host, fa il fingerprint (HTTP/API,
  Solar-Log getjp, Fronius, SMA, Modbus+SunSpec, SNMP) e suggerisce quale reader usare.
  `python3 tools/recon.py --json` per incollarne l'output e decidere insieme.
- **Broker non raggiungibile** → normale se `mqtt.experanto.it` non è ancora deployato; i dati
  vanno nel buffer e partono dopo. Controlla `broker_host`/`broker_port`/`tls`.
- **Salute del pacchetto su questo Pi/arch**:
  `sudo -u experanto-edge EXPERANTO_EDGE_CONFIG=/etc/experanto-edge/config.yaml /opt/experanto-edge/current/venv/bin/experanto-edge --selfcheck` → atteso `selfcheck ok <versione>`.
- **Non riparte al boot** → `systemctl is-enabled experanto-edge` deve dire `enabled`; se no
  `sudo systemctl enable experanto-edge`.

---

## Appendice — avvio automatico SENZA root (solo se non puoi usare l'install di sistema)

Meno robusto (niente hardening systemd né OTA helper), ma parte comunque al boot:

```bash
# in una sandbox utente ~/edge-test (vedi handoff_pi.md per crearla)
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/experanto-edge.service <<'UNIT'
[Unit]
Description=Experanto Edge (user)
After=network-online.target
[Service]
ExecStart=%h/edge-test/venv/bin/experanto-edge
Environment=EXPERANTO_EDGE_CONFIG=%h/edge-test/config.yaml
Restart=always
RestartSec=10
[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now experanto-edge
sudo loginctl enable-linger "$USER"     # fa partire i servizi --user al boot senza login
```
