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

DETTAGLIO per-inverter (temperatura / MPPT / tensione / frequenza). Sbloccato
2026-07-16 (il "860 = 503" era un formato di richiesta sbagliato + rate-limit):
  {"860": {"<idx>": null}} -> config canali dell'epoch <idx> (INDICIZZATA: la forma
        bare {"860": null} manda il DL in 500). Ogni epoch = uno snapshot config;
        l'epoch corrente e' l'indice piu' alto. Per device: channels.min = lista
        ordinata [type, channel], l'indice E' la colonna del 143. Statico -> lo
        rileggiamo di rado (cadenza storico) ma lo INCLUDIAMO in ogni snapshot,
        perche' il server e' stateless e ne ha bisogno per mappare a ogni ciclo.
  {"143": {"1": {"101": {"<dev>": null}}}} -> VALORI CORRENTI del singolo inverter:
        [[from, to, interval], [~65 valori]] (~208 B/inverter, NON la curva del
        giorno). type->campo (validato Growatt MAX-125KTL3-XLV): 6=temp 4=freq
        10=status 11=error 1=Pac 3=Uac 44=Iac 12=Udc 45/56=Idc 5=Pdc.
Il mapping colonna->campo e' interamente SERVER-SIDE (`impianti/solarlog_local.py`):
il reader forwarda solo i blocchi raw 860 (ultima epoch) + 143 (valori correnti).

Se il datalogger e' protetto da password il dettaglio richiede login utente
({"550": null} da i salt bcrypt; POST /login) + header `x-sl-csrf-protection: 1`.
Senza password il dettaglio e' esposto col solo header CSRF (caso "install pulito").
Il dettaglio e' dietro il flag `collect_inverter_detail` (default False lato agente).
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

# Numero massimo di epoch 860 da sondare cercando quella corrente (backstop: le
# epoch sono contigue 0..N e in pratica poche, ma non ne conosciamo il totale).
_MAX_EPOCHS = 40


def _epoch_query(idx: int) -> Dict[str, Any]:
    """getjp per la config-canali dell'epoch <idx> (860 INDICIZZATO, non bare)."""
    return {"860": {str(idx): None}}


def _detail_query(dev: str) -> Dict[str, Any]:
    """getjp per i VALORI CORRENTI (101) del singolo inverter (indice `dev`)."""
    return {"143": {"1": {"101": {str(dev): None}}}}


def _real_inverter_indices(serials: Any, dev_map: Any) -> List[str]:
    """Indici degli inverter REALI su cui interrogare il 143.

    Preferisce il blocco seriali 740 (``{idx: "<n> / <serial>"}``): reale se la
    parte a sinistra e' un numero (NON un IP = contatore) e la destra non e' "Err"
    (slot vuoto). Senza 740, ripiega sugli slot 782 con potenza non-zero. Evita di
    interrogare il 143 su TUTTI i 30+ slot del datalogger (scaricherebbe l'intera
    giornata per ognuno)."""
    block = serials.get("740") if isinstance(serials, dict) else None
    out: List[str] = []
    if isinstance(block, dict):
        for idx, val in block.items():
            if not isinstance(val, str) or "/" not in val:
                continue
            lhs, _, rhs = val.partition("/")
            lhs, rhs = lhs.strip(), rhs.strip()
            if rhs and rhs.lower() != "err" and "." not in lhs:
                out.append(str(idx))
        if out:
            return out
    if isinstance(dev_map, dict):
        for idx, val in dev_map.items():
            try:
                if float(val if not isinstance(val, dict) else val.get("101", 0)) > 0:
                    out.append(str(idx))
            except (TypeError, ValueError):
                continue
    return out


class SolarlogGetjpReader(Reader):
    reader_type = "solarlog_getjp"

    def __init__(self, ip: str, port: int = 80, timeout: float = 10.0,
                 spacing: float = 1.5, history_interval: float = 3600.0,
                 user_password: str = "", collect_detail: bool = True):
        # NON sollevare qui: un datalogger assente/non ancora configurato non deve far
        # crashare l'agente al boot. L'errore emerge in read() -> lo cattura run_cycle,
        # che riporta lo stato "errore" e ritenta al ciclo dopo (niente crash-loop).
        self.ip = ip
        self.base_url = f"http://{ip}:{port}"
        self.timeout = timeout
        # Il Solar-Log risponde 503 se interrogato troppo in fretta: spaziamo le query.
        self.spacing = spacing
        # Lo storico (877/878) e la config-canali 860 cambiano lentamente: li rileggiamo
        # solo ogni tot secondi, non a ogni ciclo (riduce il carico/503 sul datalogger).
        self.history_interval = history_interval
        self._last_history = 0.0
        # Password UTENTE: se presente abilita il login e il dettaglio privilegiato.
        self.user_password = user_password or ""
        # Dettaglio per-inverter (860+143). OFF di default lato AGENTE (config
        # `collect_inverter_detail`): interroga il 143 per ogni inverter reale ad
        # ogni ciclo -> carico extra sul datalogger LIVE. La libreria lo abilita di
        # default per l'uso stand-alone.
        self.collect_detail = bool(collect_detail)
        # Sessione HTTP loggata (cookie jar). None = non loggati / login non tentato.
        self._session: Optional[requests.Session] = None
        # 860 (channels.min per device, epoch corrente): cache locale statica. La
        # rifetchiamo sulla cadenza storico ma la INCLUDIAMO in OGNI snapshot: il
        # server e' stateless e senza 860 non puo' mappare le colonne del 143.
        self._channels_860: Optional[Dict[str, Any]] = None
        self._last_860 = 0.0
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

    # ---- Login utente per il dettaglio privilegiato (860/143) ----

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
            self._channels_860 = None  # ricarica la config-canali sulla nuova sessione
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

    # ---- Config-canali 860 (epoch corrente) ----

    def _fetch_channels_860(self) -> Optional[Dict[str, Any]]:
        """860 dell'epoch CORRENTE = ``{"<idx>": epoch}`` con l'indice piu' alto.

        Sonda gli indici epoch 0,1,2,... (860 INDICIZZATO: la forma bare 500-a) e
        tiene l'ultima valida = layout config attuale. best-effort (header CSRF su
        DL aperto, sessione se protetto). None se nessuna epoch risponde.
        """
        latest: Optional[Dict[str, Any]] = None
        for i in range(_MAX_EPOCHS):
            time.sleep(self.spacing)
            resp = self._getjp_priv_optional(_epoch_query(i))
            epoch = resp.get("860", {}).get(str(i)) if isinstance(resp, dict) else None
            if not (isinstance(epoch, list) and len(epoch) >= 2):
                break                          # epoch inesistente -> stop
            latest = {str(i): epoch}
        return latest

    def _read_detail(self, device_indices: List[str]) -> Dict[str, Any]:
        """Valori correnti (143:101) per ciascun inverter -> ``{"143": {idx: node}}``.

        node = ``[[from, to, interval], [~65 valori]]`` (~208 B/inverter). best-effort
        per device: un inverter che non risponde non blocca gli altri. Il server mappa
        le colonne usando il 860 (channels.min) forwardato a parte.
        """
        detail: Dict[str, Any] = {}
        for idx in device_indices:
            time.sleep(self.spacing)
            resp = self._getjp_priv_optional(_detail_query(idx))
            node = None
            if isinstance(resp, dict):
                node = resp.get("143", {}).get("1", {}).get("101", {}).get(str(idx))
            # node atteso: [[from, to, interval], [valori...]] — valori correnti.
            if isinstance(node, list) and len(node) >= 2 and isinstance(node[1], list):
                detail[str(idx)] = node
        return {"143": detail} if detail else {}

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

        # Dettaglio per-inverter (temperatura/Udc/Idc/Pdc/Uac/frequenza). OFF di
        # default (self.collect_detail). Quando abilitato: si tenta col solo header
        # CSRF (datalogger senza password lo espone gia'); se negato e c'e' una
        # password utente, _ensure_session fa login. Il 143 e' interrogato SOLO sugli
        # inverter reali (740). Il 860 (channels.min, statico) e' rifetchato sulla
        # cadenza storico ma incluso in OGNI snapshot (il server ne ha bisogno per
        # mappare). Tutto best-effort: nessuna regressione sul dato open.
        if (self.collect_detail and isinstance(devices, dict)
                and time.monotonic() >= self._detail_retry_after):
            indices = _real_inverter_indices(serials, devices.get("782"))
            if indices:
                self._ensure_session()    # login solo se c'e' password (no-op altrimenti)
                if self._channels_860 is None or (now - self._last_860 >= self.history_interval):
                    ch860 = self._fetch_channels_860()
                    if ch860:
                        self._channels_860 = ch860
                        self._last_860 = now
                detail = self._read_detail(indices)
                if detail and self._channels_860:
                    # Entrambi necessari: senza 860 le colonne del 143 sono numeri.
                    getjp["860"] = self._channels_860
                    getjp.update(detail)
                else:
                    self._detail_retry_after = time.monotonic() + self.history_interval

        return {"read_at": int(now), "getjp": getjp}

    def discover(self) -> Dict[str, Any]:
        devices = self._getjp(QUERY_DEVICES)
        time.sleep(self.spacing)
        status = self._getjp_optional(QUERY_STATUS)
        return {"read_at": int(time.time()), "getjp": {"782": devices, "608": status}}
