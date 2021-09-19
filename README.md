# KVMD
[![CI](https://github.com/pikvm/kvmd/workflows/CI/badge.svg)](https://github.com/pikvm/kvmd/actions?query=workflow%3ACI)
[![Discord](https://img.shields.io/discord/580094191938437144?logo=discord)](https://discord.gg/bpmXfz5)

This repository contains the configuration and code of KVMD, the main PIKVM daemon.
If your request does not relate directly to this codebase, please send it to issues of the [PiKVM](https://github.com/pikvm/pikvm/issues) repository.

# Update from git
```
rw
pacman -Syu
git clone --depth=1 https://github.com/pikvm/kvmd
cp -r kvmd/kvmd /usr/lib/python3.9/site-packages/
cp -r kvmd/web  /usr/share/kvmd/
reboot
```

# 2x Ezcoo KVM 4x Switch Patch (7 usable Ports)
https://github.com/semool/kvmd/commit/1f9448cbc07cefbe0d32b7178596178e8cac0b03

# 3x Ezcoo KVM 4x Switch Patch (10 usable Ports)
https://github.com/semool/kvmd/commit/d950c1c771ad30d5f2adea62a1d7eb6082f1da69

# Hue Plugin to Control SmartPlugs - Get Api Token
- http://[BridgeIP]/debug/clip.html
- Send Message: {"devicetype":"pikvm"}
- get back username: {	"success": {	"username": "apiusername"	}
- get list of all Devices: http://[BridgeIP]/api/[username]/lights
- Save DeviceID for every SmartPlug to control
