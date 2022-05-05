#!/usr/bin/env python3
import argparse
import json
import logging
import subprocess
import sys
import time

from typing import Dict, Any

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
        cmd, capture_output=True, check=True, encoding="utf_8"
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
    res = cs_api.listVolumes(virtualmachineid=vm_uuid)
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

    res = cs_api.listVolumes(id=volume_uuid)
    is_error_cs_result(res)
    volume = res["volume"][0]
    volume_size = int(volume["size"] / 2**30)  # in GiB

    snapshot_name = snapshot_map[volume_uuid]
    snapshot_gid = snapshot_name.lstrip("~")

    #
    # create a new cs volume
    #
    logging.info("Create a new volume")
    jobid = cs_api.createVolume(
        diskofferingid = config["CS_BACKUP_DISKOFFERING_ID"],
        zoneid = config["CS_ZONE_ID"],
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



def main():

    """
    list <vm_uuid>
    revert <vm_uuid> <backup_id>
    attach <vm_uuid> <backup_id> <volume_uuid> <server_uuid>
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

    restore_cmd = subparsers.add_parser("revert",
        help="Revert all disks of a VM. Leaves the VM in a STOPPED state"
    )
    restore_cmd.add_argument("vm_uuid", help="UUID of the VM to be reverted")
    restore_cmd.add_argument("backup_id", type=int,
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

    sys.exit("unknown command")

if __name__ == "__main__":
    main()
