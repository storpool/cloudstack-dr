Disaster Recovery Script for CloudStack
============================================

Prepares and starts one or more VMs on the DR cluster

Usage:
-------

```commandline
usage: start-vm-on-dr.py [-h] [-v] [-n] [-a] vm [vm ...]

positional arguments:
  vm             List of UUID of VMs to be started

optional arguments:
  -h, --help     show this help message and exit
  -v, --verbose
  -n, --noop     Do nothing. Print the commands only
  -a, --async    Don't wait for async jobs when possible
```

Example
--------

```commandline
$ ./start-vm-on-dr.py -vv 9bdc45c2-6790-4c75-8af6-c9cd35a480a1
DEBUG:root:vc-policy tag found for VM 9bdc45c2-6790-4c75-8af6-c9cd35a480a1: opt1-dr
DEBUG:root:backups found for VM 9bdc45c2-6790-4c75-8af6-c9cd35a480a1
DEBUG:root:The latest backup of VM 9bdc45c2-6790-4c75-8af6-c9cd35a480a1 is 530 minutes old.
DEBUG:root:Create a new volume from snapshot ~bgu4.b.dkt
DEBUG:root:Update path, volume f91e61ed-6b1c-433e-8cbf-3c20f0403584, vol_gid=bgu4.n.gk
DEBUG:root:Create a new volume from snapshot ~bgu4.b.dko
DEBUG:root:Update path, volume 6a718577-5696-42ba-83ee-d34333a482ae, vol_gid=bgu4.n.gm
DEBUG:root:Starting VM 9bdc45c2-6790-4c75-8af6-c9cd35a480a1
INFO:root:VM 9bdc45c2-6790-4c75-8af6-c9cd35a480a1, state Running, on host lab-cs-dev-a2-block-server-mgmt-bridge
INFO:root:0 async jobs started.
```

Installation
---------------

```commandline
pip install cs
pip install storpool
```

Configuration
--------------

Copy `dr.conf` to `/etc/storpool/dr.conf`

Notes:
 - the StorPool API is the API at the DR site.
 - `VC_SSH_HOST` is the host at the DR site with volumecare installed.

The configuration file `cloudstack.ini` must be stored in the current directory from which
the `start-vm-on-dr.py` is started or as `.cloudstack.ini` in the user's home directory.

-----------------------------

Failover procedure
==================

## DR - Switchover

1. Fence hosts at site A - TBD. e.g. IPMI restart
2. Switchover StorPool API to site B. Using GUI or CS API, 
change the configuration parameter of the StorPool **primary storage**:
   ```
   sp.enable.alternative.endpoint = true
   ```
3. Mark all VMs at site A as stopped:
   
   For each vm:
   ```
   stop virtualmachine id=<UUID> forced=true
   ```
4. Mark POD B as Active
5. Destroy System VMs
6. Restart and clean up network - TBD

Note:
  * At step 2 chenge the configuration setting of the primary storage, 
    not the configuration settings with the same name accessible from the 
    global settings list.

## VM Failover

1. Get the list of UUID of the VMs to be moved to the DR site. Every VM in the list 
    shall have its volumes snapshoted at the DR cluster. This is done by VolumeCare if 
    the VM has a valid tag `vc-policy=<policy name>`.
2. Group the VMs based on the required start order.
3. For each group execute the command:
   ```
   start-vm-on-dr.py [-v] [-a] vm [vm ...]
   ```


### Summary of the script

The script executes the following actions for each VM in the list:
   1. get the list of all volumes attached to the VM 
   2. get the list of all snapshots from the latest backup of the VM
   3. verifies there is a snapshot for each volume
   4. For each volume:
      1. Creates a new volume in StorPool from the snapshot
      2. updates the CloudStack volume to point to the newly created volume
   5. Start the VM

If the script is started with `--async` option it doesn't wait the VM to start 
before proceeding with the next VM in the list.

Failback Procedure
===================

Note:
* This procedure is given as a guideline only. It needs to be tested and confirmed.


1. Restore the StorPool cluster at main site 
2. Restore StorPool bridge
3. Clean up the storage cluster at site A from all old volumes remained
    after the VM failover process. **TBD**.
4. Make sure there are no running VMs on the hypervisors at the main site
5. Reconnect hosts at the main site to the CloudStack management server
6. Enable Pod A
7. Restart Network / migrate virtual routers / **TBD**
8. Switch StorPool API to site A. Change the setting of the primary storage  
   `sp.enable.alternative.endpoint = false`
9. Disable Pod B
10. Live migrate VMs one-by-one to site A
