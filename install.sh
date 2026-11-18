#!/bin/bash
# Install MacBook Control: the smcfan (fan) and smckbd (keyboard light) services, the
# macbook-settings helper (charge limit, keyboard options), the automatic keyboard light,
# the SMC driver fix for pre-T2 Macs (re-applied by a pacman hook after kernel updates) and the GUI.
#     sudo bash install.sh
# Remove everything again (your settings in /etc/macbook-control are kept):
#     sudo bash install.sh uninstall
set -euo pipefail
cd "$(dirname "$0")"
[ "$(id -u)" -eq 0 ] || { echo "Run it with sudo: sudo bash install.sh"; exit 1; }

MBPFAN_DROPIN=/etc/systemd/system/mbpfan.service.d/only-with-applesmc.conf
POLICIES=/usr/share/polkit-1/actions
GUI_DIR=/usr/local/share/macbook-control
DESKTOP=/usr/local/share/applications/macbook-control.desktop

USER_UNIT=/etc/systemd/user/macbook-kbdlight.service
HOOKS=/etc/pacman.d/hooks
as_user() { [ -n "${SUDO_USER:-}" ] && systemctl --user --machine="$SUDO_USER@" "$@"; }

if [ "${1:-}" = uninstall ]; then
    systemctl stop smcfan smckbd 2>/dev/null || true
    [ -x /usr/local/bin/macbook-smc-patch ] && /usr/local/bin/macbook-smc-patch --uninstall || true
    rm -f /usr/local/bin/macbook-smc-patch "$HOOKS"/75-macbook-smc-patch.hook "$HOOKS"/75-macbook-smc-patch-remove.hook
    systemctl disable macbook-settings 2>/dev/null || true
    as_user stop macbook-kbdlight 2>/dev/null || true
    systemctl --global disable macbook-kbdlight 2>/dev/null || true
    rm -f "$USER_UNIT" /usr/local/bin/macbook-kbdlight
    rm -f /usr/local/bin/smcfan /usr/local/bin/smckbd /usr/local/bin/macbook-settings /usr/local/bin/macbook-control \
          /etc/systemd/system/smcfan.service /etc/systemd/system/smckbd.service \
          /etc/systemd/system/macbook-settings.service /etc/udev/rules.d/90-smcfan.rules "$MBPFAN_DROPIN" \
          "$POLICIES"/org.macbookcontrol.smcfan.policy "$POLICIES"/org.macbookcontrol.settings.policy "$DESKTOP"
    rm -rf "$GUI_DIR"
    systemctl daemon-reload
    udevadm control --reload
    echo "MacBook Control removed."
    exit 0
fi

echo "Installing the services..."
install -Dm755 smcfan.py /usr/local/bin/smcfan
install -Dm755 smckbd.py /usr/local/bin/smckbd
install -Dm644 smcfan.service /etc/systemd/system/smcfan.service
install -Dm644 smckbd.service /etc/systemd/system/smckbd.service
install -Dm644 90-smcfan.rules /etc/udev/rules.d/90-smcfan.rules
install -Dm755 macbook-settings.py /usr/local/bin/macbook-settings
install -Dm644 macbook-settings.service /etc/systemd/system/macbook-settings.service
install -Dm755 macbook-kbdlight.py /usr/local/bin/macbook-kbdlight
install -Dm644 macbook-kbdlight.service "$USER_UNIT"
install -Dm644 org.macbookcontrol.smcfan.policy "$POLICIES"/org.macbookcontrol.smcfan.policy
install -Dm644 org.macbookcontrol.settings.policy "$POLICIES"/org.macbookcontrol.settings.policy
install -Dm755 macbook-smc-patch.py /usr/local/bin/macbook-smc-patch
install -Dm644 75-macbook-smc-patch.hook "$HOOKS"/75-macbook-smc-patch.hook
install -Dm644 75-macbook-smc-patch-remove.hook "$HOOKS"/75-macbook-smc-patch-remove.hook
install -d /etc/macbook-control

# Keep mbpfan for the LTS kernel, but skip it on kernels that ship macsmc instead of applesmc
mkdir -p "$(dirname "$MBPFAN_DROPIN")"
cat > "$MBPFAN_DROPIN" <<'EOF'
[Unit]
ConditionPathExists=!/usr/lib/modules/%v/kernel/drivers/platform/x86/macsmc-acpi.ko.zst
EOF

echo "Installing the app..."
rm -rf "$GUI_DIR"
install -d "$GUI_DIR"
install -m644 gui/*.py "$GUI_DIR"/
cat > /usr/local/bin/macbook-control <<EOF
#!/bin/sh
exec /usr/bin/python3 -I $GUI_DIR/main.py "\$@"
EOF
chmod 755 /usr/local/bin/macbook-control
install -Dm644 macbook-control.desktop "$DESKTOP"
python3 -c "import PySide6" 2>/dev/null || echo "NOTE: the app needs PySide6 to run: sudo pacman -S pyside6"

echo "Checking the SMC driver for every installed kernel..."
/usr/local/bin/macbook-smc-patch --reload || echo "NOTE: the SMC driver fix couldn't be applied (see above)"
udevadm settle

echo "Starting the services..."
systemctl daemon-reload
systemctl enable --quiet macbook-settings   # re-applies charge limit and keyboard options at boot
systemctl reset-failed mbpfan 2>/dev/null || true
udevadm control --reload
udevadm trigger --action=add --subsystem-match=misc --sysname-match=smc
udevadm settle
if [ -e /dev/smc ]; then
    systemctl restart smcfan smckbd   # restart picks up newer versions on re-runs
    systemctl restart macbook-settings || true   # re-apply the charge limit now that the battery driver is up
    sleep 3
else
    echo "NOTE: /dev/smc doesn't exist on this kernel, so smcfan and smckbd can't run (LTS kernel: mbpfan does the fan)"
fi

failed=0
for s in smcfan smckbd; do
    if [ ! -e /dev/smc ]; then
        :
    elif systemctl is-active --quiet "$s"; then
        echo "$s is running."
    else
        echo; echo "$s did NOT start:"; journalctl -u "$s" -b --no-pager -n 15
        failed=1
    fi
done

# The automatic keyboard light runs in each user's session; start it now for whoever ran sudo
systemctl --global enable --quiet macbook-kbdlight
if as_user daemon-reload 2>/dev/null && as_user restart macbook-kbdlight 2>/dev/null && sleep 1 \
        && as_user is-active --quiet macbook-kbdlight; then
    echo "macbook-kbdlight is running (automatic keyboard light)."
else
    echo "NOTE: start the automatic keyboard light in your session with: systemctl --user start macbook-kbdlight"
fi
[ -e /run/smcfan.status ] && { echo; cat /run/smcfan.status; }
[ $failed = 0 ] && echo && echo "Done. Open \"MacBook Control\" from the app menu, or run: macbook-control"
exit $failed
