from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from .common import MusicError, atomic_write, digest, identifier, json_bytes, now, parse_json
from .pano import MAX_LOG_BYTES, import_pano


def adb_path(explicit=None):
    candidates = [explicit, os.environ.get("MUSIC_ADB_EXE"), shutil.which("adb")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise MusicError("没有找到 ADB，请通过 --adb、环境变量 MUSIC_ADB_EXE 或 PATH 指定已有工具。")


def run_adb(adb, args, timeout=30):
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    result = subprocess.run([adb, *args], capture_output=True, timeout=timeout, creationflags=flags)
    if result.returncode:
        raise MusicError("ADB 读取失败，请检查连接、USB 调试授权和指定文件。")
    return result.stdout


def list_devices(adb=None):
    executable = adb_path(adb)
    output = run_adb(executable, ["devices", "-l"]).decode("utf-8", errors="replace")
    return dict(adb=executable, listing=output)


def receive_configured(workspace, config_path):
    """Read one explicitly configured, previously bound device; no implicit fallback."""
    if not config_path.exists():
        raise MusicError("尚未配置手机接收；需要先完成一次 usb-pull 绑定和 .local/phone.json 配置。")
    config = parse_json(config_path.read_bytes())
    if config.get("version") != 1:
        raise MusicError("手机接收配置版本不支持。")
    device = identifier(config.get("device_key"), "device")
    binding_path = workspace.home / ".local/devices" / (device + ".json")
    if not binding_path.exists():
        raise MusicError("设备尚未绑定，请先用明确的 serial 完成一次 usb-pull。")
    binding = parse_json(binding_path.read_bytes())
    if binding.get("device_key") != device:
        raise MusicError("设备绑定标签与配置不一致。")
    executable = adb_path(config.get("adb"))
    listing = run_adb(executable, ["devices", "-l"]).decode("utf-8")
    serials = []
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "device" and digest(fields[0].encode()) == binding["serial_sha256"]:
            serials.append(fields[0])
    if len(serials) != 1:
        raise MusicError("没有找到唯一且已授权连接的绑定设备；请连接原手机并确认 USB 调试。")
    return pull_pano(workspace, serial=serials[0], device=device,
        stream=config["stream_name"], remote=config["remote"], adb=executable,
        person=config.get("person_key", "me"), source_timezone=config.get("source_timezone"),
        collector_version=config.get("collector_version", "unknown"), rule_note=config.get("rule_note", "unknown"))


def pull_pano(workspace, *, serial, device, stream, remote, adb=None, person="me", source_timezone=None,
              collector_version="unknown", rule_note="unknown"):
    identifier(device, "device")
    identifier(stream, "stream")
    if not serial or any(c.isspace() for c in serial):
        raise MusicError("需要明确选择设备 serial。")
    if not re.fullmatch(r"/sdcard/[A-Za-z0-9_./-]+\.jsonl", remote) or ".." in remote.split("/"):
        raise MusicError("remote 需为 /sdcard 下无空格的 JSONL 路径，例如 /sdcard/Download/music/pano.jsonl。")
    executable = adb_path(adb)
    model = run_adb(executable, ["-s", serial, "shell", "getprop", "ro.product.model"]).decode().strip()
    if not model:
        raise MusicError("无法确认设备型号。")
    workspace.initialize()
    binding_path = workspace.home / ".local/devices" / (device + ".json")
    binding = dict(device_key=device, serial_sha256=digest(serial.encode()), model=model)
    with workspace.lock("usb-" + device):
        if binding_path.exists() and parse_json(binding_path.read_bytes()) != binding:
            raise MusicError("设备标签已绑定另一台设备，停止接收以免混合来源。")
        directory = workspace.home / ".local/usb-receipts"
        directory.mkdir(parents=True, exist_ok=True)
        received = directory / (uuid.uuid4().hex + ".jsonl")
        # Read-only adb pull; never delete or truncate the device file.
        run_adb(executable, ["-s", serial, "pull", remote, str(received)], timeout=60)
        if received.stat().st_size > MAX_LOG_BYTES:
            raise MusicError("设备日志超过首版大小限制，已接收副本保留。")
        result = import_pano(workspace, received, person=person, device=device, stream=stream,
                             source_timezone=source_timezone, collector_version=collector_version,
                             rule_note=rule_note)
        atomic_write(binding_path, json_bytes(binding))
        atomic_write(received.with_suffix(".receipt.json"), json_bytes(dict(**binding, remote=remote,
            received_at_utc=now(), payload_sha256=digest(received.read_bytes()), import_result=result)))
        return dict(**result, device_key=device, model=model)
