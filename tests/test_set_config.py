"""Comando remoto `set_config` (0.4.0): whitelist esplicita, validazione atomica,
persistenza su config, ack col nuovo valore.

Nato perche' con l'OTA bloccato dal sandbox systemd le due chiavi che sbloccano
dettaglio per-inverter e storico on-demand (`collect_inverter_detail`,
`persistent_commands`) erano le UNICHE non comandabili da remoto. NESSUNA chiave
di rete/WireGuard/broker/identita'/OTA e' modificabile da qui.
"""
import json
import logging

from experanto_edge.buffer import Buffer
from experanto_edge.config import Config
from experanto_edge.main import SET_CONFIG_KEYS, Agent


class FakeReader:
    reader_type = "fake"
    collect_detail = False   # come SolarlogGetjpReader: applicata a caldo

    def read(self):
        return {"read_at": 111, "getjp": {"801_170": {"ok": 1}}}

    def discover(self):
        return {"getjp": {}}


class FakeTransport:
    def __init__(self, cmd=None):
        self.published = []
        self.cmd = cmd

    def connect(self, persistent=False):
        pass

    def disconnect(self):
        pass

    def publish(self, topic, payload, qos=1, retain=False):
        self.published.append((topic, payload))
        return True

    def get_retained_command(self, topic, timeout=3.0):
        c, self.cmd = self.cmd, None
        return c


def make_agent(tmp_path, cmd=None):
    cfg = Config(device_code="EXP-T", secret="s", station_id="st-1", interval=300,
                 buffer_path=str(tmp_path / "buf.db"))
    cfg._path = str(tmp_path / "config.yaml")
    t = FakeTransport(cmd)
    return Agent(cfg, FakeReader(), t, Buffer(cfg.buffer_path)), cfg, t


# ---------------- happy path ----------------

def test_set_config_applies_persists_and_acks(tmp_path):
    agent, cfg, _ = make_agent(tmp_path)
    ok, detail = agent.set_config(
        {"set": {"collect_inverter_detail": True, "persistent_commands": True}})
    assert ok
    d = json.loads(detail)
    assert d["applied"] == {"collect_inverter_detail": True, "persistent_commands": True}
    assert d["restart_required"] == ["persistent_commands"]
    # persistita sulla config locale (riletta da zero)
    reloaded = Config.load(cfg._path)
    assert reloaded.collect_inverter_detail is True
    assert reloaded.persistent_commands is True
    # collect_inverter_detail applicata A CALDO al reader (costruito all'avvio)
    assert agent.reader.collect_detail is True


def test_set_config_end_to_end_via_command(tmp_path):
    # catena completa nel ciclo intermittente: retained cmd -> dispatcher -> ack
    agent, cfg, t = make_agent(tmp_path, cmd={
        "command_id": "sc1", "cmd": "set_config",
        "args": {"set": {"collect_inverter_detail": True}}})
    agent.run_cycle()
    acks = [p[1] for p in t.published if p[0] == cfg.topic("up/ack")]
    assert acks and acks[0]["ok"] is True
    assert json.loads(acks[0]["detail"])["applied"]["collect_inverter_detail"] is True
    assert cfg.last_command_id == "sc1"


def test_set_config_interval_and_log_level(tmp_path):
    agent, cfg, _ = make_agent(tmp_path)
    root_before = logging.getLogger().level
    try:
        ok, detail = agent.set_config({"set": {"interval": 600, "log_level": "warning"}})
        assert ok
        assert cfg.interval == 600
        assert cfg.log_level == "WARNING"           # normalizzato
        assert logging.getLogger().level == logging.WARNING   # applicato a caldo
        assert json.loads(detail)["restart_required"] == []
    finally:
        logging.getLogger().setLevel(root_before)


# ---------------- whitelist: rifiuti ----------------

def test_set_config_rejects_non_whitelisted_keys(tmp_path):
    # nessuna chiave di rete/WG/broker/identita'/OTA/percorsi da remoto
    agent, cfg, _ = make_agent(tmp_path)
    for key in ("wg_interface", "wg_address", "wg_managed_externally", "broker_host",
                "broker_port", "tls", "tls_insecure", "ca_cert", "secret", "device_code",
                "station_id", "update_base_url", "update_public_key", "ota_helper",
                "app_dir", "buffer_path", "health_path", "datalogger_ip",
                "datalogger_user_password"):
        ok, detail = agent.set_config({"set": {key: "x"}})
        assert not ok, key
        assert "non modificabile" in detail, key
    assert not (tmp_path / "config.yaml").exists()   # mai persistito nulla


def test_whitelist_has_no_network_keys():
    # tripwire sulla whitelist stessa: se qualcuno aggiunge una chiave di rete,
    # questo test lo blocca.
    for key in SET_CONFIG_KEYS:
        assert not key.startswith(("wg_", "broker_", "tls", "update_", "datalogger_"))
        assert key not in ("secret", "device_code", "ca_cert", "app_dir",
                           "ota_helper", "buffer_path", "health_path")


# ---------------- validazione atomica ----------------

def test_set_config_rejects_bad_types_and_ranges(tmp_path):
    agent, cfg, _ = make_agent(tmp_path)
    for bad in ({"collect_inverter_detail": "true"},      # stringa, non bool
                {"persistent_commands": 1},               # int, non bool
                {"interval": 10},                         # sotto il minimo
                {"interval": "600"},                      # stringa
                {"command_wait": 99},                     # sopra il massimo
                {"log_level": "VERBOSE"}):                # non nel set
        ok, _detail = agent.set_config({"set": bad})
        assert not ok, bad
    assert cfg.interval == 300 and cfg.persistent_commands is False


def test_set_config_atomic_on_partial_error(tmp_path):
    # una chiave invalida rifiuta l'INTERO comando: la valida non viene applicata
    agent, cfg, _ = make_agent(tmp_path)
    ok, _ = agent.set_config({"set": {"interval": 600, "persistent_commands": "yes"}})
    assert not ok
    assert cfg.interval == 300
    assert not (tmp_path / "config.yaml").exists()


def test_set_config_bad_payload_shape(tmp_path):
    agent, _, _ = make_agent(tmp_path)
    for args in ({}, {"set": {}}, {"set": "x"}, {"collect_inverter_detail": True}):
        ok, detail = agent.set_config(args)
        assert not ok
        assert "set" in detail


def test_set_config_rolls_back_on_save_failure(tmp_path):
    # cfg.save che alza (es. PermissionError sul Pi live con /etc root:root):
    # nessuno stato meta' applicato, ack ok=False onesto.
    agent, cfg, _ = make_agent(tmp_path)

    def boom():
        raise PermissionError("read-only")
    cfg.save = boom
    ok, detail = agent.set_config({"set": {"collect_inverter_detail": True}})
    assert not ok and "persistenza" in detail
    assert cfg.collect_inverter_detail is False        # rollback in RAM
    assert agent.reader.collect_detail is False        # niente applicazione a caldo
