# BASTION.md — il tuo VPS come punto di rendez-vous per l'SSH remoto

Setup del bastion per raggiungere i Pi dietro NAT via **reverse SSH tunnel**. Nessun
software oltre a openssh; **nessun servizio di terzi**. Puoi co-locarlo sullo stesso VPS
del broker/Experanto.

## Concetto
Ogni Pi tiene un `ssh -N -R <reverse_port>:localhost:22` in uscita verso il bastion
(utente `edge-tunnel`). Il bastion lega quella porta sul proprio **loopback**; tu la usi
per arrivare allo sshd del Pi. Ogni Pi ha una `reverse_port` **unica** (22016, 22017, …).

```
Pi (:22) ──ssh -R 22016:localhost:22──▶ bastion(127.0.0.1:22016) ◀── tu:  ssh -J te@bastion -p 22016 …
```

## 1. Utente tunnel dedicato (una volta sola)
```bash
sudo useradd -m -s /usr/sbin/nologin edge-tunnel
sudo install -d -m 700 -o edge-tunnel -g edge-tunnel /home/edge-tunnel/.ssh
sudo install -m 600 -o edge-tunnel -g edge-tunnel /dev/null /home/edge-tunnel/.ssh/authorized_keys
```

## 2. Autorizza la chiave di ogni Pi (una riga per Pi)
`install.sh` sul Pi stampa la sua **chiave pubblica**. Aggiungila alla authorized_keys di
`edge-tunnel`, **ristretta** al solo inoltro:
```
restrict,port-forwarding ssh-ed25519 AAAA...  experanto-edge@EXP-XXXX
```
- `restrict` spegne tutto (pty, agent, X11, user-rc, forwarding); `port-forwarding` ri-abilita
  **solo** l'inoltro, necessario per il `-R`. Nessun `command=` (romperebbe il tunnel `-N`).
- L'utente ha shell `nologin`: chi provasse `ssh edge-tunnel@bastion` senza `-N` viene chiuso.
- Tieni un **registro** device→reverse_port; su **revoca** di un device, togli la sua riga.

> Il forward `-R` lega la porta su **127.0.0.1** del bastion: giusto così. **Non** mettere
> `GatewayPorts yes` (esporrebbe le porte sull'IP pubblico).

## 3. sshd del bastion — restringi l'utente tunnel (consigliato)
In un drop-in `/etc/ssh/sshd_config.d/edge-tunnel.conf`:
```
Match User edge-tunnel
    AllowTcpForwarding remote     # solo -R (reverse), non -L/proxy
    PermitTTY no
    X11Forwarding no
    AllowAgentForwarding no
    PermitOpen none               # il reverse non ne ha bisogno
```
Poi `sudo sshd -t && sudo systemctl reload ssh`. (`-N` non apre un canale sessione, quindi
il tunnel resta su senza bisogno di shell.)

## 4. Raggiungi un Pi
Dal tuo laptop, col **tuo** account admin sul bastion (non `edge-tunnel`):
```bash
ssh -J <tuo_admin>@<bastion> -p <reverse_port> <utente_pi>@127.0.0.1
```
oppure in due passi:
```bash
ssh <tuo_admin>@<bastion>
ssh -p <reverse_port> <utente_pi>@localhost
```
(Poi ti autentichi allo sshd del **Pi** con l'utente/chiave del Pi.)

## 5. On-demand vs persistente
- **Persistente** (`install.sh --ssh-persistent`): il Pi tiene il tunnel sempre su (autossh
  + systemd). Serve per il **bring-up** e quando il broker non c'è: il Pi è raggiungibile
  sempre, senza dipendere da nient'altro.
- **On-demand** (default): il tunnel è giù; sale solo sul comando `open_ssh` per una finestra
  (`ssh_default_ttl`), poi si richiude. Sicuro per la flotta in produzione (broker attivo).

## 6. Igiene / sicurezza
- Una **porta** e una **chiave** per device; niente chiavi condivise.
- Solo la porta sshd del bastion è pubblica; le reverse_port stanno sul loopback.
- Ruota/rimuovi la chiave quando dismetti un Pi.
