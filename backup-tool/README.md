Backup and Restore with CloudStack and VolumeCare
======================================================

Backups are created by VolumeCare based on the policy defined for the VM.
To enable backup of a VM define a tag for the VM:

    vc-policy=<policy>

Where the `<policy>` is the selected policy defined in the VolumeCare
configuration `/etc/storpool/volumecare.conf`

To list available backups and restore use the `backup-tool.py`

Usage
==========

`backup-tool.py` can be used to:

 - list the available backups,
 - revert a VM to a previous state
 - create a new volume from a backup, and attach it to another VM
 - restore a backup onto a different, already-provisioned VM. Typically used to recover a deleted VM

List available backups
-----------------------

    backup-tool.py list [-q] <vm_uuid>

The output is a list of timestamps of the backups for the given VM.

e.g.

```commandline
$ ./backup-tool.py list ce78e620-9168-4794-91d2-88eaedf3d5de
1650637434 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650636534 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650634734 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650631134 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650627534 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650623934 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650620334 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650616734 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650613134 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650609534 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650605934 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650602334 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650598734 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650595134 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650591534 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650587934 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650584334 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650580734 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650577134 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650573534 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650569934 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650566334 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650562734 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650559134 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650555534 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650551934 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
1650533934 ['9306c26f-67a6-4a40-8d81-ad1764221441', 'fe3e930a-9925-492a-aebb-a0460db964a7']
```

Add `-q` flag to list backup IDs only, without volumes' UUID.

Revert the VM to a previous state
---------------------------------

```
backup-tool.py [-v] revert <vm_uuid> <backup_id>
```

where

 - `<vm_uuid>` is the UUID of the VM to be reverted,
 - `<backup_id>` is the timestamp of teh backup as returned by `backup-tool.py
   list`

Note:
The VM will be stopped and all disk attached to the VM will be reverted to the
snapshots in the backup. The VM will remain in the power-off state.


Example:

```commandline
$ ./backup-tool.py -vv revert ce78e620-9168-4794-91d2-88eaedf3d5de 1650613134
DEBUG:root:backups found for VM ce78e620-9168-4794-91d2-88eaedf3d5de
INFO:root:Reverting VM ce78e620-9168-4794-91d2-88eaedf3d5de to backup ID 1650613134
DEBUG:root:Getting volume list for VM UUID ce78e620-9168-4794-91d2-88eaedf3d5de
DEBUG:root:Found 2 volumes for VM ce78e620-9168-4794-91d2-88eaedf3d5de: [('9306c26f-67a6-4a40-8d81-ad1764221441', '~bgu4.b.njm'), ('fe3e930a-9925-492a-aebb-a0460db964a7', '~bgu4.b.njk')]
INFO:root:Stopping VM ce78e620-9168-4794-91d2-88eaedf3d5de
DEBUG:root:VM ce78e620-9168-4794-91d2-88eaedf3d5de is stopped
DEBUG:root:Detaching volumes: ['~bgu4.b.njm', '~bgu4.b.njk']
DEBUG:root:Copy snapshots to the local cluster
DEBUG:root:Revert volumes using local snapshots
DEBUG:root:Revert volume ~bgu4.b.njm to snapshot ~bgu4.b.nqh
DEBUG:root:Revert volume ~bgu4.b.njk to snapshot ~bgu4.b.nq7
DEBUG:root:Delete snapshots on the local cluster
INFO:root:Revert completed
```

Create a New Volume From a Backup, and Attach it to Another VM
--------------------------------------------------------------

This function creates a new volume in CloudStack, restores the selected 
backup/volume to the created volume and attaches the volume to another VM.

This function is useful to extract files from a backup.

This function does not affect the runing VM and does not revert its disks.

The newly created volume ahs to be detached and deleted manually after use.

```commandline
backup-tool.py [-v] attach vm_uuid backup_id volume_uuid server_uuid
```

where
  - vm_uuid is the UUID of the backed up VM
  - backup_id is the selected backup ID (timestamp)
  - volume_uuid is the volume UUID in CloudStack to be restored
  - server_uuid is the UUID of the backup server, where 
    the restored volume will be attached.

Example:

```commandline
$ ./backup-tool.py -vv attach ce78e620-9168-4794-91d2-88eaedf3d5de 1650613134 fe3e930a-9925-492a-aebb-a0460db964a7 a5aa538f-6f6d-427c-beba-f619c4db66b2
DEBUG:root:backups found for VM ce78e620-9168-4794-91d2-88eaedf3d5de
INFO:root:Create a new volume
DEBUG:root:New volume id: ac3e0396-3579-433b-b978-72319f4c27a6
DEBUG:root:Attach and detach the new volume
DEBUG:root:Copy snapshot bgu4.b.nq7 to the local cluster
DEBUG:root:Revert the new volume ~bgu4.b.ntm to the snapshot ~bgu4.b.nq7
DEBUG:root:Attach volume ac3e0396-3579-433b-b978-72319f4c27a6 to VM a5aa538f-6f6d-427c-beba-f619c4db66b2
INFO:root:Volume attached
DEBUG:root:Delete snapshot ~bgu4.b.nq7
```

Restore into a different VM
---------------------------

This function can be used to restore a backup into a VM different from the
original VM. The main use case is restoring a deleted VM. In this case, the
user has to manually recreate the deleted VM, by provisioning a new VM with a
similar configuration - service offering, networks, owner, etc. Critical part
is the restored VM has the same number of disks as the selected backup. The
content of the provisioned disks is discarded, so they may be created for the
original template or empty images. The size of the provisioned disks is also
not important, but for reporting and accounting purposes it is recommended to
provision the disks with the original size.

Once the VM is provisioned, it can be restored with the command:

```commandline

backup-tool.py restore [-h] vm_uuid backup_id new_vm_uuid [root_uuid]
```

where
  - vm_uuid is the UUID of the original (source) VM the backup was taken from.
  - backup_id is the ID of the backup to be restored
  - new_vm_uuid is the UUID of the target VM that will receive the restored
  content. It must already exist and have the same number of volumes as the
  source VM.
  - root_uuid is the UUID of the source VM's ROOT volume, used to identify the
  ROOT snapshot in the backup. Required when the source VM had more than one
  volume; optional if it had only one.


All commands support `-v` or `-vv` to show debug information.

Installation
===================

The host where this script is executed needs the following access:

 - CloudStack API
 - StorPool primary cluster API
 - ssh to a host on the primary cluster with VolumeCare installed (`storpool_vcctl` tool).

Requirements
-------------

 - Python 3.8 or higher
 - Python modules:
   - `storpool`
   - `cs`

```commandline
pip install storpool
pip install cs
```

Configuration
---------------

Edit settings in `backup-tool.conf` and copy it to `/etc/storpool/`.

Edit Cloudstack API credentials in `cloudstack.ini`. `cloudstack.ini` shall be
in saved the directory from where the `backup-tool.py` scripts is executed,
or saved as `~/.cloudstack.ini`.

Add StorPool API details to `storpool.conf` and save it as `/etc/storpoool.conf`.
