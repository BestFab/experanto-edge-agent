# WG_HUB.md — l'hub WireGuard per l'accesso remoto ai Pi

Setup del tuo VPS come **hub WireGuard**. Nessun servizio di terzi. Una **sola porta UDP** →
coesiste con nginx/sshd sullo stesso host (anche **UDP/443**, senza conflitti con il TCP).

## Concetto
Ogni Pi è un **peer** WG con un IP overlay fisso (`10.8.0.X`). L'hub (`10.8.0.1`) sta sul VPS
pubblico. Il Pi dietro NAT tiene aperto il buco con `PersistentKeepalive`; tu fai **SSH diretto**
all'IP overlay del Pi (dall'hub, o come peer tu stesso).

```
Pi(10.8.0.5) ──WG/UDP──▶ hub VPS(pubblico:51820 = 10.8.0.1) ◀──WG── tu(10.8.0.2):  ssh utente@10.8.0.5
```

## 1. Installa + chiavi dell'hub (una volta)
```bash
sudo apt-get install -y wireguard-tools
umask 077
wg genkey | sudo tee /etc/wireguard/hub.key | wg pubkey | sudo tee /etc/wireguard/hub.pub
# hub.pub -> la passi ai Pi con --wg-hub-pubkey. hub.key resta segreta sull'hub.
```

## 2. Config hub  `/etc/wireguard/wg-experanto.conf`
```
[Interface]
Address = 10.8.0.1/24
ListenPort = 51820          # UDP. Metti 443 (UDP) per l'egress piu' amichevole.
PrivateKey = <contenuto di /etc/wireguard/hub.key>

# --- un [Peer] per ogni Pi (aggiunto quando install.sh stampa la sua PublicKey) ---
[Peer]
PublicKey = <PublicKey del Pi>
AllowedIPs = 10.8.0.5/32    # l'IP overlay UNICO di quel Pi
```
```bash
sudo systemctl enable --now wg-quick@wg-experanto
sudo ufw allow 51820/udp    # se usi un firewall (o la porta scelta)
```

## 3. Aggiungere un Pi (per ogni datalogger)
1. Assegna un IP overlay **unico**: `10.8.0.5/32`, `10.8.0.6/32`, … (tieni un registro).
2. Sul Pi: `install.sh … --wg-endpoint <hub_pubblico>:51820 --wg-hub-pubkey <hub.pub> --wg-address 10.8.0.5/32 [--wg-persistent] [--wg-ssh-user <utente_pi>]`.
3. install.sh stampa la `PublicKey` del Pi → registrala sull'hub:
```bash
sudo wg set wg-experanto peer <PI_PUBKEY> allowed-ips 10.8.0.5/32
sudo wg-quick save wg-experanto        # persiste il peer nel file
```

## 4. Raggiungere un Pi
- **Dall'hub** (è `10.8.0.1`): `ssh <utente_pi>@10.8.0.5`.
- **Dal tuo laptop**: aggiungilo come **peer** dell'hub (config WG con `AllowedIPs = 10.8.0.0/24`
  verso l'hub) → SSH diretto a `10.8.0.X`, come fossi in LAN con i Pi.

## 5. Porta / egress dai siti clienti
Metti `ListenPort` su una porta che l'egress dei clienti lascia uscire: **UDP/443** (passa quasi
ovunque) o UDP/51820. Se una rete cliente blocca **tutto** l'UDP (raro), WG non passa → in quel
caso usa il fallback **reverse-SSH su TCP** (vedi `BASTION.md`).

## 6. Igiene / sicurezza
- Un IP overlay + una chiave per device; su **revoca**: `sudo wg set wg-experanto peer <PUBKEY> remove`.
- `AllowedIPs = 10.8.0.X/32` per-Pi sull'hub → ogni Pi può usare **solo** il suo IP (no spoofing).
- L'unica porta esposta è quella UDP: nessun TCP nuovo → **zero conflitti** con nginx(443/tcp)/sshd(22/tcp).
- L'agente alza/abbassa l'interfaccia via `sudo -n wg-quick up/down` (sudoers scoped da install.sh);
  `--wg-persistent` invece la tiene sempre su con `wg-quick@`.
