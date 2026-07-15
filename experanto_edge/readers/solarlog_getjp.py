"""Reader for Solar-Log Base 2000 (and family) via the local `getjp` JSON API.

Thin relay: it forwards the raw getjp responses; the server parses the numeric indices
into fields. Requires the datalogger's local API access to be set to "Open".

getjp queries (Solar-Log Base handbook + reverse-engineering).

Livello "open JSON" (basta una password UTENTE impostata sul datalogger; nessun login
lato agente):
  {"801": {"170": null}}  -> aggregato impianto. Indici in 801/170: 101=Pac W,
        102=Pdc W, 103=Uac media V, 104=Udc media V, 105/106=resa oggi/ieri Wh,
        109=resa totale Wh, 110-115=consumo/rete, 116=potenza installata Wp.
  {"782": null}           -> potenza AC per-device, indice 0..N (piatto: {idx:"W"}).
  {"608": null}           -> status per-device ("Normal"/"OFFLINE"/"RUNNING").
                             "RUNNING" = meter (non un inverter).
  {"740": null}           -> SERIALI per-device: {idx: "<n> / <serial>"}; l'entry
                             con un IP (es. "192.168.1.60 / ...") e' il meter.
  {"877": null}           -> storico MENSILE: [[data, resa_Wh, ...], ...].
  {"878": null}           -> storico ANNUALE: idem.

Livello "user" (login lato agente con la password UTENTE del datalogger). Sblocca il
DETTAGLIO per-inverter, non esposto all'API open:
  {"143": {"1": {"100": {"<dev>": null}}}}  -> min-data intraday per-inverter: righe
        [["HH:MM:SS", [~65 canali]], ...] con temperatura, Udc/Idc/Pdc per stringa,
        Uac per fase, frequenza, Pac per fase. Richiede l'header
        `x-sl-csrf-protection: 1`, altrimenti "ACCESS DENIED" anche da loggati.
  {"870": null}           -> dizionario canali [type, channel, trans, label, unit] che
        da' il significato delle colonne del 143 (es. type 6 = temperatura). Statico.
Login: {"550": null} da i salt bcrypt; p = bcrypt(password_user, salt_550/104);
POST /login  body `u=user&p=<hash>`  -> "SUCCESS..." + cookie di sessione.
Se `user_password` non e' impostata, il reader resta al solo livello open (nessuna
regressione): il dettaglio 143/870 semplicemente non viene raccolto.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import requests

from .base import Reader, ReaderError

log = logging.getLogger("experanto-edge")

QUERY_AGGREGATE = {"801": {"170": None}}
QUERY_DEVICES = {"782": None}
QUERY_STATUS = {"608": None}
QUERY_SERIALS = {"740": None}
QUERY_HIST_MONTH = {"877": None}
QUERY_HIST_YEAR = {"878": None}
QUERY_SALTS = {"550": None}
QUERY_CHANNELS = {"870": None}


def _detail_query(dev: str) -> Dict[str, Any]:
    """getjp per il dettaglio intraday del singolo inverter (indice `dev`)."""
    return {"143": {"1": {"100": {str(dev): None}}}}


class SolarlogGetjpReader(Reader):
    reader_type = "solarlog_getjp"

    def __init__(self, ip: str, port: int = 80, timeout: float = 10.0,
                 spacing: float = 1.5, history_interval: float = 3600.0,
                 user_password: str = ""):
        # NON sollevare qui: un datalogger assente/non ancora configurato non deve far
        # crashare l'agente al boot. L'errore emerge in read() -> lo cattura run_cycle,
        # che riporta lo stato "errore" e ritenta al ciclo dopo (niente crash-loop).
        self.ip = ip
        self.base_url = f"http://{ip}:{port}"
        self.timeout = timeout
        # Il Solar-Log risponde 503 se interrogato troppo in fretta: spaziamo le query.
        self.spacing = spacing
        # Lo storico (877/878) cambia lentamente: lo rileggiamo solo ogni tot secondi,
        # non a ogni ciclo (riduce il carico/503 sul datalogger).
        self.history_interval = history_interval
        self._last_history = 0.0
        # Password UTENTE: se presente abilita il login e il dettaglio 143/870.
        self.user_password = user_password or ""
        # Sessione HTTP loggata (cookie jar). None = non loggati / login non tentato.
        self._session: Optional[requests.Session] = None
        # Dizionario canali 870: statico, lo prendiamo una volta per sessione.
        self._channels: Optional[Any] = None
        # Backoff sui login falliti: non martellare /login a ogni ciclo.
        self._login_retry_after = 0.0
        # Backoff sul dettaglio non disponibile (ne' open ne' via login): non ritentare
        # 143 su tutti gli inverter a ogni ciclo se il datalogger non lo espone.
        self._detail_retry_after = 0.0

    # ---- POST getjp (open oppure privilegiato con sessione + header CSRF) ----

    def _post_getjp(self, query: Dict[str, Any], *, session: Optional[requests.Session] = None,
                    csrf: bool = False) -> Any:
        if not self.ip:
            raise ReaderError("datalogger_ip non configurato (datalogger assente o non ancora impostato)")
        poster = session.post if session is not None else requests.post
        headers = {"x-sl-csrf-protection": "1"} if csrf else None
        try:
            r = poster(f"{self.base_url}/getjp", json=query, timeout=self.timeout, headers=headers)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            raise ReaderError(f"getjp {query} fallita su {self.base_url}: {e}") from e

    def _getjp(self, query: Dict[str, Any]) -> Any:
        return self._post_getjp(query)

    def _getjp_optional(self, query: Dict[str, Any]) -> Any:
        """Come _getjp ma best-effort: None se fallisce (503/rete).

        Usato per i blocchi non-primari (608/740/877/878): un loro errore NON deve far
        perdere la lettura di potenza (il dato primario) ne' innescare un errore-ciclo.
        """
        try:
            return self._getjp(query)
        except ReaderError:
            return None

    # ---- Login utente per il dettaglio privilegiato (143/870) ----

    def _ensure_session(self) -> Optional[requests.Session]:
        """Restituisce una sessione loggata come "user", o None se non disponibile.

        - Nessuna password configurata -> None (resta al livello open, nessuna regressione).
        - Sessione gia' viva -> la riusa.
        - Login: 550 (salt) -> bcrypt(password, salt) -> POST /login. In backoff dopo un
          fallimento (non martella /login ogni ciclo). bcrypt assente -> None + warning.
        """
        if not self.user_password:
            return None
        if self._session is not None:
            return self._session
        if time.monotonic() < self._login_retry_after:
            return None
        try:
            import bcrypt  # dipendenza opzionale: se manca, restiamo al livello open
        except ImportError:
            log.warning("bcrypt non installato: dettaglio per-inverter (143) non disponibile")
            self._login_retry_after = time.monotonic() + self.history_interval
            return None
        try:
            salts = self._getjp(QUERY_SALTS).get("550", {})
            salt = salts.get("104")  # 104 = user_salt
            if not salt:
                raise ReaderError("salt utente (550/104) mancante")
            hashed = bcrypt.hashpw(self.user_password.encode(), salt.encode()).decode()
            sess = requests.Session()
            r = sess.post(f"{self.base_url}/login",
                          data={"u": "user", "p": hashed}, timeout=self.timeout)
            r.raise_for_status()
            if "SUCCESS" not in r.text.upper():
                raise ReaderError(f"login rifiutato: {r.text[:40]!r}")
            self._session = sess
            self._channels = None  # ricarica il dizionario canali sulla nuova sessione
            log.info("login datalogger (user) riuscito su %s", self.base_url)
            return sess
        except (ReaderError, requests.RequestException, ValueError) as e:
            log.warning("login datalogger fallito su %s: %s", self.base_url, e)
            self._login_retry_after = time.monotonic() + self.history_interval
            return None

    def _getjp_priv_optional(self, query: Dict[str, Any]) -> Any:
        """getjp col solo header CSRF, best-effort.

        Usa la sessione loggata se c'e' (datalogger protetto); altrimenti tenta comunque
        senza cookie (un datalogger senza password espone il dettaglio col solo header
        CSRF). Su errore, se eravamo loggati invalida la sessione: cookie scaduto o
        "ACCESS DENIED" -> il ciclo dopo _ensure_session rifa' il login.
        """
        try:
            return self._post_getjp(query, session=self._session, csrf=True)
        except ReaderError:
            if self._session is not None:
                self._session = None  # forza il re-login al prossimo ciclo
            return None

    def _read_detail(self, device_indices: List[str]) -> Dict[str, Any]:
        """Dettaglio per-inverter (143) per ciascun device, + dizionario canali (870).

        Forwarda solo l'ULTIMA riga intraday di ogni inverter (i valori correnti:
        temperatura, Udc/Idc/Pdc, Uac, frequenza) per non spedire tutta la serie del
        giorno a ogni ciclo. Il server mappa le colonne usando 870. Best-effort per
        device: un inverter che non risponde non blocca gli altri.
        """
        detail: Dict[str, Any] = {}
        for idx in device_indices:
            time.sleep(self.spacing)
            resp = self._getjp_priv_optional(_detail_query(idx))
            node = None
            if isinstance(resp, dict):
                node = resp.get("143", {}).get("1", {}).get("100", {}).get(str(idx))
            # node atteso: [[from, to, interval], [[time, [vals]], ...]]
            rows = node[1] if (isinstance(node, list) and len(node) >= 2
                               and isinstance(node[1], list)) else []
            # Ultima riga con almeno un valore reale: di notte gli slot recenti sono tutti
            # None (inverter spento). Forwardiamo l'ultimo campione con dati + il suo
            # timestamp; e' il server a giudicarne la freschezza (header[1] = "to").
            last = None
            for row in reversed(rows):
                vals = row[1] if isinstance(row, list) and len(row) >= 2 else None
                if isinstance(vals, list) and any(v is not None for v in vals):
                    last = row
                    break
            if last is not None:
                detail[str(idx)] = [node[0], [last]]  # header + ultima riga con dati reali
        out: Dict[str, Any] = {}
        if detail:
            out["143"] = detail
            if self._channels is None:
                time.sleep(self.spacing)
                ch = self._getjp_priv_optional(QUERY_CHANNELS)
                if isinstance(ch, dict):
                    self._channels = ch.get("870")  # forwarda la sola lista canali
            if self._channels is not None:
                out["870"] = self._channels
        return out

    # ---- Ciclo di lettura ----

    def read(self) -> Dict[str, Any]:
        # Aggregate + potenza + status + seriali: dati live (open JSON, no login).
        # Le query sono spaziate (il datalogger 503-a se troppo veloce); tutto tranne
        # 801/170 e 782 e' best-effort (il server ha fallback se un blocco manca).
        agg = self._getjp(QUERY_AGGREGATE)
        time.sleep(self.spacing)
        devices = self._getjp(QUERY_DEVICES)
        time.sleep(self.spacing)
        status = self._getjp_optional(QUERY_STATUS)
        time.sleep(self.spacing)
        serials = self._getjp_optional(QUERY_SERIALS)
        getjp: Dict[str, Any] = {"801_170": agg, "782": devices, "608": status, "740": serials}

        # Storico mensile/annuale: solo ogni history_interval secondi (cambia piano).
        now = time.time()
        if now - self._last_history >= self.history_interval:
            time.sleep(self.spacing)
            month = self._getjp_optional(QUERY_HIST_MONTH)
            time.sleep(self.spacing)
            year = self._getjp_optional(QUERY_HIST_YEAR)
            if month is not None or year is not None:
                getjp["877"] = month
                getjp["878"] = year
                self._last_history = now

        # Dettaglio per-inverter (temperatura/Udc/Idc/Pdc/Uac/frequenza). Lo si tenta col
        # solo header CSRF: un datalogger SENZA password puo' gia' esporlo. Se e' negato e
        # c'e' una password utente, _ensure_session fa login e i device riprovano con la
        # sessione. Se non arriva nulla (ne' open ne' via login), backoff per non
        # martellare 143 su ogni inverter a ogni ciclo. Nessuna regressione sul dato open.
        if isinstance(devices, dict) and time.monotonic() >= self._detail_retry_after:
            dev_map = devices.get("782")  # {idx: {...}}: gli indici sono le chiavi interne
            if isinstance(dev_map, dict) and dev_map:
                self._ensure_session()    # login solo se c'e' password (no-op altrimenti)
                detail = self._read_detail(list(dev_map.keys()))
                if detail:
                    getjp.update(detail)
                else:
                    self._detail_retry_after = time.monotonic() + self.history_interval

        return {"read_at": int(now), "getjp": getjp}

    def discover(self) -> Dict[str, Any]:
        devices = self._getjp(QUERY_DEVICES)
        time.sleep(self.spacing)
        status = self._getjp_optional(QUERY_STATUS)
        return {"read_at": int(time.time()), "getjp": {"782": devices, "608": status}}
