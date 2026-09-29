import logging

from consult2elm.main import Config, _setup_logging


def test_config_file_and_log_file(tmp_path):
    log_path = tmp_path / "bridge.log"
    conf = tmp_path / "c.conf"
    conf.write_text(f"[obd]\nbt_class = 0x000000\n[general]\nlog_file = {log_path}\nstatus_led = no\n")
    cfg = Config.load(str(conf))
    assert cfg.bt_class == "0x000000" and cfg.status_led is False
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        _setup_logging(cfg)
        logging.getLogger("consult2elm").info("hello file")
        for h in root.handlers:
            h.flush()
        assert "hello file" in log_path.read_text()
    finally:
        for h in root.handlers[len(before):]:
            root.removeHandler(h)
            h.close()
