#!/bin/bash
# Full clone of the live root SD card onto a USB-attached destination disk.
#
# Usage: sudo backup_sd_image.sh /dev/sdX <step-file>
#
# Never dd's the live, mounted root filesystem — that risks an inconsistent
# image while the show services keep writing to it. Instead: partition +
# format a fresh destination, then rsync both partitions over (the standard
# safe technique for cloning a running Linux root fs), then rewrite the
# destination's own cmdline.txt/fstab to reference its own new PARTUUIDs so
# it boots independently instead of colliding with the source card's.
#
# Three independent safety gates below (regex match, removable flag, not the
# live root disk) — this script is the last line of defense before anything
# destructive happens, so it re-checks everything the caller should have
# already checked rather than trusting it.

set -euo pipefail

DEST_DISK="${1:-}"
STEP_FILE="${2:-}"

if [[ -z "$DEST_DISK" || -z "$STEP_FILE" ]]; then
    echo "usage: $0 /dev/sdX <step-file>" >&2
    exit 1
fi

step() {
    echo "$1" > "$STEP_FILE"
    echo "[backup] $1"
}

fail() {
    step "error:$1"
    echo "[backup] FAILED: $1" >&2
    exit 1
}

# ── Safety gate 1: only ever a whole USB disk, never a partition or the mmc card ──
[[ "$DEST_DISK" =~ ^/dev/sd[a-z]$ ]] || fail "refusing target '$DEST_DISK' — must be a whole /dev/sd? disk"

# ── Safety gate 2: destination must not be the disk backing / ──
ROOT_SRC=$(findmnt -n -o SOURCE /) || fail "could not determine root source"
ROOT_DISK=$(lsblk -no PKNAME "$ROOT_SRC" 2>/dev/null || true)
[[ -n "$ROOT_DISK" && "/dev/$ROOT_DISK" != "$DEST_DISK" ]] || fail "refusing target '$DEST_DISK' — it is the live root disk"

# ── Safety gate 3: destination must be removable ──
# -d/--nodeps restricts lsblk to the disk itself — without it, lsblk also
# prints RM for every partition on the disk, so a partitioned card came back
# as "1\n1\n1" and never equaled the literal "1" this was compared against.
RM=$(lsblk -dno RM "$DEST_DISK" 2>/dev/null || echo "")
[[ "$RM" == "1" ]] || fail "refusing target '$DEST_DISK' — not flagged removable (RM=$RM)"

# ── Safety gate 4: destination must not currently be mounted anywhere ──
# This is what actually protects a drive like the music-library USB stick —
# it's removable and isn't the root disk, so gates 1-3 alone would let it
# through. A blank/spare card reader has no mountpoints at all; anything
# that does is a drive already in use for something else. Refuse rather
# than force-unmounting it out from under whatever's using it.
MOUNTED=$(lsblk -no MOUNTPOINT "$DEST_DISK" 2>/dev/null | grep -v '^$' || true)
[[ -z "$MOUNTED" ]] || fail "refusing target '$DEST_DISK' — currently mounted at: $MOUNTED"

SRC_BOOT_DEV=/dev/mmcblk0p1
SRC_ROOT_MNT=/

# ── Pre-flight size check (defense in depth — Flask already checked this) ──
DEST_BYTES=$(blockdev --getsize64 "$DEST_DISK")
USED_BYTES=$(df --output=used -B1 "$SRC_ROOT_MNT" | tail -1 | tr -d ' ')
MARGIN=$(( USED_BYTES * 15 / 100 ))
[[ $MARGIN -ge 2147483648 ]] || MARGIN=2147483648
NEEDED=$(( USED_BYTES + MARGIN ))
[[ $DEST_BYTES -ge $NEEDED ]] || fail "destination too small ($DEST_BYTES bytes, need ~$NEEDED)"

step "unmounting_dest"
for p in "${DEST_DISK}"?*; do
    [[ -b "$p" ]] || continue
    umount "$p" 2>/dev/null || true
done

step "partitioning"
SRC_BOOT_SIZE=$(blockdev --getsize64 "$SRC_BOOT_DEV")
BOOT_END_MIB=$(( SRC_BOOT_SIZE / 1024 / 1024 + 1 ))
parted --script "$DEST_DISK" \
    mklabel msdos \
    mkpart primary fat32 1MiB "${BOOT_END_MIB}MiB" \
    mkpart primary ext4 "${BOOT_END_MIB}MiB" 100%
partprobe "$DEST_DISK" || true
sleep 2

BOOT_PART="${DEST_DISK}1"
ROOT_PART="${DEST_DISK}2"
[[ -b "$BOOT_PART" && -b "$ROOT_PART" ]] || fail "partition nodes did not appear after partitioning"

step "formatting"
mkfs.vfat -F 32 -n bootfs "$BOOT_PART"
mkfs.ext4 -F -L rootfs "$ROOT_PART"

step "mounting"
DEST_ROOT_MNT=$(mktemp -d /mnt/musicman_backup_root.XXXXXX)
cleanup() {
    umount "$DEST_ROOT_MNT/boot/firmware" 2>/dev/null || true
    umount "$DEST_ROOT_MNT" 2>/dev/null || true
    rmdir "$DEST_ROOT_MNT" 2>/dev/null || true
}
trap cleanup EXIT

mount "$ROOT_PART" "$DEST_ROOT_MNT"
mkdir -p "$DEST_ROOT_MNT/boot/firmware"
mount "$BOOT_PART" "$DEST_ROOT_MNT/boot/firmware"

step "rsync_boot"
rsync -aHAX --info=progress2 /boot/firmware/ "$DEST_ROOT_MNT/boot/firmware/"

step "rsync_root"
rsync -aHAXx --info=progress2 \
    --exclude='/proc/*' --exclude='/sys/*' --exclude='/dev/*' \
    --exclude='/tmp/*' --exclude='/run/*' --exclude='/mnt/*' \
    --exclude='/media/*' --exclude='/lost+found' \
    --exclude="$DEST_ROOT_MNT" \
    / "$DEST_ROOT_MNT/" \
    | tee -a "${STEP_FILE}.rsync_progress" \
    | while read -r line; do
        echo "rsync_root:$line" > "$STEP_FILE"
      done

step "fixup"
mkdir -p "$DEST_ROOT_MNT"/{proc,sys,dev,tmp,run,mnt,media}
NEW_BOOT_PUUID=$(blkid -s PARTUUID -o value "$BOOT_PART")
NEW_ROOT_PUUID=$(blkid -s PARTUUID -o value "$ROOT_PART")
[[ -n "$NEW_BOOT_PUUID" && -n "$NEW_ROOT_PUUID" ]] || fail "could not read new PARTUUIDs"

sed -i "s/PARTUUID=[a-f0-9]*-01/PARTUUID=${NEW_BOOT_PUUID}/" "$DEST_ROOT_MNT/boot/firmware/cmdline.txt"
sed -i "s/PARTUUID=[a-f0-9]*-02/PARTUUID=${NEW_ROOT_PUUID}/" "$DEST_ROOT_MNT/boot/firmware/cmdline.txt"
sed -i "s/PARTUUID=[a-f0-9]*-01/PARTUUID=${NEW_BOOT_PUUID}/" "$DEST_ROOT_MNT/etc/fstab"
sed -i "s/PARTUUID=[a-f0-9]*-02/PARTUUID=${NEW_ROOT_PUUID}/" "$DEST_ROOT_MNT/etc/fstab"

step "unmounting"
trap - EXIT
cleanup

rm -f "${STEP_FILE}.rsync_progress"
step "done"
