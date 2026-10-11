#!/bin/sh
# Creates real loop-mounted filesystems + a local Samba share for the realfs tests.
# Linux, root only. Needs: dosfstools fusefat exfatprogs exfat-fuse ntfs-3g xfsprogs samba smbnetfs.
set -e
F=${1:-/tmp/fs}; mkdir -p "$F"; cd "$F"
mk() { truncate -s 320M "img-$1"; mkdir -p "mnt-$1"; }
mk vfat;  mkfs.vfat -F 32 img-vfat >/dev/null; LANG=C.UTF-8 fusefat -o rw+ img-vfat mnt-vfat
mk exfat; mkfs.exfat img-exfat >/dev/null; mount.exfat-fuse "$(losetup -f --show img-exfat)" mnt-exfat
mk ntfs;  mkfs.ntfs -F -Q img-ntfs >/dev/null 2>&1; ntfs-3g img-ntfs mnt-ntfs
mk xfs;   mkfs.xfs -q img-xfs; mount -o loop img-xfs mnt-xfs
mk ext4;  mkfs.ext4 -q -F img-ext4; mount -o loop img-ext4 mnt-ext4
# SMB: local Samba server, mounted through the smbnetfs FUSE client (no kernel cifs needed)
mkdir -p /tmp/smbshare /run/samba/ncalrpc /tmp/smbpid /tmp/smblock /tmp/smbstate /tmp/smbcache /tmp/smbpriv /tmp/smbncalrpc
chmod 777 /tmp/smbshare
cat > /tmp/smb.conf <<CONF
[global]
  ncalrpc dir = /tmp/smbncalrpc
  server role = standalone server
  map to guest = Bad User
  interfaces = lo
  bind interfaces only = yes
  pid directory = /tmp/smbpid
  lock directory = /tmp/smblock
  state directory = /tmp/smbstate
  cache directory = /tmp/smbcache
  private dir = /tmp/smbpriv
[share]
  path = /tmp/smbshare
  read only = no
  guest ok = yes
  force user = root
CONF
smbd -s /tmp/smb.conf -D; sleep 2
mkdir -p ~/.smb mnt-smb; printf 'auth "guest" ""\nhost 127.0.0.1 visible=true\nlocal_charset "UTF-8"\n' > ~/.smb/smbnetfs.conf; chmod 600 ~/.smb/smbnetfs.conf
LANG=C.UTF-8 smbnetfs mnt-smb; sleep 2
echo "export DATACURATOR_REALFS=vfat=$F/mnt-vfat,exfat=$F/mnt-exfat,ntfs=$F/mnt-ntfs,xfs=$F/mnt-xfs,ext4=$F/mnt-ext4,smb=$F/mnt-smb/127.0.0.1/share"
echo "export DATACURATOR_SMB_KILL=\"pkill -9 smbd\" DATACURATOR_SMB_START=\"smbd -s /tmp/smb.conf -D\""
