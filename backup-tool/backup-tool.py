#!/usr/bin/env python3
import argparse
import json
import logging
import subprocess
import sys
import time

from typing import Dict, Any, List, Optional

# pip install cs
import cs

# pip install storpool
from storpool import spapi
import confget

config = None  # Config is in /etc/storpool/backup-tool.conf
config_all = None
cs_api = None
sp_api = None     # operations client (see get_apis)
sp_api_mc = None  # multicluster discovery client (see get_apis)


def is_multicluster() -> bool:
    return config.get("SP_MULTICLUSTER", "").lower() in ("1", "true", "yes")


def get_cluster_config(cluster_name: Optional[str]) -> Dict[str, Any]:
    """Return global settings merged with optional per-cluster overrides."""
    merged = dict(config)
    if cluster_name:
        section = config_all.get(f"cluster {cluster_name}", {})
        merged.update(section)
    return merged


def sp_cluster_kwargs(cluster_name: Optional[str]) -> Dict[str, str]:
    """
    Build the StorPool bindings kwargs to target a specific subcluster.

    StorPool can address a cluster by its ID (rather than its registered
    name) using the ``~<clusterID>`` form. The bindings turn the
    ``clusterName`` kwarg into a ``RemoteCommand/<value>/`` path component,
    so passing ``~nmjc.b`` forwards the call to that subcluster to run as a
    local operation (e.g. ``RemoteCommand/~nmjc.b/VolumeRevert/...``).

    ``cluster_name`` is the bare StorPool cluster ID (e.g. ``nmjc.b``) as
    reported in the StorPool volume info.
    """
    if is_multicluster() and cluster_name:
        target = cluster_name
        if not target.startswith("~"):
            target = f"~{target}"
        return {"clusterName": target}
    return {}


def sp_volume_name_from_cs_volume(vol: Dict[str, Any]) -> str:
    """
    Build the StorPool volume reference (``~<globalId>``) from a CloudStack
    volume dict.

    The StorPool global id is the last component of the CloudStack volume
    ``path`` (e.g. a path ending in ``fir.b.qrd`` yields ``~fir.b.qrd``).
    Raise a clear error -- instead of a bare ``KeyError`` -- when the volume
    has no StorPool path yet (e.g. an ``Allocated`` volume).
    """
    path = vol.get("path")
    if not path:
        raise RuntimeError(
            f"CloudStack volume {vol.get('id')} has no StorPool path "
            "(not provisioned on StorPool yet); cannot continue"
        )
    return "~" + path.split("/")[-1]


def cluster_id_from_global_id(sp_volume_name: str) -> Optional[str]:
    """
    Derive the StorPool cluster id from a global volume/snapshot id.

    A StorPool global id has the form ``<location>.<subcluster>.<seq>`` (e.g.
    ``fir.b.qrd``), so its first two dot-separated tokens are the cluster id
    (``fir.b``). This reflects the cluster of *origin*; for a volume that has
    been migrated to another subcluster the authoritative current residence is
    ``VolumeSummary.clusterId`` (see :func:`get_cluster_for_sp_volume`). Used
    only as a fallback when the per-volume lookup cannot report a cluster.

    Returns ``None`` when ``sp_volume_name`` is not a three-part global id
    (e.g. it is a plain volume name).
    """
    parts = sp_volume_name.lstrip("~").split(".")
    if len(parts) == 3:
        return ".".join(parts[:2])
    return None


def get_cluster_for_sp_volume(sp_volume_name: str) -> Optional[str]:
    """
    Return the StorPool cluster ID where a volume resides.

    Uses the multicluster StorPool volume info (``MultiCluster/Volume`` /
    ``VolumeSummary.clusterId``), so it works regardless of the CloudStack
    VM's power state or host placement. Falls back to the cluster id embedded
    in the global id (see :func:`cluster_id_from_global_id`) when the lookup
    does not return a ``clusterId``.

    ``sp_volume_name`` is the StorPool volume reference (``~<globalId>``).
    Returns ``None`` when the cluster cannot be determined.
    """
    if not is_multicluster():
        return None
    try:
        vols = sp_api_mc.volumeList(sp_volume_name, returnRawAPIData=True)
    except spapi.ApiError as err:
        logging.warning(
            "Can't query StorPool volume %s for its cluster: %s",
            sp_volume_name, err
        )
        return cluster_id_from_global_id(sp_volume_name)
    for vol in vols:
        cluster_id = vol.get("clusterId")
        if cluster_id:
            return cluster_id
    return cluster_id_from_global_id(sp_volume_name)


def get_vm_cluster_name(vm_uuid: str) -> Optional[str]:
    """
    Resolve the StorPool subcluster ID where a CloudStack VM's volumes reside.

    The subcluster is determined from the StorPool volume info of the VM's
    volumes (see :func:`get_cluster_for_sp_volume`), not from CloudStack host
    placement, so it works for Stopped or freshly-provisioned VMs as well.
    The bare cluster ID is returned (e.g. ``nmjc.b``); :func:`sp_cluster_kwargs`
    adds the ``~`` prefix when forming the StorPool ``RemoteCommand`` target.

    Returns ``None`` (meaning: operate on the default cluster the API client
    is connected to) when not in multicluster mode or when the cluster cannot
    be determined.
    """
    if not is_multicluster():
        return None

    res = cs_api.listVolumes(virtualmachineid=vm_uuid, listall=True)
    is_error_cs_result(res)
    volumes = res.get("volume", [])
    if not volumes:
        logging.warning(
            "VM %s has no volumes; using the default StorPool cluster", vm_uuid
        )
        return None

    cluster_ids = set()
    for vol in volumes:
        # A volume may not have a StorPool path yet (e.g. Allocated state);
        # skip those and rely on the remaining volumes.
        path = vol.get("path")
        if not path:
            continue
        cluster_id = get_cluster_for_sp_volume("~" + path.split("/")[-1])
        if cluster_id:
            cluster_ids.add(cluster_id)

    if not cluster_ids:
        logging.warning(
            "Could not determine the StorPool cluster for VM %s from its "
            "volumes; using the default StorPool cluster", vm_uuid
        )
        return None

    if len(cluster_ids) > 1:
        raise RuntimeError(
            f"VM {vm_uuid} has volumes on multiple StorPool clusters "
            f"({', '.join(sorted(cluster_ids))}); this is not supported"
        )

    return next(iter(cluster_ids))


def all_configured_backup_cluster_ids() -> set:
    """Return every SP_BACKUP_CLUSTER_ID known from the config.

    This is the global value plus any per-subcluster ``[cluster <id>]``
    override. Used as a fallback when the VM's own subcluster cannot be
    resolved.
    """
    ids = set()
    global_id = config.get("SP_BACKUP_CLUSTER_ID")
    if global_id:
        ids.add(global_id)
    for section, settings in config_all.items():
        if section.startswith("cluster ") and settings.get("SP_BACKUP_CLUSTER_ID"):
            ids.add(settings["SP_BACKUP_CLUSTER_ID"])
    return ids


def get_backup_cluster_ids(vm_uuid: str, accept_all: bool = False) -> set:
    """Return the set of StorPool backup-location IDs to accept for a VM.

    Prefer the backup location configured for the VM's own subcluster. When
    that subcluster cannot be determined -- e.g. the source VM has already
    been deleted, which is the typical ``restore`` case -- fall back to every
    backup location known from the config so the backups are still found.

    ``accept_all`` forces the union of every configured backup location. This
    is used by the ``restore`` command, whose source VM may be deleted,
    migrated, or live on a different subcluster than where its backup was
    written -- so constraining to the source VM's current subcluster could
    hide an otherwise-valid backup.
    """
    if is_multicluster():
        if accept_all:
            return all_configured_backup_cluster_ids()
        cluster_name = get_vm_cluster_name(vm_uuid)
        if cluster_name:
            return {get_cluster_config(cluster_name)["SP_BACKUP_CLUSTER_ID"]}
        return all_configured_backup_cluster_ids()
    return {config["SP_BACKUP_CLUSTER_ID"]}


def get_apis():
    global cs_api, sp_api, sp_api_mc
    if cs_api is None:
        cs_api = cs.CloudStack(**cs.read_config())
    if sp_api is None:
        # Operations client: NOT a multicluster client. Operations are
        # forwarded to the subcluster that owns the volume with
        # clusterName="~<clusterID>", which builds a "RemoteCommand/~<id>/<Op>"
        # path that runs the op as a *local* operation on that subcluster.
        #
        # This must be a non-multicluster client on purpose: a multicluster
        # client turns every multiCluster-marked method (volumeRevert,
        # volumesReassignWait, snapshotDelete, ...) into a
        # "RemoteCommand/~<id>/MultiCluster/<Op>" path, which StorPool rejects
        # ("request 'MultiCluster/<Op>' is not supported") -- a forwarded
        # command runs locally on the target and must not carry the
        # MultiCluster/ segment. Verified against a live fir.b/fir.n cluster.
        sp_api = spapi.Api.fromConfig(multiCluster=False)
    if sp_api_mc is None and is_multicluster():
        # Discovery client: a multicluster client is required to read which
        # subcluster a volume currently resides on -- VolumeSummary.clusterId
        # is only populated on MultiCluster/ calls. Used without clusterName
        # (plain "MultiCluster/Volume/<name>") to address volumes globally.
        sp_api_mc = spapi.Api.fromConfig(multiCluster=True)


def read_config():
    global config, config_all
    config_all = confget.read_ini_file(confget.Config(
        [], filename="/etc/storpool/backup-tool.conf"
    ))
    config = config_all[""]


def get_backup_list(
        vm: str, accept_all_clusters: bool = False
) -> Dict[int, Dict[str, Any]]:
    cmd = [
        'storpool_vcctl',
        'status',
    ]
    if is_multicluster():
        cmd.append('-M')
    cmd.append('--json')

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
            backup_cluster_ids = get_backup_cluster_ids(
                vm, accept_all=accept_all_clusters
            )
            return {
                entry["create_ts"]: entry
                for entry in history
                if entry["id"]["location"] in backup_cluster_ids
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


def detach_volumes(
        volume_list: List[Dict[str, Any]],
        cluster_name: Optional[str],
) -> None:
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
    sp_api.volumesReassignWait(args, **sp_cluster_kwargs(cluster_name))


def snapshot_from_remote(
        snapshot_gid: str,
        cluster_name: Optional[str],
        cluster_settings: Dict[str, Any],
) -> None:
    """Pull one backup snapshot into the given subcluster (idempotent)."""
    args = {
        "remoteId": snapshot_gid,
        "remoteLocation": cluster_settings["SP_BACKUP_LOCATION_NAME"],
        "template": cluster_settings["SP_LOCAL_TEMPLATE"],
    }
    try:
        sp_api.snapshotFromRemote(args, **sp_cluster_kwargs(cluster_name))
    except spapi.ApiError as err:
        # A local copy of the snapshot may already be created. This is OK.
        if err.name != "objectExists":
            raise


def copy_snapshots_from_remote(
        volume_list: List[Dict[str, Any]],
        cluster_name: Optional[str],
        cluster_settings: Dict[str, Any],
) -> None:
    logging.debug("Copy snapshots to the local cluster")
    for vol in volume_list:
        snapshot_from_remote(
            vol["sp_snapshot"].lstrip("~"), cluster_name, cluster_settings
        )


def revert_volumes(
        volume_list: List[Dict[str, Any]],
        cluster_name: Optional[str],
        revert_size: bool = False,
) -> None:
    logging.debug("Revert volumes using local snapshots")
    for vol in volume_list:
        volume_name = vol["sp_volume_name"]
        snapshot_name = vol["sp_snapshot"]
        args = {"toSnapshot": snapshot_name}
        if revert_size:
            args["revertSize"] = True
        logging.debug("Revert volume %s to snapshot %s",
            volume_name, snapshot_name)
        sp_api.volumeRevert(
            volume_name, args, **sp_cluster_kwargs(cluster_name)
        )


def delete_local_snapshots(
        volume_list: List[Dict[str, Any]],
        cluster_name: Optional[str],
) -> None:
    logging.debug("Delete snapshots on the local cluster")
    for vol in volume_list:
        sp_api.snapshotDelete(
            vol["sp_snapshot"], **sp_cluster_kwargs(cluster_name)
        )


def revert_vm(backup: Dict[str, Any]) -> None:
    """
    Restores a VM from a backup

    :param backup:
    :return:
    """

    vm_uuid = backup["entity_id"]["name"].split("=", maxsplit=1)[1]
    cluster_name = get_vm_cluster_name(vm_uuid)
    cluster_settings = get_cluster_config(cluster_name)
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
        vol["sp_volume_name"] = sp_volume_name_from_cs_volume(vol)

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

    detach_volumes(volume_list, cluster_name)
    copy_snapshots_from_remote(volume_list, cluster_name, cluster_settings)
    revert_volumes(volume_list, cluster_name)
    delete_local_snapshots(volume_list, cluster_name)

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

    # The server VM's subcluster is where we copy the snapshot to size the new
    # volume. In most deployments the new CloudStack volume lands on this same
    # subcluster; if it does not, we re-resolve and copy again below before the
    # revert.
    cluster_name = get_vm_cluster_name(server)
    cluster_settings = get_cluster_config(cluster_name)

    #
    # copy the snapshot to the server's cluster (to read its size)
    #
    logging.debug("Copy snapshot %s to cluster %s", snapshot_gid, cluster_name)
    snapshot_from_remote(snapshot_gid, cluster_name, cluster_settings)

    snapshot_size = sp_api.snapshotDescribe(
        snapshot_name, **sp_cluster_kwargs(cluster_name)
    ).size
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

    sp_volume_name = sp_volume_name_from_cs_volume(new_cs_volume)

    #
    # The new CloudStack volume may have been placed on a different StorPool
    # subcluster than the server's existing volumes. Re-resolve from the new
    # volume itself, and if it differs make sure the snapshot also exists on
    # that subcluster before reverting (volumeRevert needs the snapshot local
    # to the volume).
    #
    volume_cluster_name = get_cluster_for_sp_volume(sp_volume_name) or cluster_name
    if volume_cluster_name != cluster_name:
        logging.info(
            "New volume %s landed on cluster %s (server is on %s); copying the "
            "snapshot there too", sp_volume_name, volume_cluster_name, cluster_name
        )
        volume_cluster_settings = get_cluster_config(volume_cluster_name)
        snapshot_from_remote(
            snapshot_gid, volume_cluster_name, volume_cluster_settings
        )

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
    sp_api.volumeRevert(
        sp_volume_name, args, **sp_cluster_kwargs(volume_cluster_name)
    )

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
    # delete the temporary snapshot copies on the cluster(s) we pulled them to
    #
    logging.debug("Delete snapshot %s", snapshot_name)
    sp_api.snapshotDelete(snapshot_name, **sp_cluster_kwargs(volume_cluster_name))
    if volume_cluster_name != cluster_name:
        try:
            sp_api.snapshotDelete(
                snapshot_name, **sp_cluster_kwargs(cluster_name)
            )
        except spapi.ApiError as err:
            # The sizing copy on the server's cluster may be absent. This is OK.
            if err.name != "objectDoesNotExist":
                raise


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

    In a StorPool multicluster deployment the restore runs against the
    StorPool subcluster where the target VM's volumes reside (resolved from
    the StorPool volume info, ``VolumeSummary.clusterId``). The target VM
    may therefore live in a different CloudStack zone or cluster than
    the source VM whose backup is being replayed.

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

    target_cluster_name = get_vm_cluster_name(new_vm_uuid)
    target_cluster_settings = get_cluster_config(target_cluster_name)

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
        vol["sp_volume_name"] = sp_volume_name_from_cs_volume(vol)

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

    detach_volumes(volume_list, target_cluster_name)
    copy_snapshots_from_remote(
        volume_list, target_cluster_name, target_cluster_settings
    )
    revert_volumes(volume_list, target_cluster_name, revert_size=True)
    delete_local_snapshots(volume_list, target_cluster_name)

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
        # The source VM may be deleted/migrated or live on a different
        # subcluster than where its backup was written, so accept backups
        # from every configured backup location.
        backup_list = get_backup_list(args.vm_uuid, accept_all_clusters=True)
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
