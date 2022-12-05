#!/usr/bin/env python3

import argparse
import json
import logging
import subprocess
import sys
import time

from typing import Dict, Any, List

# pip install storpool
from storpool import spapi
import confget

# pip install cs
import cs


config: confget.Config = None  # Config is in /etc/storpool/dr.conf
cs_api: cs.CloudStack = None
sp_api: spapi.Api = None


def read_config():
    global config
    config = confget.read_ini_file(confget.Config(
        [], filename="/etc/storpool/dr.conf"
    ))[""]


def get_apis():
    global cs_api, sp_api
    if cs_api is None:
        cs_api = cs.CloudStack(**cs.read_config())
    if sp_api is None:
        sp_api = spapi.Api(
            host=config["SP_API_HTTP_HOST"],
            port=config["SP_API_HTTP_PORT"],
            auth=config["SP_AUTH_TOKEN"]
        )


def get_volumes(vm_uuid: str) -> List[str]:
    global cs_api

    res = cs_api.listVolumes(virtualmachineid=vm_uuid)
    volume_list = res["volume"]
    return [
        vol["id"]
        for vol in volume_list
    ]


def get_vc_policy(vm_uuid: str) -> str:
    global cs_api
    res = cs_api.listTags(
        resoucetype="UserVM",
        resourceid=vm_uuid,
        key="vc-policy"
    )
    tag_list = res["tag"]
    if tag_list:
        value = tag_list[0]["value"]
        logging.debug("vc-policy tag found for VM %s: %s", vm_uuid, value)
        return value
    return None


def get_backup_list() -> Dict[int, Any]:
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
    backup_list = json.loads(process.stdout)
    return backup_list


def get_snapshot_map(backup_list: Dict[str, Any], vm_uuid:str) -> Dict[str, str]:
    """
    Get the latest backup of this VM and return the snapshot map
    """

    backup_name = f"cvm={vm_uuid}"
    for bck in backup_list:
        if (
            bck["type"] == "vm" and
            bck["id"]["name"] == backup_name
        ):
            logging.debug("backups found for VM %s", vm_uuid)
            history = bck["history"]
            transferred_backups = [
                entry
                for entry in history
                if entry["id"]["location"] == config["SP_BACKUP_CLUSTER_ID"]
            ]
            if not transferred_backups:
                return None
            latest = transferred_backups[0]
            logging.debug("The latest backup of VM %s is %d minutes old.",
                vm_uuid,
                latest["age_in_h"] * 60
            )
            return latest["extra_info"]["sp"]["map"]
    # no backups found
    return None


def fix_map(map:Dict[str, Any]) -> None:
    """
    Removes leading ~ in the key names
    """
    for key in list(map.keys()):
        if key[0] == "~":
            trimmed_k = key[1:]
            map[trimmed_k] = map.pop(key)


def check_all_volumes(
        volumes: List[str],
        snapshot_map: Dict[str, str]
) -> bool:
    for volume in volumes:
        if volume not in snapshot_map:
            logging.error("Snapshot missing for volume %s", volume)
            return False
    return True


def create_volume(snapshot: str, vm_uuid: str, vol_uuid: str, vc_policy: str,
                  noop=False) -> str:
    logging.debug("Create a new volume from snapshot %s", snapshot)
    if noop:
        return "NNN.N.NNN"
    else:
        tags = {
            "cs": "volume",
            "cvm": vm_uuid,
            "uuid": vol_uuid,
            "vc_policy": vc_policy,
        }
        res = sp_api.volumeCreate({
            "parent": snapshot,
            "tags": tags,
        })
        name = res.to_json()["name"]
        return name.lstrip("~")


def wait_job(jobid, timeout=10):
    for _ in range(timeout):
        time.sleep(1)
        job = cs_api.queryAsyncJobResult(jobid=jobid)
        if job["jobstatus"] != 0:  # 0 = running
            return job["jobresult"]
    raise RuntimeError("Timeout")


def update_path(volume:str, vol_gid:str, noop=False) -> None:
    logging.debug("Update path, volume %s, vol_gid=%s", volume, vol_gid)
    if noop:
        return
    jobid = cs_api.updateVolume(id=volume, path=f"/dev/storpool-byid/{vol_gid}")["jobid"]
    wait_job(jobid)


def start_vm(vm_uuid: str, noop=False, async_=False) -> None:
    logging.debug("Starting VM %s", vm_uuid)
    if noop:
        return
    jobid = cs_api.startVirtualMachine(
        id=vm_uuid,
        clusterid=config["CS_CLUSTER_ID"]
    )["jobid"]
    if async_:
        logging.info("Async job started - Start VM %s", vm_uuid)
        return jobid
    res = wait_job(jobid, timeout=30)["virtualmachine"]
    logging.info("VM %s, state %s, on host %s", vm_uuid,
                 res.get("state"), res.get("hostname"))


def activate_vm(vm_uuid:str, backup_list, noop=False, async_=False) -> None:
    # get the list of all volumes attached to the VM
    volumes = get_volumes(vm_uuid)

    # get vc-policy tag
    vc_policy = get_vc_policy(vm_uuid)
    if vc_policy is None:
        logging.error("vc-policy tag not found for VM %s", vm_uuid)
        return

    # get the list of all snapshots from the latest backup of the VM
    snapshot_map = get_snapshot_map(backup_list, vm_uuid)
    if not snapshot_map:
        logging.error("No backups found for VM %s", vm_uuid)
        return
    fix_map(snapshot_map)

    # Make sure there is a snapshot for each volume
    if not check_all_volumes(volumes, snapshot_map):
        logging.error("Missing snapshots for VM %s. Skipping this VM")
        return

    for volume, snapshot in snapshot_map.items():
        vol_gid = create_volume(
            snapshot,
            vm_uuid=vm_uuid,
            vol_uuid=volume,
            vc_policy=vc_policy,
            noop=noop
        )
        update_path(volume, vol_gid, noop=noop)

    # start the VM
    start_vm(vm_uuid, noop=noop, async_=async_)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--verbose', action='count', default=0)
    parser.add_argument("-n", "--noop", action="store_true",
        help="Do nothing. Print the commands only")
    parser.add_argument("-a", "--async", dest="async_", action="store_true",
        help="Don't wait for async jobs when possible")
    parser.add_argument("vm", nargs="+", help="List of UUID of VMs to be started")

    args = parser.parse_args()
    if args.verbose > 1:
        logging.basicConfig(level=logging.DEBUG)
        logging.getLogger("urllib3.connectionpool").setLevel(logging.INFO)
    elif args.verbose > 0:
        logging.basicConfig(level=logging.INFO)

    read_config()
    get_apis()

    backup_list = get_backup_list()
    job_list = []
    for vm_uuid in args.vm:
        jobid = activate_vm(vm_uuid, backup_list, noop=args.noop,
            async_=args.async_)
        if args.async_:
            job_list.append(jobid)
    logging.info("%d async jobs started.", len(job_list))

    # ToDo: Wait for async jobs to complete and report the status


if __name__ == "__main__":
    main()
