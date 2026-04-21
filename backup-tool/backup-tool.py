#!/usr/bin/env python3
import argparse
import json
import logging
import subprocess
import sys
import time

from typing import Dict, Any, Optional

# pip install cs
import cs

# pip install storpool
from storpool import spapi
import confget

config = None  # Config is in /etc/storpool/backup-tool.conf
cs_api = None
sp_api = None


def get_apis():
    global cs_api, sp_api
    if cs_api is None:
        cs_api = cs.CloudStack(**cs.read_config())
    if sp_api is None:
        sp_api = spapi.Api.fromConfig()


def read_config():
    global config
    config = confget.read_ini_file(confget.Config(
        [], filename="/etc/storpool/backup-tool.conf"
    ))[""]


def get_backup_list(vm: str) -> Dict[int, Dict[str, Any]]:
    cmd = [
        'storpool_vcctl',
        'status',
        '--json',
    ]

    if "VC_SSH_HOST" in config:
        cmd = [
            "ssh",
            "-l", config.get("VC_SSH_USER", "root"),
            config["VC_SSH_HOST"],
        ] + cmd

    process = subprocess.run(
        cmd, stdout=subprocess.PIPE, check=True, encoding="utf_8"
    )
    res = json.loads(process.stdout)

    backup_name = f"cvm={vm}"
    for bck in res:
        if (
            bck["type"] == "vm" and
            bck["id"]["name"] == backup_name
        ):
            logging.debug("backups found for VM %s", vm)
            history = bck["history"]
            return {
                entry["create_ts"]: entry
                for entry in history
                if entry["id"]["location"] == config["SP_BACKUP_CLUSTER_ID"]
            }

    # no backups found
    return {}


def wait_job(jobid, timeout=10):
    for _ in range(timeout):
        time.sleep(1)
        job = cs_api.queryAsyncJobResult(jobid=jobid)
        if job["jobstatus"] != 0:  # 0 = running
            return job["jobresult"]
    raise RuntimeError("Timeout")


def fix_map(map:Dict[Any, Any]) -> None:
    """
    Removes leading ~ in the key names
    """
    for key in list(map.keys()):
        if key[0] == "~":
            trimmed_k = key[1:]
            map[trimmed_k] = map.pop(key)



def list_volumes(backup_list, quiet=False):
    for ts, backup in backup_list.items():
        snapshot_map: Dict[str, str] = backup["extra_info"]["sp"]["map"]
        fix_map(snapshot_map)
        volume_uuids = list(snapshot_map.keys())
        if quiet:
            print(ts)
        else:
            print(ts,
                time.strftime("%c %Z", time.localtime(ts)),
                volume_uuids
            )



def is_error_cs_result(res):
    if "errorcode" in res:
        logging.error("Error executing CS command: %s", res["errortext"])
        sys.exit(1)


def revert_vm(backup: Dict[str, Any]) -> None:
    """
    Restores a VM from a backup

    :param backup:
    :return:
    """

    vm_uuid = backup["entity_id"]["name"].split("=", maxsplit=1)[1]
    snapshot_map: Dict[str, str] = backup["extra_info"]["sp"]["map"]
    fix_map(snapshot_map)
    logging.info("Reverting VM %s to backup ID %s", vm_uuid,
        backup["create_ts"])

    logging.debug("Getting volume list for VM UUID %s", vm_uuid)
    # get volumes uuid and sp GID
    res = cs_api.listVolumes(virtualmachineid=vm_uuid, listall=True)
    is_error_cs_result(res)

    # make sure all volumes are in the backup
    volume_list = res["volume"]
    for vol in volume_list:
        volume_uuid = vol["id"]
        if volume_uuid not in snapshot_map:
            raise RuntimeError(f"Volume {volume_uuid} not found in the backup")
        vol["sp_snapshot"] = snapshot_map[volume_uuid]
        sp_gid = vol["path"].split("/")[-1]
        vol["sp_volume_name"] = "~" + sp_gid

    logging.debug("Found %d volumes for VM %s: %s",
        len(volume_list),
        vm_uuid,
        repr([(v["id"], v["sp_volume_name"]) for v in volume_list])
    )

    # Stop the VM
    logging.info("Stopping VM %s", vm_uuid)
    jobid = cs_api.stopVirtualMachine(id=vm_uuid, forced=True)["jobid"]
    res = wait_job(jobid, timeout=30)
    is_error_cs_result(res)
    vm = res["virtualmachine"]
    assert vm["state"] == "Stopped"
    logging.debug("VM %s is stopped", vm_uuid)

    # detach all volumes. May not be needed, but to ensure
    logging.debug(
        "Detaching volumes: %s",
        [v["sp_volume_name"] for v in volume_list]
    )
    args = {
        "reassign": [
            {
                "volume": vol["sp_volume_name"],
                "detach": "all",
            }
            for vol in volume_list
        ],
    }
    sp_api.volumesReassignWait(args)

    # copy snapshots to the local cluster
    logging.debug("Copy snapshots to the local cluster")
    for vol in volume_list:
        snapshot_name = vol["sp_snapshot"]
        snapshot_gid = snapshot_name.lstrip("~")
        args = {
            "remoteId": snapshot_gid,
            "remoteLocation": config["SP_BACKUP_LOCATION_NAME"],
            "template": config["SP_LOCAL_TEMPLATE"],
        }
        try:
            res = sp_api.snapshotFromRemote(args)
        except spapi.ApiError as err:
            # A local copy of the snapshot may already be created. This is OK.
            if err.name != "objectExists":
                raise

    # revert volumes using local snapshots
    logging.debug("Revert volumes using local snapshots")
    for vol in volume_list:
        volume_name = vol["sp_volume_name"]
        snapshot_name = vol["sp_snapshot"]
        args = {
            "toSnapshot": snapshot_name,
        }
        logging.debug("Revert volume %s to snapshot %s",
            volume_name, snapshot_name)
        res = sp_api.volumeRevert(volume_name, args)

    # delete snapshots on the local cluster
    logging.debug("Delete snapshots on the local cluster")
    for vol in volume_list:
        snapshot_name = vol["sp_snapshot"]
        sp_api.snapshotDelete(snapshot_name)

    logging.info("Revert completed")


def create_volume_and_attach(
        volume_uuid: str,
        backup: Dict[str, Any],
        server: str
) -> None:

    """
    Creates a new volume in CS, restores the content of the backup to this
    volume, and attach the volume to an existing VM (server).

    :param volume_uuid: UUID of the volume to be restored
    :param backup: backup item, as returned by get_backup_list()
    :param server: UUID of the VM that the restored volume will be attached to
    :return: None
    """

    snapshot_map: Dict[str, str] = backup["extra_info"]["sp"]["map"]
    fix_map(snapshot_map)

    snapshot_name = snapshot_map[volume_uuid]
    snapshot_gid = snapshot_name.lstrip("~")

    #
    # copy the snapshot to the local cluster
    #
    logging.debug("Copy snapshot %s to the local cluster", snapshot_gid)
    args = {
        "remoteId": snapshot_gid,
        "remoteLocation": config["SP_BACKUP_LOCATION_NAME"],
        "template": config["SP_LOCAL_TEMPLATE"],
    }
    try:
        res = sp_api.snapshotFromRemote(args)
    except spapi.ApiError as err:
        # A local copy of the snapshot may already be created. This is OK.
        if err.name != "objectExists":
            raise

    snapshot_size = sp_api.snapshotDescribe(snapshot_name).size
    volume_size = int(snapshot_size / 2**30)
    logging.debug("Getting the size of the snapshot for the new volume %s", volume_size)

    #
    # Get VM's account, domain ID, zone ID
    # We'll need this to create the volume in the same domain, account, zone
    #

    res = cs_api.listVirtualMachines(id=server)
    vm = res["virtualmachine"][0]
    assert "account" in vm, "Can't get VM's account"
    assert "domainid" in vm, "Can't get VM's domainId"
    assert "zoneid" in vm, "Can't get VM's zoneId"


    #
    # create a new cs volume
    #
    logging.info("Create a new volume")
    logging.debug("Creating the new volume in domain ID %s", vm["domainid"])
    logging.debug("Creating the new voluem with account %s", vm["account"])
    jobid = cs_api.createVolume(
        account = vm["account"],
        domainid = vm["domainid"],
        diskofferingid = config["CS_BACKUP_DISKOFFERING_ID"],
        zoneid = vm["zoneid"],
        size = volume_size,
        name = f"Restore of {volume_uuid}"
    )["jobid"]
    res = wait_job(jobid)
    is_error_cs_result(res)
    new_cs_volume = res["volume"]
    assert new_cs_volume["state"] == "Allocated"
    new_volume_uuid = new_cs_volume["id"]
    logging.debug("New volume id: %s", new_volume_uuid)

    #
    # attach and detach the volume to change the state to from Allocated to Ready
    #
    logging.debug("Attach and detach the new volume")
    jobid = cs_api.attachVolume(id=new_volume_uuid, virtualmachineid=server)["jobid"]
    res = wait_job(jobid)
    is_error_cs_result(res)
    assert res["volume"]["state"] == "Ready"

    jobid = cs_api.detachVolume(id=new_volume_uuid)["jobid"]
    res = wait_job(jobid)
    is_error_cs_result(res)
    new_cs_volume = res["volume"]
    assert new_cs_volume["state"] == "Ready"

    # Fix. ACS 4.16 doesn't update the path on attach/detach.
    res = cs_api.listVolumes(id=new_volume_uuid)
    is_error_cs_result(res)
    new_cs_volume = res["volume"][0]

    sp_volume_gid = new_cs_volume["path"].split("/")[-1]
    sp_volume_name = f"~{sp_volume_gid}"

    #
    # revert the newly created SP volume to the snapshot
    #
    logging.debug(
        "Revert the new volume %s to the snapshot %s", sp_volume_name,
        snapshot_name
    )
    args = {
        "toSnapshot": snapshot_name,
    }
    res = sp_api.volumeRevert(sp_volume_name, args)

    #
    # attach the cs volume to the VM
    #
    logging.debug("Attach volume %s to VM %s", new_volume_uuid, server)
    jobid = cs_api.attachVolume(id=new_volume_uuid, virtualmachineid=server)["jobid"]
    vol = wait_job(jobid)["volume"]
    assert vol["state"] == "Ready"
    assert vol["virtualmachineid"] == server
    logging.info("Volume attached")

    #
    # delete snapshots on the local cluster
    #
    logging.debug("Delete snapshot %s", snapshot_name)
    sp_api.snapshotDelete(snapshot_name)


def check_backup_is_uuid_format(backup_list) -> None:
    for ts, backup in backup_list.items():
        snapshot_map = backup["extra_info"]["sp"]["map"]
        for key in snapshot_map.keys():
            if len(key) != 36 and len(key) != 37:
                raise RuntimeError(
                    f"Backup {ts} is in old gID format. Make sure VolumeCare "
                    "configuration in /etc/storpool/volumecare.conf has "
                    "`id_tag=uuid` setting in [volumecare] section."
                )


def restore_vm(
        backup: Dict[str, Any],
        new_vm_uuid: str,
        root_uuid: Optional[str],
) -> None:

    """
    Restore the content of a backup onto an existing, different VM.

    This is used to recover a VM whose original has been deleted (or is
    otherwise unavailable) by replaying its backup onto a freshly
    provisioned replacement VM.

    The target VM (``new_vm_uuid``) must already exist and must have the
    same number of volumes as the backup. The target VM's ROOT volume is
    restored from the source VM's ROOT snapshot; the remaining DATADISKs
    are paired with the backup's remaining snapshots in arbitrary order
    (the operator is expected to treat data disks as interchangeable).

    Volume sizes on the target VM do not need to match the backup: the
    StorPool ``volumeRevert`` operation resizes each target volume to
    match its snapshot.

    The target VM is stopped (force) before the revert and is left in the
    ``Stopped`` state when this function returns, so that the operator
    can inspect it before starting.

    :param backup: backup item, as returned by :func:`get_backup_list`.
    :param new_vm_uuid: UUID of the target VM that will receive the
        restored content.
    :param root_uuid: UUID of the ROOT volume of the *source* VM (the one
        the backup was taken from). Required when the source VM had more
        than one volume, so the ROOT snapshot can be identified. May be
        ``None`` when the backup contains a single volume.
    :raises RuntimeError: if ``root_uuid`` is required but missing or
        unknown, or if the target VM's volume count does not match the
        backup, or if the target VM does not have exactly one ROOT
        volume.
    :return: None
    """

    snapshot_map: Dict[str, str] = backup["extra_info"]["sp"]["map"]
    fix_map(snapshot_map)

    original_volume_uuids = list(snapshot_map.keys())

    #
    # Figure out which of the source's volumes is the ROOT.
    # If the source VM had only one volume, the ROOT is unambiguous.
    #
    if len(original_volume_uuids) == 1:
        source_root_uuid = original_volume_uuids[0]
        if root_uuid is not None and root_uuid != source_root_uuid:
            raise RuntimeError(
                f"Specified root UUID {root_uuid} does not match the only "
                f"volume in the backup ({source_root_uuid})"
            )
    else:
        if root_uuid is None:
            raise RuntimeError(
                "Source VM had multiple volumes; the UUID of the original "
                "ROOT volume must be specified"
            )
        if root_uuid not in snapshot_map:
            raise RuntimeError(
                f"Root UUID {root_uuid} not found in the backup"
            )
        source_root_uuid = root_uuid

    logging.info("Restoring backup %s to VM %s",
        backup["create_ts"], new_vm_uuid)

    #
    # Get the target VM's volumes and match them to the backup's snapshots.
    #
    logging.debug("Getting volume list for target VM UUID %s", new_vm_uuid)
    res = cs_api.listVolumes(virtualmachineid=new_vm_uuid, listall=True)
    is_error_cs_result(res)
    target_volumes = res["volume"]

    if len(target_volumes) != len(original_volume_uuids):
        raise RuntimeError(
            f"Target VM has {len(target_volumes)} volume(s), but backup "
            f"has {len(original_volume_uuids)} volume(s)"
        )

    target_roots = [v for v in target_volumes if v["type"] == "ROOT"]
    target_datadisks = [v for v in target_volumes if v["type"] == "DATADISK"]

    if len(target_roots) != 1:
        raise RuntimeError(
            f"Target VM must have exactly one ROOT volume, "
            f"found {len(target_roots)}"
        )
    target_root = target_roots[0]

    source_datadisk_uuids = [
        u for u in original_volume_uuids if u != source_root_uuid
    ]

    if len(source_datadisk_uuids) != len(target_datadisks):
        raise RuntimeError(
            f"Target VM has {len(target_datadisks)} DATADISK(s), but "
            f"backup has {len(source_datadisk_uuids)} DATADISK(s)"
        )

    def annotate(vol: Dict[str, Any], source_uuid: str) -> None:
        vol["sp_source_uuid"] = source_uuid
        vol["sp_snapshot"] = snapshot_map[source_uuid]
        sp_gid = vol["path"].split("/")[-1]
        vol["sp_volume_name"] = "~" + sp_gid

    annotate(target_root, source_root_uuid)

    #
    # Pair target DATADISKs with source DATADISK snapshots in arbitrary
    # order. There is no mapping preserved in the backup and the order
    # doesn't matter: volumeRevert will adjust the target volume's size
    # to match the snapshot.
    #
    for target_vol, source_uuid in zip(target_datadisks, source_datadisk_uuids):
        annotate(target_vol, source_uuid)

    volume_list = [target_root] + target_datadisks

    logging.debug(
        "Matched %d volume(s) on VM %s: %s",
        len(volume_list),
        new_vm_uuid,
        repr([
            (v["id"], v["sp_volume_name"], v["sp_source_uuid"])
            for v in volume_list
        ])
    )

    # Stop the target VM
    logging.info("Stopping VM %s", new_vm_uuid)
    jobid = cs_api.stopVirtualMachine(id=new_vm_uuid, forced=True)["jobid"]
    res = wait_job(jobid, timeout=30)
    is_error_cs_result(res)
    vm = res["virtualmachine"]
    assert vm["state"] == "Stopped"
    logging.debug("VM %s is stopped", new_vm_uuid)

    # Detach all volumes on the StorPool side. May not be needed, but to ensure
    logging.debug(
        "Detaching volumes: %s",
        [v["sp_volume_name"] for v in volume_list]
    )
    args = {
        "reassign": [
            {
                "volume": vol["sp_volume_name"],
                "detach": "all",
            }
            for vol in volume_list
        ],
    }
    sp_api.volumesReassignWait(args)

    # Copy snapshots to the local cluster
    logging.debug("Copy snapshots to the local cluster")
    for vol in volume_list:
        snapshot_name = vol["sp_snapshot"]
        snapshot_gid = snapshot_name.lstrip("~")
        args = {
            "remoteId": snapshot_gid,
            "remoteLocation": config["SP_BACKUP_LOCATION_NAME"],
            "template": config["SP_LOCAL_TEMPLATE"],
        }
        try:
            sp_api.snapshotFromRemote(args)
        except spapi.ApiError as err:
            # A local copy of the snapshot may already be created. This is OK.
            if err.name != "objectExists":
                raise

    # Revert target volumes using the local snapshots
    logging.debug("Revert volumes using local snapshots")
    for vol in volume_list:
        volume_name = vol["sp_volume_name"]
        snapshot_name = vol["sp_snapshot"]
        logging.debug("Revert volume %s to snapshot %s",
            volume_name, snapshot_name)
        sp_api.volumeRevert(volume_name, {"toSnapshot": snapshot_name,
                                          "revertSize": True})

    # Delete snapshots on the local cluster
    logging.debug("Delete snapshots on the local cluster")
    for vol in volume_list:
        sp_api.snapshotDelete(vol["sp_snapshot"])

    logging.info("Restore completed")


def main():

    """
    list <vm_uuid>
    revert <vm_uuid> <backup_id>
    attach <vm_uuid> <backup_id> <volume_uuid> <server_uuid>
    restore <vm_uuid> <backup_id> <new_vm_uuid> [<old_root_uuid>]
    """

    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--verbose', action='count', default=0)
    subparsers = parser.add_subparsers(dest="command")

    list_cmd = subparsers.add_parser("list",
        help="List available backups of a VM")
    list_cmd.add_argument("-q", "--quiet", action='store_true',
        help="List backup IDs only. Don't show volumes' UUID"
    )
    list_cmd.add_argument("vm_uuid", help="UUID of the VM")

    revert_cmd = subparsers.add_parser("revert",
        help="Revert all disks of a VM. Leaves the VM in a STOPPED state"
    )
    revert_cmd.add_argument("vm_uuid", help="UUID of the VM to be reverted")
    revert_cmd.add_argument("backup_id", type=int,
        help="ID of the backup to be restored")


    attach_cmd = subparsers.add_parser("attach",
        help="Attach a single disk from a backup as a disk to another VM"
             " (e.g. backup server). This operation doesn't revert the VM."
    )
    attach_cmd.add_argument("vm_uuid", help="UUID of the source VM")
    attach_cmd.add_argument("backup_id", type=int, help="ID of the backup")
    attach_cmd.add_argument("volume_uuid",
        help="UUID of the volume to be restored")
    attach_cmd.add_argument(
        "server_uuid",
        help="UUID of the backup server, where the restored volume will be attached."
    )

    restore_cmd = subparsers.add_parser("restore",
        help="Restore a backup onto a different, already-provisioned VM. "
            "Typically used to recover a deleted or unrecoverable VM by "
            "replaying its backup onto a replacement VM. The target VM "
            "must have the same number of volumes as the backup; volume "
            "sizes do not need to match (they will be adjusted by the "
            "revert). The target VM is left in the STOPPED state."
    )
    restore_cmd.add_argument("vm_uuid",
        help="UUID of the original (source) VM the backup was taken from.")
    restore_cmd.add_argument("backup_id", type=int,
        help="ID of the backup to be restored")
    restore_cmd.add_argument("new_vm_uuid",
        help="UUID of the target VM that will receive the restored content. "
            "It must already exist and have the same number of volumes as "
            "the source VM.")
    restore_cmd.add_argument("root_uuid", nargs="?",
        help="UUID of the source VM's ROOT volume, used to identify the "
            "ROOT snapshot in the backup. Required when the source VM had "
            "more than one volume; optional if it had only one.")

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return 1

    if args.verbose > 1:
        logging.basicConfig(level=logging.DEBUG)
        logging.getLogger("urllib3.connectionpool").setLevel(logging.INFO)
    elif args.verbose > 0:
        logging.basicConfig(level=logging.INFO)

    read_config()
    get_apis()

    if args.command == "list":
        backup_list = get_backup_list(args.vm_uuid)
        check_backup_is_uuid_format(backup_list)
        list_volumes(backup_list, args.quiet)
        return 0

    if args.command == "revert":
        backup_list = get_backup_list(args.vm_uuid)
        try:
            backup = backup_list[args.backup_id]
        except KeyError:
            logging.error("Backup ID %s not found for VM %s",
                          args.backup_id, args.vm_uuid)
            sys.exit(1)
        revert_vm(backup)
        return 0

    if args.command == "attach":
        backup_list = get_backup_list(args.vm_uuid)
        try:
            backup = backup_list[args.backup_id]
        except KeyError:
            logging.error("Backup ID %s not found for VM %s",
                          args.backup_id, args.vm_uuid)
            sys.exit(1)
        create_volume_and_attach(args.volume_uuid, backup, args.server_uuid)
        return 0

    if args.command == "restore":
        backup_list = get_backup_list(args.vm_uuid)
        try:
            backup = backup_list[args.backup_id]
        except KeyError:
            logging.error("Backup ID %s not found for VM %s",
                          args.backup_id, args.vm_uuid)
            sys.exit(1)
        restore_vm(backup, args.new_vm_uuid, args.root_uuid)
        return 0


    sys.exit("unknown command")

if __name__ == "__main__":
    main()
