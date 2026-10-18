#!/usr/bin/env python3
"""reMarkable 2 Type Folio Extra AltGr Layouts (US hardware keyboard).

Stock xochitl compiles its keyboard layouts into the Qt platform plugin
/usr/lib/plugins/platforms/libepaper.so and offers no way to add one. This
tool binary-patches the compiled-in US keymap so that AltGr (right Alt)
produces national characters, in the spirit of the "Polish programmers"
layout. Several layouts are available (see `layouts` command); each mirrors
the letter assignments of the corresponding xkeyboard-config layout.

It is build-agnostic: the keymap array is located via the dynamic symbol
  EpaperEvdevKeyboardMap::Locale::UnitedStates::keymap
and the 16-byte entry format is verified against known entries before any
byte is modified. New entries overwrite mappings for keys that do not
physically exist on the Type Folio (numeric keypad, NumLock, ScrollLock,
function keys); colliding AltGr mappings are replaced in place.

Usage:
    typefolio-altgr.py interactive                   # guided step-by-step wizard
    typefolio-altgr.py layouts                       # list available layouts
    typefolio-altgr.py setup                         # install an SSH key (asks password once)
    typefolio-altgr.py check                         # report device/patch status
    typefolio-altgr.py patch [--layout pl] [--dry-run]
    typefolio-altgr.py restore                       # restore the original plugin
    Options: --host root@10.11.99.1  --ask-pass  --no-restart

New here? Run `typefolio-altgr.py interactive` — it walks you through
connecting, authenticating, and choosing a layout, whatever state the
tablet is in.

Authentication: with SSH keys installed (see `setup`) everything just works;
otherwise pass --ask-pass to type the tablet's root password (shown on the
device under Settings -> General -> Help -> Copyright and licenses). The
TYPEFOLIO_PASSWORD environment variable is honored with --ask-pass.

Requires: python3 and OpenSSH (ssh/scp) on this machine.
Tested on reMarkable 2, OS 3.26 and 3.28. After every OS update, re-run `patch`.
"""

import argparse
import atexit
import getpass
import hashlib
import os
import stat
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

PLUGIN = "/usr/lib/plugins/platforms/libepaper.so"
BACKUP_DIR = "/home/root"
SYMBOL = b"_ZN22EpaperEvdevKeyboardMap6Locale12UnitedStates6keymapE"

ENTRY = struct.Struct("<HHIBBHI")  # keycode, unicode, qtcode, mods, pad, flags, special
ENTRY_SIZE = ENTRY.size  # 16

FLAG_DEAD, FLAG_LETTER, FLAG_MODIFIER, FLAG_SYSTEM = 1, 2, 4, 8
MOD_SHIFT, MOD_ALTGR = 0x01, 0x02
QT_KEY_ALTGR = 0x01001103
KEY_RIGHTALT = 100

# Keys absent on the Type Folio whose keymap entries are safe to sacrifice:
# numeric keypad, NumLock, ScrollLock, KPENTER, KPSLASH, and F1-F12.
SACRIFICIAL = ({55, 69, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 83,
                96, 98} | set(range(59, 69)) | {87, 88})

# US key -> Linux keycode, and the Qt keycode of that key's base US character.
KEYS = {}
for i, ch in enumerate("1234567890-="):
    KEYS[ch] = (2 + i, ord("1234567890-="[i].upper() if ch.isalpha() else ch))
for i, ch in enumerate("qwertyuiop[]"):
    KEYS[ch] = (16 + i, ord(ch.upper()))
for i, ch in enumerate("asdfghjkl;'"):
    KEYS[ch] = (30 + i, ord(ch.upper()))
for i, ch in enumerate("zxcvbnm,./"):
    KEYS[ch] = (44 + i, ord(ch.upper()))
KEYS["\\"] = (43, ord("\\"))
KEYS["`"] = (41, ord("`"))

# Layout tables: key -> (AltGr char, AltGr+Shift char or None).
# Sources: xkeyboard-config symbols files (letter assignments only; the
# source layouts' punctuation extras and dead keys are intentionally omitted).
LAYOUTS = {
    "pl": ("Polish (programmers)", {
        "a": "ąĄ", "c": "ćĆ", "e": "ęĘ", "l": "łŁ", "n": "ńŃ",
        "o": "óÓ", "s": "śŚ", "x": "źŹ", "z": "żŻ",
    }),
    "ro": ("Romanian (programmers)", {
        "q": "âÂ", "w": "ßẞ", "t": "țȚ", "i": "îÎ",
        "a": "ăĂ", "s": "șȘ", "d": "đĐ", "l": "łŁ",
    }),
    "lv": ("Latvian", {
        "e": "ēĒ", "r": "ŗŖ", "u": "ūŪ", "i": "īĪ", "o": "ōŌ",
        "a": "āĀ", "s": "šŠ", "g": "ģĢ", "k": "ķĶ", "l": "ļĻ",
        "z": "žŽ", "c": "čČ", "n": "ņŅ",
    }),
    "lt": ("Lithuanian (US)", {
        "1": "ąĄ", "2": "čČ", "3": "ęĘ", "4": "ėĖ", "5": "įĮ",
        "6": "šŠ", "7": "ųŲ", "8": "ūŪ", "=": "žŽ",
    }),
    "hr": ("Croatian (US)", {
        "y": "zZ", "z": "yY", "[": "šŠ", "]": "đĐ", "k": "ł",
        "l": "Ł", ";": "čČ", "'": "ćĆ", "\\": "žŽ",
    }),
    "szl": ("Silesian", {
        "e": "ãÃ", "o": "ôÔ", "u": "ōŌ", "p": "õÕ", "a": "ŏŎ",
        "s": "śŚ", "z": "żŻ", "x": "źŹ", "c": "ćĆ", "n": "ńŃ",
    }),
    "csb": ("Kashubian", {
        "e": "éÉ", "r": "ëË", "u": "ùÙ", "i": "òÒ", "o": "óÓ",
        "p": "ôÔ", "a": "ąĄ", "s": "ãÃ", "l": "łŁ", "z": "żŻ",
        "n": "ńŃ",
    }),
    "en-intl": ("English (US intl., AltGr letters only)", {
        "q": "äÄ", "w": "åÅ", "e": "éÉ", "r": "ëË", "t": "þÞ",
        "y": "üÜ", "u": "úÚ", "i": "íÍ", "o": "óÓ", "p": "öÖ",
        "a": "áÁ", "s": "ß", "d": "ðÐ", "j": "ïÏ", "k": "œŒ",
        "l": "øØ", "z": "æÆ", "x": "œŒ", "n": "ñÑ", ",": "çÇ",
    }),
}


def layout_entries(name):
    """Yield (keycode, required_mods, unicode, qtcode) for a layout."""
    _, table = LAYOUTS[name]
    for key, chars in table.items():
        kc, qt = KEYS[key]
        yield kc, MOD_ALTGR, ord(chars[0]), qt
        if len(chars) > 1:
            yield kc, MOD_ALTGR | MOD_SHIFT, ord(chars[1]), qt


class Elf:
    """Minimal read-only ELF parser (32/64-bit little-endian)."""

    def __init__(self, data):
        if data[:4] != b"\x7fELF" or data[5] != 1:
            raise SystemExit("error: not a little-endian ELF file")
        self.data = data
        self.is64 = data[4] == 2
        if self.is64:
            e_shoff = struct.unpack_from("<Q", data, 0x28)[0]
            shentsize, shnum = struct.unpack_from("<HH", data, 0x3A)
            self.sections = [
                struct.unpack_from("<IIQQQQIIQQ", data, e_shoff + i * shentsize)
                for i in range(shnum)
            ]
        else:
            e_shoff = struct.unpack_from("<I", data, 0x20)[0]
            shentsize, shnum = struct.unpack_from("<HH", data, 0x2E)
            self.sections = [
                struct.unpack_from("<10I", data, e_shoff + i * shentsize)
                for i in range(shnum)
            ]
        # section tuple: name, type, flags, addr, offset, size, link, info, align, entsize
        self.dynsym = self._section_by_type(11)  # SHT_DYNSYM
        if not self.dynsym:
            raise SystemExit("error: no .dynsym section")
        self.dynstr = self.sections[self.dynsym[6]]

    def _section_by_type(self, t):
        return next((s for s in self.sections if s[1] == t), None)

    def find_symbol(self, name):
        """Return (vaddr, size) of a dynamic symbol."""
        off, size = self.dynsym[4], self.dynsym[5]
        str_off = self.dynstr[4]
        if self.is64:
            entsize, fmt = 24, "<IBBHQQ"  # name, info, other, shndx, value, size
        else:
            entsize, fmt = 16, "<IIIBBH"  # name, value, size, info, other, shndx
        for pos in range(off, off + size, entsize):
            if self.is64:
                nm, _, _, _, value, sz = struct.unpack_from(fmt, self.data, pos)
            else:
                nm, value, sz, _, _, _ = struct.unpack_from(fmt, self.data, pos)
            end = self.data.index(b"\0", str_off + nm)
            if self.data[str_off + nm : end] == name:
                return value, sz
        return None, None

    def vaddr_to_offset(self, vaddr):
        for s in self.sections:
            addr, off, size = s[3], s[4], s[5]
            if s[1] != 8 and addr <= vaddr < addr + size:  # skip SHT_NOBITS
                return vaddr - addr + off
        raise SystemExit(f"error: vaddr {vaddr:#x} not in any section")


def locate_keymap(data):
    elf = Elf(data)
    vaddr, size = elf.find_symbol(SYMBOL)
    if vaddr is None:
        raise SystemExit(
            "error: US keymap symbol not found — plugin layout changed, "
            "this tool needs updating for your OS version"
        )
    if size % ENTRY_SIZE:
        raise SystemExit(f"error: keymap size {size:#x} not a multiple of {ENTRY_SIZE}")
    off = elf.vaddr_to_offset(vaddr)
    # Format sanity check: plain 'q' entry must exist as-is in the table.
    q_entry = ENTRY.pack(16, ord("q"), ord("Q"), 0, 0, FLAG_LETTER, 0)
    if q_entry not in data[off : off + size]:
        raise SystemExit(
            "error: keymap entry format check failed — struct layout changed, "
            "refusing to patch"
        )
    return off, size


def iter_entries(data, off, size):
    for pos in range(off, off + size, ENTRY_SIZE):
        yield pos, ENTRY.unpack_from(data, pos)


def analyze(data):
    off, size = locate_keymap(data)
    altgr_ok = False
    slots = []
    existing = {}  # (keycode, mods) -> entry position
    for pos, (kc, uni, qt, mods, _, flags, special) in iter_entries(data, off, size):
        if kc == KEY_RIGHTALT and flags == FLAG_MODIFIER and special == MOD_ALTGR:
            altgr_ok = True
        if kc in SACRIFICIAL and flags == 0:
            slots.append(pos)
        elif mods & MOD_ALTGR:
            existing[(kc, mods)] = (pos, uni)
    matches = {}
    for name in LAYOUTS:
        want = list(layout_entries(name))
        got = sum(1 for kc, m, uni, _ in want
                  if existing.get((kc, m), (None, None))[1] == uni)
        matches[name] = (got, len(want))
    return {
        "offset": off, "size": size, "entries": size // ENTRY_SIZE,
        "altgr_wired": altgr_ok, "slots": slots, "existing": existing,
        "matches": matches,
    }


def detect_layout(info):
    """Return (name, got, want) of the best-matching layout, or None."""
    best = max(info["matches"].items(), key=lambda kv: kv[1][0] / kv[1][1])
    name, (got, want) = best
    return (name, got, want) if got else None


def apply_patch(data, layout):
    """Return (patched_bytes, message). Raises SystemExit on any safety failure."""
    data = bytearray(data)
    info = analyze(data)
    want = list(layout_entries(layout))
    got, total = info["matches"][layout]
    if got == total:
        return bytes(data), "already patched"
    detected = detect_layout(info)
    if detected and detected[0] != layout:
        raise SystemExit(
            f"error: device already carries '{detected[0]}' entries "
            f"({detected[1]}/{detected[2]}) — run `restore` first, then patch"
        )
    if not info["altgr_wired"]:
        # Right Alt not wired as AltGr in this build: rewrite its entry.
        fixed = False
        for pos, (kc, *_rest) in iter_entries(data, info["offset"], info["size"]):
            if kc == KEY_RIGHTALT:
                data[pos : pos + ENTRY_SIZE] = ENTRY.pack(
                    KEY_RIGHTALT, 0xFFFF, QT_KEY_ALTGR, 0, 0, FLAG_MODIFIER, MOD_ALTGR)
                fixed = True
                break
        if not fixed:
            raise SystemExit("error: no KEY_RIGHTALT entry found to wire as AltGr")
    # Colliding AltGr mappings are replaced in place; the rest go into slots.
    slots = list(info["slots"])
    needed = sum(1 for kc, m, _, _ in want if (kc, m) not in info["existing"])
    if len(slots) < needed:
        raise SystemExit(
            f"error: only {len(slots)} sacrificial slots for {needed} new entries"
        )
    for kc, mods, uni, qt in want:
        if (kc, mods) in info["existing"]:
            pos = info["existing"][(kc, mods)][0]
        else:
            pos = slots.pop(0)
        data[pos : pos + ENTRY_SIZE] = ENTRY.pack(kc, uni, qt, mods, 0, FLAG_LETTER, 0)
    got, total = analyze(data)["matches"][layout]
    if got != total:
        raise SystemExit("error: post-patch verification failed")
    return bytes(data), f"patched {total} entries ({LAYOUTS[layout][0]})"


def sha8(b):
    return hashlib.sha256(b).hexdigest()[:8]


SSH_ENV = None  # set up by use_password(); None means key-based auth
KEY_OPTS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=8"]
PW_OPTS = ["-o", "NumberOfPasswordPrompts=1",
           "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=8"]
SSH_OPTS = KEY_OPTS
PASSWORD_HINT = ("The root password is shown on the tablet under "
                 "Settings > General > Help >\nCopyright and licenses "
                 "(scroll to the bottom).")


def use_keys():
    """Switch back to key-based authentication."""
    global SSH_ENV, SSH_OPTS
    SSH_ENV, SSH_OPTS = None, KEY_OPTS


def use_password(pw=None):
    """Route the root password to ssh/scp via SSH_ASKPASS (no sshpass needed)."""
    global SSH_ENV, SSH_OPTS
    if pw is None:
        pw = os.environ.get("TYPEFOLIO_PASSWORD") or getpass.getpass(
            "reMarkable root password: ")
    fd, askpass = tempfile.mkstemp(prefix="typefolio-askpass-")
    os.write(fd, b'#!/bin/sh\nprintf "%s" "$TYPEFOLIO_PW"\n')
    os.close(fd)
    os.chmod(askpass, stat.S_IRWXU)
    atexit.register(os.unlink, askpass)
    SSH_ENV = {**os.environ, "TYPEFOLIO_PW": pw, "SSH_ASKPASS": askpass,
               "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": os.environ.get("DISPLAY", ":0")}
    SSH_OPTS = PW_OPTS


def probe(host):
    """Try to reach the tablet. Returns 'ok', or a failure kind + stderr."""
    r = subprocess.run(["ssh", *SSH_OPTS, host, "true"],
                       capture_output=True, text=True, env=SSH_ENV)
    if r.returncode == 0:
        return "ok", ""
    err = r.stderr
    low = err.lower()
    if "host key verification failed" in low or "remote host identification has changed" in low:
        kind = "hostkey"
    elif "permission denied" in low or "authentication failed" in low:
        kind = "auth"
    elif ("no route to host" in low or "connection refused" in low
          or "timed out" in low or "network is unreachable" in low
          or "could not resolve" in low or "name or service not known" in low):
        kind = "unreachable"
    else:
        kind = "other"
    return kind, err.strip()


# Newer OS builds dropped update.conf; fall back to /etc/os-release.
# `head` makes the pipeline succeed even when neither file exists.
VERSION_CMD = ("{ sed -n 's/^REMARKABLE_RELEASE_VERSION=//p' /usr/share/remarkable/update.conf; "
               "sed -n 's/^IMG_VERSION=//p' /etc/os-release | tr -d '\"'; } 2>/dev/null | head -n 1")


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, env=SSH_ENV, **kw)


def ssh(host, script, capture=False):
    return run(["ssh", *SSH_OPTS, host, script], capture_output=capture, text=capture)


def ssh_rw(host, script):
    """Run `script` with the root filesystem writable.

    Newer OS builds mount / read-only. If it is, remount it read-write for
    the duration of `script` and put it back to read-only afterwards (also
    on failure). The exit status of `script` is preserved.
    """
    return ssh(host,
               "if grep -q ' / [^ ]* ro[ ,]' /proc/mounts; then "
               "mount -o remount,rw / || exit 1; "
               "trap 'mount -o remount,ro / 2>/dev/null' EXIT; "
               "fi; " + script)


def fetch_plugin(host, tmpdir):
    local = Path(tmpdir) / "libepaper.so"
    run(["scp", "-q", *SSH_OPTS, f"{host}:{PLUGIN}", str(local)])
    return local.read_bytes()


def cmd_layouts(_args):
    for name, (label, table) in LAYOUTS.items():
        chars = " ".join(chars[0] for chars in table.values())
        print(f"{name:8s} {label:40s} {chars}")


def cmd_check(args):
    with tempfile.TemporaryDirectory() as tmp:
        data = fetch_plugin(args.host, tmp)
    info = analyze(data)
    ver = ssh(args.host,
              VERSION_CMD,
              capture=True).stdout.strip()
    detected = detect_layout(info)
    if detected is None:
        state = "not patched"
    else:
        name, got, total = detected
        full = "fully" if got == total else f"PARTIALLY ({got}/{total})"
        state = f"{full} patched: {name} — {LAYOUTS[name][0]}"
    print(f"OS version:      {ver or 'unknown'}")
    print(f"plugin sha256:   {sha8(data)}…")
    print(f"US keymap:       {info['entries']} entries at offset {info['offset']:#x}")
    print(f"AltGr modifier:  {'wired' if info['altgr_wired'] else 'NOT wired'}")
    print(f"Layout:          {state}")


def deploy(host, patched, orig, no_restart=False):
    """Back up the current plugin, install `patched`, restart xochitl."""
    backup = f"{BACKUP_DIR}/libepaper.so.orig-{sha8(orig)}"
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "libepaper-patched.so"
        local.write_bytes(patched)
        ssh(host, f"cp -n {PLUGIN} {backup}")
        run(["scp", "-q", *SSH_OPTS, str(local), f"{host}:/tmp/libepaper-patched.so"])
        ssh_rw(host, f"mv /tmp/libepaper-patched.so {PLUGIN}")
    print(f"Deployed. Original kept at {backup}")
    restart(host, no_restart)


def do_restore(host, no_restart=False, quiet=False):
    out = ssh(host, f"ls -t {BACKUP_DIR}/libepaper.so.orig-* 2>/dev/null | head -n 1",
              capture=True).stdout.strip()
    if not out:
        raise SystemExit(f"error: no backup found in {BACKUP_DIR} on the device")
    ssh_rw(host, f"cp {out} {PLUGIN}")
    print(f"Restored {out} -> {PLUGIN}")
    restart(host, no_restart, quiet)


def install_key(host):
    """Install an SSH public key on the tablet; returns the key path used."""
    ssh_dir = Path.home() / ".ssh"
    pub = next((p for p in (ssh_dir / f"id_{t}.pub" for t in ("ed25519", "ecdsa", "rsa"))
                if p.exists()), None)
    if pub is None:
        ssh_dir.mkdir(mode=0o700, exist_ok=True)
        key = ssh_dir / "id_ed25519"
        print(f"No SSH key found — generating {key}")
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)])
        pub = key.with_suffix(".pub")
    pubkey = pub.read_text().strip()
    print(f"Installing {pub} on {host} …")
    ssh(host,
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        f"grep -qF '{pubkey}' ~/.ssh/authorized_keys 2>/dev/null || "
        f"printf '%s\\n' '{pubkey}' >> ~/.ssh/authorized_keys && "
        "chmod 600 ~/.ssh/authorized_keys")
    return pub


def cmd_patch(args):
    with tempfile.TemporaryDirectory() as tmp:
        orig = fetch_plugin(args.host, tmp)
    patched, msg = apply_patch(orig, args.layout)
    if msg == "already patched":
        print(f"Device already has the '{args.layout}' layout — nothing to do.")
        return
    print(f"{msg} (sha {sha8(orig)}… -> {sha8(patched)}…)")
    if args.dry_run:
        print("Dry run — device not modified.")
        return
    deploy(args.host, patched, orig, args.no_restart)


def cmd_setup(args):
    """Install an SSH public key on the tablet so no password is needed later."""
    install_key(args.host)
    use_keys()  # verify that key-based auth now works
    ssh(args.host, "true")
    print("Key installed — future runs won't need the password.")


def cmd_restore(args):
    do_restore(args.host, args.no_restart)


def restart(host, no_restart=False, quiet=False):
    if no_restart:
        if not quiet:
            print("Skipped xochitl restart (--no-restart). Restart it to take effect.")
        return
    ssh(host, "systemctl restart xochitl && sleep 2 && systemctl is-active --quiet xochitl")
    print("xochitl restarted and running.")


def ask(prompt, default=None):
    """Prompt for a line of input; Enter accepts `default`."""
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        raise SystemExit("\nAborted.")
    return answer or (default or "")


def confirm(prompt, default=True):
    d = "Y/n" if default else "y/N"
    while True:
        answer = ask(f"{prompt} ({d})").lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def step(n, title):
    print(f"\n\033[1mStep {n}: {title}\033[0m")


def wizard_connect(host):
    """Reach the tablet, resolving connection and auth problems interactively."""
    while True:
        kind, err = probe(host)
        if kind == "ok":
            print(f"Connected to {host}.")
            return host
        if kind == "unreachable":
            print(f"\nCan't reach {host}.")
            print("  - Is the tablet connected by USB and powered on?")
            print("  - Over Wi-Fi, use the IP from Settings > General > Help > "
                  "Copyright and licenses.")
            choice = ask("Press Enter to retry, or type a different host", "retry")
            if choice != "retry":
                host = choice if "@" in choice else f"root@{choice}"
            continue
        if kind == "hostkey":
            print("\nThe tablet's SSH host key has changed.")
            print("After a reMarkable OS update this is normal — the system boots from")
            print("the other partition, which has its own key. It can also mean someone")
            print("is intercepting the connection, so only continue on a trusted network.")
            if not confirm("Forget the old key and reconnect?", default=True):
                raise SystemExit("Aborted.")
            hostname = host.split("@")[-1]
            subprocess.run(["ssh-keygen", "-R", hostname],
                           capture_output=True, text=True)
            continue
        if kind == "auth":
            print("\nSSH keys aren't set up for this tablet (or were removed).")
            print(PASSWORD_HINT)
            use_password(getpass.getpass("reMarkable root password: "))
            kind2, err2 = probe(host)
            if kind2 == "ok":
                print("Password accepted.")
                if confirm("Install an SSH key so you won't need the password again?",
                           default=True):
                    install_key(host)
                    use_keys()
                    if probe(host)[0] != "ok":  # fall back to the password
                        use_password(SSH_ENV and SSH_ENV.get("TYPEFOLIO_PW"))
                        print("Key install didn't take effect; continuing with the password.")
                    else:
                        print("Key installed — future runs won't need the password.")
                return host
            use_keys()
            print(f"\nThat didn't work ({kind2}).")
            if err2 and kind2 == "other":
                print(err2)
            if not confirm("Try again?", default=True):
                raise SystemExit("Aborted.")
            continue
        print(f"\nSSH failed:\n{err}")
        if not confirm("Try again?", default=True):
            raise SystemExit("Aborted.")


def choose_layout(current):
    names = sorted(LAYOUTS)
    print("\nAvailable layouts (AltGr + letter; add Shift for uppercase):\n")
    for i, name in enumerate(names, 1):
        label, table = LAYOUTS[name]
        chars = " ".join(c[0] for c in table.values())
        mark = " (currently installed)" if name == current else ""
        print(f"  {i}. {name:8s} {label}{mark}")
        print(f"     {chars}")
    default = str(names.index(current) + 1) if current else str(names.index("pl") + 1)
    while True:
        answer = ask("\nWhich layout?", default)
        if answer.isdigit() and 1 <= int(answer) <= len(names):
            return names[int(answer) - 1]
        if answer in names:
            return answer
        print("Please enter a number from the list, or a layout name.")


def cmd_interactive(args):
    print("\n\033[1mreMarkable 2 Type Folio — Extra AltGr Layouts installer\033[0m")
    print("This adds national characters on AltGr (right Alt) to the Type Folio.")
    print("Nothing is changed until you confirm, and it can always be undone.")

    step(1, "Connect to the tablet")
    print("Connect the tablet by USB. It appears as 10.11.99.1.")
    host = ask("Tablet address", args.host)
    if "@" not in host:
        host = f"root@{host}"
    host = wizard_connect(host)

    step(2, "Check the tablet")
    with tempfile.TemporaryDirectory() as tmp:
        data = fetch_plugin(host, tmp)
    ver = ssh(host,
              VERSION_CMD,
              capture=True).stdout.strip()
    info = analyze(data)
    detected = detect_layout(info)
    print(f"reMarkable OS {ver or 'unknown'}, keyboard layout table found "
          f"({info['entries']} keys).")
    current = None
    if detected is None:
        print("No layout patch installed yet.")
    else:
        name, got, total = detected
        current = name
        state = "installed" if got == total else f"partially installed ({got}/{total})"
        print(f"Layout patch {state}: {name} — {LAYOUTS[name][0]}")

    step(3, "Choose what to do")
    if current:
        print(f"1. Switch to a different layout (replaces {current})")
        print(f"2. Remove the patch and restore the original keyboard")
        print(f"3. Reinstall {current} (e.g. after a reMarkable OS update)")
        action = ask("Your choice", "1")
        if action == "2":
            if not confirm("Restore the original keyboard layout?", default=True):
                raise SystemExit("Aborted.")
            step(4, "Restoring")
            do_restore(host, args.no_restart)
            print("\nDone — the keyboard is back to stock.")
            return
        if action == "3":
            layout = current
        else:
            layout = choose_layout(current)
            if layout != current:
                print(f"\nSwitching layouts restores the original plugin first, "
                      f"then applies {layout}.")
    else:
        layout = choose_layout(None)

    label, table = LAYOUTS[layout]
    step(4, f"Install {label}")
    examples = list(table.items())[:3]
    for key, chars in examples:
        upper = f", AltGr+Shift+{key.upper()} = {chars[1]}" if len(chars) > 1 else ""
        print(f"  AltGr+{key.upper()} = {chars[0]}{upper}")
    if not confirm(f"Install {label} on {host}?", default=True):
        raise SystemExit("Aborted.")

    if current and current != layout:
        print("\nRestoring the original plugin first …")
        do_restore(host, no_restart=True, quiet=True)
        with tempfile.TemporaryDirectory() as tmp:
            data = fetch_plugin(host, tmp)
    patched, msg = apply_patch(data, layout)
    if msg == "already patched":
        print("Already installed — nothing to do.")
        return
    print(msg)
    deploy(host, patched, data, args.no_restart)

    print("\n\033[1mDone.\033[0m Attach the Type Folio and try it in a typed note:")
    for key, chars in examples:
        print(f"  AltGr+{key.upper()} -> {chars[0]}")
    print("\nKeep the Type Folio layout set to US/English in Settings > "
          "Language and keyboard.")
    print("A reMarkable OS update will undo this — just run the installer again.")
    print(f"To undo now:  {sys.argv[0]} restore --host {host}")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("command",
                   choices=["interactive", "layouts", "setup", "check", "patch", "restore"])
    p.add_argument("--layout", default="pl", choices=sorted(LAYOUTS),
                   help="layout to apply with `patch` (default: %(default)s)")
    p.add_argument("--host", default="root@10.11.99.1",
                   help="SSH destination of the tablet (default: %(default)s)")
    p.add_argument("--ask-pass", action="store_true",
                   help="authenticate with the tablet's root password instead of SSH keys")
    p.add_argument("--dry-run", action="store_true", help="patch: don't modify the device")
    p.add_argument("--no-restart", action="store_true", help="don't restart xochitl")
    args = p.parse_args()
    if args.ask_pass or args.command == "setup":
        use_password()
    try:
        {"interactive": cmd_interactive, "layouts": cmd_layouts, "setup": cmd_setup,
         "check": cmd_check, "patch": cmd_patch,
         "restore": cmd_restore}[args.command](args)
    except KeyboardInterrupt:
        raise SystemExit("\nAborted.")
    except subprocess.CalledProcessError as e:
        hint = ("" if args.ask_pass or args.command in ("setup", "interactive") else
                "\nhint: if SSH keys aren't set up on the tablet, retry with "
                "--ask-pass, run `setup` once, or use `interactive`")
        raise SystemExit(f"error: command failed: {' '.join(e.cmd)}{hint}")


if __name__ == "__main__":
    main()
