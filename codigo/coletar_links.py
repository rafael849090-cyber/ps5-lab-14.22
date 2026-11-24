#!/usr/bin/env python3
"""Script simples para registrar links oficiais e de pesquisa."""

links = {
    "sony_oficial": "https://www.playstation.com/en-us/support/hardware/ps5/system-software/",
    "firmware_list": "https://github.com/MewX/ps5-official-firmware-list",
    "psdevwiki": "https://www.psdevwiki.com/ps5/",
    "zigzag_research": "https://github.com/ZigZagBoi1/PS5-14.10-Research",
    "rehost_1422": "https://github.com/Redomic/PS5-1422-REHOST",
}

print("=== Links de referência — PS5 Lab 14.22 ===\n")
for nome, url in links.items():
    print(f"{nome}: {url}")
