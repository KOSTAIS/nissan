import os

from consult2elm.data import EngineData
from consult2elm.power import PowerManager


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def fake_sysfs(tmp_path):
    for n in range(4):
        d = tmp_path / f"cpu{n}"
        (d / "cpufreq").mkdir(parents=True)
        (d / "online").write_text("1")
    (tmp_path / "cpu0" / "cpufreq" / "scaling_governor").write_text("ondemand")
    return str(tmp_path)


RUNNING = EngineData(rpm=800, coolant_c=80, speed_kmh=0, battery_v=14.0)
IGN_ON_ENGINE_OFF = EngineData(rpm=0, coolant_c=80, speed_kmh=0, battery_v=12.4)


def test_sleep_and_wake_on_rpm(tmp_path):
    sysfs = fake_sysfs(tmp_path)
    clock = Clock()
    pm = PowerManager(idle_after=60, cpu_sysfs=sysfs, clock=clock)
    pm.report(RUNNING)
    clock.t = 59
    pm.report(IGN_ON_ENGINE_OFF)
    assert not pm.sleeping
    clock.t = 61
    pm.report(IGN_ON_ENGINE_OFF)
    assert pm.sleeping
    assert (tmp_path / "cpu3" / "online").read_text() == "0"
    assert (tmp_path / "cpu0" / "cpufreq" / "scaling_governor").read_text() == "powersave"
    pm.report(None)
    assert pm.sleeping
    pm.report(RUNNING)
    assert not pm.sleeping
    assert (tmp_path / "cpu3" / "online").read_text() == "1"
    assert (tmp_path / "cpu0" / "cpufreq" / "scaling_governor").read_text() == "ondemand"


def test_wake_on_ecu_counts_ignition_as_activity(tmp_path):
    clock = Clock()
    pm = PowerManager(wake_on="ecu", idle_after=10, cpu_sysfs=fake_sysfs(tmp_path), clock=clock)
    clock.t = 20
    pm.report(IGN_ON_ENGINE_OFF)
    assert not pm.sleeping


def test_shutdown_after_long_idle(tmp_path):
    calls = []
    clock = Clock()
    pm = PowerManager(
        idle_after=10, shutdown_after=100, cpu_sysfs=fake_sysfs(tmp_path), clock=clock,
        runner=lambda cmd, check: calls.append(cmd),
    )
    clock.t = 99
    pm.report(None)
    assert calls == []
    clock.t = 100
    pm.report(None)
    pm.report(None)
    assert calls == [["systemctl", "poweroff"]]


def test_cpu_hotplug_not_allowed(tmp_path, caplog):
    sysfs = fake_sysfs(tmp_path)
    for n in (1, 2, 3):
        (tmp_path / f"cpu{n}" / "online").chmod(0o444)
    clock = Clock()
    pm = PowerManager(idle_after=1, cpu_sysfs=sysfs, clock=clock)
    clock.t = 2
    if os.geteuid() == 0:
        real_write = PowerManager._write
        pm._write = lambda path, value: False if path.endswith("online") else real_write(path, value)
    pm.report(None)
    assert pm.sleeping and not pm._hotplug_ok
    assert (tmp_path / "cpu0" / "cpufreq" / "scaling_governor").read_text() == "powersave"
